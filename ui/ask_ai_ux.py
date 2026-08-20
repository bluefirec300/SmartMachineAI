from __future__ import annotations

"""
Phase 18 - AI User Experience. Small, UI-agnostic helpers for
ui/pages/1_Ask_AI.py's status/progress rendering. Deliberately has NO
Streamlit import (and no dependency on it) so these are directly unit
testable and never leak into app/ask.py's deterministic layers - the
page owns all rendering, this module only owns wording.
"""

# Ordered so a caller can render "genuine stages only" - a stage is
# skipped by the UI whenever it never actually happens for a given
# question (e.g. "validating" is real only for the Phase 15 domain
# path, which is the only place ai.grounding_guard.check_grounding()
# is ever called - see app/ask.py's _render_interpretation_answer()).
STAGE_PREPARING = "preparing_context"
STAGE_GENERATING = "generating_ai"
STAGE_VALIDATING = "validating"


def provider_status_label(provider: str | None) -> str:
    """Maps an AskResult.provider value to the short, factual phrase
    used in the long-running status line. Never exposes a URL, API
    key, or other provider connection detail - name only."""
    if provider == "ollama":
        return "local AI"
    if provider == "openai":
        return "OpenAI"
    if provider:
        return provider
    return "AI"


def stage_message(stage: str | None, provider_label: str) -> str:
    """
    The single honest status line for a still-running request.
    `stage` is whatever the backend last reported via on_progress (or
    None before the very first callback fires). Deliberately no
    percentage, no ETA - only what's actually known.
    """
    if stage == STAGE_GENERATING:
        return f"Generating explanation with {provider_label}..."
    if stage == STAGE_VALIDATING:
        return "Validating the explanation against the data..."
    return "Working on your question..."


def fallback_reason_prefix(fallback_used: bool, grounding_status: str) -> str | None:
    """
    Returns a short, user-facing reason line to prepend to a fallback
    answer, or None when no prefix is warranted (a normal grounded
    answer, or a deterministic-only answer that was never trying to
    use AI phrasing to begin with - see the "not_applicable + not
    fallback" case, which callers should treat as ordinary and skip).

    Only two distinguishable failure categories exist in AskResult
    today (see app/ask.py's _render_interpretation_answer()): the
    provider was unavailable/errored (grounding_status
    "not_applicable" with fallback_used True), or the AI's wording was
    generated but rejected by grounding (grounding_status
    "violation_detected"). Both already return the deterministic
    fallback text as AskResult.answer - this function only adds the
    short reason line in front of it, never changes what's shown.
    """
    if grounding_status == "violation_detected":
        return "AI wording did not pass validation. Showing the deterministic evidence instead."
    if fallback_used:
        return "AI explanation is unavailable right now. Showing the deterministic evidence instead."
    return None


# Phase 18 item 10/11 - verified individually against the live AskEngine
# (each resolves to a real, single-shot deterministic answer, not a
# clarification menu or misroute) before being exposed here. Every
# equipment reference includes an explicit plant qualifier (P01/P02)
# because both plants are fully enabled - a bare "CHL01" is genuinely
# ambiguous. "Energy opportunity"-themed phrasing was tried repeatedly
# (several wordings, with and without a plant/area qualifier) and never
# resolved cleanly - Ask AI's query engine does not yet reliably answer
# that class of question (Energy Opportunities is a separate page/
# engine, not wired into the NLP resolver) - excluded rather than
# shown untested, per explicit instruction.
SUGGESTED_QUESTIONS: tuple[str, ...] = (
    "How is AC01 doing?",
    "Why has P01 CHL01 power increased?",
    "Compare P01 CHL01 and P02 CHL01",
    "Show me the P01 CHL01 power trend",
    "How is everything doing?",
)
