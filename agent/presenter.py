"""Turns a TurnResult into the "view" the page renders. Pure functions: no Streamlit, no I/O.

  default view   the answer in recruiter language, a flag block if a person the answer is about had instruction-like text,
                 limit/error notices, and one trust line
  evidence       one card per person the answer ranks or is about, all in the same shape (agent/cards.py)
  technical      one block per model round: tokens, tools called and what came back, the answer's validation (developer view)

One set decides who the answer is about (validation's report.candidates: ranked, declared, then named in the prose), and that same
set drives the flag block and the evidence cards, so a flagged-text panel never appears without the notice that explains it.
Everything is derived from application state, never from the model's description of its own work.
"""
import json

from agent.cards import candidate_card
from agent.humanize import humanize, redact_flagged
from agent.tool_specs import ARG_MODELS

LIMIT_TEXT = {"round_limit": "ran out of steps", "call_budget_exceeded": "reached its lookup limit", "turn_timeout": "ran out of time"}
REPAIR_LABELS = {"unseen_candidate": "a candidate not in any record", "candidate_not_retrieved": "a ranked candidate whose record had not been read",
                 "unknown_role": "a role not in any record", "unsupported_number": "a number that was not in the data",
                 "wrong_target": "a target attributed to the wrong clock"}
MAX_CARDS_RANKED, MAX_CARDS_OTHER = 5, 3
TOOL_LABELS = {"list_roles": "Looking up roles", "search_candidates": "Searching candidates",
               "get_candidates": "Reading candidate records", "get_candidate_communications": "Reading candidate emails",
               "get_pipeline_analytics": "Checking pipeline data", "get_tasks": "Reviewing tasks", "get_recruiting_overview": "Checking what needs attention"}


def progress_label(event: dict) -> str:
    """What the assistant is doing right now, from real orchestrator events (never invented narration)."""
    if event.get("type") == "tools":
        return " and ".join(dict.fromkeys(TOOL_LABELS.get(n, "Looking things up") for n in event.get("names", []))) or "Looking things up"
    return "Working out the answer"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _possessive(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def _name(result, cid: str) -> str:
    return result.evidence.display_name(cid) or "a candidate"


def _subjects(result) -> list[str]:
    """Candidate IDs the answer is about, in order (ranked first)."""
    return list(result.report.candidates) if result.report else []


def flag_blocks(result) -> list[dict]:
    """One titled block per person the answer is about whose CV, email or note contained instruction-like text: what it tried to
    do, that it was ignored, and the original text for review. Nothing for people the answer is not about (developer view only)."""
    about = set(_subjects(result))
    out: dict[str, dict] = {}
    for s in result.flag_sources:
        if s["candidate_id"] not in about:
            continue
        name = _name(result, s["candidate_id"])
        b = out.setdefault(s["candidate_id"], {"candidate": name, "sources": [], "intents": []})
        b["sources"].append({"label": s["field_label"], "location": s["location"], "excerpt": s["excerpt"]})
        b["intents"] += [i for i in s["intents"] if i not in b["intents"]]
    for b in out.values():
        where = _join(list(dict.fromkeys(src["label"] for src in b["sources"])))
        b["title"] = f"Instruction-like text found in {_possessive(b['candidate'])} {where}"
        b["text"] = ((f"The text {_join(b['intents'])}. " if b["intents"] else "") +
                     "The assistant treated it as candidate data and ignored it; it did not affect this answer.")
    return list(out.values())


def notices(result) -> list[dict]:
    out = []
    if result.status == "error":
        out.append({"level": "error", "text": result.answer})
    if result.status == "limit_reached":
        out.append({"level": "warning", "text": f"The assistant {LIMIT_TEXT.get(result.limit_code, 'hit a limit')} before finishing, "
                                                "so this answer may be incomplete."})
    return out


def trust(result) -> dict:
    r = result.report
    if r is None or result.status == "error":
        return {"state": "none", "text": ""}
    if not r.ok:
        return {"state": "partial", "text": "Some details in this answer could not be matched to the records."}
    return {"state": "checked", "text": "Checked against the source records."}


def evidence_cards(result) -> list[dict]:
    """Cards for a ranking (in its order), else for the first few people the answer names whose records were read. Answers built
    from tasks or the overview (no records read) get no cards: the answer itself is the list."""
    ev, sub = result.evidence, result.submitted
    ranked = {r.candidate_id: r for r in (sub.ranking if sub else [])}
    if ranked:
        people = [c for c in ranked if ev.record(c)][:MAX_CARDS_RANKED]
    else:                      # no declared ranking: the first few people whose records were read, in the order the answer names them
        named, _ = ev.mentioned_in(result.answer)
        people = [c for c in dict.fromkeys(named + _subjects(result)) if ev.record(c)][:MAX_CARDS_OTHER]
    return [candidate_card(ev.record(c), _name(result, c), ev.role_title(ev.candidate_role(c) or ""),
                           ranked[c].reason if c in ranked else None, ranked[c].gaps if c in ranked else None) for c in people]


ROUND_ACTIONS = {"submit": "Submitted the answer", "plain_text": "Answered in plain text", "empty": "Empty reply",
                 "wrap_up": "Limit reached: answered without tools"}
SHOWN_IDS = 8


def _arguments(e) -> str:
    """The arguments the model chose, minus untouched defaults, e.g. role="Backend Engineer"."""
    defaults = {k: f.default for k, f in ARG_MODELS[e.tool_name].model_fields.items()} if e.tool_name in ARG_MODELS else {}
    shown = {k: v for k, v in e.safe_arguments.items() if v is not None and v != defaults.get(k, object())}
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in shown.items())


def _size(chars: int) -> str:
    return f"{chars / 1000:.1f}k chars" if chars >= 1000 else f"{chars} chars"


RECORD_KINDS = {"cv": ("candidate record", "candidate records"), "search": ("candidate row", "candidate rows"),
                "feedback": ("feedback entry", "feedback entries"), "stage": ("stage event", "stage events"), "task": ("task", "tasks"),
                "email": ("email", "emails"), "role": ("role", "roles"), "analytics": ("pipeline snapshot", "pipeline snapshots")}


def _records(refs: list[str]) -> list[str]:
    """Record IDs grouped by kind, e.g. "8 candidate records: C104, C108, ..." then "18 feedback entries: FB-013, ...". """
    groups: dict[str, list[str]] = {}
    for r in refs:
        kind, _, ident = r.partition(":")
        groups.setdefault(kind, []).append(ident)
    out = []
    for kind in [k for k in RECORD_KINDS if k in groups] + [k for k in groups if k not in RECORD_KINDS]:
        ids = groups[kind]
        one, many = RECORD_KINDS.get(kind, (kind, kind))
        out.append(f"{len(ids)} {one if len(ids) == 1 else many}: " + ", ".join(ids[:SHOWN_IDS]) + (f" +{len(ids) - SHOWN_IDS} more" if len(ids) > SHOWN_IDS else ""))
    return out


def _call_view(e) -> dict:
    s = e.result_summary
    if e.status != "success":
        returned = f"failed: {e.error_code or e.status}"
    else:
        whole_role = e.tool_name == "get_candidates" and e.safe_arguments.get("role")
        counts = ", ".join(f"{n} {'active candidates' if whole_role and k == 'candidates' else k.replace('_', ' ')}"
                           for k, n in s.get("counts", {}).items())
        returned = " · ".join(p for p in (counts or "returned", _size(e.result_chars), f"{e.duration_ms} ms") if p)
    notes = []
    if s.get("omitted_ids"):
        notes.append("cut to fit the size limit: " + ", ".join(s["omitted_ids"]))
    if s.get("content_flags"):
        notes.append("instruction-like text flagged in " + ", ".join(sorted({f["candidate_id"] for f in s["content_flags"]})))
    return {"call": f"{e.tool_name}({_arguments(e)})", "ok": e.status == "success", "returned": returned,
            "records": _records(e.evidence_refs), "notes": notes}


def technical_view(result) -> dict:
    """How the answer was produced, one block per model round: what that round cost, the tools it called (what each returned,
    how big it was) or the answer it submitted and whether validation accepted it. Built from the trace, never from the model."""
    t = result.trace.totals()
    calls: dict[int, list] = {}
    for e in result.trace.ordered():
        calls.setdefault(e.round, []).append(_call_view(e))
    rounds = []
    for r in result.trace.rounds:
        did = ROUND_ACTIONS.get(r.action, f"Called {len(calls.get(r.round, []))} tool(s)")
        rounds.append({"round": r.round, "tokens_in": r.prompt_tokens, "tokens_out": r.completion_tokens, "reasoning": r.reasoning_tokens,
                       "seconds": r.duration_ms / 1000,
                       "action": did, "outcome": r.outcome, "problems": r.problems, "calls": calls.get(r.round, [])})
    tokens = result.usage["prompt_tokens"] + result.usage["completion_tokens"]
    return {"title": f"How this answer was made · {t['model_rounds']} round(s) · {t['tool_calls']} tool call(s) · {tokens:,} tokens · "
                     f"{t['total_ms'] / 1000:.1f}s",
            "rounds": rounds, "model_rounds": t["model_rounds"], "tokens": tokens, "refs": result.evidence.refs(),
            "validation": result.report.problems() if result.report else [], "empty_retries": result.empty_retries,
            "corrected": [REPAIR_LABELS.get(c, c) for c in result.repaired], "plain_text_answer": bool(result.report and not result.report.structured),
            "about": ([result.evidence.display_name(c) or c for c in result.report.candidates] +
                      [result.evidence.role_title(r) or r for r in result.report.roles]) if result.report else [],
            "flags_not_shown": sorted({f["candidate_id"] for f in result.content_flags} - set(_subjects(result)))}


def trace_lines(view: dict) -> list[str]:
    """The technical view as plain text lines (CLI and logs)."""
    out = [view["title"]]
    for r in view["rounds"]:
        out.append(f"Round {r['round']} · {r['tokens_in']:,} in / {r['tokens_out']:,} out ({r['reasoning']:,} reasoning) tokens · {r['seconds']:.1f}s · {r['action']}"
                   + (f" -> {r['outcome']}" if r["outcome"] else ""))
        for c in r["calls"]:
            out.append(f"   {'' if c['ok'] else '! '}{c['call']} -> {c['returned']}")
            out += [f"     {n}" for n in c["records"] + c["notes"]]
        out += [f"   - {p}" for p in r["problems"]]
    return out


def build_view(result) -> dict:
    return {"status": result.status, "answer": redact_flagged(humanize(result.answer, result.evidence), result.flag_sources),
            "notices": notices(result), "flags": flag_blocks(result), "trust": trust(result),
            "evidence": evidence_cards(result), "technical": technical_view(result)}


def primary_text(view: dict) -> str:
    """Everything visible by default, as one string (for budgets and leak checks in tests and evals)."""
    return "\n".join([n["text"] for n in view["notices"]] + [f"{b['title']}. {b['text']}" for b in view["flags"]]
                     + [view["answer"], view["trust"]["text"]]).strip()
