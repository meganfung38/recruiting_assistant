"""OpenAI adapter tests with a fake SDK client (no network)."""
import asyncio
from types import SimpleNamespace as NS

import httpx2 as httpx
import openai
import pytest

from agent.llm import LLMUnavailable, OpenAILLM


def fake_client(message=None, raises=None):
    async def create(**kwargs):
        create.kwargs = kwargs
        if raises:
            raise raises
        return NS(choices=[NS(message=message)], usage=NS(prompt_tokens=7, completion_tokens=3))
    return NS(chat=NS(completions=NS(create=create))), create


def tc(i, name, args, typ="function"):
    return NS(id=i, type=typ, function=NS(name=name, arguments=args))


def run(llm, **kw):
    return asyncio.run(llm.complete([{"role": "user", "content": "q"}], **kw))


def test_parses_text_tool_calls_and_usage():
    client, create = fake_client(NS(content=None, tool_calls=[tc("a", "get_pipeline_analytics", '{"role": "ROLE-BE"}'), tc("b", "x", "{oops")]))
    r = run(OpenAILLM("m", client=client), tools=[{"type": "function"}])
    assert [(c.id, c.name, c.arguments) for c in r.tool_calls] == [("a", "get_pipeline_analytics", {"role": "ROLE-BE"}), ("b", "x", None)]
    assert r.message["tool_calls"][0]["function"]["arguments"] == '{"role": "ROLE-BE"}'     # sent back unchanged
    assert r.usage == {"prompt_tokens": 7, "completion_tokens": 3}
    assert create.kwargs["model"] == "m" and "temperature" not in create.kwargs and create.kwargs["tools"]


def test_plain_answer_and_optional_temperature():
    client, create = fake_client(NS(content="hello", tool_calls=None))
    r = run(OpenAILLM("m", temperature=0.2, client=client))
    assert r.content == "hello" and r.tool_calls == [] and r.message == {"role": "assistant", "content": "hello"}
    assert create.kwargs["temperature"] == 0.2 and "tools" not in create.kwargs


def test_non_function_tool_calls_are_ignored():
    client, _ = fake_client(NS(content="x", tool_calls=[tc("a", "custom", "{}", typ="custom")]))
    assert run(OpenAILLM("m", client=client)).tool_calls == []


def _status_error(cls, status):
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
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
            self.chat = NS(completions=NS(create=self.create))

        async def create(self, **kwargs):
            return NS(choices=[NS(message=NS(content="ok", tool_calls=None))], usage=None)
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    llm = OpenAILLM("m", api_key="k")

    async def two_rounds():
        await llm.complete([{"role": "user", "content": "a"}])
        await llm.complete([{"role": "user", "content": "b"}])
    asyncio.run(two_rounds())
    assert len(made) == 1                      # reused across the rounds of one turn
    asyncio.run(two_rounds())
    assert len(made) == 2                      # new loop (next turn) -> new client


# ---- the empty completion observed live: finish_reason=stop, content='', no tool calls, 3 completion tokens
def test_the_real_adapter_and_orchestrator_recover_from_the_observed_empty_completion():
    from agent.orchestrator import Orchestrator
    from tests.test_agent import FakeSession
    from tests.test_executor import FakeClient
    replies = [NS(content="", tool_calls=None, refusal=None), NS(content="Recovered answer.", tool_calls=None, refusal=None)]

    async def create(**kwargs):
        return NS(choices=[NS(message=replies.pop(0), finish_reason="stop")], usage=NS(prompt_tokens=4474, completion_tokens=3))
    client = NS(chat=NS(completions=NS(create=create)))
    r = asyncio.run(OpenAILLM("m", client=client).complete([{"role": "user", "content": "q"}]))
    assert r.content == "" and r.tool_calls == [] and r.usage["completion_tokens"] == 3         # the adapter passes the emptiness through
    replies.insert(0, NS(content="", tool_calls=None, refusal=None))
    out = Orchestrator(OpenAILLM("m", client=client), session_factory=lambda: FakeSession(FakeClient())).run_turn("q")
    assert out.status == "ok" and out.answer == "Recovered answer." and out.empty_retries == 1
