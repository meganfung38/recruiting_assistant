"""Executor-side argument models: the SINGLE source for (a) validating model-supplied tool calls and
(b) the tool schemas shown to the LLM. A parity test keeps them consistent with the MCP server."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RoleRef = Annotated[str, Field(min_length=2, max_length=60, pattern=r"^[A-Za-z][A-Za-z0-9 &/-]*$",
                                description="Role ID (ROLE-BE) or role name (Backend Engineer)")]
CandidateId = Annotated[str, Field(pattern=r"^C\d{3}$", description="Candidate ID, e.g. C104")]
CandidateRef = Annotated[str, Field(min_length=2, max_length=60, pattern=r"^[A-Za-z][A-Za-z0-9 .'-]*$",
                                    description="Candidate ID (C113) or the person's name (Mateo Villareal)")]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown arguments are rejected, never ignored


class ListRolesArgs(_Args):
    pass


class SearchCandidatesArgs(_Args):
    role: RoleRef | None = None
    name: Annotated[str, Field(max_length=60, description="Part or all of a person's name")] | None = None
    stage: Annotated[str, Field(max_length=40, description="Only people currently at this stage")] | None = None
    status: Literal["active", "rejected", "withdrawn"] | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 20


class GetCandidatesArgs(_Args):
    candidate_ids: Annotated[list[CandidateId], Field(min_length=1, max_length=20, description="1 to 20 candidate IDs")] | None = None
    names: Annotated[list[Annotated[str, Field(min_length=2, max_length=60)]],
                     Field(min_length=1, max_length=10, description="1 to 10 people's names, full or partial")] | None = None
    role: RoleRef | None = None

    @model_validator(mode="after")
    def _one_of(self):
        if sum(x is not None for x in (self.candidate_ids, self.names, self.role)) != 1:
            raise ValueError("give exactly one of candidate_ids, names or role")
        return self


class GetCandidateCommunicationsArgs(_Args):
    candidate_id: CandidateId
    limit: Annotated[int, Field(ge=1, le=10)] = 10


class GetPipelineAnalyticsArgs(_Args):
    role: RoleRef


class GetRecruitingOverviewArgs(_Args):
    pass


class GetTasksArgs(_Args):
    role: RoleRef | None = None
    candidate: CandidateRef | None = None
    status: Literal["pending", "completed", "cancelled"] | None = None
    overdue_only: bool = False
    limit: Annotated[int, Field(ge=1, le=50)] = 50


ARG_MODELS: dict[str, type[_Args]] = {
    "list_roles": ListRolesArgs, "search_candidates": SearchCandidatesArgs,
    "get_candidates": GetCandidatesArgs, "get_candidate_communications": GetCandidateCommunicationsArgs,
    "get_pipeline_analytics": GetPipelineAnalyticsArgs, "get_tasks": GetTasksArgs,
    "get_recruiting_overview": GetRecruitingOverviewArgs,
}


def _clean(schema):
    """Drop pydantic noise (titles) so the model sees a compact JSON schema."""
    if isinstance(schema, dict):
        return {k: _clean(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_clean(v) for v in schema]
    return schema


def tool_definitions(descriptions: dict[str, str]) -> list[dict]:
    """OpenAI-style function definitions generated from the executor models."""
    return [{"type": "function", "function": {
        "name": name, "description": descriptions.get(name, ""),
        "parameters": _clean(model.model_json_schema())}} for name, model in ARG_MODELS.items()]
