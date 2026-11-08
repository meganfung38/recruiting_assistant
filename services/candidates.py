"""Role and candidate queries, plus deterministic triage signals (never a combined score).

Skill matching is a coarse whole-word, case-insensitive keyword match on CV text. It can be gamed
by keyword stuffing and is a prefilter only. Free-text fields are returned raw here; the agent's
projection layer wraps them as untrusted and strips what the model must not see.
"""
import json
import re
from datetime import datetime

from services.analytics import days_between, waiting_state
from services.clock import AS_OF, PACIFIC, parse_z
from services.content_flags import flags_for, sources_for
from services.db import NotFound, load_role, rows, use
from services.tasks import task_view

CV_MAX_CHARS = 4000
EMAIL_MAX_CHARS = 800
MAX_IDS, MAX_SEARCH, MAX_COMMS = 6, 20, 10
REC_VALUES = ("strong_yes", "yes", "mixed", "no", "strong_no")
STATUSES = ("active", "rejected", "withdrawn")


def list_roles(as_of: datetime = AS_OF, conn=None) -> list[dict]:
    with use(conn) as c:
        return [{"role_id": r["role_id"], "title": r["title"], "opened_at": r["opened_at"],
                 "role_age_days": (as_of - parse_z(r["opened_at"])).days}
                for r in rows(c, "SELECT * FROM roles ORDER BY role_id")]


def get_role(role_id: str, as_of: datetime = AS_OF, conn=None) -> dict:
    with use(conn) as c:
        r = load_role(c, role_id)
    return {**r, "role_age_days": (as_of - parse_z(r["opened_at"])).days}


def _matches(text: str, skill: dict) -> bool:
    return any(re.search(rf"\b{re.escape(p)}\b", text, re.I) for p in [skill["name"], *skill["aliases"]])


def interview_evidence(events: list[dict], feedback: list[dict], as_of: datetime) -> dict:
    """How much interview evidence exists, kept apart on purpose: more interviews is not a stronger candidate.
    completed = interviews that have taken place by as_of; received = feedback entries on file; pending = completed interviews whose
    stage has no feedback yet; stages_with_feedback = distinct stages that have any."""
    done = [e["stage"] for e in events if e["interview_at"] and parse_z(e["interview_at"]) <= as_of]
    fed = {f["stage"] for f in feedback}
    return {"interviews_completed": len(done), "feedback_received": len(feedback),
            "feedback_pending": sum(st not in fed for st in done), "stages_with_feedback": len(fed)}


def _day(iso: str) -> str:
    d = parse_z(iso).astimezone(PACIFIC)
    return f"{d:%b} {d.day}"


def stage_status(events: list[dict], feedback: list[dict], status: str, as_of: datetime) -> str:
    """One plain sentence about where the candidate stands in their current (last) stage. Booked, held and feedback are kept apart:
    scheduled_at is when the interview was BOOKED, interview_at exists only once it has been held."""
    e = events[-1]
    if status != "active":
        return f"{status.capitalize()} at {e['stage']}" + (f" on {_day(e['exited_at'])}." if e["exited_at"] else ".")
    got = [f for f in feedback if f["stage"] == e["stage"]]
    note = (f"feedback received {_day(max(f['submitted_at'] for f in got))}" if got else "no feedback yet")
    if e["stage"] == "Resume Review":
        return f"Resume Review: in review since {_day(e['entered_at'])}, " + ("review note on file." if got else "no review note yet.")
    if e["scheduled_at"] is None:
        step = "interview not booked yet"
    elif e["interview_at"] is None or parse_z(e["interview_at"]) > as_of:
        step = f"interview booked {_day(e['scheduled_at'])}, not held yet"
    else:
        step = f"interview held {_day(e['interview_at'])}"
    return f"{e['stage']}: {step}. {note[0].upper() + note[1:]}."


def _fit_signals(cv: str, role: dict, feedback: list[dict], last: dict, active: bool, as_of: datetime, events: list[dict] | None = None) -> dict:
    req, pref = role["requirements"]["required_skills"], role["requirements"]["preferred_skills"]
    counts = {k: sum(f["recommendation"] == k for f in feedback) for k in REC_VALUES}
    latest = max(feedback, key=lambda f: f["interview_at"]) if feedback else None
    scores = [v for f in feedback for v in f["criteria"].values()]
    state, wait = waiting_state(last, [f for f in feedback if f["stage"] == last["stage"]], as_of) if active else (None, None)
    age = days_between(parse_z(last["entered_at"]), as_of) if active else None
    target = role["stage_targets"][last["stage"]]["duration_days"]
    return {
        "required_skills_matched": [s["name"] for s in req if _matches(cv, s)],
        "required_skills_missing": [s["name"] for s in req if not _matches(cv, s)],
        "preferred_skills_matched_count": sum(_matches(cv, s) for s in pref),
        "feedback_count": len(feedback), **interview_evidence(events if events is not None else [last], feedback, as_of),
        "recommendation_counts": counts,
        "latest_recommendation": latest["recommendation"] if latest else None,
        "avg_criteria_score": round(sum(scores) / len(scores), 2) if scores else None,
        "days_in_current_stage": round(age, 4) if active else None,
        "stage_duration_target_days": target,
        "over_duration_target": age > target if active and target is not None else None,
        "waiting_state": state, "waiting_days": round(wait, 4) if wait is not None else None}


def _feedback_by_candidate(c, cand_ids: list[str]) -> dict:
    out: dict = {cid: [] for cid in cand_ids}
    for f in rows(c, "SELECT * FROM interview_feedback ORDER BY interview_at, feedback_id"):
        if f["candidate_id"] in out:
            out[f["candidate_id"]].append({**f, "criteria": json.loads(f["criteria_json"])})
    return out


def _events_by_candidate(c) -> dict:
    out: dict = {}
    for e in rows(c, "SELECT * FROM stage_events ORDER BY candidate_id, entered_at, event_id"):
        out.setdefault(e["candidate_id"], []).append(e)
    return out


def search_candidates(role_id: str, status: str = "active", stage: str | None = None, limit: int = MAX_SEARCH,
                      as_of: datetime = AS_OF, conn=None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if not 1 <= limit <= MAX_SEARCH:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH}")
    with use(conn) as c:
        role = load_role(c, role_id)
        if stage is not None and stage not in role["stage_order"]:
            raise ValueError(f"stage must be one of {role['stage_order']}")
        cands = rows(c, "SELECT * FROM candidates WHERE role_id = ? AND application_status = ? ORDER BY candidate_id",
                     role_id, status)
        if stage is not None:
            cands = [x for x in cands if x["current_stage"] == stage]
        shown = cands[:limit]
        fbs = _feedback_by_candidate(c, [x["candidate_id"] for x in shown])
        evs = _events_by_candidate(c)
    out = []
    for x in shown:
        sig = _fit_signals(x["cv_text"], role, fbs[x["candidate_id"]], evs[x["candidate_id"]][-1], status == "active", as_of,
                           evs[x["candidate_id"]])
        out.append({"candidate_id": x["candidate_id"], "role_id": role_id, "role_title": role["title"], "name": x["name"], "current_stage": x["current_stage"],
                    "status": x["application_status"], "stage_status": stage_status(evs[x["candidate_id"]], fbs[x["candidate_id"]], x["application_status"], as_of),
                    "waiting_state": sig["waiting_state"],
                    "waiting_days": sig["waiting_days"], "days_in_current_stage": sig["days_in_current_stage"],
                    "fit_signals": sig,
                    "content_flags": flags_for(x["candidate_id"], scanned := {
                        "cv_text": x["cv_text"],
                        **{f"feedback_summary:{f['feedback_id']}": f["summary_text"] for f in fbs[x["candidate_id"]]}}),
                    # raw excerpts for the trusted boundary only: the projection allowlist drops this before the model sees a row
                    "flag_sources": sources_for(x["candidate_id"], scanned)})
    return {"role_id": role_id, "candidates": out, "total_matching": len(cands), "returned": len(out)}


def get_candidates(candidate_ids: list[str], as_of: datetime = AS_OF, conn=None) -> list[dict]:
    ids = list(dict.fromkeys(candidate_ids))
    if not 1 <= len(ids) <= MAX_IDS:
        raise ValueError(f"provide between 1 and {MAX_IDS} candidate IDs")
    out = []
    with use(conn) as c:
        for cid in ids:
            r = rows(c, "SELECT * FROM candidates WHERE candidate_id = ?", cid)
            if not r:
                out.append({"candidate_id": cid, "not_found": True})
                continue
            x = r[0]
            events = rows(c, "SELECT * FROM stage_events WHERE candidate_id = ? ORDER BY entered_at, event_id", cid)
            fb = _feedback_by_candidate(c, [cid])[cid]
            tasks = rows(c, "SELECT * FROM tasks WHERE candidate_id = ? AND status = 'pending' ORDER BY due_at", cid)
            active = x["application_status"] == "active"
            state, wait = (waiting_state(events[-1], [f for f in fb if f["stage"] == events[-1]["stage"]], as_of)
                           if active else (None, None))
            out.append({
                "candidate_id": cid, "role_id": x["role_id"], "name": x["name"], "current_stage": x["current_stage"],
                "application_status": x["application_status"],
                "stage_status": stage_status(events, fb, x["application_status"], as_of), "applied_at": x["applied_at"],
                "cv_text": x["cv_text"][:CV_MAX_CHARS], "cv_truncated": len(x["cv_text"]) > CV_MAX_CHARS,
                "stage_history": events,
                "feedback": [{k: v for k, v in f.items() if k != "criteria_json"} for f in fb],
                "feedback_count": len(fb), **interview_evidence(events, fb, as_of), "waiting_state": state,
                "days_in_current_stage": round(days_between(parse_z(events[-1]["entered_at"]), as_of), 4) if active else None,
                "waiting_days": round(wait, 4) if wait is not None else None,
                "pending_tasks": [task_view(t, as_of, {cid: x["name"]}, {x["role_id"]: load_role(c, x["role_id"])["title"]}) for t in tasks]})
    return out


def get_candidate_communications(candidate_id: str, limit: int = MAX_COMMS, conn=None) -> dict:
    if not 1 <= limit <= MAX_COMMS:
        raise ValueError(f"limit must be between 1 and {MAX_COMMS}")
    with use(conn) as c:
        if not rows(c, "SELECT 1 FROM candidates WHERE candidate_id = ?", candidate_id):
            raise NotFound(f"candidate {candidate_id}")
        msgs = rows(c, "SELECT * FROM email_messages WHERE candidate_id = ? ORDER BY sent_at DESC, email_id DESC",
                    candidate_id)
    return {"candidate_id": candidate_id, "total_messages": len(msgs), "returned": min(len(msgs), limit),
            "messages": [{"email_id": m["email_id"], "sent_at": m["sent_at"], "direction": m["direction"],
                          "subject": m["subject"], "body": m["body"][:EMAIL_MAX_CHARS],
                          "body_truncated": len(m["body"]) > EMAIL_MAX_CHARS} for m in msgs[:limit]]}
