"""Deterministic placeholder prose, used only when data/prose/*.json is absent (or frozen=False).

It must satisfy data/check_consistency.py on its own, so it is reason-aware and dated.
Stub CVs list skills explicitly and avoid every skill keyword elsewhere, so whole-word
skill matching is exact.
"""
from data.scenarios import RR, ROLE_BY_ID, SPEC_BY_ID, STAGE_EVENTS, role_of
from services.clock import pacific_date

_TITLE = {"ROLE-BE": "Backend developer", "ROLE-MSE": "Controls and systems engineer",
          "ROLE-PO": "People operations generalist"}


def cv_text(cid: str) -> str:
    spec = SPEC_BY_ID[cid]
    role = ROLE_BY_ID[role_of(cid)]
    req = [s["name"] for s in role["requirements"]["required_skills"] if s["name"] not in spec.lack]
    pref = [s["name"] for s in role["requirements"]["preferred_skills"]][: spec.pref]
    depth = {5: "Led several projects end to end.", 4: "Owned significant parts of delivery.",
             3: "Contributed steadily on a small team.", 2: "Early-career, mostly supporting tasks.",
             1: "Limited relevant history."}[spec.q]
    return (f"{spec.name}\n{_TITLE[role['role_id']]}, {spec.yrs} years of experience at fictional employers.\n"
            f"{depth}\nSkills: {', '.join(req + pref)}.")


def _event(cid: str, stage: str) -> dict:
    return next(e for e in STAGE_EVENTS if e["candidate_id"] == cid and e["stage"] == stage)


_WITHDRAWAL = {  # reason_code -> sentence (each carries the keyword the checker looks for)
    "accepted_other_offer": "I have accepted an offer elsewhere.",
    "compensation_mismatch": "The compensation range does not match my expectations.",
    "withdrew_personal": "I am withdrawing for personal reasons.",
    "withdrew_scheduling_delay": "My interview was never scheduled, so I am moving on.",
    "withdrew_no_reason": "Thank you for your consideration.",
}
_SIMPLE = {
    "application_ack": ("Thanks for applying", "Hello {n}, we received your application for {t}. We will review it soon."),
    "applicant_reply": ("Re: Thanks for applying", "Thank you. Happy to provide anything else you need."),
    "reschedule_request": ("Request to reschedule {s}", "Hello, I need to move my {s}. Could we find another time?"),
    "wait_complaint": ("Still waiting to be scheduled",
                       "Hello, I am still waiting to be scheduled for the {s}. I have not heard back."),
    "status_inquiry": ("Checking in on scheduling",
                       "Hello, I am still waiting to be scheduled for the {s}. Could you share an update?"),
}


def email(m: dict, name: str, cid: str) -> tuple[str, str]:
    kind, ev = m["kind"], _event(m["candidate_id"], m["stage"])
    fill = {"n": name, "t": ROLE_BY_ID[role_of(cid)]["title"], "s": m["stage"]}
    if kind in _SIMPLE:
        subj, body = _SIMPLE[kind]
    elif kind == "schedule_invite":
        subj = "Interview scheduled: {s}"
        body = ("Hello {n}, your {s} is booked for " + pacific_date(ev["interview_at"]) + "."
                if ev["interview_at"] else "Hello {n}, your {s} is being booked; details to follow.")
    elif kind == "rejection":
        subj = "Update on your application"
        body = ("Hello {n}, we will not be moving forward after the {s}."
                + (f" The role requires {ev['reason_detail']} experience." if ev["reason_detail"] else ""))
    elif kind == "withdrawal":
        subj = "Withdrawing my application"
        body = "Hello, I would like to withdraw my application for {t}. " + _WITHDRAWAL[ev["reason_code"]]
    else:
        raise ValueError(kind)
    return subj.format(**fill), body.format(**fill)


def feedback_summary(f: dict) -> str:
    when = pacific_date(f["interview_at"])
    head = "Resume review note" if f["stage"] == RR else f"{f['stage']} feedback"
    return f"{head} dated {when} ({f['stage']}). Recommendation: {f['recommendation']}."


_REASON = {
    "missing_required_skill": "Screened out at {s}: no {d} experience shown, a required skill.",
    "lower_interview_signal": "Not advanced after {s}: interview signal below the bar.",
    "compensation_mismatch": "Withdrew during {s} after compensation expectations did not align.",
    "accepted_other_offer": "Withdrew during {s}: accepted another offer.",
    "withdrew_scheduling_delay": "Withdrew while waiting to be scheduled for the {s}.",
    "withdrew_personal": "Withdrew during {s} for personal reasons.",
    "withdrew_no_reason": "Withdrew during {s}; no reason given.",
    "no_response": "No response from candidate during {s}.",
}


def exit_reason(e: dict) -> str | None:
    code = e["reason_code"]
    return _REASON[code].format(s=e["stage"], d=e["reason_detail"]) if code else None
