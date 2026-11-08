"""Per-turn evidence registry and answer validation.

The registry holds the record IDs and aggregate values the model actually received. It is built only from
structured fields (`source_refs`, IDs under allowlisted keys) of SUCCESSFUL calls, never by parsing free text,
so candidate-supplied text cannot create entries.

Validation is deterministic and checks PRESENCE and EXACTNESS, not meaning: a citation that exists can still be a
poor reason for a claim. Semantic correctness is covered by the model-dependent evals.
"""
import math
import re
from dataclasses import dataclass, field
from typing import Any

from agent import cards as card_builder

CITATION_RE = re.compile(r"^(cv:C\d{3}|search:C\d{3}|feedback:FB-\d{3}|stage:EV-\d{3}|email:EM-\d{3}|task:T-\d{3}|role:ROLE-[A-Z]{2,3}"
                         r"|analytics:ROLE-[A-Z]{2,3}@\d{4}-\d{2}-\d{2})$")


def _candidate_ids(node) -> set[str]:
    """IDs under 'candidate_id' keys, skipping not-found stubs and never descending into untrusted text."""
    found: set[str] = set()
    if isinstance(node, dict):
        if "untrusted_text" in node or node.get("not_found"):
            return found
        for k, v in node.items():
            if k == "candidate_id" and isinstance(v, str) and re.fullmatch(r"C\d{3}", v):
                found.add(v)
            else:
                found |= _candidate_ids(v)
    elif isinstance(node, list):
        for v in node:
            found |= _candidate_ids(v)
    return found


def _names(node) -> dict[str, str]:
    """{candidate_id: name} for plain-string names under allowlisted fields (a wrapped/untrusted name is skipped)."""
    out: dict[str, str] = {}
    if isinstance(node, dict):
        if "untrusted_text" in node or node.get("not_found"):
            return out
        nm = node.get("name") if isinstance(node.get("name"), str) else node.get("candidate_name")
        if isinstance(node.get("candidate_id"), str) and isinstance(nm, str):
            out[node["candidate_id"]] = nm
        for v in node.values():
            out.update(_names(v))
    elif isinstance(node, list):
        for v in node:
            out.update(_names(v))
    return out


def _roles(node) -> dict[str, str]:
    """{role_id: title} wherever a result carries both (list_roles, search rows, records, analytics)."""
    out: dict[str, str] = {}
    if isinstance(node, dict):
        if isinstance(node.get("role_id"), str) and isinstance(node.get("title") or node.get("role_title"), str):
            out[node["role_id"]] = node.get("title") or node["role_title"]
        for v in node.values():
            out.update(_roles(v))
    elif isinstance(node, list):
        for v in node:
            out.update(_roles(v))
    return out


def _candidate_roles(node, inherited: str | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(node, dict):
        role = node.get("role_id") if isinstance(node.get("role_id"), str) else inherited
        if isinstance(node.get("candidate_id"), str) and role:
            out[node["candidate_id"]] = role
        for v in node.values():
            out.update(_candidate_roles(v, role))
    elif isinstance(node, list):
        for v in node:
            out.update(_candidate_roles(v, inherited))
    return out


def aggregates(tool: str, raw_envelope: dict) -> dict[str, Any]:
    """Raw values kept (in memory only) so figures can be validated later: analytics, tasks, and per-candidate numbers.
    `cv:<ID>` holds record fields from get_candidates (feedback_count, waiting_days, days_in_current_stage, ...);
    `search:<ID>` holds a search row (fit_signals such as avg_criteria_score, waiting_days). CV text is not kept."""
    data = raw_envelope.get("data") or {}
    if tool == "get_pipeline_analytics":
        return {r: data for r in raw_envelope["source_refs"] if r.startswith("analytics:")}
    if tool == "get_tasks":                                       # each task, and the list's totals (overdue_total, pending_total)
        totals = {k: v for k, v in data.items() if k != "tasks"}
        return {**{f"task:{t['task_id']}": t for t in data.get("tasks", [])},
                **({"tasks:" + ",".join(sorted(t["task_id"] for t in data.get("tasks", []))): totals} if totals else {})}
    if tool == "get_recruiting_overview":                         # per-role counts citable as role:<ID>; waiting people as search:<ID>
        out = {f"role:{r['role_id']}": r for r in data.get("roles", [])}
        out.update({f"search:{w['candidate_id']}": {"waiting_days": w["waiting_days"]}
                    for g in ("waiting_to_be_scheduled", "interview_booked_not_held", "waiting_for_feedback") for w in data.get(g, [])})
        return out
    if tool == "list_roles":                                      # a role's age and open date, so "open for N days" can be verified
        return {f"role:{r['role_id']}": {k: v for k, v in r.items() if k in ("role_id", "title", "opened_at", "role_age_days")}
                for r in data.get("roles", []) if isinstance(r, dict) and r.get("role_id")}
    if tool == "get_candidates":                                  # each record, and the pending tasks inside it
        found = [c for c in data.get("candidates", []) if not c.get("not_found")]
        return {**{f"cv:{c['candidate_id']}": {k: v for k, v in c.items() if k != "cv_text"} for c in found},
                **{f"task:{t['task_id']}": t for c in found for t in c.get("pending_tasks", [])}}
    if tool == "search_candidates":
        return {f"search:{c['candidate_id']}": {"fit_signals": c["fit_signals"], "waiting_days": c["waiting_days"],
                                           "days_in_current_stage": c["days_in_current_stage"]}
                for c in data.get("candidates", [])}
    return {}


def _shown_task_ids(env: dict) -> set[str]:
    return {r[5:] for r in env.get("source_refs", []) if r.startswith("task:")}


class EvidenceRegistry:
    def __init__(self) -> None:
        self._refs: dict[str, str] = {}   # citation ref -> tool that returned it
        self._candidates: set[str] = set()
        self._retrieved: set[str] = set()  # candidates whose full record came back from get_candidates
        self._raw: dict[str, Any] = {}
        self._names: dict[str, str] = {}
        self._cards: dict[str, dict] = {}     # ref -> plain-language evidence card (see agent/cards.py)
        self._roles: dict[str, str] = {}      # role_id -> title, learned from any result that carries both
        self._cand_roles: dict[str, str] = {} # candidate_id -> role_id, to tell apart two candidates who share a name
        self._flagged: set[str] = set()   # candidates whose content a tool result flagged as instruction-like

    def add(self, tool: str, safe_envelope: dict, raw_aggregates: dict | None = None) -> None:
        """Register a successful call's evidence. Callers must not invoke this for failed calls."""
        for ref in safe_envelope["source_refs"]:
            if CITATION_RE.match(ref):
                self._refs.setdefault(ref, tool)
        self._candidates |= _candidate_ids(safe_envelope["data"])
        self._names.update(_names(safe_envelope["data"]))
        self._roles.update(_roles(safe_envelope["data"]))
        self._cand_roles.update(_candidate_roles(safe_envelope["data"]))
        self._cards.update({r: c for r, c in card_builder.build(tool, safe_envelope, self._names).items() if r in self._refs})
        self._flagged |= {f["candidate_id"] for f in safe_envelope.get("content_flags", []) if re.fullmatch(r"C\d{3}", str(f.get("candidate_id")))}
        if tool == "get_candidates":
            self._retrieved |= {c["candidate_id"] for c in (safe_envelope["data"] or {}).get("candidates", [])
                                if not c.get("not_found")}
        shown = _candidate_ids(safe_envelope["data"])          # keep raw values only for what the model was shown
        for ref, value in (raw_aggregates or {}).items():
            if ref in safe_envelope["source_refs"] or (ref.startswith(("cv:", "search:")) and ref.partition(":")[2] in shown) \
                    or (ref.startswith("tasks:") and set(ref[6:].split(",")) <= _shown_task_ids(safe_envelope)):
                if isinstance(value, dict) and isinstance(self._raw.get(ref), dict):
                    self._raw[ref] = {**self._raw[ref], **value}   # search signals + record fields for one candidate
                else:
                    self._raw[ref] = value

    def has(self, ref: str) -> bool:
        return ref in self._refs

    def refs(self) -> list[str]:
        return sorted(self._refs)

    def card(self, ref: str) -> dict | None:
        """The evidence card for `ref`, its title resolved with the best name known NOW (a later lookup may have taught us the name)."""
        c = self._cards.get(ref)
        if c is None or "{who}" not in c["title"]:
            return c
        return card_builder.resolve_title(c, self.display_name(c.get("who_id") or ""), parenthesised=c["kind"] == "task")

    def role_title(self, role_id: str) -> str | None:
        return self._roles.get(role_id)

    def display_name(self, candidate_id: str) -> str | None:
        """The name to show a recruiter. Only if two candidates share a name is anything added: their role, else (last resort) the ID."""
        name = self._names.get(candidate_id)
        twins = [c for c, n in self._names.items() if n == name and c != candidate_id] if name else []
        if not twins:
            return name
        role = self._cand_roles.get(candidate_id)
        same_role = any(self._cand_roles.get(c) == role for c in twins)
        return f"{name} ({candidate_id})" if same_role or not role else f"{name} ({self._roles.get(role, role)})"

    def candidate_name(self, candidate_id: str) -> str | None:
        return self._names.get(candidate_id)

    def flagged_candidates(self) -> list[str]:
        return sorted(self._flagged)

    def retrieved(self, candidate_id: str) -> bool:
        return candidate_id in self._retrieved

    def retrieved_candidates(self) -> list[str]:
        return sorted(self._retrieved)

    def candidate_seen(self, candidate_id: str) -> bool:
        return candidate_id in self._candidates

    def candidate_ids(self) -> set[str]:
        return set(self._candidates)

    def raw(self, ref: str) -> Any:
        return self._raw.get(ref)

    def raw_refs(self) -> list[str]:
        return sorted(self._raw)


# ----------------------------------------------------------------------------- answer validation
INLINE_CITATION = re.compile(r"\[(cv|search|feedback|stage|email|task|role|analytics):([^\]\s]{1,60})\]")
_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[([^\]]+)\])?$")
FLOAT_TOLERANCE = dict(rel_tol=1e-6, abs_tol=1e-4)


def extract_citations(markdown: str) -> list[str]:
    """Every inline citation-shaped token in the text, in order, de-duplicated. Malformed IDs are kept so they fail."""
    return list(dict.fromkeys(f"{kind}:{ident}" for kind, ident in INLINE_CITATION.findall(markdown)))


def resolve_path(data: Any, path: str) -> tuple[bool, Any]:
    """Follow e.g. 'stages[Technical Interview].scheduling.lag_median_scheduled_days' in a stored raw result.
    A [selector] is a list index or the 'stage' name of exactly one item."""
    node = data
    for seg in path.split("."):
        m = _SEGMENT.match(seg)
        if not m or not isinstance(node, dict) or m.group(1) not in node:
            return False, None
        node, sel = node[m.group(1)], m.group(2)
        if sel is None:
            continue
        if not isinstance(node, list):
            return False, None
        if sel.isdigit():
            if int(sel) >= len(node):
                return False, None
            node = node[int(sel)]
        else:
            hits = [x for x in node if isinstance(x, dict) and x.get("stage") == sel]
            if len(hits) != 1:
                return False, None
            node = hits[0]
    return True, node


@dataclass
class FigureResult:
    label: str
    value: float
    evidence: str
    field_path: str
    verified: bool
    reason: str | None = None
    source_ref: str | None = None      # where the number was actually found (may differ from the cited kind)
    relabelled: bool = False           # the model cited cv: but the number came from a search row (or the reverse)


@dataclass
class ValidationReport:
    machine_checked: bool = True                        # False for the plain-text fallback (figures not checked)
    bad_citations: list[str] = field(default_factory=list)
    citation_hints: dict[str, str] = field(default_factory=dict)   # ref -> how to cite it correctly
    unseen_candidates: list[str] = field(default_factory=list)
    not_retrieved: list[str] = field(default_factory=list)
    figures: list[FigureResult] = field(default_factory=list)

    @property
    def unverified_figures(self) -> list[FigureResult]:
        return [f for f in self.figures if not f.verified]

    @property
    def categories(self) -> list[str]:
        return [c for c, bad in (("unsupported_citation", self.bad_citations), ("unseen_candidate", self.unseen_candidates),
                                 ("candidate_not_retrieved", self.not_retrieved), ("unverified_figure", self.unverified_figures)) if bad]

    @property
    def ok(self) -> bool:
        return not self.categories

    def problems(self) -> list[str]:
        """Plain-language problems, for the repair message to the model (contains only IDs and numbers)."""
        out = [f"Citation not found in this turn's tool results: [{c}]" + (f". {self.citation_hints[c]}" if c in self.citation_hints else "")
               for c in self.bad_citations]
        out += [f"Candidate {c} did not appear in any tool result this turn." for c in self.unseen_candidates]
        out += [f"Candidate {c} is listed but its record was never retrieved with get_candidates; retrieve it or remove it from candidates."
                for c in self.not_retrieved]
        out += [f"Figure '{f.label}' ({f.value}) is unverified: {f.reason}" for f in self.unverified_figures]
        return out


def _number(found: bool, value) -> bool:
    return found and isinstance(value, (int, float)) and not isinstance(value, bool)


def check_figure(f, registry: "EvidenceRegistry") -> FigureResult:
    """Verify the number against the stored raw data at (evidence, field_path).

    Label slips are forgiven, wrong numbers are not. If the cited evidence does not contain that field at all (e.g. a per-role overdue
    count cited to a person's row), the field is looked up in this turn's other results and the figure is verified where the SAME value
    is found, reporting the true source. If the cited evidence does contain the field and the value differs, the figure is unverified:
    that is how a number carried over from another role (14 of 17 cited to the wrong role) is caught."""
    base = dict(label=f.label, value=f.value, evidence=f.evidence, field_path=f.field_path)
    raw = registry.raw(f.evidence)
    found, actual = resolve_path(raw, f.field_path) if raw is not None else (False, None)
    if found:
        if not _number(found, actual):
            return FigureResult(**base, verified=False, reason=f"the value at that path is not a number ({actual!r})")
        if not math.isclose(f.value, actual, **FLOAT_TOLERANCE):
            return FigureResult(**base, verified=False, reason=f"the data says {actual}, not {f.value}", source_ref=f.evidence)
        return FigureResult(**base, verified=True, source_ref=f.evidence)
    elsewhere = [(ref, v) for ref in registry.raw_refs() if ref != f.evidence
                 for ok, v in [resolve_path(registry.raw(ref), f.field_path)] if _number(ok, v)]
    same = [(ref, v) for ref, v in elsewhere if ref.partition(":")[2] == f.evidence.partition(":")[2]]
    if same:                                          # the same person or role has the field under another source (cv: vs search:)
        ref, v = same[0]
        if math.isclose(f.value, v, **FLOAT_TOLERANCE):
            return FigureResult(**base, verified=True, source_ref=ref, relabelled=True)
        return FigureResult(**base, verified=False, reason=f"the data says {v}, not {f.value}", source_ref=ref, relabelled=True)
    if not elsewhere:                                 # the path itself is malformed: look the field up by its own name
        leaf = _SEGMENT.match(f.field_path.split(".")[-1])
        elsewhere = [(ref, registry.raw(ref)[leaf.group(1)]) for ref in registry.raw_refs()
                     if leaf and isinstance(registry.raw(ref), dict) and _number(True, registry.raw(ref).get(leaf.group(1)))] if leaf else []
    match = next((ref for ref, v in elsewhere if math.isclose(f.value, v, **FLOAT_TOLERANCE)), None)
    if match:                                         # cited to an unrelated record (e.g. a role count cited to a person's row)
        return FigureResult(**base, verified=True, source_ref=match, relabelled=True)
    if raw is None:
        return FigureResult(**base, verified=False, reason=f"no stored result for evidence '{f.evidence}'")
    return FigureResult(**base, verified=False, reason=f"path '{f.field_path}' not found in {f.evidence}")


def _hints(bad: list[str], registry: "EvidenceRegistry") -> dict[str, str]:
    """cv: means the record was retrieved with get_candidates; search: means only a search row was seen."""
    out = {}
    for ref in bad:
        kind, _, ident = ref.partition(":")
        if kind == "cv" and registry.has(f"search:{ident}"):
            out[ref] = (f"You only saw {ident} in a search result, not their record: cite [search:{ident}] for what the search row "
                        f"showed, or retrieve them with get_candidates to cite [cv:{ident}]")
        elif kind == "search" and registry.has(f"cv:{ident}"):
            out[ref] = f"You retrieved {ident}'s record; cite [cv:{ident}] instead"
    return out


def validate_submission(answer, registry: "EvidenceRegistry") -> ValidationReport:
    """Check a SubmittedAnswer: every citation exists, every listed candidate's record was retrieved, every figure matches the data."""
    r = ValidationReport()
    cited = extract_citations(answer.answer_markdown) + [e for c in answer.candidates for e in c.evidence]
    r.bad_citations = [c for c in dict.fromkeys(cited) if not registry.has(c)]
    r.citation_hints = _hints(r.bad_citations, registry)
    for c in answer.candidates:
        if not registry.retrieved(c.candidate_id):
            (r.not_retrieved if registry.candidate_seen(c.candidate_id) else r.unseen_candidates).append(c.candidate_id)
    r.figures = [check_figure(f, registry) for f in answer.figures]
    return r


def validate_plain(text: str, registry: "EvidenceRegistry") -> ValidationReport:
    """Fallback for an answer given as plain text: citations by regex; figures cannot be machine-checked."""
    r = ValidationReport(machine_checked=False)
    r.bad_citations = [c for c in extract_citations(text) if not registry.has(c)]
    r.citation_hints = _hints(r.bad_citations, registry)
    return r


def render_markdown(markdown: str, report: ValidationReport) -> str:
    """Show unsupported citations as visibly unverified instead of hiding them."""
    bad = set(report.bad_citations)
    return INLINE_CITATION.sub(lambda m: f"[{m.group(1)}:{m.group(2)} \u26a0 unverified]"
                               if f"{m.group(1)}:{m.group(2)}" in bad else m.group(0), markdown)
