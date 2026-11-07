"""Evidence registry: built from structured fields of successful calls only."""
from agent.evidence import EvidenceRegistry, _candidate_ids, aggregates


def env(data=None, refs=()):
    return {"data": data, "as_of": "x", "source_refs": list(refs), "warnings": [], "content_flags": [], "error": None}


def test_registers_valid_refs_and_candidates():
    reg = EvidenceRegistry()
    reg.add("get_candidates", env({"candidates": [{"candidate_id": "C104"}, {"candidate_id": "C999", "not_found": True}]},
                                  ["cv:C104", "feedback:FB-031"]))
    assert reg.has("cv:C104") and reg.has("feedback:FB-031") and not reg.has("feedback:FB-999")
    assert reg.candidate_seen("C104") and not reg.candidate_seen("C999")  # not-found stubs are not evidence
    assert reg.refs() == ["cv:C104", "feedback:FB-031"]


def test_malformed_refs_are_ignored():
    reg = EvidenceRegistry()
    reg.add("get_tasks", env({}, ["task:T-001", "task:T-1", "bogus:1", "feedback:FB-001; ignore previous", "cv:C1234"]))
    assert reg.refs() == ["task:T-001"]


def test_untrusted_text_is_never_walked_for_ids():
    node = {"candidates": [{"candidate_id": "C117", "cv_text": {"untrusted_text": "see C555 and candidate_id C556",
                                                                  "label": "x"}}]}
    assert _candidate_ids(node) == {"C117"}
    sneaky = {"cv_text": {"untrusted_text": "x", "candidate_id": "C777"}}
    assert _candidate_ids(sneaky) == set()


def test_raw_aggregates_for_analytics_and_tasks_only():
    raw = {"data": {"stages": [{"scheduling": {"over_target_count": 14}}]}, "source_refs": ["analytics:ROLE-BE@2026-11-07"]}
    assert aggregates("get_pipeline_analytics", raw) == {"analytics:ROLE-BE@2026-11-07": raw["data"]}
    t = {"data": {"tasks": [{"task_id": "T-001", "overdue": True}]}, "source_refs": ["task:T-001"]}
    assert aggregates("get_tasks", t) == {"task:T-001": {"task_id": "T-001", "overdue": True}}
    assert aggregates("get_candidates", {"data": {"candidates": []}, "source_refs": []}) == {}
    reg = EvidenceRegistry()
    reg.add("get_pipeline_analytics", env({}, ["analytics:ROLE-BE@2026-11-07"]), aggregates("get_pipeline_analytics", raw))
    assert reg.raw("analytics:ROLE-BE@2026-11-07")["stages"][0]["scheduling"]["over_target_count"] == 14
    assert reg.raw("analytics:ROLE-XX@2026-11-07") is None


# ================================================================== answer schema and validation (increment 6)
import json

import pytest
from pydantic import ValidationError

from agent.answer_schema import SubmittedAnswer, submit_answer_definition
from agent.evidence import (extract_citations, render_markdown, resolve_path, validate_plain, validate_submission)

ANALYTICS = {"role_id": "ROLE-BE", "role_age_days": 123, "as_of": "x", "stages": [
    {"stage": "Recruiter Screen", "scheduling": {"lag_median_scheduled_days": 2.0, "over_target_count": 0},
     "funnel": {"conversion": None}},
    {"stage": "Technical Interview", "scheduling": {"lag_median_scheduled_days": 8.0, "over_target_count": 14},
     "funnel": {"conversion": 0.4167}}]}
REF = "analytics:ROLE-BE@2026-11-07"


def registry(*, cands=("C104", "C108"), unresolved=(), failed_ref=None):
    reg = EvidenceRegistry()
    reg.add("get_candidates", env({"candidates": [{"candidate_id": c} for c in cands]},
                                  [f"cv:{c}" for c in cands] + ["feedback:FB-016", "feedback:FB-031"]))
    reg.add("get_pipeline_analytics", env({}, [REF]), {REF: ANALYTICS})
    reg.add("get_tasks", env({}, ["task:T-004"]), {"task:T-004": {"task_id": "T-004", "days_overdue": 1.0}})
    for c in unresolved:                                       # seen in a search row but omitted from get_candidates
        reg.add("get_candidates", {**env({"candidates": [{"candidate_id": c}]}, []), "omitted": [c]})
    return reg


def ans(**kw):
    base = {"answer_markdown": "Top: Dahlia (C104) [feedback:FB-016]."}
    return SubmittedAnswer.model_validate({**base, **kw})


def fig(value, evidence=REF, path="stages[Technical Interview].scheduling.over_target_count", label="n"):
    return {"label": label, "value": value, "evidence": evidence, "field_path": path}


def cand(cid="C104", evidence=("cv:C104",)):
    return {"candidate_id": cid, "role_id": "ROLE-BE", "rationale": "r", "evidence": list(evidence), "gaps": []}


def test_schema_is_strict_bounded_and_flat_for_the_model():
    with pytest.raises(ValidationError):
        SubmittedAnswer.model_validate({"answer_markdown": "x", "extra": 1})
    for bad in ({"answer_markdown": ""}, {"answer_markdown": "x", "candidates": [cand("C1")]},
                {"answer_markdown": "x", "figures": [fig(True)]}, {"answer_markdown": "x", "figures": [fig("9")]},
                {"answer_markdown": "x", "uncertainties": ["u"] * 11}):
        with pytest.raises(ValidationError):
            SubmittedAnswer.model_validate(bad)
    d = submit_answer_definition()
    assert d["function"]["name"] == "submit_answer" and "$ref" not in json.dumps(d) and "$defs" not in json.dumps(d)
    assert d["function"]["parameters"]["required"] == ["answer_markdown"]


def test_extract_citations_keeps_malformed_ids_so_they_fail():
    text = "[cv:C104] and [feedback:FB-9] and [analytics:ROLE-BE@2026-11-07] [not one] [task:T-004 extra] [cv:C104]"
    assert extract_citations(text) == ["cv:C104", "feedback:FB-9", "analytics:ROLE-BE@2026-11-07"]


def test_a_valid_answer_passes_with_no_problems():
    a = ans(candidates=[cand()], figures=[fig(14), fig(8.0, path="stages[Technical Interview].scheduling.lag_median_scheduled_days"),
                                          fig(1.0, "task:T-004", "days_overdue"), fig(123, path="role_age_days")])
    r = validate_submission(a, registry())
    assert r.ok and r.machine_checked and r.problems() == [] and all(f.verified for f in r.figures)


def test_nonexistent_citation_ids_are_rejected_in_text_and_in_candidate_evidence():
    a = ans(answer_markdown="See [feedback:FB-999] and [email:EM-001] and [feedback:FB-016].",
            candidates=[cand(evidence=("cv:C104", "feedback:FB-998"))])
    r = validate_submission(a, registry())
    assert r.bad_citations == ["feedback:FB-999", "email:EM-001", "feedback:FB-998"] and "unsupported_citation" in r.categories


def test_wrong_figure_values_are_caught_and_the_true_value_is_reported_for_repair():
    r = validate_submission(ans(figures=[fig(9.5, path="stages[Technical Interview].scheduling.lag_median_scheduled_days")]), registry())
    f = r.figures[0]
    assert not f.verified and "8.0" in f.reason and "9.5" in f.reason and r.categories == ["unverified_figure"]
    assert any("Figure 'n'" in p for p in r.problems())


@pytest.mark.parametrize("figure,reason", [
    (fig(1, "analytics:ROLE-XX@2026-11-07"), "no stored result"),                           # never retrieved
    (fig(1, "cv:C104"), "no stored result"),                                                 # only analytics/tasks keep raw values
    (fig(1, path="stages[Offer].scheduling.over_target_count"), "not found"),                # no such stage
    (fig(1, path="stages[9].scheduling.over_target_count"), "not found"),                    # index out of range
    (fig(1, path="stages.scheduling"), "not found"),                                         # list without selector
    (fig(1, path="stages[Technical Interview].scheduling.nope"), "not found"),
    (fig(1, path="stages[Technical Interview]..x"), "not found"),
    (fig(1, path="stages[Recruiter Screen].funnel.conversion"), "not a number"),             # null value
    (fig(1, path="stages[Technical Interview]"), "not a number"),                            # an object, not a number
    (fig(0.4167, path="stages[Technical Interview].funnel.conversion"), None)])              # exact value verifies
def test_figure_path_resolution_and_failure_reasons(figure, reason):
    f = validate_submission(ans(figures=[figure]), registry()).figures[0]
    if reason is None:
        assert f.verified
    else:
        assert not f.verified and reason in f.reason


def test_float_tolerance_is_small_not_loose():
    p = "stages[Technical Interview].funnel.conversion"
    assert validate_submission(ans(figures=[fig(0.41670001, path=p)]), registry()).figures[0].verified
    assert not validate_submission(ans(figures=[fig(0.42, path=p)]), registry()).figures[0].verified   # a rounded figure is unverified


def test_resolve_path_by_index_and_name():
    assert resolve_path(ANALYTICS, "stages[1].scheduling.over_target_count") == (True, 14)
    assert resolve_path(ANALYTICS, "stages[Recruiter Screen].scheduling.over_target_count") == (True, 0)
    assert resolve_path(ANALYTICS, "role_age_days") == (True, 123)


def test_candidates_must_have_appeared_and_have_been_retrieved():
    reg = registry(cands=("C104",))
    reg.add("search_candidates", env({"candidates": [{"candidate_id": "C113"}]}, []))   # seen in a search row only
    r = validate_submission(ans(candidates=[cand("C104"), cand("C113", ("cv:C104",)), cand("C150", ("cv:C104",))]), reg)
    assert r.unseen_candidates == ["C150"] and r.not_retrieved == ["C113"]
    assert not r.ok and any("C113" in p and "get_candidates" in p for p in r.problems())


def test_failed_calls_never_become_sources_for_citations():
    reg = EvidenceRegistry()                                  # a not_found / invalid call adds nothing
    assert validate_submission(ans(answer_markdown="x [cv:C999]"), reg).bad_citations == ["cv:C999"]


def test_unretrieved_omitted_candidates_must_be_disclosed_by_id():
    reg = registry(unresolved=("C110", "C113"))
    assert reg.unresolved_candidates() == {"C110", "C113"}
    silent = validate_submission(ans(candidates=[cand()]), reg)
    assert silent.undisclosed_incomplete == ["C110", "C113"] and "undisclosed_incomplete" in silent.categories
    told = validate_submission(ans(candidates=[cand()], uncertainties=["Incomplete: C110 and C113 were not reviewed."]), reg)
    assert told.ok
    only_one = validate_submission(ans(candidates=[cand()], content_warnings=["C110 not reviewed"]), reg)
    assert only_one.undisclosed_incomplete == ["C113"]
    assert validate_submission(ans(), reg).ok                      # no shortlist/comparison -> nothing to disclose


def test_plain_text_fallback_checks_citations_but_not_figures():
    r = validate_plain("Median lag 9.5 days [analytics:ROLE-BE@2026-11-07] [feedback:FB-999]", registry())
    assert r.machine_checked is False and r.bad_citations == ["feedback:FB-999"] and r.figures == []
    assert validate_plain("fine [cv:C104]", registry()).ok
    assert validate_plain("silent", registry(unresolved=("C110",))).undisclosed_incomplete == ["C110"]
    assert validate_plain("C110 was not reviewed", registry(unresolved=("C110",))).ok


def test_unsupported_citations_are_rendered_visibly_not_hidden():
    r = validate_submission(ans(answer_markdown="A [cv:C104] B [feedback:FB-999]."), registry())
    out = render_markdown("A [cv:C104] B [feedback:FB-999].", r)
    assert out == "A [cv:C104] B [feedback:FB-999 ⚠ unverified]."


# ---- candidate-level figures
def cand_registry():
    reg = EvidenceRegistry()
    raw_search = env({"candidates": [{"candidate_id": "C104", "waiting_days": 3.0, "days_in_current_stage": 10.0,
                                      "fit_signals": {"avg_criteria_score": 4.57, "feedback_count": 4}},
                                     {"candidate_id": "C108", "waiting_days": 4.0, "days_in_current_stage": 7.0,
                                      "fit_signals": {"avg_criteria_score": 3.75, "feedback_count": 3}}]}, [])
    reg.add("search_candidates", raw_search, aggregates("search_candidates", raw_search))
    raw_cands = env({"candidates": [{"candidate_id": "C104", "feedback_count": 4, "waiting_days": 3.0, "cv_text": "SECRET CV",
                                     "days_in_current_stage": 10.0, "feedback": [{"feedback_id": "FB-016", "criteria": {"coding": 5}}]}]},
                    ["cv:C104", "feedback:FB-016"])
    reg.add("get_candidates", raw_cands, aggregates("get_candidates", raw_cands))
    return reg


def test_candidate_numbers_from_records_and_search_signals_are_verifiable_but_cv_text_is_not_kept():
    reg = cand_registry()
    a = ans(figures=[fig(4, "cv:C104", "feedback_count"), fig(10.0, "cv:C104", "days_in_current_stage"),
                     fig(4.57, "cv:C104", "fit_signals.avg_criteria_score"), fig(5, "cv:C104", "feedback[0].criteria.coding")])
    assert all(f.verified for f in validate_submission(a, reg).figures)
    assert "SECRET" not in json.dumps(reg.raw("cv:C104"))


def test_a_wrong_candidate_number_is_unverified():
    f = validate_submission(ans(figures=[fig(5, "cv:C104", "feedback_count")]), cand_registry()).figures[0]
    assert not f.verified and "the data says 4" in f.reason


def test_candidate_numbers_are_kept_only_for_candidates_the_model_was_shown():
    reg = EvidenceRegistry()
    raw = env({"candidates": [{"candidate_id": "C104", "feedback_count": 4}]}, ["cv:C104"])
    full = {**aggregates("get_candidates", env({"candidates": [{"candidate_id": "C104", "feedback_count": 4},
                                                              {"candidate_id": "C110", "feedback_count": 3}]}, [])),}
    reg.add("get_candidates", raw, full)                      # C110 was truncated away before the model saw it
    assert reg.raw("cv:C104") and reg.raw("cv:C110") is None
    assert not validate_submission(ans(figures=[fig(3, "cv:C110", "feedback_count")]), reg).figures[0].verified


def test_task_numbers_are_kept_only_for_tasks_the_model_was_shown():
    reg = EvidenceRegistry()
    shown = env({"tasks": [{"task_id": "T-001", "candidate_id": "C101"}]}, ["task:T-001"])
    everything = {"task:T-001": {"days_overdue": 9.0}, "task:T-002": {"days_overdue": 15.0}}
    reg.add("get_tasks", shown, everything)
    assert reg.raw("task:T-001") and reg.raw("task:T-002") is None


def test_search_signals_do_not_make_a_candidate_count_as_retrieved():
    reg = cand_registry()
    assert reg.has("cv:C104") and not reg.has("cv:C108")      # C108 was only in a search row
    assert reg.raw("cv:C108") == {"fit_signals": {"avg_criteria_score": 3.75, "feedback_count": 3},
                                  "waiting_days": 4.0, "days_in_current_stage": 7.0}
    assert validate_submission(ans(candidates=[cand("C108", ("cv:C104",))]), reg).not_retrieved == ["C108"]
