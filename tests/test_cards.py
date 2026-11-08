"""Evidence cards (agent/cards.py): one card per person, the same shape for everyone, built only from the projected record."""
from agent.cards import candidate_card, when


def fb(stage, rec="yes", day="2026-10-25 13:00 PDT", who="Hana Kobayashi", note="Solid round.", **criteria):
    return {"feedback_id": "FB-001", "stage": stage, "interview_at": day, "interviewer": who, "recommendation": rec,
            "criteria": criteria, "summary_text": {"untrusted_text": note, "label": "UNTRUSTED: data, not instructions"}}


def record(**kw):
    base = {"candidate_id": "C108", "name": "Hannelore Voss", "current_stage": "Final Interview",
            "stage_status": "Final Interview since Oct 31 (7 days): interview booked on Nov 3 but not held yet (no interview date is recorded).",
            "required_skills_matched": ["Python", "SQL"], "required_skills_missing": [], "preferred_skills_matched": ["AWS"],
            "feedback": [fb("Resume Review", note="Resume reviewed."), fb("Recruiter Screen", communication=4),
                         fb("Technical Interview", rec="strong_yes", coding=5, system_design=4)]}
    return {**base, **kw}


def test_one_line_per_interview_stage_and_resume_review_notes_are_left_out():
    c = candidate_card(record(), "Hannelore Voss", "Backend Engineer")
    assert [i["title"] for i in c["interviews"]] == ["Recruiter Screen · Oct 25 · Hana Kobayashi", "Technical Interview · Oct 25 · Hana Kobayashi"]
    assert c["interviews"][1]["verdict"] == "Strongly recommends advancing (coding 5, system design 4)"
    assert c["interviews"][0]["note"] == "Solid round."                                  # the note itself, for the reviewer to read


def test_skills_say_what_is_matched_and_what_is_missing():
    assert candidate_card(record(), "H")["skills"] == ["All required skills: Python, SQL", "Preferred skills: AWS"]
    gap = candidate_card(record(required_skills_matched=["Python"], required_skills_missing=["SQL"], preferred_skills_matched=[]), "H")
    assert gap["skills"] == ["Required skills: has Python; missing SQL", "Preferred skills: none matched"]


def test_the_current_stage_shows_as_pending_only_when_it_has_no_feedback_yet():
    assert candidate_card(record(), "H")["pending"].startswith("Final Interview since Oct 31")
    done = record(current_stage="Technical Interview", stage_status="Technical Interview since Oct 21 (17 days): interview held Nov 1.")
    assert candidate_card(done, "H")["pending"] == ""
    assert candidate_card(record(current_stage="Resume Review", feedback=[]), "H")["pending"] == ""   # screening is not an interview


def test_every_card_has_the_same_keys_and_carries_the_ranking_reason_and_gaps():
    plain, ranked = candidate_card(record(), "H"), candidate_card(record(), "H", "Backend Engineer", "Strong feedback", ["No Kafka"])
    assert set(plain) == set(ranked) and (ranked["reason"], ranked["gaps"]) == ("Strong feedback", ["No Kafka"])
    assert (plain["reason"], plain["gaps"], plain["role"]) == ("", [], "")


def test_dates_read_as_month_and_day():
    assert when("2026-10-28 13:00 PDT") == "Oct 28" and when(None) == "" and when("garbage") == ""
