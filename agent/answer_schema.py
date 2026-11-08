"""`submit_answer`: the local finalization tool. It is handled by the orchestrator, is NOT an MCP tool and is
not in the seven-tool allowlist.

The model writes the answer in plain recruiter language and declares only WHO it is about (candidate and role IDs) and, for a
ranking or comparison, the order with a one-line reason. It does not write citations or figure paths: the application derives the
evidence from those declarations and checks the numbers in the prose deterministically (agent/evidence.py). Bookkeeping the model is
bad at is done by code; judgment and wording stay with the model."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

CandidateId = Annotated[str, Field(pattern=r"^C\d{3}$")]
RoleId = Annotated[str, Field(pattern=r"^ROLE-[A-Z]{2,3}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ranked(_Strict):
    candidate_id: CandidateId
    reason: Annotated[str, Field(max_length=300, description="What puts them here, in terms of skills and what the interview feedback says")]
    gaps: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=5, description="Missing skills, concerns, or evidence still pending")] = []


class SubmittedAnswer(_Strict):
    answer_markdown: Annotated[str, Field(min_length=1, max_length=8000, description=(
        "The answer the recruiter reads, in markdown and plain words: names, never IDs or field names, and no citation tokens. "
        "First sentence: the answer, correcting any wrong premise in the question (e.g. how long a role has been open). Three or "
        "more roles, tasks, people or stages: ONE markdown table with a header row, not bullets. Copy every number exactly as the "
        "tool results give it; never add up or derive numbers yourself."))]
    candidates: Annotated[list[CandidateId], Field(max_length=30, description="IDs of every candidate the answer talks about")] = []
    roles: Annotated[list[RoleId], Field(max_length=5, description="IDs of every role the answer talks about")] = []
    ranking: Annotated[list[Ranked], Field(max_length=10, description=(
        "REQUIRED whenever the answer ranks, orders or compares candidates (a top three, who is strongest, X versus Y), best first; "
        "the recruiter's evidence cards follow this order. Leave empty for status, task and pipeline answers."))] = []
    uncertainties: Annotated[list[Annotated[str, Field(max_length=400)]], Field(max_length=10, description="Missing evidence, small samples")] = []


DESCRIPTION = (
    "Finish the turn: submit the final answer exactly once, after you have all the evidence you need. Do not call it in the same "
    "response as other tools. Every declared candidate and role, and every number in the answer, is checked against this turn's "
    "tool results.")


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
