# Demo questions

Twelve questions covering the five things a recruiter asks. Each was run three times against the seeded data (as of Nov 7, 2026) and checked
against the database, not against the model's own explanation. "Expected" is what the data says; every run below matched it unless noted.

| # | Ask | Expected (from the data) |
|---|---|---|
| **Candidate status** | | |
| 1 | Where does Hannelore Voss stand in the process? | Final Interview, interview booked on Nov 3, not held yet, no feedback yet |
| 2 | Who is at the final interview stage for Backend Engineer? | Dahlia Ramanathan (interview held Nov 3, feedback in Nov 4) and Hannelore Voss (booked, not held) |
| **Pipeline bottlenecks** | | |
| 3 | Where are we losing candidates for the Backend Engineer role? | Technical Interview: 17 reached, 5 moved on, 2 rejected, 5 withdrew; 3 people still unscheduled for 22, 16 and 12 days |
| 4 | What is going on with Manufacturing Systems Engineer hiring? | Open 180 days; 6 Resume Review rejections for a missing required skill (a sourcing signal); 2 people in Final Interview already past the 7-day target |
| **Interview feedback delays** | | |
| 5 | Which interviews are still waiting on feedback? | Five people: Xiomara Lindholm (9 days), Wyatt Nakamura (6), Valentina Roos (4), Lena Sorensen (3), Yara Eklund (1.5) |
| 6 | Why is People Operations hiring slow? | Hiring Manager Interview feedback: median 3 days against a 2-day target, 7 of 8 over; 4 people waiting on feedback; 5 overdue tasks |
| **Comparisons** | | |
| 7 | Who are the top three Backend Engineer candidates? | Dahlia Ramanathan, Junko Takeda, Hannelore Voss (same three in every run) |
| 8 | Compare Dahlia Ramanathan and Junko Takeda. | Both match all required skills; Dahlia has more interview evidence (4 reviews, 3 interviews) than Junko (3 reviews, 2 interviews) |
| **Actions that need attention** | | |
| 9 | What needs my attention today? | Book Mateo Villareal (22 days), Kofi Adeyemi (16) and Quentin Abara (12); chase feedback for Xiomara Lindholm, Wyatt Nakamura and others |
| 10 | Which tasks are overdue? | 10 tasks, 15 days to under a day overdue, in one table |
| 11 | Who has been waiting longest for an interview? | Mateo Villareal, 22 days, not yet booked; then Kofi Adeyemi 16 and Quentin Abara 12 |
| 12 | Which role needs the most attention? | Table: Backend Engineer 123 days open, 3 to schedule, 1 on feedback, 4 overdue tasks; Manufacturing 180, 1, 0, 1; People Operations 82, 1, 4, 5 |

Follow-ups worth showing in the same conversation: "How many are at the final stage?" then "Give me those candidates and their status";
"Which role needs attention?" then "Give me the pipeline for Manufacturing Systems Engineer".

## What changed because of this pass
Fixes made after a first run of these questions and the notes in `interesting-conversations.md`:
- **Invented numbers.** "82.35%, 14 over-target" were Backend Engineer's numbers (14 of 17) attached to Manufacturing, whose real figure is 0 of 11.
  The assistant no longer receives precomputed rates, is told never to quote percentages (it says "14 of 17"), and must keep each role's numbers apart.
- **Wrong people under the wrong role.** Search rows now carry their role.
- **One overview instead of arithmetic.** A new read-only tool, `get_recruiting_overview`, gives per-role counts and groups every active person by what
  they are waiting for. "What needs attention", "which role", "who is waiting longest", "how many at the final stage" and "where is X" read from it,
  so the model no longer counts across several results. A test recomputes it from the raw tables.
- **Tasks and pipeline are tables; answers end with next steps** (person, action, why) for attention, delay and bottleneck questions.
- **Plain words.** "Reached" and "currently at" instead of "entered" and "in stage".
- **No mid-answer self-corrections;** a stage is called a problem only when a number is over target.
- **Developer view is a timeline:** each model round, the tool it called with arguments and what came back (including records the size cap left out),
  and which round submitted the answer.
- **Why top three read 6 of 8 candidates:** a single record lookup is capped at 6. The assistant now reads the six with the most feedback and
  says how many it did not rank.

## Observed across 54 turns (18 questions/turns x 3 runs)
No percentages, no identifiers or tool names in the default view, no failed turns, median 115 words, 3 model rounds, about 5 seconds (slowest 11 s).
Shortlists take 5 to 6 rounds and 40-60k tokens.

## Known weak spots (not fixed, not hidden)
- Looking up one named person can take about ten search calls. The answer is right, but slow.
- The model occasionally submits a count as a figure, which triggers a silent correction round (13 of 54 turns had some correction); the final answer was
  still checked.
- Role-priority questions are a judgment call: People Operations and Backend Engineer both defend as "most attention" and the answer can vary between them.
- Answers to "what needs my attention" sometimes include the role table although the instructions say not to.
