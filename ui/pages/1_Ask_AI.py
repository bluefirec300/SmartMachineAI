from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ask import AskEngine, AskResult


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
#    "result": AskResult | None, "started_at": float}
# A plain dict living inside session_state (not new session_state keys)
# so a background thread can safely update it without touching Streamlit's
# script-run machinery directly.
if "ask_job" not in st.session_state:
    st.session_state["ask_job"] = None

# Phase 15 - session-scoped "currently discussing" equipment context.
# Lives ONLY in THIS browser session's st.session_state - AskEngine
# itself never stores this across calls, so one user's follow-up
# context can never leak into another session (the approved
# session-safety correction). ask_equipment_context is the canonical
# instance_key passed into AskEngine.ask_structured(); the _label is
# presentation-only.
if "ask_equipment_context" not in st.session_state:
    st.session_state["ask_equipment_context"] = None
    st.session_state["ask_equipment_context_label"] = None

engine = st.session_state["ask_engine"]

if engine is None:
    st.error(f"Could not start the Q&A engine: {st.session_state['ask_engine_error']}")
    st.stop()

# Phase 15 - cross-page entry point. Another page links here with
# ?ask_equipment=<instance_key>&ask_label=<display name> - read exactly
# once, sets the session-scoped equipment context, and is cleared
# immediately so a page refresh doesn't keep re-applying it. This never
# auto-submits a question - the engineer still has to type or ask one.
query_equipment = st.query_params.get("ask_equipment")

if query_equipment:
    st.session_state["ask_equipment_context"] = query_equipment
    st.session_state["ask_equipment_context_label"] = st.query_params.get("ask_label", query_equipment)
    st.query_params.clear()


def _run_job(engine: AskEngine, question: str, context_equipment: str | None, job: dict) -> None:
    try:
        result = engine.ask_structured(question, context_equipment=context_equipment)
        job["result"] = result
        job["answer"] = result.answer
    except Exception as error:
        job["result"] = None
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
        st.session_state["ask_equipment_context"] = None
        st.session_state["ask_equipment_context_label"] = None
        engine._pending = None
        st.rerun()

st.info(
    "Root-cause questions can take up to ~10 minutes to answer on this "
    "hardware (CPU-only inference). Simpler questions (current value, "
    "trend, threshold) usually take 1-3 minutes. The question keeps "
    "running in the background even if you switch to another page - "
    "come back to this page any time to check on it."
)

if st.session_state["ask_equipment_context_label"]:
    context_col, forget_col = st.columns([4, 1])

    with context_col:
        st.caption(
            f"Currently discussing: **{st.session_state['ask_equipment_context_label']}** - "
            "a follow-up question that doesn't name different equipment will assume this one."
        )

    with forget_col:
        if st.button("Forget", disabled=job_running, key="forget_equipment_context"):
            st.session_state["ask_equipment_context"] = None
            st.session_state["ask_equipment_context_label"] = None
            st.rerun()

for message in st.session_state["ask_history"]:
    with st.chat_message(message["role"]):
        st.write(message["content"])

        evidence = message.get("evidence")

        if evidence:
            with st.expander("View Evidence"):
                st.caption(
                    f"Intent: {evidence['intent'] or 'tag-level (existing pipeline)'} · "
                    f"Provider: {evidence['provider'] or 'unavailable'} · "
                    f"Grounding: {evidence['grounding_status']} · "
                    f"Deterministic fallback used: {evidence['fallback_used']}"
                )
                st.json(evidence["context"], expanded=False)

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
        result: AskResult | None = job.get("result")
        evidence = None

        if result is not None:
            # A single cleanly-resolved equipment carries forward as the
            # new session context for the NEXT follow-up (an explicit
            # new equipment named in a later question always overrides
            # this again - see AskEngine._answer_equipment_interpretation()).
            # Comparison/factory-summary answers resolve zero-or-two
            # entities, so they deliberately leave the single-equipment
            # context untouched rather than guessing which one to keep.
            if result.resolved_entities and len(result.resolved_entities) == 1:
                new_instance_key = result.resolved_entities[0].get("instance_key")

                if new_instance_key:
                    st.session_state["ask_equipment_context"] = new_instance_key
                    label = new_instance_key

                    if result.structured_context is not None:
                        label = result.structured_context.get("equipment", {}).get("display_name", new_instance_key)

                    st.session_state["ask_equipment_context_label"] = label

            if result.structured_context is not None:
                evidence = {
                    "intent": result.intent,
                    "provider": result.provider,
                    "grounding_status": result.grounding_status,
                    "fallback_used": result.fallback_used,
                    "resolved_entities": result.resolved_entities,
                    "context": result.structured_context,
                }

        st.session_state["ask_history"].append(
            {"role": "assistant", "content": job["answer"], "evidence": evidence}
        )
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
        "result": None,
        "started_at": time.time(),
    }
    st.session_state["ask_job"] = new_job

    thread = threading.Thread(
        target=_run_job,
        args=(engine, question, st.session_state["ask_equipment_context"], new_job),
        daemon=True,
    )
    thread.start()

    st.rerun()
