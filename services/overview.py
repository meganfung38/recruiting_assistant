"""One cross-role snapshot, so questions that compare roles or look across everyone ("which role needs attention", "who has waited
longest for an interview", "what needs attention") are answered from ONE result instead of the model adding up several.

Per role: days open, active candidates, how many are at each stage, how many are waiting to be scheduled / booked but not yet
interviewed / waiting on feedback, and overdue tasks. Then the people in those three waiting groups, longest wait first, each with
role, stage and a dated status sentence. Everyone else (in review, awaiting a decision) is only counted: search_candidates lists them.
Everything is derived from the same search rows and tasks the other tools use.
"""
from datetime import datetime

from services.candidates import list_roles, search_candidates
from services.clock import AS_OF
from services.db import use
from services.tasks import get_tasks

GROUPS = {"waiting_to_be_scheduled": "awaiting_scheduling", "interview_booked_not_held": "awaiting_interview",
          "waiting_for_feedback": "awaiting_feedback"}


def get_recruiting_overview(as_of: datetime = AS_OF, conn=None) -> dict:
    roles, people = [], []
    with use(conn) as c:
        for r in list_roles(as_of, c):
            rows = search_candidates(r["role_id"], as_of=as_of, conn=c)["candidates"]
            count = lambda state: sum(x["waiting_state"] == state for x in rows)
            roles.append({"role_id": r["role_id"], "title": r["title"], "role_age_days": r["role_age_days"], "active_candidates": len(rows),
                          "active_by_stage": {st: n for st in r["stage_order"] if (n := sum(x["current_stage"] == st for x in rows))},
                          **{group: count(state) for group, state in GROUPS.items()},
                          "overdue_tasks": get_tasks(r["role_id"], overdue_only=True, as_of=as_of, conn=c)["total_matching"]})
            people += [{"candidate_id": x["candidate_id"], "name": x["name"], "role_id": r["role_id"], "role_title": r["title"],
                        "stage": x["current_stage"], "waiting_state": x["waiting_state"], "waiting_days": x["waiting_days"],
                        "stage_status": x["stage_status"]} for x in rows if x["waiting_state"] in GROUPS.values()]
        overdue_total = get_tasks(overdue_only=True, as_of=as_of, conn=c)["total_matching"]
    people.sort(key=lambda w: (-w["waiting_days"], w["role_id"], w["name"]))
    return {"roles": roles, "overdue_tasks_total": overdue_total,
            **{group: [p for p in people if p["waiting_state"] == state] for group, state in GROUPS.items()}}
