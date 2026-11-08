"""Captures the ORIGINAL flagged candidate text so a human reviewer can read it.

This runs at the trusted boundary on the raw tool result, before projection. The result lives only in TurnContext / TurnResult for
the UI. It is deliberately never added to the trace, the evidence registry, the model's messages, or any log, so injected
instructions are not repeated into the conversation. Display must render it as inert text.
"""
import re

from services.content_flags import sources_for

MAX_EXCERPT = 700
FIELD_LABELS = {"cv_text": "CV", "feedback_summary": "interview feedback", "exit_reason": "exit reason",
                "email_subject": "email subject", "email_body": "email"}


def _clean(src: dict, candidate_id: str) -> dict | None:
    """Accept only well-formed entries (search rows arrive from the server as plain data)."""
    try:
        field, excerpt = str(src["field"]), str(src["excerpt"])[:MAX_EXCERPT]
        intents = [str(i)[:80] for i in src.get("intents", [])][:6]
        where = str(src.get("location", ""))[:30]
    except (KeyError, TypeError, AttributeError):
        return None
    if not re.fullmatch(r"C\d{3}", candidate_id) or not excerpt:
        return None
    return {"candidate_id": candidate_id, "field": field, "field_label": FIELD_LABELS.get(field.split(":")[0], "text"),
            "location": where, "excerpt": excerpt, "intents": intents}


def capture(tool: str, raw_envelope: dict) -> list[dict]:
    data = raw_envelope.get("data") or {}
    found: list[dict] = []
    if tool == "get_candidates":
        for c in data.get("candidates", []):
            if c.get("not_found"):
                continue
            fields = {"cv_text": c.get("cv_text")}
            fields.update({f"feedback_summary:{f['feedback_id']}": f.get("summary_text") for f in c.get("feedback", [])})
            fields.update({f"exit_reason:{e['event_id']}": e.get("exit_reason") for e in c.get("stage_history", [])})
            found += sources_for(c["candidate_id"], fields)
    elif tool == "get_candidate_communications":
        for m in data.get("messages", []):
            found += sources_for(data["candidate_id"], {f"email_subject:{m['email_id']}": m.get("subject"), f"email_body:{m['email_id']}": m.get("body")})
    elif tool == "search_candidates":
        for row in data.get("candidates", []):
            found += [dict(s, candidate_id=row["candidate_id"]) for s in row.get("flag_sources", []) if isinstance(s, dict)]
    return [c for c in (_clean(s, s.get("candidate_id", "")) for s in found) if c]


def merge(existing: list[dict], new: list[dict]) -> list[dict]:
    """One entry per (candidate, field); a later call may add the same field again."""
    seen = {(s["candidate_id"], s["field"]) for s in existing}
    return existing + [s for s in new if (s["candidate_id"], s["field"]) not in seen]
