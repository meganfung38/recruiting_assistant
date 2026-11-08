"""`submit_answer`: the local finalization tool. It is handled by the orchestrator, is NOT an MCP tool and is
not in the seven-tool allowlist. The model uses it to hand back a structured, citable answer that the
deterministic validator in agent/evidence.py can check."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt

CandidateId = Annotated[str, Field(pattern=r"^C\d{3}$")]
RoleId = Annotated[str, Field(pattern=r"^ROLE-[A-Z]{2,3}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateEntry(_Strict):
    candidate_id: CandidateId
    role_id: RoleId
    rationale: Annotated[str, Field(max_length=1200, description="Why, in terms of the role's requirements and evidence")]
    evidence: Annotated[list[Annotated[str, Field(max_length=60)]], Field(max_length=12, description="Citation IDs, e.g. feedback:FB-031, cv:C104 (record retrieved), search:C104 (search row only)")]
    gaps: Annotated[list[Annotated[str, Field(max_length=300)]], Field(max_length=8, description="Missing evidence or concerns, e.g. no system design feedback")]


class Figure(_Strict):
    label: Annotated[str, Field(max_length=120)]
    value: StrictInt | StrictFloat          # a real number copied exactly from the tool result (booleans are rejected)
    evidence: Annotated[str, Field(max_length=60, description="Source of the number: analytics:ROLE-BE@<date>, task:T-004, or cv:C104 for a candidate's own numbers")]
    field_path: Annotated[str, Field(max_length=200, description="Where the exact value sits, e.g. stages[Technical Interview].scheduling.lag_median_scheduled_days, days_overdue, feedback_count, days_in_current_stage, fit_signals.avg_criteria_score")]


class SubmittedAnswer(_Strict):
    answer_markdown: Annotated[str, Field(min_length=1, max_length=8000, description="Narrative with inline citations like [feedback:FB-018]")]
    candidates: Annotated[list[CandidateEntry], Field(max_length=10)] = []
    figures: Annotated[list[Figure], Field(max_length=20)] = []
    uncertainties: Annotated[list[Annotated[str, Field(max_length=400)]], Field(max_length=10)] = []
    content_warnings: Annotated[list[Annotated[str, Field(max_length=400)]], Field(max_length=10, description="Unused: leave empty. The application reports flagged content itself.")] = []


DESCRIPTION = (
    "Finish the turn: submit the final answer exactly once, after you have all the evidence you need. "
    "Do not call it in the same response as other tools. Every citation, candidate and figure is checked against the "
    "tool results you received this turn.")


def _inline(node, defs):
    """Resolve $ref/$defs so the schema shown to the model is flat and self-contained."""
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(defs[node["$ref"].split("/")[-1]], defs)
        return {k: _inline(v, defs) for k, v in node.items() if k not in ("$defs", "title")}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def submit_answer_definition() -> dict:
    schema = SubmittedAnswer.model_json_schema()
    return {"type": "function", "function": {"name": "submit_answer", "description": DESCRIPTION,
                                             "parameters": _inline(schema, schema.get("$defs", {}))}}
