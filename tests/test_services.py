"""Service-layer tests. Every number is compared against an independent plain-Python recomputation
over the scenario rows (tests/scenario_facts.py) AND against the hand-authored seed_expectations.json.
"""
import inspect
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from data import scenarios as sc
from data.seed import build
from services import analytics, candidates, tasks
from services.clock import AS_OF
from services.db import NotFound, connect
from tests import scenario_facts as facts

EXPECT = json.loads((Path(__file__).resolve().parent.parent / "data" / "seed_expectations.json").read_text())
ROLES = ["ROLE-BE", "ROLE-MSE", "ROLE-PO"]
TI, HMI, FI, RS, RR = ("Technical Interview", "Hiring Manager Interview", "Final Interview",
                       "Recruiter Screen", "Resume Review")


@pytest.fixture(scope="session")
def db_file(tmp_path_factory):
    return build(tmp_path_factory.mktemp("svc") / "svc.db", "injected")


@pytest.fixture
def conn(db_file):
    c = connect(db_file)
    yield c
    c.close()


def stage_of(role, stage, conn, **kw):
    out = analytics.get_pipeline_analytics(role, conn=conn, **kw)
    return next(s for s in out["stages"] if s["stage"] == stage)


# ------------------------------------------------------------------ roles
def test_list_roles_carries_what_a_role_requires(conn):
    roles = {r["role_id"]: r for r in candidates.list_roles(conn=conn)}
    assert {k: v["role_age_days"] for k, v in roles.items()} == EXPECT["role_age_days"]
    be = roles["ROLE-BE"]
    assert be["title"] == "Backend Engineer" and be["stage_order"] == ["Resume Review", "Recruiter Screen", "Technical Interview", "Final Interview"]
    assert be["required_skills"][0] == "Python" and be["interview_criteria"]
    assert all(set(r) >= {"required_skills", "preferred_skills", "interview_criteria"} for r in roles.values())


@pytest.mark.parametrize("ref, want", [("ROLE-BE", "ROLE-BE"), ("role-mse", "ROLE-MSE"), ("Backend Engineer", "ROLE-BE"),
                                       ("people operations", "ROLE-PO"), ("  Manufacturing  ", "ROLE-MSE")])
def test_a_role_can_be_named_by_id_or_title(conn, ref, want):
    from services.db import resolve_role_id
    assert resolve_role_id(conn, ref) == want
    assert analytics.get_pipeline_analytics(ref, conn=conn)["role_id"] == want


@pytest.mark.parametrize("ref", ["Sales", "Engineer", "ROLE-XX", ""])
def test_an_unknown_or_ambiguous_role_names_the_known_roles(conn, ref):
    from services.db import resolve_role_id
    with pytest.raises(NotFound) as exc:
        resolve_role_id(conn, ref)
    assert "ROLE-BE (Backend Engineer)" in str(exc.value)                      # tells the model how to retry


# ------------------------------------------------------------------ analytics vs independent recomputation
@pytest.mark.parametrize("role", ROLES)
def test_funnel_matches_independent_recompute(role, conn):
    for stage in sc.ROLE_BY_ID[role]["stage_order"]:
        f = stage_of(role, stage, conn)["funnel"]
        want = facts.funnel(role, stage)
        assert {k: f[k] for k in want} == want
        assert want == EXPECT["funnel"][role][stage]
        denom = want["advanced"] + want["rejected"] + want["withdrawn"]
        assert f["conversion_denominator"] == denom


@pytest.mark.parametrize("key", list(EXPECT["headline"]))
def test_headline_metrics_match_independent_recompute_and_expectations(key, conn):
    role, stage = key.split("|")
    s = stage_of(role, stage, conn)
    sch, fb, dur = s["scheduling"], s["feedback"], s["duration"]
    got = {
        "scheduled_n": sch["scheduled_n"], "scheduling_lag_median_scheduled": sch["lag_median_scheduled_days"],
        "awaiting_scheduling": sch["awaiting_scheduling"],
        "unscheduled_active_wait_median": sch["unscheduled_active_wait_median_days"],
        "unscheduled_active_wait_min": sch["unscheduled_active_wait_min_days"],
        "unscheduled_active_wait_max": sch["unscheduled_active_wait_max_days"],
        "unscheduled_active_over_target": sch["unscheduled_active_over_target"],
        "exited_unscheduled_by_outcome": sch["exited_unscheduled_by_outcome"],
        "scheduling_over_target_count": sch["over_target_count"], "scheduling_observations": sch["observations"],
        "completed_duration_median": dur["completed_median_days"], "completed_n": dur["completed_n"],
        "completed_over_duration_target": dur["completed_over_target"],
        "active_over_duration_target": dur["active_over_target"],
        "interviews_held": fb["interviews_held"], "feedback_submitted": fb["submitted"],
        "feedback_lag_median": fb["lag_median_days"], "feedback_lag_n": fb["lag_n"],
        "feedback_over_target_count": fb["over_target_count"], "feedback_missing_overdue": fb["missing_overdue"],
        "feedback_missing_not_yet_due": fb["missing_not_yet_due"],
    }
    independent = {**facts.scheduling_stats(role, stage), **facts.feedback_stats(role, stage),
                   **facts.duration_stats(role, stage)}
    for name, want in EXPECT["headline"][key].items():
        assert got[name] == pytest.approx(want, abs=1e-3), (name, "service vs expectations")
        assert independent[name] == pytest.approx(want, abs=1e-3), (name, "independent vs expectations")


@pytest.mark.parametrize("role", ROLES)
def test_every_stage_matches_independent_recompute(role, conn):
    for stage in sc.ROLE_BY_ID[role]["stage_order"]:
        s = stage_of(role, stage, conn)
        d, want = s["duration"], facts.duration_stats(role, stage)
        assert d["completed_n"] == want["completed_n"]
        assert d["completed_median_days"] == pytest.approx(want["completed_duration_median"] or 0, abs=1e-3) \
            or want["completed_duration_median"] is None
        assert d["active_over_target"] == want["active_over_duration_target"]
        assert s["reasons"] == facts.reasons(role, stage)
        if s["scheduling"]:
            w = facts.scheduling_stats(role, stage)
            assert s["scheduling"]["observations"] == w["scheduling_observations"]
            assert s["scheduling"]["over_target_count"] == w["scheduling_over_target_count"]
            assert s["scheduling"]["share_over_target"] == pytest.approx(w["share_over_target"], abs=1e-3)
            f = facts.feedback_stats(role, stage)
            assert s["feedback"]["over_target_or_overdue_share"] == pytest.approx(f["share_over_or_overdue"] or 0, abs=1e-3) \
                or f["share_over_or_overdue"] is None


def test_resume_review_has_no_scheduling_or_feedback_metrics(conn):
    s = stage_of("ROLE-BE", RR, conn)
    assert s["scheduling"] is None and s["feedback"] is None  # recruiter notes are excluded from feedback lag
    assert s["targets"]["scheduling_days"] is None and s["targets"]["feedback_days"] is None


def test_final_interview_conversion_is_null_with_note(conn):
    for role in ROLES:
        f = stage_of(role, FI, conn)["funnel"]
        assert f["conversion"] is None and f["conversion_note"] == "no offer stage in dataset"
    be = stage_of("ROLE-BE", TI, conn)["funnel"]
    assert be["conversion"] == pytest.approx(5 / 12, abs=1e-3)  # active candidates are not counted as failures
    assert "conversion_note" not in be


def test_scheduling_never_drops_unscheduled_candidates(conn):
    s = stage_of("ROLE-BE", TI, conn)
    sch = s["scheduling"]
    assert sch["observations"] == s["funnel"]["entered"] == 17
    assert sch["scheduled_n"] + sch["awaiting_scheduling"] + sum(sch["exited_unscheduled_by_outcome"].values()) == 17
    # the median over only the scheduled would be a misleading headline on its own: unscheduled waits are far longer
    assert sch["unscheduled_active_wait_median_days"] > sch["lag_median_scheduled_days"]


def test_low_sample_flags_use_each_metrics_own_n(conn):
    for role in ROLES:
        for s in analytics.get_pipeline_analytics(role, conn=conn)["stages"]:
            assert s["low_sample"]["duration"] == (s["duration"]["completed_n"] < 5)
            if s["scheduling"]:
                assert s["low_sample"]["scheduling"] == (s["scheduling"]["scheduled_n"] < 5)
                assert s["low_sample"]["feedback"] == (s["feedback"]["lag_n"] < 5)
    assert stage_of("ROLE-BE", FI, conn)["low_sample"]["duration"] is True  # only 3 completed Final Interviews


# ------------------------------------------------------------------ story / dominance checks (4.6) via services
def test_backend_dominance_and_controls(conn):
    shares = {s: stage_of("ROLE-BE", s, conn)["scheduling"]["share_over_target"] for s in (RS, TI, FI)}
    assert max(shares, key=shares.get) == TI and shares[RS] == 0 and shares[FI] == 0
    for stage in (RS, FI):
        s = stage_of("ROLE-BE", stage, conn)
        assert s["scheduling"]["lag_median_scheduled_days"] <= s["targets"]["scheduling_days"]
        assert s["duration"]["completed_median_days"] <= s["targets"]["duration_days"]
    ti = stage_of("ROLE-BE", TI, conn)
    assert ti["feedback"]["lag_median_days"] <= ti["targets"]["feedback_days"]


def test_manufacturing_controls_and_people_ops_dominance(conn):
    ti = stage_of("ROLE-MSE", TI, conn)
    assert ti["scheduling"]["lag_median_scheduled_days"] <= 7 and ti["feedback"]["lag_median_days"] <= 2
    assert ti["scheduling"]["scheduled_n"] >= 5 and ti["feedback"]["lag_n"] >= 5
    rr = stage_of("ROLE-MSE", RR, conn)
    assert rr["reasons"]["missing_required_skill"] >= 5
    shares = {s: stage_of("ROLE-PO", s, conn)["feedback"]["over_target_or_overdue_share"] for s in (RS, HMI, FI)}
    assert max(shares, key=shares.get) == HMI
    hm = stage_of("ROLE-PO", HMI, conn)
    assert hm["feedback"]["lag_median_days"] > 2 and hm["scheduling"]["over_target_count"] == 0


# ------------------------------------------------------------------ target semantics
def test_targets_are_strictly_greater_than(conn):
    # PO C144 was booked exactly 5.0 days after entering HMI (target 5); C142's feedback came exactly 2.0 days later (target 2).
    hm = stage_of("ROLE-PO", HMI, conn)
    assert hm["scheduling"]["over_target_count"] == 0
    assert hm["feedback"]["over_target_count"] == 7 and hm["feedback"]["lag_n"] == 8  # one of eight lags equals the target


def test_boundary_with_shifted_as_of(conn):
    # C117 entered TI exactly 12 days before AS_OF. Move as-of back 5 days -> wait is exactly the 7-day target.
    at_target = stage_of("ROLE-BE", TI, conn, as_of=AS_OF - timedelta(days=5))["scheduling"]
    assert at_target["unscheduled_active_over_target"] == 2  # C111 and C113 only; C117 equals the target
    assert stage_of("ROLE-BE", TI, conn, as_of=AS_OF - timedelta(days=4))["scheduling"]["unscheduled_active_over_target"] == 3
    # C111 entered 16 days before AS_OF: age 14.0 is not over the 14-day duration target, 15.0 is.
    def c111(as_of):
        rows = candidates.search_candidates("ROLE-BE", stage=TI, as_of=as_of, conn=conn)["candidates"]
        return next(r for r in rows if r["candidate_id"] == "C111")["fit_signals"]
    assert c111(AS_OF - timedelta(days=2))["over_duration_target"] is False
    assert c111(AS_OF - timedelta(days=1))["over_duration_target"] is True


def test_three_targets_are_never_substituted(db_file, tmp_path):
    def with_targets(change):
        path = tmp_path / "t.db"
        shutil.copy(db_file, path)
        c = connect(path)
        for r in c.execute("SELECT role_id, stage_targets_json FROM roles").fetchall():
            t = json.loads(r["stage_targets_json"])
            for stage in t:
                t[stage].update({k: v for k, v in change.items() if t[stage][k] is not None})
            c.execute("UPDATE roles SET stage_targets_json=? WHERE role_id=?", (json.dumps(t), r["role_id"]))
        c.commit()
        out = analytics.get_pipeline_analytics("ROLE-PO", conn=c)
        c.close()
        return next(s for s in out["stages"] if s["stage"] == HMI)

    base = with_targets({})
    huge_duration = with_targets({"duration_days": 999})   # only the duration view may change
    assert huge_duration["feedback"] == base["feedback"] and huge_duration["scheduling"] == base["scheduling"]
    assert huge_duration["duration"]["active_over_target"] == 0
    huge_feedback = with_targets({"feedback_days": 999})   # only the feedback view may change
    assert huge_feedback["feedback"]["over_target_count"] == 0 and huge_feedback["feedback"]["missing_overdue"] == 0
    assert huge_feedback["duration"] == base["duration"] and huge_feedback["scheduling"] == base["scheduling"]
    huge_sched = with_targets({"scheduling_days": 999})
    assert huge_sched["scheduling"]["over_target_count"] == 0 and huge_sched["feedback"] == base["feedback"]


# ------------------------------------------------------------------ search_candidates + fit signals
def test_search_defaults_and_shape(conn):
    r = candidates.search_candidates("ROLE-BE", conn=conn)
    assert r["total_matching"] == r["returned"] == 8  # C104,C108,C110-C113,C117,C120
    assert {c["status"] for c in r["candidates"]} == {"active"}
    for c in r["candidates"]:
        assert set(c) == {"candidate_id", "role_id", "role_title", "name", "current_stage", "status", "stage_status", "waiting_state", "waiting_days",
                          "days_in_current_stage", "fit_signals", "content_flags", "flag_sources"}
        assert "Skills:" not in json.dumps(c) and "@" not in json.dumps(c)  # no CV text, no addresses
    assert candidates.search_candidates("ROLE-BE", stage=TI, conn=conn)["returned"] == 5
    assert candidates.search_candidates("ROLE-BE", status="withdrawn", conn=conn)["total_matching"] == 6
    assert candidates.search_candidates("ROLE-BE", limit=3, conn=conn)["returned"] == 3
    assert candidates.search_candidates("ROLE-BE", limit=3, conn=conn)["total_matching"] == 8
    for bad in (dict(limit=21), dict(limit=0), dict(status="hired"), dict(stage="Offer")):
        with pytest.raises(ValueError):
            candidates.search_candidates("ROLE-BE", conn=conn, **bad)
    with pytest.raises(NotFound):
        candidates.search_candidates("ROLE-XX", conn=conn)


def test_search_finds_a_person_by_name_in_any_role_and_status(conn):
    (h,) = candidates.search_candidates(name="hannelore", conn=conn)["candidates"]
    assert (h["candidate_id"], h["role_title"]) == ("C108", "Backend Engineer")
    assert [r["candidate_id"] for r in candidates.search_candidates(name="Hannelore Voss", conn=conn)["candidates"]] == ["C108"]
    withdrawn = conn.execute("SELECT name, candidate_id FROM candidates WHERE application_status = 'withdrawn' ORDER BY candidate_id").fetchone()
    found = candidates.search_candidates(name=withdrawn["name"], conn=conn)["candidates"]
    assert withdrawn["candidate_id"] in [r["candidate_id"] for r in found]   # a name search is not limited to active people
    assert candidates.search_candidates(name="Nobody Atall", conn=conn)["total_matching"] == 0


def test_search_without_a_role_covers_every_role(conn):
    finals = candidates.search_candidates(stage="final interview", conn=conn)["candidates"]   # stage name is case-insensitive
    want = [r[0] for r in conn.execute("SELECT candidate_id FROM candidates WHERE application_status = 'active' "
                                       "AND current_stage = 'Final Interview' ORDER BY candidate_id")]
    assert [r["candidate_id"] for r in finals] == want and len({r["role_id"] for r in finals}) > 1


@pytest.mark.parametrize("role", ROLES)
def test_waiting_state_for_every_active_candidate_matches_independent(role, conn):
    for row in candidates.search_candidates(role, conn=conn)["candidates"]:
        state, wait = facts.waiting_state(row["candidate_id"])
        assert row["waiting_state"] == state and row["waiting_days"] == pytest.approx(wait, abs=1e-3)
        assert row["fit_signals"]["waiting_state"] == state
    for cid, (state, wait) in EXPECT["waiting"].items():
        if sc.role_of(cid) == role:
            r = next(x for x in candidates.search_candidates(role, conn=conn)["candidates"] if x["candidate_id"] == cid)
            assert (r["waiting_state"], r["waiting_days"]) == (state, pytest.approx(wait, abs=1e-3))


def test_fit_signals_match_independent_recompute(conn):
    for role in ROLES:
        for status in ("active", "rejected", "withdrawn"):
            for r in candidates.search_candidates(role, status=status, conn=conn)["candidates"]:
                cid, sig = r["candidate_id"], r["fit_signals"]
                spec = sc.SPEC_BY_ID[cid]
                fb = [f for f in sc.FEEDBACK if f["candidate_id"] == cid]
                assert sig["required_skills_missing"] == list(spec.lack)
                assert sig["preferred_skills_matched_count"] == spec.pref
                assert sig["feedback_count"] == len(fb) == sum(sig["recommendation_counts"].values())
                if fb:
                    assert sig["latest_recommendation"] == max(fb, key=lambda f: f["interview_at"])["recommendation"]
                scores = [v for f in fb for v in f["criteria"].values()]
                assert sig["avg_criteria_score"] == (pytest.approx(sum(scores) / len(scores), abs=0.01) if scores else None)
                if status != "active":
                    assert r["waiting_state"] is None and sig["over_duration_target"] is None


def test_candidate_with_no_feedback_is_explicit(conn):
    c120 = next(r for r in candidates.search_candidates("ROLE-BE", conn=conn)["candidates"] if r["candidate_id"] == "C120")
    s = c120["fit_signals"]
    assert s["feedback_count"] == 0 and s["latest_recommendation"] is None and s["avg_criteria_score"] is None
    assert c120["waiting_state"] == "in_review"


def test_no_combined_ranking_score(conn):
    for r in candidates.search_candidates("ROLE-BE", conn=conn)["candidates"]:
        assert not any(k in json.dumps(r).lower() for k in ('"score"', '"rank"', "overall"))


def test_injection_payload_cannot_move_signals(conn, db_file, tmp_path):
    base = build(tmp_path / "base.db", "baseline")
    c2 = connect(base)
    def sig(c):
        return next(r for r in candidates.search_candidates("ROLE-BE", conn=c)["candidates"] if r["candidate_id"] == "C117")
    assert sig(conn)["fit_signals"] == sig(c2)["fit_signals"]  # signals identical; only content_flags differ
    assert sig(conn)["content_flags"] and not sig(c2)["content_flags"]
    c2.close()


# ------------------------------------------------------------------ get_candidates
def test_get_candidates_known_unknown_and_limits(conn):
    out = candidates.get_candidates(["C123", "C999", "C123"], conn=conn)
    assert [o["candidate_id"] for o in out] == ["C123", "C999"]  # de-duplicated, order kept
    assert out[1] == {"candidate_id": "C999", "not_found": True}
    with pytest.raises(ValueError):
        candidates.get_candidates([], conn=conn)
    with pytest.raises(ValueError):
        candidates.get_candidates([f"C{n}" for n in range(101, 122)], conn=conn)       # 21 IDs
    assert len(candidates.get_candidates([f"C{n}" for n in range(101, 121)], conn=conn)) == 20


def test_get_candidates_by_role_returns_every_active_candidate_as_a_comparison_view(conn):
    out = candidates.get_candidates(role="Backend Engineer", conn=conn)
    active = [r["candidate_id"] for r in candidates.search_candidates("ROLE-BE", conn=conn)["candidates"]]
    assert [c["candidate_id"] for c in out] == active
    assert all("stage_history" not in c and "pending_tasks" not in c and c["feedback_received"] == len(c["feedback"]) for c in out)
    by_id = candidates.get_candidates(active[:1], conn=conn)[0]
    assert {k: v for k, v in by_id.items() if k not in ("stage_history", "pending_tasks")} == out[0]
    for bad in (dict(), dict(candidate_ids=["C104"], role="ROLE-BE")):
        with pytest.raises(ValueError):
            candidates.get_candidates(conn=conn, **bad)


def test_get_candidates_skills_match_the_search_signals(conn):
    for c in candidates.get_candidates(role="ROLE-MSE", conn=conn):
        sig = next(r for r in candidates.search_candidates("ROLE-MSE", conn=conn)["candidates"] if r["candidate_id"] == c["candidate_id"])["fit_signals"]
        assert c["required_skills_missing"] == sig["required_skills_missing"]
        assert len(c["preferred_skills_matched"]) == sig["preferred_skills_matched_count"]


def test_c123_supports_demo_4(conn):
    c = candidates.get_candidates(["C123"], conn=conn)[0]
    assert c["current_stage"] == EXPECT["demo4_c123"]["stage"] == TI
    assert c["feedback_count"] == 3 and len(c["feedback"]) == 3
    latest = max(c["feedback"], key=lambda f: f["interview_at"])
    assert latest["stage"] == TI and latest["feedback_id"].startswith("FB-")
    assert c["stage_history"][-1]["event_id"].startswith("EV-")
    assert c["waiting_state"] == "awaiting_decision" and c["waiting_days"] == pytest.approx(5, abs=1e-3)
    assert c["days_in_current_stage"] == pytest.approx(13, abs=1e-3)       # a different clock from waiting_days
    peers = [r for r in candidates.search_candidates("ROLE-MSE", stage=TI, conn=conn)["candidates"]
             if r["candidate_id"] != "C123"]
    assert len(peers) == EXPECT["demo4_c123"]["active_peers_in_stage"]


def test_get_candidates_excludes_contact_details_and_links_tasks(conn):
    out = candidates.get_candidates(["C112", "C101"], conn=conn)
    assert "contact_email" not in json.dumps(out) and "@" not in json.dumps(out)
    c112 = out[0]
    assert [t["task_id"] for t in c112["pending_tasks"]] == ["T-004"] and c112["pending_tasks"][0]["overdue"]
    assert c112["feedback_count"] == 2 and c112["waiting_state"] == "awaiting_feedback"  # RR note + RS; TI missing
    assert out[1]["pending_tasks"] == []


def test_cv_is_returned_verbatim_for_injected_candidate_and_truncated_when_long(conn):
    c117 = candidates.get_candidates(["C117"], conn=conn)[0]
    assert c117["cv_text"].endswith(sc.INJECTION_PAYLOAD) and c117["cv_truncated"] is False
    old = candidates.CV_MAX_CHARS
    candidates.CV_MAX_CHARS = 50
    try:
        c = candidates.get_candidates(["C117"], conn=conn)[0]
        assert len(c["cv_text"]) == 50 and c["cv_truncated"] is True
    finally:
        candidates.CV_MAX_CHARS = old


# ------------------------------------------------------------------ communications
def test_communications(conn):
    r = candidates.get_candidate_communications("C114", conn=conn)
    assert r["total_messages"] == 5 and r["returned"] == 5
    sent = [m["sent_at"] for m in r["messages"]]
    assert sent == sorted(sent, reverse=True)  # newest first
    assert any("waiting" in m["body"].lower() for m in r["messages"])  # backs the scheduling-delay withdrawal
    assert "@" not in json.dumps(r)
    assert candidates.get_candidate_communications("C114", limit=2, conn=conn)["returned"] == 2
    for bad in (0, 11):
        with pytest.raises(ValueError):
            candidates.get_candidate_communications("C114", limit=bad, conn=conn)
    with pytest.raises(NotFound):
        candidates.get_candidate_communications("C999", conn=conn)
    for cid in ("C101", "C117", "C150"):
        assert all(len(m["body"]) <= 800 for m in candidates.get_candidate_communications(cid, conn=conn)["messages"])


# ------------------------------------------------------------------ tasks
def test_tasks_overdue_and_pending_match_expectations(conn):
    pending = tasks.get_tasks(status="pending", conn=conn)
    assert sorted(t["task_id"] for t in pending["tasks"]) == EXPECT["tasks"]["pending_ids"] == facts.pending_task_ids()
    overdue = tasks.get_tasks(overdue_only=True, conn=conn)
    assert sorted(t["task_id"] for t in overdue["tasks"]) == EXPECT["tasks"]["overdue_ids"] == facts.overdue_task_ids()
    assert all(t["status"] == "pending" for t in overdue["tasks"])  # overdue is a subset of pending
    assert all(t["days_overdue"] > 0 for t in overdue["tasks"])
    assert all(t["days_overdue"] is None for t in pending["tasks"] if not t["overdue"])


def test_task_boundaries_strictly_before_as_of(conn):
    b = EXPECT["tasks"]["boundary"]
    by_id = {t["task_id"]: t for t in tasks.get_tasks(conn=conn)["tasks"]}
    assert by_id[b["due_exactly_at_as_of"]]["overdue"] is False
    assert by_id[b["due_one_minute_after"]]["overdue"] is False
    assert by_id[b["due_one_minute_before"]]["overdue"] is True
    # moving the clock one minute forward flips the exact-boundary task
    later = {t["task_id"]: t for t in tasks.get_tasks(as_of=AS_OF + timedelta(minutes=1), conn=conn)["tasks"]}
    assert later[b["due_exactly_at_as_of"]]["overdue"] is True and later[b["due_one_minute_after"]]["overdue"] is False


def test_task_completed_and_cancelled_are_never_overdue(conn):
    for t in tasks.get_tasks(conn=conn)["tasks"]:
        if t["status"] != "pending":
            assert t["overdue"] is False


def test_task_filters_and_limits(conn):
    assert {t["role_id"] for t in tasks.get_tasks(role="ROLE-PO", conn=conn)["tasks"]} == {"ROLE-PO"}
    assert [t["task_id"] for t in tasks.get_tasks(candidate="C113", conn=conn)["tasks"]] == ["T-002", "T-005"] \
        or {t["task_id"] for t in tasks.get_tasks(candidate="C113", conn=conn)["tasks"]} == {"T-002", "T-005"}
    r = tasks.get_tasks(limit=5, conn=conn)
    assert r["returned"] == 5 and r["total_matching"] == 28
    po_overdue = tasks.get_tasks(role="ROLE-PO", overdue_only=True, conn=conn)
    assert {t["task_id"] for t in po_overdue["tasks"]} == {"T-017", "T-018", "T-019", "T-021", "T-022"}
    for bad in (dict(limit=0), dict(limit=51), dict(status="done")):
        with pytest.raises(ValueError):
            tasks.get_tasks(conn=conn, **bad)
    with pytest.raises(NotFound):
        tasks.get_tasks(role="ROLE-XX", conn=conn)
    with pytest.raises(NotFound):
        tasks.get_tasks(candidate="C999", conn=conn)


# ------------------------------------------------------------------ clock discipline
def test_every_service_function_takes_as_of_defaulting_to_the_fixed_clock():
    for fn in (candidates.list_roles, candidates.search_candidates, candidates.get_candidates,
               analytics.get_pipeline_analytics, tasks.get_tasks):
        assert inspect.signature(fn).parameters["as_of"].default == AS_OF, fn.__name__


def test_sql_is_parameterised(conn):
    """A role id containing SQL must be treated as data, not interpreted."""
    with pytest.raises(NotFound):
        analytics.get_pipeline_analytics("ROLE-BE' OR '1'='1", conn=conn)
    assert tasks.get_tasks(candidate=None, conn=conn)["total_matching"] == 28


def test_missing_database_is_an_error_not_an_empty_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        connect(tmp_path / "nope.db")
    assert not (tmp_path / "nope.db").exists()


@pytest.mark.parametrize("missing", ["duration_days", "scheduling_days", "feedback_days"])
def test_a_stage_missing_one_target_omits_only_that_block(db_file, tmp_path, missing):
    """Config robustness: a null target must disable its own metric block, not crash the others."""
    path = tmp_path / "partial.db"
    shutil.copy(db_file, path)
    c = connect(path)
    t = json.loads(c.execute("SELECT stage_targets_json FROM roles WHERE role_id='ROLE-PO'").fetchone()[0])
    t[HMI][missing] = None
    c.execute("UPDATE roles SET stage_targets_json=? WHERE role_id='ROLE-PO'", (json.dumps(t),))
    c.commit()
    full = stage_of("ROLE-PO", HMI, connect(db_file))
    s = stage_of("ROLE-PO", HMI, c)
    block = {"duration_days": "duration", "scheduling_days": "scheduling", "feedback_days": "feedback"}
    for tgt, name in block.items():
        assert s[name] == (None if tgt == missing else full[name]), (missing, name)
    sig = candidates.search_candidates("ROLE-PO", conn=c)  # fit signals tolerate it too
    assert sig["returned"] > 0
    if missing == "duration_days":
        row = next(r for r in sig["candidates"] if r["candidate_id"] == "C148")
        assert row["fit_signals"]["over_duration_target"] is None
    c.close()


def test_search_flags_only_the_injected_cv_and_never_returns_matched_text(conn, db_file, tmp_path):
    flagged = {}
    for role in ROLES:
        for status in ("active", "rejected", "withdrawn"):
            for r in candidates.search_candidates(role, status=status, conn=conn)["candidates"]:
                if r["content_flags"]:
                    flagged[r["candidate_id"]] = r["content_flags"]
    assert list(flagged) == ["C117"]
    assert {f["field"] for f in flagged["C117"]} == {"cv_text"}
    assert {f["pattern_id"] for f in flagged["C117"]} >= {"system_override", "ignore_instructions", "rank_candidate", "concealment"}
    assert "SYSTEM" not in json.dumps(flagged) and "Ignore all" not in json.dumps(flagged)
    base = connect(build(tmp_path / "b.db", "baseline"))  # same data without the payload: zero flags anywhere
    for role in ROLES:
        for status in ("active", "rejected", "withdrawn"):
            assert all(not r["content_flags"] for r in candidates.search_candidates(role, status=status, conn=base)["candidates"])
    base.close()


def test_get_candidates_days_in_stage_matches_search_and_is_null_when_not_active(conn):
    search = {r["candidate_id"]: r for r in candidates.search_candidates("ROLE-MSE", conn=conn)["candidates"]}
    ids = list(search)
    for c in candidates.get_candidates(ids[:6], conn=conn) + candidates.get_candidates(ids[6:], conn=conn):
        assert c["days_in_current_stage"] == pytest.approx(search[c["candidate_id"]]["days_in_current_stage"], abs=1e-3)
    assert candidates.get_candidates(["C132"], conn=conn)[0]["days_in_current_stage"] is None   # rejected at Recruiter Screen


def test_search_hands_the_boundary_a_raw_excerpt_only_for_flagged_fields(conn):
    rows = {r["candidate_id"]: r for r in candidates.search_candidates("ROLE-BE", conn=conn)["candidates"]}
    assert rows["C104"]["flag_sources"] == []
    (src,) = rows["C117"]["flag_sources"]
    assert src["field"] == "cv_text" and src["location"] == "near the end"       # the payload ends a ~550-character CV
    assert src["excerpt"] == sc.INJECTION_PAYLOAD                                  # the whole payload paragraph, nothing else
    assert "asked to be ranked first" in src["intents"] and "asked for the instructions to be kept hidden" in src["intents"]


def test_task_rows_carry_readable_names_and_role_level_tasks_carry_none(conn):
    by_id = {t["task_id"]: t for t in tasks.get_tasks(conn=conn)["tasks"]}
    assert (by_id["T-002"]["candidate_name"], by_id["T-002"]["role_title"]) == ("Mateo Villareal", "Backend Engineer")
    assert (by_id["T-017"]["candidate_name"], by_id["T-017"]["role_title"]) == ("Valentina Roos", "People Operations Specialist")
    assert by_id["T-028"]["candidate_id"] is None and by_id["T-028"]["candidate_name"] is None and by_id["T-028"]["role_title"] == "People Operations Specialist"
    c112 = candidates.get_candidates(["C112"], conn=conn)[0]["pending_tasks"][0]
    assert c112["candidate_name"] == "Lena Sorensen" and c112["role_title"] == "Backend Engineer"


def test_interview_evidence_matches_independent_recompute(conn):
    """Completed interviews, feedback received and feedback pending are separate counts, recomputed here straight from the tables."""
    seen = 0
    for role in ROLES:
        for status in ("active", "rejected", "withdrawn"):
            for r in candidates.search_candidates(role, status=status, conn=conn)["candidates"]:
                cid, sig = r["candidate_id"], r["fit_signals"]
                held = [e["stage"] for e in conn.execute("SELECT stage, interview_at FROM stage_events WHERE candidate_id = ?", (cid,))
                        if e["interview_at"] and e["interview_at"] <= AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ")]
                fed = {f["stage"] for f in conn.execute("SELECT stage FROM interview_feedback WHERE candidate_id = ?", (cid,))}
                n_fb = conn.execute("SELECT COUNT(*) FROM interview_feedback WHERE candidate_id = ?", (cid,)).fetchone()[0]
                assert sig["interviews_completed"] == len(held)
                assert sig["feedback_received"] == sig["feedback_count"] == n_fb
                assert sig["feedback_pending"] == sum(s not in fed for s in held)
                assert sig["stages_with_feedback"] == len(fed)
                full = candidates.get_candidates([cid], conn=conn)[0]
                assert {k: full[k] for k in ("interviews_completed", "feedback_received", "feedback_pending", "stages_with_feedback")} == \
                       {k: sig[k] for k in ("interviews_completed", "feedback_received", "feedback_pending", "stages_with_feedback")}
                seen += sig["feedback_pending"] > 0
    assert seen, "dataset should contain at least one candidate with feedback still pending"


def _expected_status(conn, cid):
    """Stage status rebuilt from the raw tables with its own date logic, not by calling the service."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def day(iso):
        d = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Los_Angeles"))
        return d.strftime("%b ") + str(d.day)

    now = AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ")
    status = conn.execute("SELECT application_status FROM candidates WHERE candidate_id = ?", (cid,)).fetchone()[0]
    e = conn.execute("SELECT * FROM stage_events WHERE candidate_id = ? ORDER BY entered_at DESC, event_id DESC", (cid,)).fetchone()
    subs = [r[0] for r in conn.execute("SELECT submitted_at FROM interview_feedback WHERE candidate_id = ? AND stage = ?", (cid, e["stage"]))]
    if status != "active":
        return f"{status.capitalize()} at {e['stage']}" + (f" on {day(e['exited_at'])}." if e["exited_at"] else ".")
    entered = datetime.strptime(e["entered_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
    n = round((AS_OF - entered).total_seconds() / 86400)
    head = f"{e['stage']} since {day(e['entered_at'])} (" + ("under a day" if n < 1 else f"{n} day{'s' if n != 1 else ''}") + ")"
    if e["stage"] == "Resume Review":
        return f"{head}: " + ("review note on file." if subs else "no review note yet.")
    if not e["scheduled_at"]:
        return f"{head}: interview not booked yet."
    if not e["interview_at"] or e["interview_at"] > now:
        return f"{head}: interview booked on {day(e['scheduled_at'])} but not held yet (no interview date is recorded)."
    return f"{head}: interview held {day(e['interview_at'])}; " + (f"feedback received {day(max(subs))}." if subs else "feedback not received yet.")


def test_stage_status_matches_independent_recompute_for_every_candidate(conn):
    kinds, n = set(), 0
    for cid in [r[0] for r in conn.execute("SELECT candidate_id FROM candidates ORDER BY candidate_id")]:
        want = _expected_status(conn, cid)
        got = candidates.get_candidates([cid], conn=conn)[0]["stage_status"]
        assert got == want, cid
        kinds.add(("booked" if "booked" in got else "held" if "held" in got else "other"))
        n += 1
    assert n >= 40 and {"booked", "held"} <= kinds
    rows_ = [r for role in ROLES for s in ("active", "rejected", "withdrawn") for r in candidates.search_candidates(role, status=s, conn=conn)["candidates"]]
    assert all(r["stage_status"] == _expected_status(conn, r["candidate_id"]) for r in rows_)


def test_hannelore_final_interview_is_booked_not_held(conn):
    s = candidates.get_candidates(["C108"], conn=conn)[0]["stage_status"]
    assert s.startswith("Final Interview since ") and s.endswith(": interview booked on Nov 3 but not held yet (no interview date is recorded).")


def test_mateo_status_says_where_his_22_days_come_from(conn):
    s = candidates.get_candidates(["C113"], conn=conn)[0]["stage_status"]
    assert s == "Technical Interview since Oct 16 (22 days): interview not booked yet."


def test_stage_history_summaries_are_dated_sentences(conn):
    hist = candidates.get_candidates(["C113"], conn=conn)[0]["stage_history"]
    assert [h["summary"] for h in hist] == ["Resume Review, Oct 10 to Oct 12: advanced.",
                                            "Recruiter Screen, Oct 12 to Oct 16: interview held Oct 14; advanced.",
                                            "Technical Interview, Oct 16 to now: interview not booked yet."]


def test_recruiting_overview_matches_independent_counts(conn):
    from services import overview
    o = overview.get_recruiting_overview(conn=conn)
    now = AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert o["overdue_tasks_total"] == conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'pending' AND due_at < ?", (now,)).fetchone()[0] == 10
    for r in o["roles"]:
        rid = r["role_id"]
        act = conn.execute("SELECT COUNT(*) FROM candidates WHERE role_id = ? AND application_status = 'active'", (rid,)).fetchone()[0]
        unbooked = conn.execute("""SELECT COUNT(*) FROM candidates c JOIN stage_events e ON e.candidate_id = c.candidate_id AND e.exited_at IS NULL
                                   WHERE c.role_id = ? AND c.application_status = 'active' AND e.stage != 'Resume Review' AND e.scheduled_at IS NULL""", (rid,)).fetchone()[0]
        no_fb = conn.execute("""SELECT COUNT(*) FROM candidates c JOIN stage_events e ON e.candidate_id = c.candidate_id AND e.exited_at IS NULL
                                WHERE c.role_id = ? AND c.application_status = 'active' AND e.interview_at IS NOT NULL
                                AND NOT EXISTS (SELECT 1 FROM interview_feedback f WHERE f.candidate_id = c.candidate_id AND f.stage = e.stage)""", (rid,)).fetchone()[0]
        overdue = conn.execute("SELECT COUNT(*) FROM tasks WHERE role_id = ? AND status = 'pending' AND due_at < ?", (rid, now)).fetchone()[0]
        assert (r["active_candidates"], r["waiting_to_be_scheduled"], r["waiting_for_feedback"], r["overdue_tasks"]) == (act, unbooked, no_fb, overdue)
    assert sum(r["overdue_tasks"] for r in o["roles"]) <= o["overdue_tasks_total"]
    sched, feedback = o["waiting_to_be_scheduled"], o["waiting_for_feedback"]
    for group in (sched, o["interview_booked_not_held"], feedback):
        days = [w["waiting_days"] for w in group]
        assert days == sorted(days, reverse=True)
    assert sched[0]["name"] == "Mateo Villareal" and round(sched[0]["waiting_days"]) == 22
    assert sorted(w["name"] for w in feedback) == ["Lena Sorensen", "Valentina Roos", "Wyatt Nakamura", "Xiomara Lindholm", "Yara Eklund"]
    everyone = sched + o["interview_booked_not_held"] + feedback
    assert len(everyone) == len({w["candidate_id"] for w in everyone})
    assert all(sum(r["active_by_stage"].values()) == r["active_candidates"] for r in o["roles"])
    assert "other_active" not in o                     # everyone else is only counted; search_candidates lists them
    assert [len(g) for g in (sched, o["interview_booked_not_held"], feedback)] == [sum(r[k] for r in o["roles"]) for k in ("waiting_to_be_scheduled", "interview_booked_not_held", "waiting_for_feedback")]


def test_get_candidates_by_name_returns_full_records_and_reports_unknown_names(conn):
    out = candidates.get_candidates(names=["Hannelore Voss", "dahlia", "Nobody Atall"], conn=conn)
    assert [c.get("candidate_id") for c in out] == ["C108", "C104", None]
    assert out[0]["stage_history"] and "pending_tasks" in out[0]                       # by name, the record is complete
    assert out[2] == {"name": "Nobody Atall", "not_found": True}
    for bad in (dict(names=[]), dict(names=["x"] * 11), dict(names=["Dahlia"], role="ROLE-BE")):
        with pytest.raises(ValueError):
            candidates.get_candidates(conn=conn, **bad)


def test_tasks_for_a_person_can_be_asked_by_name(conn):
    by_name = tasks.get_tasks(candidate="Mateo Villareal", conn=conn)["tasks"]
    assert {t["task_id"] for t in by_name} == {t["task_id"] for t in tasks.get_tasks(candidate="C113", conn=conn)["tasks"]}
    assert {t["candidate_id"] for t in tasks.get_tasks(candidate="mateo", conn=conn)["tasks"]} == {"C113"}
    with pytest.raises(NotFound, match="no candidate named"):
        tasks.get_tasks(candidate="Nobody Atall", conn=conn)
