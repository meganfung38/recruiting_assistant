"""CLI rendering of the checked answer (no model, no key)."""
from agent.cli import format_result, main
from tests.test_agent import cand_arg, say, sorch, submit, call


def run(steps):
    o, _, _ = sorch(steps)
    return o.run_turn("q")


def test_structured_answer_shows_who_it_is_about_the_ranking_and_the_round_trace():
    r = run([call(("1", "get_candidates", {"candidate_ids": ["C101"]})), call(("2", "get_pipeline_analytics", {"role": "ROLE-BE"})),
             submit(answer_markdown="Pick N N. Backend Engineer lag is 8 days.", ranking=[cand_arg()], uncertainties=["Small sample"])])
    text = format_result(r)
    assert "About: C101, ROLE-BE" in text and "1. C101: Strong" in text and "Uncertainty: Small sample" in text
    assert "How this answer was made · 3 round(s) · 2 tool call(s)" in text and "NOT MATCHED" not in text and "corrected once" not in text
    assert 'get_candidates(candidate_ids=["C101"])' in text and "Submitted the answer -> accepted" in text


def test_unmatched_numbers_and_the_repair_note_are_visible():
    bad = lambda sid: submit(sid, answer_markdown="Backend Engineer lag is 9.5 days.")
    text = format_result(run([call(("1", "get_pipeline_analytics", {"role": "ROLE-BE"})), bad("a"), bad("b")]))
    assert "[NOT MATCHED] The number 9.5" in text and "corrected once after validation: unsupported_number" in text


def test_missing_database_and_missing_model_are_clear_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert main(["q", "--db", str(tmp_path / "nope.db")]) == 2
    assert "python -m data.seed" in capsys.readouterr().err
