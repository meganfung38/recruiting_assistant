"""OpenAI Responses API adapter (the project's one LLM provider). The orchestrator depends only on the `complete()` shape below,
so tests swap in a scripted model and no API key is needed to test the loop.

Why the Responses API: with function tools, Chat Completions only allows reasoning_effort "none" for gpt-5.4-mini, so the agent could
not reason at all (it misread which of 3, 4 and 5 was largest). The conversation is still kept in the Chat Completions message shape
(system/user/assistant with tool_calls/tool), which the orchestrator stores; this adapter converts it on every call. Requests are
stateless (store=False): the full context is sent each round, as before.
"""
import asyncio
import json
import os
from dataclasses import dataclass, field

import openai

from agent.tool_executor import ToolCall


class LLMUnavailable(Exception):
    """The model API could not be used. `code` is stable; `detail` is the exception class and HTTP status only."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    message: dict                       # the assistant message to append to the conversation, as sent back to the API
    usage: dict = field(default_factory=dict)


def _args(raw: str | None):
    """Tool-call arguments arrive as a JSON string; anything unparseable becomes None (-> invalid_arguments)."""
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _input_items(messages: list[dict]) -> list[dict]:
    """Chat-shaped messages -> Responses API input items."""
    items: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m.get("content") or ""})
        elif role == "assistant" and m.get("tool_calls"):
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            items += [{"type": "function_call", "call_id": tc["id"], "name": tc["function"]["name"],
                       "arguments": tc["function"]["arguments"] or "{}"} for tc in m["tool_calls"]]
        else:
            items.append({"role": role, "content": m.get("content") or ""})
    return items


def _tools(tools: list[dict]) -> list[dict]:
    """Chat-style {"type": "function", "function": {...}} -> Responses-style flat function tools (non-strict: optional args exist)."""
    return [{"type": "function", "name": t["function"]["name"], "description": t["function"].get("description", ""),
             "parameters": t["function"]["parameters"], "strict": False} for t in tools if t.get("type") == "function"]


class OpenAILLM:
    def __init__(self, model: str | None = None, api_key: str | None = None, temperature: float | None = None,
                 timeout: float = 20.0, client=None, reasoning_effort: str | None = None):
        # A normal round takes under 5 s. 20 s per request lets the SDK's automatic retry recover from a stalled request (one took
        # 61.6 s for 57 tokens at a 60 s timeout) while staying inside the 90 s turn limit.
        self.model = model or os.environ.get("OPENAI_MODEL")
        if not self.model:
            raise ValueError("OPENAI_MODEL is not set (see .env.example)")
        self.temperature = temperature if temperature is not None else (
            float(os.environ["OPENAI_TEMPERATURE"]) if os.environ.get("OPENAI_TEMPERATURE") else None)
        # "low" by default: enough to compare numbers and follow the answer rules; "none" turns reasoning off.
        self.reasoning_effort = reasoning_effort or os.environ.get("OPENAI_REASONING_EFFORT") or "low"
        self._injected, self._api_key, self._timeout = client, api_key or os.environ.get("OPENAI_API_KEY"), timeout
        self._client, self._loop = None, None

    @property
    def client(self):
        """The SDK client's connections belong to the event loop that opened them. Each assistant turn runs in
        its own asyncio.run(), so a client is created per loop (reused across the rounds of one turn)."""
        if self._injected is not None:
            return self._injected
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client, self._loop = openai.AsyncOpenAI(api_key=self._api_key, timeout=self._timeout), loop
        return self._client

    async def aclose(self) -> None:
        """Close the SDK client opened in the current loop (called at the end of each turn)."""
        client, self._client, self._loop = self._client, None, None
        if client is not None:
            await client.close()

    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        kwargs = {"model": self.model, "input": _input_items(messages), "store": False}
        if self.reasoning_effort != "none":
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        if tools:
            kwargs["tools"] = _tools(tools)
        if self.temperature is not None:        # optional: reasoning models usually reject a temperature setting
            kwargs["temperature"] = self.temperature
        try:
            resp = await self.client.responses.create(**kwargs)
        except openai.AuthenticationError as exc:
            raise LLMUnavailable("llm_auth_error", f"{type(exc).__name__} {exc.status_code}") from exc
        except openai.RateLimitError as exc:
            raise LLMUnavailable("llm_rate_limited", f"{type(exc).__name__} {exc.status_code}") from exc
        except openai.BadRequestError as exc:
            raise LLMUnavailable("llm_bad_request", f"{type(exc).__name__} {exc.status_code}") from exc
        except openai.OpenAIError as exc:
            raise LLMUnavailable("llm_unavailable", type(exc).__name__) from exc
        calls = [o for o in (resp.output or []) if getattr(o, "type", None) == "function_call"]
        text = "".join(c.text for o in (resp.output or []) if getattr(o, "type", None) == "message"
                       for c in (o.content or []) if getattr(c, "type", None) == "output_text") or None
        assistant: dict = {"role": "assistant", "content": text}
        if calls:
            assistant["tool_calls"] = [{"id": c.call_id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                                       for c in calls]
        u = getattr(resp, "usage", None)
        details = getattr(u, "output_tokens_details", None)
        usage = {"prompt_tokens": getattr(u, "input_tokens", 0) or 0, "completion_tokens": getattr(u, "output_tokens", 0) or 0,
                 "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0}
        return LLMResponse(text, [ToolCall(c.call_id, c.name, _args(c.arguments)) for c in calls], assistant, usage)
