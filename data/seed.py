"""Deterministic seeder. No LLM calls, no clock reads.

    python -m data.seed [--variant baseline|injected] [--db PATH]

Prose comes from data/prose/*.json when present (frozen LLM output), else from stub_prose.
The `injected` variant (default) appends the prompt-injection payload to C117's CV.
"""
import argparse
import hashlib
import json
from pathlib import Path

from data import scenarios as sc
from data import stub_prose as stub
from services.db import connect, db_path

HERE = Path(__file__).resolve().parent
TABLES = {  # table -> primary key, for the canonical dump
    "roles": "role_id", "candidates": "candidate_id", "stage_events": "event_id",
    "interview_feedback": "feedback_id", "email_messages": "email_id", "tasks": "task_id",
}


def _frozen(name: str) -> dict:
    p = HERE / "prose" / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _insert(conn, table: str, row: dict) -> None:
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


def build(path: str | Path | None = None, variant: str = "injected", frozen: bool = True) -> Path:
    assert variant in ("baseline", "injected"), variant
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    cv_p, em_p, fb_p, er_p = (_frozen(n) if frozen else {} for n in ("cv", "emails", "feedback", "exit_reasons"))
    name_of = {c["candidate_id"]: c["name"] for c in sc.CANDIDATES}
    conn = connect(target)
    with conn:
        conn.executescript((HERE / "schema.sql").read_text())
        for r in sc.ROLES:
            _insert(conn, "roles", {
                "role_id": r["role_id"], "title": r["title"], "department": r["department"],
                "opened_at": r["opened_at"], "status": r["status"],
                "requirements_json": json.dumps(r["requirements"], sort_keys=True),
                "stage_order_json": json.dumps(r["stage_order"]),
                "stage_targets_json": json.dumps(r["stage_targets"], sort_keys=True)})
        for c in sc.CANDIDATES:
            cv = cv_p.get(c["candidate_id"]) or stub.cv_text(c["candidate_id"])
            if c["candidate_id"] == "C117" and variant == "injected":
                cv = f"{cv}\n\n{sc.INJECTION_PAYLOAD}"
            _insert(conn, "candidates", {**c, "cv_text": cv})
        for e in sc.STAGE_EVENTS:
            row = {k: v for k, v in e.items() if k != "seq"}
            row["exit_reason"] = er_p.get(e["event_id"]) or stub.exit_reason(e)
            _insert(conn, "stage_events", row)
        for f in sc.FEEDBACK:
            row = {k: v for k, v in f.items() if k != "criteria"}
            row["criteria_json"] = json.dumps(f["criteria"], sort_keys=True)
            row["summary_text"] = fb_p.get(f["feedback_id"]) or stub.feedback_summary(f)
            _insert(conn, "interview_feedback", row)
        for m in sc.EMAILS:
            subj, body = em_p.get(m["email_id"]) or stub.email(m, name_of[m["candidate_id"]], m["candidate_id"])
            _insert(conn, "email_messages", {
                "email_id": m["email_id"], "candidate_id": m["candidate_id"], "sent_at": m["sent_at"],
                "direction": m["direction"], "subject": subj, "body": body})
        for t in sc.TASKS:
            _insert(conn, "tasks", t)
    conn.close()
    return target


def canonical_hash(path: str | Path) -> str:
    """sha256 of every table dumped in primary-key order; identical for identical data."""
    conn = connect(path)
    h = hashlib.sha256()
    for table, pk in TABLES.items():
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY {pk}").fetchall()
        h.update(json.dumps([list(r) for r in rows], sort_keys=True).encode())
    conn.close()
    return h.hexdigest()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="injected", choices=["baseline", "injected"])
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    out = build(a.db, a.variant)
    print(f"Built {a.variant} DB at {out} (hash {canonical_hash(out)[:12]})")
