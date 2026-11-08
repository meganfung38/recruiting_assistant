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


def render_details(v: dict) -> None:
    d = v["details"]
    if not (d["evidence"] or d["unverified"]):
        return
    with st.expander("Evidence"):
        if d["unverified"]:
            st.markdown("**Could not be verified**\n" + "\n".join(f"- {u}" for u in d["unverified"]))
        for g in d["evidence"]:
            st.markdown(f"**{g['person']}**")
            for card in g["cards"]:
                st.markdown(f"{card['title']}" + "".join(f"  \n{line}" for line in card["lines"]))
                if card["quote"]:
                    st.caption(card["quote"]["label"])
                    st.code(card["quote"]["text"], language=None, wrap_lines=True)        # inert text: nothing in it is interpreted


def render_flagged(v: dict) -> None:
    if not v["flagged"]:
        return
    with st.expander("View the original flagged text"):
        for f in v["flagged"]:
            st.markdown(f"**{f['candidate']}**: {f['source']}" + (f", {f['location']}" if f["location"] else ""))
            st.caption(FLAG_CAPTION)
            st.code(f["excerpt"], language=None, wrap_lines=True)                          # inert text: never rendered as markdown or HTML


def render_technical(v: dict) -> None:
    t = v["technical"]
    with st.expander(t["title"]):
        if t["rows"]:
            st.dataframe(t["rows"], hide_index=True, width="stretch")
        st.caption(f"{t['model_rounds']} model round(s) · {t['tokens']:,} tokens · {t['empty_retries']} empty reply retried. "
                   "Recorded by the application, not written by the model.")
        if t["corrected"]:
            st.caption("Corrected once after validation: " + "; ".join(t["corrected"]) + ".")
        for p in t["validation"]:
            st.caption(f"Validation: {p}")
        st.caption(f"Received {t['records_received']} records across {t['candidates_seen']} candidates. Cited: " + (", ".join(t["refs"]) or "none"))


def notice(n: dict) -> None:
    """A bordered note, not a yellow or red banner: the page stays monochrome. Real failures keep st.error."""
    if n["level"] == "error":
        st.error(n["text"])
    else:
        with st.container(border=True):
            st.markdown(n["text"])


def render_view(v: dict, technical: bool) -> None:
    for n in v["notices"]:
        if n["placement"] == "top":
            notice(n)
    st.markdown(v["answer"])
    if v["trust"]["text"]:
        st.caption(v["trust"]["text"])
    for n in v["notices"]:
        if n["placement"] == "foot":
            st.caption(n["text"])
    render_flagged(v)
    render_details(v)
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
    st.caption("Decision support only. Hiring decisions stay with you.")

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
