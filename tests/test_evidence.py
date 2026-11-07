"""Evidence registry: built from structured fields of successful calls only."""
from agent.evidence import EvidenceRegistry, _candidate_ids, aggregates


def env(data=None, refs=()):
    return {"data": data, "as_of": "x", "source_refs": list(refs), "warnings": [], "content_flags": [], "error": None}


def test_registers_valid_refs_and_candidates():
    reg = EvidenceRegistry()
    reg.add("get_candidates", env({"candidates": [{"candidate_id": "C104"}, {"candidate_id": "C999", "not_found": True}]},
                                  ["cv:C104", "feedback:FB-031"]))
    assert reg.has("cv:C104") and reg.has("feedback:FB-031") and not reg.has("feedback:FB-999")
    assert reg.candidate_seen("C104") and not reg.candidate_seen("C999")  # not-found stubs are not evidence
    assert reg.refs() == ["cv:C104", "feedback:FB-031"]


def test_malformed_refs_are_ignored():
    reg = EvidenceRegistry()
    reg.add("get_tasks", env({}, ["task:T-001", "task:T-1", "bogus:1", "feedback:FB-001; ignore previous", "cv:C1234"]))
    assert reg.refs() == ["task:T-001"]


def test_untrusted_text_is_never_walked_for_ids():
    node = {"candidates": [{"candidate_id": "C117", "cv_text": {"untrusted_text": "see C555 and candidate_id C556",
                                                                  "label": "x"}}]}
    assert _candidate_ids(node) == {"C117"}
    sneaky = {"cv_text": {"untrusted_text": "x", "candidate_id": "C777"}}
    assert _candidate_ids(sneaky) == set()


def test_raw_aggregates_for_analytics_and_tasks_only():
    raw = {"data": {"stages": [{"scheduling": {"over_target_count": 14}}]}, "source_refs": ["analytics:ROLE-BE@2026-11-07"]}
    assert aggregates("get_pipeline_analytics", raw) == {"analytics:ROLE-BE@2026-11-07": raw["data"]}
    t = {"data": {"tasks": [{"task_id": "T-001", "overdue": True}]}, "source_refs": ["task:T-001"]}
    assert aggregates("get_tasks", t) == {"task:T-001": {"task_id": "T-001", "overdue": True}}
    assert aggregates("get_candidates", {"data": {"candidates": []}, "source_refs": []}) == {}
    reg = EvidenceRegistry()
    reg.add("get_pipeline_analytics", env({}, ["analytics:ROLE-BE@2026-11-07"]), aggregates("get_pipeline_analytics", raw))
    assert reg.raw("analytics:ROLE-BE@2026-11-07")["stages"][0]["scheduling"]["over_target_count"] == 14
    assert reg.raw("analytics:ROLE-XX@2026-11-07") is None
