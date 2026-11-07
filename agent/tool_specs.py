"""Executor-side argument models: the SINGLE source for (a) validating model-supplied tool calls and
(b) the tool schemas shown to the LLM. A parity test keeps them consistent with the MCP server."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

RoleId = Annotated[str, Field(pattern=r"^ROLE-[A-Z]{2,3}$", description="Role ID, e.g. ROLE-BE")]
CandidateId = Annotated[str, Field(pattern=r"^C\d{3}$", description="Candidate ID, e.g. C104")]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown arguments are rejected, never ignored


class ListRolesArgs(_Args):
    pass


class GetRoleArgs(_Args):
    role_id: RoleId


class SearchCandidatesArgs(_Args):
    role_id: RoleId
    status: Literal["active", "rejected", "withdrawn"] = "active"
    stage: Annotated[str, Field(max_length=40, description="Filter to one current stage")] | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 20


class GetCandidatesArgs(_Args):
    candidate_ids: Annotated[list[CandidateId], Field(min_length=1, max_length=6)]


class GetCandidateCommunicationsArgs(_Args):
    candidate_id: CandidateId
    limit: Annotated[int, Field(ge=1, le=10)] = 10


class GetPipelineAnalyticsArgs(_Args):
    role_id: RoleId


class GetTasksArgs(_Args):
    role_id: RoleId | None = None
    candidate_id: CandidateId | None = None
    status: Literal["pending", "completed", "cancelled"] | None = None
    overdue_only: bool = False
    limit: Annotated[int, Field(ge=1, le=50)] = 50


ARG_MODELS: dict[str, type[_Args]] = {
    "list_roles": ListRolesArgs, "get_role": GetRoleArgs, "search_candidates": SearchCandidatesArgs,
    "get_candidates": GetCandidatesArgs, "get_candidate_communications": GetCandidateCommunicationsArgs,
    "get_pipeline_analytics": GetPipelineAnalyticsArgs, "get_tasks": GetTasksArgs,
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
