"""Handles the local `submit_answer` tool: parse -> validate against this turn's evidence -> accept, or reject once
with specific problems (the single repair round). Whatever is still wrong after the repair round is not hidden:
it is rendered as unverified. Deterministic; the model cannot talk its way past it."""
from dataclasses import dataclass, field
from pydantic import ValidationError

from agent.answer_schema import SubmittedAnswer
from agent.evidence import EvidenceRegistry, ValidationReport, render_markdown, validate_plain, validate_submission
from agent.tool_executor import ToolCall

FIX = ("Correct these and call submit_answer again, on its own. Use the results you already have: fix a figure's evidence or "
       "field_path, correct its value, or remove it (figures are optional); never call tools just to support a figure. Call a tool only "
       "if a listed candidate's record is missing. This is the only repair opportunity: anything still unsupported will be shown to the "
       "recruiter as unverified.")


@dataclass
class Decision:
    content: dict                                   # the tool message returned to the model
    answer: SubmittedAnswer | None = None           # set when accepted
    report: ValidationReport | None = None
    rendered: str = ""                              # accepted markdown with unverified citations marked


@dataclass
class Finalizer:
    evidence: EvidenceRegistry
    repair_used: bool = False
    repaired: list[str] = field(default_factory=list)   # categories found on the FIRST submission (eval signal)

    def _reject(self, reason: str, problems: list[str]) -> Decision:
        return Decision({"status": "rejected", "reason": reason, "problems": problems, "how_to_fix": FIX})

    def _repair_or_accept(self, report: ValidationReport, answer, markdown: str) -> Decision:
        """One repair round; whatever is still wrong afterwards is accepted but shown as unverified."""
        if not self.repaired:
            self.repaired = report.categories                # what the FIRST submission got wrong (an eval signal)
        if report.categories and not self.repair_used:
            self.repair_used = True
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
        return self._repair_or_accept(validate_submission(answer, self.evidence), answer, answer.answer_markdown)

    def plain_text(self, text: str) -> tuple[Decision, str | None]:
        """Fallback when the model answers in prose. Returns (decision, nudge): a nudge means 'repair once'."""
        report = validate_plain(text, self.evidence)
        d = self._repair_or_accept(report, None, text)
        if d.content["status"] == "rejected":
            return d, ("Your answer has problems:\n- " + "\n- ".join(report.problems()) +
                       "\nGive the corrected answer by calling submit_answer.")
        return d, None
