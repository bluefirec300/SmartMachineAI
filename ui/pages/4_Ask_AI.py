from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ask import AskEngine


st.set_page_config(page_title="Ask AI - SmartMachineAI", page_icon="💬", layout="wide")
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

engine = st.session_state["ask_engine"]

if engine is None:
    st.error(f"Could not start the Q&A engine: {st.session_state['ask_engine_error']}")
    st.stop()

status_col, clear_col = st.columns([4, 1])

with status_col:
    if engine.ai_provider is not None:
        st.caption(f"AI provider: {engine.ai_provider.provider} ({engine.ai_provider.model})")
    else:
        st.caption(f"AI phrasing unavailable ({engine.ai_error}) - showing deterministic-only answers.")

with clear_col:
    if st.button("Clear conversation"):
        st.session_state["ask_history"] = []
        engine._pending = None
        st.rerun()

st.info(
    "Root-cause questions can take up to ~10 minutes to answer on this "
    "hardware (CPU-only inference). Simpler questions (current value, "
    "trend, threshold) usually take 1-3 minutes. This page waits for the "
    "full answer, so please don't close the tab while it's thinking."
)

for message in st.session_state["ask_history"]:
    with st.chat_message(message["role"]):
        st.write(message["content"])

question = st.chat_input('Ask about the factory, e.g. "why is the compressor pressure dropping"')

if question:
    st.session_state["ask_history"].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("Thinking - this can take several minutes..."):
            try:
                answer = engine.ask(question)
            except Exception as error:
                answer = f"Error answering question: {error}"
        st.write(answer)

    st.session_state["ask_history"].append({"role": "assistant", "content": answer})
