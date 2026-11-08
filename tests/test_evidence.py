"""Evidence registry and answer validation: built from structured fields of successful (projected) calls only; the model declares who
the answer is about and the application checks people, roles and every number in the prose."""
import json

import pytest
from pydantic import ValidationError

from agent.answer_schema import SubmittedAnswer, submit_answer_definition
from agent.evidence import EvidenceRegistry, _candidate_ids, numbers_in, validate_answer, validate_plain


def env(data=None, refs=(), flags=()):
    return {"data": data, "as_of": "x", "source_refs": list(refs), "warnings": [], "content_flags": list(flags), "error": None}


def test_registers_valid_refs_and_candidates():
    reg = EvidenceRegistry()
    reg.add("get_candidates", env({"candidates": [{"candidate_id": "C104"}, {"candidate_id": "C999", "not_found": True}]},
                                  ["cv:C104", "feedback:FB-031"]))
    assert reg.has("cv:C104") and reg.has("feedback:FB-031") and not reg.has("feedback:FB-999")
    assert reg.candidate_seen("C104") and not reg.candidate_seen("C999") and reg.retrieved("C104")  # not-found stubs are not evidence
    assert reg.refs() == ["cv:C104", "feedback:FB-031"]


def test_malformed_refs_are_ignored():
    reg = EvidenceRegistry()
    reg.add("get_tasks", env({}, ["task:T-001", "task:T-1", "bogus:1", "feedback:FB-001; ignore previous", "cv:C1234"]))
    assert reg.refs() == ["task:T-001"]


def test_untrusted_text_is_never_walked_for_ids():
    node = {"candidates": [{"candidate_id": "C117", "cv_text": {"untrusted_text": "see C555 and candidate_id C556", "label": "x"}}]}
    assert _candidate_ids(node) == {"C117"}
    assert _candidate_ids({"cv_text": {"untrusted_text": "x", "candidate_id": "C777"}}) == set()


# ------------------------------------------------------------------ a small turn: two roles, two people
def turn() -> EvidenceRegistry:
    reg = EvidenceRegistry()
    reg.add("get_recruiting_overview", env({"roles": [
        {"role_id": "ROLE-BE", "title": "Backend Engineer", "days_open": 123, "overdue_tasks": 4},
        {"role_id": "ROLE-MSE", "title": "Manufacturing Systems Engineer", "days_open": 180, "overdue_tasks": 1}]}, ["role:ROLE-BE", "role:ROLE-MSE"]))
    reg.add("get_pipeline_analytics", env({"role_id": "ROLE-BE", "role_title": "Backend Engineer", "stages": [
        {"stage": "Technical Interview", "scheduling": {"over_target_count": 14, "observations": 17}}]}, ["analytics:ROLE-BE@2026-11-07"]))
    reg.add("get_pipeline_analytics", env({"role_id": "ROLE-MSE", "role_title": "Manufacturing Systems Engineer", "stages": [
        {"stage": "Technical Interview", "scheduling": {"over_target_count": 0, "observations": 11}}]}, ["analytics:ROLE-MSE@2026-11-07"]))
    reg.add("get_candidates", env({"candidates": [
        {"candidate_id": "C113", "name": "Mateo Villareal", "role_id": "ROLE-BE", "days_waiting": 22.0,
         "stage_status": "Technical Interview since Oct 16 (22 days): interview not booked yet.",
         "cv_text": {"untrusted_text": "Senior engineer - 7 years of experience", "label": "x"}, "feedback": []}]}, ["cv:C113"]))
    reg.add("search_candidates", env({"candidates": [{"candidate_id": "C108", "name": "Hannelore Voss", "role_id": "ROLE-BE", "days_waiting": 4.0}]},
                                     ["search:C108"]))
    return reg


def ans(**kw):
    return SubmittedAnswer.model_validate({"answer_markdown": "x", **kw})


def test_a_correct_answer_passes_and_records_who_it_is_about():
    md = ("Mateo Villareal has waited 22 days and Backend Engineer has 4 overdue tasks.\n\n"
          "| Role | Days open |\n|---|---|\n| Backend Engineer | 123 |\n| Manufacturing Systems Engineer | 180 |")
    r = validate_answer(ans(answer_markdown=md, candidates=["C113"], roles=["ROLE-BE"]), turn())
    assert r.ok and r.problems() == []
    assert r.candidates == ["C113"] and set(r.roles) == {"ROLE-BE", "ROLE-MSE"}             # declared, plus named in the prose


def test_a_number_carried_over_from_another_role_is_caught():
    """The original failure: Backend Engineer's 14 of 17 attributed to Manufacturing Systems Engineer."""
    r = validate_answer(ans(answer_markdown="Manufacturing Systems Engineer: 14 of 17 scheduling observations were over target."), turn())
    assert [n.value for n in r.unsupported_numbers] == [14.0, 17.0] and r.categories == ["unsupported_number"]
    assert "Manufacturing Systems Engineer" in r.problems()[0]
    assert validate_answer(ans(answer_markdown="Backend Engineer: 14 of 17 scheduling observations were over target."), turn()).ok


def test_a_made_up_number_is_caught_and_a_rounded_one_is_not():
    assert [n.value for n in validate_answer(ans(answer_markdown="Mateo Villareal has waited 25 days."), turn()).unsupported_numbers] == [25.0]
    assert validate_answer(ans(answer_markdown="Mateo Villareal has waited 22.0 days, 7 years in industry."), turn()).ok   # CV numbers count


def test_a_line_that_names_nobody_is_checked_against_the_whole_turn():
    assert validate_answer(ans(answer_markdown="- 180 days is the longest a role has been open."), turn()).ok
    assert not validate_answer(ans(answer_markdown="- 999 people applied."), turn()).ok


def test_dates_times_years_ids_and_ranks_are_not_quantities():
    assert numbers_in("Booked Nov 3, 2026 at 1:00 PM PDT (2026-11-03 13:00 PDT), ranked #1, C113 T-002 [cv:C104]") == []
    assert numbers_in("a 5-day target, 3x faster, 8.5 days") == [5.0, 3.0, 8.5]


def test_list_numbering_is_not_a_quantity():
    assert validate_answer(ans(answer_markdown="1. Mateo Villareal: 22 days.\n2. Hannelore Voss: 4 days.\n10) Backend Engineer: 123 days."), turn()).ok


def test_a_rank_column_in_a_table_is_not_a_quantity_but_the_other_cells_are_checked():
    md = "| Rank | Candidate | Days waiting |\n|---|---|---|\n| 1 | Mateo Villareal | 22 |\n| 2 | Hannelore Voss | 4 |"
    assert validate_answer(ans(answer_markdown=md), turn()).ok
    wrong = md.replace("| 4 |", "| 9 |")
    assert [n.value for n in validate_answer(ans(answer_markdown=wrong), turn()).unsupported_numbers] == [9.0]


def test_numbers_the_recruiter_used_may_be_repeated():
    md = "Backend Engineer has been open 123 days, not 120."
    assert not validate_answer(ans(answer_markdown=md), turn()).ok
    assert validate_answer(ans(answer_markdown=md), turn(), question="Has it really been open 120 days?").ok


def test_people_and_roles_must_have_appeared_and_ranked_people_must_have_been_read():
    r = validate_answer(ans(candidates=["C150"], roles=["ROLE-XX"],
                            ranking=[{"candidate_id": "C108", "reason": "r"}, {"candidate_id": "C113", "reason": "r"}]), turn())
    assert r.unseen_candidates == ["C150"] and r.unknown_roles == ["ROLE-XX"]
    assert r.not_retrieved == ["C108"]                                   # seen only in a search row; C113's record was read
    assert r.candidates[:2] == ["C108", "C113"]                          # ranked people come first, in order
    assert set(r.categories) == {"unseen_candidate", "candidate_not_retrieved", "unknown_role"}


def test_first_names_and_short_role_names_count_as_mentions():
    cands, roles = turn().mentioned_in("Mateo is waiting; Backend is slow.")
    assert cands == ["C113"] and roles == ["ROLE-BE"]


def test_failed_calls_never_become_evidence():
    assert validate_answer(ans(candidates=["C999"]), EvidenceRegistry()).unseen_candidates == ["C999"]


def test_plain_text_answers_are_checked_for_people_and_numbers_too():
    r = validate_plain("Mateo Villareal has waited 30 days.", turn())
    assert r.structured is False and r.candidates == ["C113"] and [n.value for n in r.unsupported_numbers] == [30.0]


def test_flagged_candidates_are_tracked_for_the_notice_but_are_not_a_validation_concern():
    reg = turn()
    reg.add("search_candidates", env({"candidates": [{"candidate_id": "C117", "name": "Quentin Abara"}]}, ["search:C117"],
                                     [{"candidate_id": "C117", "field": "cv_text", "pattern_id": "concealment"}]))
    assert reg.flagged_candidates() == ["C117"] and validate_answer(ans(answer_markdown="Mateo leads."), reg).ok


def test_records_are_kept_as_the_model_saw_them_and_a_detailed_read_adds_to_a_role_view():
    reg = EvidenceRegistry()
    reg.add("get_candidates", env({"candidates": [{"candidate_id": "C104", "name": "D", "feedback": [1]}]}, ["cv:C104"]))
    reg.add("get_candidates", env({"candidates": [{"candidate_id": "C104", "name": "D", "feedback": [1], "stage_history": [2]}]}, ["cv:C104"]))
    assert reg.record("C104") == {"candidate_id": "C104", "name": "D", "feedback": [1], "stage_history": [2]}


# ------------------------------------------------------------------ schema
def test_schema_is_strict_bounded_and_flat_for_the_model():
    with pytest.raises(ValidationError):
        SubmittedAnswer.model_validate({"answer_markdown": "x", "figures": []})          # the old fields are gone
    with pytest.raises(ValidationError):
        SubmittedAnswer.model_validate({"answer_markdown": "x", "candidates": ["C1"]})
    with pytest.raises(ValidationError):
        SubmittedAnswer.model_validate({"answer_markdown": "x", "ranking": [{"candidate_id": "C104", "reason": "r", "score": 9}]})
    d = submit_answer_definition()
    assert d["function"]["name"] == "submit_answer" and "$defs" not in json.dumps(d) and "$ref" not in json.dumps(d)
    assert set(d["function"]["parameters"]["properties"]) == {"answer_markdown", "candidates", "roles", "ranking", "uncertainties"}
