"""Sanitized execution trace, produced by Python instrumentation (never authored by the LLM).

Events hold tool name, validated arguments, status, timing, counts and IDs only. Never CV text,
feedback, emails, prompts, keys, or raw model reasoning.
"""
import re
import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field


class TraceEvent(BaseModel):
    trace_id: str
    call_id: str
    sequence: int                     # dispatch order
    round: int = 0                    # the model round whose tool call this was (1-based)
    tool_name: str
    safe_arguments: dict
    status: Literal["success", "error", "timeout", "validation_error"]
    duration_ms: int
    started_ms: int                   # offsets from turn start; parallel calls keep dispatch order
    completed_ms: int
    result_summary: dict = Field(default_factory=dict)
    result_chars: int = 0             # size of what the model received (its main context cost)
    evidence_refs: list[str] = Field(default_factory=list)
    error_code: str | None = None


class RoundRecord(BaseModel):
    """One model call: what it cost and what the model did with it. The model's reasoning is never recorded, only its actions."""
    round: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    action: Literal["tools", "submit", "plain_text", "empty", "wrap_up"] = "tools"
    outcome: str = ""                 # e.g. "accepted", "sent back", "deferred: call submit_answer alone"
    problems: list[str] = Field(default_factory=list)   # why an answer was sent back (IDs and numbers only)


def safe_tool_name(name: object) -> str:
    """Model-supplied tool names are echoed only if they look like identifiers."""
    return name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) else "<invalid>"


def summarize(safe_env: dict) -> dict:
    """Deterministic summary of a PROJECTED result: counts, IDs and flag pattern IDs, no text."""
    data = safe_env.get("data") or {}
    return {
        "counts": {k: len(v) for k, v in data.items() if isinstance(v, list)},
        "source_ids": list(safe_env.get("source_refs", []))[:40],
        "content_flags": [{"candidate_id": f["candidate_id"], "pattern_id": f["pattern_id"]}
                          for f in safe_env.get("content_flags", [])],
        "omitted_ids": list(safe_env.get("omitted", [])),
        "warnings": len(safe_env.get("warnings", []))}


class Trace:
    def __init__(self, clock=time.monotonic):
        self.trace_id = uuid.uuid4().hex[:12]
        self._clock = clock
        self._t0 = clock()
        self._seq = 0
        self.events: list[TraceEvent] = []
        self.rounds: list[RoundRecord] = []
        self.model_rounds = 0

    def now_ms(self) -> int:
        return int((self._clock() - self._t0) * 1000)

    def next_sequence(self) -> int:
        self._seq += 1
        return self._seq

    def record(self, *, call_id, sequence, tool_name, safe_arguments, status, started_ms,
               result_summary=None, evidence_refs=None, error_code=None, result_chars=0) -> TraceEvent:
        done = self.now_ms()
        ev = TraceEvent(trace_id=self.trace_id, call_id=str(call_id)[:64], sequence=sequence, round=self.model_rounds,
                        tool_name=safe_tool_name(tool_name), safe_arguments=safe_arguments, status=status,
                        duration_ms=max(0, done - started_ms), started_ms=started_ms, completed_ms=done,
                        result_summary=result_summary or {}, evidence_refs=evidence_refs or [], error_code=error_code,
                        result_chars=result_chars)
        self.events.append(ev)
        return ev

    def begin_round(self) -> RoundRecord:
        self.model_rounds += 1
        rec = RoundRecord(round=self.model_rounds)
        self.rounds.append(rec)
        return rec

    def ordered(self) -> list[TraceEvent]:
        return sorted(self.events, key=lambda e: e.sequence)

    def totals(self) -> dict:
        return {"tool_calls": len(self.events), "total_ms": self.now_ms(), "model_rounds": self.model_rounds,
                "prompt_tokens": sum(r.prompt_tokens for r in self.rounds), "completion_tokens": sum(r.completion_tokens for r in self.rounds)}

    def to_log(self) -> dict:
        """The whole trace as plain data, for the per-turn JSONL log (same safety rules: IDs, counts, sizes, no record text)."""
        return {"trace_id": self.trace_id, **self.totals(), "rounds": [r.model_dump() for r in self.rounds],
                "calls": [e.model_dump(exclude={"trace_id"}) for e in self.ordered()]}
