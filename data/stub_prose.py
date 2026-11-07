"""Deterministic placeholder prose, used until LLM-generated text is frozen into data/prose/*.json.

Stub CVs list skills explicitly and avoid every skill keyword and alias elsewhere, so
whole-word skill matching on them is exact (tests rely on this).
"""
from data.scenarios import RR, ROLE_BY_ID, SPEC_BY_ID, role_of

_TITLE = {"ROLE-BE": "Backend developer", "ROLE-MSE": "Controls and systems engineer",
          "ROLE-PO": "People operations generalist"}
_REASON_TEXT = {
    "missing_required_skill": "Screened out: CV does not show a required skill.",
    "lower_interview_signal": "Not moving forward: interview signal below the bar.",
    "compensation_mismatch": "Compensation expectations did not align.",
    "accepted_other_offer": "Accepted another offer.",
    "withdrew_scheduling_delay": "Withdrew after waiting to be scheduled.",
    "withdrew_personal": "Withdrew for personal reasons.",
    "withdrew_no_reason": "Withdrew without giving a reason.",
    "no_response": "No response from candidate.",
}


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


_EMAIL = {
    "application_ack": ("Thanks for applying", "Hello {n}, we received your application for {t}. We will review it soon."),
    "applicant_reply": ("Re: Thanks for applying", "Thank you. Happy to provide anything else you need."),
    "schedule_invite": ("Interview scheduled: {s}", "Hello {n}, your {s} is booked. Details to follow."),
    "reschedule_request": ("Request to reschedule {s}", "Hello, I need to move my {s}. Could we find another time?"),
    "rejection": ("Update on your application", "Hello {n}, we will not be moving forward after the {s}."),
    "withdrawal": ("Withdrawing my application", "Hello, I would like to withdraw my application for {t}."),
    "wait_complaint": ("Still waiting to be scheduled",
                       "Hello, I am still waiting to be scheduled for the {s}. I have not heard back."),
    "status_inquiry": ("Checking in on scheduling", "Hello, could you share an update on scheduling the {s}?"),
}


def email(m: dict, name: str, cid: str) -> tuple[str, str]:
    subj, body = _EMAIL[m["kind"]]
    fill = {"n": name, "t": ROLE_BY_ID[role_of(cid)]["title"], "s": m["stage"]}
    return subj.format(**fill), body.format(**fill)


def feedback_summary(f: dict) -> str:
    if f["stage"] == RR:
        return f"Resume review note: recommendation {f['recommendation']}."
    return f"{f['stage']} feedback: recommendation {f['recommendation']}."


def exit_reason(code: str | None) -> str | None:
    return _REASON_TEXT.get(code) if code else None
