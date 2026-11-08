# Design notes

How the assistant is built and why. Each decision lists the problem it answers; the table at the end lists the failures, found in manual
testing (`docs/manual_testing/`) and evals (`docs/eval_results.md`), that led to them.

## Shape
`data/` (seeded SQLite, fixed clock) -> `services/` (deterministic queries and analytics) -> `mcp_server/` (seven read-only tools over MCP
stdio) -> `agent/` (model loop with a trusted executor and an answer validator) -> `app.py` (Streamlit) and `agent/cli.py`.

The model chooses tools and writes the answer; code does everything that must be right every time: data access, calculations, limits,
what the model may see, checking the answer, and what the recruiter is shown.

## Data
- **Scenario-first.** `data/scenarios.py` defines structured facts (stage events with dates and outcomes, feedback scores and
  recommendations, emails, tasks); `data/seed.py` builds SQLite from them deterministically. Prose was generated from those rows and frozen
  in `data/prose/*.json`, and `data/check_consistency.py` checks it against the structure. Expected numbers in
  `data/seed_expectations.json` are hand-written and compared, in tests, with both the services and an independent recomputation.
- **Stories that are computable.** Backend Engineer: waits to be booked for the Technical Interview, and withdrawals while waiting.
  Manufacturing Systems Engineer: rejections for a missing required skill. People Operations: late interviewer feedback.
- **Three clocks, three targets.** Each interview stage has a time-in-stage target, a booking target (entered to booked) and a feedback
  target (interview to feedback). They measure different things and are never substituted for one another.
- **A fixed clock** (Nov 7, 2026, 12:00 PST), because every reported number is "time since a stored date": with the real clock the stories,
  the expected values and eval comparisons would drift daily.
- **Interviewer notes are specific to each candidate** (`data/compose_feedback.py` builds them from each record's scores, recommendation and
  the candidate's CV), because notes assembled from a small sentence bank made two candidates' evidence word-for-word identical.

## Tools
- **Shaped around the recruiter's questions**, so most answers take one or two calls: a role or a person can be named
  ("Backend Engineer", "Hannelore Voss"); `get_candidates(role=...)` returns every active candidate's evidence for a shortlist in one
  result; `get_recruiting_overview` answers cross-role questions without the model adding up several results.
- **Facts arrive as sentences where the model would otherwise reconstruct them**: "Technical Interview since Oct 16 (22 days): interview
  not booked yet."; one line per past stage. Booked, held and feedback received are kept apart; the planned interview date is not in the
  data, and the sentence says so.
- **Recruiter vocabulary** in what the model reads (`days_open`, `days_waiting`, `waiting_for: "interview to be booked"`, `reached`,
  `there_now`, reasons such as "withdrew: waited too long to be scheduled"). Counts are named for who they count
  (`ever_waited_past_booking_target` includes people who have left). Fields the model narrated or ranked by (interview counts,
  recommendation tallies) are not shown. Services keep the spec's field names; only the model's copy is renamed.
- **Presentation advice travels with the data** (`how_to_show` in a result, e.g. "one table, overdue first"). The model followed it far more
  reliably there than in a tool description or the system prompt.
- **Errors say how to recover**: an unknown role lists the known roles; a malformed candidate ID points to looking the person up by name.

## Trust boundary (`agent/tool_executor.py`, `agent/projection.py`)
- Allowlist derived from the server's own tool list; strict argument models (also the source of the schemas the model sees, with a
  parity test against the server); per-turn limits (8 rounds, 24 calls, 90 s per turn, 32,000 characters per result; larger results are
  cut by whole records with a warning naming what was left out).
- **Projection** rebuilds every result from an explicit allowlist. Contact details never reach the model. Candidate-written text is wrapped
  as labelled untrusted data, and sentences matching the instruction detector are replaced by a marker before the model reads them; the
  original is kept only for the reviewer's screen (`agent/flag_sources.py`), never in the trace, logs or model messages.

## Answer contract and checks (`agent/answer_schema.py`, `agent/evidence.py`, `agent/finalizer.py`)
- **The model owns judgment and wording; the application owns provenance.** `submit_answer` takes plain prose, the IDs of the people and
  roles it is about, and an optional ranking with one-line reasons and gaps. The model writes no citations, IDs or figure paths: that
  bookkeeping caused most repair rounds and display bugs when the model did it.
- **Deterministic checks** against what this turn's tools returned: every declared person and role appeared; every ranked person's record
  was read; every number in the prose occurs in the data for the people or roles named on the same line (dates, times, IDs, list numbering
  and rank columns excluded); every "N-day <clock> target" is that clock's target for the stage on the line. One repair round with the
  specific problems; anything still unmatched is shown as not verified. The checks prove presence, not that a sentence is right.

## Presentation (`agent/presenter.py`, `agent/cards.py`, `app.py`)
- **One "who is this answer about" set** (ranked, declared, then named in the prose, in order) drives both the flag block and the evidence
  cards, so the original flagged text never appears without the notice that explains it, and never for someone the answer is not about.
- **Evidence cards** have the same shape for everyone: skills, one line per interview stage with the interviewer's note, anything still
  pending. Resume-review notes are left out for everyone, so cards are comparable side by side.
- **A small display safety net** (`agent/humanize.py`) maps a stray ID to a name and drops citation tokens line by line, never touching list
  markers or table pipes.
- **Developer view and turn log**: one block per model round (tokens including reasoning, time, each tool call and what came back, the
  answer's verdict). Each turn is appended to `logs/turns.jsonl` (IDs, counts, sizes, tokens; no record text), which is how every
  manual-testing observation was traced before code changed.

## Conversation and model
- Earlier turns are replayed as question, answer, and a separate note of who the answer was about (never old tool results), so follow-ups
  are cheap and re-fetch facts. The note is its own message: appended to the answer, it was copied into the model's next reply.
- OpenAI Responses API with reasoning effort `low` (`agent/llm.py`): Chat Completions allows no reasoning together with function tools
  for `gpt-5.4-mini`, and without it the agent misjudged simple comparisons. Medium was measured: slower, not more accurate here. Requests
  time out after 20 s so a stalled request is retried within the turn limit.
- The system prompt (`agent/prompts.py`, about 8k characters) holds general rules and field meanings, not per-question routing.

## Failures that shaped the design
| Observed | Cause | Change |
|---|---|---|
| Backend's "14 of 17 over target" reported for Manufacturing | three similar analytics results; the model combined them | cross-role overview tool; numbers checked per role on each line |
| Shortlist read 6 of 8 candidates, 57k tokens, 6 rounds | 6-ID cap as a proxy for result size; large records | whole-role comparison view in one call: 2 rounds, about 13k tokens |
| "Where is Hannelore" took 11 calls | no lookup by name | names accepted by tools |
| "Where does 22 days come from?" | status sentence without dates | dated status and history sentences |
| Correction round for a figure path the model guessed | model asked to write evidence paths | answer contract: no paths; deterministic number checks |
| A feedback list collapsed into one line | regex removal of an ID broke a list marker | line-safe display clean-up; the model writes no IDs |
| Top-three evidence uneven; two candidates' notes identical | panel built from the model's citations; templated prose | uniform cards from records; notes regenerated per candidate |
| Flagged text shown with no explanation | notice and panel used different rules | one subject set; a titled block or nothing |
| "[About: Yara Eklund]" shown to the recruiter | history note appended to the answer was imitated | note kept as a separate message, stripped if copied |
| Medians and targets looked contradictory | unlabelled column mixing two medians and clocks | timing only in sentences naming the clock; funnel table |
| "7-day time-in-stage target" (it was the booking target) | clock confusion | deterministic clock-target check |
| "Who waited longest of three" picked the wrong person | no reasoning; unordered comparison | Responses API reasoning; list waits largest first |

## What would change with a real ATS
Verified identity and role-based access; requisition- and field-level authorization; opaque candidate references and PII redaction
before model calls; provider data-handling terms (zero retention) and region; retention and deletion policy; durable audit logs;
fairness and adverse-impact review of any ranking assistance, with the hiring decision left to a person; read-only scoped credentials and
human approval for any write; ATS API realities (pagination, rate limits, eventual consistency, messy data, free-text stages, a live
clock); rate limiting and cost controls; monitoring and eval drift tracking.
