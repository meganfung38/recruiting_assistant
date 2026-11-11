"""Recruiter assistant: single-page Streamlit chat over a fictional dataset.

Run:  streamlit run app.py

What a recruiter sees by default is the answer, any notice that matters, and one line saying it was checked. The interview notes and
emails it relied on, and the original flagged text, are one click away; raw IDs, validation and tool calls are behind the
"Show technical details" switch. Streamlit re-runs this script on every interaction, so NOTHING with a live connection or event loop
is kept in st.session_state: each question opens one MCP session inside one asyncio.run() (Orchestrator.run_turn) and closes it.
Note for editors: never write a bare non-call expression statement here (Streamlit would display its value); use if/else statements.
"""
import logging

import streamlit as st

from agent import runtime
from agent.orchestrator import ConversationState
from agent.presenter import build_view, progress_label
from services.clock import AS_OF, PACIFIC

EXAMPLES = [
    "Where does Hannelore Voss stand in the process?",
    "Who are the top three Backend Engineer candidates, and why?",
    "Where are we losing candidates for the Backend Engineer role?",
    "Which interviews are still waiting on feedback?",
    "What needs my attention today?",
    "Which role needs the most attention?",
    "Compare Dahlia Ramanathan and Junko Takeda. Who has stronger interview evidence?",
]
FLAG_CAPTION = "Candidate-supplied text, shown for your review only. The assistant treated it as data and did not follow it."

st.set_page_config(page_title="Recruiting Assistant", layout="centered")
# The theme (.streamlit/config.toml) does the colouring. This only hides Streamlit's animated "running" figures in the corner:
# the in-chat status line already says what the assistant is doing.
st.markdown("<style>[data-testid='stStatusWidget']{display:none}</style>", unsafe_allow_html=True)


def reset() -> None:
    st.session_state.conversation = ConversationState()
    st.session_state.messages = []
    st.session_state.pop("queued", None)


def queue(prompt: str) -> None:
    st.session_state.queued = prompt


def render_evidence(v: dict) -> None:
    """One card per person, the same shape for everyone: skills, each interview's verdict and note, what is still pending."""
    if not v["evidence"]:
        return
    ranked = any(c["reason"] for c in v["evidence"])
    with st.expander("Evidence for this ranking" if ranked else "Evidence"):
        for n, c in enumerate(v["evidence"], 1):
            st.markdown(f"**{f'{n}. ' if ranked else ''}{c['person']}**" + (f" · {c['role']}" if c["role"] else ""))
            if c["status"]:
                st.caption(c["status"])
            for line in c["skills"]:
                st.markdown(f"- {line}")
            for i in c["interviews"]:
                st.markdown(f"- **{i['title']}**: {i['verdict']}")
                if i["note"]:
                    st.code(i["note"], language=None, wrap_lines=True)        # inert text: nothing in it is interpreted
            if c["pending"]:
                st.markdown(f"- Still pending: {c['pending']}")
            if c["gaps"]:
                st.markdown("- Gaps: " + "; ".join(c["gaps"]))


def render_flags(v: dict) -> None:
    """A titled, bordered block per flagged person the answer is about; the original text is inside it, collapsed."""
    for b in v["flags"]:
        with st.container(border=True):
            st.markdown(f"**{b['title']}**")
            st.markdown(b["text"])
            with st.expander("Show the original text"):
                for src in b["sources"]:
                    st.caption(f"{src['label'].capitalize()}" + (f", {src['location']}" if src["location"] else "") + ". " + FLAG_CAPTION)
                    st.code(src["excerpt"], language=None, wrap_lines=True)    # inert text: never rendered as markdown or HTML


def render_technical(v: dict) -> None:
    """One block per model round: its cost, the tools it called and what came back, or the answer it submitted and the verdict."""
    t = v["technical"]
    with st.expander(t["title"]):
        for r in t["rounds"]:
            with st.container(border=True):
                head = f"**Round {r['round']}** · {r['action']}" + (f" → {r['outcome']}" if r["outcome"] else "")
                st.markdown(head)
                st.caption(f"{r['tokens_in']:,} tokens in · {r['tokens_out']:,} out ({r['reasoning']:,} reasoning) · {r['seconds']:.1f}s")
                for c in r["calls"]:
                    st.markdown(f"{'' if c['ok'] else '⚠ '}`{c['call']}` → {c['returned']}")
                    for line in c["records"] + c["notes"]:
                        st.caption(line)
                for p in r["problems"]:
                    st.caption(f"Sent back because: {p}")
        if t["validation"]:
            st.caption("Not matched to the records in the final answer: " + "; ".join(t["validation"]))
        if t["about"]:
            st.caption("The answer is about: " + ", ".join(t["about"]))
        if t["flags_not_shown"]:
            st.caption("Instruction-like text was flagged in " + ", ".join(t["flags_not_shown"]) + " (not part of this answer, so not shown).")
        st.caption(f"Records received: {', '.join(t['refs']) or 'none'}. Recorded by the application, not written by the model.")


def render_view(v: dict, technical: bool) -> None:
    for n in v["notices"]:
        if n["level"] == "error":
            st.error(n["text"])
        else:
            with st.container(border=True):
                st.markdown(n["text"])
    render_flags(v)
    st.markdown(v["answer"])
    if v["trust"]["text"]:
        st.caption(v["trust"]["text"])
    render_evidence(v)
    if technical:
        render_technical(v)


def answer(prompt: str) -> None:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        try:
            progress = st.empty()                      # the running-status line disappears once the answer is ready
            with progress.status("Working on it", expanded=False) as status:
                orchestrator = runtime.make_orchestrator()
                result = orchestrator.run_turn(prompt, st.session_state.conversation,
                                               on_progress=lambda e: status.update(label=progress_label(e)))
            progress.empty()
        except runtime.NotConfigured as exc:
            st.error(str(exc))
            return
        except Exception:                      # never show a traceback to the recruiter
            logging.exception("assistant turn failed")
            st.error("Something went wrong while answering. Please try again.")
            return
        view = build_view(result)
        render_view(view, st.session_state.get("show_technical", False))
    st.session_state.messages.append({"role": "assistant", "view": view})


if "conversation" not in st.session_state:
    reset()

as_of = AS_OF.astimezone(PACIFIC)
problem = runtime.preflight()
with st.sidebar:
    st.markdown("### Recruiting Assistant")
    st.button("New conversation", on_click=reset, width="stretch")
    st.caption("Suggested questions")
    for i, text in enumerate(EXAMPLES):
        st.button(text, on_click=queue, args=(text,), key=f"example-{i}", disabled=bool(problem), width="stretch")
    st.divider()
    st.toggle("Developer view", key="show_technical",
              help="Adds the tools called, record counts, validation results and token use under each answer. For debugging and demos; recruiters don't need it.")
    st.caption(f"Sample data as of {as_of:%b} {as_of.day}, {as_of.year}. Everyone in it is fictional.")

if problem:
    st.error(problem)

# Read the input first (it is pinned to the bottom wherever it is called) so the examples are not drawn beside an answer that is about
# to be produced in this very run.
prompt = st.chat_input("Ask about candidates, interviews, pipelines or tasks", disabled=bool(problem)) or st.session_state.pop("queued", None)

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        # An if/else STATEMENT, not a conditional expression: Streamlit auto-displays the value of any bare non-call expression, and
        # st.markdown() returns a DeltaGenerator whose class documentation it would print into the chat.
        if m["role"] == "assistant":
            render_view(m["view"], st.session_state.get("show_technical", False))
        else:
            st.markdown(m["content"])

if not st.session_state.messages and not prompt:
    st.markdown("## What would you like to know?")
    st.caption("Ask about candidates, interviews, hiring pipelines or tasks, or pick a suggested question from the sidebar.")

if prompt and not problem:
    answer(prompt)
