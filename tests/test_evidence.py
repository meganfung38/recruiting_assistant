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
        reg.add("search_candidates", env({"candidates": [{"candidate_id": c}]}, [f"cv:{c}"]))
        reg.add("get_candidates", {**env({"candidates": []}, []), "omitted": [c]})
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




def test_plain_text_fallback_checks_citations_but_not_figures():
    r = validate_plain("Median lag 9.5 days [analytics:ROLE-BE@2026-11-07] [feedback:FB-999]", registry())
    assert r.machine_checked is False and r.bad_citations == ["feedback:FB-999"] and r.figures == []
    assert validate_plain("fine [cv:C104]", registry()).ok


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
                                      "fit_signals": {"avg_criteria_score": 3.75, "feedback_count": 3}}]}, ["search:C104", "search:C108"])
    reg.add("search_candidates", raw_search, aggregates("search_candidates", raw_search))
    raw_cands = env({"candidates": [{"candidate_id": "C104", "feedback_count": 4, "waiting_days": 3.0, "cv_text": "SECRET CV",
                                     "days_in_current_stage": 10.0, "feedback": [{"feedback_id": "FB-016", "criteria": {"coding": 5}}]}]},
                    ["cv:C104", "feedback:FB-016"])
    reg.add("get_candidates", raw_cands, aggregates("get_candidates", raw_cands))
    return reg


def test_candidate_numbers_from_records_and_search_signals_are_verifiable_but_cv_text_is_not_kept():
    reg = cand_registry()
    a = ans(figures=[fig(4, "cv:C104", "feedback_count"), fig(10.0, "cv:C104", "days_in_current_stage"),
                     fig(4.57, "search:C104", "fit_signals.avg_criteria_score"), fig(5, "cv:C104", "feedback[0].criteria.coding")])
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
    assert reg.retrieved("C104") and not reg.retrieved("C108")     # C108 was only in a search row: not retrieved ...
    assert reg.raw("search:C108") == {"fit_signals": {"avg_criteria_score": 3.75, "feedback_count": 3},
                                      "waiting_days": 4.0, "days_in_current_stage": 7.0}
    assert reg.has("search:C108") and not reg.has("cv:C108")        # ... citable as a search row, never as a retrieved record
    assert reg.raw("cv:C108") is None
    assert validate_submission(ans(candidates=[cand("C108", ("search:C108",))]), reg).not_retrieved == ["C108"]


def test_a_candidate_that_was_requested_but_omitted_counts_as_not_retrieved_not_as_unseen():
    reg = registry(unresolved=("C110",))
    r = validate_submission(ans(candidates=[cand("C110", ("cv:C104",)), cand("C150", ("cv:C104",))]), reg)
    assert r.not_retrieved == ["C110"] and r.unseen_candidates == ["C150"]                 # a never-requested ID is a different problem




def test_flagged_candidates_are_tracked_for_the_notice_but_are_not_a_validation_concern():
    reg = registry()
    reg.add("search_candidates", {**env({"candidates": [{"candidate_id": "C117"}]}, ["search:C117"]),
                                  "content_flags": [{"candidate_id": "C117", "field": "cv_text", "pattern_id": "concealment"}]})
    assert reg.flagged_candidates() == ["C117"]
    assert validate_submission(ans(answer_markdown="Top: Dahlia."), reg).ok         # no warning from the model is required
    assert validate_plain("All fine.", reg).ok and validate_submission(ans(), registry()).ok


# ---- cv: (record retrieved) versus search: (search row seen)
def test_cv_means_retrieved_and_search_means_a_search_row_only():
    reg = cand_registry()                                                    # C104: searched AND retrieved; C108: searched only
    ok = validate_submission(ans(answer_markdown="[cv:C104] [search:C104] [search:C108]"), reg)
    assert ok.ok and ok.bad_citations == []
    bad = validate_submission(ans(answer_markdown="C108 has a flag [cv:C108]."), reg)
    assert bad.bad_citations == ["cv:C108"] and "only saw C108 in a search result" in bad.problems()[0]
    assert "[search:C108]" in bad.problems()[0] and "get_candidates" in bad.problems()[0]


def test_citing_search_for_a_candidate_you_retrieved_is_valid_but_citing_cv_for_a_never_seen_one_is_not():
    reg = cand_registry()
    assert validate_submission(ans(answer_markdown="[search:C104]"), reg).ok         # search rows are real returned records too
    assert validate_submission(ans(answer_markdown="[cv:C150] [search:C150]"), reg).bad_citations == ["cv:C150", "search:C150"]
    reg2 = EvidenceRegistry()
    reg2.add("get_candidates", env({"candidates": [{"candidate_id": "C104"}]}, ["cv:C104"]))
    hint = validate_submission(ans(answer_markdown="[search:C104]"), reg2)
    assert hint.bad_citations == ["search:C104"] and "You retrieved C104" in hint.problems()[0] and "[cv:C104]" in hint.problems()[0]


def test_a_figure_is_verified_where_it_actually_lives_and_the_true_source_is_reported():
    reg = cand_registry()
    own = validate_submission(ans(figures=[fig(4.57, "search:C104", "fit_signals.avg_criteria_score")]), reg).figures[0]
    assert own.verified and own.source_ref == "search:C104" and not own.relabelled
    slip = validate_submission(ans(figures=[fig(4.57, "cv:C104", "fit_signals.avg_criteria_score")]), reg)
    f = slip.figures[0]                                                              # cited as cv:, but it is a search-row signal
    assert slip.ok and f.verified and f.relabelled and f.source_ref == "search:C104" and f.evidence == "cv:C104"
    assert validate_submission(ans(figures=[fig(4, "search:C104", "feedback_count")]), reg).figures[0].relabelled   # and the reverse slip
    assert validate_submission(ans(figures=[fig(4, "cv:C104", "feedback_count")]), reg).figures[0].relabelled is False


def test_relabelling_never_rescues_a_wrong_value_or_an_unseen_source():
    reg = cand_registry()
    wrong = validate_submission(ans(figures=[fig(4.0, "cv:C104", "fit_signals.avg_criteria_score")]), reg).figures[0]
    assert not wrong.verified and "the data says 4.57" in wrong.reason and wrong.source_ref == "search:C104"
    c108 = validate_submission(ans(figures=[fig(3.75, "cv:C108", "fit_signals.avg_criteria_score")]), reg).figures[0]
    assert c108.verified and c108.relabelled and c108.source_ref == "search:C108"      # C108 has only a search row, which holds it
    nothing = validate_submission(ans(figures=[fig(1, "cv:C150", "feedback_count")]), reg).figures[0]
    assert not nothing.verified and "no stored result" in nothing.reason             # no candidate record or search row at all


def test_flagged_search_only_candidates_can_be_cited_honestly_as_search():
    reg = EvidenceRegistry()
    reg.add("search_candidates", {**env({"candidates": [{"candidate_id": "C117"}]}, ["search:C117"]),
                                  "content_flags": [{"candidate_id": "C117", "field": "cv_text", "pattern_id": "concealment"}]})
    a = ans(answer_markdown="C117's CV was flagged [search:C117].", content_warnings=["C117 had instruction-like text; ignored."])
    assert validate_submission(a, reg).ok
    assert not reg.retrieved("C117") and not reg.has("cv:C117")                      # the recruiter can see the CV was never read


def test_a_roles_age_is_a_verifiable_figure_via_its_role_reference():
    roles = [{"role_id": "ROLE-BE", "title": "Backend Engineer", "opened_at": "2026-07-07T20:00:00Z", "role_age_days": 123},
             {"role_id": "ROLE-MSE", "title": "Manufacturing Systems Engineer", "opened_at": "2026-05-11T20:00:00Z", "role_age_days": 180}]
    raw = env({"roles": roles}, ["role:ROLE-BE", "role:ROLE-MSE"])
    reg = EvidenceRegistry()
    reg.add("list_roles", raw, aggregates("list_roles", raw))
    ok = validate_submission(ans(answer_markdown="MSE is oldest [role:ROLE-MSE].", figures=[fig(180, "role:ROLE-MSE", "role_age_days")]), reg)
    assert ok.figures[0].verified and not ok.bad_citations
    assert not validate_submission(ans(figures=[fig(180, "role:ROLE-BE", "role_age_days")]), reg).figures[0].verified      # right number, wrong role


def _two_roles_registry():
    """Analytics for two roles whose Technical Interview numbers differ, plus overview counts per role."""
    reg = EvidenceRegistry()
    for role, over in (("ROLE-BE", 14), ("ROLE-MSE", 0)):
        ref = f"analytics:{role}@2026-11-07"
        data = {"role_id": role, "stages": [{"stage": "Technical Interview", "scheduling": {"over_target_count": over, "observations": 17}}]}
        reg.add("get_pipeline_analytics", env(data, [ref]), {ref: data})
    rows = [{"role_id": "ROLE-BE", "title": "Backend Engineer", "overdue_tasks": 4}, {"role_id": "ROLE-PO", "title": "People Ops", "overdue_tasks": 5}]
    overview = {"roles": rows, "waiting_to_be_scheduled": [{"candidate_id": "C155", "waiting_days": 4.0}]}
    reg.add("get_recruiting_overview", env(overview, ["role:ROLE-BE", "role:ROLE-PO", "search:C155"]),
            aggregates("get_recruiting_overview", {"data": overview, "source_refs": ["role:ROLE-BE", "role:ROLE-PO", "search:C155"]}))
    return reg


def test_a_number_carried_over_from_another_role_is_never_verified():
    """The original failure: Backend Engineer's 14 over-target observations cited as Manufacturing's. MSE's own field says 0."""
    path = "stages[Technical Interview].scheduling.over_target_count"
    r = validate_submission(ans(figures=[fig(14, "analytics:ROLE-MSE@2026-11-07", path)]), _two_roles_registry()).figures[0]
    assert not r.verified and "the data says 0" in r.reason


def test_a_role_count_cited_to_the_wrong_kind_of_record_is_verified_at_its_true_source():
    r = validate_submission(ans(figures=[fig(5, "search:C155", "overdue_tasks")]), _two_roles_registry()).figures[0]
    assert r.verified and r.relabelled and r.source_ref == "role:ROLE-PO"
    wrong = validate_submission(ans(figures=[fig(9, "search:C155", "overdue_tasks")]), _two_roles_registry()).figures[0]
    assert not wrong.verified                                                    # no record anywhere holds 9 there


def test_task_totals_and_tasks_inside_a_record_can_be_verified():
    reg = EvidenceRegistry()
    tasks = {"tasks": [{"task_id": "T-002", "days_overdue": 15.0}], "total_matching": 1, "returned": 1, "overdue_total": 10, "pending_total": 21}
    raw = {"data": tasks, "source_refs": ["task:T-002"]}
    reg.add("get_tasks", env(tasks, ["task:T-002"]), aggregates("get_tasks", raw))
    totals = validate_submission(ans(figures=[fig(21, "task:T-002", "pending_total")]), reg).figures[0]
    assert totals.verified and totals.source_ref == "tasks:T-002"
    rec = {"candidates": [{"candidate_id": "C113", "pending_tasks": [{"task_id": "T-005", "days_overdue": 3.0}]}]}
    reg.add("get_candidates", env(rec, ["cv:C113", "task:T-005"]), aggregates("get_candidates", {"data": rec, "source_refs": ["cv:C113", "task:T-005"]}))
    assert validate_submission(ans(figures=[fig(3.0, "task:T-005", "days_overdue")]), reg).figures[0].verified


def test_a_malformed_path_is_looked_up_by_its_field_name_but_never_against_the_cited_value():
    reg = _two_roles_registry()
    ok = validate_submission(ans(figures=[fig(5, "analytics:ROLE-PO@2026-11-07", "roles[People Operations].overdue_tasks")]), reg).figures[0]
    assert ok.verified and ok.source_ref == "role:ROLE-PO"                    # the number exists, under its true source
    bad = validate_submission(ans(figures=[fig(7, "analytics:ROLE-PO@2026-11-07", "roles[People Operations].overdue_tasks")]), reg).figures[0]
    assert not bad.verified
