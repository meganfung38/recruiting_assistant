# Design notes

## Shape
`data/` (seeded SQLite, fixed clock) -> `services/` (deterministic queries and analytics) -> `mcp_server/` (eight read-only tools over MCP stdio)
-> `agent/` (model loop with a trusted executor) -> `app.py` / `agent/cli.py`.

The model only chooses tools. Every call goes through `ToolExecutor`, which enforces the tool allowlist, validates arguments, applies per-turn limits
(rounds, calls, time, result size), and projects the result before the model sees it.

## Trust boundary
- **Projection** (`agent/projection.py`): explicit per-tool field allowlists. Contact details never reach the model. Candidate-supplied text
  (CV, email, feedback, exit reasons) is wrapped as labelled untrusted data, and the sentences matching the instruction detector are replaced by a
  marker. Flags are computed from the original text; the original excerpt is kept only for the reviewer's screen (`agent/flag_sources.py`),
  never in the trace, evidence registry or model messages.
- **Evidence** (`agent/evidence.py`): a per-turn registry built only from structured fields of successful calls. The model submits a structured
  answer (`submit_answer`); the finalizer checks citations, candidates, exact figures and disclosure of candidates a truncated result omitted.
  One repair round, then the answer is shown with its problems visible. Validation checks presence and exactness, not meaning.
- **Truncation**: a size-capped result lists omitted IDs; the application tracks them and nudges the model to fetch them in small batches, or to
  disclose the gap by ID. This costs tokens and is the known efficiency trade-off.

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

## What would change with a real ATS
Verified identity and role-based access; requisition and field-level authorization; opaque candidate references and PII redaction before model calls;
provider data-handling terms (zero retention) and region; retention and deletion policy; durable audit logs; fairness and adverse-impact review of any
ranking assistance with the human decision-maker kept in the loop; read-only scoped credentials and human approval for any write; ATS API realities
(pagination, rate limits, eventual consistency, messy data, free-text stages); rate limiting and cost controls; monitoring and eval drift tracking.
