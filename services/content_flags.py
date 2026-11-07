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
