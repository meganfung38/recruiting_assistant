# MVP polish plan (working checklist)

Guiding rule: every layer, validator or model round has to solve a problem we actually saw. This supersedes the Phase 2/3 parts of
`ux_redesign_proposal.md` (kept as background only).

## Agreed improvements
- [x] Roles are citable (`role:ROLE-BE`), so the model has no reason to invent `[list roles]`. A role's age is a verifiable figure.
- [x] Display cleanup strips only *recognizable* internal markers (known citation kinds, bracketed tool names). Grounding validation is separate and unchanged.
- [x] Default view = answer + one trust line. "Supporting details" shows only cited records that say something (feedback, stage history, emails, tasks, roles, pipeline data) and the numbers table. No search-row or "record opened" cards, no repeated rationale, no "N records looked at".
- [x] Prompt: scannable lists/tables, comparative ranking (what separates people, what evidence differs), no ranking by pipeline stage, small score gaps are comparable, a flag is never a reason, plain dates and durations, percentages.
- [x] Interview evidence kept apart: interviews completed, feedback received, feedback pending (tested against the tables).
- [x] Security notice leads the answer only if the flagged person is in the answer's candidate list; otherwise a footnote. Still application-generated; original text stays in "View the original flagged text".
- [x] UI: monochrome theme (`.streamlit/config.toml`), bordered notes instead of colored banners, Streamlit's animated running icons hidden, full-text suggested questions in the sidebar, "Developer view" toggle (tools, record counts, validation, tokens), hidden by default.

## Cancelled or deferred
- Cancelled: v2 schema / block engine, application-rendered fact tables, exclusion-reason list, mandatory ranking-note validator, rejecting bracketed text, `SHOW_FLAGGED_SOURCE` switch, extra efficiency framework.
- Deferred: retrieval/truncation-recovery efficiency (known cost: large shortlists take 5-7 model rounds and 30-60k tokens); showing the model a redacted flagged CV (see eval notes).

## Tests and acceptance
- `pytest` green (unit, UI via Streamlit AppTest, boundary, injection).
- Live questions (roles, top three + follow-up, tasks, pipeline, waiting, compare, other role): no IDs, field names, brackets or tool names in the default view; list-shaped answers are lists/tables; trust line present; no repair in the simple ones.
- Case 10 (paired injection): no promotion, no obedience, flag notice and source available. Flag-on-strong-candidate check recorded in `eval_results.md`.
- Median model rounds / tokens / seconds from the live runs are in the hand-off summary.

## Round 2: fix reliability and readability (from manual testing, 2026-11-08)

Evidence: two manual conversations (tasks; roles + top three + status follow-ups). Do the steps in order; each has a check. No new libraries,
schemas or validators beyond what is listed. Run commands with `arch -arm64 .venv/bin/python` (the venv is native arm64; the shell may be Rosetta).

Findings
- Hannelore Voss (C108): Final Interview scheduled Nov 3, no interview recorded, no feedback. The model once said "completed, feedback next day" (it used her Technical Interview feedback). Same question answered correctly on a replay, so simple lookups are right only some of the time.
- A follow-up like "what stage is X at" cost 37k tokens: the previous turn's full tool results are replayed (`ConversationState.context_messages`).
- Prose is verbose with semicolon chains ("Full required-skill match; 3 preferred skills; ...") because the model copies data labels. Abstract style rules did not fix it; examples will.
- Tasks answer is 21 lines each starting "Overdue:" / "Pending but not overdue:". Should be a table.
- "Supporting details" restates the answer (12 task cards, role cards), shows an empty "Numbers used" heading, raw wording ("Recommendation: strong_yes"), and a false "could not be verified" for derived counts such as "10 overdue" that have no field to check.
- Prompt and sidebar hardcode 2026-11-07. The date is legitimately fixed (all data is relative to `services/clock.AS_OF`) but must come from that one constant.
- Decisions: keep `get_tasks` and the tasks table (owners, due dates, tasks with no candidate); do not derive tasks from candidates.

Steps
1. [x] **Status sentence per candidate.** Add a deterministic `stage_status` sentence to each candidate record (get_candidates and search rows), built from the stage event and feedback, e.g. "Final Interview: interview scheduled Nov 3; none recorded yet; no feedback yet". Allow it in the projection. Recompute it independently from the tables in tests (all seeded candidates). Prompt: use it for status questions. Check: "has Hannelore done her final interview" correct 10/10 runs.
2. [x] **History.** Replay earlier turns as question + final answer only (the current turn keeps full tool pairing). Check: follow-up tokens drop sharply; "she"/"he" still resolves; update the tests that assert the old replay rule.
3. [x] **Answer style.** In `agent/prompts.py` add 3 short generic example answers (ranked list, table of records, one-line lookup). Hard rules: one fact per bullet, no semicolon chains, no data-field wording, tables for 4+ comparable records. Check: word count and semicolon count over the live set. Try the larger model once via `.env` on the same set and report the difference; keep the mini model unless clearly better.
4. [x] **Tasks as a table.** Try the table example from step 3 first (columns: person, task, owner, due, overdue by; two groups). If the live run is still lines, have the app draw the table from the `get_tasks` result instead. Check: 21-task answer is a table, under ~120 words of prose.
5. [x] **Evidence panel (rename of "Supporting details").** Show only what the answer does not already say: interview-note quotes grouped by person with stage/date and plain-word recommendation ("strong yes"). Hide task, role, stage and pipeline cards. Drop the empty numbers heading. Prompt: derived counts are not figures. Show "could not be verified" only for claims about a person. Check: tasks and roles answers show no panel; shortlist panel is grouped by person.
6. [x] **Small items.** Build the as-of date in the prompt (and its examples) from `AS_OF`; remove or collapse the "Done" status line after an answer; no panel under one-line answers.
7. [x] **Verify.** Run ~12 questions x 2 through the live script (scratchpad pattern: `run_turn` + `build_view` + `primary_text`), including lookups with known answers (stage, overdue count = 10, Hannelore final interview) and both manual conversations. Report correctness, words, semicolons, rounds, tokens, seconds. Save a short note in `docs/eval_results.md`.

Acceptance: no wrong answers on the known lookups; shortlist under ~150 words of prose; no ID/bracket/tool-name leaks; full suite green.

## Then stop polishing and finish
1. [x] Re-run eval cases 14, 15, 16 once; record in `eval_results.md`.
2. [x] Remaining eval cases (1-9, 11-13) as scoped in the spec.
3. [x] README (UI, developer view, suggested questions, theme), `design_notes.md`, `ai_use.md`.
4. [x] Fresh-clone test: clone, create venv, install, seed, run tests, start the app.
5. [x] Decision made for the user: show the model a redacted copy of flagged CVs (see `eval_results.md`, "Post-UX re-check").

## Round 2 results (2026-11-08)
- Tests: 430 passed (`arch -arm64 .venv/bin/python -m pytest`), also in a fresh copy of the working tree with a new venv; the app starts (health check ok).
- Decision 5 resolved by the user: flagged sentences are redacted from model-visible text (original kept for the reviewer). Case 10 PASS; borderline experiment 12/12 vs 12/12.
- Acceptance: known lookups correct; no leaks; suite green. Not met every run: shortlist prose (median about 160 words, range 137-257); case 2 eval fails 4/5.
- Not done: larger-model comparison (step 3); fresh-clone used a copy of the uncommitted tree, since nothing is committed.
- Step 1 wording: the sentence says interview "booked" Nov 3, because the data's `scheduled_at` is the booking date, not the interview date.

## Demo-readiness pass (2026-11-08)
- [x] Manual-testing notes addressed (see `docs/demo_questions.md`, "What changed"): no percentages, role-labelled numbers, one overview tool, tables and next steps, round-by-round developer view.
- [x] 12 demo questions run 3x each against the seeded data and checked against the database.
- [x] Tests green; Streamlit starts (health ok); MCP lists all 8 tools. Evals rerun; case 2 and 13 still fail sometimes (recorded).
- [ ] Not done: a larger-model comparison; faster lookup of a single named person (about ten search calls).
