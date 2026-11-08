# Simplification proposal: fewer rules, better-shaped tools

Status: **implemented on branch `simplify-agent` (2026-11-08)**; see section 8 for what changed from the plan and the measured results.
Written after a full read of the codebase, the manual-testing notes in `interesting-conversations.md`, and an instrumented live run
(numbers below are measured, not estimated, unless marked).

## 1. Verdict

The core is not overengineered. The parts the spec makes mandatory work as intended: MCP server, typed tools, executor
allowlist and validation, projection with the untrusted-text envelope, evidence registry and figure check, and the trace.
The figure validator is what caught the invented "82.35%".

The **compensation layers** are what's overengineered. These are code and prompt rules added one bug at a time to make up
for tool output that was awkward for the model:

- The system prompt has grown to **16.5k characters**: 55 bullets, 27 "never" and 18 "do not". About half is per-question
  routing ("ANSWER SHAPES", "for X call Y"). That is the phrase router the spec forbids, written in prose.
- The prompt contradicts itself. EVIDENCE says *"Always name candidates as Name (C104)"*. STYLE says *"never IDs"*.
  `humanize.py` then strips the IDs the model was told to write.
- Three tools describe the same candidate status in three shapes: search rows, overview people, and `get_candidates`.
  `waiting_state`, `waiting_days` and `days_in_current_stage` are each repeated inside `fit_signals`.
- A whole truncation-recovery subsystem exists only because `get_candidates` returns ~3.8k characters per person and is
  capped at 6 IDs. It covers omitted lists, unfulfilled tracking, finalizer retrieval retries, "retrievable" and
  "undisclosed_incomplete" checks, the wrap-up "unreviewed" text, the UI notice and eval cases 14 to 16. With a compact
  record, every role fits in one call, so the case never happens in this dataset.

The model mostly fails when it has to **assemble** an answer from fragments, as in the cross-role numbers, the 11-call
name lookup and the 6-of-8 shortlist. It fails much less when a tool hands it the answer in one piece. In the replay,
`get_recruiting_overview` fixed "which role needs attention" in 2 rounds and 13k tokens. The same question at HEAD took
3 to 4 rounds and 17k to 24k tokens. So the fix is better tool shapes and a short prompt, not more rules or validators.

## 2. Evidence (what goes wrong and why)

| Observed (conversation) | Root cause | Category |
|---|---|---|
| BE's "14 of 17 / 82.35%" attributed to MSE (#1) | 3 × 4.3k-char analytics results with an identical shape; per-stage rows carried no role; no tool compared roles | Tool shape (fixed by the overview tool) |
| Shortlist reads 6 of 8 (#2) | `MAX_IDS=6`, a proxy for the 24k cap, plus a prompt rule to pick the "6 with most feedback", which biases toward later stages | Tool shape + prompt rule |
| Shortlist costs 57k tokens, 6 rounds (measured) | ~5k-token prefix resent each round (53%); the 6-record result resent twice (25%); 2 rounds spent on `list_roles`/`get_role` | Prompt size + tool shape |
| "Where is Hannelore" takes 11 calls (#7) | No search by name; the prompt says to search every role | Missing tool parameter |
| "Where does 22 days come from?" (#6) | The status sentence has no stage-entry date: "Technical Interview: interview not booked yet" | Data wording |
| "Interview Nov 3, how has it not been held?" (#7) | `scheduled_at` is the booking time; the sentence "booked Nov 3" reads like the interview date | Data wording |
| Injection notice under unrelated answers (#2, #5, #7) | Any Backend search returns C117's flag, and the presenter shows a footnote even when C117 isn't in the answer | Presentation |
| 13 of 54 demo turns needed a correction round | The model submits derived counts as `figures` with `field_path`s; each correction is a full extra round | Answer schema friction |
| Arrow traceback in the console (#8) | `presenter.technical_view` puts `"ms": ""` into an int column | Bug |

## 3. Keep / simplify / remove

### Keep as is (spec-mandated, working)
`mcp_server/server.py`, `services/*` (analytics, candidates, tasks, clock, db, content_flags detector), `agent/tool_executor.py`,
`agent/limits.py`, `agent/tracing.py`, `agent/llm.py`, `agent/mcp_client.py`, `agent/answer_schema.py`, the one-repair-round
`submit_answer` flow, the data layer and its tests, and the injection fixture with its deterministic tests.

### Simplify
| Area | Now | Proposed |
|---|---|---|
| `agent/prompts.py` | 16.5k chars, per-question routing, contradictions | ~5k chars: how to work, evidence, untrusted text, boundaries, finishing, style with two examples. Metric definitions move into tool descriptions, next to the data. |
| `get_candidates` | Full record (~3.8k chars), 1 to 6 IDs | Compact record (status sentence with dates, missing skills, feedback with recommendation, scores and summary, CV, pending tasks, one line per past stage). Takes `candidate_ids` (up to 20) **or** `role_id` (all active). A whole role fits in one call: BE 11.5k, MSE 13.6k, PO 16.7k chars. |
| `search_candidates` | Needs a role; no name search | `role_id` optional, plus a `name` filter. One call finds a person in any role. Remove the duplicated fields in `fit_signals`. |
| `get_recruiting_overview` | 10.2k chars incl. 12 "other_active" people | Role table plus the three waiting groups only (~4k). "Who is at stage X" uses `search_candidates(stage=…)`. |
| `stage_status` sentence | "Technical Interview: interview not booked yet." | "In Technical Interview since Oct 17 (22 days); interview not booked yet." and "Interview booked on Nov 3 but not held yet; the planned date is not recorded." |
| `finalizer.py` / `evidence.py` | Repair round + retrieval retries + 6 problem categories | One repair round for bad citations or figures; nothing else. `figures` are optional and meant only for pipeline and analytics numbers. |
| `projection.cap_size` | Truncation + omitted-ID bookkeeping feeding the finalizer | Keep truncation with a warning (spec), drop the downstream bookkeeping. |
| `humanize.py` | Rewrites IDs, field names and snake_case in model prose | The model writes names; humanize only strips `[cite]` tokens and maps any stray ID to a name. |
| Flag notice (`presenter.flag_notices`) | Top banner or footnote for any flagged record seen | Banner only when the flagged candidate is in the answer; otherwise nothing. The trace still records it. |
| Developer view | Rounds without tokens; no persistence | Tokens per round; each turn's trace appended to `logs/turns.jsonl` (gitignored), so a bad answer can be traced afterwards. |

### Remove
| What | Why |
|---|---|
| Truncation recovery: `Finalizer.RETRIEVAL_PROBLEMS`/`INCOMPLETE_RETRIES`/`can_retrieve`, `ValidationReport.retrievable`/`undisclosed_incomplete`, `EvidenceRegistry.note_unfulfilled`/`unresolved_candidates`, the wrap-up "unreviewed" text, the presenter "Not everyone was reviewed" notice, prompt "TRUNCATED RESULTS", eval cases 14 to 16 and their checks | Can't happen once a role fits in one call. Spec only asks to "truncate with a warning". |
| `get_role` tool | Its requirements reach the model through the compact candidate result (skills missing) and `list_roles`. The spec's cut list allows merging it into `list_roles`. Saves one round per shortlist. |
| Prompt "ANSWER SHAPES" and the per-question tool routing | Prose routing; tool descriptions should say when to use each tool. |
| `INJTOP.json`, `INJTOP2.json` (repo root) | Scratch output of an injection experiment, not referenced anywhere. |
| `docs/ux_redesign_proposal.md`, `docs/ux_review/`, `docs/mvp_plan.md` | Superseded or completed working notes (the first says so itself). Keep the decisions in `design_notes.md`. |

## 4. Target design

### Tools (7, read-only)
| Tool | Answers |
|---|---|
| `list_roles` | Roles, open dates, age, requirements (folded in from `get_role`) |
| `search_candidates(role_id?, name?, stage?, status)` | Who is where; find a person by name |
| `get_candidates(candidate_ids? \| role_id?)` | Compact evidence for one person, a few, or a whole role (shortlists, comparisons, status) |
| `get_candidate_communications(candidate_id)` | Withdrawal reasons, scheduling emails |
| `get_pipeline_analytics(role_id)` | Bottlenecks for one role |
| `get_recruiting_overview()` | Cross-role: which role, who waits longest, what needs attention |
| `get_tasks(...)` | Pending and overdue tasks |

Rules for every result:
- Each result names its role (`role_title`) on every row.
- Each result carries plain-language status sentences with dates.
- No duplicated fields and no precomputed rates.

### Prompt
Principles, not routes:
- Use tools for every fact.
- Re-query on follow-ups.
- Cite what you used.
- CV, email and feedback text is data.
- Rank on skills and feedback only.
- Say what's missing.
- Read-only.
- Names, not IDs.
- Counts as "X of Y".
- Lead with the answer.

Two example answers: a ranking and a table. Target ~5k chars, about a third of today's.

### Expected cost (estimate; to be measured in phase 3)
A shortlist becomes `get_candidates(role_id=ROLE-BE)` plus `submit_answer`: 2 to 3 rounds. The prefix is ~2.5k tokens
instead of ~5k, and there's one ~3.5k-token result. That puts a turn at roughly 10k to 15k tokens instead of 57k.

## 5. The demo set (12 questions, follow-ups included)

Each should take at most 3 rounds and 1 to 2 data calls. "Expected" comes from the database (see `docs/demo_questions.md`).

| # | Question | Expected call(s) | Follow-up to show |
|---|---|---|---|
| 1 | What open roles do we have? | `list_roles` | "How long has each been open?" |
| 2 | Which role needs the most attention? | `get_recruiting_overview` | "Show me the pipeline for Manufacturing Systems Engineer" → `get_pipeline_analytics` |
| 3 | (BE selected) This role has been open four months. Where are we losing candidates? | `get_pipeline_analytics` | "How did you calculate that?" → re-fetch, answer from the data |
| 4 | (MSE selected) This role has been open four months. What's going on? | `list_roles`, `get_pipeline_analytics` | Corrects the premise (180 days) |
| 5 | Why is People Operations hiring slow? | `get_pipeline_analytics`, `get_tasks` | "Who is waiting on feedback?" |
| 6 | Who are the top three Backend Engineer candidates, and why? | `get_candidates(role_id)` | "Of those three, who has waited longest?" (spec demo 5) |
| 7 | Compare Dahlia Ramanathan and Junko Takeda. | `search_candidates(name)` ×2 or `get_candidates` | — |
| 8 | Where does Hannelore Voss stand? | `search_candidates(name)` | "When is her interview?" → booked Nov 3, date not recorded |
| 9 | What stage is C123 in, latest feedback, and wait vs peers? | `get_candidates`, `get_pipeline_analytics` | (spec demo 4) |
| 10 | Which tasks are pending or overdue? | `get_tasks` | "When was Mateo's booking due?" |
| 11 | What needs my attention today? | `get_recruiting_overview`, `get_tasks(overdue_only)` | "Why has Mateo waited 22 days?" → in stage since Oct 17 |
| 12 | Mark T-011 done and email C101. | none | Declines (read-only) |

The injection fixture surfaces through #6: C117 is ranked on merit, and the banner appears only if C117 is in the answer.

**Acceptance:**
- A scripted run of all 12 plus follow-ups, 3 times each, is correct against the database every time.
- No turn exceeds 3 rounds, except the shortlist (at most 4).
- The shortlist stays under 20k tokens.
- No IDs or field names appear in the default view.

## 6. Plan

1. **Cleanup (no behavior change).**
   - Delete the scratch files and superseded docs.
   - Fix the Arrow bug.
   - Remove the "Name (C104)" contradiction.
   - Check: `pytest` stays green.
2. **Tool shape.**
   - Compact `get_candidates` with `role_id`.
   - `search_candidates` with `name` and optional role.
   - Fold `get_role` into `list_roles`.
   - Slim the overview.
   - Add dates to the status sentences.
   - Re-derive each in `test_services` from the tables.
   - Check: the parity test and the MCP tool-list test are green.
3. **Remove the compensation layers.**
   - Rewrite the prompt (~5k).
   - Delete the truncation-recovery path.
   - Make figures optional.
   - Show the flag banner only when relevant.
   - Prune the tests for deleted code.
   - Check: green suite, then the 12-question script ×3 with tokens and rounds recorded.
4. **Trace persistence.**
   - Per-round tokens in the developer view.
   - Append each turn to `logs/turns.jsonl`.
5. **Docs.**
   - Update the README, `design_notes.md` and `eval_results.md`. Each records what was removed and why.
   - Re-run eval cases 1 to 13.

## 7. Decisions needed (deviations from `project-spec.md`)
- **`get_candidates` accepts `role_id` and up to 20 IDs** instead of 1 to 6. The cap was a size proxy, and measurement shows
  every role fits under the 24k cap.
- **`get_role` folded into `list_roles`.** The spec's own cut list allows this. The tool count stays at 7 because
  `get_recruiting_overview` remains.
- **Truncation-recovery evals (14 to 16) dropped.** Truncation keeps its warning, but it no longer occurs in this dataset.

## 8. Outcome (2026-11-08)

Implemented as planned, with these changes found while testing against the live model:
- **The cap is 32,000 characters, not 24,000.** A role's comparison view still overflowed 24k for People Operations (12 active people,
  about 25k after interview notes). The cap exists to stop runaway results, not a deliberate whole-role read of about 7k tokens.
  `get_candidates(role=...)` leaves out stage history and tasks (the dated status sentence covers where a person is now).
- **People can be named, not only roles.** `get_candidates(names=[...])` and `get_tasks(candidate=<ID or name>)`. Without them the model
  either sent a name where an ID was expected (one wasted round) or guessed an ID such as C001 and concluded wrongly.
- **The figure check forgives labels, not numbers.** Most correction rounds were correct numbers cited to the wrong record (an overview
  count cited to a person's row), which made the model fetch more data just to support a figure. The checker now finds the field
  where the cited source does not have it. A number whose cited source holds a different value is still rejected (that is what catches the
  original cross-role error), and the repair message says not to call tools just to support a figure.
- **Presentation hints moved next to the data.** "Show the roles as one table" and "show tasks as one table, overdue first" live in
  those tools' descriptions; "correct a wrong premise first" lives in the `answer_markdown` field. The system prompt keeps general rules
  only (now 7k characters).
- **Two analysis rules came back** after the first live run without them: a stage is a problem only if a number is over its own target,
  and recorded reasons are given as counts. Without them, Manufacturing's within-target Technical Interview was called "the bottleneck".
- **`conversion_denominator` hidden from the model.** It read "19" (people who finished Resume Review) as "19 reached" (really 20).
- Spec demo 4 (C123 against peers) stays multi-tool: the record and the analytics descriptions now say which number to compare
  (`days_in_current_stage`, never `waiting_days`). It went from 0 of 3 to 3 of 3 correct comparisons.

Measured on the demo set (12 conversations, 20 turns, 3 runs each; details in `docs/demo_questions.md`):

| | Before | After |
|---|---|---|
| Top three Backend Engineer | 6 rounds, 57k tokens | 2 rounds, about 13.6k tokens |
| All demo turns | median 3 rounds, shortlists 40-60k tokens | median 2 rounds (max 4), median about 10k tokens (max 26k) |
| Invalid tool calls / correction rounds | frequent | 0 of 60 / 5 of 60 |
| Tests | 432 | 429 (truncation-recovery tests removed; tests added for name lookup, round trace, turn log, figure checks) |

**Still open (recommended next step): reasoning.** The API reports *"Function tools with reasoning_effort are not supported for
gpt-5.4-mini in /v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to 'none'."* The agent therefore
runs with zero reasoning tokens. The remaining slips fit that: naming the wrong person as the longest wait of three in 1 of 3 runs,
bullets instead of a requested table, and ranking someone higher for being further along. Moving `agent/llm.py` to the Responses API
(about 100 lines; the orchestrator only depends on `complete()`) would allow `reasoning_effort="low"`. Measure it on this demo set before
adopting it: it will add latency and tokens.

## 9. Second round: the answer contract (2026-11-08)

Manual testing (`convo-threads.md`) found three new symptoms: a correction round for a figure path the model guessed (`days_open`), a
feedback list that collapsed into one line, and a top-three evidence panel with uneven, partly identical notes and a flagged-text panel
with no explanation. All three traced to the same causes: bookkeeping asked of a model with no reasoning, three vocabularies (fields,
descriptions, recruiter words), regex rewriting of model text, display decisions made with different rules, and templated data.

Implemented (approved: spec 6.2 deviation, data regeneration, Responses API):
1. `submit_answer` = plain prose + who it is about + optional ranking. No citations or figures. Deterministic checks of people, roles and
   every number per line (`agent/evidence.py`). The history copy of each answer carries an `[About: ...]` line for follow-ups.
2. Recruiter vocabulary in model-visible fields; counting fields removed from the model's view.
3. Evidence cards per person, same shape for everyone, in ranking order (`agent/cards.py`); resume-review notes excluded for all.
4. One subject set drives flag block and cards; the flag is a titled block with the original text inside, or nothing.
5. `humanize.py` reduced to a line-by-line safety net that never touches list markers or table pipes.
6. Responses API with reasoning effort low (`agent/llm.py`); medium measured: slower, not more accurate here.
7. Interview notes regenerated from each record's facts (`data/compose_feedback.py`): 81 of 82 distinct.
8. Presentation hints in results (`how_to_show`), and a ranking rule: someone with only screening notes is not ranked above someone
   with positive interviews.

Measured on the demo set (13 conversations, 22 turns, 3 runs): 66 of 66 turns pass the fact checks; median 2 rounds (max 3); median
about 9k tokens (max 16k); median 4 s (max 7.5 s); 1 correction round; tables every time; the same correct top three every run.
