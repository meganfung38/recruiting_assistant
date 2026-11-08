"""Builds the orchestrator for a UI/CLI turn from the environment. Kept separate so tests can swap it for a scripted one.
Nothing returned here is cached across turns: each turn runs in its own event loop (see Orchestrator.run_turn)."""
import os
from pathlib import Path

from dotenv import load_dotenv

from agent.limits import Limits
from agent.llm import OpenAILLM
from agent.orchestrator import Orchestrator
from services.db import db_path


TURN_LOG = Path(__file__).resolve().parent.parent / "logs" / "turns.jsonl"   # one JSON line per answered question (gitignored)


class NotConfigured(Exception):
    """A setup problem the user can fix; the message is safe to show."""


def preflight() -> str | None:
    """None when ready, else a short instruction. Never reveals secret values."""
    load_dotenv()
    if not Path(db_path()).exists():
        return f"The database {db_path()} was not found. Run `python -m data.seed` first."
    if not os.environ.get("OPENAI_API_KEY"):
        return "OPENAI_API_KEY is not set. Copy `.env.example` to `.env` and fill it in."
    if not os.environ.get("OPENAI_MODEL"):
        return "OPENAI_MODEL is not set. Add it to `.env` (see `.env.example`)."
    return None


def make_orchestrator() -> Orchestrator:
    problem = preflight()
    if problem:
        raise NotConfigured(problem)
    return Orchestrator(OpenAILLM(), limits=Limits(), log_path=os.environ.get("RECRUITING_TURN_LOG") or TURN_LOG)
