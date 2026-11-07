"""Deterministic pipeline analytics, per role and stage, relative to `as_of`.

Three different targets are used for three different clocks and are never substituted:
  duration_days   -> time in stage          scheduling_days -> entered to booked
  feedback_days   -> interview to feedback submitted
"Over target" is strictly greater than. Unscheduled candidates are scheduling observations (lower
bounds), never silently dropped. Recorded reasons are not proven causes.
"""
from datetime import datetime
from statistics import median

from services.clock import AS_OF, iso_z, parse_z
from services.db import load_role, rows, use

LOW_SAMPLE_N = 5


def days_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400


def _r(x):
    return None if x is None else round(x, 4)


def _med(xs):
    return _r(median(xs)) if xs else None


def waiting_state(event: dict, feedback: list[dict], as_of: datetime = AS_OF) -> tuple[str, float]:
    """(state, wait_days) for an ACTIVE candidate's current stage event; `feedback` is for that stage."""
    if event["stage"] == "Resume Review":
        return "in_review", days_between(parse_z(event["entered_at"]), as_of)
    if event["scheduled_at"] is None:
        return "awaiting_scheduling", days_between(parse_z(event["entered_at"]), as_of)
    if event["interview_at"] is None:
        return "awaiting_interview", days_between(parse_z(event["scheduled_at"]), as_of)
    if not feedback:
        return "awaiting_feedback", days_between(parse_z(event["interview_at"]), as_of)
    latest = max(parse_z(f["submitted_at"]) for f in feedback)
    return "awaiting_decision", days_between(latest, as_of)


def _funnel(evs, is_last):
    out = {k: sum(e["exit_outcome"] == k for e in evs) for k in ("advanced", "rejected", "withdrawn")}
    denom = sum(out.values())
    f = {"entered": len({e["candidate_id"] for e in evs}), **out,
         "still_in_stage": sum(e["exited_at"] is None for e in evs),
         "conversion": None, "conversion_denominator": denom}
    if is_last:
        f["conversion_note"] = "no offer stage in dataset"
    elif denom > 0:
        f["conversion"] = _r(out["advanced"] / denom)
    return f


def _duration(evs, target, as_of):
    done = [days_between(parse_z(e["entered_at"]), parse_z(e["exited_at"])) for e in evs if e["exited_at"]]
    act = [days_between(parse_z(e["entered_at"]), as_of) for e in evs if not e["exited_at"]]
    over = sum(d > target for d in done)
    return {"completed_median_days": _med(done), "completed_n": len(done), "completed_over_target": over,
            "share_over_target": _r(over / len(done)) if done else None,
            "active_n": len(act), "active_median_age_days": _med(act), "active_max_age_days": _r(max(act)) if act else None,
            "active_over_target": sum(d > target for d in act)}


def _scheduling(evs, target, as_of):
    obs = []  # (kind, wait_days, outcome)
    for e in evs:
        ent = parse_z(e["entered_at"])
        if e["scheduled_at"]:
            obs.append(("scheduled", days_between(ent, parse_z(e["scheduled_at"])), None))
        elif e["exited_at"] is None:
            obs.append(("awaiting_scheduling", days_between(ent, as_of), None))
        else:
            obs.append(("exited_unscheduled", days_between(ent, parse_z(e["exited_at"])), e["exit_outcome"]))
    sched = [w for k, w, _ in obs if k == "scheduled"]
    wait = [w for k, w, _ in obs if k == "awaiting_scheduling"]
    by_outcome = {}
    for k, _, o in obs:
        if k == "exited_unscheduled":
            by_outcome[o] = by_outcome.get(o, 0) + 1
    over = sum(w > target for _, w, _ in obs)
    return {"scheduled_n": len(sched), "lag_median_scheduled_days": _med(sched),
            "awaiting_scheduling": len(wait), "unscheduled_active_wait_median_days": _med(wait),
            "unscheduled_active_wait_max_days": _r(max(wait)) if wait else None,
            "unscheduled_active_over_target": sum(w > target for w in wait),
            "exited_unscheduled_by_outcome": by_outcome,
            "over_target_count": over, "observations": len(obs),
            "share_over_target": _r(over / len(obs)) if obs else None}


def _feedback(evs, fb_by, target, as_of):
    held = [e for e in evs if e["interview_at"]]
    lags, overdue, not_due = [], 0, 0
    for e in held:
        f = fb_by.get((e["candidate_id"], e["stage"]))
        if f:
            lags.append(days_between(parse_z(e["interview_at"]), parse_z(f["submitted_at"])))
        elif days_between(parse_z(e["interview_at"]), as_of) > target:
            overdue += 1
        else:
            not_due += 1
    over = sum(lag > target for lag in lags)
    denom = len(lags) + overdue
    return {"interviews_held": len(held), "submitted": len(lags), "lag_median_days": _med(lags),
            "lag_n": len(lags), "over_target_count": over, "missing_overdue": overdue,
            "missing_not_yet_due": not_due,
            "over_target_or_overdue_share": _r((over + overdue) / denom) if denom else None}


def get_pipeline_analytics(role_id: str, as_of: datetime = AS_OF, conn=None) -> dict:
    with use(conn) as c:
        role = load_role(c, role_id)
        events = rows(c, "SELECT e.* FROM stage_events e JOIN candidates c USING (candidate_id) "
                         "WHERE c.role_id = ? ORDER BY e.event_id", role_id)
        fb_by = {(f["candidate_id"], f["stage"]): f for f in rows(
            c, "SELECT f.* FROM interview_feedback f JOIN candidates c USING (candidate_id) "
               "WHERE c.role_id = ? AND f.stage != 'Resume Review'", role_id)}  # recruiter notes excluded
    stages = []
    for i, stage in enumerate(role["stage_order"]):
        evs, tgt = [e for e in events if e["stage"] == stage], role["stage_targets"][stage]
        # Each block needs its OWN target; a stage may legitimately define only some of them.
        sched = _scheduling(evs, tgt["scheduling_days"], as_of) if tgt["scheduling_days"] is not None else None
        fb = _feedback(evs, fb_by, tgt["feedback_days"], as_of) if tgt["feedback_days"] is not None else None
        dur = _duration(evs, tgt["duration_days"], as_of) if tgt["duration_days"] is not None else None
        reasons = {}
        for e in evs:
            if e["reason_code"]:
                reasons[e["reason_code"]] = reasons.get(e["reason_code"], 0) + 1
        stages.append({
            "stage": stage, "targets": tgt, "funnel": _funnel(evs, i == len(role["stage_order"]) - 1),
            "duration": dur, "scheduling": sched, "feedback": fb, "reasons": reasons,
            "low_sample": {"duration": dur["completed_n"] < LOW_SAMPLE_N if dur else None,
                           "scheduling": sched["scheduled_n"] < LOW_SAMPLE_N if sched else None,
                           "feedback": fb["lag_n"] < LOW_SAMPLE_N if fb else None}})
    return {"role_id": role_id, "role_age_days": (as_of - parse_z(role["opened_at"])).days,
            "as_of": iso_z(as_of), "stages": stages}
