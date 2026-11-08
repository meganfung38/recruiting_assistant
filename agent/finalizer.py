"""Handles the local `submit_answer` tool: parse -> validate against this turn's evidence -> accept, or reject once
with specific problems (the single repair round). Whatever is still wrong after the repair round is not hidden:
it is rendered as unverified. Deterministic; the model cannot talk its way past it."""
import json
from dataclasses import dataclass, field
from typing import Callable

from pydantic import ValidationError

from agent.answer_schema import SubmittedAnswer
from agent.evidence import EvidenceRegistry, ValidationReport, render_markdown, validate_plain, validate_submission
from agent.tool_executor import ToolCall

FIX = ("Correct these and call submit_answer again, on its own. You may call data tools first. This is the only repair "
       "opportunity: anything still unsupported will be shown to the recruiter marked as unverified.")


@dataclass
class Decision:
    content: dict                                   # the tool message returned to the model
    answer: SubmittedAnswer | None = None           # set when accepted
    report: ValidationReport | None = None
    rendered: str = ""                              # accepted markdown with unverified citations marked


# Problems whose remedy is to FETCH data (or drop the candidate), not to fix a typo: they get extra retries of their own.
RETRIEVAL_PROBLEMS = {"undisclosed_incomplete", "candidate_not_retrieved", "unretrieved_available"}
INCOMPLETE_RETRIES = 2


@dataclass
class Finalizer:
    evidence: EvidenceRegistry
    can_retrieve: Callable[[], bool] = lambda: False       # does the turn still have budget to fetch more records?
    repair_used: bool = False
    incomplete_rejections: int = 0
    repaired: list[str] = field(default_factory=list)   # categories found on the FIRST submission (eval signal)

    def _reject(self, reason: str, problems: list[str]) -> Decision:
        return Decision({"status": "rejected", "reason": reason, "problems": problems, "how_to_fix": FIX})

    def _repair_or_accept(self, report: ValidationReport, answer, markdown: str) -> Decision:
        """One repair round for citation/figure/candidate-ID problems. Separately, an answer that hides or ranks unretrieved
        candidates may be sent back up to INCOMPLETE_RETRIES more times: it needs more data, not a typo fix, and ranking
        from a partial set is the costly failure. What remains afterwards is accepted but shown as unverified/incomplete."""
        other = [c for c in report.categories if c not in RETRIEVAL_PROBLEMS]
        incomplete = any(c in RETRIEVAL_PROBLEMS for c in report.categories)
        if not self.repaired:
            self.repaired = report.categories                # what the FIRST submission got wrong (an eval signal)
        if (other and not self.repair_used) or (incomplete and self.incomplete_rejections < INCOMPLETE_RETRIES):
            self.repair_used = self.repair_used or bool(other)
            self.incomplete_rejections += incomplete
            return self._reject("validation_failed", report.problems())
        note = {"status": "accepted", "unverified_items": len(report.problems())}
        return Decision(note, answer, report, render_markdown(markdown, report))

    def submit(self, call: ToolCall, mixed: bool, duplicate: bool) -> Decision:
        if duplicate:
            return Decision({"status": "rejected", "reason": "submit_answer_called_more_than_once"})
        if mixed:   # does not consume the repair round: the model simply has not finished gathering evidence
            return Decision({"status": "rejected", "reason": "call_submit_answer_alone",
                             "how_to_fix": "Call submit_answer in its own response, after you have all tool results."})
        try:
            answer = SubmittedAnswer.model_validate(call.arguments if isinstance(call.arguments, dict) else {})
        except ValidationError as exc:
            problems = [f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()][:8]
            return self._reject("invalid_arguments", problems)
        return self._repair_or_accept(validate_submission(answer, self.evidence, self.can_retrieve()), answer, answer.answer_markdown)

    def plain_text(self, text: str) -> tuple[Decision, str | None]:
        """Fallback when the model answers in prose. Returns (decision, nudge): a nudge means 'repair once'."""
        report = validate_plain(text, self.evidence, self.can_retrieve())
        d = self._repair_or_accept(report, None, text)
        if d.content["status"] == "rejected":
            return d, ("Your answer has problems:\n- " + "\n- ".join(report.problems()) +
                       "\nGive the corrected answer by calling submit_answer.")
        return d, None
