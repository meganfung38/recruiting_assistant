"""Turns a TurnResult into the "view" the page renders, in three tiers. Pure functions: no Streamlit, no I/O.

  default view   the answer in recruiter language, plus notices that matter and one trust line
  evidence       only what the answer does not already say: interview notes and emails it relied on, grouped by person
  technical      raw IDs, validation outcome, tool calls, tokens (shown only when switched on)

Everything is derived from deterministic application state, never from the model's description of its own work. Original flagged
candidate text appears only in `flagged`, for a human reviewer, and is never given to the model or the trace.
"""
from agent.evidence import extract_citations
from agent.humanize import humanize, redact_flagged
from services.content_flags import INTENTS

# Only sources that add something a recruiter can check beyond the answer: what interviewers wrote and what was emailed. Tasks, roles, stage
# history, search rows and pipeline numbers just repeat the answer (their detail is in the technical tier).
EVIDENCE_KINDS = ("feedback", "email")
ONE_LINER_WORDS = 40
LIMIT_TEXT = {"round_limit": "ran out of steps", "call_budget_exceeded": "reached its lookup limit", "turn_timeout": "ran out of time"}
REPAIR_LABELS = {"unsupported_citation": "a reference that was not in the records", "unverified_figure": "a figure that did not match the data",
                 "unseen_candidate": "a candidate not in any record", "candidate_not_retrieved": "a candidate whose record had not been opened",
                 "undisclosed_incomplete": "an undisclosed incomplete comparison", "unretrieved_available": "candidates that could still be looked up"}
MAX_EVIDENCE_SHOWN = 12
TOOL_LABELS = {"list_roles": "Looking up roles", "get_role": "Checking the role's requirements", "search_candidates": "Searching candidates",
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


def cited_refs(result) -> set[str]:
    """Citations the answer actually relies on: valid inline citations, candidate evidence, and figure sources."""
    refs = set(extract_citations(result.answer))
    if result.submitted:
        refs |= {e for c in result.submitted.candidates for e in c.evidence} | {f.evidence for f in result.submitted.figures}
    if result.report:
        refs |= {f.source_ref for f in result.report.figures if f.source_ref}
    return refs


def _name(result, cid: str) -> str:
    return result.evidence.display_name(cid) or "a candidate"


def _mentioned_candidates(result) -> set[str]:
    """Candidates the answer is about: the ones it lists. Only a plain-text answer (nothing listed) falls back to names in its text, so a
    side remark about a flagged person does not make the notice lead an unrelated answer."""
    if result.submitted and result.submitted.candidates:
        return {c.candidate_id for c in result.submitted.candidates}
    ids: set[str] = set()
    low = result.answer.lower()
    for cid in result.evidence.candidate_ids():
        name = result.evidence.candidate_name(cid)
        if cid in result.answer or (name and name.lower() in low):
            ids.add(cid)
    return ids


def flagged_view(result) -> list[dict]:
    """What a reviewer may read: the original flagged text with its source and what it tried to do (plain words, no detector labels)."""
    return [{"candidate": _name(result, s["candidate_id"]), "candidate_id": s["candidate_id"], "source": s["field_label"], "location": s["location"],
             "excerpt": s["excerpt"], "intents": s["intents"]} for s in result.flag_sources]


def flag_notices(result) -> list[dict]:
    """One plain-language notice. It leads the answer when the flagged person is part of it, otherwise it is a quiet footnote."""
    if not result.flag_sources:
        return []
    relevant = _mentioned_candidates(result)
    groups: dict[bool, list[dict]] = {True: [], False: []}
    for s in result.flag_sources:
        groups[s["candidate_id"] in relevant].append(s)
    out = []
    for is_relevant, sources in groups.items():
        if not sources:
            continue
        subjects = _join(list(dict.fromkeys(f"{_possessive(_name(result, s['candidate_id']))} {s['field_label']}" for s in sources)))
        asked = _join(list(dict.fromkeys(i for s in sources for i in s["intents"])))
        what = f" The text {asked}." if asked else ""
        text = (f"Potentially manipulative instructions were found in {subjects}.{what} They were ignored in this evaluation." if is_relevant else
                f"While looking things up, the assistant came across potentially manipulative instructions in {subjects}.{what} "
                "They were ignored and did not affect this answer.")
        out.append({"level": "warning" if is_relevant else "info", "text": text, "placement": "top" if is_relevant else "foot", "has_source": True})
    return out


def notices(result) -> list[dict]:
    out = []
    if result.status == "error":
        out.append({"level": "error", "text": result.answer, "placement": "top"})
    if result.status == "limit_reached":
        out.append({"level": "warning", "text": f"The assistant {LIMIT_TEXT.get(result.limit_code, 'hit a limit')} before finishing, so this answer may be incomplete.",
                    "placement": "top"})
    if result.unresolved_candidates:
        who = _join([_name(result, c) for c in result.unresolved_candidates])
        out.append({"level": "warning", "placement": "top",
                    "text": f"Not everyone was reviewed: {who} could not be looked up in full, so this answer may not cover "
                            "them."})
    return out + flag_notices(result)


def unverified_sentences(result) -> list[str]:
    """Only claims about a person: a derived count or a pipeline figure has no card to check, so it is not flagged here."""
    r = result.report
    if r is None:
        return []
    out = [f"{_name(result, c)} was named in the answer but could not be found in the records." for c in r.unseen_candidates]
    return out + [f"{_name(result, c)}'s full record was not opened, so they were judged only from search results." for c in r.not_retrieved]


def trust(result) -> dict:
    r = result.report
    if r is None or result.status == "error":
        return {"state": "none", "text": ""}
    if not r.ok and (r.bad_citations or r.unseen_candidates or r.not_retrieved or r.unverified_figures):
        return {"state": "partial", "text": "Some details in this answer could not be verified against the records."}
    return {"state": "checked", "text": "Checked against the source records."} if r.machine_checked else {"state": "none", "text": ""}


def candidate_rows(result) -> list[dict]:
    rows = []
    for c in (result.submitted.candidates if result.submitted else []):
        titles = [card["title"] for ref in c.evidence if (card := result.evidence.card(ref))]
        rows.append({"name": _name(result, c.candidate_id), "role": result.evidence.role_title(c.role_id) or "", "why": humanize(c.rationale, result.evidence),
                     "gaps": [humanize(g, result.evidence) for g in c.gaps], "sources": titles})
    return rows


def evidence_groups(result) -> list[dict]:
    """Cited interview notes and emails, grouped by person. Nothing for a one-line answer, or when no such source was relied on."""
    if "\n" not in result.answer.strip() and len(result.answer.split()) < ONE_LINER_WORDS:
        return []
    cited, groups, shown = cited_refs(result), {}, 0
    for ref in result.evidence.refs():
        card = result.evidence.card(ref)
        if ref.partition(":")[0] not in EVIDENCE_KINDS or ref not in cited or not card or shown >= MAX_EVIDENCE_SHOWN:
            continue
        person = result.evidence.display_name(card.get("who_id") or "") or "Other"
        groups.setdefault(person, []).append({k: card[k] for k in ("title", "lines", "quote")})   # no ref / ID keys in the default tiers
        shown += 1
    return [{"person": p, "cards": cards} for p, cards in groups.items()]


def technical_view(result) -> dict:
    """A round-by-round timeline: each model round, the data tools it called (with what came back), or the answer it submitted."""
    t = result.trace.totals()
    rows, by_round = [], {}
    for e in result.trace.ordered():
        by_round.setdefault(e.round, []).append(e)
    for rnd in range(1, max(t["model_rounds"], max(by_round, default=0)) + 1):
        events = by_round.get(rnd, [])
        if not events:
            last = rnd == t["model_rounds"]
            rows.append({"Round": rnd, "Step": "Answer submitted" if last else "Answer submitted and sent back for correction (or no tool call)",
                         "Details": "", "Returned": "", "ms": ""})
        for e in events:
            args = ", ".join(f"{k}={v}" for k, v in e.safe_arguments.items() if v is not None)
            counts = ", ".join(f"{k}: {v}" for k, v in e.result_summary.get("counts", {}).items())
            omitted = e.result_summary.get("omitted_ids", [])
            flags = sorted({f["pattern_id"] for f in e.result_summary.get("content_flags", [])})
            returned = counts or (e.error_code or "")
            rows.append({"Round": rnd, "Step": e.tool_name + ("" if e.status == "success" else f" ({e.status})"), "Details": args,
                         "Returned": returned + (f"; omitted by size cap: {', '.join(omitted)}" if omitted else "") + (f"; flagged: {', '.join(flags)}" if flags else ""),
                         "ms": e.duration_ms})
    return {"title": f"Tools used ({t['tool_calls']}) · {t['total_ms'] / 1000:.1f}s", "rows": rows, "model_rounds": t["model_rounds"],
            "tokens": result.usage["prompt_tokens"] + result.usage["completion_tokens"], "refs": result.evidence.refs(), "records_received": len(result.evidence.refs()), "candidates_seen": len(result.evidence.candidate_ids()),
            "validation": result.report.problems() if result.report else [], "empty_retries": result.empty_retries,
            "corrected": [REPAIR_LABELS.get(c, c) for c in result.repaired], "plain_text_answer": bool(result.report and not result.report.machine_checked)}


def build_view(result) -> dict:
    return {"status": result.status, "answer": redact_flagged(humanize(result.answer, result.evidence), result.flag_sources), "notices": notices(result), "trust": trust(result),
            "details": {"evidence": evidence_groups(result), "unverified": unverified_sentences(result)},
            "flagged": flagged_view(result), "technical": technical_view(result)}


def primary_text(view: dict) -> str:
    """Everything visible by default, as one string (for budgets and leak checks in tests and evals)."""
    return "\n".join([n["text"] for n in view["notices"] if n["placement"] == "top"] + [view["answer"], view["trust"]["text"]]).strip()
