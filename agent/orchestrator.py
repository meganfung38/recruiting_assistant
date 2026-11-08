"""One assistant turn: model <-> tools loop. The model only chooses tools; every call goes through the
ToolExecutor (the trusted boundary). One MCP stdio session is opened per turn inside a single asyncio.run()
and closed in a finally (see README: Streamlit reruns the script, so nothing is cached across turns).
"""
import asyncio
import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent.answer_schema import SubmittedAnswer, submit_answer_definition
from agent.evidence import EvidenceRegistry, ValidationReport, validate_plain
from agent.finalizer import Finalizer
from agent.limits import LimitExceeded, Limits, TurnBudget
from agent.llm import LLMUnavailable
from agent.mcp_client import McpSession
from agent.prompts import EMPTY_NUDGE, LIMIT_MESSAGES, SYSTEM_PROMPT, WRAP_UP_NOTICE
from agent.tool_executor import ToolExecutor, TurnContext
from agent.tracing import Trace

MAX_EMPTY_RETRIES = 2          # empty model replies tolerated per turn before giving up (each also uses one model round)
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
    """Per-session history. Every earlier turn is replayed as the question and the final answer only, never its tool results:
    that keeps follow-ups small and makes the model re-query anything that may have changed. Only the current turn
    keeps its tool calls and results paired (they are built fresh in _loop)."""
    turns: list[list[dict]] = field(default_factory=list)

    def context_messages(self) -> list[dict]:
        """Each earlier turn as: the question, the visible answer, and (if any) the separate note of who it was about."""
        out = []
        for msgs in self.turns:
            tail = [msgs[-2], msgs[-1]] if _is_about_note(msgs[-1]) and len(msgs) > 2 else [msgs[-1]]
            out += [msgs[0], *tail]
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
    report: ValidationReport | None = None       # who the answer is about, and the people / roles / numbers checks
    repaired: list[str] = field(default_factory=list)   # problem categories found on the first submission
    empty_retries: int = 0                       # empty model replies that were nudged and retried this turn
    flag_sources: list = field(default_factory=list)   # raw flagged candidate text for the human reviewer; never sent anywhere else
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})


def _notify(callback, event: dict) -> None:
    """Progress is a convenience for the UI: a failing callback must never affect the turn."""
    if callback is not None:
        try:
            callback(event)
        except Exception:
            pass


ABOUT_PREFIX = "Context for follow-up questions (not part of the answer, never repeat it): the previous answer was about "


def _about_note(report, evidence) -> dict | None:
    """For the model's history only: who the answer was about, with IDs, so a follow-up ("of those three") can fetch them again.
    It is a separate system message, not part of the assistant's answer: when it was appended to the answer text, the model copied
    the format into its next reply and the recruiter saw it."""
    if report is None or not (report.candidates or report.roles):
        return None
    people = [f"{evidence.candidate_name(c) or c} ({c})" for c in report.candidates[:12]]
    roles = [f"{evidence.role_title(r) or r} ({r})" for r in report.roles]
    return {"role": "system", "content": ABOUT_PREFIX + "; ".join(people + roles) + "."}


def _is_about_note(m: dict) -> bool:
    return m.get("role") == "system" and str(m.get("content", "")).startswith(ABOUT_PREFIX)


def user_message(question: str, role_context: str | None) -> dict:
    if not role_context:
        return {"role": "user", "content": question}
    note = (f"[UI context: the recruiter has {role_context} selected. This is a default, not a restriction; "
            "the question may concern other roles.]")
    return {"role": "user", "content": f"{note}\n\n{question}"}


class Orchestrator:
    def __init__(self, llm, db_path=None, limits: Limits = Limits(), session_factory=None, system_prompt: str = SYSTEM_PROMPT,
                 log_path: str | Path | None = None):
        self.llm, self.limits, self.system_prompt = llm, limits, system_prompt
        self.session_factory = session_factory or (lambda: McpSession(db_path))
        self.log_path = Path(log_path) if log_path else None      # one JSON line per turn, for tracing a bad answer afterwards

    def run_turn(self, question: str, state: ConversationState | None = None, role_context: str | None = None,
                 on_progress=None) -> TurnResult:
        """Sync entry point for the CLI and Streamlit: exactly one event loop and one MCP session per call.
        `on_progress(event)` is called with {"type": "model"} before each model call and {"type": "tools", "names": [...]} before tool calls."""
        state = state if state is not None else ConversationState()
        result = asyncio.run(self._run(question, state, role_context, on_progress))
        if result.status != "error":
            state.turns.append(result.messages)
        self._log(question, result)
        return result

    def _log(self, question: str, result: TurnResult) -> None:
        """Append the turn to the JSONL log: question, answer, status, validation problems, and the sanitized trace (IDs, counts,
        sizes, tokens; never record text). A logging failure never affects the turn."""
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "question": question, "status": result.status,
                     "answer": result.answer, "repaired": result.repaired, "problems": result.report.problems() if result.report else [],
                     **result.trace.to_log()}
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def _run(self, question, state, role_context, on_progress=None) -> TurnResult:
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
                return await self._loop(executor, ctx, state, user, on_progress, question)
            finally:
                close = getattr(self.llm, "aclose", None)
                if close:
                    await close()                      # close HTTP connections while their event loop is still running

    def _failed(self, code, ctx, turn_msgs, usage=None) -> TurnResult:
        return TurnResult(UNAVAILABLE[code], "error", ctx.trace, ctx.evidence, error_code=code, messages=turn_msgs,
                          usage=usage or {"prompt_tokens": 0, "completion_tokens": 0})

    async def _ask(self, messages, tools, ctx, timeout: float, usage: dict):
        """One model call, recorded as a round (tokens and time) whatever happens next."""
        rec, t0 = ctx.trace.begin_round(), time.monotonic()
        try:
            resp = await asyncio.wait_for(self.llm.complete(messages, tools), max(0.1, timeout))
        finally:
            rec.duration_ms = int((time.monotonic() - t0) * 1000)
        rec.prompt_tokens, rec.completion_tokens = resp.usage.get("prompt_tokens", 0), resp.usage.get("completion_tokens", 0)
        rec.reasoning_tokens = resp.usage.get("reasoning_tokens", 0)
        for k in usage:
            usage[k] += resp.usage.get(k, 0)
        return resp, rec

    async def _loop(self, executor, ctx, state, user, on_progress=None, question: str = "") -> TurnResult:
        messages = [{"role": "system", "content": self.system_prompt}, *state.context_messages(), user]
        turn_msgs, flags = [user], []
        tools = executor.tool_definitions() + [submit_answer_definition()]   # submit_answer is local, never sent to MCP
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        fin, accepted, answer_text, limit = Finalizer(ctx.evidence, question), None, None, None
        empty = 0

        while accepted is None and answer_text is None and limit is None:
            try:
                ctx.budget.begin_round()
                if ctx.budget.remaining_s() <= 0:
                    raise LimitExceeded("turn_timeout")
                _notify(on_progress, {"type": "model"})
                resp, rec = await self._ask(messages, tools, ctx, ctx.budget.remaining_s(), usage)
            except LimitExceeded as exc:
                limit = exc.code
                break
            except asyncio.TimeoutError:
                limit = "turn_timeout"
                break
            except LLMUnavailable as exc:
                return self._failed(exc.code, ctx, turn_msgs, usage)
            if not resp.tool_calls and not (resp.content or "").strip():
                # An empty completion (no text, no tool call) is a model hiccup, not an answer. It is NOT added to the
                # conversation (an empty assistant message is invalid to replay); nudge and try again, within bounds.
                empty += 1
                rec.action, rec.outcome = "empty", "empty reply, nudged"
                if empty > MAX_EMPTY_RETRIES:
                    failed = self._failed("empty_answer", ctx, turn_msgs, usage)
                    failed.empty_retries = empty - 1
                    return failed
                messages.append({"role": "system", "content": EMPTY_NUDGE})
                continue
            messages.append(resp.message)
            turn_msgs.append(resp.message)
            if not resp.tool_calls:                                   # plain-text answer: validated by regex only
                decision, nudge = fin.plain_text(resp.content)
                rec.action = "plain_text"
                if nudge:
                    rec.outcome, rec.problems = "sent back", decision.content.get("problems", [])
                    messages.append({"role": "system", "content": nudge})
                    continue
                rec.outcome = "accepted"
                accepted, answer_text = decision, decision.rendered
                break
            submits = [tc for tc in resp.tool_calls if tc.name == "submit_answer"]
            data_calls = [tc for tc in resp.tool_calls if tc.name != "submit_answer"]
            if data_calls:
                _notify(on_progress, {"type": "tools", "names": [tc.name for tc in data_calls]})
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
                if i == 0:
                    rec.action = "submit" if not data_calls else "tools"
                    rec.outcome = ("accepted" if d.content.get("status") == "accepted" else
                                   "sent back" if d.content.get("reason") in ("validation_failed", "invalid_arguments") else
                                   f"deferred: {d.content.get('reason', '')}")
                    rec.problems = list(d.content.get("problems", []))
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
            if submitted is None:        # plain text: the model's own message is already last
                turn_msgs[-1] = {"role": "assistant", "content": answer_text}
            else:                        # structured: history ends in the visible answer text, after the tool result
                turn_msgs.append({"role": "assistant", "content": answer_text})
            if (note := _about_note(report, ctx.evidence)):
                turn_msgs.append(note)
        elif limit is not None:
            status = "limit_reached"
            raw = await self._wrap_up(messages, ctx, limit, usage)
            report = validate_plain(raw, ctx.evidence, question)
            answer_text = raw
            turn_msgs.append({"role": "assistant", "content": answer_text})
        unique = {(f["candidate_id"], f["field"], f["pattern_id"]): f for f in flags}
        return TurnResult(answer_text, status, ctx.trace, ctx.evidence, limit_code=limit,
                          content_flags=[unique[k] for k in sorted(unique)], messages=turn_msgs, usage=usage,
                          submitted=submitted, report=report, repaired=fin.repaired, empty_retries=empty,
                          flag_sources=ctx.flag_sources)

    async def _wrap_up(self, messages, ctx, limit: str, usage: dict) -> str:
        """After a limit: one last model call with tools disabled, so the recruiter still gets what was found."""
        static = f"{LIMIT_MESSAGES[limit]} This answer is incomplete."
        if limit == "turn_timeout":
            return static                                            # no time left to ask again
        notice = {"role": "system", "content": WRAP_UP_NOTICE.format(reason=LIMIT_MESSAGES[limit].rstrip("."))}
        try:
            resp, rec = await self._ask([*messages, notice], None, ctx, WRAP_UP_GRACE_S, usage)
        except (LLMUnavailable, asyncio.TimeoutError):
            return static
        rec.action, rec.outcome = "wrap_up", "answered without tools after a limit"
        return (resp.content or "").strip() or static
