"""Model-dependent evals. Opt-in: without OPENAI_API_KEY this prints a notice and exits 0.

    python -m evals.run_evals                      # cases 10, 14, 15
    python -m evals.run_evals --cases 10 --runs 3  # fewer runs
Writes docs/eval_results.md (generated sections only; hand-written notes are preserved) and raw runs to docs/eval_runs/.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agent.limits import Limits
from agent.llm import LLMUnavailable, OpenAILLM
from agent.orchestrator import Orchestrator
from data.seed import build
from evals import report
from evals.checks import CHECKS, summarize_injection

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = "10,14,15"


def load_cases(path: Path = ROOT / "evals" / "cases.yaml") -> dict[int, dict]:
    return {c["id"]: c for c in yaml.safe_load(path.read_text())}


def limits_for(case: dict) -> Limits:
    return Limits(**{k: v for k, v in case.get("limits", {}).items()})


def record_from_result(case: dict, condition: str, run: int, result, seconds: float) -> dict:
    """Everything the checkers need, as plain JSON-able data."""
    a = result.submitted
    return {
        "case": case["id"], "condition": condition, "run": run, "status": result.status, "error_code": result.error_code,
        "limit_code": result.limit_code, "repaired": list(result.repaired), "structured": a is not None,
        "answer_markdown": result.answer, "candidates": [c.model_dump() for c in a.candidates] if a else None,
        "uncertainties": list(a.uncertainties) if a else [], "content_warnings": list(a.content_warnings) if a else [],
        "evidence_refs": result.evidence.refs(), "retrieved_candidates": result.evidence.retrieved_candidates(), "tool_sequence": [e.tool_name for e in result.trace.ordered()],
        "trace": [e.model_dump() for e in result.trace.ordered()], "content_flags": result.content_flags,
        "unresolved": result.unresolved_candidates, "unverified_figures": len(result.report.unverified_figures) if result.report else 0,
        "model_rounds": result.trace.model_rounds, "usage": result.usage, "seconds": round(seconds, 1)}


def default_factory(temperature):
    def make(case: dict, db: Path) -> Orchestrator:
        return Orchestrator(OpenAILLM(temperature=temperature), db_path=db, limits=limits_for(case))
    return make


def one_run(make, case, condition, db, run) -> dict:
    t0 = time.monotonic()
    result = make(case, db).run_turn(case["prompt"], role_context=case.get("role_context"))
    return record_from_result(case, condition, run, result, time.monotonic() - t0)


def truncation_verdict(case: dict, records: list[dict]) -> tuple[list[dict], dict]:
    check, per = CHECKS[case["checks"][0]], []
    for r in records:
        if r["status"] == "error":
            per.append({"status": "error", "reasons": [r["error_code"]]})
            continue
        answer = {"structured": r["structured"], "candidates": r["candidates"] or [], "uncertainties": r["uncertainties"],
                  "content_warnings": r["content_warnings"], "answer_markdown": r["answer_markdown"]}
        per.append(check(r["trace"], answer))
    counts = {k: sum(p["status"] == k for p in per) for k in ("pass", "fail", "not_exercised", "error")}
    exercised = counts["pass"] + counts["fail"]
    verdict = "inconclusive" if exercised < case["min_exercised"] else ("pass" if counts["fail"] == 0 else "fail")
    return per, {"verdict": verdict, "exercised": exercised, "counts": counts}


def run_cases(cases: list[dict], dbs: dict, make, workers: int = 3, log=print) -> dict:
    jobs = []
    for c in cases:
        for cond in (["baseline", "injected"] if c.get("paired") else [c["db_variant"]]):
            jobs += [(c, cond, dbs[cond], n) for n in range(c["runs"])]
    log(f"running {len(jobs)} turns with {workers} workers")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        recs = list(pool.map(lambda j: one_run(make, *j), jobs))
    out = {}
    for c in cases:
        mine = [r for r in recs if r["case"] == c["id"]]
        if c.get("paired"):
            out[c["id"]] = {"records": mine, "summary": summarize_injection(
                [r for r in mine if r["condition"] == "baseline"], [r for r in mine if r["condition"] == "injected"],
                c.get("min_surfaced", 0.8))}
        else:
            per, verdict = truncation_verdict(c, mine)
            out[c["id"]] = {"records": mine, "per_run": per, "verdict": verdict}
    return out


def probe_temperature(llm_cls=OpenAILLM, value: float = 0.2):
    """Low temperature if the model accepts it (some reasoning models reject the parameter)."""
    import asyncio

    async def go():
        llm = llm_cls(temperature=value)
        try:
            await llm.complete([{"role": "user", "content": "Reply with: ok"}])
        finally:
            await llm.aclose()
    try:
        asyncio.run(go())
        return value
    except LLMUnavailable:
        return None


def main(argv=None, make=None, dbs=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.run_evals")
    ap.add_argument("--cases", default=DEFAULT_CASES)
    ap.add_argument("--runs", type=int, help="override runs per condition")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "docs" / "eval_results.md"))
    ap.add_argument("--raw-dir", default=str(ROOT / "docs" / "eval_runs"))
    a = ap.parse_args(argv)
    load_dotenv()
    if make is None and not os.environ.get("OPENAI_API_KEY"):
        print("Skipping model-dependent evals: OPENAI_API_KEY is not set (this is opt-in).")
        return 0
    all_cases = load_cases()
    chosen = [all_cases[int(i)] for i in a.cases.split(",")]
    if a.runs:
        chosen = [{**c, "runs": a.runs, "min_exercised": min(c.get("min_exercised", a.runs), a.runs)} for c in chosen]
    temperature = None
    if make is None:
        temperature = probe_temperature()
        make = default_factory(temperature)
    if dbs is None:
        tmp = Path(tempfile.mkdtemp(prefix="evals-"))
        dbs = {v: build(tmp / f"{v}.db", v) for v in ("baseline", "injected")}
    results = run_cases(chosen, dbs, make, a.workers)
    sections, now = {}, datetime.now(timezone.utc)
    for c in chosen:
        r = results[c["id"]]
        sections[c["id"]] = (report.render_injection(r["summary"]) if c.get("paired")
                             else report.render_truncation(c, r["per_run"], r["verdict"]))
    model = os.environ.get("OPENAI_MODEL", "?")
    info = (f"**Last run:** {now:%Y-%m-%d %H:%M} UTC · model `{model}` · temperature "
            f"{temperature if temperature is not None else 'model default (parameter not sent or not accepted)'} · "
            f"cases {a.cases} · dataset as-of 2026-11-07 (fictional)")
    report.write_report(Path(a.out), info, sections)
    raw = Path(a.raw_dir)
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"{now:%Y%m%d-%H%M%S}.json").write_text(json.dumps(
        {"model": model, "temperature": temperature, "cases": a.cases, "results": results}, indent=1, default=str))
    for c in chosen:
        r = results[c["id"]]
        v = r["summary"]["verdict"] if c.get("paired") else r["verdict"]["verdict"]
        print(f"case {c['id']} ({c['name']}): {str(v).upper()}")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
