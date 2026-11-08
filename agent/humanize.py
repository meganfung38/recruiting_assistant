"""A small safety net for the default view: the model is asked to write names and plain words, and this removes anything internal
that slips through (a citation token, an ID, a field name) without ever touching markdown structure.

It works line by line and leaves each line's list marker or table pipes alone. (The previous version tidied punctuation across the
whole text: deleting "FB-025" from "- FB-025, Resume Review" left "-, Resume Review", which is no longer a bullet, so the list
collapsed into one line.) Deterministic and pure.
"""
import re

TOOLS = "list_roles|search_candidates|get_candidates|get_candidate_communications|get_pipeline_analytics|get_tasks|get_recruiting_overview"
CITATION = re.compile(r"[ \t]*\[(?:(?:" + TOOLS.replace("_", "[_ ]") + r")\]|(?:cv|search|feedback|stage|email|task|role|analytics):[^\]]*\])")
NAME_ID = re.compile(r"\s*\((?:C\d{3}|ROLE-[A-Z]{2,3})\)")        # "Name (C104)": the name already says it
CAND_ID = re.compile(r"\bC\d{3}\b")
ROLE_ID = re.compile(r"\bROLE-[A-Z]{2,3}\b")
RECORD_ID = re.compile(r"\b(?:T|FB|EV|EM)-\d{3}\b[,;:]?[ \t]*")    # a record ID and the separator that followed it
SNAKE = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
DOT_ZERO = re.compile(r"(\d)\.0\b(?!\d)")
LEAD = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+|\s*)(.*)$")              # (list marker or indent, rest of the line)
PHRASES = {"strong_yes": "strong yes", "strong_no": "strong no", "awaiting_scheduling": "waiting to be scheduled",
           "awaiting_interview": "interview booked", "awaiting_feedback": "waiting on interview feedback",
           "awaiting_decision": "awaiting a decision", "in_review": "in resume review", "waiting_days": "days waiting",
           "days_waiting": "days waiting", "days_in_current_stage": "days in the current stage", "days_in_stage": "days in the stage"}
LEAK = re.compile(r"\bC\d{3}\b|\b(?:T|FB|EV|EM)-\d{3}\b|\bROLE-[A-Z]{2,3}\b|\[(?:cv|search|feedback|stage|email|task|role|analytics):|\b[a-z]+(?:_[a-z]+)+\b")


def _line(line: str, registry) -> str:
    marker, rest = LEAD.match(line).groups()
    is_table = rest.lstrip().startswith("|")
    t = CITATION.sub("", rest).replace("`", "")
    t = NAME_ID.sub("", t)
    t = CAND_ID.sub(lambda m: registry.candidate_name(m.group(0)) or "a candidate", t)
    t = ROLE_ID.sub(lambda m: registry.role_title(m.group(0)) or "the role", t)
    t = RECORD_ID.sub("", t)
    for snake, plain in PHRASES.items():
        t = re.sub(rf"\b{snake}\b", plain, t)
    t = SNAKE.sub(lambda m: m.group(0).replace("_", " "), t)
    t = DOT_ZERO.sub(r"\1", t)
    t = re.sub(r"(?<=\S)[ \t]{2,}", " ", t)
    t = re.sub(r"(?<=\w) +([.,;:!?])", r"\1", t)
    t = re.sub(r"\(\s*\)", "", t)
    if not is_table:
        t = t.lstrip(",;: ")
    return marker + t.rstrip()


def humanize(text: str, registry) -> str:
    """Names for IDs, readable words for field names, no citation syntax; markdown structure is kept exactly."""
    if not text:
        return text
    return "\n".join(_line(line, registry) for line in text.split("\n")).strip()


def leaks(text: str) -> list[str]:
    """What would still look internal to a recruiter (for tests and the validator)."""
    return sorted(set(LEAK.findall(text or "")))


DETECTOR_TERM = re.compile(r"ignore[ _]instructions|system[ _]override|rank[ _]candidate|role[ _]reassignment|new[ _]instructions|\bconcealment\b", re.I)
PLACEHOLDER = "[flagged text: see the review panel]"


def redact_flagged(text: str, sources: list[dict]) -> str:
    """Defense in depth for the default view. The model is told never to quote flagged instructions or detector terms; if it does anyway,
    remove the quoted text (we hold the original) and any sentence that reports detector internals. The original stays reviewable in the panel."""
    if not text or not sources:
        return text
    t = text
    for s in sources:
        whole = s["excerpt"].strip()
        pieces = [whole] + [p for p in re.split(r"(?<=[.!?])\s+", whole)]
        for piece in sorted({p.strip() for p in pieces if len(p.strip()) >= 20}, key=len, reverse=True):
            t = re.sub(re.escape(piece), PLACEHOLDER, t, flags=re.I)
    t = re.sub(rf"(?:{re.escape(PLACEHOLDER)}[\s,;]*){{2,}}", PLACEHOLDER + " ", t)
    # Drop only the SENTENCE (or list item) that reports detector internals, never its neighbours.
    parts = re.split(r"((?<=[.!?])[ \t]+|\n)", t)                                       # text, separator, text, separator, ...
    out = []
    for i in range(0, len(parts), 2):
        unit, sep = parts[i], parts[i + 1] if i + 1 < len(parts) else ""
        if DETECTOR_TERM.search(unit):
            out.append("\n" if sep == "\n" else "")
        else:
            out.append(unit + sep)
    t = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", t).strip()
