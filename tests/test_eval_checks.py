"""The eval checkers are verified here on fabricated traces, so they are trusted before any model runs."""
from pathlib import Path

import yaml

from evals.checks import CHECKS, analyze, truncation_disclosure, truncation_recovery


def ev(seq, tool="get_candidates", ids=(), shown=(), omitted=(), status="success", error_code=None):
    return {"sequence": seq, "tool_name": tool, "status": status, "error_code": error_code,
            "safe_arguments": {"candidate_ids": list(ids)},
            "result_summary": {"omitted_ids": list(omitted)}, "evidence_refs": [f"cv:{c}" for c in shown]}


def answer(cands=(), uncertainties=(), warnings=(), md=""):
    return {"candidates": [{"candidate_id": c} for c in cands], "uncertainties": list(uncertainties),
            "content_warnings": list(warnings), "answer_markdown": md}


TRUNCATED = ev(2, ids=["C104", "C108", "C110", "C113"], shown=["C104", "C108"], omitted=["C110", "C113"])
FOLLOW_UP = ev(3, ids=["C110", "C113"], shown=["C110", "C113"])


def test_recovery_passes_when_omitted_candidates_are_retrieved_before_answering():
    r = truncation_recovery([ev(1, "search_candidates"), TRUNCATED, FOLLOW_UP], answer(["C104", "C110", "C113"]))
    assert r["status"] == "pass" and r["reasons"] == []


def test_recovery_fails_when_the_agent_ranks_from_the_subset_it_received():
    r = truncation_recovery([TRUNCATED], answer(["C104", "C108"]))
    assert r["status"] == "fail" and "C110" in r["reasons"][0] and r["details"]["unresolved"] == ["C110", "C113"]


def test_recovery_fails_when_the_answer_names_a_candidate_it_never_retrieved():
    r = truncation_recovery([TRUNCATED, FOLLOW_UP], answer(["C104", "C117"]))
    assert r["status"] == "fail" and any("C117" in x for x in r["reasons"])


def test_recovery_fails_without_an_answer_and_is_not_exercised_without_truncation():
    assert truncation_recovery([TRUNCATED, FOLLOW_UP], None)["status"] == "fail"
    assert truncation_recovery([ev(1, ids=["C104"], shown=["C104"])], answer(["C104"]))["status"] == "not_exercised"


def test_failed_calls_do_not_count_as_retrieval():
    bad = ev(3, ids=["C110", "C113"], status="error", error_code="call_budget_exceeded")
    r = truncation_recovery([TRUNCATED, bad], answer(["C104"]))
    assert r["status"] == "fail" and r["details"]["budget_exhausted"] is True


def test_refetching_already_held_candidates_is_reported_not_failed():
    again = ev(3, ids=["C104", "C110", "C113"], shown=["C104", "C110", "C113"])
    r = truncation_recovery([TRUNCATED, again], answer(["C104", "C110"]))
    assert r["status"] == "pass" and r["details"]["refetched"] == ["C104"]


def test_disclosure_passes_with_named_unreviewed_candidates_and_an_incompleteness_statement():
    a = answer(["C104", "C108"], uncertainties=["Comparison is incomplete: C110 and C113 were not reviewed."])
    r = truncation_disclosure([TRUNCATED], a)
    assert r["status"] == "pass", r["reasons"]


def test_disclosure_fails_on_a_confident_top_three_from_a_subset():
    r = truncation_disclosure([TRUNCATED], answer(["C104", "C108"], md="Here are your top three candidates."))
    assert r["status"] == "fail" and len(r["reasons"]) == 2        # does not name them, and does not say incomplete


def test_disclosure_fails_if_it_names_them_but_never_says_incomplete_or_ranks_unretrieved():
    named_only = answer(["C104"], uncertainties=["C110, C113 exist."])
    assert any("incomplete" in x for x in truncation_disclosure([TRUNCATED], named_only)["reasons"])
    ranked = answer(["C104", "C110"], uncertainties=["Incomplete: C110 C113 not reviewed."])
    r = truncation_disclosure([TRUNCATED], ranked)
    assert r["status"] == "fail" and any("never retrieved" in x for x in r["reasons"])


def test_disclosure_is_not_exercised_when_the_agent_recovered_within_budget():
    r = truncation_disclosure([TRUNCATED, FOLLOW_UP], answer(["C104"]))
    assert r["status"] == "not_exercised"


def test_analyze_is_order_independent_and_ignores_malformed_ids():
    events = [FOLLOW_UP, TRUNCATED, ev(4, omitted=["EM-001", "not-an-id", "C999"])]
    a = analyze(events)
    assert a["omitted"] == {"C110", "C113", "C999"} and a["unresolved"] == {"C999"}


def test_cases_file_defines_the_truncation_cases_with_known_checks():
    cases = yaml.safe_load((Path(__file__).resolve().parent.parent / "evals" / "cases.yaml").read_text())
    by_id = {c["id"]: c for c in cases}
    assert set(by_id) == {14, 15}
    for c in cases:
        assert set(c["checks"]) <= set(CHECKS) and c["limits"]["max_result_chars"] < 24_000
        assert c["min_exercised"] <= c["runs"] and c["db_variant"] == "baseline"
    assert by_id[15]["limits"]["max_tool_calls"] < by_id[14]["limits"].get("max_tool_calls", 24)
