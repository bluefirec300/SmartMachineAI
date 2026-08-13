from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ask import AskEngine


st.title("💬 Ask AI")

if "ask_engine" not in st.session_state:
    try:
        st.session_state["ask_engine"] = AskEngine()
        st.session_state["ask_engine_error"] = None
    except Exception as error:
        st.session_state["ask_engine"] = None
        st.session_state["ask_engine_error"] = str(error)

if "ask_history" not in st.session_state:
    st.session_state["ask_history"] = []

# Holds the in-flight background question, if any:
#   {"question": str, "status": "running" | "done", "answer": str | None,
#    "started_at": float}
# A plain dict living inside session_state (not new session_state keys)
# so a background thread can safely update it without touching Streamlit's
# script-run machinery directly.
if "ask_job" not in st.session_state:
    st.session_state["ask_job"] = None

engine = st.session_state["ask_engine"]

if engine is None:
    st.error(f"Could not start the Q&A engine: {st.session_state['ask_engine_error']}")
    st.stop()


def _run_job(engine: AskEngine, question: str, job: dict) -> None:
    try:
        job["answer"] = engine.ask(question)
    except Exception as error:
        job["answer"] = f"Error answering question: {error}"
    job["status"] = "done"


job_running = st.session_state["ask_job"] is not None and st.session_state["ask_job"]["status"] == "running"

status_col, clear_col = st.columns([4, 1])

with status_col:
    if engine.ai_provider is not None:
        st.caption(f"AI provider: {engine.ai_provider.provider} ({engine.ai_provider.model})")
    else:
        st.caption(f"AI phrasing unavailable ({engine.ai_error}) - showing deterministic-only answers.")

with clear_col:
    if st.button("Clear conversation", disabled=job_running):
        st.session_state["ask_history"] = []
        st.session_state["ask_job"] = None
        engine._pending = None
        st.rerun()

st.info(
    "Root-cause questions can take up to ~10 minutes to answer on this "
    "hardware (CPU-only inference). Simpler questions (current value, "
    "trend, threshold) usually take 1-3 minutes. The question keeps "
    "running in the background even if you switch to another page - "
    "come back to this page any time to check on it."
)

for message in st.session_state["ask_history"]:
    with st.chat_message(message["role"]):
        st.write(message["content"])

job = st.session_state["ask_job"]

if job is not None:
    with st.chat_message("assistant"):
        if job["status"] == "running":
            elapsed = int(time.time() - job["started_at"])
            minutes, seconds = divmod(elapsed, 60)
            st.info(
                f'Still thinking about: "{job["question"]}" '
                f"({minutes}m {seconds}s elapsed) - feel free to check "
                f"other pages, this keeps running in the background."
            )
        else:
            st.write(job["answer"])

    if job["status"] == "running":
        time.sleep(3)
        st.rerun()
    else:
        st.session_state["ask_history"].append({"role": "assistant", "content": job["answer"]})
        st.session_state["ask_job"] = None
        st.rerun()

question = st.chat_input(
    'Ask about the factory, e.g. "why is the compressor pressure dropping"',
    disabled=job_running,
)

if question:
    st.session_state["ask_history"].append({"role": "user", "content": question})
    new_job = {
        "question": question,
        "status": "running",
        "answer": None,
        "started_at": time.time(),
    }
    st.session_state["ask_job"] = new_job

    thread = threading.Thread(target=_run_job, args=(engine, question, new_job), daemon=True)
    thread.start()

    st.rerun()
