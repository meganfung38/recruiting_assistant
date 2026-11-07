"""SQLite access. DB path comes from RECRUITING_DB_PATH so tests can use temp DBs."""
import os
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "recruiting.db"


def db_path() -> Path:
    return Path(os.environ.get("RECRUITING_DB_PATH") or DEFAULT_DB)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
