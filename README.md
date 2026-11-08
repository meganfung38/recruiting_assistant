# Recruiting Assistant

Recruiter-facing assistant over a **fully fictional** recruiting dataset (see `project-spec.md`).
Dataset, services, MCP server, trusted executor, validated answers, CLI, Streamlit UI and evals are in place. Design rationale is in
`docs/design_notes.md`, AI use in `docs/ai_use.md`, eval results in `docs/eval_results.md`.

All people, emails (`example.com`) and companies are synthetic.
The fixed as-of clock is **2026-11-07 12:00 PST (20:00 UTC)**; timestamps are stored in UTC.

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set OPENAI_API_KEY and OPENAI_MODEL to use the assistant (tests do not need them)
```

### Troubleshooting: `incompatible architecture (have 'x86_64', need 'arm64')`
On an Apple Silicon Mac the virtual environment must be built by the same kind of shell that will run it. If it was built under
Rosetta (x86_64) and you run it from a normal terminal (arm64), or the reverse, compiled packages such as `pydantic_core` fail to load.
Check with `uname -m` (should print `arm64` on an M-series Mac in a normal terminal), then rebuild:
```bash
rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

## Seed the database and run tests
```bash
python -m data.seed                      # -> data/recruiting.db (injected variant, default)
python -m data.seed --variant baseline --db /path/to/baseline.db
python -m data.check_consistency        # prose vs structured facts (also run in tests)
python -m pytest
```
Set `RECRUITING_DB_PATH` to choose which SQLite file the services read.

## Run the app (Streamlit)
```bash
python -m data.seed && streamlit run app.py
```
One chat page. The sidebar has a **New conversation** button, seven **suggested questions** (the demo set, see `docs/demo_questions.md`) (full text, one click), a **Developer view**
switch and the fixed as-of date. The theme is monochrome (`.streamlit/config.toml`).

What a recruiter sees by default: the answer (names, plain dates, lists or tables), a notice only when it matters (flagged text,
incomplete review, a limit), and one line saying whether it was **checked against the source records**. Under it, an **Evidence**
expander appears only when the answer relied on interview notes or emails that it does not already say, grouped by person. There is no
raw analytics table and no card for tasks, roles or pipeline numbers. If a CV, email or note contained instruction-like text, a notice
says so, and **View the original flagged text** shows it as inert text for the reviewer.

**Developer view** (off by default) adds, under each answer, **How this answer was made**: one block per model round with its token cost
and time, each tool it called (arguments, what came back, result size, record IDs), or the answer it submitted and whether validation
accepted it or sent it back (and why). All of it is recorded by the application, not written by the model. IDs and citation syntax appear
only there. Every answered question is also appended to `logs/turns.jsonl` (gitignored; set `RECRUITING_TURN_LOG` to move it) with the
same per-round trace, so a bad answer can be traced afterwards. The log holds IDs, counts, sizes and tokens, never record text.

## The tools (read-only MCP)
| Tool | Answers | Takes |
|---|---|---|
| `list_roles` | roles, days open, required/preferred skills, interview criteria, stages | - |
| `search_candidates` | who is where; find a person; who is at a stage | optional `role`, `name`, `stage`, `status` |
| `get_candidates` | full evidence for people: dated status, skills, feedback, history, tasks, CV | `names`, `candidate_ids`, or `role` (every active candidate, for shortlists) |
| `get_candidate_communications` | a candidate's emails | `candidate_id` |
| `get_pipeline_analytics` | one role's funnel, time in stage, scheduling wait, feedback lag, reasons | `role` |
| `get_recruiting_overview` | cross-role snapshot and the people waiting, longest first | - |
| `get_tasks` | tasks with owner, due date, overdue | optional `role`, `candidate`, `status`, `overdue_only` |

A role can be given by ID or name ("Backend Engineer"), and a person by name, so most questions need one or two calls. Each candidate
record carries a dated status sentence ("Technical Interview since Oct 16 (22 days): interview not booked yet."), so the model restates
facts instead of reconstructing them from timestamps.

Streamlit re-runs the script on every interaction, so each question opens and closes its own MCP session inside one `asyncio.run()`
and `st.session_state` keeps only plain data. Earlier turns are replayed to the model as question and final answer only (no old tool
results), so follow-ups such as "which of those three?" stay cheap and facts are fetched again. There is no streaming: you see a status
line while the assistant works, which disappears when the answer is shown.

## Run the agent (CLI)
```bash
cp .env.example .env     # set OPENAI_API_KEY and OPENAI_MODEL (never commit .env)
python -m data.seed
python -m agent.cli "Which recruiting tasks are pending or overdue?"
python -m agent.cli --chat --role ROLE-BE      # multi-turn: ask a follow-up like "Of those three, who has waited longest?"
```
Each answer prints the real tool trace and the evidence IDs the model received. One MCP stdio session is opened per turn
(and closed after it), because Streamlit reruns the script on every interaction.

## Tests and evals
```bash
python -m pytest                    # deterministic: no API key, no network (includes the injection suite, tests/test_injection.py)
python -m evals.run_evals           # model-dependent, opt-in: skips cleanly without OPENAI_API_KEY; writes docs/eval_results.md
python -m evals.run_evals --cases 10 --runs 3
python -m evals.run_evals --cases 1,2,3,4,5,6,7,8,9,11,12,13   # single-answer scenarios from the spec
```
Case 10 (paired injection) has its own rules; 1-9 and 11-13 are scored on tool calls, cited records, exact figures and facts from the
seeded data, never prose quality. (Cases 14-16, truncation recovery, were retired when a whole role started fitting in one result.)
`docs/eval_results.md` records every run (including failures and what was changed). Read it before trusting any rate: samples are small.

## Data design (so far)
`data/scenarios.py` holds the curated facts (who is in which stage, with dates and outcomes).
`data/seed.py` turns them into SQLite deterministically. Prose (CVs, emails, feedback summaries, exit reasons) was written from those rows by an AI
assistant, reviewed, and frozen in `data/prose/*.json`; seeding makes no model calls.
`data/check_consistency.py` verifies the prose against the structure (CV skills vs the declared
gaps, dates in feedback and invites, scheduling-delay threads, no instruction-like text outside the
C117 payload). `data/seed_expectations.json` is hand-authored and checked against an independent recomputation
(`tests/scenario_facts.py`).

Fictional policy targets (days) per interview stage: time-in-stage, entered-to-booked, interview-to-feedback.
Recruiter Screen 7/3/2, Technical Interview 14/7/2, Hiring Manager Interview 10/5/2, Final Interview 7/5/2.
Resume Review: 5 days in stage.

## Limits you should know about
- Citation and figure checks verify that a record exists and a number matches it. They do not prove the claim made with it is right; the
  evals test some of that, with small samples.
- Skill matching is a whole-word keyword match on CV text. It can be gamed and is a prefilter, not a score.
- The injection detector is a keyword list. Before the model reads a flagged CV, email or note, the sentences that matched are replaced by a marker
  (the rest stays), and the recruiter sees the original. Paraphrased or non-English instructions evade it. This is defense in depth, not
  "prompt-injection-proof"; results are in `docs/eval_results.md`.
- The model is called through Chat Completions with function tools. For `gpt-5.4-mini` that combination runs with **no reasoning**
  (the API rejects `reasoning_effort` with tools on that endpoint), which shows up as occasional slips in comparing numbers or following
  formatting rules. Moving the adapter to the Responses API would allow reasoning; see `docs/simplification_proposal.md`.
- A result is capped at 32,000 characters (a whole role's comparison view is about 12-30k). Anything larger is cut by whole records with a
  warning naming what was left out.
- All data is fictional and relative to the fixed as-of date in `services/clock.py`.
