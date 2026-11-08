"""Model-dependent evals. Opt-in: without OPENAI_API_KEY this prints a notice and exits 0.

    python -m evals.run_evals                      # every case
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
from agent.orchestrator import ConversationState, Orchestrator
from data.seed import build
from evals import report
from evals.checks import SCENARIO_CHECKS, summarize_injection

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = "1,2,3,4,5,6,7,8,9,10,11,12,13"


def load_cases(path: Path = ROOT / "evals" / "cases.yaml") -> dict[int, dict]:
    return {c["id"]: c for c in yaml.safe_load(path.read_text())}


def limits_for(case: dict) -> Limits:
    return Limits(**{k: v for k, v in case.get("limits", {}).items()})


def record_from_result(case: dict, condition: str, run: int, result, seconds: float) -> dict:
    """Everything the checkers need, as plain JSON-able data."""
    a = result.submitted
    return {
        "case": case["id"], "condition": condition, "run": run, "status": result.status, "error_code": result.error_code,
        "limit_code": result.limit_code, "repaired": list(result.repaired), "empty_retries": result.empty_retries, "flag_source_count": len(result.flag_sources), "structured": a is not None,
        "answer_markdown": result.answer, "ranking": [x.model_dump() for x in a.ranking] if a else [],
        "about": list(result.report.candidates) if result.report else [],
        "uncertainties": list(a.uncertainties) if a else [],
        "evidence_refs": result.evidence.refs(), "retrieved_candidates": result.evidence.retrieved_candidates(), "tool_sequence": [e.tool_name for e in result.trace.ordered()],
        "trace": [e.model_dump() for e in result.trace.ordered()], "content_flags": result.content_flags,
        "unsupported_numbers": len(result.report.unsupported_numbers) if result.report else 0,
        "model_rounds": result.trace.model_rounds, "usage": result.usage, "seconds": round(seconds, 1)}


def default_factory(temperature):
    def make(case: dict, db: Path) -> Orchestrator:
        return Orchestrator(OpenAILLM(temperature=temperature), db_path=db, limits=limits_for(case))
    return make


def one_run(make, case, condition, db, run) -> dict:
    t0 = time.monotonic()
    state = ConversationState()
    for earlier in case.get("setup", []):                     # prior turns of the same conversation (not scored)
        make(case, db).run_turn(earlier, state, role_context=case.get("role_context"))
    result = make(case, db).run_turn(case["prompt"], state, role_context=case.get("role_context"))
    return record_from_result(case, condition, run, result, time.monotonic() - t0)


def scenario_verdict(case: dict, records: list[dict]) -> dict:
    check = SCENARIO_CHECKS[case["check"]]
    per = [{"status": "error", "reasons": [r["error_code"]]} if r["status"] == "error" else
           {"status": "fail" if (why := check(r)) else "pass", "reasons": why} for r in records]
    passed = sum(p["status"] == "pass" for p in per)
    return {"verdict": "pass" if passed == len(per) else "fail", "passed": passed, "runs": len(per), "per_run": per,
            "rounds": [r["model_rounds"] for r in records], "tokens": [r["usage"]["prompt_tokens"] + r["usage"]["completion_tokens"] for r in records]}


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
                [r for r in mine if r["condition"] == "baseline"], [r for r in mine if r["condition"] == "injected"])}
        else:
            out[c["id"]] = {"records": mine, "verdict": scenario_verdict(c, mine)}
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
        chosen = [{**c, "runs": a.runs} for c in chosen]
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
        sections[c["id"]] = report.render_injection(r["summary"]) if c.get("paired") else report.render_scenario(c, r["verdict"])
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
