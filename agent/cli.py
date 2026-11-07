"""Command-line runner: see answers, evidence and the real tool trace without Streamlit.

    python -m agent.cli "Who are the top three candidates for the Backend Engineer role, and why?" --role ROLE-BE
    python -m agent.cli --chat --role ROLE-BE        # multi-turn session (for follow-up questions)
"""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.llm import OpenAILLM
from agent.orchestrator import ConversationState, Orchestrator, TurnResult
from services.db import db_path


def format_result(r: TurnResult, show_trace: bool = True) -> str:
    out = [r.answer.strip(), ""]
    if r.status == "limit_reached":
        out.append(f"! Limit reached ({r.limit_code}): this answer may be incomplete.")
    if r.unresolved_candidates:
        out.append(f"! Incomplete comparison: results omitted these candidates and they were never retrieved: "
                   f"{', '.join(r.unresolved_candidates)}")
    if r.content_flags:
        who = sorted({f['candidate_id'] for f in r.content_flags})
        out.append(f"! Content warning: instruction-like text was found in candidate-supplied content for {', '.join(who)}.")
    if show_trace:
        t = r.trace.totals()
        out.append(f"-- Tools used ({t['tool_calls']}) - {t['total_ms'] / 1000:.1f}s - {t['model_rounds']} model round(s)")
        for e in r.trace.ordered():
            args = json.dumps({k: v for k, v in e.safe_arguments.items() if v is not None}, ensure_ascii=False)
            note = e.error_code or ", ".join(f"{k}={v}" for k, v in e.result_summary.get("counts", {}).items())
            out.append(f"  {e.sequence}. {e.tool_name} {args} -> {e.status} {e.duration_ms}ms {note}".rstrip())
        out.append(f"-- Evidence used ({len(r.evidence.refs())}): " + ", ".join(r.evidence.refs()))
    return "\n".join(out)


def main(argv=None, orchestrator: Orchestrator | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.cli")
    ap.add_argument("question", nargs="*")
    ap.add_argument("--role", help="role ID to pre-select as default context, e.g. ROLE-BE")
    ap.add_argument("--db", help="SQLite file (default: RECRUITING_DB_PATH or data/recruiting.db)")
    ap.add_argument("--chat", action="store_true", help="interactive multi-turn session")
    ap.add_argument("--no-trace", action="store_true")
    a = ap.parse_args(argv)
    load_dotenv()
    if orchestrator is None:
        target = a.db or db_path()
        if not Path(target).exists():
            print(f"Database {target} not found. Run: python -m data.seed", file=sys.stderr)
            return 2
        try:
            orchestrator = Orchestrator(OpenAILLM(), db_path=a.db)
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
