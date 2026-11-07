"""Per-turn limits. Configurable constants; the executor and orchestrator both enforce them."""
import time
from dataclasses import dataclass


class LimitExceeded(Exception):
    """A per-turn budget was hit. `code` is a stable, model-safe error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Limits:
    max_rounds: int = 8
    max_tool_calls: int = 24
    tool_timeout_s: float = 10.0
    turn_timeout_s: float = 90.0
    max_result_chars: int = 24_000


class TurnBudget:
    """Mutable counters for one assistant turn."""

    def __init__(self, limits: Limits = Limits(), clock=time.monotonic):
        self.limits, self._clock = limits, clock
        self.started = clock()
        self.calls = 0
        self.rounds = 0

    def remaining_s(self) -> float:
        return self.limits.turn_timeout_s - (self._clock() - self.started)

    def begin_round(self) -> None:
        if self.rounds >= self.limits.max_rounds:
            raise LimitExceeded("round_limit")
        self.rounds += 1

    def count_call(self) -> float:
        """Reserve one tool call; returns the timeout (seconds) to use for it."""
        if self.remaining_s() <= 0:
            raise LimitExceeded("turn_timeout")
        if self.calls >= self.limits.max_tool_calls:
            raise LimitExceeded("call_budget_exceeded")
        self.calls += 1
        return min(self.limits.tool_timeout_s, self.remaining_s())
