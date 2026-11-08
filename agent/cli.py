"""Command-line runner: see answers, evidence and the real tool trace without Streamlit.

    python -m agent.cli "Who are the top three candidates for the Backend Engineer role, and why?" --role ROLE-BE
    python -m agent.cli --chat --role ROLE-BE        # multi-turn session (for follow-up questions)
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.limits import Limits
from agent.llm import OpenAILLM
from agent.orchestrator import ConversationState, Orchestrator, TurnResult
from agent.presenter import technical_view, trace_lines
from agent.runtime import TURN_LOG
from services.db import db_path


def format_structured(r: TurnResult) -> list[str]:
    """The declared parts of a submit_answer and what validation found: who it is about, the ranking, caveats, problems."""
    a, out = r.submitted, []
    if r.report is not None and (r.report.candidates or r.report.roles):
        out.append("  About: " + ", ".join(r.report.candidates + r.report.roles))
    for n, x in enumerate(a.ranking if a else [], 1):
        out.append(f"  {n}. {x.candidate_id}: {x.reason}" + (f"  Gaps: {'; '.join(x.gaps)}" if x.gaps else ""))
    out += [f"  Uncertainty: {u}" for u in (a.uncertainties if a else [])]
    out += [f"  [NOT MATCHED] {p}" for p in (r.report.problems() if r.report else [])]
    return ["-- Checked answer details", *out] if out else []


def format_result(r: TurnResult, show_trace: bool = True) -> str:
    out = [r.answer.strip(), ""]
    out += format_structured(r)
    if r.repaired:
        out.append(f"(the answer was corrected once after validation: {', '.join(r.repaired)})")
    if r.status == "limit_reached":
        out.append(f"! Limit reached ({r.limit_code}): this answer may be incomplete.")
    if r.content_flags:
        who = sorted({f['candidate_id'] for f in r.content_flags})
        out.append(f"! Content warning: instruction-like text was found in candidate-supplied content for {', '.join(who)}.")
    if show_trace:
        out += ["--", *trace_lines(technical_view(r))]
        out.append(f"-- Evidence used ({len(r.evidence.refs())}): " + ", ".join(r.evidence.refs()))
    return "\n".join(out)


def main(argv=None, orchestrator: Orchestrator | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.cli")
    ap.add_argument("question", nargs="*")
    ap.add_argument("--role", help="role ID to pre-select as default context, e.g. ROLE-BE")
    ap.add_argument("--db", help="SQLite file (default: RECRUITING_DB_PATH or data/recruiting.db)")
    ap.add_argument("--chat", action="store_true", help="interactive multi-turn session")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--max-result-chars", type=int, help="override the per-result size cap (to force truncation)")
    ap.add_argument("--max-tool-calls", type=int, help="override the per-turn tool-call budget")
    ap.add_argument("--max-rounds", type=int, help="override the model-round cap")
    a = ap.parse_args(argv)
    load_dotenv()
    if orchestrator is None:
        target = a.db or db_path()
        if not Path(target).exists():
            print(f"Database {target} not found. Run: python -m data.seed", file=sys.stderr)
            return 2
        try:
            base = Limits()
            limits = Limits(max_rounds=a.max_rounds or base.max_rounds, max_tool_calls=a.max_tool_calls or base.max_tool_calls,
                            max_result_chars=a.max_result_chars or base.max_result_chars)
            orchestrator = Orchestrator(OpenAILLM(), db_path=a.db, limits=limits, log_path=os.environ.get("RECRUITING_TURN_LOG") or TURN_LOG)
        except ValueError as exc:
            print(f"{exc}", file=sys.stderr)
            return 2
    state = ConversationState()
    ask = " ".join(a.question)
    if not a.chat and not ask:
        ap.error("give a question, or use --chat")
    while True:
        if a.chat:
            try:
                ask = input("you> ").strip()
            except EOFError:
                return 0
            if ask in ("exit", "quit"):
                return 0
            if ask == "/reset":
                state.reset()
                print("(conversation reset)")
                continue
            if not ask:
                continue
        result = orchestrator.run_turn(ask, state, a.role)
        print(format_result(result, not a.no_trace))
        if not a.chat:
            return 0 if result.status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
