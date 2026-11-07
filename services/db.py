"""SQLite access. DB path comes from RECRUITING_DB_PATH so tests can use temp DBs."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "recruiting.db"


def db_path() -> Path:
    return Path(os.environ.get("RECRUITING_DB_PATH") or DEFAULT_DB)


def connect(path: str | Path | None = None, create: bool = False) -> sqlite3.Connection:
    target = Path(path or db_path())
    if not create and not target.exists():  # don't silently create an empty DB
        raise FileNotFoundError(f"{target} not found; run `python -m data.seed`")
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class NotFound(Exception):
    """A requested role or candidate does not exist."""


@contextmanager
def use(conn: sqlite3.Connection | None = None):
    """Yield the given connection, or open (and close) one from RECRUITING_DB_PATH."""
    if conn is not None:
        yield conn
        return
    c = connect()
    try:
        yield c
    finally:
        c.close()


def rows(conn: sqlite3.Connection, sql: str, *args) -> list[dict]:
    """Parameterised query -> list of dicts. Never build `sql` from user input."""
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def load_role(conn: sqlite3.Connection, role_id: str) -> dict:
    r = rows(conn, "SELECT * FROM roles WHERE role_id = ?", role_id)
    if not r:
        raise NotFound(f"role {role_id}")
    r = r[0]
    return {"role_id": r["role_id"], "title": r["title"], "department": r["department"],
            "opened_at": r["opened_at"], "status": r["status"],
            "requirements": json.loads(r["requirements_json"]),
            "stage_order": json.loads(r["stage_order_json"]),
            "stage_targets": json.loads(r["stage_targets_json"])}
