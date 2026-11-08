"""One assistant turn: model <-> tools loop. The model only chooses tools; every call goes through the
ToolExecutor (the trusted boundary). One MCP stdio session is opened per turn inside a single asyncio.run()
and closed in a finally (see README: Streamlit reruns the script, so nothing is cached across turns).
"""
import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from agent.answer_schema import SubmittedAnswer, submit_answer_definition
from agent.evidence import EvidenceRegistry, ValidationReport, render_markdown, validate_plain
from agent.finalizer import Finalizer
from agent.limits import LimitExceeded, Limits, TurnBudget
from agent.llm import LLMUnavailable
from agent.mcp_client import McpSession
from agent.prompts import LIMIT_MESSAGES, SYSTEM_PROMPT, WRAP_UP_NOTICE
from agent.tool_executor import ToolExecutor, TurnContext
from agent.tracing import Trace

WRAP_UP_GRACE_S = 30.0          # extra time allowed for the final no-tools answer after a step/call limit
UNAVAILABLE = {
    "llm_auth_error": "The language model rejected the API key. Check OPENAI_API_KEY.",
    "llm_rate_limited": "The language model is rate limited right now. Try again shortly.",
    "llm_bad_request": "The language model rejected the request. Check OPENAI_MODEL.",
    "llm_unavailable": "The language model is unavailable right now.",
    "data_service_unavailable": "The recruiting data service could not be started.",
    "empty_answer": "The model returned no answer.",
}


@dataclass
class ConversationState:
    """Per-session history. The latest turn is replayed in full (tool calls and results kept paired);
    older turns are reduced to the question and the final answer, which keeps context small and pushes the
    model to re-query anything that may have changed."""
    turns: list[list[dict]] = field(default_factory=list)

    def context_messages(self) -> list[dict]:
        out: list[dict] = []
        for i, msgs in enumerate(self.turns):
            out += msgs if i == len(self.turns) - 1 else [msgs[0], msgs[-1]]
        return out

    def reset(self) -> None:
        self.turns.clear()


@dataclass
class TurnResult:
    answer: str
    status: str                                   # ok | limit_reached | error
    trace: Trace
    evidence: EvidenceRegistry
    error_code: str | None = None
    limit_code: str | None = None
    content_flags: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)   # this turn's messages, for history
    submitted: SubmittedAnswer | None = None     # structured answer (None for plain-text / limit / error answers)
    report: ValidationReport | None = None       # citation / candidate / figure validation of the final answer
    repaired: list[str] = field(default_factory=list)   # problem categories found on the first submission
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})

    @property
    def unresolved_candidates(self) -> list[str]:
        return sorted(self.evidence.unresolved_candidates())


def user_message(question: str, role_context: str | None) -> dict:
    if not role_context:
        return {"role": "user", "content": question}
    note = (f"[UI context: the recruiter has {role_context} selected. This is a default, not a restriction; "
            "the question may concern other roles.]")
    return {"role": "user", "content": f"{note}\n\n{question}"}


class Orchestrator:
    def __init__(self, llm, db_path=None, limits: Limits = Limits(), session_factory=None, system_prompt: str = SYSTEM_PROMPT):
        self.llm, self.limits, self.system_prompt = llm, limits, system_prompt
        self.session_factory = session_factory or (lambda: McpSession(db_path))

    def run_turn(self, question: str, state: ConversationState | None = None, role_context: str | None = None) -> TurnResult:
        """Sync entry point for the CLI and Streamlit: exactly one event loop and one MCP session per call."""
        state = state if state is not None else ConversationState()
        result = asyncio.run(self._run(question, state, role_context))
        if result.status != "error":
            state.turns.append(result.messages)
        return result

    async def _run(self, question, state, role_context) -> TurnResult:
        ctx = TurnContext(budget=TurnBudget(self.limits), trace=Trace())
        user = user_message(question, role_context)
        async with AsyncExitStack() as stack:
            try:
                session = await stack.enter_async_context(self.session_factory())
                executor = ToolExecutor(session)
                await executor.start()
            except Exception:                      # MCP startup failure or a server/tool contract mismatch
                return self._failed("data_service_unavailable", ctx, [user])
            try:
                return await self._loop(executor, ctx, state, user)
            finally:
                close = getattr(self.llm, "aclose", None)
                if close:
                    await close()                      # close HTTP connections while their event loop is still running

    def _failed(self, code, ctx, turn_msgs, usage=None) -> TurnResult:
        return TurnResult(UNAVAILABLE[code], "error", ctx.trace, ctx.evidence, error_code=code, messages=turn_msgs,
                          usage=usage or {"prompt_tokens": 0, "completion_tokens": 0})

    async def _ask(self, messages, tools, ctx, timeout: float):
        ctx.trace.model_rounds += 1
        return await asyncio.wait_for(self.llm.complete(messages, tools), max(0.1, timeout))

    async def _loop(self, executor, ctx, state, user) -> TurnResult:
        messages = [{"role": "system", "content": self.system_prompt}, *state.context_messages(), user]
        turn_msgs, flags = [user], []
        tools = executor.tool_definitions() + [submit_answer_definition()]   # submit_answer is local, never sent to MCP
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        def can_retrieve() -> bool:     # a rejected answer costs a fetch round plus a resubmit round, so both must still fit
            lim = ctx.budget.limits
            return ctx.budget.calls < lim.max_tool_calls and ctx.budget.rounds <= lim.max_rounds - 2 and ctx.budget.remaining_s() > 15
        fin, accepted, answer_text, limit = Finalizer(ctx.evidence, can_retrieve), None, None, None

        def tally(resp):
            for k in usage:
                usage[k] += resp.usage.get(k, 0)

        while accepted is None and answer_text is None and limit is None:
            try:
                ctx.budget.begin_round()
                if ctx.budget.remaining_s() <= 0:
                    raise LimitExceeded("turn_timeout")
                resp = await self._ask(messages, tools, ctx, ctx.budget.remaining_s())
            except LimitExceeded as exc:
                limit = exc.code
                break
            except asyncio.TimeoutError:
                limit = "turn_timeout"
                break
            except LLMUnavailable as exc:
                return self._failed(exc.code, ctx, turn_msgs, usage)
            tally(resp)
            messages.append(resp.message)
            turn_msgs.append(resp.message)
            if not resp.tool_calls:                                   # plain-text answer: validated by regex only
                if not (resp.content or "").strip():
                    return self._failed("empty_answer", ctx, turn_msgs, usage)
                decision, nudge = fin.plain_text(resp.content)
                if nudge:
                    messages.append({"role": "system", "content": nudge})
                    continue
                accepted, answer_text = decision, decision.rendered
                break
            submits = [tc for tc in resp.tool_calls if tc.name == "submit_answer"]
            data_calls = [tc for tc in resp.tool_calls if tc.name != "submit_answer"]
            outcomes = await asyncio.gather(*(executor.execute(tc, ctx) for tc in data_calls))
            results = {}
            for tc, out in zip(data_calls, outcomes):
                results[tc.id] = out.content
                flags += out.content.get("content_flags", [])
                if out.error_code in ("call_budget_exceeded", "turn_timeout"):
                    limit = out.error_code
            for i, tc in enumerate(submits):
                d = fin.submit(tc, mixed=bool(data_calls), duplicate=i > 0)
                results[tc.id] = d.content
                if d.answer is not None and accepted is None:
                    accepted = d
            for tc in resp.tool_calls:
                msg = {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(results[tc.id], ensure_ascii=False)}
                messages.append(msg)
                turn_msgs.append(msg)
            if accepted is not None:
                answer_text = accepted.rendered
                limit = None
        status, report, submitted = "ok", None, None
        if accepted is not None:
            report, submitted = accepted.report, accepted.answer
            if submitted is None:        # plain text: the model's own message is already last; show the marked-up version
                turn_msgs[-1] = {"role": "assistant", "content": answer_text}
            else:                        # structured: history ends in the visible answer text, after the tool result
                turn_msgs.append({"role": "assistant", "content": answer_text})
        elif limit is not None:
            status = "limit_reached"
            raw = await self._wrap_up(messages, ctx, limit, usage)
            report = validate_plain(raw, ctx.evidence)
            answer_text = render_markdown(raw, report)
            turn_msgs.append({"role": "assistant", "content": answer_text})
        unique = {(f["candidate_id"], f["field"], f["pattern_id"]): f for f in flags}
        return TurnResult(answer_text, status, ctx.trace, ctx.evidence, limit_code=limit,
                          content_flags=[unique[k] for k in sorted(unique)], messages=turn_msgs, usage=usage,
                          submitted=submitted, report=report, repaired=fin.repaired)

    async def _wrap_up(self, messages, ctx, limit: str, usage: dict) -> str:
        """After a limit: one last model call with tools disabled, so the recruiter still gets what was found."""
        static = f"{LIMIT_MESSAGES[limit]} The evidence gathered so far is listed below, but this answer is incomplete."
        if limit == "turn_timeout":
            return static                                            # no time left to ask again
        missing = sorted(ctx.evidence.unresolved_candidates())
        unreviewed = (f" Their records were requested but never retrieved, so you must name every one of them as not reviewed: "
                      f"{', '.join(missing)}.") if missing else ""
        notice = {"role": "system", "content": WRAP_UP_NOTICE.format(reason=LIMIT_MESSAGES[limit].rstrip("."), unreviewed=unreviewed)}
        try:
            resp = await self._ask([*messages, notice], None, ctx, WRAP_UP_GRACE_S)
        except (LLMUnavailable, asyncio.TimeoutError):
            return static
        for k in usage:
            usage[k] += resp.usage.get(k, 0)
        return (resp.content or "").strip() or static
