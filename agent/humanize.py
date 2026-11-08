"""Turns model text into recruiter language: no citation tokens, no internal IDs, no field names.

This is a SAFETY NET for the default view. The prompt tells the model to write names and plain words; this guarantees that a slip
never reaches the recruiter. Deterministic and pure; the original text is still validated and kept for the technical details.
"""
import re

TOOLS = "list_roles|search_candidates|get_candidates|get_candidate_communications|get_pipeline_analytics|get_tasks|get_recruiting_overview"
CITATION = re.compile(r"[ \t]*\[(?:(?:" + TOOLS.replace("_", "[_ ]") + r")\]|(?:cv|search|feedback|stage|email|task|role|analytics):[^\]]*\])")
NAME_ID = re.compile(r"\s*\((?:C\d{3}|ROLE-[A-Z]{2,3})\)")        # "Name (C104)" and "Backend Engineer (ROLE-BE)": the title already says it
CAND_ID = re.compile(r"\bC\d{3}\b")
ROLE_ID = re.compile(r"\bROLE-[A-Z]{2,3}\b")
TASK_ID = re.compile(r"\bT-\d{3}\b")
OTHER_ID = re.compile(r"\b(?:FB|EV|EM)-\d{3}\b")
SNAKE = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
DOT_ZERO = re.compile(r"(\d)\.0\b(?!\d)")
PHRASES = {  # more natural than a plain underscore-to-space
    "awaiting_scheduling": "waiting to be scheduled", "awaiting_interview": "interview booked", "awaiting_feedback": "waiting on interview feedback",
    "awaiting_decision": "awaiting a decision", "in_review": "in resume review", "waiting_days": "days waiting", "days_in_current_stage": "days in the current stage",
    "strong_yes": "strong yes", "strong_no": "strong no"}
LEAK = re.compile(r"\bC\d{3}\b|\b(?:T|FB|EV|EM)-\d{3}\b|\bROLE-[A-Z]{2,3}\b|\[(?:cv|search|feedback|stage|email|task|role|analytics):|\b[a-z]+(?:_[a-z]+)+\b")


def humanize(text: str, registry) -> str:
    """Names for IDs, readable words for field names, and no citation syntax."""
    if not text:
        return text
    t = CITATION.sub("", text).replace("`", "")                                 # backticks mark field names / code, never recruiter language
    t = NAME_ID.sub("", t)                                                      # "Dahlia Ramanathan (C104)" -> "Dahlia Ramanathan"
    t = CAND_ID.sub(lambda m: registry.candidate_name(m.group(0)) or "a candidate", t)
    t = ROLE_ID.sub(lambda m: registry.role_title(m.group(0)) or "the role", t)
    t = TASK_ID.sub(lambda m: (registry.card(f"task:{m.group(0)}") or {}).get("title", ""), t)       # a task ID becomes its readable name
    t = OTHER_ID.sub("", t)
    for snake, plain in PHRASES.items():
        t = re.sub(rf"\b{snake}\b", plain, t)
    t = SNAKE.sub(lambda m: m.group(0).replace("_", " "), t)
    t = DOT_ZERO.sub(r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" +([.,;:!?])", r"\1", t)                                       # citation removal leaves "word ." behind
    return re.sub(r"\(\s*\)", "", t).strip()


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
