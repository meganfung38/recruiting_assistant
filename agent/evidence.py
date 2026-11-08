"""Per-turn evidence registry and answer validation.

The registry holds what the model was actually shown this turn (built only from structured fields of SUCCESSFUL calls, never by
parsing free text for IDs): which candidates and roles appeared, which candidate records were read, each record as the model saw
it (for the evidence cards), and every number the model saw, grouped by the candidate or role it belongs to.

The model no longer writes citations or figure paths. It writes plain prose and declares which people and roles the answer is
about, plus an optional ranking. Validation is deterministic and checks PRESENCE, not meaning:
  - every declared or ranked candidate appeared this turn, and every ranked candidate's record was read;
  - every declared role appeared this turn;
  - every number in the prose occurs in this turn's data for the people or roles named on the same line (or anywhere in the
    turn's data when the line names nobody). A number carried over from another role, or one the model made up, is reported.
A number that exists is not proof the sentence using it is right; the evals cover some of that.
"""
import re
from dataclasses import dataclass, field

CITATION_RE = re.compile(r"^(cv:C\d{3}|search:C\d{3}|feedback:FB-\d{3}|stage:EV-\d{3}|email:EM-\d{3}|task:T-\d{3}|role:ROLE-[A-Z]{2,3}"
                         r"|analytics:ROLE-[A-Z]{2,3}@\d{4}-\d{2}-\d{2})$")
CAND_RE, ROLE_RE = re.compile(r"C\d{3}"), re.compile(r"ROLE-[A-Z]{2,3}")
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
NOT_A_QUANTITY = re.compile(                    # dates, times, years, IDs and ranks are not quantities to check
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}(?:[ T]\d{{2}}:\d{{2}}(?::\d{{2}})?)?(?: ?[A-Z]{{3}})?|\b{MONTH} \d{{1,2}}(?:st|nd|rd|th)?(?:,? \d{{4}})?"
    r"|\b\d{1,2}:\d{2}(?: ?[AaPp]\.?[Mm]\.?)?(?: [A-Z]{3})?|\b(?:19|20)\d{2}\b|\b(?:C\d{3}|ROLE-[A-Z]{2,3}|(?:FB|EV|EM|T)-\d{3})\b|#\d+"
    r"|\[[a-z]+:[^\]]*\]")
LIST_MARKER = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s+")
# "a 7-day time-in-stage target": the number must be THAT clock's target for the stage on the line (three clocks, three targets).
CLOCK_TARGET = re.compile(r"(\d+)[- ]day (time[- ]in[- ](?:the[- ])?stage|stage[- ]duration|duration|booking|scheduling|feedback) target", re.I)
CLOCK_KEY = {"time": "duration_days", "stage": "duration_days", "duration": "duration_days", "booking": "scheduling_days",
             "scheduling": "scheduling_days", "feedback": "feedback_days"}
CLOCK_WORDS = {"duration_days": "time-in-stage", "scheduling_days": "booking", "feedback_days": "feedback"}
NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\d)")
ANY = "*"


def numbers_in(text: str) -> list[float]:
    return [float(n) for n in NUMBER.findall(NOT_A_QUANTITY.sub(" ", text or ""))]


def _matches(value: float, seen: set[float]) -> bool:
    """Exact, or the same value as shown rounded (22.0 -> 22, 8.4583 -> 8.5 or 8)."""
    return any(abs(value - p) < 1e-6 or round(p) == value or round(p, 1) == value for p in seen)


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


def _collect(node, cand: str | None, role: str | None, add) -> None:
    """Every number the model saw, tagged with the innermost candidate (else role) it belongs to. List sizes count too
    ("8 active candidates" may be the length of a list); numbers inside text count (a CV's "7 years", a status's "22 days")."""
    if isinstance(node, dict):
        if "untrusted_text" in node:
            for n in numbers_in(str(node["untrusted_text"])):
                add(n, cand, role)
            return
        if isinstance(node.get("candidate_id"), str) and CAND_RE.fullmatch(node["candidate_id"]) and not node.get("not_found"):
            cand = node["candidate_id"]
        if isinstance(node.get("role_id"), str) and ROLE_RE.fullmatch(node["role_id"]):
            role = node["role_id"]
        for v in node.values():
            _collect(v, cand, role, add)
    elif isinstance(node, list):
        add(float(len(node)), cand, role)
        for v in node:
            _collect(v, cand, role, add)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, (int, float)):
        add(float(node), cand, role)
    elif isinstance(node, str):
        for n in numbers_in(node):
            add(n, cand, role)


class EvidenceRegistry:
    def __init__(self) -> None:
        self._refs: dict[str, str] = {}       # citable ref -> tool that returned it (trace and developer view)
        self._candidates: set[str] = set()    # candidates that appeared in any result
        self._retrieved: set[str] = set()     # candidates whose record came back from get_candidates
        self._records: dict[str, dict] = {}   # candidate_id -> the record exactly as the model saw it (projected)
        self._names: dict[str, str] = {}
        self._roles: dict[str, str] = {}      # role_id -> title, learned from any result that carries both
        self._cand_roles: dict[str, str] = {} # candidate_id -> role_id
        self._numbers: dict[str, set[float]] = {ANY: set()}   # candidate_id / role_id / ANY -> numbers the model saw
        self._targets: dict[tuple[str, str], dict] = {}       # (role_id, stage) -> {duration_days, scheduling_days, feedback_days}

    def add(self, tool: str, safe_envelope: dict) -> None:
        """Register a successful call's evidence (projected result only). Callers must not invoke this for failed calls."""
        data = safe_envelope["data"]
        for ref in safe_envelope["source_refs"]:
            if CITATION_RE.match(ref):
                self._refs.setdefault(ref, tool)
        self._candidates |= _candidate_ids(data)
        self._names.update(_names(data))
        self._roles.update(_roles(data))
        self._cand_roles.update(_candidate_roles(data))
        if tool == "get_pipeline_analytics" and isinstance(data, dict):
            for st in data.get("stages", []):
                if isinstance(st.get("targets"), dict) and isinstance(st.get("stage"), str):
                    self._targets[(str(data.get("role_id")), st["stage"])] = st["targets"]
        if tool == "get_candidates":
            for c in (data or {}).get("candidates", []):
                if c.get("not_found") or not isinstance(c.get("candidate_id"), str):
                    continue
                self._retrieved.add(c["candidate_id"])
                self._records[c["candidate_id"]] = {**self._records.get(c["candidate_id"], {}), **c}   # by-ID detail adds to a role view

        def put(n: float, cand: str | None, role: str | None) -> None:
            self._numbers[ANY].add(n)
            key = cand or role
            if key:
                self._numbers.setdefault(key, set()).add(n)
        _collect(data, None, None, put)

    # ---- lookups
    def has(self, ref: str) -> bool:
        return ref in self._refs

    def refs(self) -> list[str]:
        return sorted(self._refs)

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

    def candidate_role(self, candidate_id: str) -> str | None:
        return self._cand_roles.get(candidate_id)

    def record(self, candidate_id: str) -> dict | None:
        return self._records.get(candidate_id)

    def retrieved(self, candidate_id: str) -> bool:
        return candidate_id in self._retrieved

    def retrieved_candidates(self) -> list[str]:
        return sorted(self._retrieved)

    def candidate_seen(self, candidate_id: str) -> bool:
        return candidate_id in self._candidates

    def role_seen(self, role_id: str) -> bool:
        return role_id in self._roles

    def candidate_ids(self) -> set[str]:
        return set(self._candidates)

    # ---- who a piece of text is about
    def mentioned_in(self, text: str) -> tuple[list[str], list[str]]:
        """(candidate IDs in the order they are first mentioned, role IDs) named in `text` by full name, by a first name no one else
        shares, or by role title (or its first word when that is unique, e.g. "Backend")."""
        firsts: dict[str, list[str]] = {}
        for cid, name in self._names.items():
            firsts.setdefault(name.split()[0], []).append(cid)
        at: dict[str, int] = {}                  # candidate -> first position in the text, so the order is the order they are mentioned
        for cid, name in self._names.items():
            hits = [m.start() for p in ([name] + ([name.split()[0]] if len(firsts[name.split()[0]]) == 1 else []))
                    for m in [re.search(rf"\b{re.escape(p)}\b", text)] if m]
            if hits:
                at[cid] = min(hits)
        cands = sorted(at, key=at.get)
        heads: dict[str, list[str]] = {}
        for rid, title in self._roles.items():
            heads.setdefault(title.split()[0], []).append(rid)
        roles = [rid for rid, title in self._roles.items()
                 if re.search(rf"\b{re.escape(title)}\b", text, re.I) or (len(heads[title.split()[0]]) == 1 and re.search(rf"\b{re.escape(title.split()[0])}\b", text))]
        return cands, roles

    def targets_for(self, line: str, roles: list[str]) -> list[tuple[str, dict]]:
        """(stage, targets) for the stages named on the line (all known stages if none), within the line's roles if any."""
        known = [(r, s, t) for (r, s), t in self._targets.items() if not roles or r in roles]
        named = [(s, t) for r, s, t in known if re.search(rf"\b{re.escape(s)}\b", line, re.I)]
        return named or [(s, t) for r, s, t in known]

    def numbers_for(self, cands: list[str], roles: list[str]) -> set[float]:
        """Numbers a line about these people/roles may use: their own, plus each person's role (targets, stage medians)."""
        if not cands and not roles:
            return self._numbers[ANY]
        keys = set(cands) | set(roles) | {self._cand_roles[c] for c in cands if c in self._cand_roles}
        return set().union(*(self._numbers.get(k, set()) for k in keys))


# ----------------------------------------------------------------------------- answer validation
@dataclass
class UnsupportedNumber:
    value: float
    line: str
    about: list[str]                  # names / role titles the line is about ([] = the whole turn's data was searched)


@dataclass
class ValidationReport:
    structured: bool = True                              # False for a plain-text answer (no declared subjects or ranking)
    candidates: list[str] = field(default_factory=list)  # who the answer is about: ranked first, then declared, then named
    roles: list[str] = field(default_factory=list)
    unseen_candidates: list[str] = field(default_factory=list)
    not_retrieved: list[str] = field(default_factory=list)
    unknown_roles: list[str] = field(default_factory=list)
    unsupported_numbers: list[UnsupportedNumber] = field(default_factory=list)
    wrong_targets: list[str] = field(default_factory=list)   # "a 7-day time-in-stage target" where that clock's target is 14 days

    @property
    def categories(self) -> list[str]:
        return [c for c, bad in (("unseen_candidate", self.unseen_candidates), ("candidate_not_retrieved", self.not_retrieved),
                                 ("unknown_role", self.unknown_roles), ("unsupported_number", self.unsupported_numbers),
                                 ("wrong_target", self.wrong_targets)) if bad]

    @property
    def ok(self) -> bool:
        return not self.categories

    def problems(self) -> list[str]:
        """Plain problems for the repair message to the model and the developer view (IDs, numbers, short quotes)."""
        out = [f"Candidate {c} did not appear in any tool result this turn; remove them or look them up." for c in self.unseen_candidates]
        out += [f"Candidate {c} is ranked but their record was not read; call get_candidates for them or take them out of the ranking."
                for c in self.not_retrieved]
        out += [f"Role {r} did not appear in any tool result this turn." for r in self.unknown_roles]
        out += [f"The number {n.value:g} in \"{n.line.strip()[:90]}\" is not in this turn's data"
                + (f" for {', '.join(n.about)}" if n.about else "") + ". Use the exact value from the results, or remove it."
                for n in self.unsupported_numbers]
        return out + self.wrong_targets


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _check_numbers(text: str, registry: EvidenceRegistry, allowed: set[float]) -> list[UnsupportedNumber]:
    out, header = [], None                                       # header: column names of the table being read, if any
    for line in (text or "").splitlines():
        is_row = line.lstrip().startswith("|")
        if not is_row:
            header = None
        elif header is None:                                     # a table's first row names its columns
            header = [h.lower() for h in _cells(line)]
            continue
        if re.fullmatch(r"\s*\|?[\s:|-]*\|?\s*", line):          # blank lines and table separators
            continue
        checked = LIST_MARKER.sub("", line)                      # "1. Dahlia ..." numbers a list item, it is not a quantity
        if is_row:                                               # a rank/position column ("| 1 | Dahlia |") orders rows, it is not a quantity
            checked = "|".join(c for i, c in enumerate(_cells(line))
                               if not (i < len(header) and re.fullmatch(r"#|no\.?|rank|position|order", header[i]) and re.fullmatch(r"\d+", c)))
        values = numbers_in(checked)
        if not values:
            continue
        cands, roles = registry.mentioned_in(line)
        seen = registry.numbers_for(cands, roles) | allowed
        about = [registry.display_name(c) or c for c in cands] + [registry.role_title(r) or r for r in roles]
        out += [UnsupportedNumber(v, line, about) for v in dict.fromkeys(values) if not _matches(v, seen)]
    return out


def _check_targets(text: str, registry: EvidenceRegistry) -> list[str]:
    """Every "N-day <clock> target" names the target of that clock for a stage on the same line. Catches the clock mix-up where a
    7-day booking target was called a time-in-stage target (that clock's target was 14 days)."""
    out = []
    for line in (text or "").splitlines():
        for m in CLOCK_TARGET.finditer(line):
            key = CLOCK_KEY[m.group(2).split()[0].split("-")[0].lower()]
            stages = registry.targets_for(line, registry.mentioned_in(line)[1])
            right = sorted({t.get(key) for _, t in stages if t.get(key) is not None})
            if right and float(m.group(1)) not in right:
                out.append(f"\"{m.group(0)}\" in \"{line.strip()[:90]}\": the {CLOCK_WORDS[key]} target there is "
                           + " or ".join(f"{d:g} days" for d in right) + f". {m.group(1)} days is a different clock's target; "
                           "name the clock that target belongs to.")
    return out


def validate_answer(answer, registry: EvidenceRegistry, question: str = "") -> ValidationReport:
    """Check a SubmittedAnswer against what this turn's tools returned."""
    ranked = [r.candidate_id for r in answer.ranking]
    named, named_roles = registry.mentioned_in(answer.answer_markdown)
    r = ValidationReport(candidates=list(dict.fromkeys(ranked + list(answer.candidates) + named)),
                         roles=list(dict.fromkeys(list(answer.roles) + named_roles)))
    r.unseen_candidates = [c for c in dict.fromkeys(ranked + list(answer.candidates)) if not registry.candidate_seen(c)]
    r.not_retrieved = [c for c in dict.fromkeys(ranked) if registry.candidate_seen(c) and not registry.retrieved(c)]
    r.unknown_roles = [x for x in dict.fromkeys(answer.roles) if not registry.role_seen(x)]
    r.unsupported_numbers = _check_numbers(answer.answer_markdown, registry, set(numbers_in(question)))
    r.wrong_targets = _check_targets(answer.answer_markdown, registry)
    return r


def validate_plain(text: str, registry: EvidenceRegistry, question: str = "") -> ValidationReport:
    """An answer given as plain text: no declared subjects or ranking, but the people it names and its numbers are still checked."""
    named, named_roles = registry.mentioned_in(text)
    r = ValidationReport(structured=False, candidates=named, roles=named_roles)
    r.unsupported_numbers = _check_numbers(text, registry, set(numbers_in(question)))
    r.wrong_targets = _check_targets(text, registry)
    return r
