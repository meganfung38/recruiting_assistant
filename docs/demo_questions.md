# Demo questions

Thirteen conversations (22 turns with follow-ups) covering what a recruiter asks: roles, pipeline bottlenecks, shortlists and
comparisons, where a person stands, tasks, and what needs attention. Run 3 times on the final code, against the seeded data (as of
Nov 7, 2026), through the real orchestrator and the recruiter-facing view (the same path as the app). "Facts" are automated checks of
the visible answer against the database (for example the right names, "180", "Oct 23"); prose quality was read by hand.


| # | Question | Rounds (median) | Tokens (median) | Time (median) | Facts |
|---|---|---|---|---|---|
| 1 | What open roles do we have? | 2 | 7.9k | 3.6s | 3/3 |
|   | → How long has each been open? | 2 | 8.1k | 2.5s | 3/3 |
| 2 | Which role needs the most attention? | 2 | 9.7k | 4.1s | 3/3 |
|   | → Show me the pipeline for Manufacturing Systems Engineer | 2 | 9.7k | 3.8s | 3/3 |
| 3 | (Backend Engineer) This role has been open for four months. Where is our process losing candidates? | 2 | 9.6k | 5.2s | 3/3 |
|   | → How did you calculate that? | 2 | 10.3k | 4.0s | 3/3 |
| 4 | (Manufacturing Systems Engineer) This role has been open for four months. What is going on? | 2 | 9.6k | 5.0s | 3/3 |
| 5 | Why is People Operations hiring slow? | 2 | 9.5k | 4.6s | 3/3 |
|   | → Who is waiting on feedback? | 2 | 11.0k | 4.5s | 3/3 |
| 6 | Who are the top three Backend Engineer candidates, and why? | 2 | 13.7k | 4.5s | 3/3 |
|   | → Of those three, who has been waiting longest? | 2 | 11.6k | 3.8s | 3/3 |
| 7 | Compare Dahlia Ramanathan and Junko Takeda. | 2 | 10.1k | 4.3s | 3/3 |
| 8 | Where does Hannelore Voss stand in the process? | 2 | 8.7k | 3.0s | 3/3 |
|   | → When is her interview? | 2 | 8.9k | 3.6s | 3/3 |
| 9 | What stage is C123 in, what was their latest interview feedback, and how long have they been waiting compared with others at that stage? | 3 | 15.6k | 5.2s | 3/3 |
| 10 | Which recruiting tasks are pending or overdue? | 2 | 11.2k | 5.8s | 3/3 |
|   | → When was Mateo Villareal's interview booking due? | 2 | 9.3k | 2.8s | 3/3 |
| 11 | What needs my attention today? | 2 | 9.9k | 4.1s | 3/3 |
|   | → Why has Mateo waited 22 days? | 2 | 9.3k | 3.6s | 3/3 |
| 12 | Mark T-011 done and email C101. | 1 | 3.8k | 2.5s | 3/3 |
| 13 | Where does Hannelore Voss stand in the process? | 2 | 8.7k | 3.3s | 3/3 |
|   | → show me all the feedback we have for voss | 2 | 9.0k | 3.5s | 3/3 |

Across the 66 turns: no failed fact checks, no IDs or field names in the recruiter's view, median 2 model rounds (max 3), median about
10k tokens (max 20k), median 4 seconds, two correction rounds, no invalid tool calls, and no answer marked "not matched". Roles, tasks
and pipeline stages came as tables in every run, and C123 was compared on time in stage (13 days) in every run. One turn took 65 s
because a single model request stalled at the provider; the request timeout is now 20 s so a stall is retried sooner.

Also checked: "Tell me about Quentin Abara." opens with the titled block "Instruction-like text found in Quentin Abara's CV", then the
answer and his evidence card. When Quentin is not part of an answer, nothing about the flag is shown to the recruiter.

## Known weak spots (not fixed, not hidden)
- **Ranking at the margin varies.** The top three was Dahlia Ramanathan, Junko Takeda and Hannelore Voss in 2 of 3 runs; the third run
  ranked Mateo Villareal (strong CV, screening feedback only) third, despite the rule that someone not yet interviewed is not ranked above
  people with positive interviews. Its follow-up then correctly named the longest wait among the three it had ranked.
- The number check proves a number exists in this turn's data for the people or roles on the same line, not that the sentence using it
  is right. Numbers written as words ("three") are not checked.
- In comparisons the model sometimes says one person is "further along" alongside the real reasons.
- Declining a write request is correct but sometimes describes the task at length.
