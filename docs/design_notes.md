# Design notes

## Shape
`data/` (seeded SQLite, fixed clock) -> `services/` (deterministic queries and analytics) -> `mcp_server/` (seven read-only tools over MCP stdio)
-> `agent/` (model loop with a trusted executor) -> `app.py` / `agent/cli.py`.

The model only chooses tools. Every call goes through `ToolExecutor`, which enforces the tool allowlist, validates arguments, applies per-turn limits
(rounds, calls, time, result size), and projects the result before the model sees it.

## Trust boundary
- **Projection** (`agent/projection.py`): explicit per-tool field allowlists. Contact details never reach the model. Candidate-supplied text
  (CV, email, feedback, exit reasons) is wrapped as labelled untrusted data, and the sentences matching the instruction detector are replaced by a
  marker. Flags are computed from the original text; the original excerpt is kept only for the reviewer's screen (`agent/flag_sources.py`),
  never in the trace, evidence registry or model messages.
- **Evidence** (`agent/evidence.py`): a per-turn registry built only from structured fields of successful calls. The model submits a structured
  answer (`submit_answer`) declaring who it is about; the finalizer checks people, roles, that ranked candidates were read, and every number
  in the prose against the data for the subject on that line. One repair round, then the answer is shown with its problems visible.
  Validation checks presence, not meaning.
- **Truncation**: a result over 32,000 characters is cut by whole records, with a warning and the omitted IDs. It does not happen with
  this dataset's normal questions.

## Decisions made during polish (Round 2)
- **Stage status is computed, not inferred.** Each candidate record carries a one-sentence `stage_status` built from the stage event and feedback,
  keeping "interview booked", "interview held" and "feedback received" apart. A manual test showed the model once reporting an interview as completed
  from an earlier stage's feedback. The data's `scheduled_at` is the booking date, so the sentence says "booked".
- **History is question + final answer only.** Replaying the previous turn's tool results cost about 37k tokens for a simple follow-up.
- **Evidence panel is minimal.** Only interview notes and emails the answer cites, grouped by person, and only when the answer is longer than a line.
  Tasks, roles, stage history and pipeline numbers repeat the answer, so they live in the developer view.
- **Prompt carries examples, not more rules**, and gets the as-of date from `services/clock.AS_OF`.
- **Redaction over exposure.** An earlier version showed the model the flagged text as labelled data. Now only the matched sentences are removed.
- Cancelled: a second answer schema, application-rendered fact tables, mandatory ranking-note validator, extra efficiency framework.

## Simplification (2026-11-08)
Full reasoning and measurements: `docs/simplification_proposal.md`. In short, the model failed most when it had to assemble an answer from
fragments, so the fix was better-shaped tools and a shorter prompt, not more rules or validators.
- **Tools answer questions in one call.** Roles and people can be named; `get_candidates(role=...)` returns every active candidate's
  comparison view (the 6-ID cap, a proxy for result size, is gone); the overview answers cross-role questions; `get_role` was folded into
  `list_roles`. A shortlist went from 6 model rounds and 57k tokens to 2 rounds and about 13k.
- **Status and history are dated sentences** built in the service, so "where does 22 days come from" is answered by the data itself.
- **The prompt went from 16.5k to 7k characters.** Per-question routing ("for X call Y") was removed; when a tool's output has a natural
  presentation (a table per role, a table per task), the hint lives in that tool's description, next to the data.
- **Removed:** the truncation-recovery path (omitted-ID tracking, retrieval retries, disclosure checks, eval cases 14-16), precomputed
  rates (the model quoted them out of context), `conversion_denominator` in the model's view (read as "reached"), and the injection
  footnote under answers that did not involve the flagged person.
- **Trace:** one record per model round (tokens, time, action, validation verdict), shown as round blocks in the developer view and
  appended to `logs/turns.jsonl`.

## The answer contract (2026-11-08, second round)
Manual testing showed that most remaining failures came from bookkeeping the model was asked to do: inline citation tokens and figure
paths (repair rounds; a path named `days_open` for a field called `role_age_days`), IDs written into prose and then stripped by regex
(which merged a list into one line), and an evidence panel shaped by whichever records the model happened to cite.
- **The model owns judgment and wording; the application owns structure and provenance.** `submit_answer` takes plain prose, the IDs of
  the people and roles it is about, and an optional ranking with one-line reasons. Evidence is derived from those declarations.
- **Numbers are checked deterministically**, line by line, against what the model saw for the people and roles on that line
  (`agent/evidence.py`). This catches a number carried over from another role, which was the original failure.
- **One vocabulary.** The model sees recruiter words (`days_open`, `days_waiting`, `waiting_for: "interview to be booked"`, `reached`,
  `there_now`); counting fields that it narrated or ranked by are not shown. Services keep the spec's names.
- **One "who is this answer about" set** (ranked, declared, then named in the prose, in order) drives the flag block and the evidence
  cards, so the original flagged text can no longer appear without the notice that explains it.
- **Presentation hints travel with the data** (`how_to_show` in each result where a shape helps). The model followed them far more
  reliably than the same advice in a tool description: role tables went from 0-3 of 3 runs to 3 of 3.
- **Reasoning** via the Responses API at low effort; the adapter converts the stored chat-shaped history on each call.
- **Distinct evidence.** Interview notes were regenerated from each record's own facts (`data/compose_feedback.py`).
- **History notes are separate messages.** Who an answer was about is kept for follow-ups as its own system message, not appended to
  the answer: when it was part of the answer text, the model copied "[About: ...]" into its next reply and the recruiter saw it.
- **Timing is a sentence that names its clock, not a table column.** Each stage has three targets on three clocks (time in the stage,
  waiting to be booked, waiting for feedback; fictional policy in `data/scenarios.py`). A "median days vs target" column put one clock's
  target beside another clock's sentence (14-day time-in-stage target in the table, "3 people over the scheduling target" after it).
  The "where are we losing candidates" table is now funnel and reasons only; timing appears only where it explains a loss, with the clock
  and its own target in words. Analytics has no per-person waits, so the hint forbids listing them from it (the model had invented
  "16, 16 and 22 days" from a median and a maximum), and an example in the hint uses invented numbers so it cannot be copied as data.
- **Counts say who they count.** In scheduling, `over_target_count` counted everyone who ever reached the stage (14 of 17), and was read
  as people there now; the model sees it as `ever_waited_past_booking_target` (past tense: a name with "waiting" in it was read as "still waiting"). Reason codes arrive in words.
- **Developer view counts say what they count**: "8 active candidates" for a whole-role read, and record IDs grouped by kind
  ("8 candidate records", "18 feedback entries").

## What would change with a real ATS
Verified identity and role-based access; requisition and field-level authorization; opaque candidate references and PII redaction before model calls;
provider data-handling terms (zero retention) and region; retention and deletion policy; durable audit logs; fairness and adverse-impact review of any
ranking assistance with the human decision-maker kept in the loop; read-only scoped credentials and human approval for any write; ATS API realities
(pagination, rate limits, eventual consistency, messy data, free-text stages); rate limiting and cost controls; monitoring and eval drift tracking.
