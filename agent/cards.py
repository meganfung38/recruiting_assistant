"""Evidence cards: plain-language descriptions of the records the model was shown, for the "See evidence" panels.

Built deterministically from the PROJECTED (already allowlisted) tool results, so a card can never show more than the model saw.
Candidate-supplied text appears only inside `quote`, labelled, to be rendered as inert text. Memory only: cards are never put in the
trace or the model's context.
"""
import re

REC_WORDS = {"strong_yes": "Strongly recommends advancing", "yes": "Recommends advancing", "mixed": "Mixed view",
             "no": "Does not recommend advancing", "strong_no": "Strongly does not recommend advancing"}
STATE_WORDS = {"awaiting_scheduling": "waiting to be scheduled", "awaiting_interview": "interview booked, not yet held",
               "awaiting_feedback": "interview held, feedback not yet in", "awaiting_decision": "feedback in, awaiting a decision",
               "in_review": "resume under review"}
QUOTE_CHARS = 500
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def when(display: str | None) -> str:
    """'2026-10-28 13:00 PDT' -> 'Oct 28'."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", display or "")
    return f"{_MONTHS[int(m.group(2)) - 1]} {int(m.group(3))}" if m else ""


def text_of(v) -> str | None:
    """A field that may be wrapped as untrusted: {'untrusted_text': ...} -> the text."""
    return v if isinstance(v, str) else v.get("untrusted_text") if isinstance(v, dict) else None


def words(snake: str) -> str:
    return snake.replace("_", " ")


def _quote(label: str, text: str | None) -> dict | None:
    t = (text or "").strip()
    return {"label": label, "text": t[:QUOTE_CHARS] + ("…" if len(t) > QUOTE_CHARS else "")} if t else None


def _card(ref, kind, title, lines, quote=None, full_record=False, who_id=None) -> dict:
    """`title` may contain {who}: resolved when the card is shown (the candidate's name may only be learned by a later lookup)."""
    return {"ref": ref, "kind": kind, "title": title, "lines": [l for l in lines if l], "quote": quote, "full_record": full_record, "who_id": who_id}


def resolve_title(card: dict, name: str | None, parenthesised: bool) -> dict:
    who = (f" ({name})" if parenthesised else name) if name else ("" if parenthesised else "the candidate")
    return {**card, "title": card["title"].replace("{who}", who)}


def _candidate(c: dict, out: dict) -> None:
    cid, name = c["candidate_id"], text_of(c.get("name")) or "This candidate"
    out[f"cv:{cid}"] = _card(f"cv:{cid}", "cv", f"{name}: full record opened", [
        f"Currently in {c.get('current_stage')}" if c.get("current_stage") else "", f"Status: {c.get('application_status')}" if c.get("application_status") else "",
        f"Applied {when(c.get('applied_at'))}" if c.get("applied_at") else "",
        f"Right now: {STATE_WORDS[c['waiting_state']]}" if c.get("waiting_state") in STATE_WORDS else ""],
        _quote("CV excerpt (candidate-supplied text)", text_of(c.get("cv_text"))), full_record=True)
    for f in c.get("feedback", []):
        if not f.get("feedback_id"):
            continue
        who = f.get("interviewer") if isinstance(f.get("interviewer"), str) else None
        review = f.get("stage") == "Resume Review"
        title = (f"Resume review note, {when(f.get('interview_at'))}" if review else f"{f.get('stage', 'Interview')} feedback, {when(f.get('interview_at'))}"
                 + (f" ({who})" if who else ""))
        scores = ", ".join(f"{words(k)} {v}" for k, v in (f.get("criteria") or {}).items() if isinstance(v, (int, float)))
        out[f"feedback:{f['feedback_id']}"] = _card(f"feedback:{f['feedback_id']}", "feedback", title, [
            REC_WORDS.get(f.get("recommendation"), ""), f"Scores: {scores}" if scores else ""],
            _quote("Reviewer's note" if review else "Interviewer's note", text_of(f.get("summary_text"))), who_id=cid)
    for e in c.get("stage_history", []):
        if not e.get("event_id"):
            continue
        bits = [f"Interview booked {when(e['scheduled_at'])}" if e.get("scheduled_at") else "", f"Interview held {when(e['interview_at'])}" if e.get("interview_at") else "",
                f"Left the stage {when(e['exited_at'])} ({e.get('exit_outcome')})" if e.get("exited_at") else "Still in this stage"]
        out[f"stage:{e['event_id']}"] = _card(f"stage:{e['event_id']}", "stage", f"{name}: {e.get('stage', 'stage')}, entered {when(e.get('entered_at'))}", bits)


def evidence_line(fit: dict) -> str:
    """Interviews held, feedback received and feedback pending, stated separately: more interviews does not mean a stronger candidate."""
    done, got, wait = fit.get("interviews_completed"), fit.get("feedback_received"), fit.get("feedback_pending")
    if done is None or got is None or wait is None:
        return f"Interview and review notes on file: {fit.get('feedback_count')}" if fit.get("feedback_count") is not None else ""
    return f"Interviews held: {done}. Feedback received: {got}. Feedback pending: {wait}."


def build(tool: str, env: dict, names: dict[str, str]) -> dict[str, dict]:
    """{ref: card} for the successful result `env` (projected form). Never raises: cards are a convenience, so a malformed
    record yields fewer cards, not a failed turn."""
    try:
        return _build(tool, env, names)
    except (KeyError, TypeError, ValueError, AttributeError):
        return {}


def _build(tool: str, env: dict, names: dict[str, str]) -> dict[str, dict]:
    data, out = env.get("data") or {}, {}
    if tool == "get_candidates":
        for c in data.get("candidates", []):
            if not c.get("not_found"):
                _candidate(c, out)
    elif tool == "search_candidates":
        for r in data.get("candidates", []):
            fit, name = r.get("fit_signals") or {}, text_of(r.get("name")) or "This candidate"
            have, miss = fit.get("required_skills_matched") or [], fit.get("required_skills_missing") or []
            out[f"search:{r['candidate_id']}"] = _card(f"search:{r['candidate_id']}", "search", f"{name}: search summary (record not opened)", [
                f"Currently in {r.get('current_stage')}", f"Right now: {STATE_WORDS[r['waiting_state']]}" if r.get("waiting_state") in STATE_WORDS else "",
                f"Required skills matched: {len(have)} of {len(have) + len(miss)}" if have or miss else "",
                evidence_line(fit)])
    elif tool in ("list_roles", "get_role"):
        for r in data.get("roles", [data]):
            if r.get("role_id") and r.get("title") and f"role:{r['role_id']}" in env.get("source_refs", []):
                out[f"role:{r['role_id']}"] = _card(f"role:{r['role_id']}", "role", r["title"], [
                    f"Opened {when(r.get('opened_at'))}" if r.get("opened_at") else "", f"Open for {r['role_age_days']} days" if r.get("role_age_days") is not None else ""])
    elif tool == "get_candidate_communications":
        for m in data.get("messages", []):
            inbound = m.get("direction") == "inbound"
            body = "\n\n".join(t for t in (text_of(m.get("subject")), text_of(m.get("body"))) if t)
            out[f"email:{m['email_id']}"] = _card(f"email:{m['email_id']}", "email", f"Email {'from' if inbound else 'to'} {{who}}, {when(m.get('sent_at'))}", [],
                                                  _quote("Email (candidate-supplied text)" if inbound else "Recruiter's email", body), who_id=data.get("candidate_id"))
    elif tool == "get_tasks":
        for t in data.get("tasks", []):
            out[f"task:{t['task_id']}"] = _card(f"task:{t['task_id']}", "task", f"{t.get('description')}" + (f" ({t['candidate_name']})" if isinstance(t.get("candidate_name"), str) else "{who}"), [
                f"Owner: {t.get('owner')}", f"Due {when(t.get('due_at'))}", "Overdue" + (f" by {t['days_overdue']:.0f} day(s)" if t.get("days_overdue") and t["days_overdue"] >= 1 else " (under a day)") if t.get("overdue") else ""],
                                                        who_id=t.get("candidate_id"))
    elif tool == "get_pipeline_analytics":
        ref = next((r for r in env.get("source_refs", []) if r.startswith("analytics:")), None)
        if ref:
            title = data.get("role_title") or data.get("role_id")
            stages = [f"{s.get('stage')}: {s['funnel']['entered']} entered, {s['funnel']['still_in_stage']} still in the stage"
                      for s in data.get("stages", []) if (s.get("funnel") or {}).get("entered") is not None and "still_in_stage" in s["funnel"]]
            out[ref] = _card(ref, "analytics", f"{title} pipeline, as of {ref.partition('@')[2]}", [f"Role open {data.get('role_age_days')} days"] + stages)
    return out
