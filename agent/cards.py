"""Evidence cards: one card per person the answer is about, all in the same shape, so a ranking or comparison can be checked
side by side.

  Skills          required skills matched or missing, preferred skills matched
  Interviews      one line per interview stage with feedback: stage, date, interviewer, recommendation in words, scores,
                  and the interviewer's note (shown as inert text)
  Still pending   the current stage when it has no feedback yet (e.g. "booked on Nov 3 but not held yet")

Resume-review notes are left out for everyone: they are a recruiter's screening note, not interview evidence, and including them
for some people made it look as if those people had more evidence. Built only from the record the model was shown (projected), so
a card can never show more than the model saw. Pure functions, no Streamlit.
"""
import re

REC_WORDS = {"strong_yes": "Strongly recommends advancing", "yes": "Recommends advancing", "mixed": "Mixed view",
             "no": "Does not recommend advancing", "strong_no": "Strongly does not recommend advancing"}
SCREENING = "Resume Review"
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def when(display: str | None) -> str:
    """'2026-10-28 13:00 PDT' -> 'Oct 28'."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", display or "")
    return f"{_MONTHS[int(m.group(2)) - 1]} {int(m.group(3))}" if m else ""


def text_of(v) -> str | None:
    """A field that may be wrapped as untrusted: {'untrusted_text': ...} -> the text."""
    return v if isinstance(v, str) else v.get("untrusted_text") if isinstance(v, dict) else None


def _skills(r: dict) -> list[str]:
    have, miss, pref = r.get("required_skills_matched") or [], r.get("required_skills_missing") or [], r.get("preferred_skills_matched") or []
    lines = [("All required skills: " + ", ".join(have)) if have and not miss else
             ("Required skills: has " + (", ".join(have) or "none") + "; missing " + ", ".join(miss)) if miss else ""]
    lines.append("Preferred skills: " + (", ".join(pref) if pref else "none matched"))
    return [line for line in lines if line]


def candidate_card(record: dict, name: str, role: str | None = None, reason: str | None = None, gaps: list[str] | None = None) -> dict:
    interviews = []
    for f in record.get("feedback", []):
        if f.get("stage") == SCREENING:
            continue
        scores = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in (f.get("criteria") or {}).items() if isinstance(v, (int, float)))
        who = f.get("interviewer") if isinstance(f.get("interviewer"), str) else ""
        interviews.append({"title": " · ".join(p for p in (f.get("stage"), when(f.get("interview_at")), who) if p),
                           "verdict": REC_WORDS.get(f.get("recommendation"), "") + (f" ({scores})" if scores else ""),
                           "note": (text_of(f.get("summary_text")) or "").strip()})
    current = record.get("current_stage")
    has_current = any(f.get("stage") == current for f in record.get("feedback", []))
    status = record.get("stage_status") if isinstance(record.get("stage_status"), str) else ""
    pending = status if current and current != SCREENING and not has_current else ""
    return {"person": name, "role": role or "", "reason": reason or "", "gaps": list(gaps or []), "status": status,
            "skills": _skills(record), "interviews": interviews, "pending": pending}
