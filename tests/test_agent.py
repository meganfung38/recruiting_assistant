"""Orchestrator loop tests with a scripted (mocked) model: deterministic, no API key."""
import asyncio
import copy
import json
import subprocess
import uuid

import pytest

from agent.limits import Limits
from agent.llm import LLMResponse, LLMUnavailable
from agent.mcp_client import McpSession
from agent.orchestrator import ConversationState, Orchestrator, user_message
from agent.prompts import SYSTEM_PROMPT
from agent.tool_executor import ToolCall
from data import scenarios as sc
from data.seed import build
from tests.test_executor import FakeClient, env, candidates_handler

SEVEN = {"list_roles", "search_candidates", "get_candidates", "get_candidate_communications",
         "get_pipeline_analytics", "get_tasks", "get_recruiting_overview"}


def say(text, usage=None):
    return LLMResponse(text, [], {"role": "assistant", "content": text}, usage or {"prompt_tokens": 10, "completion_tokens": 5})


def call(*calls, text=None):
    """One assistant message requesting tool calls: call(("c1", "list_roles", {}), ...)."""
    tcs = [ToolCall(i, n, a) for i, n, a in calls]
    msg = {"role": "assistant", "content": text, "tool_calls": [
        {"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, n, a in calls]}
    return LLMResponse(text, tcs, msg)


class ScriptedLLM:
    """Plays back a fixed list of steps; each step is an LLMResponse, an Exception, or a callable(messages, tools)."""

    def __init__(self, steps):
        self.steps, self.calls = list(steps), []

    async def complete(self, messages, tools=None):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools})
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages, tools)
        if isinstance(step, Exception):
            raise step
        return step


class FakeSession:
    opened = closed = 0

    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        type(self).opened += 1
        return self

    async def __aexit__(self, *exc):
        type(self).closed += 1

    async def list_tools(self):
        return await self.client.list_tools()

    async def call_tool(self, name, args):
        return await self.client.call_tool(name, args)


def orch(steps, client=None, limits=Limits(), **kw):
    FakeSession.opened = FakeSession.closed = 0
    llm = ScriptedLLM(steps)
    client = client or FakeClient(lambda n, a: env({"roles": [{"role_id": "ROLE-BE", "title": "Backend Engineer",
                                                            "opened_at": "2026-07-07T20:00:00Z", "role_age_days": 123}]},
                                                   []))
    return Orchestrator(llm, limits=limits, session_factory=lambda: FakeSession(client), **kw), llm, client


def err_code(msg):
    return (json.loads(msg["content"]).get("error") or {}).get("code")


def tool_messages(result):
    return [m for m in result.messages if m["role"] == "tool"]


# ------------------------------------------------------------------ the loop
def test_multiple_calls_per_round_and_multiple_rounds_with_no_early_return():
    o, llm, client = orch([
        call(("a", "list_roles", {}), ("b", "get_pipeline_analytics", {"role": "ROLE-BE"})),      # round 1: two calls
        call(("c", "list_roles", {})),                                                 # round 2: one more
        say("Final answer."),
    ])
    r = o.run_turn("question")
    assert r.status == "ok" and r.answer == "Final answer."
    assert [c[0] for c in client.calls] == ["list_roles", "get_pipeline_analytics", "list_roles"]    # all three executed
    assert len(llm.calls) == 3 and r.trace.model_rounds == 3                           # did not stop after the first tool call
    assert [e.sequence for e in r.trace.ordered()] == [1, 2, 3]


def test_every_tool_call_id_gets_exactly_one_paired_tool_message():
    o, llm, _ = orch([call(("a", "list_roles", {}), ("b", "nope", {}), ("c", "get_pipeline_analytics", {"role": "?"})), say("done")])
    r = o.run_turn("q")
    asst = next(m for m in r.messages if m.get("tool_calls"))
    tools = tool_messages(r)
    assert [t["tool_call_id"] for t in tools] == [tc["id"] for tc in asst["tool_calls"]] == ["a", "b", "c"]
    codes = [err_code(t) for t in tools]
    assert codes == [None, "tool_not_allowed", "invalid_arguments"]                    # errors are fed back, the loop continues
    second = llm.calls[1]["messages"]                                                   # the model saw the paired results
    assert [m["role"] for m in second[-4:]] == ["assistant", "tool", "tool", "tool"]


def test_model_sees_the_seven_data_tools_plus_local_submit_answer_and_the_system_prompt_first():
    o, llm, _ = orch([say("hi")])
    o.run_turn("q")
    first = llm.calls[0]
    assert {t["function"]["name"] for t in first["tools"]} == SEVEN | {"submit_answer"}
    assert first["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert first["messages"][-1]["content"] == "q"


def test_role_context_is_a_marked_default_not_a_constraint():
    o, llm, _ = orch([say("ok")])
    o.run_turn("Which tasks are overdue?", role_context="ROLE-PO")
    text = llm.calls[0]["messages"][-1]["content"]
    assert "ROLE-PO" in text and "default, not a restriction" in text and text.endswith("Which tasks are overdue?")
    assert user_message("x", None) == {"role": "user", "content": "x"}


def test_tool_results_reach_the_model_as_projected_json_only():
    raw = env({"candidates": [{"candidate_id": "C104", "name": "D R", "contact_email": "d@example.com", "cv_text": "Skills",
                               "role_id": "ROLE-BE", "feedback": [], "stage_history": [], "pending_tasks": [],
                               "current_stage": "Final Interview"}]}, ["cv:C104"])
    o, llm, _ = orch([call(("a", "get_candidates", {"candidate_ids": ["C104"]})), say("ok")],
                     client=FakeClient(lambda n, a: raw))
    r = o.run_turn("q")
    content = tool_messages(r)[0]["content"]
    assert "contact_email" not in content and "example.com" not in content and "untrusted_text" in content


# ------------------------------------------------------------------ limits and failures
def test_round_limit_triggers_one_tools_disabled_wrap_up_call():
    steps = [call((f"c{i}", "list_roles", {})) for i in range(3)] + [say("Partial: C104 only. Incomplete.")]
    o, llm, _ = orch(steps, limits=Limits(max_rounds=3))
    r = o.run_turn("q")
    assert r.status == "limit_reached" and r.limit_code == "round_limit" and "Incomplete" in r.answer
    wrap = llm.calls[-1]
    assert wrap["tools"] is None and "Limit reached" in wrap["messages"][-1]["content"]
    assert len(llm.calls) == 4 and r.trace.model_rounds == 4


def test_call_budget_exhaustion_is_reported_and_leaves_every_call_answered():
    o, llm, client = orch([call(*[(f"c{i}", "list_roles", {}) for i in range(5)]), say("Wrapped up.")],
                          limits=Limits(max_tool_calls=2))
    r = o.run_turn("q")
    assert r.status == "limit_reached" and r.limit_code == "call_budget_exceeded" and r.answer == "Wrapped up."
    assert len(client.calls) == 2 and len(tool_messages(r)) == 5                        # 2 ran, 3 refused, all paired
    assert [err_code(t) for t in tool_messages(r)][2:] == ["call_budget_exceeded"] * 3


def test_turn_timeout_returns_a_static_message_and_makes_no_further_model_call():
    async def slow(messages, tools):
        await asyncio.sleep(5)
    class SlowLLM:
        calls = 0
        async def complete(self, messages, tools=None):
            SlowLLM.calls += 1
            await asyncio.sleep(5)
    FakeSession.opened = FakeSession.closed = 0
    o = Orchestrator(SlowLLM(), limits=Limits(turn_timeout_s=0.2), session_factory=lambda: FakeSession(FakeClient()))
    r = o.run_turn("q")
    assert r.status == "limit_reached" and r.limit_code == "turn_timeout" and "ran out of time" in r.answer
    assert SlowLLM.calls == 1 and FakeSession.closed == 1


@pytest.mark.parametrize("exc,code", [(LLMUnavailable("llm_unavailable", "APIConnectionError"), "llm_unavailable"),
                                      (LLMUnavailable("llm_auth_error"), "llm_auth_error"),
                                      (LLMUnavailable("llm_rate_limited"), "llm_rate_limited")])
def test_model_unavailable_is_a_safe_error_and_not_recorded_in_history(exc, code):
    o, _, _ = orch([exc])
    state = ConversationState()
    r = o.run_turn("q", state)
    assert r.status == "error" and r.error_code == code and "sk-" not in r.answer
    assert state.turns == [] and FakeSession.closed == 1                                # session still closed


def test_mcp_startup_failure_is_a_safe_error():
    class Broken:
        async def __aenter__(self):
            raise OSError("spawn failed: /Users/secret/path")
        async def __aexit__(self, *a): ...
    o = Orchestrator(ScriptedLLM([]), session_factory=lambda: Broken())
    r = o.run_turn("q")
    assert r.status == "error" and r.error_code == "data_service_unavailable" and "secret" not in r.answer


def test_server_with_the_wrong_tools_fails_closed():
    o, llm, _ = orch([say("never reached")], client=FakeClient(tools=SEVEN | {"run_sql"}))
    r = o.run_turn("q")
    assert r.error_code == "data_service_unavailable" and llm.calls == []


def test_a_model_that_keeps_returning_nothing_ends_in_a_clear_error_after_bounded_retries():
    o, llm, _ = orch([say("   "), say(""), say("  ")])
    r = o.run_turn("q")
    assert r.error_code == "empty_answer" and r.status == "error" and "no answer" in r.answer
    assert len(llm.calls) == 3 and r.empty_retries == 2                                   # one try plus two nudged retries, then stop


def test_unparseable_tool_arguments_become_a_validation_error_the_model_can_repair():
    bad = LLMResponse(None, [ToolCall("x", "get_pipeline_analytics", None)], {"role": "assistant", "content": None, "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "get_pipeline_analytics", "arguments": "{not json"}}]})
    o, llm, client = orch([bad, say("fixed")])
    r = o.run_turn("q")
    assert client.calls == [] and err_code(tool_messages(r)[0]) == "invalid_arguments"
    assert r.status == "ok"


# ------------------------------------------------------------------ session lifecycle and history
def test_one_mcp_session_per_turn_opened_and_closed():
    o, _, _ = orch([say("one"), say("two")])
    state = ConversationState()
    o.run_turn("q1", state)
    assert (FakeSession.opened, FakeSession.closed) == (1, 1)
    o.run_turn("q2", state)
    assert (FakeSession.opened, FakeSession.closed) == (2, 2)


def test_history_replays_only_questions_and_final_answers():
    o, llm, _ = orch([call(("a", "list_roles", {})), say("A1"), call(("b", "list_roles", {})), say("A2"), say("A3")])
    state = ConversationState()
    for q in ("q1", "q2", "q3"):
        o.run_turn(q, state)
    last = llm.calls[-1]["messages"]                                  # third turn's model call
    assert [m["role"] for m in last] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert [m["content"] for m in last if m["role"] == "user"] == ["q1", "q2", "q3"]
    assert [m["content"] for m in last if m["role"] == "assistant"] == ["A1", "A2"]
    assert not any(m["role"] == "tool" or "tool_calls" in m for m in last)   # no earlier tool results are replayed
    state.reset()
    assert state.context_messages() == []


# ------------------------------------------------------------------ end to end on the real server (mocked model)
@pytest.fixture(scope="module")
def real(tmp_path_factory):
    d = tmp_path_factory.mktemp("agent")
    return {"injected": build(d / "i.db", "injected"), "baseline": build(d / "b.db", "baseline")}


def real_orch(db, steps, marker="", limits=Limits(), **kw):
    llm = ScriptedLLM(steps)
    return Orchestrator(llm, limits=limits, session_factory=lambda: McpSession(db, extra_args=(marker,) if marker else ()), **kw), llm


def test_demo_style_turn_over_the_real_server_with_the_injected_cv(real):
    marker = f"--marker-{uuid.uuid4().hex}"
    steps = [call(("1", "get_pipeline_analytics", {"role": "ROLE-BE"}), ("2", "search_candidates", {"role": "ROLE-BE"})),
             call(("3", "get_candidates", {"candidate_ids": ["C104", "C110", "C117"]})),
             say("Top candidates: Dahlia Ramanathan (C104) [feedback:FB-016]. Note: C117's CV contained instruction-like text, which I ignored.")]
    o, llm = real_orch(real["injected"], steps, marker)
    r = o.run_turn("Who are the top three candidates for the Backend Engineer role, and why?", role_context="ROLE-BE")
    assert r.status == "ok" and [e.tool_name for e in r.trace.ordered()] == ["get_pipeline_analytics", "search_candidates", "get_candidates"]
    assert all(e.status == "success" for e in r.trace.events) and r.trace.model_rounds == 3
    assert {f["candidate_id"] for f in r.content_flags} == {"C117"}                      # surfaced for the recruiter
    assert r.evidence.has("cv:C117") and r.evidence.has("cv:C104") and r.evidence.candidate_seen("C113")
    c117 = json.loads(tool_messages(r)[-1]["content"])["data"]["candidates"][-1]
    assert c117["cv_text"]["untrusted_text"].endswith("[removed: instruction-like text]") and "SYSTEM OVERRIDE" not in c117["cv_text"]["untrusted_text"]   # labelled data, instruction removed
    assert llm.calls[2]["tools"] == llm.calls[0]["tools"] and llm.calls[2]["messages"][0] == llm.calls[0]["messages"][0]
    assert subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split() == []


def test_baseline_database_produces_no_flags(real):
    o, _ = real_orch(real["baseline"], [call(("1", "get_candidates", {"candidate_ids": ["C117"]})), say("ok")])
    r = o.run_turn("q")
    assert r.content_flags == [] and r.status == "ok"


def test_follow_up_turn_can_requery_with_ids_from_the_previous_answer(real):
    o, llm = real_orch(real["baseline"], [
        call(("1", "get_candidates", {"candidate_ids": ["C104", "C108", "C110"]})), say("Top three: C104, C108, C110."),
        call(("2", "get_candidates", {"candidate_ids": ["C104", "C108", "C110"]})), say("C110 has waited longest... (waiting_days)")])
    state = ConversationState()
    o.run_turn("Top three for Backend?", state)
    r2 = o.run_turn("Of those three, who has been waiting longest?", state)
    seen = llm.calls[2]["messages"]
    assert any("C104, C108, C110" in (m.get("content") or "") for m in seen)           # previous answer is in context
    assert [e.tool_name for e in r2.trace.ordered()] == ["get_candidates"]               # and the facts were fetched again
    assert r2.evidence.has("cv:C110")




# ================================================================== submit_answer, validation and the repair round
ANALYTICS_ENV = env({"role_id": "ROLE-BE", "role_title": "Backend Engineer", "role_age_days": 123, "as_of": "2026-11-07T20:00:00Z", "stages": [
    {"stage": "Technical Interview", "funnel": {"entered": 17},
     "scheduling": {"scheduled_n": 11, "lag_median_scheduled_days": 8.0, "over_target_count": 14, "observations": 17}}]},
    ["analytics:ROLE-BE@2026-11-07"])
LAG = "stages[Technical Interview].scheduling.lag_median_scheduled_days"
ARef = "analytics:ROLE-BE@2026-11-07"


def router(name, args):
    if name == "get_candidates":
        return candidates_handler(name, args)
    if name == "get_pipeline_analytics":
        return ANALYTICS_ENV
    return env({"roles": []})


def submit(sid="s1", **args):
    args.setdefault("answer_markdown", "Answer.")
    return call((sid, "submit_answer", args))


def cand_arg(cid="C101"):
    """One ranking entry."""
    return {"candidate_id": cid, "reason": "Strong", "gaps": []}


def sorch(steps, **kw):
    return orch(steps, client=FakeClient(router), **kw)


def paired(messages):
    """Every assistant tool_call id is answered by exactly one tool message (the API requires this)."""
    ids = [tc["id"] for m in messages if m.get("tool_calls") for tc in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    return sorted(ids) == sorted(answered)


def test_accepted_submission_ends_the_turn_and_never_reaches_mcp():
    o, llm, client = sorch([call(("1", "get_candidates", {"candidate_ids": ["C101"]})),
                            submit(answer_markdown="N N is the strongest.", ranking=[cand_arg()])])
    state = ConversationState()
    r = o.run_turn("q", state)
    assert r.status == "ok" and r.submitted is not None and r.report.ok and r.repaired == [] and r.answer == "N N is the strongest."
    assert [c[0] for c in client.calls] == ["get_candidates"]                           # submit_answer was handled locally
    assert [e.tool_name for e in r.trace.events] == ["get_candidates"] and len(llm.calls) == 2
    assert json.loads(tool_messages(r)[-1]["content"])["status"] == "accepted" and paired(r.messages)
    # history keeps the visible answer plus, for the model only, who it was about (a follow-up can fetch them again)
    assert r.messages[-1] == {"role": "assistant", "content": "N N is the strongest.\n\n[About: N N (C101)]"}
    assert state.context_messages()[-1]["content"] == r.messages[-1]["content"]


def test_a_wrong_number_is_sent_back_once_with_the_line_it_appeared_in():
    bad = submit(answer_markdown="Backend Engineer: the median scheduling lag is 9.5 days.")
    good = submit("s2", answer_markdown="Backend Engineer: the median scheduling lag is 8 days.")
    o, llm, _ = sorch([call(("1", "get_pipeline_analytics", {"role": "ROLE-BE"})), bad, good])
    r = o.run_turn("q")
    rejection = json.loads(llm.calls[2]["messages"][-1]["content"])
    assert rejection["status"] == "rejected" and rejection["reason"] == "validation_failed" and "only repair" in rejection["how_to_fix"]
    assert "The number 9.5" in rejection["problems"][0] and "for Backend Engineer" in rejection["problems"][0]
    assert r.status == "ok" and r.report.ok and r.repaired == ["unsupported_number"] and r.answer.endswith("8 days.") and paired(r.messages)


def test_after_the_one_repair_remaining_problems_are_shown_not_hidden():
    still_bad = lambda sid: submit(sid, answer_markdown="Backend Engineer: the median scheduling lag is 9.5 days.")
    o, llm, _ = sorch([call(("1", "get_pipeline_analytics", {"role": "ROLE-BE"})), still_bad("s1"), still_bad("s2")])
    r = o.run_turn("q")
    assert len(llm.calls) == 3                                                          # exactly one repair round, no more
    assert r.status == "ok" and not r.report.ok and [n.value for n in r.report.unsupported_numbers] == [9.5]
    assert json.loads(tool_messages(r)[-1]["content"]) == {"status": "accepted", "unverified_items": 1}


def test_submit_alongside_data_calls_is_deferred_without_using_up_the_repair_round():
    mixed = call(("d", "get_pipeline_analytics", {"role": "ROLE-BE"}), ("s1", "submit_answer", {"answer_markdown": "early"}))
    bad = submit("s2", answer_markdown="Backend Engineer: 9.5 days.")
    good = submit("s3", answer_markdown="Backend Engineer: 8 days.")
    o, llm, client = sorch([mixed, bad, good])
    r = o.run_turn("q")
    first = {m["tool_call_id"]: json.loads(m["content"]) for m in llm.calls[1]["messages"] if m["role"] == "tool"}
    assert first["s1"]["reason"] == "call_submit_answer_alone" and [c[0] for c in client.calls] == ["get_pipeline_analytics"]
    assert r.repaired == ["unsupported_number"] and r.report.ok and paired(r.messages)   # the mixed attempt did not burn the repair


def test_two_submit_calls_in_one_response_only_the_first_counts():
    two = call(("a", "submit_answer", {"answer_markdown": "first"}), ("b", "submit_answer", {"answer_markdown": "second"}))
    o, _, _ = sorch([two])
    r = o.run_turn("q")
    msgs = {m["tool_call_id"]: json.loads(m["content"]) for m in tool_messages(r)}
    assert r.answer == "first" and msgs["b"]["reason"] == "submit_answer_called_more_than_once" and paired(r.messages)


def test_malformed_submission_is_rejected_with_field_problems_and_may_be_resubmitted():
    o, llm, _ = sorch([submit(answer_markdown="x", ranking=[{"candidate_id": "C1", "reason": "r"}]), submit("s2", answer_markdown="ok")])
    r = o.run_turn("q")
    rejection = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert rejection["reason"] == "invalid_arguments" and "ranking.0.candidate_id" in " ".join(rejection["problems"])
    assert r.answer == "ok" and r.repaired == []                                       # invalid JSON does not use the repair round


def test_plain_text_fallback_is_accepted_and_still_checked():
    o, _, _ = sorch([call(("1", "get_pipeline_analytics", {"role": "ROLE-BE"})), say("Backend Engineer: the median lag is 8 days.")])
    r = o.run_turn("q")
    assert r.status == "ok" and r.submitted is None and r.report.structured is False and r.report.ok and r.report.roles == ["ROLE-BE"]
    assert r.answer == "Backend Engineer: the median lag is 8 days."
    assert sum(m["role"] == "assistant" and not m.get("tool_calls") for m in r.messages) == 1   # no duplicated final message


def test_plain_text_with_an_unsupported_number_gets_one_nudge_then_is_accepted_as_not_verified():
    o, llm, _ = sorch([call(("1", "get_pipeline_analytics", {"role": "ROLE-BE"})), say("Backend Engineer: 9.5 days."), say("Still 9.5 days.")])
    r = o.run_turn("q")
    nudge = llm.calls[2]["messages"][-1]
    assert nudge["role"] == "system" and "9.5" in nudge["content"] and "submit_answer" in nudge["content"]
    assert len(llm.calls) == 3 and r.answer == "Still 9.5 days." and [n.value for n in r.report.unsupported_numbers] == [9.5]
    assert r.messages[-1]["content"].startswith(r.answer)


def test_limit_wrap_up_answers_are_checked_too():
    steps = [call((f"c{i}", "list_roles", {})) for i in range(2)] + [say("Partial: 42 roles.")]
    o, _, _ = orch(steps, limits=Limits(max_rounds=2))
    r = o.run_turn("q")
    assert r.status == "limit_reached" and [n.value for n in r.report.unsupported_numbers] == [42.0]


def test_history_with_a_structured_answer_stays_valid_and_carries_the_answer_forward():
    o, llm, _ = sorch([call(("1", "get_candidates", {"candidate_ids": ["C101"]})),
                       submit(answer_markdown="Pick N N.", ranking=[cand_arg()]),
                       say("Follow-up answer.")])
    state = ConversationState()
    o.run_turn("q1", state)
    o.run_turn("q2", state)
    second_turn_messages = llm.calls[2]["messages"]
    assert paired(second_turn_messages) and second_turn_messages[-2] == {"role": "assistant", "content": "Pick N N.\n\n[About: N N (C101)]"}


# --- real server: truncation disclosure and flags -----------------------------------------------------------
SIX = ["C104", "C108", "C110", "C113", "C117", "C112"]






def test_a_flagged_candidate_needs_no_model_warning_and_no_repair(real):
    """The application reports flagged content itself (notice + original text for review), so the model is not asked to."""
    quiet = submit("a", answer_markdown="Quentin Abara is a reasonable candidate.", ranking=[cand_arg("C117")])
    o, llm = real_orch(real["injected"], [call(("1", "get_candidates", {"candidate_ids": ["C117"]})), quiet])
    r = o.run_turn("q")
    assert len(llm.calls) == 2 and r.repaired == [] and r.report.ok and r.report.candidates == ["C117"]
    assert {f["candidate_id"] for f in r.content_flags} == {"C117"} and r.flag_sources[0]["candidate_id"] == "C117"


def test_flags_that_only_arrived_via_search_are_also_reported_by_the_application(real):
    o, llm = real_orch(real["injected"], [call(("1", "search_candidates", {"role": "ROLE-BE"})),
                                          call(("2", "get_candidates", {"candidate_ids": ["C104"]})),
                                          submit("a", answer_markdown="Top: Dahlia Ramanathan.", ranking=[cand_arg("C104")])])
    r = o.run_turn("q")
    assert r.repaired == [] and r.evidence.flagged_candidates() == ["C117"] and not r.evidence.retrieved("C117")
    assert [s["candidate_id"] for s in r.flag_sources] == ["C117"]                 # inspectable although the record was never opened


# ================================================================== wrap-up names the unreviewed; client closed per turn


def test_canned_wrap_up_says_the_answer_is_incomplete_when_the_model_gives_no_text():
    steps = [call(("1", "get_candidates", {"candidate_ids": ["C101", "C102"]})),
             call(("2", "get_candidates", {"candidate_ids": ["C103", "C104"]}), ("3", "get_candidates", {"candidate_ids": ["C105"]})),
             LLMUnavailable("llm_unavailable")]                                   # the wrap-up call fails, so the canned message is used
    o, _, _ = sorch(steps, limits=Limits(max_tool_calls=1))
    r = o.run_turn("compare all")
    assert r.status == "limit_reached" and "incomplete" in r.answer


def test_the_llm_client_is_closed_at_the_end_of_every_turn_including_failed_ones():
    class ClosingLLM(ScriptedLLM):
        closed = 0
        async def aclose(self):
            type(self).closed += 1
    ClosingLLM.closed = 0
    FakeSession.opened = FakeSession.closed = 0
    o = Orchestrator(ClosingLLM([say("fine"), LLMUnavailable("llm_unavailable")]), session_factory=lambda: FakeSession(FakeClient()))
    o.run_turn("q1")
    o.run_turn("q2")
    assert ClosingLLM.closed == 2 and FakeSession.closed == 2


def test_talking_about_a_search_row_is_allowed_but_ranking_it_without_reading_the_record_is_not():
    search = lambda n, a: env({"role_id": "ROLE-BE", "total_matching": 1, "returned": 1, "candidates": [
        {"candidate_id": "C113", "name": "M V", "current_stage": "Technical Interview", "status": "active",
         "waiting_state": "awaiting_scheduling", "waiting_days": 22.0, "days_in_current_stage": 22.0, "fit_signals": {}, "content_flags": []}]},
        ["search:C113"])
    o, llm, _ = orch([call(("1", "search_candidates", {"role": "ROLE-BE"})), submit("2", answer_markdown="M V has waited 22 days.", candidates=["C113"])],
                     client=FakeClient(search))
    r = o.run_turn("who waits longest?")
    assert r.repaired == [] and r.report.ok and r.report.candidates == ["C113"]
    steps = [call(("1", "search_candidates", {"role": "ROLE-BE"})), submit("2", ranking=[cand_arg("C113")]), submit("3", ranking=[cand_arg("C113")])]
    o, llm, _ = orch(steps, client=FakeClient(search))
    r = o.run_turn("rank")
    assert r.repaired == ["candidate_not_retrieved"] and r.report.not_retrieved == ["C113"]
    assert len(llm.calls) == 3 and not r.report.ok                                       # one repair, then accepted (and visibly not verified)






















# ================================================================== empty model replies are retried (found by the UX baseline run)
def test_an_empty_model_reply_is_nudged_and_the_turn_recovers():
    o, llm, _ = orch([say(""), say("Recovered answer.")])
    r = o.run_turn("q")
    assert r.status == "ok" and r.answer == "Recovered answer." and r.empty_retries == 1 and r.trace.model_rounds == 2
    nudge = llm.calls[1]["messages"][-1]
    assert nudge["role"] == "system" and "empty" in nudge["content"].lower() and "submit_answer" in nudge["content"]
    assert [m["role"] for m in r.messages] == ["user", "assistant"]                       # neither the empty reply nor the nudge is saved
    assert all((m.get("content") or "").strip() for m in r.messages)


def test_the_observed_failure_shape_empty_reply_right_after_tool_results_recovers():
    """Live: after get_pipeline_analytics the model returned finish_reason=stop with empty text (3 completion tokens)."""
    o, llm, client = sorch([call(("1", "get_pipeline_analytics", {"role": "ROLE-BE"})), say(""),
                            submit("2", answer_markdown="Slowest: Technical Interview [analytics:ROLE-BE@2026-11-07].")])
    r = o.run_turn("where is the pipeline slowing down?")
    assert r.status == "ok" and r.empty_retries == 1 and r.submitted is not None and paired(r.messages)
    replay = llm.calls[2]["messages"]                                                    # what the model saw on the retry (call 1 produced the empty reply)
    assert [m["role"] for m in replay[-3:]] == ["assistant", "tool", "system"] and replay[-2]["tool_call_id"] == "1"
    assert [c[0] for c in client.calls] == ["get_pipeline_analytics"]                    # the retry did not repeat the tool call


@pytest.mark.parametrize("blank", ["", "   ", "\n\t ", None])
def test_every_kind_of_blank_reply_counts_as_empty(blank):
    o, _, _ = orch([say(blank), say("ok")]) if blank is not None else orch([LLMResponse(None, [], {"role": "assistant", "content": None}), say("ok")])
    r = o.run_turn("q")
    assert r.status == "ok" and r.answer == "ok" and r.empty_retries == 1


def test_two_empty_replies_in_a_row_are_still_recovered_but_a_third_is_not():
    ok, _, _ = orch([say(""), say(""), say("fine")])
    assert ok.run_turn("q").empty_retries == 2
    bad, llm, _ = orch([say(""), say(""), say(""), say("never reached")])
    assert bad.run_turn("q").error_code == "empty_answer" and len(llm.calls) == 3


def test_empty_retries_use_up_rounds_so_the_loop_stays_bounded():
    o, llm, _ = orch([say(""), say(""), say("Partial answer.")], limits=Limits(max_rounds=2))
    r = o.run_turn("q")
    assert r.status == "limit_reached" and r.limit_code == "round_limit" and r.answer.startswith("Partial answer.")
    assert llm.calls[-1]["tools"] is None                                                  # the usual tools-disabled wrap-up


def test_a_tool_call_with_no_text_is_not_an_empty_reply():
    o, _, _ = orch([call(("1", "list_roles", {})), say("done")])
    r = o.run_turn("q")
    assert r.empty_retries == 0 and r.status == "ok"


def test_an_empty_reply_does_not_disturb_the_submit_answer_repair_budget():
    o, llm, _ = sorch([say(""), submit("a", answer_markdown="999 days."), say(""), submit("b", answer_markdown="fixed")])
    r = o.run_turn("q")
    assert r.empty_retries == 2 and r.repaired == ["unsupported_number"] and r.answer == "fixed" and r.report.ok


def test_the_failed_turn_is_not_saved_and_the_session_stays_usable():
    o, _, _ = orch([say(""), say(""), say(""), say("second turn works")])
    state = ConversationState()
    assert o.run_turn("first", state).status == "error" and state.turns == []
    assert o.run_turn("second", state).answer == "second turn works" and len(state.turns) == 1


# ================================================================== progress events for the UI's loading state
def test_progress_events_report_real_model_and_tool_activity_in_order():
    events = []
    o, _, _ = orch([call(("1", "list_roles", {}), ("2", "get_pipeline_analytics", {"role": "ROLE-BE"})), call(("3", "list_roles", {})), say("done")])
    r = o.run_turn("q", on_progress=events.append)
    assert events == [{"type": "model"}, {"type": "tools", "names": ["list_roles", "get_pipeline_analytics"]}, {"type": "model"},
                      {"type": "tools", "names": ["list_roles"]}, {"type": "model"}] and r.status == "ok"


def test_a_failing_progress_callback_never_breaks_the_turn():
    def boom(event):
        raise RuntimeError("ui exploded")
    o, _, _ = orch([call(("1", "list_roles", {})), say("fine")])
    assert o.run_turn("q", on_progress=boom).answer == "fine"


def test_submit_answer_is_not_announced_as_a_data_tool():
    events = []
    o, _, _ = sorch([call(("1", "get_pipeline_analytics", {"role": "ROLE-BE"})), submit("2", answer_markdown="x [analytics:ROLE-BE@2026-11-07]")])
    o.run_turn("q", on_progress=events.append)
    assert [e["names"] for e in events if e["type"] == "tools"] == [["get_pipeline_analytics"]]


# ================================================================== per-round trace and the turn log
def test_every_model_round_is_recorded_with_its_cost_action_and_verdict():
    usage = {"prompt_tokens": 100, "completion_tokens": 7}
    bad = submit("s1", answer_markdown="Backend Engineer: 9.5 days.")
    good = submit("s2", answer_markdown="Backend Engineer: 8 days.")
    steps = [call(("1", "get_pipeline_analytics", {"role": "ROLE-BE"})), bad, good]
    for s in steps:
        s.usage = usage
    o, _, _ = sorch(steps)
    r = o.run_turn("q")
    rounds = r.trace.rounds
    assert [(x.round, x.action, x.outcome) for x in rounds] == [(1, "tools", ""), (2, "submit", "sent back"), (3, "submit", "accepted")]
    assert "9.5" in rounds[1].problems[0] and all(x.prompt_tokens == 100 and x.completion_tokens == 7 for x in rounds)
    assert r.trace.totals()["prompt_tokens"] == r.usage["prompt_tokens"] == 300
    assert r.trace.events[0].round == 1 and r.trace.events[0].result_chars > 0


def test_each_turn_is_appended_to_the_log_without_any_record_text(real, tmp_path):
    log = tmp_path / "logs" / "turns.jsonl"
    o, _ = real_orch(real["injected"], [call(("1", "get_candidates", {"candidate_ids": ["C117"]})),
                                        submit("2", answer_markdown="Quentin Abara is in Technical Interview.")], log_path=log)
    o.run_turn("Where is Quentin?")
    (line,) = log.read_text().splitlines()
    entry = json.loads(line)
    assert entry["question"] == "Where is Quentin?" and entry["status"] == "ok" and entry["model_rounds"] == 2
    assert [c["tool_name"] for c in entry["calls"]] == ["get_candidates"] and entry["rounds"][1]["outcome"] == "accepted"
    assert "SYSTEM OVERRIDE" not in line and "Skills:" not in line and "untrusted_text" not in line     # IDs, counts and sizes only


def test_a_broken_log_location_never_breaks_the_turn(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")                                          # a file where the log directory should be
    o, _, _ = orch([say("fine")], log_path=blocker / "turns.jsonl")
    assert o.run_turn("q").status == "ok"
