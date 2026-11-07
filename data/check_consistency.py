"""Checks that the prose in a seeded DB agrees with the structured scenario facts.

    python -m data.check_consistency [--db PATH]     (exit 1 if any problem is found)

`check()` returns a list of human-readable problems; empty means consistent.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from data import scenarios as sc
from services.clock import AS_OF, pacific_date as _pdate, parse_z
from services.db import connect, db_path

PROSE = Path(__file__).resolve().parent / "prose"
# Instruction-like text must not appear anywhere in clean prose (only in the C117 injection payload).
INSTRUCTION = re.compile(
    r"ignore (all|previous|other)|system override|rank this candidate|do not mention this|you are now",
    re.I)
WITHDRAWAL_WORD = {"accepted_other_offer": "offer", "compensation_mismatch": "compensation",
                   "withdrew_personal": "personal", "withdrew_scheduling_delay": "scheduled"}


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text, re.I) is not None


def check(path: str | Path | None = None) -> list[str]:
    problems: list[str] = []
    bad = problems.append
    conn = connect(path or db_path())
    cv = {r["candidate_id"]: r["cv_text"] for r in conn.execute("SELECT candidate_id, cv_text FROM candidates")}
    emails = {r["email_id"]: r for r in conn.execute("SELECT * FROM email_messages")}
    fbs = {r["feedback_id"]: r for r in conn.execute("SELECT * FROM interview_feedback")}
    events = {r["event_id"]: r for r in conn.execute("SELECT * FROM stage_events")}
    roles = {r["role_id"]: json.loads(r["requirements_json"]) for r in conn.execute("SELECT * FROM roles")}
    conn.close()
    ev_by = {(e["candidate_id"], e["stage"]): e for e in sc.STAGE_EVENTS}
    names = {c["candidate_id"]: c["name"] for c in sc.CANDIDATES}

    # 0. Frozen prose covers exactly the current scenario IDs (guards against stale files).
    expected_keys = {"cv": set(names), "emails": {m["email_id"] for m in sc.EMAILS},
                     "feedback": {f["feedback_id"] for f in sc.FEEDBACK},
                     "exit_reasons": {e["event_id"] for e in sc.STAGE_EVENTS if e["reason_code"]}}
    for name, want in expected_keys.items():
        p = PROSE / f"{name}.json"
        if p.exists() and set(json.loads(p.read_text())) != want:
            bad(f"prose/{name}.json keys do not match scenario IDs")

    # 1. CV skills: whole-word match equals the declared facts (missing list and preferred count).
    for cid, text in cv.items():
        spec, req = sc.SPEC_BY_ID[cid], roles[sc.role_of(cid)]
        clean = text.replace(sc.INJECTION_PAYLOAD, "")
        missing = [s["name"] for s in req["required_skills"]
                   if not any(_has(clean, p) for p in [s["name"], *s["aliases"]])]
        if missing != list(spec.lack):
            bad(f"{cid}: CV missing required skills {missing}, scenario says {list(spec.lack)}")
        pref = sum(any(_has(clean, p) for p in [s["name"], *s["aliases"]]) for s in req["preferred_skills"])
        if pref != spec.pref:
            bad(f"{cid}: CV mentions {pref} preferred skills, scenario says {spec.pref}")

    # 2. No instruction-like text outside the declared payload; no cross-candidate name leaks.
    texts = [(cid, "cv", t.replace(sc.INJECTION_PAYLOAD, "")) for cid, t in cv.items()]
    texts += [(r["candidate_id"], r["email_id"], r["subject"] + "\n" + r["body"]) for r in emails.values()]
    texts += [(r["candidate_id"], r["feedback_id"], r["summary_text"]) for r in fbs.values()]
    texts += [(e["candidate_id"], e["event_id"], e["exit_reason"] or "") for e in events.values()]
    for cid, where, t in texts:
        if INSTRUCTION.search(t):
            bad(f"{cid} {where}: instruction-like text in clean prose")
        for other, nm in names.items():
            if other != cid and nm in t:
                bad(f"{cid} {where}: mentions another candidate ({other})")

    # 3. Emails vs the structured facts.
    for m in sc.EMAILS:
        row = emails.get(m["email_id"])
        if row is None:
            bad(f"{m['email_id']}: missing from DB")
            continue
        cid, kind, stage = m["candidate_id"], m["kind"], m["stage"]
        ev, text = ev_by[(cid, stage)], row["subject"] + "\n" + row["body"]
        if len(row["body"]) > 800:
            bad(f"{m['email_id']}: body longer than 800 chars")
        if kind == "reschedule_request":
            if not (parse_z(ev["scheduled_at"]) <= parse_z(row["sent_at"]) <= parse_z(ev["interview_at"])):
                bad(f"{m['email_id']}: reschedule email outside booking-to-interview window")
            if stage not in text:
                bad(f"{m['email_id']}: reschedule email does not name {stage}")
        elif kind in ("wait_complaint", "status_inquiry"):
            end = parse_z(ev["exited_at"]) if ev["exited_at"] else AS_OF
            if not (parse_z(ev["entered_at"]) <= parse_z(row["sent_at"]) <= end):
                bad(f"{m['email_id']}: {kind} outside the {stage} window")
            if not (_has(row["body"], "waiting") and _has(row["body"], "scheduled")):
                bad(f"{m['email_id']}: {kind} should mention waiting to be scheduled")
        elif kind == "schedule_invite" and ev["interview_at"]:
            if _pdate(ev["interview_at"]) not in row["body"]:
                bad(f"{m['email_id']}: invite date does not match interview_at")
        elif kind == "rejection" and ev["reason_code"] == "missing_required_skill":
            if ev["reason_detail"] not in row["body"]:
                bad(f"{m['email_id']}: rejection should name {ev['reason_detail']}")
        elif kind == "withdrawal":
            word = WITHDRAWAL_WORD.get(ev["reason_code"])
            if word and word not in row["body"].lower():
                bad(f"{m['email_id']}: withdrawal email should mention '{word}'")
    for e in sc.STAGE_EVENTS:  # every scheduling-delay withdrawal has a matching thread
        if e["reason_code"] == "withdrew_scheduling_delay":
            thread = [m for m in sc.EMAILS if m["candidate_id"] == e["candidate_id"]
                      and m["kind"] == "wait_complaint" and m["stage"] == e["stage"]]
            if not thread:
                bad(f"{e['candidate_id']}: scheduling-delay withdrawal has no backing email")

    # 4. Feedback text vs facts: stage, date and recommendation.
    for f in sc.FEEDBACK:
        row = fbs.get(f["feedback_id"])
        if row is None:
            bad(f"{f['feedback_id']}: missing from DB")
            continue
        text = row["summary_text"]
        if _pdate(row["interview_at"]) not in text:
            bad(f"{f['feedback_id']}: summary date does not match interview_at")
        if f"Recommendation: {row['recommendation']}." not in text:
            bad(f"{f['feedback_id']}: summary recommendation does not match the record")
        if row["stage"] != sc.RR and row["stage"].lower() not in text.lower() and row["stage"] != sc.RS:
            bad(f"{f['feedback_id']}: summary does not name the stage")

    # 5. Exit reasons agree with reason codes.
    for e in events.values():
        text = (e["exit_reason"] or "").lower()
        code = e["reason_code"]
        if bool(code) != bool(text):
            bad(f"{e['event_id']}: exit_reason presence does not match reason_code")
        word = {"missing_required_skill": (e["reason_detail"] or "").lower(), "accepted_other_offer": "offer",
                "compensation_mismatch": "compensation", "withdrew_scheduling_delay": "waiting",
                "withdrew_personal": "personal"}.get(code)
        if word and word not in text:
            bad(f"{e['event_id']}: exit_reason should mention '{word}'")
    return problems


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    found = check(ap.parse_args().db)
    print("\n".join(found) if found else "Consistent: prose agrees with structured facts.")
    sys.exit(1 if found else 0)
