# AI use

Written for the author to review and correct before submission; it describes what was done with Claude Code and how each part was checked.

| Part | AI role | How it was verified |
|---|---|---|
| Fictional prose data (CVs, emails, feedback, exit reasons) | Generated from curated structured rows, then frozen in `data/prose/*.json`. Interview notes were regenerated on 2026-11-08 with a deterministic composer (`data/compose_feedback.py`) from each record's stage, date, interviewer, scores, recommendation and the candidate's CV, because the first notes reused a small sentence bank (Dahlia's and Junko's notes were identical) | `data/check_consistency.py` (skills vs gaps, dates, recommendation, stage, no stray instruction-like text), the seed tests, and a distinctness check (81 of 82 interview notes distinct) |
| Services, analytics, MCP server, executor, evidence validation | Written with AI assistance | Unit tests; analytics and status sentences are recomputed independently from the raw tables (`tests/scenario_facts.py`, `tests/test_services.py`) against hand-authored `data/seed_expectations.json` |
| Streamlit UI and presenter | Written with AI assistance | Streamlit AppTest suite; leak checks for IDs, brackets and tool names in the default view |
| Tests | Written with AI assistance | Run on every change; boundary and injection tests check what the model is actually sent |
| Prompts | Drafted with AI assistance, tuned from live failures | Live runs recorded in `docs/eval_results.md` and the notes there |
| Evals | Written with AI assistance | Checker logic has unit tests; results are small samples and are labelled as such |
| README and design notes | Drafted with AI assistance | Read against the code |

Time spent and anything else the author did by hand: _to be filled in by the author._
