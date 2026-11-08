# Demo questions

Twelve conversations (20 turns with follow-ups) covering what a recruiter asks: roles, pipeline bottlenecks, shortlists and
comparisons, where a person stands, tasks, and what needs attention. Each was run 3 times against the seeded data (as of
Nov 7, 2026), through the real orchestrator and the recruiter-facing view, on 2026-11-08 after the simplification
(`docs/simplification_proposal.md`). "Facts" means automated checks of the visible answer against the database (for example
"Oct 23", "180", the right names). Prose quality was read by hand; it is not scored here.

| # | Question | Tools the model chose | Rounds (median) | Tokens (median) | Facts |
|---|---|---|---|---|---|
| 1 | What open roles do we have? | list_roles | 2 | 8.0k | 3/3 |
|   | → How long has each been open? | list_roles | 2 | 8.2k | 3/3 |
| 2 | Which role needs the most attention? | get_recruiting_overview | 2 | 9.8k | 1/3 * |
|   | → Show me the pipeline for Manufacturing Systems Engineer | get_pipeline_analytics | 2 | 9.6k | 3/3 |
| 3 | (Backend selected) This role has been open for four months. Where is our process losing candidates? | get_pipeline_analytics | 2 | 9.6k | 3/3 |
|   | → How did you calculate that? | get_pipeline_analytics (fetched again) | 2 | 9.8k | 3/3 |
| 4 | (Manufacturing selected) This role has been open for four months. What is going on? | get_pipeline_analytics, get_recruiting_overview | 3 | 16.7k | 3/3 |
| 5 | Why is People Operations hiring slow? | get_pipeline_analytics, get_recruiting_overview | 3 | 17.2k | 3/3 |
|   | → Who is waiting on feedback? | get_recruiting_overview | 2 | 10.5k | 3/3 |
| 6 | Who are the top three Backend Engineer candidates, and why? | get_candidates(role) | 2 | 13.7k | 3/3 |
|   | → Of those three, who has been waiting longest? | get_candidates(names) | 2 | 11.3k | 3/3 ** |
| 7 | Compare Dahlia Ramanathan and Junko Takeda. | get_candidates(names) | 2 | 10.2k | 3/3 |
| 8 | Where does Hannelore Voss stand in the process? | get_candidates(names) | 2 | 8.7k | 3/3 |
|   | → When is her interview? | get_candidates(names) | 2 | 8.9k | 2/3 |
| 9 | What stage is C123 in, what was their latest interview feedback, and how long have they been waiting compared with others at that stage? | get_candidates, get_pipeline_analytics | 3 | 15.2k | 3/3 |
| 10 | Which recruiting tasks are pending or overdue? | get_tasks | 2 | 11.1k | 3/3 |
|   | → When was Mateo Villareal's interview booking due? | get_tasks(candidate name) | 2 | 8.9k | 3/3 |
| 11 | What needs my attention today? | get_recruiting_overview | 2 | 10.3k | 2/3 |
|   | → Why has Mateo waited 22 days? | get_candidates(names) | 3 | 15.7k | 3/3 |
| 12 | Mark T-011 done and email C101. | (declines; may look the task up) | 3 | 11.6k | 3/3 |

Across all 60 turns: median 2 model rounds (max 4), median about 10k tokens (max 26k), median 4 seconds (max 9), no failed turns, no IDs or
field names in the recruiter's view, 5 turns needed a correction round, and the model made no invalid tool call.

\* The facts are right, but in 2 of 3 runs the per-role numbers came as bullets without days open instead of a table.
\*\* The check only confirms an answer was given. Reading them: the right person (Junko Takeda, 5 days waiting for a decision) was named in
2 of 3 runs.

## Before and after (same questions)
| | Before (2026-11-08 morning) | After |
|---|---|---|
| Top three Backend Engineer | 6 rounds, 57k tokens, 6 of 8 candidates read first | 2 rounds, about 13.6k tokens, all 8 read in one call |
| Where does Hannelore Voss stand | up to 11 tool calls (searched every role) | 1 call, 2 rounds |
| Which role needs attention | 3-4 rounds, 17-24k tokens, once mixed two roles' numbers | 2 rounds, about 10k tokens, one cross-role result |
| System prompt | 16.5k characters | 7k characters |

## Known weak spots (not fixed, not hidden)
- The model runs with **no reasoning**: with function tools on the Chat Completions endpoint, `gpt-5.4-mini` rejects any
  `reasoning_effort` other than none. Remaining slips look like that: naming the wrong person as "longest of three" in 1 of 3 runs,
  and bullets where a table was asked for. Moving the adapter to the Responses API is the recommended next step.
- In comparisons the model sometimes still mentions that one person is "further along", although the prompt says that is not a reason to
  rank them higher.
- Declining a write request ("mark T-011 done") is correct but wordy: it describes the task instead of just declining.
