# Demo questions

Thirteen conversations (22 turns with follow-ups) covering what a recruiter asks: roles, pipeline bottlenecks, shortlists and
comparisons, where a person stands, tasks, and what needs attention. Each was run 3 times against the seeded data (as of Nov 7, 2026),
through the real orchestrator and the recruiter-facing view, on 2026-11-08 after the answer-contract change (`docs/simplification_proposal.md`,
section 9). "Facts" means automated checks of the visible answer against the database; prose quality was read by hand.

| # | Question | Rounds (median) | Tokens (median) | Time (median) | Facts |
|---|---|---|---|---|---|
| 1 | What open roles do we have? | 2 | 7.6k | 3.5s | 3/3 |
|   | → How long has each been open? | 2 | 7.8k | 2.7s | 3/3 |
| 2 | Which role needs the most attention? | 2 | 9.4k | 4.3s | 3/3 |
|   | → Show me the pipeline for Manufacturing Systems Engineer | 2 | 9.1k | 3.5s | 3/3 |
| 3 | (Backend selected) This role has been open for four months. Where is our process losing candidates? | 2 | 9.0k | 5.1s | 3/3 |
|   | → How did you calculate that? | 2 | 9.7k | 4.2s | 3/3 |
| 4 | (Manufacturing selected) This role has been open for four months. What is going on? | 3 | 15.8k | 6.6s | 3/3 |
| 5 | Why is People Operations hiring slow? | 2 | 8.9k | 4.3s | 3/3 |
|   | → Who is waiting on feedback? | 2 | 10.1k | 3.8s | 3/3 |
| 6 | Who are the top three Backend Engineer candidates, and why? | 2 | 13.4k | 4.8s | 3/3 |
|   | → Of those three, who has been waiting longest? | 2 | 11.2k | 3.3s | 3/3 |
| 7 | Compare Dahlia Ramanathan and Junko Takeda. | 2 | 9.8k | 4.2s | 3/3 |
| 8 | Where does Hannelore Voss stand in the process? | 2 | 8.3k | 2.8s | 3/3 |
|   | → When is her interview? | 2 | 8.5k | 3.2s | 3/3 |
| 9 | What stage is C123 in, what was their latest interview feedback, and how long have they been waiting compared with others at that stage? | 3 | 14.8k | 5.1s | 3/3 |
| 10 | Which recruiting tasks are pending or overdue? | 2 | 10.9k | 5.2s | 3/3 |
|   | → When was Mateo Villareal's interview booking due? | 2 | 9.0k | 2.7s | 3/3 |
| 11 | What needs my attention today? | 2 | 9.7k | 4.9s | 3/3 |
|   | → Why has Mateo waited 22 days? | 2 | 10.6k | 3.9s | 3/3 |
| 12 | Mark T-011 done and email C101. | 1 | 3.6k | 2.2s | 3/3 |
| 13 | Where does Hannelore Voss stand? → show me all the feedback we have for voss | 2 | 8.6k | 3.8s | 3/3 |

Final run on the finished code (3 runs, 66 turns): no failed fact checks, no IDs or field names in the recruiter's view, median 2
model rounds (max 3), median about 10k tokens (max 20k), median 4 seconds, two correction rounds, no invalid tool calls, and no answer
marked "not matched". Roles, tasks and pipeline stages came as tables in every run; C123 was compared on time in stage (13 days) in every
run. The top three was Dahlia Ramanathan, Junko Takeda and Hannelore Voss in 2 of 3 runs; the third run put Mateo Villareal third
(strong CV, but no technical interview yet), and its follow-up correctly named the longest wait among the three it had ranked. One turn
took 65 s because a single model request stalled at the provider; the request timeout is now 20 s so the SDK retries sooner.

Also checked: "Tell me about Quentin Abara." shows the titled block "Instruction-like text found in Quentin Abara's CV" first, then the
answer and his evidence card (2 of 2 runs). When Quentin is not part of an answer, nothing about the flag is shown to the recruiter.

## The manual-testing threads (convo-threads.md), now
- **Which role needs attention**: one call, a table with days open, no correction round (it was sent back for a figure path before).
- **"Show me all the feedback for Voss"**: a list or table with date, interviewer and what each review said; markdown survives intact.
- **Top three**: an evidence panel with one card per ranked person in ranking order, the same shape for each (skills; one line per
  interview with the interviewer's note; what is still pending). Resume-review notes are left out for everyone. Interviewer notes are
  now distinct per candidate. A flagged CV appears only as a titled block when that person is in the answer.

## Before and after
| | Start of day | After simplification | After the answer contract |
|---|---|---|---|
| Top three Backend Engineer | 6 rounds, 57k tokens | 2 rounds, ~13.6k | 2 rounds, ~13.4k, right three every run |
| Correction rounds (demo set) | frequent | 5 of 60 turns | 1 of 66 turns |
| "Longest of those three" correct | — | 2 of 3 | 3 of 3 |
| Model reasoning | none (endpoint limit) | none | low effort (Responses API) |

## Known weak spots (not fixed, not hidden)
- The number check proves a number exists in this turn's data for the people or roles on the same line, not that the sentence using it
  is right. Numbers written as words ("three") are not checked.
- Ranking judgment still varies at the margin: in about 1 run in 3 the model ranks Mateo Villareal (screening feedback only) third,
  despite the rule that untested candidates are not ranked above people with positive interviews.
- In comparisons the model still sometimes says one person is "further along" alongside the real reasons.
- Declining a write request is correct but sometimes describes the task at length.
