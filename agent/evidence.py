"""Per-turn evidence registry: the set of record IDs and aggregate values the model actually received.

Built only from structured fields (`source_refs`, IDs under allowlisted keys) of SUCCESSFUL calls,
never by parsing free text, so candidate-supplied text cannot create entries. Citation and figure
validation (increment 6) is layered on this.
"""
import re
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
    """Raw values kept (in memory only) so figures can be validated later: analytics and tasks."""
    data = raw_envelope.get("data") or {}
    if tool == "get_pipeline_analytics":
        return {r: data for r in raw_envelope["source_refs"] if r.startswith("analytics:")}
    if tool == "get_tasks":
        return {f"task:{t['task_id']}": t for t in data.get("tasks", [])}
    return {}


class EvidenceRegistry:
    def __init__(self) -> None:
        self._refs: dict[str, str] = {}   # citation ref -> tool that returned it
        self._candidates: set[str] = set()
        self._raw: dict[str, Any] = {}
        self._omitted: set[str] = set()   # candidates a size-capped result left out

    def add(self, tool: str, safe_envelope: dict, raw_aggregates: dict | None = None) -> None:
        """Register a successful call's evidence. Callers must not invoke this for failed calls."""
        for ref in safe_envelope["source_refs"]:
            if CITATION_RE.match(ref):
                self._refs.setdefault(ref, tool)
        self._candidates |= _candidate_ids(safe_envelope["data"])
        self._omitted |= {i for i in safe_envelope.get("omitted", []) if re.fullmatch(r"C\d{3}", i)}
        self._raw.update(raw_aggregates or {})

    def has(self, ref: str) -> bool:
        return ref in self._refs

    def refs(self) -> list[str]:
        return sorted(self._refs)

    def candidate_seen(self, candidate_id: str) -> bool:
        return candidate_id in self._candidates

    def candidate_ids(self) -> set[str]:
        return set(self._candidates)

    def unresolved_candidates(self) -> set[str]:
        """Candidates a truncated result omitted and that no later successful call retrieved (no `cv:` record)."""
        return {c for c in self._omitted if not self.has(f"cv:{c}")}

    def raw(self, ref: str) -> Any:
        return self._raw.get(ref)
