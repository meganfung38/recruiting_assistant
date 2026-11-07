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
def test_list_and_get_role(conn):
    roles = {r["role_id"]: r for r in candidates.list_roles(conn=conn)}
    assert {k: v["role_age_days"] for k, v in roles.items()} == EXPECT["role_age_days"]
    r = candidates.get_role("ROLE-BE", conn=conn)
    assert r["stage_order"] == ["Resume Review", "Recruiter Screen", "Technical Interview", "Final Interview"]
    assert r["stage_targets"][TI] == {"duration_days": 14, "scheduling_days": 7, "feedback_days": 2}
    assert [s["name"] for s in r["requirements"]["required_skills"]][0] == "Python"
    with pytest.raises(NotFound):
        candidates.get_role("ROLE-XX", conn=conn)


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
        assert set(c) == {"candidate_id", "name", "current_stage", "status", "waiting_state", "waiting_days",
                          "days_in_current_stage", "fit_signals"}
        assert "cv_text" not in json.dumps(c) and "@" not in json.dumps(c)
    assert candidates.search_candidates("ROLE-BE", stage=TI, conn=conn)["returned"] == 5
    assert candidates.search_candidates("ROLE-BE", status="withdrawn", conn=conn)["total_matching"] == 6
    assert candidates.search_candidates("ROLE-BE", limit=3, conn=conn)["returned"] == 3
    assert candidates.search_candidates("ROLE-BE", limit=3, conn=conn)["total_matching"] == 8
    for bad in (dict(limit=21), dict(limit=0), dict(status="hired"), dict(stage="Offer")):
        with pytest.raises(ValueError):
            candidates.search_candidates("ROLE-BE", conn=conn, **bad)
    with pytest.raises(NotFound):
        candidates.search_candidates("ROLE-XX", conn=conn)


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
    assert sig(conn) == sig(c2)
    c2.close()


# ------------------------------------------------------------------ get_candidates
def test_get_candidates_known_unknown_and_limits(conn):
    out = candidates.get_candidates(["C123", "C999", "C123"], conn=conn)
    assert [o["candidate_id"] for o in out] == ["C123", "C999"]  # de-duplicated, order kept
    assert out[1] == {"candidate_id": "C999", "not_found": True}
    with pytest.raises(ValueError):
        candidates.get_candidates([], conn=conn)
    with pytest.raises(ValueError):
        candidates.get_candidates([f"C{n}" for n in range(101, 108)], conn=conn)


def test_c123_supports_demo_4(conn):
    c = candidates.get_candidates(["C123"], conn=conn)[0]
    assert c["current_stage"] == EXPECT["demo4_c123"]["stage"] == TI
    assert c["feedback_count"] == 3 and len(c["feedback"]) == 3
    latest = max(c["feedback"], key=lambda f: f["interview_at"])
    assert latest["stage"] == TI and latest["feedback_id"].startswith("FB-")
    assert c["stage_history"][-1]["event_id"].startswith("EV-")
    assert c["waiting_state"] == "awaiting_decision" and c["waiting_days"] == pytest.approx(5, abs=1e-3)
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
    assert {t["role_id"] for t in tasks.get_tasks(role_id="ROLE-PO", conn=conn)["tasks"]} == {"ROLE-PO"}
    assert [t["task_id"] for t in tasks.get_tasks(candidate_id="C113", conn=conn)["tasks"]] == ["T-002", "T-005"] \
        or {t["task_id"] for t in tasks.get_tasks(candidate_id="C113", conn=conn)["tasks"]} == {"T-002", "T-005"}
    r = tasks.get_tasks(limit=5, conn=conn)
    assert r["returned"] == 5 and r["total_matching"] == 28
    po_overdue = tasks.get_tasks(role_id="ROLE-PO", overdue_only=True, conn=conn)
    assert {t["task_id"] for t in po_overdue["tasks"]} == {"T-017", "T-018", "T-019", "T-021", "T-022"}
    for bad in (dict(limit=0), dict(limit=51), dict(status="done")):
        with pytest.raises(ValueError):
            tasks.get_tasks(conn=conn, **bad)
    with pytest.raises(NotFound):
        tasks.get_tasks(role_id="ROLE-XX", conn=conn)
    with pytest.raises(NotFound):
        tasks.get_tasks(candidate_id="C999", conn=conn)


# ------------------------------------------------------------------ clock discipline
def test_every_service_function_takes_as_of_defaulting_to_the_fixed_clock():
    for fn in (candidates.list_roles, candidates.get_role, candidates.search_candidates, candidates.get_candidates,
               analytics.get_pipeline_analytics, tasks.get_tasks):
        assert inspect.signature(fn).parameters["as_of"].default == AS_OF, fn.__name__


def test_sql_is_parameterised(conn):
    """A role id containing SQL must be treated as data, not interpreted."""
    with pytest.raises(NotFound):
        analytics.get_pipeline_analytics("ROLE-BE' OR '1'='1", conn=conn)
    assert tasks.get_tasks(candidate_id=None, conn=conn)["total_matching"] == 28


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
