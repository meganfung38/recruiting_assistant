"""The trusted execution boundary: plain Python between the model and the MCP server.

Every model-requested call goes through `ToolExecutor.execute`: allowlist -> argument validation ->
budget -> MCP call with timeout -> projection -> size cap -> evidence + trace. `execute` never raises
for model-attributable problems; it returns a ToolOutcome whose `content` is safe to show the model.
"""
import asyncio
from dataclasses import dataclass, field

from pydantic import ValidationError

from agent import flag_sources
from agent.evidence import EvidenceRegistry, aggregates
from agent.limits import LimitExceeded, Limits, TurnBudget
from agent.projection import cap_size, project
from agent.tool_specs import ARG_MODELS, tool_definitions
from agent.tracing import Trace, safe_tool_name, summarize

ERROR_MESSAGES = {
    "invalid_arguments": "Invalid arguments.",
    "call_budget_exceeded": "The tool-call budget for this turn is used up. Answer with the evidence you already have.",
    "turn_timeout": "The time limit for this turn was reached. Answer with the evidence you already have.",
    "tool_timeout": "The tool took too long to respond.",
    "upstream_error": "The data service is unavailable right now.",
}
STATUS_FOR = {"tool_not_allowed": "validation_error", "invalid_arguments": "validation_error",
              "tool_timeout": "timeout", "turn_timeout": "timeout"}


class ServerContractError(Exception):
    """The MCP server's tools do not match the executor's expected seven; fail closed at startup."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ToolOutcome:
    call_id: str
    name: str
    ok: bool
    content: dict                      # exactly what is appended to the model's context
    error_code: str | None = None


@dataclass
class TurnContext:
    budget: TurnBudget = field(default_factory=lambda: TurnBudget(Limits()))
    evidence: EvidenceRegistry = field(default_factory=EvidenceRegistry)
    trace: Trace = field(default_factory=Trace)
    flag_sources: list = field(default_factory=list)    # raw flagged text for the human reviewer ONLY (see agent/flag_sources.py)


def _error_content(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


class ToolExecutor:
    def __init__(self, client):
        self.client = client           # anything with: async list_tools() and async call_tool(name, args)
        self.allowed: frozenset[str] = frozenset()
        self.descriptions: dict[str, str] = {}

    async def start(self) -> None:
        """Derive the allowlist from the server's own tool list; refuse to run on any mismatch."""
        advertised = await self.client.list_tools()
        if set(advertised) != set(ARG_MODELS):
            raise ServerContractError(f"server tools {sorted(advertised)} != expected {sorted(ARG_MODELS)}")
        self.descriptions, self.allowed = advertised, frozenset(advertised)

    def tool_definitions(self) -> list[dict]:
        return tool_definitions(self.descriptions)

    async def execute(self, call: ToolCall, ctx: TurnContext) -> ToolOutcome:
        seq, t0 = ctx.trace.next_sequence(), ctx.trace.now_ms()
        name = call.name if isinstance(call.name, str) else ""

        def fail(code: str, message: str | None = None, args: dict | None = None) -> ToolOutcome:
            if name == "get_candidates" and args and code in ("call_budget_exceeded", "turn_timeout", "tool_timeout", "upstream_error"):
                ctx.evidence.note_unfulfilled(args.get("candidate_ids", []))     # wanted, but could not be retrieved
            ctx.trace.record(call_id=call.id, sequence=seq, tool_name=name, started_ms=t0, error_code=code,
                             safe_arguments=args if args is not None else {},
                             status=STATUS_FOR.get(code, "error"))
            return ToolOutcome(call.id, safe_tool_name(name), False,
                               _error_content(code, message or ERROR_MESSAGES[code]), code)

        if name not in self.allowed:
            return fail("tool_not_allowed", f"Unknown tool. Available tools: {sorted(self.allowed)}")
        raw_args = call.arguments if isinstance(call.arguments, dict) else {}
        try:
            args = ARG_MODELS[name].model_validate(raw_args)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
            # Values are never echoed or traced: only argument names are kept.
            return fail("invalid_arguments", f"Invalid arguments: {problems}"[:600], {"_arg_names": sorted(map(str, raw_args))})
        safe_args = args.model_dump(mode="json")
        try:
            timeout = ctx.budget.count_call()
        except LimitExceeded as exc:
            return fail(exc.code, args=safe_args)
        try:
            raw = await asyncio.wait_for(self.client.call_tool(name, safe_args), timeout)
        except asyncio.TimeoutError:
            return fail("tool_timeout" if timeout >= ctx.budget.limits.tool_timeout_s else "turn_timeout", args=safe_args)
        except Exception:                      # protocol failure, crash, ...: never leak details to the model
            return fail("upstream_error", args=safe_args)

        ctx.flag_sources = flag_sources.merge(ctx.flag_sources, flag_sources.capture(name, raw))   # before projection; UI-only
        safe = cap_size(name, project(name, raw), ctx.budget.limits.max_result_chars)
        if safe["error"]:                      # an expected tool-level error (e.g. not_found): shown, not evidence
            code = safe["error"]["code"]
            ctx.trace.record(call_id=call.id, sequence=seq, tool_name=name, safe_arguments=safe_args, status="error",
                             started_ms=t0, error_code=code, result_summary=summarize(safe))
            return ToolOutcome(call.id, name, False, safe, code)
        ctx.evidence.add(name, safe, aggregates(name, raw))
        ctx.trace.record(call_id=call.id, sequence=seq, tool_name=name, safe_arguments=safe_args, status="success",
                         started_ms=t0, result_summary=summarize(safe), evidence_refs=safe["source_refs"])
        return ToolOutcome(call.id, name, True, safe)
