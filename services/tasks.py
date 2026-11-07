"""Recruiting tasks. Overdue = pending and due_at strictly before as_of."""
from datetime import datetime

from services.clock import AS_OF, parse_z
from services.db import NotFound, rows, use

STATUSES = ("pending", "completed", "cancelled")
MAX_LIMIT = 50


def task_view(t: dict, as_of: datetime = AS_OF) -> dict:
    overdue = t["status"] == "pending" and parse_z(t["due_at"]) < as_of
    return {**t, "overdue": overdue,
            "days_overdue": round((as_of - parse_z(t["due_at"])).total_seconds() / 86400, 4) if overdue else None}


def get_tasks(role_id=None, candidate_id=None, status=None, overdue_only=False, limit=MAX_LIMIT,
              as_of: datetime = AS_OF, conn=None) -> dict:
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    with use(conn) as c:
        if role_id and not rows(c, "SELECT 1 FROM roles WHERE role_id = ?", role_id):
            raise NotFound(f"role {role_id}")
        if candidate_id and not rows(c, "SELECT 1 FROM candidates WHERE candidate_id = ?", candidate_id):
            raise NotFound(f"candidate {candidate_id}")
        where, args = [], []
        for col, val in (("role_id", role_id), ("candidate_id", candidate_id), ("status", status)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY due_at, task_id"
        views = [task_view(t, as_of) for t in rows(c, sql, *args)]
    if overdue_only:
        views = [v for v in views if v["overdue"]]
    return {"tasks": views[:limit], "total_matching": len(views), "returned": min(len(views), limit)}
