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

CITATION_RE = re.compile(r"^(cv:C\d{3}|feedback:FB-\d{3}|stage:EV-\d{3}|email:EM-\d{3}|task:T-\d{3}"
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


def aggregates(tool: str, raw_envelope: dict) -> dict[str, Any]:
    """Raw values kept (in memory only) so figures can be validated later: analytics, tasks, and per-candidate numbers.
    Candidate values live under `cv:<ID>`: record fields from get_candidates (feedback_count, waiting_days,
    days_in_current_stage, ...) and, from search rows, `fit_signals` (avg_criteria_score, ...). CV text is not kept."""
    data = raw_envelope.get("data") or {}
    if tool == "get_pipeline_analytics":
        return {r: data for r in raw_envelope["source_refs"] if r.startswith("analytics:")}
    if tool == "get_tasks":
        return {f"task:{t['task_id']}": t for t in data.get("tasks", [])}
    if tool == "get_candidates":
        return {f"cv:{c['candidate_id']}": {k: v for k, v in c.items() if k != "cv_text"}
                for c in data.get("candidates", []) if not c.get("not_found")}
    if tool == "search_candidates":
        return {f"cv:{c['candidate_id']}": {"fit_signals": c["fit_signals"], "waiting_days": c["waiting_days"],
                                           "days_in_current_stage": c["days_in_current_stage"]}
                for c in data.get("candidates", [])}
    return {}


class EvidenceRegistry:
    def __init__(self) -> None:
        self._refs: dict[str, str] = {}   # citation ref -> tool that returned it
        self._candidates: set[str] = set()
        self._raw: dict[str, Any] = {}
        self._omitted: set[str] = set()   # candidates a size-capped result left out, or a refused/failed request asked for

    def add(self, tool: str, safe_envelope: dict, raw_aggregates: dict | None = None) -> None:
        """Register a successful call's evidence. Callers must not invoke this for failed calls."""
        for ref in safe_envelope["source_refs"]:
            if CITATION_RE.match(ref):
                self._refs.setdefault(ref, tool)
        self._candidates |= _candidate_ids(safe_envelope["data"])
        self._omitted |= {i for i in safe_envelope.get("omitted", []) if re.fullmatch(r"C\d{3}", i)}
        shown = _candidate_ids(safe_envelope["data"])          # keep raw values only for what the model was shown
        for ref, value in (raw_aggregates or {}).items():
            if ref in safe_envelope["source_refs"] or (ref.startswith("cv:") and ref[3:] in shown):
                if isinstance(value, dict) and isinstance(self._raw.get(ref), dict):
                    self._raw[ref] = {**self._raw[ref], **value}   # search signals + record fields for one candidate
                else:
                    self._raw[ref] = value

    def has(self, ref: str) -> bool:
        return ref in self._refs

    def refs(self) -> list[str]:
        return sorted(self._refs)

    def candidate_seen(self, candidate_id: str) -> bool:
        return candidate_id in self._candidates

    def candidate_ids(self) -> set[str]:
        return set(self._candidates)

    def note_unfulfilled(self, candidate_ids) -> None:
        """Candidates the model asked get_candidates for, but a limit/timeout/outage stopped it receiving. They stay
        unresolved until a later successful call retrieves them."""
        self._omitted |= {i for i in candidate_ids if re.fullmatch(r"C\d{3}", i)}

    def unresolved_candidates(self) -> set[str]:
        """Candidates a truncated result omitted and that no later successful call retrieved (no `cv:` record)."""
        return {c for c in self._omitted if not self.has(f"cv:{c}")}

    def raw(self, ref: str) -> Any:
        return self._raw.get(ref)


# ----------------------------------------------------------------------------- answer validation
INLINE_CITATION = re.compile(r"\[(cv|feedback|stage|email|task|analytics):([^\]\s]{1,60})\]")
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


@dataclass
class ValidationReport:
    machine_checked: bool = True                        # False for the plain-text fallback (figures not checked)
    bad_citations: list[str] = field(default_factory=list)
    unseen_candidates: list[str] = field(default_factory=list)
    not_retrieved: list[str] = field(default_factory=list)
    figures: list[FigureResult] = field(default_factory=list)
    undisclosed_incomplete: list[str] = field(default_factory=list)

    @property
    def unverified_figures(self) -> list[FigureResult]:
        return [f for f in self.figures if not f.verified]

    @property
    def categories(self) -> list[str]:
        return [c for c, bad in (("unsupported_citation", self.bad_citations), ("unseen_candidate", self.unseen_candidates),
                                 ("candidate_not_retrieved", self.not_retrieved), ("unverified_figure", self.unverified_figures),
                                 ("undisclosed_incomplete", self.undisclosed_incomplete)) if bad]

    @property
    def ok(self) -> bool:
        return not self.categories

    def problems(self) -> list[str]:
        """Plain-language problems, for the repair message to the model (contains only IDs and numbers)."""
        out = [f"Citation not found in this turn's tool results: [{c}]" for c in self.bad_citations]
        out += [f"Candidate {c} did not appear in any tool result this turn." for c in self.unseen_candidates]
        out += [f"Candidate {c} is listed but its record was never retrieved with get_candidates; retrieve it or remove it from candidates."
                for c in self.not_retrieved]
        out += [f"Figure '{f.label}' ({f.value}) is unverified: {f.reason}" for f in self.unverified_figures]
        if self.undisclosed_incomplete:
            out.append("Earlier results omitted these candidates and they were never retrieved: "
                       f"{', '.join(self.undisclosed_incomplete)}. Retrieve them in small batches, or name them in "
                       "uncertainties and say the comparison is incomplete (do not present a definitive ranking).")
        return out


def check_figure(f, registry: "EvidenceRegistry") -> FigureResult:
    base = dict(label=f.label, value=f.value, evidence=f.evidence, field_path=f.field_path)
    raw = registry.raw(f.evidence)
    if raw is None:
        return FigureResult(**base, verified=False, reason=f"no stored result for evidence '{f.evidence}'")
    found, actual = resolve_path(raw, f.field_path)
    if not found:
        return FigureResult(**base, verified=False, reason=f"path '{f.field_path}' not found in {f.evidence}")
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return FigureResult(**base, verified=False, reason=f"the value at that path is not a number ({actual!r})")
    if not math.isclose(f.value, actual, **FLOAT_TOLERANCE):
        return FigureResult(**base, verified=False, reason=f"the data says {actual}, not {f.value}")
    return FigureResult(**base, verified=True)


def _mentions(text: str, ids) -> list[str]:
    return [i for i in ids if not re.search(rf"\b{re.escape(i)}\b", text)]


def validate_submission(answer, registry: "EvidenceRegistry") -> ValidationReport:
    """Check a SubmittedAnswer: citations, candidates, figures, and disclosure of unretrieved candidates."""
    r = ValidationReport()
    cited = extract_citations(answer.answer_markdown) + [e for c in answer.candidates for e in c.evidence]
    r.bad_citations = [c for c in dict.fromkeys(cited) if not registry.has(c)]
    for c in answer.candidates:
        if not registry.candidate_seen(c.candidate_id):
            r.unseen_candidates.append(c.candidate_id)
        elif not registry.has(f"cv:{c.candidate_id}"):
            r.not_retrieved.append(c.candidate_id)
    r.figures = [check_figure(f, registry) for f in answer.figures]
    if answer.candidates:                               # a shortlist/comparison must not hide unretrieved candidates
        r.undisclosed_incomplete = _mentions(" ".join([*answer.uncertainties, *answer.content_warnings]),
                                             sorted(registry.unresolved_candidates()))
    return r


def validate_plain(text: str, registry: "EvidenceRegistry") -> ValidationReport:
    """Fallback for an answer given as plain text: citations by regex; figures cannot be machine-checked."""
    r = ValidationReport(machine_checked=False)
    r.bad_citations = [c for c in extract_citations(text) if not registry.has(c)]
    r.undisclosed_incomplete = _mentions(text, sorted(registry.unresolved_candidates()))
    return r


def render_markdown(markdown: str, report: ValidationReport) -> str:
    """Show unsupported citations as visibly unverified instead of hiding them."""
    bad = set(report.bad_citations)
    return INLINE_CITATION.sub(lambda m: f"[{m.group(1)}:{m.group(2)} \u26a0 unverified]"
                               if f"{m.group(1)}:{m.group(2)}" in bad else m.group(0), markdown)
