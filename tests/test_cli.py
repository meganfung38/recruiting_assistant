"""CLI rendering of the checked answer (no model, no key)."""
from agent.cli import format_result, main
from tests.test_agent import ARef, LAG, cand_arg, fig_arg, say, sorch, submit, call


def run(steps):
    o, _, _ = sorch(steps)
    return o.run_turn("q")


def test_structured_answer_shows_candidates_figures_and_caveats():
    r = run([call(("1", "get_candidates", {"candidate_ids": ["C101"]})), call(("2", "get_pipeline_analytics", {"role_id": "ROLE-BE"})),
             submit(answer_markdown="Pick C101 [cv:C101]. Lag 8.0 [analytics:ROLE-BE@2026-11-07].", candidates=[cand_arg()],
                    figures=[fig_arg(8.0)], uncertainties=["Small sample"], content_warnings=["C117 CV had instruction-like text"])])
    text = format_result(r)
    assert "Candidate C101" in text and f"[verified] Median scheduling lag (days) = 8  ({ARef} {LAG})" in text
    assert "Uncertainty: Small sample" in text and "Content warning (from the model)" in text
    assert "-- Tools used (2)" in text and "UNVERIFIED" not in text and "corrected once" not in text


def test_unverified_items_the_repair_note_and_unchecked_banner_are_visible():
    bad = lambda sid: submit(sid, answer_markdown="x [feedback:FB-999]", figures=[fig_arg(9.5)])
    r = run([call(("1", "get_pipeline_analytics", {"role_id": "ROLE-BE"})), bad("a"), bad("b")])
    text = format_result(r)
    assert "[UNVERIFIED]" in text and "the data says 8.0, not 9.5" in text and "FB-999 ⚠ unverified" in text
    assert "could not be verified" in text and "corrected once after validation: unsupported_citation, unverified_figure" in text
    plain = format_result(run([say("Plain answer.")]))
    assert "not machine-checked" in plain


def test_missing_database_and_missing_model_are_clear_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert main(["q", "--db", str(tmp_path / "nope.db")]) == 2
    assert "python -m data.seed" in capsys.readouterr().err
