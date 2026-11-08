"""OpenAI Chat Completions adapter (the project's one LLM provider). The orchestrator depends only on the
`complete()` shape below, so tests swap in a scripted model and no API key is needed to test the loop."""
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


class OpenAILLM:
    def __init__(self, model: str | None = None, api_key: str | None = None, temperature: float | None = None,
                 timeout: float = 60.0, client=None):
        self.model = model or os.environ.get("OPENAI_MODEL")
        if not self.model:
            raise ValueError("OPENAI_MODEL is not set (see .env.example)")
        self.temperature = temperature if temperature is not None else (
            float(os.environ["OPENAI_TEMPERATURE"]) if os.environ.get("OPENAI_TEMPERATURE") else None)
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
        kwargs = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if self.temperature is not None:        # optional: some models reject a temperature setting
            kwargs["temperature"] = self.temperature
        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as exc:
            raise LLMUnavailable("llm_auth_error", f"{type(exc).__name__} {exc.status_code}") from exc
        except openai.RateLimitError as exc:
            raise LLMUnavailable("llm_rate_limited", f"{type(exc).__name__} {exc.status_code}") from exc
        except openai.BadRequestError as exc:
            raise LLMUnavailable("llm_bad_request", f"{type(exc).__name__} {exc.status_code}") from exc
        except openai.OpenAIError as exc:
            raise LLMUnavailable("llm_unavailable", type(exc).__name__) from exc
        msg = resp.choices[0].message
        calls = [tc for tc in (msg.tool_calls or []) if getattr(tc, "type", "function") == "function"]
        assistant: dict = {"role": "assistant", "content": msg.content}
        if calls:
            assistant["tool_calls"] = [{"id": tc.id, "type": "function",
                                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                       for tc in calls]
        usage = {"prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                 "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0}
        return LLMResponse(msg.content, [ToolCall(tc.id, tc.function.name, _args(tc.function.arguments)) for tc in calls],
                           assistant, usage)
