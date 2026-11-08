# UX redesign proposal (for review; nothing is implemented)

Responds to `feedback.md`. Evidence for every claim below comes from running the real app with the real model on your nine workflows; the captured output is in `docs/ux_review/baseline_current_ui.md`. No application code was changed while preparing this.

## 0. Summary

**Diagnosis.** The system was built to be *auditable*, and the UI currently shows the audit trail as if it were the product. In the baseline, 9 of 10 answers show raw internal IDs or citation syntax to the recruiter, 8 of 10 show snake_case field names or detector labels, the median answer is 190 words (the shortlist is 371), and a candidates table was attached to 4 answers that were not shortlists.

**Core idea.** *The model decides and explains; the application states facts and shows its work on demand.* Anything computable from data (a name, a stage, a wait time, a count, a due date) is rendered by the application from the verified record, never copied by the model. The model supplies only what needs judgment: which items, in what order, and why.

**Three tiers of information.** (1) *Primary:* headline answer, the items it concerns, material caveats. (2) *Secondary, on demand:* human-readable supporting evidence. (3) *Technical, behind a switch:* raw IDs, validation, tool calls.

**Two defects found while investigating, independent of design:** a Streamlit rendering bug (cause identified, section 2) and an intermittent empty model response that surfaces as "The model returned no answer" (3 of 5 tries on the pipeline question). Both can be fixed first, separately (Phase 0).

**Scale of change.** Presenter/UI work is substantial but contained. The answer schema, prompts and validator must also change (section 6); that is the largest and riskiest part, so I propose a small spike before committing to it.

---

## 1. What I measured (baseline, before any change)

Real model (`gpt-5.4-mini`), real app, default (visible without clicking) view only.

| Workflow | Words | Raw IDs / citations | Findings |
|---|---|---|---|
| Shortlist | 371 | 23 | Narrative and table say the same thing. Injection warning shown 3 times. |
| Follow-up "why 2nd above 3rd" | 163 | 10 | Good content; raw citations; a technical "not machine-checked" banner. |
| Follow-up "what was in the CV" | 87 | 3 | Paraphrase only; source text not inspectable. |
| Comparison (C104 vs C110) | 190 | 15 | IDs lead names; table repeats prose; no side-by-side. |
| Individual review (Junko) | 264 | 12 | Opens with a warning about a *different* candidate; a banner exposes validator internals. |
| Pipeline | 10 | 0 | **Error: empty model response** (intermittent). |
| Overdue tasks | 361 | 50 | Same ten tasks shown twice (two tables); IDs for candidates and roles. |
| Waiting longest | 193 | 11 | Right answer; `awaiting_scheduling`, `waiting_days`; an irrelevant candidate listed as a result row. |
| Cross-role attention | 179 | 6 | Sound reasoning; `over_target_or_overdue_share of 0.9091`; no table. |
| Security question | 147 | 7 | Detector labels listed as if meaningful; no source shown. |

Cross-cutting tallies (10 answers):

- Injection banner in the primary view: 5. In 2 of those the question was about something else.
- "Warnings from the assistant" restating the banner: 4.
- Candidates table on a non-shortlist answer: 4.
- Validator internals in a banner ("Figure ... is unverified: path ..."): 1.
- Technical "answered in plain text / not machine-checked" notice: 2.

**Ranking stability (your item 5).** 12 identical shortlist runs: 11 gave Dahlia, Junko, Hannelore; 1 gave Dahlia, Junko, **Mateo**. So your observation is real and reproducible (about 8%). The difference is not stage as such; it is the amount of interview evidence:

| Candidate | Current stage | Interview feedback exists for |
|---|---|---|
| Dahlia Ramanathan | Final Interview | Resume, Recruiter Screen, Technical, **Final** |
| Junko Takeda | Technical Interview | Resume, Recruiter Screen, **Technical** |
| Hannelore Voss | Final Interview | Resume, Recruiter Screen, **Technical** |
| Mateo Villareal | Technical Interview | Resume, Recruiter Screen **only** |

## 2. The rendering bug (your item 1)

**Cause.** `app.py` has this as a bare expression statement at module level:

```python
render_view(m["view"]) if m["role"] == "assistant" else st.markdown(m["content"])
```

For user messages, `st.markdown(...)` returns a `DeltaGenerator`. Streamlit's "magic" feature auto-displays the value of any bare expression via `st.write`, and for that object it prints the class documentation. It appears from the second turn, when history is re-rendered by that loop. I reproduced it: an element of type `Help` appears in the page tree holding `DeltaGenerator(...)`.

**Why my tests missed it.** My text-collecting helper looked at a fixed list of element types; `Help` was not on the list, and I never asserted on the *complete* set of element types.

**Fix (Streamlit layer, one line plus a test).** Write it as an `if/else` statement so no value is displayed. Regression test: after several turns, walk the entire rendered element tree and assert every element type is on an allow-list (no `Help`, no unknown types) and no text contains `DeltaGenerator`, `<function`, or `Docstring`. This test runs on every render path, including history re-rendering.

## 3. Design principles

1. **The application states facts; the model explains.** Names, stages, dates, waits, counts and metric values are rendered from the verified registry, referenced by ID in the structured answer. Wrong or inconsistent facts (e.g. a model writing the wrong stage) become impossible to display.
2. **One representation per fact.** Each piece of information appears once, in the form best suited to it.
3. **Progressive disclosure in three tiers** (section 4).
4. **Answer first.** No introductory filler; the first line is the answer.
5. **Presentation follows the question,** from a small set of patterns (section 5), with a safe fallback.
6. **Auditability is preserved by moving it, not removing it.**

## 4. Information hierarchy

| Tier | Audience | Contents | How reached |
|---|---|---|---|
| **1 Primary** | Recruiter | Headline answer; the items it concerns (cards/rows/tiles); how they were compared (1 line); material caveats only; relevant safety notice | Visible by default |
| **2 Supporting** | Recruiter, curious | Per item: the evidence behind each reason, as readable cards ("Technical interview feedback, Oct 28, Tobias Lindqvist: recommends advancing"), quoted source text where relevant; figure provenance in plain words | "See evidence" expander on each item |
| **3 Technical** | Developer/auditor | Raw record IDs, validation outcome and repair notes, tool-call trace, token counts | Sidebar switch "Show technical details" (off by default) adds a collapsed panel under each answer |

One quiet trust line sits under each answer: "Checked against the source records" (or an amber "Some details could not be verified" with a link to what, in plain language). It replaces today's banners about figures, validation and plain-text fallbacks.

Sketch (shortlist):

```
 Dahlia, Junko and Hannelore are the strongest Backend Engineer candidates so far.
 Compared on required skills first, then how thoroughly each has been interviewed.

 1  Dahlia Ramanathan                      Final Interview   [ Interviewed through Final ]
    - Strong system-design and coding feedback in both technical rounds
    - Led a team that redesigned the settlement pipeline (8 yrs experience)
    Gap: final decision still pending
    > See evidence

 2  Junko Takeda                           Technical Interview   [ Technical interview done ]
    ...
 3  Hannelore Voss                         Final Interview   [ Technical interview done ]
    ...
 Note: all three have completed at least a technical interview.

 Checked against the source records
```

## 5. Presentation patterns

Seven patterns plus a fallback. The model picks `kind` from the question's intent; the application validates that the chosen kind's required fields are present and falls back to `answer` otherwise.

| Kind | Used for | Shape | Taken from data by the application |
|---|---|---|---|
| `shortlist` | "top three", "who should we consider" | Headline; basis line; ranked cards (max 3, or as asked) with 2-3 reasons and at most 2 gaps each; evidence badge | Name, stage, evidence badge, wait status |
| `comparison` | "compare A and B" | Headline verdict; side-by-side columns (stage, interview evidence, strengths, gaps); one-line conclusion | Same fields per candidate |
| `candidate` | "status of X" | One focused card: stage, latest feedback in one sentence, what is outstanding, suggested next action | Stage, dates, waits, outstanding tasks |
| `metrics` | "where is the pipeline slowing" | Headline; 3-4 metric tiles; short interpretation; up to 3 next steps | Metric value, unit, "based on n", target |
| `tasks` | "what tasks are overdue" | Headline with counts; list grouped by urgency/owner: person, task, owner, how overdue, context | Names, owner, due date, overdue days |
| `attention` | "who has waited longest", "which roles need attention" | Headline; short ranked list (candidates or roles) with the reason in plain words | Waits, counts, names |
| `security` | "what was detected in X's CV" | Plain explanation; original text in a labeled, escaped panel | Original excerpt, location |
| `answer` (fallback, also follow-ups) | Anything else, short factual follow-ups | Headline plus up to 3 short paragraphs; sources on demand | none |

These are not templates the model fills in blindly: only the fields relevant to the chosen kind exist, which removes the current pressure to emit `candidates`, `figures` and `caveats` for every question (section 6).

Tasks sketch:

```
 10 recruiting tasks are overdue; 6 are for Backend Engineer.

 Most overdue
 Mateo Villareal    Book technical interview       Jordan Ellis    15 days overdue
 Kofi Adeyemi       Book technical interview       Jordan Ellis     9 days overdue
 Xiomara Lindholm   Collect interview feedback     Sam Whitaker     7 days overdue
 ...
 Suggested next step: the three scheduling tasks for Backend candidates have waited longest.
```

Metrics sketch:

```
 Technical Interview is where Backend candidates are getting stuck.

 [ 8 days ]            [ 14 of 17 ]            [ 3 waiting ]
 median wait to        waits over the          candidates not yet
 schedule (target 7)   7-day target            scheduled (up to 22 days)

 Candidates wait much longer to be booked than anywhere else in the process; feedback is quick.
 Next: book the three waiting candidates; review interviewer availability.
```

## 6. Answer schema, prompts and validation (these genuinely change)

**Where the current schema causes redundancy.**

- `answer_markdown` carries the whole narrative *and* `candidates[]` carries the same people again with rationale *and* `figures[]` repeats the numbers. Three representations of one answer.
- `candidates[]` must hold candidates, so the model lists irrelevant people (the injected candidate appeared as a "result" in the waiting-time answer) to satisfy validation or to mention a warning.
- `content_warnings` is model-authored but restates the deterministic banner (shown in 4 of 10 answers).
- Citations are inline `[cv:C104]` tokens inside prose, so the model's text is both the message and the audit trail.

**Proposed v2 (sketch).**

```json
{
  "kind": "shortlist | comparison | candidate | metrics | tasks | attention | security | answer",
  "headline": "<=200 chars: the direct answer, names not IDs",
  "basis": "<=240 chars: how things were compared (shortlist/comparison/attention)",
  "items": [
    { "ref": {"candidate_id": "C104"},
      "reasons": [ {"text": "<=140 chars", "support": ["feedback:FB-015", "cv:C104"]} ],
      "gaps":    [ {"text": "<=140 chars", "support": [...]} ],
      "ranking_note": "required only if ranked above a candidate with deeper interview evidence" },
    { "ref": {"task_id": "T-002"}, "context": "<=120 chars" },
    { "ref": {"role_id": "ROLE-PO"}, "reasons": [...] } ],
  "metrics":   [ {"label": "Median wait to schedule", "evidence": "analytics:ROLE-BE@2026-11-07",
                  "field_path": "stages[Technical Interview].scheduling.lag_median_scheduled_days", "unit": "days"} ],
  "caveats":    [ {"text": "<=160 chars", "support": [...]} ],
  "next_steps": ["<=120 chars"],
  "attachments": ["flagged_source:C117"]
}
```

What this changes:

- **Facts come from `ref`, not text.** The UI resolves name, stage, waits, task details from the registry. The model never writes "currently in Final Interview".
- **Metrics are the validated figures.** The displayed number is the registry value formatted by `unit`, so the figures table disappears and an unverified number cannot be shown as verified.
- **`support` is structured and validated** exactly as citations are today (exists in this turn's results; `cv:` only for retrieved records; `search:` for search rows).
- **Hard length limits** encourage brevity and make "no repeated content" checkable.
- **`caveats`** hold only *material* items. Incomplete-comparison and flagged-content notices are generated by the application, not by the model.
- **`attachments`** let the model *request* that the application show a flagged source panel, without copying the text.

What stays unchanged: the seven-tool allowlist; retrieval-before-ranking; retrieve-first pushback with its bounded retries; citation and figure verification; candidate-seen checks; one repair round; bounded rounds/calls/time; the single `ToolExecutor` boundary; all projection rules.

New validator checks: kind-specific required fields; no internal IDs or snake_case terms in recruiter-visible text (regex; one repair); every `ref` appears in this turn's results; evidence-level rule (section 9); no sentence repeated across headline/body/caveats (near-duplicate check).

Prompt changes: lead with the answer; names only; no IDs, field names or tool names in text; never quote candidate-supplied instructions (the application shows them); mention a flagged candidate in prose only if relevant; keep caveats to what changes the recommendation; choose `kind` from the question's intent; concise defaults with permission to expand when asked.

**Cost/risk.** The structured format is more demanding for a small model. I propose a spike first (Phase 3a): implement the schema for `shortlist`, `tasks` and `answer`, run N=20 on the baseline questions, and measure compliance and repair rate before building the rest. If compliance is poor, fall back to fewer kinds.

## 7. Citations a recruiter can use

**What changes for the user:** no `[cv:C104]` anywhere. Each reason can be opened to show the evidence behind it, in words.

**Mechanism.** The registry already knows every record the model received. At registration time it will also build a small deterministic *evidence card* from the already-projected data (in memory only, never in the trace or model context):

| Source | Card title | Detail shown |
|---|---|---|
| Interview feedback | "Technical interview feedback, Oct 28 (Tobias Lindqvist)" | Recommendation in words; the feedback summary quoted, labeled as the interviewer's note |
| Candidate record (`cv:`) | "Dahlia Ramanathan: full record opened" | Current stage, history, CV excerpt quoted and labeled "candidate-supplied" |
| Search row (`search:`) | "Dahlia Ramanathan: search summary (record not opened)" | Skills matched, waiting status; plainly says the full record was not read |
| Stage event | "Entered Technical Interview on Oct 21" | Dates |
| Email | "Email from the candidate, Oct 26" | Subject and quoted body, labeled candidate-supplied |
| Task | "Book technical interview, owner Jordan Ellis" | Due date, overdue days |
| Analytics | "Backend Engineer pipeline, as of Nov 7" | Metric in words, "based on 11 candidates", target |

Under each item, an expander "See evidence" lists the cards grouped by reason, so the recruiter sees *which evidence supports which claim*. The difference between a full record and a search summary is conveyed in plain words and by the card title, never by syntax. Candidate-supplied text inside cards is rendered as escaped plain text.

**Validation is untouched:** the `support` references go through the same registry checks; the cards are derived from the same data.

**Tradeoff:** one expander per item is more testable than a popover (Streamlit's headless harness has no first-class popover element). I recommend expanders.

## 8. Names, not IDs

- The model is told to use names and the schema carries IDs only in `ref`/`support`, which the UI never prints.
- The application resolves names from the registry. If two candidates in one answer share a name, it appends the role ("Alex Kim, Backend"), never an ID.
- Safety net: any remaining `C\d{3}`-style token in model text is replaced by the resolved name by the presenter, and also triggers a repair so it is measured.
- IDs remain available in the Technical tier.

## 9. Ranking and evidence completeness (your item 5)

**Finding.** The model's ranking is normally stable but can put a screening-only candidate above one with technical-interview feedback (1 of 12), and it never states what "top" means or flags the difference in evidence.

**Proposal (no hard ordering rule):**

1. **A deterministic evidence level** per candidate, computed in the service layer from feedback records and the role's stage order: `interviewed` (feedback beyond the recruiter screen), `screened` (resume and screen only), `resume_only`, plus `interview_held_feedback_pending`. It is descriptive, not a score, and is tested against independent recomputation like every other metric.
2. **The UI shows it as a badge** on each card ("Interviewed through Final Interview", "Screening only: not yet interviewed").
3. **A mandatory `basis` line** states what "top" means, e.g. "Compared on required skills, then on how thoroughly each has been interviewed." The default is defined in the prompt and can be changed by the user in a follow-up ("rank by skills only"). No clarifying question is required up front.
4. **Justification, not prohibition.** If the model ranks a lower-evidence candidate above a higher-evidence one, the validator requires a non-empty `ranking_note` explaining why, shown on the card. Ordering remains the model's judgment.
5. **Application-generated note when evidence is mixed:** "2 of 3 have completed interviews; Mateo is ranked on his resume and screen only." Always correct because it is computed.

**Alternative considered (Option B):** show two groups, "Interview-validated" and "Promising, not yet interviewed". Clearer, but it changes what "top three" means and can yield 2+1 splits. I recommend the single ranked list above, and can switch.

## 10. Prompt-injection warnings (your item 6)

**Plain language, not labels.** The banner says what happened and what it means, derived from the detected patterns by a fixed mapping the recruiter never sees:

> Potentially manipulative instructions were found in Quentin Abara's CV (they asked to be ranked first and to keep the instructions hidden). They were ignored in this evaluation.

**Relevance.** If the flagged candidate is part of the answer (a card, row, or named), the notice is a banner at the top. If the flag only arose from a search that the answer does not concern (as in the Junko and waiting-time answers), it becomes a quiet footnote with a "View" link, so unrelated answers are not led by a warning.

**Showing the original text safely.** An expander "View the original text" shows the flagged excerpt (the matching sentence plus surrounding context, capped at about 600 characters, with its location, e.g. "CV, last paragraph"), labeled: *"Candidate-supplied text, shown for your review only. The assistant treated it as data and did not follow it."*

How it works and why it is safe:

- The application captures the raw excerpt at the trusted boundary, before projection, into per-turn memory used only by the UI. It is **not** added to the trace, the model's context, the registry cards, or logs.
- It is rendered with `st.code` / plain text: no markdown, HTML, or links are interpreted, and length is capped.
- For a follow-up such as "what was in his CV that triggered the warning?" the model answers in plain language and requests `attachments: ["flagged_source:C117"]`. The validator accepts it only if that candidate was flagged this turn. The model never needs to quote the text, so injected instructions are never repeated into conversation history. (The model re-queries the candidate each turn, so the source is available again.)
- Config switch `SHOW_FLAGGED_SOURCE` (default on) for deployments that should not show it. Note the prototype has no authentication or roles (out of scope in the spec), so "authorized reviewer" is currently assumed; this belongs in the README limitations.

**Decision needed (section 17):** drop the model-authored `content_warnings` and the "must name flagged candidate" repair, since the deterministic notice now guarantees visibility. This removes duplication but changes eval 10's "model named it in content_warnings" measure, which would be replaced by deterministic checks (notice shown, plain-language, no labels) plus the existing obey/repeat/merit checks.

## 11. Sidebar, framing, loading, empty states

- **Remove** the default-role dropdown. Roles are specified in the question; follow-ups keep context through conversation history. The orchestrator keeps its `role_context` parameter for the CLI and evals only. Suggested questions name the role explicitly.
- **As-of date:** a small "Sample data as of Nov 7, 2026" line; no UTC explanation.
- **Notices:** one understated footer in the sidebar: "Sample data: all people are fictional. Decision support only; hiring decisions stay with you."
- **Controls:** "New conversation"; "Show technical details" switch.
- **Loading:** status text derived from the tools actually being called ("Looking up candidate records", "Checking pipeline data", "Reviewing tasks"), via a small progress hook in the orchestrator. Real activity, not invented narration. Generic fallback.
- **Empty state:** a short welcome and about six example questions grouped by workflow (the three current suggestions stay).
- **Errors:** plain-language, one line, with a retry hint; the empty-model-response failure is retried once automatically (Phase 0).

## 12. Safeguards: preserved vs changed

| Safeguard | Status |
|---|---|
| Seven-tool MCP allowlist; `submit_answer` stays local | Preserved |
| Deterministic citation and figure validation | Preserved; applied to `support` and `metrics` |
| Candidate-retrieval requirement; retrieve-first pushback; bounded retries | Preserved |
| Prompt-injection detection, untrusted wrapping, safe handling | Preserved; source display added (section 10) |
| Incomplete-comparison disclosure | Preserved; notice generated by the application |
| Bounded rounds, calls, time; one repair round | Preserved |
| Trace excludes CV/feedback/payload text | Preserved; flagged excerpt kept out of trace and model context |
| `search:` vs `cv:` distinction | Preserved internally; shown in words |
| Model-authored `content_warnings` | **Changed** (decision 1) |
| Eval 10/14/15/16 | **Migrated** to the new schema; criteria preserved, one measure replaced |

## 13. Which layer each change belongs to

| Change | Streamlit/presenter | Boundary / registry | Services | Schema / prompts / validator | Evals |
|---|---|---|---|---|---|
| Fix rendering bug + regression test | yes | | | | |
| Empty-completion retry | | | | orchestrator | |
| Sidebar, tiers, loading, empty state | yes | progress hook | | | |
| Plain-language banners, relevance | yes | | | | |
| Flagged source panel | yes | capture excerpt | | `attachments` | yes |
| Evidence cards, "See evidence" | yes | build cards | | `support` | |
| Names not IDs | resolve + scrub | names (exists) | | prompt + ID check | |
| Evidence badge | render | | `evidence_level` | `ranking_note` rule | yes |
| Patterns per question type | renderers | | | `kind` + per-kind fields | yes |
| Metrics tiles from validated values | render | | | `metrics` | yes |
| Brevity, no duplicates | | | | length limits, duplicate check, prompt | yes |

## 14. Validation plan

Two kinds of tests, kept separate.

**A. Deterministic (no model, runs in CI).**
1. *Element allow-list on every render path* including history re-render (the bug).
2. For each `kind`, scripted structured answers → default view contains the expected components and none of: raw IDs, snake_case terms, detector labels, tool names, "validation", "machine-checked", "untrusted".
3. Word budgets per kind (initial targets below); no repeated sentence.
4. Technical details hidden by default and present when switched on.
5. Flagged-source panel: escaped (HTML/markdown/link payload rendered as text), capped, labeled, absent from trace/messages; banner vs footnote relevance rule.
6. Evidence cards: correct titles and dates; search summary clearly distinguished from full record.
7. Validator: kind requirements, `support`, `metrics` verification, names, `ranking_note` rule, `attachments`.
8. Services: `evidence_level` against independent recomputation.
9. Sidebar, empty state, loading labels, error states.

**B. Model-dependent response quality (opt-in, key required).**
- A regression set of 12 questions: your nine workflows plus three that guard against overfitting: a false premise ("this role has been open four months" for Manufacturing), an unknown candidate (C999), and a write request (decline). Each run N=3 to 5 on the final code.
- Automatic checks on the *recruiter-visible output*: acceptable `kind` chosen; primary-view word budget; zero raw IDs/jargon; names used; every reason has support; exact task set (10 overdue) for the tasks question; Mateo at 22 days for "waiting longest"; shortlist has at most 3 cards, each with a badge, and obeys the ranking-note rule; the flagged panel appears when asked; a follow-up does not re-render a shortlist; empty-completion rate.
- Multi-turn: three scripted conversations (shortlist, then "why 2nd above 3rd", then "what was in the CV"; compare, then follow-up; tasks, then "who owns the oldest one").
- Existing evals 10, 14, 15, 16 re-run on the new schema.
- **Human review:** rendered text of every workflow saved under `docs/ux_review/after/` next to the baseline and scored against five questions (direct, scannable, non-redundant, supported, useful to a non-technical recruiter). I will not use an LLM judge for pass/fail: it would reward fluent length and hide the failures this redesign targets.

**Initial targets (to tune with evidence, not claims):** headline <= 25 words; shortlist primary view <= 200 words (baseline 371); other kinds <= 150 words of prose plus tiles/rows; zero IDs/snake_case/detector labels in the primary view (baseline 9/10 and 8/10 violate); no duplicate sentences; flagged notice present 100% when the flagged candidate is in the answer.

**Limits I will state plainly.** Headless tests cannot judge layout, spacing, or visual hierarchy. I cannot view the app in a browser from here, so layout and visual polish need your review before the redesign is called done.

## 15. Implementation plan

| Phase | Scope | Size | Gate |
|---|---|---|---|
| **0. Independent fixes** | Rendering bug + element allow-list test; automatic retry of an empty model response (bounded, with a nudge); move validator text out of banners | S | Tests pass; pipeline question no longer errors in 10 live runs |
| **1. Foundations** | Evidence cards in the registry; flagged-excerpt capture; plain-language notices + relevance; sidebar simplification; technical-details switch; loading hook; three-tier view model; name resolution + scrub | M | Deterministic tests 1, 4, 5, 6, 9 |
| **2. Services** | `evidence_level` with independent recomputation tests | S | Services tests |
| **3a. Schema spike** | v2 schema for `shortlist`, `tasks`, `answer`; N=20 on baseline questions: compliance, repair rate, cost | S-M | **Go/no-go**: compliance and repair rate acceptable |
| **3b. Full schema** | Remaining kinds, validator checks, finalizer changes, renderers, eval migration (10/14/15/16) | L | All deterministic tests; evals re-run |
| **4. Evaluation and tuning** | Regression set, multi-turn conversations, prompt tuning, before/after review artifacts, README/design notes | M | Success criterion below; your browser review |

Phase 0 can ship immediately and independently. Phases 1 and 2 are valuable even if the schema spike fails. Rollback is per phase (each lands behind passing tests; v1 rendering remains until 3b completes).

## 16. Risks and tradeoffs

- **Model compliance with a richer schema** (smaller model): addressed by the spike, a small set of kinds, a repair round, and the `answer` fallback.
- **More app logic means more places to be wrong:** rendering facts from the registry is a trade of model errors for application bugs, covered by deterministic tests and independent recomputation.
- **Cost and latency:** shorter outputs should help; extra repair rounds could hurt. Measure in the spike. (Current recovery runs already cost about 45-56k tokens per turn.)
- **Evals change meaning** where they depended on model-authored warnings; migrated deliberately, with the change documented.
- **Overfitting to my questions:** mitigated by the three guard questions, N>1 runs, and keeping the fallback pattern; real recruiter feedback is the true test.
- **Flagged-source display** has no access control in the prototype.

## 17. Decisions I need from you

1. **Drop model-authored `content_warnings`** and rely on the deterministic notice (recommended). Replaces one eval-10 measure.
2. **Ranking:** Option A (single ranked list + evidence badge + mandatory basis + justification rule), recommended; or Option B (two groups).
3. **Evidence level definition:** "interviewed" = feedback exists beyond the recruiter screen. Confirm, or specify.
4. **Remove the role dropdown entirely;** keep `role_context` for CLI/evals only (recommended).
5. **Technical details:** a sidebar switch (recommended) or a collapsed panel always present under each answer.
6. **Show flagged source text** to any user of the prototype, with a config switch to hide it (recommended).
7. **Loading status from real tool activity** via an orchestrator hook (recommended).
8. **Start with Phase 0 now,** separately, before the redesign (recommended).
9. **Duplicate names** disambiguated by role rather than ID (recommended).
