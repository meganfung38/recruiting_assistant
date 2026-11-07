# Recruiting Assistant

Recruiter-facing assistant over a **fully fictional** recruiting dataset (see `project-spec.md`).
Work in progress: increments 0, 1a and 1b (scaffold, structured dataset, frozen prose) are done.

All people, emails (`example.com`) and companies are synthetic.
The fixed as-of clock is **2026-11-07 12:00 PST (20:00 UTC)**; timestamps are stored in UTC.

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add OPENAI_API_KEY later (not needed yet)
```

## Seed the database and run tests
```bash
python -m data.seed                      # -> data/recruiting.db (injected variant, default)
python -m data.seed --variant baseline --db /path/to/baseline.db
python -m data.check_consistency        # prose vs structured facts (also run in tests)
python -m pytest
```
Set `RECRUITING_DB_PATH` to choose which SQLite file the services read.

## Data design (so far)
`data/scenarios.py` holds the curated facts (who is in which stage, with dates and outcomes).
`data/seed.py` turns them into SQLite deterministically. Prose (CVs, emails, feedback summaries, exit reasons) was written from those rows by an AI
assistant, reviewed, and frozen in `data/prose/*.json`; seeding makes no model calls.
`data/check_consistency.py` verifies the prose against the structure (CV skills vs the declared
gaps, dates in feedback and invites, scheduling-delay threads, no instruction-like text outside the
C117 payload). `data/seed_expectations.json` is hand-authored and checked against an independent recomputation
(`tests/scenario_facts.py`).

Fictional policy targets (days) per interview stage: time-in-stage, entered-to-booked, interview-to-feedback.
Recruiter Screen 7/3/2, Technical Interview 14/7/2, Hiring Manager Interview 10/5/2, Final Interview 7/5/2.
Resume Review: 5 days in stage.
