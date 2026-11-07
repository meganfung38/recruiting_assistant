"""Independent plain-Python recomputation of dataset facts, straight from the scenario rows.

Deliberately does NOT use services/ or SQL. Services are later compared against this.
"""
from statistics import median

from data import scenarios as sc
from services.clock import AS_OF, parse_z

DAY = 86400.0


def days(a, b) -> float:
    return (b - a).total_seconds() / DAY


def _role_of(e):
    return sc.role_of(e["candidate_id"])


def events(role, stage):
    return [e for e in sc.STAGE_EVENTS if _role_of(e) == role and e["stage"] == stage]


def targets(role, stage):
    return sc.ROLE_BY_ID[role]["stage_targets"][stage]


def funnel(role, stage):
    evs = events(role, stage)
    out = {k: sum(e["exit_outcome"] == k for e in evs) for k in ("advanced", "rejected", "withdrawn")}
    return {"entered": len({e["candidate_id"] for e in evs}), **out,
            "still_in_stage": sum(e["exited_at"] is None for e in evs)}


def sched_observations(role, stage):
    """[(kind, wait_days, outcome)] kind in scheduled | awaiting_scheduling | exited_unscheduled."""
    obs = []
    for e in events(role, stage):
        ent = parse_z(e["entered_at"])
        if e["scheduled_at"]:
            obs.append(("scheduled", days(ent, parse_z(e["scheduled_at"])), None))
        elif e["exited_at"] is None:
            obs.append(("awaiting_scheduling", days(ent, AS_OF), None))
        else:
            obs.append(("exited_unscheduled", days(ent, parse_z(e["exited_at"])), e["exit_outcome"]))
    return obs


def feedback_for(e):
    return [f for f in sc.FEEDBACK if f["candidate_id"] == e["candidate_id"] and f["stage"] == e["stage"]
            and f["interview_at"] == e["interview_at"]]


def feedback_stats(role, stage):
    tgt = targets(role, stage)["feedback_days"]
    held = [e for e in events(role, stage) if e["interview_at"]]
    lags, missing_overdue, missing_not_due = [], 0, 0
    for e in held:
        fb = feedback_for(e)
        if fb:
            lags.append(days(parse_z(e["interview_at"]), parse_z(fb[0]["submitted_at"])))
        elif days(parse_z(e["interview_at"]), AS_OF) > tgt:
            missing_overdue += 1
        else:
            missing_not_due += 1
    over = sum(lag > tgt for lag in lags)
    return {"interviews_held": len(held), "feedback_submitted": len(lags),
            "feedback_lag_median": median(lags) if lags else None, "feedback_lag_n": len(lags),
            "feedback_over_target_count": over, "feedback_missing_overdue": missing_overdue,
            "feedback_missing_not_yet_due": missing_not_due,
            "share_over_or_overdue": (over + missing_overdue) / (len(lags) + missing_overdue)
            if (lags or missing_overdue) else None}


def scheduling_stats(role, stage):
    tgt = targets(role, stage)["scheduling_days"]
    obs = sched_observations(role, stage)
    sched = [w for k, w, _ in obs if k == "scheduled"]
    unsched = [w for k, w, _ in obs if k == "awaiting_scheduling"]
    by_outcome = {}
    for k, _, o in obs:
        if k == "exited_unscheduled":
            by_outcome[o] = by_outcome.get(o, 0) + 1
    over = sum(w > tgt for _, w, _ in obs)
    return {"scheduled_n": len(sched), "scheduling_lag_median_scheduled": median(sched) if sched else None,
            "awaiting_scheduling": len(unsched),
            "unscheduled_active_wait_median": median(unsched) if unsched else None,
            "unscheduled_active_wait_max": max(unsched) if unsched else None,
            "unscheduled_active_over_target": sum(w > tgt for w in unsched),
            "exited_unscheduled_by_outcome": by_outcome,
            "scheduling_over_target_count": over, "scheduling_observations": len(obs),
            "share_over_target": over / len(obs) if obs else None}


def duration_stats(role, stage):
    tgt = targets(role, stage)["duration_days"]
    done = [days(parse_z(e["entered_at"]), parse_z(e["exited_at"])) for e in events(role, stage) if e["exited_at"]]
    active = [days(parse_z(e["entered_at"]), AS_OF) for e in events(role, stage) if not e["exited_at"]]
    return {"completed_duration_median": median(done) if done else None, "completed_n": len(done),
            "completed_over_duration_target": sum(d > tgt for d in done),
            "active_over_duration_target": sum(d > tgt for d in active)}


def reasons(role, stage):
    out = {}
    for e in events(role, stage):
        if e["reason_code"]:
            out[e["reason_code"]] = out.get(e["reason_code"], 0) + 1
    return out


def waiting_state(cid):
    """(state, wait_days) for an active candidate, else None."""
    cand = next(c for c in sc.CANDIDATES if c["candidate_id"] == cid)
    if cand["application_status"] != "active":
        return None
    e = [x for x in sc.STAGE_EVENTS if x["candidate_id"] == cid][-1]
    if e["stage"] == sc.RR:
        return "in_review", days(parse_z(e["entered_at"]), AS_OF)
    if not e["scheduled_at"]:
        return "awaiting_scheduling", days(parse_z(e["entered_at"]), AS_OF)
    if not e["interview_at"]:
        return "awaiting_interview", days(parse_z(e["scheduled_at"]), AS_OF)
    fb = feedback_for(e)
    if not fb:
        return "awaiting_feedback", days(parse_z(e["interview_at"]), AS_OF)
    return "awaiting_decision", days(parse_z(fb[0]["submitted_at"]), AS_OF)


def overdue_task_ids():
    return sorted(t["task_id"] for t in sc.TASKS if t["status"] == "pending" and parse_z(t["due_at"]) < AS_OF)


def pending_task_ids():
    return sorted(t["task_id"] for t in sc.TASKS if t["status"] == "pending")


def missing_feedback_candidates():
    """Candidates with an interview_at but no feedback record (interview stages only)."""
    out = []
    for e in sc.STAGE_EVENTS:
        if e["interview_at"] and not feedback_for(e):
            out.append(e["candidate_id"])
    return sorted(out)
