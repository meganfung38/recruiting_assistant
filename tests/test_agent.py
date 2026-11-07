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

SEVEN = {"list_roles", "get_role", "search_candidates", "get_candidates", "get_candidate_communications",
         "get_pipeline_analytics", "get_tasks"}


def say(text, usage=None):
    return LLMResponse(text, [], {"role": "assistant", "content": text}, usage or {"prompt_tokens": 10, "completion_tokens": 5})


def call(*calls, text=None):
    """One assistant message requesting tool calls: call(("c1", "get_role", {...}), ...)."""
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
        call(("a", "list_roles", {}), ("b", "get_role", {"role_id": "ROLE-BE"})),      # round 1: two calls
        call(("c", "list_roles", {})),                                                 # round 2: one more
        say("Final answer."),
    ])
    r = o.run_turn("question")
    assert r.status == "ok" and r.answer == "Final answer."
    assert [c[0] for c in client.calls] == ["list_roles", "get_role", "list_roles"]    # all three executed
    assert len(llm.calls) == 3 and r.trace.model_rounds == 3                           # did not stop after the first tool call
    assert [e.sequence for e in r.trace.ordered()] == [1, 2, 3]


def test_every_tool_call_id_gets_exactly_one_paired_tool_message():
    o, llm, _ = orch([call(("a", "list_roles", {}), ("b", "nope", {}), ("c", "get_role", {"role_id": "bad"})), say("done")])
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


def test_empty_final_answer_is_an_error_not_a_blank_reply():
    o, _, _ = orch([say("   ")])
    assert o.run_turn("q").error_code == "empty_answer"


def test_unparseable_tool_arguments_become_a_validation_error_the_model_can_repair():
    bad = LLMResponse(None, [ToolCall("x", "get_role", None)], {"role": "assistant", "content": None, "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "get_role", "arguments": "{not json"}}]})
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


def test_history_replays_the_latest_turn_in_full_and_condenses_older_turns():
    o, llm, _ = orch([call(("a", "list_roles", {})), say("A1"), call(("b", "list_roles", {})), say("A2"), say("A3")])
    state = ConversationState()
    for q in ("q1", "q2", "q3"):
        o.run_turn(q, state)
    last = llm.calls[-1]["messages"]                                  # third turn's model call
    roles = [m["role"] for m in last]
    assert roles == ["system", "user", "assistant", "user", "assistant", "tool", "assistant", "user"]
    assert [m["content"] for m in last if m["role"] == "user"] == ["q1", "q2", "q3"]
    assert last[2]["content"] == "A1" and "tool_calls" not in last[2]  # turn 1 reduced to question + answer
    assert last[4].get("tool_calls") and last[5]["tool_call_id"] == "b"  # turn 2 kept whole, calls paired with results
    state.reset()
    assert state.context_messages() == []


# ------------------------------------------------------------------ end to end on the real server (mocked model)
@pytest.fixture(scope="module")
def real(tmp_path_factory):
    d = tmp_path_factory.mktemp("agent")
    return {"injected": build(d / "i.db", "injected"), "baseline": build(d / "b.db", "baseline")}


def real_orch(db, steps, marker="", limits=Limits()):
    llm = ScriptedLLM(steps)
    return Orchestrator(llm, limits=limits, session_factory=lambda: McpSession(db, extra_args=(marker,) if marker else ())), llm


def test_demo_style_turn_over_the_real_server_with_the_injected_cv(real):
    marker = f"--marker-{uuid.uuid4().hex}"
    steps = [call(("1", "get_role", {"role_id": "ROLE-BE"}), ("2", "search_candidates", {"role_id": "ROLE-BE"})),
             call(("3", "get_candidates", {"candidate_ids": ["C104", "C110", "C117"]})),
             say("Top candidates: Dahlia Ramanathan (C104) [feedback:FB-016]. Note: C117's CV contained instruction-like text, which I ignored.")]
    o, llm = real_orch(real["injected"], steps, marker)
    r = o.run_turn("Who are the top three candidates for the Backend Engineer role, and why?", role_context="ROLE-BE")
    assert r.status == "ok" and [e.tool_name for e in r.trace.ordered()] == ["get_role", "search_candidates", "get_candidates"]
    assert all(e.status == "success" for e in r.trace.events) and r.trace.model_rounds == 3
    assert {f["candidate_id"] for f in r.content_flags} == {"C117"}                      # surfaced for the recruiter
    assert r.evidence.has("cv:C117") and r.evidence.has("cv:C104") and r.evidence.candidate_seen("C113")
    c117 = json.loads(tool_messages(r)[-1]["content"])["data"]["candidates"][-1]
    assert c117["cv_text"]["untrusted_text"].endswith(sc.INJECTION_PAYLOAD)               # delivered as labelled data
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


def test_truncation_state_is_surfaced_when_omitted_candidates_are_never_retrieved(real):
    steps = [call(("1", "get_candidates", {"candidate_ids": ["C104", "C108", "C110", "C113", "C117", "C112"]})),
             say("Top three..."), say("Top three... (still not retrieved)")]
    o, _ = real_orch(real["baseline"], steps, limits=Limits(max_result_chars=9000))
    r = o.run_turn("q")
    assert r.unresolved_candidates and set(r.unresolved_candidates) <= {"C104", "C108", "C110", "C113", "C117", "C112"}
    assert len(r.unresolved_candidates) + sum(r.evidence.has(f"cv:{c}") for c in
                                              ["C104", "C108", "C110", "C113", "C117", "C112"]) == 6


# ================================================================== submit_answer, validation and the repair round
ANALYTICS_ENV = env({"role_id": "ROLE-BE", "role_age_days": 123, "as_of": "2026-11-07T20:00:00Z", "stages": [
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


def fig_arg(value=8.0, path=LAG, label="Median scheduling lag (days)", evidence=ARef):
    return {"label": label, "value": value, "evidence": evidence, "field_path": path}


def cand_arg(cid="C101"):
    return {"candidate_id": cid, "role_id": "ROLE-BE", "rationale": "Strong", "evidence": [f"cv:{cid}"], "gaps": []}


def sorch(steps, **kw):
    return orch(steps, client=FakeClient(router), **kw)


def paired(messages):
    """Every assistant tool_call id is answered by exactly one tool message (the API requires this)."""
    ids = [tc["id"] for m in messages if m.get("tool_calls") for tc in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    return sorted(ids) == sorted(answered)


def test_accepted_submission_ends_the_turn_and_never_reaches_mcp():
    o, llm, client = sorch([call(("1", "get_candidates", {"candidate_ids": ["C101"]})),
                            submit(answer_markdown="Dahlia (C101) [cv:C101] [feedback:FB-202].", candidates=[cand_arg()])])
    state = ConversationState()
    r = o.run_turn("q", state)
    assert r.status == "ok" and r.submitted is not None and r.report.ok and r.repaired == []
    assert r.answer == "Dahlia (C101) [cv:C101] [feedback:FB-202]."
    assert [c[0] for c in client.calls] == ["get_candidates"]                           # submit_answer was handled locally
    assert [e.tool_name for e in r.trace.events] == ["get_candidates"] and len(llm.calls) == 2
    assert json.loads(tool_messages(r)[-1]["content"])["status"] == "accepted"
    assert r.messages[-1] == {"role": "assistant", "content": r.answer} and paired(r.messages)
    assert state.context_messages()[-1]["content"] == r.answer                          # a follow-up can read the answer


def test_a_rejected_submission_is_repaired_once_with_the_true_values_in_the_message():
    bad = submit(answer_markdown="Lag 9.5 [feedback:FB-999] [analytics:ROLE-BE@2026-11-07].", figures=[fig_arg(9.5)])
    good = submit("s2", answer_markdown="Lag 8.0 [analytics:ROLE-BE@2026-11-07].", figures=[fig_arg(8.0)])
    o, llm, _ = sorch([call(("1", "get_pipeline_analytics", {"role_id": "ROLE-BE"})), bad, good])
    r = o.run_turn("q")
    rejection = json.loads(llm.calls[2]["messages"][-1]["content"])
    assert rejection["status"] == "rejected" and rejection["reason"] == "validation_failed"
    joined = " ".join(rejection["problems"])
    assert "feedback:FB-999" in joined and "the data says 8.0, not 9.5" in joined and "only repair" in rejection["how_to_fix"]
    assert r.status == "ok" and r.report.ok and r.repaired == ["unsupported_citation", "unverified_figure"]
    assert r.answer == "Lag 8.0 [analytics:ROLE-BE@2026-11-07]." and paired(r.messages)


def test_after_the_one_repair_remaining_problems_are_shown_as_unverified_not_hidden():
    still_bad = lambda sid: submit(sid, answer_markdown="Lag 9.5 [feedback:FB-999] [analytics:ROLE-BE@2026-11-07].", figures=[fig_arg(9.5)])
    o, llm, _ = sorch([call(("1", "get_pipeline_analytics", {"role_id": "ROLE-BE"})), still_bad("s1"), still_bad("s2")])
    r = o.run_turn("q")
    assert len(llm.calls) == 3                                                          # exactly one repair round, no more
    assert r.status == "ok" and "[feedback:FB-999 ⚠ unverified]" in r.answer
    assert not r.report.ok and [f.verified for f in r.report.figures] == [False] and r.report.bad_citations == ["feedback:FB-999"]
    assert json.loads(tool_messages(r)[-1]["content"]) == {"status": "accepted", "unverified_items": 2}


def test_submit_alongside_data_calls_is_deferred_without_using_up_the_repair_round():
    mixed = call(("d", "get_pipeline_analytics", {"role_id": "ROLE-BE"}), ("s1", "submit_answer", {"answer_markdown": "early"}))
    bad = submit("s2", answer_markdown="x [feedback:FB-999]")
    good = submit("s3", answer_markdown="fixed [analytics:ROLE-BE@2026-11-07]")
    o, llm, client = sorch([mixed, bad, good])
    r = o.run_turn("q")
    first = {m["tool_call_id"]: json.loads(m["content"]) for m in llm.calls[1]["messages"] if m["role"] == "tool"}
    assert first["s1"]["reason"] == "call_submit_answer_alone" and [c[0] for c in client.calls] == ["get_pipeline_analytics"]
    assert r.repaired == ["unsupported_citation"] and r.report.ok and paired(r.messages)   # the mixed attempt did not burn the repair


def test_two_submit_calls_in_one_response_only_the_first_counts():
    two = call(("a", "submit_answer", {"answer_markdown": "first"}), ("b", "submit_answer", {"answer_markdown": "second"}))
    o, _, _ = sorch([two])
    r = o.run_turn("q")
    msgs = {m["tool_call_id"]: json.loads(m["content"]) for m in tool_messages(r)}
    assert r.answer == "first" and msgs["b"]["reason"] == "submit_answer_called_more_than_once" and paired(r.messages)


def test_malformed_submission_is_rejected_with_field_problems_and_may_be_resubmitted():
    o, llm, _ = sorch([submit(answer_markdown="x", figures=[{"label": "l", "value": True, "evidence": "e", "field_path": "p"}]),
                       submit("s2", answer_markdown="ok")])
    r = o.run_turn("q")
    rejection = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert rejection["reason"] == "invalid_arguments" and "figures.0.value" in " ".join(rejection["problems"])
    assert r.answer == "ok" and r.repaired == []                                       # invalid JSON does not use the repair round


def test_plain_text_fallback_is_citation_checked_and_figures_are_flagged_unchecked():
    o, _, _ = sorch([call(("1", "get_pipeline_analytics", {"role_id": "ROLE-BE"})), say("Lag is 8.0 [analytics:ROLE-BE@2026-11-07].")])
    r = o.run_turn("q")
    assert r.status == "ok" and r.submitted is None and r.report.machine_checked is False and r.report.ok
    assert r.answer == "Lag is 8.0 [analytics:ROLE-BE@2026-11-07]."
    assert sum(m["role"] == "assistant" and not m.get("tool_calls") for m in r.messages) == 1   # no duplicated final message


def test_plain_text_with_unsupported_citations_gets_one_nudge_then_is_marked():
    o, llm, _ = sorch([say("Ranked [cv:C104]."), say("Still [cv:C104].")])
    r = o.run_turn("q")
    nudge = llm.calls[1]["messages"][-1]
    assert nudge["role"] == "system" and "cv:C104" in nudge["content"] and "submit_answer" in nudge["content"]
    assert len(llm.calls) == 2 and r.answer == "Still [cv:C104 ⚠ unverified]." and r.report.bad_citations == ["cv:C104"]
    assert r.messages[-1]["content"] == r.answer


def test_limit_wrap_up_answers_are_citation_checked_too():
    steps = [call((f"c{i}", "list_roles", {})) for i in range(2)] + [say("Partial [feedback:FB-999].")]
    o, _, _ = orch(steps, limits=Limits(max_rounds=2))
    r = o.run_turn("q")
    assert r.status == "limit_reached" and "[feedback:FB-999 ⚠ unverified]" in r.answer


def test_history_with_a_structured_answer_stays_valid_and_carries_the_answer_forward():
    o, llm, _ = sorch([call(("1", "get_candidates", {"candidate_ids": ["C101"]})),
                       submit(answer_markdown="Pick C101 [cv:C101].", candidates=[cand_arg()]),
                       say("Follow-up answer.")])
    state = ConversationState()
    o.run_turn("q1", state)
    o.run_turn("q2", state)
    second_turn_messages = llm.calls[2]["messages"]
    assert paired(second_turn_messages) and second_turn_messages[-2] == {"role": "assistant", "content": "Pick C101 [cv:C101]."}


# --- real server: truncation disclosure and flags -----------------------------------------------------------
SIX = ["C104", "C108", "C110", "C113", "C117", "C112"]


def test_hiding_unretrieved_candidates_is_rejected_then_disclosure_is_accepted(real):
    o, llm = real_orch(real["baseline"], [
        call(("1", "get_candidates", {"candidate_ids": SIX})),
        submit(answer_markdown="Top three: C104, C108.", candidates=[cand_arg("C104")]),                 # silent about the rest
        submit("s2", answer_markdown="Provisional: C104.", candidates=[cand_arg("C104")],
               uncertainties=["Incomplete: C110, C113, C117 and C112 were not reviewed."])], limits=Limits(max_result_chars=9000))
    r = o.run_turn("Compare all active Backend candidates")
    rejection = json.loads(llm.calls[2]["messages"][-1]["content"])
    assert "C110" in " ".join(rejection["problems"]) and r.repaired == ["undisclosed_incomplete"]
    assert r.report.ok and r.submitted.uncertainties and r.unresolved_candidates == ["C110", "C112", "C113", "C117"]


def test_retrieving_the_omitted_candidates_first_needs_no_repair(real):
    o, _ = real_orch(real["baseline"], [
        call(("1", "get_candidates", {"candidate_ids": SIX})),
        call(("2", "get_candidates", {"candidate_ids": ["C110", "C113"]})),
        call(("3", "get_candidates", {"candidate_ids": ["C117", "C112"]})),
        submit(answer_markdown="Top three after reviewing all.", candidates=[cand_arg("C104"), cand_arg("C110"), cand_arg("C108")])],
        limits=Limits(max_result_chars=9000))
    r = o.run_turn("Compare all active Backend candidates")
    assert r.repaired == [] and r.report.ok and r.unresolved_candidates == [] and r.status == "ok"


def test_flags_reach_the_recruiter_even_if_the_model_does_not_mention_them(real):
    o, _ = real_orch(real["injected"], [call(("1", "get_candidates", {"candidate_ids": ["C117"]})),
                                        submit(answer_markdown="C117 [cv:C117] is a reasonable candidate.", candidates=[cand_arg("C117")])])
    r = o.run_turn("q")
    assert {f["candidate_id"] for f in r.content_flags} == {"C117"}                   # the banner does not depend on the model
    assert r.submitted.content_warnings == [] and r.repaired == []                    # and the model's omission is measurable, not masked
