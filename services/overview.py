"""One deterministic snapshot of all active candidates (grouped by what they are waiting for) and per-role counts, for "what needs attention", "which role", "who is at stage X"
and "who has waited longest" questions, so the assistant never counts across several tool results itself. People waiting to be scheduled,
for an interview to be held, or for feedback come first (longest wait first), then everyone else; everything is derived from the same
search rows and tasks the other tools use."""
from datetime import datetime

from services.candidates import list_roles, search_candidates
from services.clock import AS_OF
from services.db import use
from services.tasks import get_tasks

WAITING_STATES = ("awaiting_scheduling", "awaiting_interview", "awaiting_feedback")


def get_recruiting_overview(as_of: datetime = AS_OF, conn=None) -> dict:
    roles, people = [], []
    with use(conn) as c:
        for r in list_roles(as_of, c):
            rows = search_candidates(r["role_id"], as_of=as_of, conn=c)["candidates"]
            mine = [x for x in rows if x["waiting_state"] in WAITING_STATES]
            overdue = get_tasks(role_id=r["role_id"], overdue_only=True, as_of=as_of, conn=c)["total_matching"]
            count = lambda state: sum(x["waiting_state"] == state for x in mine)
            roles.append({"role_id": r["role_id"], "title": r["title"], "role_age_days": r["role_age_days"], "active_candidates": len(rows),
                          "waiting_to_be_scheduled": count("awaiting_scheduling"), "interview_booked_not_held": count("awaiting_interview"),
                          "waiting_for_feedback": count("awaiting_feedback"), "overdue_tasks": overdue,
                          "active_by_stage": {st: sum(x["current_stage"] == st for x in rows) for st in dict.fromkeys(x["current_stage"] for x in rows)}})
            people += [{"candidate_id": x["candidate_id"], "name": x["name"], "role_id": r["role_id"], "role_title": r["title"],
                         "stage": x["current_stage"], "waiting_state": x["waiting_state"], "waiting_days": x["waiting_days"],
                         "stage_status": x["stage_status"]} for x in rows]
        unowned = get_tasks(overdue_only=True, as_of=as_of, conn=c)["total_matching"]
    people.sort(key=lambda w: (-w["waiting_days"], w["role_id"], w["name"]))
    groups = {"waiting_to_be_scheduled": "awaiting_scheduling", "interview_booked_not_held": "awaiting_interview", "waiting_for_feedback": "awaiting_feedback"}
    out = {"roles": roles, "overdue_tasks_total": unowned}
    for key, state in groups.items():                      # longest wait first inside each group
        out[key] = [p for p in people if p["waiting_state"] == state]
    out["other_active"] = sorted((p for p in people if p["waiting_state"] not in WAITING_STATES), key=lambda p: (p["role_id"], p["stage"], p["name"]))
    return out
