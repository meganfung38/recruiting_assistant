"""Command-line runner: see answers, evidence and the real tool trace without Streamlit.

    python -m agent.cli "Who are the top three candidates for the Backend Engineer role, and why?" --role ROLE-BE
    python -m agent.cli --chat --role ROLE-BE        # multi-turn session (for follow-up questions)
"""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.limits import Limits
from agent.llm import OpenAILLM
from agent.orchestrator import ConversationState, Orchestrator, TurnResult
from services.db import db_path


def format_structured(r: TurnResult) -> list[str]:
    """The checked parts of a submit_answer: candidates, figures with their verification status, caveats."""
    a, out = r.submitted, []
    if a is None:
        return out
    for c in a.candidates:
        out.append(f"  Candidate {c.candidate_id} ({c.role_id}): {c.rationale}"
                   + (f"  Gaps: {'; '.join(c.gaps)}" if c.gaps else "") + f"  Evidence: {', '.join(c.evidence)}")
    for f in (r.report.figures if r.report else []):
        src = f"{f.source_ref} (cited as {f.evidence})" if f.relabelled else f.evidence
        out.append(f"  [{'verified' if f.verified else 'UNVERIFIED'}] {f.label} = {f.value:g}  ({src} {f.field_path})"
                   + ("" if f.verified else f"  -> {f.reason}"))
    out += [f"  Uncertainty: {u}" for u in a.uncertainties] + [f"  Content warning (from the model): {w}" for w in a.content_warnings]
    return ["-- Checked answer details", *out] if out else []


def format_result(r: TurnResult, show_trace: bool = True) -> str:
    out = [r.answer.strip(), ""]
    out += format_structured(r)
    if r.report is not None and not r.report.machine_checked:
        out.append("! Figures in this answer were not machine-checked (the model answered in plain text).")
    if r.report is not None and (r.report.bad_citations or r.report.unseen_candidates or r.report.not_retrieved
                                 or r.report.unverified_figures):
        out.append("! Some claims could not be verified against the tool results and are marked as unverified above.")
    if r.repaired:
        out.append(f"(the answer was corrected once after validation: {', '.join(r.repaired)})")
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
            orchestrator = Orchestrator(OpenAILLM(), db_path=a.db, limits=limits)
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
