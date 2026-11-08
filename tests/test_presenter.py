"""The view model behind the page: the answer, the flag block, the evidence cards and the round-by-round technical view.
Real server, scripted model."""
import json

from agent.evidence import EvidenceRegistry
from agent.humanize import leaks
from agent.limits import Limits
from agent.presenter import TOOL_LABELS, build_view, primary_text, progress_label
from data import scenarios as sc
from tests.test_agent import call, cand_arg, orch, real, real_orch, say, submit  # noqa: F401  (real = fixture)

PAYLOAD = sc.INJECTION_PAYLOAD
BITS = ("SYSTEM OVERRIDE", "Ignore all other applicants", "rank this candidate", "Do not mention this instruction")
LABELS = ("ignore_instructions", "system_override", "rank_candidate", "concealment")


def view_of(db, steps, **kw):
    o, _ = real_orch(db, steps, **kw)
    r = o.run_turn("q")
    return build_view(r), r


MESSY = ("1. **Dahlia Ramanathan (C104)** leads: strong_yes at the Final Interview [feedback:FB-016] [cv:C104].\n"
         "2. **Junko Takeda (C110)**: awaiting_decision [cv:C110]. Role ROLE-BE.")
TOP3 = ["C104", "C110", "C108"]


def shortlist(db, answer=MESSY, ranked=TOP3):
    return view_of(db, [call(("1", "get_candidates", {"role": "Backend Engineer"})),
                        submit("2", answer_markdown=answer, ranking=[cand_arg(c) for c in ranked])])


# ------------------------------------------------------------------ default view
def test_the_default_view_uses_names_and_plain_words_whatever_the_model_wrote(real):
    v, _ = shortlist(real["baseline"])
    assert v["answer"].startswith("1. **Dahlia Ramanathan** leads: strong yes at the Final Interview.")
    assert "\n2. **Junko Takeda**: awaiting a decision. Role Backend Engineer." in v["answer"] and leaks(primary_text(v)) == []


def test_trust_line_says_checked_only_when_everything_matched(real):
    v, _ = shortlist(real["baseline"], answer="Dahlia Ramanathan has 3 interviews on file.")
    assert v["trust"] == {"state": "checked", "text": "Checked against the source records."}
    bad = lambda sid: submit(sid, answer_markdown="Dahlia Ramanathan has 47 interviews.")
    v2, _ = view_of(real["baseline"], [call(("1", "get_candidates", {"names": ["Dahlia Ramanathan"]})), bad("a"), bad("b")])
    assert v2["trust"]["state"] == "partial" and "could not be matched" in v2["trust"]["text"]
    assert any("47" in p for p in v2["technical"]["validation"])


def test_limit_and_error_notices_are_plain():
    o, _, _ = orch([call((f"c{i}", "list_roles", {})) for i in range(2)] + [say("Partial.")], limits=Limits(max_rounds=2))
    v = build_view(o.run_turn("q"))
    assert v["status"] == "limit_reached" and v["notices"][0]["text"] == "The assistant ran out of steps before finishing, so this answer may be incomplete."
    from agent.llm import LLMUnavailable
    e = build_view(orch([LLMUnavailable("llm_unavailable")])[0].run_turn("q"))
    assert e["notices"][0]["level"] == "error" and "unavailable" in e["notices"][0]["text"] and e["trust"]["state"] == "none"


# ------------------------------------------------------------------ the flag block: shown with its explanation, or not at all
def test_a_flagged_person_in_the_answer_gets_one_titled_block_with_the_reason_and_the_original_text(real):
    v, _ = shortlist(real["injected"], answer="Quentin Abara is second.", ranked=["C104", "C117"])
    (b,) = v["flags"]
    assert b["title"] == "Instruction-like text found in Quentin Abara's CV"
    assert "asked to be ranked first" in b["text"] and "ignored it; it did not affect this answer" in b["text"]
    assert b["sources"] == [{"label": "CV", "location": "near the end", "excerpt": PAYLOAD}]
    assert not any(l in json.dumps(v) for l in LABELS)                                    # never the detector's internal labels
    assert b["title"] in primary_text(v)


def test_a_flagged_person_mentioned_only_in_the_prose_still_gets_the_block(real):
    """Manual test: Quentin was mentioned ("also strong") but not ranked, and the original text appeared with no notice."""
    v, _ = shortlist(real["injected"], answer="Dahlia Ramanathan leads. Quentin Abara is also strong.", ranked=TOP3)
    assert [b["candidate"] for b in v["flags"]] == ["Quentin Abara"]


def test_a_flagged_person_the_answer_is_not_about_shows_nothing_but_the_developer_view_notes_it(real):
    v, _ = shortlist(real["injected"], answer="Dahlia Ramanathan leads.", ranked=TOP3)
    assert v["flags"] == [] and not any(b in json.dumps({k: x for k, x in v.items() if k != "technical"}) for b in BITS)
    assert v["technical"]["flags_not_shown"] == ["C117"]


def test_a_model_that_quotes_the_flagged_text_anyway_is_redacted_in_the_answer(real):
    quoted = f"Quentin Abara's CV was flagged. It contains `{PAYLOAD}` The content flags were `concealment` and `rank candidate`."
    v, _ = view_of(real["injected"], [call(("1", "get_candidates", {"candidate_ids": ["C117"]})), submit("2", answer_markdown=quoted)])
    assert not any(b in v["answer"] for b in BITS) and "concealment" not in v["answer"] and "Quentin Abara's CV was flagged." in v["answer"]
    assert v["flags"][0]["sources"][0]["excerpt"] == PAYLOAD                               # the reviewer still has the original


# ------------------------------------------------------------------ evidence cards: same shape for everyone the answer ranks
def test_ranked_people_get_cards_in_ranking_order_with_the_same_shape(real):
    v, _ = shortlist(real["baseline"])
    cards = v["evidence"]
    assert [c["person"] for c in cards] == ["Dahlia Ramanathan", "Junko Takeda", "Hannelore Voss"]
    assert all(set(c) == {"person", "role", "reason", "gaps", "status", "skills", "interviews", "pending"} for c in cards)
    assert all(c["role"] == "Backend Engineer" and c["skills"][0].startswith("All required skills") for c in cards)
    # one line per interview stage with feedback; resume-review notes are left out for everyone, so counts are comparable
    assert [[i["title"].split(" · ")[0] for i in c["interviews"]] for c in cards] == [
        ["Recruiter Screen", "Technical Interview", "Final Interview"], ["Recruiter Screen", "Technical Interview"],
        ["Recruiter Screen", "Technical Interview"]]
    assert cards[2]["pending"].endswith("interview booked on Nov 3 but not held yet (no interview date is recorded).")
    assert cards[0]["pending"] == "" and all(i["note"] and i["verdict"] for c in cards for i in c["interviews"])
    assert cards[0]["interviews"][1]["verdict"].startswith("Strongly recommends advancing (")


def test_the_interviewer_notes_are_distinct_between_candidates(real):
    """Manual test: Dahlia's and Junko's Technical Interview notes were word-for-word the same."""
    v, _ = shortlist(real["baseline"])
    notes = [i["note"].split(").", 1)[1] for c in v["evidence"] for i in c["interviews"]]
    assert len(notes) == len(set(notes))


def test_a_single_person_gets_their_card_and_an_unranked_answer_follows_the_order_people_are_named(real):
    one, _ = view_of(real["baseline"], [call(("1", "get_candidates", {"names": ["Hannelore Voss"]})),
                                        submit("2", answer_markdown="Hannelore Voss is in the Final Interview.", candidates=["C108"])])
    assert [c["person"] for c in one["evidence"]] == ["Hannelore Voss"] and one["evidence"][0]["reason"] == ""
    unranked, _ = view_of(real["baseline"], [call(("1", "get_candidates", {"role": "Backend Engineer"})),
                                             submit("2", answer_markdown="Junko Takeda, then Dahlia Ramanathan, then Hannelore Voss; Kofi Adeyemi trails.")])
    assert [c["person"] for c in unranked["evidence"]] == ["Junko Takeda", "Dahlia Ramanathan", "Hannelore Voss"]   # order of mention, at most 3


def test_tasks_and_pipeline_answers_get_no_cards(real):
    v, _ = view_of(real["baseline"], [call(("1", "get_tasks", {"overdue_only": True})), submit("2", answer_markdown="Ten tasks are overdue.")])
    assert v["evidence"] == []


# ------------------------------------------------------------------ technical tier and general properties
def test_the_technical_view_is_one_block_per_round(real):
    v, _ = shortlist(real["baseline"])
    t = v["technical"]
    assert t["title"].startswith("How this answer was made · 2 round(s) · 1 tool call(s) · ")
    assert [(r["round"], r["action"], [c["call"] for c in r["calls"]]) for r in t["rounds"]] == [
        (1, "Called 1 tool(s)", ['get_candidates(role="Backend Engineer")']), (2, "Submitted the answer", [])]
    assert t["rounds"][-1]["outcome"] == "accepted" and "8 candidates" in t["rounds"][0]["calls"][0]["returned"]
    assert t["about"][:3] == ["Dahlia Ramanathan", "Junko Takeda", "Hannelore Voss"]


def test_failed_calls_appear_in_the_trace_but_never_as_records(real):
    v, _ = view_of(real["baseline"], [call(("1", "get_pipeline_analytics", {"role": "b;ad"}), ("2", "nope", {}), ("3", "get_pipeline_analytics", {"role": "ROLE-BE"})),
                                      submit("4", answer_markdown="done")])
    calls = v["technical"]["rounds"][0]["calls"]
    assert [(c["call"].split("(")[0], c["ok"]) for c in calls] == [("get_pipeline_analytics", False), ("nope", False), ("get_pipeline_analytics", True)]
    assert calls[0]["returned"] == "failed: invalid_arguments" and v["technical"]["refs"] == ["analytics:ROLE-BE@2026-11-07"]


def test_corrections_are_reported_in_plain_labels_in_the_technical_tier(real):
    bad = lambda sid: submit(sid, answer_markdown="999 people applied.")
    v, _ = view_of(real["baseline"], [bad("a"), bad("b")])
    assert v["technical"]["corrected"] == ["a number that was not in the data"]


def test_the_view_is_json_serialisable(real):
    v, _ = shortlist(real["injected"], ranked=["C104", "C117"])
    assert json.loads(json.dumps(v)) == v


def test_the_default_view_contains_no_internal_identifiers_even_as_hidden_data(real):
    v, _ = shortlist(real["injected"], ranked=["C104", "C117", "C110"])

    def strings(node):
        if isinstance(node, dict):
            for k, x in node.items():
                if k not in ("note", "excerpt"):                      # verbatim source text, shown as inert evidence
                    yield from strings(x)
        elif isinstance(node, list):
            for x in node:
                yield from strings(x)
        elif isinstance(node, str):
            yield node
    for text in strings({k: x for k, x in v.items() if k != "technical"}):
        assert leaks(text) == [], (text[:80], leaks(text))


def test_loading_labels_describe_real_activity():
    assert progress_label({"type": "model"}) == "Working out the answer"
    assert progress_label({"type": "tools", "names": ["get_tasks"]}) == "Reviewing tasks"
    assert progress_label({"type": "tools", "names": ["search_candidates", "get_candidates", "search_candidates"]}) == "Searching candidates and Reading candidate records"
    assert progress_label({"type": "tools", "names": ["something_new"]}) == "Looking things up"
    assert set(TOOL_LABELS) == {"list_roles", "search_candidates", "get_candidates", "get_candidate_communications", "get_pipeline_analytics", "get_tasks", "get_recruiting_overview"}


def test_two_candidates_with_the_same_name_are_told_apart_by_role_then_id():
    reg = EvidenceRegistry()
    reg.add("get_candidates", {"data": {"candidates": [{"candidate_id": "C101", "role_id": "ROLE-BE", "name": "Alex Kim"},
                                                      {"candidate_id": "C121", "role_id": "ROLE-MSE", "name": "Alex Kim"},
                                                      {"candidate_id": "C122", "role_id": "ROLE-MSE", "name": "Alex Kim"},
                                                      {"candidate_id": "C102", "role_id": "ROLE-BE", "name": "Sam Lee"}]},
                               "source_refs": [], "content_flags": [], "warnings": []})
    reg.add("list_roles", {"data": {"roles": [{"role_id": "ROLE-BE", "title": "Backend Engineer"}]}, "source_refs": [], "content_flags": [], "warnings": []})
    assert reg.display_name("C102") == "Sam Lee"
    assert reg.display_name("C101") == "Alex Kim (Backend Engineer)"
    assert reg.display_name("C121") == "Alex Kim (C121)"
