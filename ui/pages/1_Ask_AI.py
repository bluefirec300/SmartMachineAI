from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ask import AskEngine, AskResult
from ui.ask_ai_ux import (
    SUGGESTED_QUESTIONS,
    fallback_reason_prefix,
    provider_status_label,
    stage_message,
)

logger = logging.getLogger(__name__)

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
#    "result": AskResult | None, "started_at": float, "stage": str | None}
# A plain dict living inside session_state (not new session_state keys)
# so a background thread can safely update it without touching Streamlit's
# script-run machinery directly. "stage" is Phase 18's small, optional
# progress signal - see ui.ask_ai_ux.stage_message() for how it's
# rendered and app/ask.py's ask_structured(on_progress=...) for where
# it comes from.
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
    def _on_progress(stage: str) -> None:
        job["stage"] = stage

    try:
        result = engine.ask_structured(question, context_equipment=context_equipment, on_progress=_on_progress)
        job["result"] = result
        job["answer"] = result.answer
    except Exception:
        # Phase 18 - never leak a raw exception/traceback into the
        # chat. The real exception (with traceback) is preserved in
        # server-side logging for engineering diagnosis via
        # logger.exception(); the engineer only ever sees a safe,
        # generic message.
        logger.exception("Ask AI request failed for question: %r", question)
        job["result"] = None
        job["answer"] = "Ask AI could not complete this request."
    job["status"] = "done"


def _display_text(result: AskResult | None, raw_answer: str) -> str:
    """
    Phase 18 - prefixes a short, honest reason line onto the answer
    when it's a deterministic fallback (provider unavailable/failed,
    or the AI's wording was rejected by grounding). The rejected AI
    text itself is never available here to begin with - AskResult.answer
    already IS the deterministic fallback in both cases (see
    app/ask.py's _render_interpretation_answer()), so there is no
    "unsafe answer flashing before replacement" risk; this only adds
    a one-line reason in front of what was already going to be shown.
    """
    if result is None:
        return raw_answer

    prefix = fallback_reason_prefix(result.fallback_used, result.grounding_status)

    if prefix is None:
        return raw_answer

    return f"{prefix}\n\n{raw_answer}"


def _submit(question: str) -> None:
    """
    Phase 18 - the single entry point for starting a new Ask AI
    request, used identically by the chat input AND every suggested-
    question button (item 11: no separate execution path). Anything
    that reaches here goes through the exact same
    AskEngine.ask_structured() call, with the same session-scoped
    equipment context, the same deterministic routing/grounding, and
    the same fallback behavior as before.
    """
    st.session_state["ask_history"].append({"role": "user", "content": question})
    new_job = {
        "question": question,
        "status": "running",
        "answer": None,
        "result": None,
        "started_at": time.time(),
        "stage": None,
    }
    st.session_state["ask_job"] = new_job

    thread = threading.Thread(
        target=_run_job,
        args=(engine, question, st.session_state["ask_equipment_context"], new_job),
        daemon=True,
    )
    thread.start()

    st.rerun()


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

# Phase 18 item 10 - a small, verified set of example questions (see
# ui.ask_ai_ux.SUGGESTED_QUESTIONS' own docstring for how each one was
# checked against the live engine before being included here). Only
# shown when there's no conversation yet and nothing running, so it
# never crowds an active chat.
if not st.session_state["ask_history"] and not job_running:
    st.caption("Try asking:")
    suggestion_cols = st.columns(len(SUGGESTED_QUESTIONS))

    for column, suggestion in zip(suggestion_cols, SUGGESTED_QUESTIONS):
        with column:
            if st.button(suggestion, key=f"suggested_{suggestion}", width="stretch"):
                _submit(suggestion)

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

                if evidence.get("resolved_entities"):
                    names = ", ".join(
                        e.get("instance_key", "?") for e in evidence["resolved_entities"] if isinstance(e, dict)
                    )
                    if names:
                        st.caption(f"Resolved equipment: {names}")

                context = evidence.get("context") or {}
                limitations = context.get("data_limitations")
                if limitations:
                    st.caption("Evidence limitations: " + "; ".join(limitations))

                built_at = (context.get("provenance") or {}).get("context_built_at")
                if built_at:
                    st.caption(f"Evidence built at: {built_at}")

                source_modules = (context.get("provenance") or {}).get("source_modules") or []
                if any("history" in module for module in source_modules):
                    st.caption("Includes historical context.")

                with st.expander("Technical detail (raw context)"):
                    st.json(context, expanded=False)

job = st.session_state["ask_job"]

if job is not None:
    with st.chat_message("assistant"):
        if job["status"] == "running":
            elapsed = int(time.time() - job["started_at"])
            minutes, seconds = divmod(elapsed, 60)
            elapsed_text = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
            provider_label = provider_status_label(engine.ai_provider.provider if engine.ai_provider else None)
            st.info(
                f'{stage_message(job.get("stage"), provider_label)} '
                f'(Elapsed: {elapsed_text}) - question: "{job["question"]}" - '
                "feel free to check other pages, this keeps running in the background."
            )
        else:
            st.write(_display_text(job.get("result"), job["answer"]))

    if job["status"] == "running":
        time.sleep(3)
        st.rerun()
    else:
        result: AskResult | None = job.get("result")
        evidence = None
        display_text = _display_text(result, job["answer"])

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
            {"role": "assistant", "content": display_text, "evidence": evidence}
        )
        st.session_state["ask_job"] = None
        st.rerun()

question = st.chat_input(
    'Ask about the factory, e.g. "why is the compressor pressure dropping"',
    disabled=job_running,
)

if question:
    _submit(question)
