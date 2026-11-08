"""OpenAI adapter tests with a fake SDK client (no network)."""
import asyncio
from types import SimpleNamespace as NS

import httpx2 as httpx
import openai
import pytest

from agent.llm import LLMUnavailable, OpenAILLM


def resp(text=None, calls=(), usage=(7, 3, 2)):
    out = ([NS(type="reasoning")] + [NS(type="function_call", call_id=i, name=n, arguments=a) for i, n, a in calls]
           + ([NS(type="message", content=[NS(type="output_text", text=text)])] if text is not None else []))
    return NS(output=out, usage=NS(input_tokens=usage[0], output_tokens=usage[1], output_tokens_details=NS(reasoning_tokens=usage[2])))


def fake_client(response=None, raises=None):
    async def create(**kwargs):
        create.kwargs = kwargs
        if raises:
            raise raises
        return response
    return NS(responses=NS(create=create)), create


def run(llm, messages=None, **kw):
    return asyncio.run(llm.complete(messages or [{"role": "user", "content": "q"}], **kw))


TOOL = {"type": "function", "function": {"name": "get_tasks", "description": "d", "parameters": {"type": "object", "properties": {}}}}


def test_parses_tool_calls_text_and_usage_including_reasoning():
    client, create = fake_client(resp(calls=[("a", "get_pipeline_analytics", '{"role": "ROLE-BE"}'), ("b", "x", "{oops")]))
    r = run(OpenAILLM("m", client=client), tools=[TOOL])
    assert [(c.id, c.name, c.arguments) for c in r.tool_calls] == [("a", "get_pipeline_analytics", {"role": "ROLE-BE"}), ("b", "x", None)]
    assert r.message["tool_calls"][0]["function"]["arguments"] == '{"role": "ROLE-BE"}'     # stored unchanged, chat-shaped
    assert r.usage == {"prompt_tokens": 7, "completion_tokens": 3, "reasoning_tokens": 2}
    assert create.kwargs["model"] == "m" and create.kwargs["reasoning"] == {"effort": "low"} and create.kwargs["store"] is False
    assert create.kwargs["tools"] == [{"type": "function", "name": "get_tasks", "description": "d",
                                       "parameters": {"type": "object", "properties": {}}, "strict": False}]
    assert "temperature" not in create.kwargs


def test_chat_shaped_history_is_converted_to_response_items():
    history = [{"role": "system", "content": "S"}, {"role": "user", "content": "q"},
               {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_tasks", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c1", "content": '{"data": 1}'}, {"role": "assistant", "content": "done"}]
    client, create = fake_client(resp(text="ok"))
    run(OpenAILLM("m", client=client), history)
    assert create.kwargs["input"] == [{"role": "system", "content": "S"}, {"role": "user", "content": "q"},
                                      {"type": "function_call", "call_id": "c1", "name": "get_tasks", "arguments": "{}"},
                                      {"type": "function_call_output", "call_id": "c1", "output": '{"data": 1}'},
                                      {"role": "assistant", "content": "done"}]


def test_plain_answer_optional_temperature_and_reasoning_off():
    client, create = fake_client(resp(text="hello"))
    r = run(OpenAILLM("m", temperature=0.2, reasoning_effort="none", client=client))
    assert r.content == "hello" and r.tool_calls == [] and r.message == {"role": "assistant", "content": "hello"}
    assert create.kwargs["temperature"] == 0.2 and "tools" not in create.kwargs and "reasoning" not in create.kwargs


def test_only_function_calls_become_tool_calls():
    client, _ = fake_client(NS(output=[NS(type="web_search_call"), NS(type="message", content=[NS(type="output_text", text="x")])], usage=None))
    r = run(OpenAILLM("m", client=client))
    assert r.tool_calls == [] and r.content == "x" and r.usage["prompt_tokens"] == 0


def _status_error(cls, status):
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return cls("boom sk-secret", response=httpx.Response(status, request=req), body=None)


@pytest.mark.parametrize("exc,code", [
    (_status_error(openai.AuthenticationError, 401), "llm_auth_error"),
    (_status_error(openai.RateLimitError, 429), "llm_rate_limited"),
    (_status_error(openai.BadRequestError, 400), "llm_bad_request"),
    (openai.APIConnectionError(request=httpx.Request("POST", "https://x")), "llm_unavailable")])
def test_api_errors_map_to_stable_codes_without_leaking_messages(exc, code):
    client, _ = fake_client(raises=exc)
    with pytest.raises(LLMUnavailable) as e:
        run(OpenAILLM("m", client=client))
    assert e.value.code == code and "sk-secret" not in e.value.detail


def test_model_must_be_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(ValueError):
        OpenAILLM(client=object())


def test_a_fresh_sdk_client_is_used_for_each_event_loop(monkeypatch):
    """Regression: a client cached across asyncio.run() calls fails with 'Event loop is closed' on the 2nd turn."""
    made = []

    class FakeAsyncOpenAI:
        def __init__(self, **kw):
            made.append(self)
            self.responses = NS(create=self.create)

        async def create(self, **kwargs):
            return resp(text="ok")
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    llm = OpenAILLM("m", api_key="k")

    async def two_rounds():
        await llm.complete([{"role": "user", "content": "a"}])
        await llm.complete([{"role": "user", "content": "b"}])
    asyncio.run(two_rounds())
    assert len(made) == 1                      # reused across the rounds of one turn
    asyncio.run(two_rounds())
    assert len(made) == 2                      # new loop (next turn) -> new client


# ---- the empty reply observed live (no text, no tool call): the adapter passes it through and the orchestrator nudges once
def test_the_real_adapter_and_orchestrator_recover_from_an_empty_reply():
    from agent.orchestrator import Orchestrator
    from tests.test_agent import FakeSession
    from tests.test_executor import FakeClient
    replies = [resp(text=None), resp(text="Recovered answer.")]

    async def create(**kwargs):
        return replies.pop(0)
    client = NS(responses=NS(create=create))
    r = asyncio.run(OpenAILLM("m", client=client).complete([{"role": "user", "content": "q"}]))
    assert r.content is None and r.tool_calls == [] and r.usage["completion_tokens"] == 3
    replies.insert(0, resp(text=None))
    out = Orchestrator(OpenAILLM("m", client=client), session_factory=lambda: FakeSession(FakeClient())).run_turn("q")
    assert out.status == "ok" and out.answer == "Recovered answer." and out.empty_retries == 1
