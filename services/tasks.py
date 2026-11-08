"""Recruiting tasks. Overdue = pending and due_at strictly before as_of."""
import re
from datetime import datetime

from services.clock import AS_OF, parse_z
from services.db import NotFound, resolve_role_id, rows, use

STATUSES = ("pending", "completed", "cancelled")
MAX_LIMIT = 50


def task_view(t: dict, as_of: datetime = AS_OF, names: dict | None = None, titles: dict | None = None) -> dict:
    overdue = t["status"] == "pending" and parse_z(t["due_at"]) < as_of
    return {**t, "candidate_name": (names or {}).get(t.get("candidate_id")), "role_title": (titles or {}).get(t.get("role_id")), "overdue": overdue,
            "days_overdue": round((as_of - parse_z(t["due_at"])).total_seconds() / 86400, 4) if overdue else None}


def _candidate_ids(c, candidate: str) -> list[str]:
    """A candidate reference -> IDs: an ID ("C113"), or a name whose every word appears in the person's name ("Mateo Villareal")."""
    if re.fullmatch(r"C\d{3}", candidate.strip()):
        if not rows(c, "SELECT 1 FROM candidates WHERE candidate_id = ?", candidate.strip()):
            raise NotFound(f"candidate {candidate.strip()}")
        return [candidate.strip()]
    words = candidate.lower().split()
    hits = [r["candidate_id"] for r in rows(c, "SELECT candidate_id, name FROM candidates ORDER BY candidate_id")
            if words and all(w in r["name"].lower() for w in words)]
    if not hits:
        raise NotFound(f"no candidate named '{candidate}'")
    return hits


def get_tasks(role=None, candidate=None, status=None, overdue_only=False, limit=MAX_LIMIT,
              as_of: datetime = AS_OF, conn=None) -> dict:
    """`role` is a role ID or title (see services.db.resolve_role_id); `candidate` is a candidate ID or a person's name."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    with use(conn) as c:
        role_id = resolve_role_id(c, role) if role else None
        cand_ids = _candidate_ids(c, candidate) if candidate else None
        where, args = [], []
        for col, val in (("role_id", role_id), ("status", status)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        if cand_ids:
            where.append(f"candidate_id IN ({', '.join('?' for _ in cand_ids)})")   # placeholders only; values are bound
            args += cand_ids
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY due_at, task_id"
        names = {r["candidate_id"]: r["name"] for r in rows(c, "SELECT candidate_id, name FROM candidates")}
        titles = {r["role_id"]: r["title"] for r in rows(c, "SELECT role_id, title FROM roles")}
        views = [task_view(t, as_of, names, titles) for t in rows(c, sql, *args)]
    if overdue_only:
        views = [v for v in views if v["overdue"]]
    return {"tasks": views[:limit], "total_matching": len(views), "returned": min(len(views), limit),
            "overdue_total": sum(v["overdue"] for v in views), "pending_total": sum(v["status"] == "pending" for v in views)}
