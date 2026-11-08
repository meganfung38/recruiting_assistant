"""One-off composer for the interviewer notes in data/prose/feedback.json (run by hand; seeding never calls it).

Why: the first notes were assembled from a bank of about 60 sentences, so different candidates had word-for-word identical notes
("Drew clear service boundaries..." for both Dahlia Ramanathan and Junko Takeda), which made evidence look copied. Each note is now
built from that feedback record's own facts: stage, date, interviewer, the score on each criterion, the recommendation, and the
candidate's CV (employer, skills, a missing required skill). Phrasing varies by record. Resume Review notes are left as they are.

    python -m data.compose_feedback          # rewrites the interview notes in data/prose/feedback.json, then re-run the seed

The consistency checker (data/check_consistency.py) still requires the interview date, the stage name and "Recommendation: <rec>."
"""
import hashlib
import json
import re
from pathlib import Path

from data import scenarios as sc
from services.clock import pacific_date

PROSE = Path(__file__).resolve().parent / "prose"

# Per criterion and score band: (what was observed). {skill}, {company}, {gap} are filled from the candidate's CV.
BANK = {
    "system_design": {
        "high": ["Sketched a design for the problem in a few minutes and explained each trade-off before being asked.",
                 "Broke the design into clear services, named the failure modes and chose {skill} for a reason they could defend.",
                 "Designed for failure from the start: retries, idempotency and back-pressure all came up without prompting.",
                 "Strong design: started from the data model and scaled it out cleanly when the load was raised."],
        "good": ["Reached a sound design, though the data model needed one round of questions to firm up.",
                 "Solid design overall; leaned on patterns from {company} and adjusted them sensibly when challenged."],
        "ok": ["The design worked for the happy path but did not consider load or failure until prompted.",
               "Reasonable first design; struggled to say how it would change at ten times the traffic."],
        "low": ["Could not settle on a structure for the design and needed repeated hints.",
                "The design missed the core consistency problem even after it was pointed out."]},
    "coding": {
        "high": ["Wrote working, tested code quickly and caught an off-by-one before running it.",
                 "Clean, idiomatic implementation; used {skill} confidently and covered the edge cases unprompted.",
                 "Finished both exercises early and refactored the first one for readability.",
                 "Fluent with {skill}; wrote the tests first and the code passed them on the first run."],
        "good": ["Got to a correct solution with small slips that they fixed themselves.",
                 "Readable code and sensible tests; a little slow on the second exercise.",
                 "Correct solution; tests covered the main cases but not the empty input.",
                 "Comfortable with {skill}; needed a hint on the last part of the exercise."],
        "ok": ["Reached a working solution only with some prompting; tests were thin.",
               "Code ran but had a bug in the error path that they did not spot."],
        "low": ["Did not finish the exercise and several correctness issues went unnoticed.",
                "Struggled with the basics of the exercise{gap_clause}."]},
    "controls_design": {
        "high": ["Laid out a control loop with clear interlocks and explained the safety cases first, citing their work at {company}.",
                 "Strong controls design: sensible state machine, alarms and a recovery plan, with {skill} used where it fits."],
        "good": ["Good controls design overall; the alarm handling needed one follow-up question.",
                 "Sound sequencing logic; drew on {company} experience and adjusted it to the line in the exercise."],
        "ok": ["The sequence worked but recovery after a fault was left vague.",
               "Covered the normal flow; had to be prompted about interlocks."],
        "low": ["Could not produce a workable sequence and missed the safety interlocks.",
                "The design skipped fault handling entirely{gap_clause}."]},
    "technical_depth": {
        "high": ["Went deep on {skill} and answered every follow-up with specifics from {company}.",
                 "Explained the root cause of a past line failure precisely and what they changed afterwards."],
        "good": ["Good depth on {skill}; a little general on data collection from the floor.",
                 "Knew the material well and gave concrete numbers from their own projects."],
        "ok": ["Comfortable with the basics; answers stayed general when pushed for detail.",
               "Some depth on {skill}, less on the integration side."],
        "low": ["Answers stayed at the surface and examples were thin{gap_clause}.",
                "Could not explain the systems on their own CV in any depth."]},
    "stakeholder_management": {
        "high": ["Described handling a difficult manager conversation at {company} with tact and a clear outcome.",
                 "Excellent judgement on competing requests from leaders; prioritised and explained why."],
        "good": ["Handled the stakeholder scenario well; could be firmer when pushed back on.",
                 "Good examples of keeping managers informed during a change at {company}."],
        "ok": ["Reasonable instincts with stakeholders but examples were generic.",
               "Handled the scenario, though they agreed to every request without weighing them."],
        "low": ["Struggled to describe how they would manage a disagreement with a leader.",
                "Examples of working with stakeholders were vague{gap_clause}."]},
    "process_design": {
        "high": ["Designed a clear onboarding process in the exercise, with owners and checkpoints, informed by {skill} work at {company}.",
                 "Strong process thinking: mapped the steps, the failure points and how they would measure it."],
        "good": ["Good process design; measurement was an afterthought.",
                 "Sensible process with clear owners; would benefit from more automation."],
        "ok": ["The process covered the basics but had gaps at the hand-offs.",
               "A workable process that relied too much on manual follow-up."],
        "low": ["The process they described had no clear owners or checkpoints.",
                "Could not turn the scenario into a process{gap_clause}."]},
    "communication": {
        "high": ["Very clear communicator who checked understanding along the way.",
                 "Articulate and concise; summarised their reasoning well at the end.",
                 "Explained complex points simply and invited questions.",
                 "Easy to follow throughout and precise with terms."],
        "good": ["Communicated clearly, if occasionally at length.",
                 "Clear and well organised answers.",
                 "Explained their thinking well; a couple of answers ran long.",
                 "Good listener; answers were clear once they settled in.",
                 "Clear explanations, though they skipped over a few details."],
        "ok": ["Communication was adequate but answers wandered.",
               "Sometimes hard to follow; needed to be asked to summarise.",
               "Answers were understandable but rarely concise."],
        "low": ["Hard to follow and gave few concrete examples.",
                "Answers were unclear and often off the question."]},
}
VERDICT = {
    "strong_yes": ["One of the strongest candidates seen for this role.", "Clear hire signal from this round.",
                   "Would be glad to have them on the team.", "Strongly recommend moving to the next step."],
    "yes": ["Meets the bar for this stage.", "Comfortable moving them forward.", "A solid round; recommend advancing.",
            "Good fit for the level; happy to see them progress."],
    "mixed": ["Mixed round: strengths and real gaps.", "Borderline for this stage."],
    "no": ["Does not meet the bar at this stage.", "Not ready for this role."],
    "strong_no": ["Well below the bar for this role.", "Clear no from this round."],
}
SCREEN = {  # Recruiter Screen: communication only, plus logistics in the candidate's own terms
    "yes": ["Motivated by the {title} role and gave a clear account of their time at {company}.",
            "Explained why they are leaving {company} and what they want next; expectations fit the role."],
    "mixed": ["Interest in the role is unclear and compensation expectations may be above the range.",
              "Gave only a vague account of their work at {company}."],
}


def _band(score: int) -> str:
    return "high" if score >= 5 else "good" if score == 4 else "ok" if score == 3 else "low"


def _pick(options: list[str], key: str) -> str:
    return options[int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(options)]


def _cv_facts(cv: str, role: dict) -> dict:
    jobs = re.findall(r"^- ([^(\n]+?) \(\d{4}[^)]*\): (.+)$", cv, re.M)
    companies = [j[0] for j in jobs] or ["their last employer"]
    skills = [s.strip() for s in (re.search(r"Skills: (.+)", cv).group(1).split(",") if re.search(r"Skills: (.+)", cv) else [])]
    req = [s["name"] for s in role["requirements"]["required_skills"]]
    missing = [s for s in req if s.lower() not in cv.lower()]
    return {"companies": companies, "jobs": jobs, "skills": skills or req, "missing": missing}


def _example(facts: dict, f: dict) -> str:
    """One sentence tied to this candidate's own CV, so no two notes read alike."""
    jobs = facts["jobs"] or [("their last employer", "")]
    company, what = jobs[0 if f["stage"] != sc.FI else min(1, len(jobs) - 1)]
    what = what[:1].lower() + what[1:].rstrip(".") if what else ""
    good = f["recommendation"] in ("yes", "strong_yes")
    if not what:
        return f"Their main example came from {company}."
    return (f"Their main example came from {company}, where the CV says they {what}." if good and re.match(r"^[a-z]+ed\b", what) else
            f"Their main example came from {company} ({what})." if good else
            f"Their main example from {company} did not hold up well under follow-up questions.")


def compose(f: dict, cv: str) -> str:
    role = sc.ROLE_BY_ID[sc.role_of(f["candidate_id"])]
    facts = _cv_facts(cv, role)
    key = f["candidate_id"] + f["stage"]
    fill = {"company": facts["companies"][0 if f["stage"] != sc.FI else -1], "title": role["title"],
            "skill": _pick(facts["skills"], key + "skill"),
            "gap_clause": f", and had no {facts['missing'][0]} experience to draw on" if facts["missing"] else ""}
    when = pacific_date(f["interview_at"])
    if f["stage"] == sc.RS:
        head = f"Recruiter screen held on {when} ({f['interviewer']})."
        body = [_pick(SCREEN["yes" if f["recommendation"] in ("yes", "strong_yes") else "mixed"], key).format(**fill),
                _pick(BANK["communication"][_band(f["criteria"].get("communication", 3))], key + "c")]
    else:
        head = f"{f['stage']} held on {when} (interviewer: {f['interviewer']})."
        body = [_pick(BANK[c][_band(v)], key + c).format(**fill) for c, v in f["criteria"].items()]
        body.insert(1, _example(facts, f))
    return " ".join([head, *body, _pick(VERDICT[f["recommendation"]], key + "v"), f"Recommendation: {f['recommendation']}."])


def main() -> None:
    cvs = json.loads((PROSE / "cv.json").read_text())
    path = PROSE / "feedback.json"
    notes = json.loads(path.read_text())
    for f in sc.FEEDBACK:
        if f["stage"] != sc.RR:
            notes[f["feedback_id"]] = compose(f, cvs[f["candidate_id"]])
    path.write_text(json.dumps(notes, indent=1, ensure_ascii=False) + "\n")
    bodies = [v.split(").", 1)[1] for k, v in notes.items() if not v.startswith("Resume reviewed")]
    print(f"wrote {path}: {len(bodies)} interview notes, {len(set(bodies))} distinct")


if __name__ == "__main__":
    main()
