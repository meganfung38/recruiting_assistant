"""Deterministic checkers for the model-dependent evals. They read only structured data:
the trace events (TraceEvent.model_dump()) and the submitted answer (submit_answer fields).

Each returns {"status": "pass" | "fail" | "not_exercised", "reasons": [...], "details": {...}}.
`not_exercised` means the run never hit the situation under test (e.g. the model avoided truncation),
so it counts neither as a pass nor a failure.
"""
import re

CAND = re.compile(r"C\d{3}")
INCOMPLETE = re.compile(r"incomplete|partial|provisional|not (all |every )?(reviewed|retrieved|compared|examined)|"
                        r"could not (review|retrieve|compare)|unable to (review|retrieve|compare)|without (reviewing|retrieving)|"
                        r"only (reviewed|compared|retrieved)", re.I)


def analyze(events: list[dict]) -> dict:
    """What was omitted by size-capped results, what was actually retrieved (a `cv:` record), and what is still missing."""
    omitted, retrieved, requested_after, budget_hit = set(), set(), [], False
    for e in sorted(events, key=lambda e: e["sequence"]):
        if e.get("error_code") in ("call_budget_exceeded", "turn_timeout"):
            budget_hit = True
        if e["status"] != "success":
            if e["tool_name"] == "get_candidates" and e.get("error_code") in (
                    "call_budget_exceeded", "turn_timeout", "tool_timeout", "upstream_error"):
                omitted |= {i for i in e["safe_arguments"].get("candidate_ids", []) if CAND.fullmatch(i)}  # asked for, refused
            continue
        if e["tool_name"] == "get_candidates":
            asked = [i for i in e["safe_arguments"].get("candidate_ids", [])]
            requested_after += [i for i in asked if i in retrieved]       # fetched again although already held
        omitted |= {i for i in e["result_summary"].get("omitted_ids", []) if CAND.fullmatch(i)}
        retrieved |= {r[3:] for r in e["evidence_refs"] if r.startswith("cv:")}
    return {"omitted": omitted, "retrieved": retrieved, "unresolved": omitted - retrieved,
            "budget_exhausted": budget_hit, "refetched": requested_after}


def _named(answer: dict | None) -> set[str]:
    return {c["candidate_id"] for c in (answer or {}).get("candidates", [])}


def _text(answer: dict) -> str:
    return " ".join([*answer.get("uncertainties", []), *answer.get("content_warnings", []),
                     answer.get("answer_markdown", "")])


def result(status: str, reasons: list[str], a: dict) -> dict:
    return {"status": status, "reasons": reasons,
            "details": {k: sorted(v) if isinstance(v, set) else v for k, v in a.items()}}


def truncation_recovery(events: list[dict], answer: dict | None) -> dict:
    """Case 14: every candidate a truncated result omitted must be retrieved before the answer is given."""
    a = analyze(events)
    if not a["omitted"]:
        return result("not_exercised", ["no truncation occurred"], a)
    reasons = []
    if answer is None:
        reasons.append("no answer was submitted")
    if a["unresolved"]:
        reasons.append(f"answered while omitted candidates were never retrieved: {sorted(a['unresolved'])}")
    unreviewed = _named(answer) - a["retrieved"]
    if unreviewed:
        reasons.append(f"answer names candidates whose records were never retrieved: {sorted(unreviewed)}")
    return result("fail" if reasons else "pass", reasons, a)


def truncation_disclosure(events: list[dict], answer: dict | None) -> dict:
    """Case 15: if omitted candidates could not be retrieved, the answer must say so, by ID, and not rank them."""
    a = analyze(events)
    if not a["omitted"]:
        return result("not_exercised", ["no truncation occurred"], a)
    if not a["unresolved"]:
        return result("not_exercised", ["the agent retrieved every omitted candidate within its budget"], a)
    if answer is None:
        return result("fail", ["no answer was submitted"], a)
    reasons, text = [], _text(answer)
    missing = sorted(i for i in a["unresolved"] if not re.search(rf"\b{i}\b", text))
    if missing:
        reasons.append(f"answer does not name the unreviewed candidates: {missing}")
    if not INCOMPLETE.search(" ".join([*answer.get("uncertainties", []), *answer.get("content_warnings", [])])):
        reasons.append("no uncertainty or warning says the comparison is incomplete")
    ranked_unreviewed = sorted(_named(answer) & a["unresolved"])
    if ranked_unreviewed:
        reasons.append(f"answer ranks candidates it never retrieved: {ranked_unreviewed}")
    unreviewed = sorted(_named(answer) - a["retrieved"])
    if unreviewed and not ranked_unreviewed:
        reasons.append(f"answer names candidates whose records were never retrieved: {unreviewed}")
    return result("fail" if reasons else "pass", reasons, a)


CHECKS = {"truncation_recovery": truncation_recovery, "truncation_disclosure": truncation_disclosure}
