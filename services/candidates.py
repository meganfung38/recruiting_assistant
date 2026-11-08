"""Role and candidate queries, plus deterministic triage signals (never a combined score).

Skill matching is a coarse whole-word, case-insensitive keyword match on CV text. It can be gamed
by keyword stuffing and is a prefilter only. Free-text fields are returned raw here; the agent's
projection layer wraps them as untrusted and strips what the model must not see.

A role may be referenced by ID or title everywhere (services.db.resolve_role_id). Status and history come back as short dated
sentences built here, deterministically, so the model restates facts instead of reconstructing them from timestamps.
"""
import json
import re
from datetime import datetime

from services.analytics import days_between, waiting_state
from services.clock import AS_OF, PACIFIC, parse_z
from services.content_flags import flags_for, sources_for
from services.db import NotFound, load_role, resolve_role_id, rows, use
from services.tasks import task_view

CV_MAX_CHARS = 4000
EMAIL_MAX_CHARS = 800
MAX_IDS, MAX_NAMES, MAX_SEARCH, MAX_COMMS = 20, 10, 20, 10
REC_VALUES = ("strong_yes", "yes", "mixed", "no", "strong_no")
STATUSES = ("active", "rejected", "withdrawn")
OUTCOME_WORDS = {"advanced": "advanced", "rejected": "rejected", "withdrawn": "withdrew"}


def list_roles(as_of: datetime = AS_OF, conn=None) -> list[dict]:
    """Every role with what is needed to judge candidates against it (the skills and interview criteria)."""
    out = []
    with use(conn) as c:
        for r in rows(c, "SELECT role_id FROM roles ORDER BY role_id"):
            role = load_role(c, r["role_id"])
            req = role["requirements"]
            out.append({"role_id": role["role_id"], "title": role["title"], "department": role["department"],
                        "opened_at": role["opened_at"], "role_age_days": (as_of - parse_z(role["opened_at"])).days,
                        "required_skills": [s["name"] for s in req["required_skills"]],
                        "preferred_skills": [s["name"] for s in req["preferred_skills"]],
                        "interview_criteria": req["criteria"], "stage_order": role["stage_order"]})
    return out


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


def _days(n: int) -> str:
    return "under a day" if n < 1 else f"{n} day" + ("" if n == 1 else "s")


def stage_status(events: list[dict], feedback: list[dict], status: str, as_of: datetime) -> str:
    """One plain sentence about where the candidate stands in their current (last) stage, with the dates it rests on.
    scheduled_at is when the interview was BOOKED (the planned interview date is not in the data); interview_at exists only once
    it has been held. Booked, held and feedback are kept apart so they are never conflated."""
    e = events[-1]
    if status != "active":
        return f"{status.capitalize()} at {e['stage']}" + (f" on {_day(e['exited_at'])}." if e["exited_at"] else ".")
    got = [f for f in feedback if f["stage"] == e["stage"]]
    head = f"{e['stage']} since {_day(e['entered_at'])} ({_days(round(days_between(parse_z(e['entered_at']), as_of)))})"
    if e["stage"] == "Resume Review":
        return f"{head}: " + ("review note on file." if got else "no review note yet.")
    if e["scheduled_at"] is None:
        return f"{head}: interview not booked yet."
    if e["interview_at"] is None or parse_z(e["interview_at"]) > as_of:
        return f"{head}: interview booked on {_day(e['scheduled_at'])} but not held yet (no interview date is recorded)."
    fb = f"feedback received {_day(max(f['submitted_at'] for f in got))}." if got else "feedback not received yet."
    return f"{head}: interview held {_day(e['interview_at'])}; {fb}"


def event_summary(e: dict, as_of: datetime = AS_OF) -> str:
    """One past or current stage as a sentence, e.g. 'Recruiter Screen, Oct 7 to Oct 17: interview held Oct 14; advanced.'"""
    span = f"{e['stage']}, {_day(e['entered_at'])} to " + (_day(e["exited_at"]) if e["exited_at"] else "now")
    steps = []
    if e["interview_at"] and parse_z(e["interview_at"]) <= as_of:
        steps.append(f"interview held {_day(e['interview_at'])}")
    elif e["scheduled_at"]:
        steps.append(f"interview booked {_day(e['scheduled_at'])}, not held")
    elif e["stage"] != "Resume Review":
        steps.append("interview never booked" if e["exited_at"] else "interview not booked yet")
    if e["exit_outcome"]:
        why = e["reason_code"].replace("_", " ") + (f": {e['reason_detail']}" if e["reason_detail"] else "") if e["reason_code"] else ""
        steps.append(OUTCOME_WORDS.get(e["exit_outcome"], e["exit_outcome"]) + (f" ({why})" if why else ""))
    return span + (": " + "; ".join(steps) if steps else "") + "."


def _skills(cv: str, role: dict) -> dict:
    req, pref = role["requirements"]["required_skills"], role["requirements"]["preferred_skills"]
    return {"required_skills_matched": [s["name"] for s in req if _matches(cv, s)],
            "required_skills_missing": [s["name"] for s in req if not _matches(cv, s)],
            "preferred_skills_matched": [s["name"] for s in pref if _matches(cv, s)]}


def _fit_signals(cv: str, role: dict, feedback: list[dict], last: dict, active: bool, as_of: datetime, events: list[dict] | None = None) -> dict:
    skills = _skills(cv, role)
    counts = {k: sum(f["recommendation"] == k for f in feedback) for k in REC_VALUES}
    latest = max(feedback, key=lambda f: f["interview_at"]) if feedback else None
    scores = [v for f in feedback for v in f["criteria"].values()]
    state, wait = waiting_state(last, [f for f in feedback if f["stage"] == last["stage"]], as_of) if active else (None, None)
    age = days_between(parse_z(last["entered_at"]), as_of) if active else None
    target = role["stage_targets"][last["stage"]]["duration_days"]
    return {
        "required_skills_matched": skills["required_skills_matched"], "required_skills_missing": skills["required_skills_missing"],
        "preferred_skills_matched_count": len(skills["preferred_skills_matched"]),
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


def _name_matches(name: str, query: str) -> bool:
    """Every word of the query appears in the name, ignoring case ('hannelore', 'Voss', 'hannelore voss')."""
    return all(w in name.lower() for w in query.lower().split())


def search_candidates(role: str | None = None, status: str | None = None, stage: str | None = None, limit: int = MAX_SEARCH,
                      name: str | None = None, as_of: datetime = AS_OF, conn=None) -> dict:
    """Compact rows. `role` is optional (all roles when omitted). `status` defaults to active, except that a name search
    looks at every status so a person is never reported missing just because they were rejected."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if not 1 <= limit <= MAX_SEARCH:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH}")
    statuses = [status] if status else (list(STATUSES) if name else ["active"])
    with use(conn) as c:
        role_ids = [resolve_role_id(c, role)] if role else [r["role_id"] for r in rows(c, "SELECT role_id FROM roles ORDER BY role_id")]
        roles = {rid: load_role(c, rid) for rid in role_ids}
        if stage is not None:
            valid = list(dict.fromkeys(s for r in roles.values() for s in r["stage_order"]))
            match = next((s for s in valid if s.lower() == stage.strip().lower()), None)
            if match is None:
                raise ValueError(f"stage must be one of {valid}")
            stage = match
        cands = [x for rid in role_ids for st in statuses
                 for x in rows(c, "SELECT * FROM candidates WHERE role_id = ? AND application_status = ? ORDER BY candidate_id", rid, st)]
        cands.sort(key=lambda x: x["candidate_id"])
        cands = [x for x in cands if (stage is None or x["current_stage"] == stage) and (not name or _name_matches(x["name"], name))]
        shown = cands[:limit]
        fbs = _feedback_by_candidate(c, [x["candidate_id"] for x in shown])
        evs = _events_by_candidate(c)
    out = []
    for x in shown:
        cid, r = x["candidate_id"], roles[x["role_id"]]
        active = x["application_status"] == "active"
        sig = _fit_signals(x["cv_text"], r, fbs[cid], evs[cid][-1], active, as_of, evs[cid])
        out.append({"candidate_id": cid, "role_id": x["role_id"], "role_title": r["title"], "name": x["name"], "current_stage": x["current_stage"],
                    "status": x["application_status"], "stage_status": stage_status(evs[cid], fbs[cid], x["application_status"], as_of),
                    "waiting_state": sig["waiting_state"],
                    "waiting_days": sig["waiting_days"], "days_in_current_stage": sig["days_in_current_stage"],
                    "fit_signals": sig,
                    "content_flags": flags_for(cid, scanned := {
                        "cv_text": x["cv_text"],
                        **{f"feedback_summary:{f['feedback_id']}": f["summary_text"] for f in fbs[cid]}}),
                    # raw excerpts for the trusted boundary only: the projection allowlist drops this before the model sees a row
                    "flag_sources": sources_for(cid, scanned)})
    return {"role_id": role_ids[0] if role else None, "candidates": out, "total_matching": len(cands), "returned": len(out)}


def get_candidates(candidate_ids: list[str] | None = None, role: str | None = None, names: list[str] | None = None,
                   as_of: datetime = AS_OF, conn=None) -> list[dict]:
    """Evidence for candidates picked by ID, by name, or as every ACTIVE candidate of one role (shortlists in one call). Exactly one of
    `candidate_ids` (1 to MAX_IDS), `names` (1 to MAX_NAMES; each matches any status, every word of it in the name) or `role`.
    Unknown IDs come back as {candidate_id, not_found: True}, unmatched names as {name, not_found: True}.
    By role, each record is the comparison view: without stage_history and pending_tasks (the status sentence already says where
    the person is now), so a whole role fits in one result. By ID or name, the record is complete."""
    if sum(x is not None for x in (candidate_ids, role, names)) != 1:
        raise ValueError("provide exactly one of candidate_ids, names or role")
    out, unmatched = [], []
    with use(conn) as c:
        if role is not None:
            ids = [r["candidate_id"] for r in rows(c, "SELECT candidate_id FROM candidates WHERE role_id = ? AND application_status = 'active' "
                                                      "ORDER BY candidate_id", resolve_role_id(c, role))]
        elif names is not None:
            if not 1 <= len(names) <= MAX_NAMES:
                raise ValueError(f"provide between 1 and {MAX_NAMES} names")
            everyone = rows(c, "SELECT candidate_id, name FROM candidates ORDER BY candidate_id")
            ids = []
            for n in names:
                hits = [x["candidate_id"] for x in everyone if n.strip() and _name_matches(x["name"], n)]
                ids += hits
                unmatched += [] if hits else [{"name": n, "not_found": True}]
            ids = list(dict.fromkeys(ids))[:MAX_IDS]
        else:
            ids = list(dict.fromkeys(candidate_ids))
            if not 1 <= len(ids) <= MAX_IDS:
                raise ValueError(f"provide between 1 and {MAX_IDS} candidate IDs")
        roles: dict = {}
        for cid in ids:
            r = rows(c, "SELECT * FROM candidates WHERE candidate_id = ?", cid)
            if not r:
                out.append({"candidate_id": cid, "not_found": True})
                continue
            x = r[0]
            role_rec = roles.setdefault(x["role_id"], load_role(c, x["role_id"]))
            events = rows(c, "SELECT * FROM stage_events WHERE candidate_id = ? ORDER BY entered_at, event_id", cid)
            fb = _feedback_by_candidate(c, [cid])[cid]
            tasks = rows(c, "SELECT * FROM tasks WHERE candidate_id = ? AND status = 'pending' ORDER BY due_at", cid)
            active = x["application_status"] == "active"
            state, wait = (waiting_state(events[-1], [f for f in fb if f["stage"] == events[-1]["stage"]], as_of)
                           if active else (None, None))
            rec = {
                "candidate_id": cid, "role_id": x["role_id"], "role_title": role_rec["title"], "name": x["name"],
                "current_stage": x["current_stage"], "application_status": x["application_status"],
                "stage_status": stage_status(events, fb, x["application_status"], as_of), "applied_at": x["applied_at"],
                **_skills(x["cv_text"], role_rec),
                "cv_text": x["cv_text"][:CV_MAX_CHARS], "cv_truncated": len(x["cv_text"]) > CV_MAX_CHARS,
                "stage_history": [{**e, "summary": event_summary(e, as_of)} for e in events],
                "feedback": [{k: v for k, v in f.items() if k != "criteria_json"} for f in fb],
                "feedback_count": len(fb), **interview_evidence(events, fb, as_of), "waiting_state": state,
                "days_in_current_stage": round(days_between(parse_z(events[-1]["entered_at"]), as_of), 4) if active else None,
                "waiting_days": round(wait, 4) if wait is not None else None,
                "pending_tasks": [task_view(t, as_of, {cid: x["name"]}, {x["role_id"]: role_rec["title"]}) for t in tasks]}
            if role is not None:
                del rec["stage_history"], rec["pending_tasks"]
            out.append(rec)
    return out + unmatched


def get_candidate_communications(candidate_id: str, limit: int = MAX_COMMS, conn=None) -> dict:
    if not 1 <= limit <= MAX_COMMS:
        raise ValueError(f"limit must be between 1 and {MAX_COMMS}")
    with use(conn) as c:
        cand = rows(c, "SELECT name FROM candidates WHERE candidate_id = ?", candidate_id)
        if not cand:
            raise NotFound(f"candidate {candidate_id}")
        msgs = rows(c, "SELECT * FROM email_messages WHERE candidate_id = ? ORDER BY sent_at DESC, email_id DESC",
                    candidate_id)
    return {"candidate_id": candidate_id, "name": cand[0]["name"], "total_messages": len(msgs), "returned": min(len(msgs), limit),
            "messages": [{"email_id": m["email_id"], "sent_at": m["sent_at"], "direction": m["direction"],
                          "subject": m["subject"], "body": m["body"][:EMAIL_MAX_CHARS],
                          "body_truncated": len(m["body"]) > EMAIL_MAX_CHARS} for m in msgs[:limit]]}
