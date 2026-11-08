"""Deterministic detector for instruction-like text in untrusted (candidate-supplied) fields.

A recruiter-facing aid and a regression-test hook, NOT a security guarantee: paraphrases, other
languages or obfuscation will evade it. Flags carry pattern IDs only, never the matched text.
"""
import re

PATTERNS = {
    "ignore_instructions": r"\b(ignore|disregard)\s+(all|any|previous|prior|other|the above|earlier)\b",
    "system_override": r"\bsystem\s+(override|prompt|instructions?)\b",
    "rank_candidate": r"\brank\s+(this|the|that)\s+(candidate|applicant|resume|cv)\b",
    "concealment": r"\bdo\s+not\s+(mention|reveal|disclose|tell)\b",
    "role_reassignment": r"\byou\s+are\s+now\b",
    "new_instructions": r"\b(new|updated)\s+instructions?\b",
}
_COMPILED = {pid: re.compile(rx, re.I) for pid, rx in PATTERNS.items()}


def scan(text: str | None) -> list[str]:
    """Pattern IDs that match `text`, in a stable order."""
    return [pid for pid, rx in _COMPILED.items() if text and rx.search(text)]


def flags_for(candidate_id: str, fields: dict[str, str | None]) -> list[dict]:
    """[{candidate_id, field, pattern_id}] for every (field, pattern) that matches. No matched text."""
    return [{"candidate_id": candidate_id, "field": field, "pattern_id": pid}
            for field, text in fields.items() for pid in scan(text)]


# What each pattern means in plain words, for the recruiter-facing notice. The pattern IDs themselves are never shown.
INTENTS = {
    "ignore_instructions": "told the reviewer to ignore other applicants or earlier rules",
    "system_override": "claimed system-level authority",
    "rank_candidate": "asked to be ranked first",
    "concealment": "asked for the instructions to be kept hidden",
    "role_reassignment": "tried to change the reviewer's role",
    "new_instructions": "presented new instructions",
}
EXCERPT_CHARS = 600


def intents(pattern_ids) -> list[str]:
    return [INTENTS[p] for p in PATTERNS if p in set(pattern_ids)]


def _location(start: int, length: int) -> str:
    if length < 400:
        return "in the text"
    ratio = start / length
    return "near the start" if ratio < 0.2 else "near the end" if ratio > 0.7 else "in the middle"


# A sentence ends at . ! ? followed by whitespace or the end of the text, or at a line break. (Not at the dot inside "example.com".)
_BOUNDARY = re.compile(r"[.!?](?=\s|$)|\n")
REDACTED = "[removed: instruction-like text]"


def _sentence(text: str, first: int, last: int) -> tuple[int, int]:
    """Bounds of the sentence(s) holding text[first:last]."""
    before = [m.end() for m in _BOUNDARY.finditer(text, 0, first)]
    after = _BOUNDARY.search(text, last)
    return (before[-1] if before else 0), (after.end() if after else len(text))


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> tuple[str, str]:
    """(the sentences containing the matches, where in the text they sit). RAW candidate-supplied text: for the human reviewer's
    screen only. It must never be added to a trace, a log, the evidence registry, or any message sent to the model."""
    spans = sorted((m.start(), m.end()) for rx in _COMPILED.values() for m in rx.finditer(text or ""))
    if not spans:
        return "", ""
    first, last = spans[0][0], spans[-1][1]
    start, end = _sentence(text, first, last)
    piece = text[start:end].strip()
    if len(piece) > limit:
        piece = piece[:limit].rstrip() + "\u2026"
    return piece, _location(first, len(text))


def redact(text: str) -> str:
    """The text the MODEL may read: only the sentences that contain a detected instruction are replaced by a marker, so the rest of a CV
    (skills, experience) stays intact. The original is kept separately for the human reviewer (see sources_for)."""
    spans = sorted((m.start(), m.end()) for rx in _COMPILED.values() for m in rx.finditer(text or ""))
    if not spans:
        return text
    cuts: list[list[int]] = []
    for st, en in spans:
        a, b = _sentence(text, st, en)
        if cuts and a <= cuts[-1][1]:
            cuts[-1][1] = max(cuts[-1][1], b)
        else:
            cuts.append([a, b])
    out, pos = [], 0
    for a, b in cuts:
        out += [text[pos:a], (" " if text[a:a + 1] != "\n" and a > pos else "") + REDACTED + (" " if b < len(text) and text[b - 1:b] != "\n" else "")]
        pos = b
    return re.sub(r"[ \t]{2,}", " ", "".join(out) + text[pos:]).strip()


def sources_for(candidate_id: str, fields: dict[str, str | None]) -> list[dict]:
    """One entry per flagged field: {candidate_id, field, location, excerpt, intents}. Raw text inside; see `excerpt`."""
    out = []
    for field, text in fields.items():
        hits = scan(text)
        if hits:
            piece, where = excerpt(text)
            out.append({"candidate_id": candidate_id, "field": field, "location": where, "excerpt": piece, "intents": intents(hits)})
    return out
