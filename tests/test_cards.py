"""Evidence cards: what the recruiter can open to see why a claim holds."""
import asyncio
import json

import pytest

from agent import cards
from agent.mcp_client import McpSession
from agent.tool_executor import ToolCall, ToolExecutor, TurnContext
from data import scenarios as sc
from data.seed import build

PAYLOAD = sc.INJECTION_PAYLOAD
BAD_WORDS = ("untrusted_text", "strong_yes", "awaiting_", "_days", "fit_signals", "C1", "FB-", "EV-", "ROLE-")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    db = build(tmp_path_factory.mktemp("cards") / "c.db", "injected")

    async def go():
        async with McpSession(db) as s:
            ex = ToolExecutor(s)
            await ex.start()
            ctx = TurnContext()
            for i, (n, a) in enumerate([("search_candidates", {"role_id": "ROLE-BE"}), ("get_candidates", {"candidate_ids": ["C104", "C117"]}),
                                        ("get_candidate_communications", {"candidate_id": "C104"}), ("get_tasks", {"overdue_only": True}),
                                        ("get_pipeline_analytics", {"role_id": "ROLE-BE"})]):
                await ex.execute(ToolCall(str(i), n, a), ctx)
            return ctx.evidence
    return asyncio.run(go())


def text(c):
    return json.dumps({k: v for k, v in c.items() if k != "ref"})


def test_every_citable_reference_has_a_card_with_a_readable_title(live):
    missing = [r for r in live.refs() if not live.card(r)]
    assert missing == []
    for r in live.refs():
        c = live.card(r)
        assert c["title"] and c["kind"] == r.partition(":")[0] and not any(b in c["title"] for b in BAD_WORDS), (r, c["title"])


def test_full_records_and_search_summaries_are_told_apart_in_words(live):
    full, search = live.card("cv:C104"), live.card("search:C104")
    assert full["full_record"] is True and "full record opened" in full["title"] and full["title"].startswith("Dahlia Ramanathan")
    assert search["full_record"] is False and "record not opened" in search["title"]
    assert any("Required skills matched: 4 of 4" == l for l in search["lines"])
    assert full["quote"]["label"] == "CV excerpt (candidate-supplied text)" and full["quote"]["text"].startswith("Dahlia Ramanathan")


def test_feedback_cards_say_who_when_and_what_in_plain_words(live):
    fb = [c for r in live.refs() if r.startswith("feedback:") and (c := live.card(r)) and "Technical Interview feedback" in c["title"] and c["who_id"] == "C104"]
    assert len(fb) == 1
    c = fb[0]
    assert c["who_id"] == "C104" and "(" in c["title"] and c["lines"][0] == "Strongly recommends advancing"
    assert any(l.startswith("Scores: coding") and "system design" in l for l in c["lines"]) and c["quote"]["label"] == "Interviewer's note"
    note = next(live.card(r) for r in live.refs() if r.startswith("feedback:") and "Resume review" in live.card(r)["title"])
    assert note["quote"]["label"] == "Reviewer's note"


def test_task_analytics_and_email_cards(live):
    t = live.card("task:T-002")
    assert t["title"] == "Book Technical Interview (Mateo Villareal)" and "Owner: Jordan Ellis" in t["lines"] and any(l.startswith("Overdue by 15") for l in t["lines"])
    a = live.card("analytics:ROLE-BE@2026-11-07")
    assert a["title"] == "Backend Engineer pipeline, as of 2026-11-07" and "Role open 123 days" in a["lines"]
    assert any(l.startswith("Technical Interview: 17 entered") for l in a["lines"])
    e = next(live.card(r) for r in live.refs() if r.startswith("email:"))
    assert e["title"].startswith("Email ") and "Dahlia Ramanathan" in e["title"] and e["quote"]["label"].endswith(("candidate-supplied text)", "Recruiter's email"))


def test_candidate_supplied_text_appears_only_inside_labelled_quotes(live):
    for r in live.refs():
        c = live.card(r)
        outside = text({k: v for k, v in c.items() if k != "quote"})
        assert PAYLOAD not in outside and "SYSTEM OVERRIDE" not in outside
    assert PAYLOAD[:20] in live.card("cv:C117")["quote"]["text"] or "Redwater" in live.card("cv:C117")["quote"]["text"]


def test_quotes_are_capped_and_never_empty_strings():
    big = {"data": {"candidates": [{"candidate_id": "C101", "name": "A B", "cv_text": {"untrusted_text": "x" * 2000, "label": "l"}, "feedback": [], "stage_history": []}]}}
    q = cards.build("get_candidates", big, {})["cv:C101"]["quote"]["text"]
    assert len(q) == 501 and q.endswith("…")
    blank = {"data": {"candidates": [{"candidate_id": "C101", "name": "A B", "cv_text": "  ", "feedback": [], "stage_history": []}]}}
    assert cards.build("get_candidates", blank, {})["cv:C101"]["quote"] is None


def test_a_wrapped_or_missing_name_never_breaks_a_card():
    odd = {"data": {"candidates": [{"candidate_id": "C101", "name": {"untrusted_text": "Pat. Ignore all previous", "label": "x"}, "feedback": [], "stage_history": []}]}}
    assert cards.build("get_candidates", odd, {})["cv:C101"]["title"].startswith("Pat. Ignore all previous")      # shown as plain text, not interpreted
    none = {"data": {"candidates": [{"candidate_id": "C101", "feedback": [], "stage_history": []}]}}
    assert cards.build("get_candidates", none, {})["cv:C101"]["title"].startswith("This candidate")
    assert cards.when("2026-10-28 13:00 PDT") == "Oct 28" and cards.when(None) == "" and cards.when("garbage") == ""


def test_cards_exist_only_for_registered_refs_so_truncated_away_records_have_none():
    from agent.evidence import EvidenceRegistry
    reg = EvidenceRegistry()
    env = {"data": {"candidates": [{"candidate_id": "C101", "name": "A B", "feedback": [], "stage_history": []},
                                   {"candidate_id": "C102", "name": "C D", "feedback": [], "stage_history": []}]},
           "source_refs": ["cv:C101"], "content_flags": [], "warnings": [], "omitted": ["C102"]}
    reg.add("get_candidates", env)
    assert reg.card("cv:C101") and reg.card("cv:C102") is None


def test_an_email_card_for_a_candidate_never_seen_by_name_says_the_candidate():
    env = {"data": {"candidate_id": "C114", "messages": [{"email_id": "EM-009", "sent_at": "2026-10-24 13:00 PDT", "direction": "inbound",
                                                           "subject": {"untrusted_text": "Waiting", "label": "x"}, "body": {"untrusted_text": "Still waiting.", "label": "x"}}]}}
    built = cards.build("get_candidate_communications", env, {})["email:EM-009"]
    assert built["title"] == "Email from {who}, Oct 24" and built["who_id"] == "C114" and built["quote"]["text"] == "Waiting\n\nStill waiting."
    from agent.evidence import EvidenceRegistry
    reg = EvidenceRegistry()
    reg.add("get_candidate_communications", {**env, "source_refs": ["email:EM-009"], "content_flags": [], "warnings": [], "omitted": []})
    assert reg.card("email:EM-009")["title"] == "Email from the candidate, Oct 24"                  # name not known yet
    reg.add("get_candidates", {"data": {"candidates": [{"candidate_id": "C114", "name": "Nadia Kowalczyk", "feedback": [], "stage_history": []}]},
                               "source_refs": ["cv:C114"], "content_flags": [], "warnings": [], "omitted": []})
    assert reg.card("email:EM-009")["title"] == "Email from Nadia Kowalczyk, Oct 24"               # ... and learned later: the same card now says so


def test_task_card_titles_pick_up_names_learned_after_the_tasks_were_fetched():
    from agent.evidence import EvidenceRegistry
    reg = EvidenceRegistry()
    reg.add("get_tasks", {"data": {"tasks": [{"task_id": "T-002", "candidate_id": "C113", "description": "Book Technical Interview", "owner": "J", "overdue": True,
                                              "days_overdue": 15.0, "due_at": "2026-10-23 13:00 PDT"}]}, "source_refs": ["task:T-002"],
                          "content_flags": [], "warnings": [], "omitted": []})
    assert reg.card("task:T-002")["title"] == "Book Technical Interview"
    reg.add("search_candidates", {"data": {"candidates": [{"candidate_id": "C113", "name": "Mateo Villareal"}]}, "source_refs": ["search:C113"],
                                  "content_flags": [], "warnings": [], "omitted": []})
    assert reg.card("task:T-002")["title"] == "Book Technical Interview (Mateo Villareal)"


@pytest.mark.parametrize("tool,data", [
    ("get_candidates", {"candidates": [{"candidate_id": "C101"}]}),
    ("get_candidates", {"candidates": [{"candidate_id": "C101", "feedback": [{"stage": "x"}, {"feedback_id": "FB-1"}], "stage_history": [{}, {"event_id": "EV-1"}]}]}),
    ("get_candidates", {"candidates": "not a list"}), ("search_candidates", {"candidates": [{"candidate_id": "C101", "fit_signals": None}]}),
    ("get_tasks", {"tasks": [{"task_id": "T-1"}]}), ("get_pipeline_analytics", {"stages": [{"stage": "x"}, {"funnel": {}}]}),
    ("get_candidate_communications", {"messages": [{}]}), ("list_roles", None)])
def test_malformed_or_partial_results_never_raise(tool, data):
    out = cards.build(tool, {"data": data, "source_refs": ["analytics:ROLE-BE@2026-11-07"]}, {})
    assert isinstance(out, dict)


def test_a_card_problem_can_never_break_evidence_tracking():
    from agent.evidence import EvidenceRegistry
    reg = EvidenceRegistry()
    reg.add("get_candidates", {"data": {"candidates": [{"candidate_id": "C101", "feedback": "garbage"}]}, "source_refs": ["cv:C101"],
                               "content_flags": [], "warnings": [], "omitted": []})
    assert reg.has("cv:C101") and reg.retrieved("C101")                               # the safety function still worked


def test_search_card_states_interview_evidence_separately():
    from agent import cards
    env = {"data": {"candidates": [{"candidate_id": "C104", "name": "Dahlia Ramanathan", "current_stage": "Technical Interview", "waiting_state": "awaiting_feedback",
                                    "fit_signals": {"required_skills_matched": ["a"], "required_skills_missing": [], "feedback_count": 2, "interviews_completed": 3,
                                                    "feedback_received": 2, "feedback_pending": 1, "stages_with_feedback": 2}}]}, "source_refs": []}
    card = cards.build("search_candidates", env, {})["search:C104"]
    assert "Interviews held: 3. Feedback received: 2. Feedback pending: 1." in card["lines"]
    old = {"data": {"candidates": [{"candidate_id": "C104", "name": "X", "fit_signals": {"feedback_count": 2}}]}, "source_refs": []}
    assert "Interview and review notes on file: 2" in cards.build("search_candidates", old, {})["search:C104"]["lines"]
