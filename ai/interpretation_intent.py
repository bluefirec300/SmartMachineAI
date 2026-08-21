from __future__ import annotations

"""
Phase 15 - domain-intent classification for the AI Interpretation Layer.

Deliberately the SAME architectural pattern engine/concept_extractor.py
already uses for its own 10 intents (an ordered phrase-check chain, not
ML/embeddings, not a revival of the dead ai/llm_semantic_router.py /
ai/hybrid_router.py prototypes - see the Phase 15 audit). Both of those,
plus the rest of the "v2 deterministic query engine" cluster they were
part of, were removed for real in Phase V2.5's cleanup follow-up after
a fresh dependency trace confirmed zero live importers.

INTENT ROUTING PRECEDENCE (the approved correction) - this module answers
ONLY "is this a Phase 15 domain question, and if so which one", by phrase
matching alone. It is deliberately independent of what
engine.industrial_query_engine.IndustrialQueryEngine.query() resolved -
tested live against the required regression phrases, that ordering
matters:

  "Is CHL01 wasting energy?" resolves CONFIDENTLY (0.99) to a real tag
  (Energy_kWh) under the existing tag-level engine - if this classifier
  only ran when the old engine failed to resolve, this question would
  never reach Phase 15 at all, and would be answered as a literal
  "what is CHL01's current energy reading" question instead of the
  Energy Opportunity question it actually is.

  "Why is WSP01 unhealthy?" / "Why is CHL01 performance getting worse?"
  both classify as root_cause under the OLD engine (they start with
  "why is") - if Phase 15 classification only fired for
  current_data/equipment_status, these would be silently swallowed by
  the existing root_cause pipeline and never reach Phase 15 either.

So: phrase-check FIRST (this module), independent of the old engine's
own intent/status. The caller (app/ask.py) applies the actual A/B/C/D
precedence:
  A. No domain phrase matched -> use the existing tag-level pipeline
     UNCHANGED (whatever engine.industrial_query_engine already does,
     including its own clarification/menu behavior).
  B. A domain phrase matched -> route through the Structured AI
     Interpretation path (this module's returned intent).
  C. No domain phrase matched AND the old engine's own intent is
     "root_cause" -> the existing root_cause pipeline (unchanged) -
     e.g. "why is the compressor pressure dropping" stays exactly as
     it already worked before Phase 15.
  D. Ambiguous entity within a Phase 15 intent (e.g. "WSP01" naming no
     plant) is NOT decided here - it is handled by
     engine.industrial_query_engine.resolve_equipment()'s own
     clarification_required status, downstream of this classifier.
"""

HEALTH_EXPLANATION = "HEALTH_EXPLANATION"
MAINTENANCE_REVIEW = "MAINTENANCE_REVIEW"
PERFORMANCE_REVIEW = "PERFORMANCE_REVIEW"
ENERGY_REVIEW = "ENERGY_REVIEW"
ANOMALY_EXPLANATION = "ANOMALY_EXPLANATION"
SAVINGS_STATUS = "SAVINGS_STATUS"
COMPARISON = "COMPARISON"
FACTORY_SUMMARY = "FACTORY_SUMMARY"
GENERAL_ENGINEERING_QUERY = "GENERAL_ENGINEERING_QUERY"

# Ordered - first match wins. COMPARISON/FACTORY_SUMMARY are checked
# first since they are structurally distinct question shapes (no single
# equipment, or two); GENERAL_ENGINEERING_QUERY is checked LAST as the
# deliberate catch-all for "what's wrong with X"/"summarize X" style
# questions that don't name a specific domain.
_TRIGGER_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        FACTORY_SUMMARY,
        (
            "what should engineering look at",
            "what needs attention",
            "what should i look at today",
            "what should we look at today",
            "factory summary",
            "factory-wide summary",
            "overall engineering status",
            "engineering priorities today",
        ),
    ),
    (
        COMPARISON,
        (
            "compare ",
            "comparing ",
            "which is better",
            "which one needs more attention",
            " vs ",
            " vs. ",
            " versus ",
        ),
    ),
    (
        SAVINGS_STATUS,
        (
            "did it actually save",
            "did it save energy",
            "did the intervention",
            "verified saving",
            "savings verification",
            "was the saving verified",
            "actually save energy",
            "actually save money",
        ),
    ),
    (
        ENERGY_REVIEW,
        (
            "wasting energy",
            "wasting power",
            "energy opportunity",
            "energy opportunities",
            "energy waste",
            "energy saving opportunity",
            "is it wasting",
        ),
    ),
    (
        MAINTENANCE_REVIEW,
        (
            "should maintenance",
            "maintenance check",
            "maintenance review",
            "needs maintenance",
            "maintenance priority",
            "does maintenance need",
            "what should maintenance",
        ),
    ),
    (
        PERFORMANCE_REVIEW,
        (
            "performance getting worse",
            "performance degrading",
            "asset performance",
            "is performance",
            "performance declining",
            "degrading relative to",
        ),
    ),
    (
        ANOMALY_EXPLANATION,
        (
            "statistical anomaly",
            "why is this flagged",
            "why was this flagged",
            "anomalies",
            "anomaly",
        ),
    ),
    (
        HEALTH_EXPLANATION,
        (
            "unhealthy",
            "is healthy",
            "how healthy",
            "health score",
            "equipment health",
            "in attention",
            "in investigate",
        ),
    ),
    (
        GENERAL_ENGINEERING_QUERY,
        (
            "what is wrong with",
            "what's wrong with",
            "summarize the current",
            "summarize engineering",
            "engineering concerns",
            "give me an overview of",
            # Found missing during live commissioning (2026-08-17): "why
            # does P01 WSP01 need attention?" matched no trigger at all
            # and silently fell through to the OLD tag-level pipeline
            # instead of Phase 15 - "attention" alone isn't domain-
            # specific (it spans Health's ATTENTION band, Maintenance
            # Priority, and Asset Performance's attention_score), so
            # this routes to the cross-domain catch-all rather than
            # narrowly to HEALTH_EXPLANATION.
            "need attention",
            "needs attention",
            "require attention",
            "requires attention",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Phase 16.3 - Data Health / AI grounding integration. Determines whether
# a Phase 15 answer for this intent/question materially depends on
# CURRENT/RECENT equipment telemetry (and therefore needs Data Health
# gating), as opposed to a static/reference question. Deliberately
# co-located here rather than in ai/context_builder.py or ai/grounding_guard.py
# so both of those modules share exactly one definition.
# ---------------------------------------------------------------------------

# Which Phase 15 intents are fundamentally about CURRENT equipment
# condition/telemetry-derived analytics. COMPARISON is deliberately
# excluded here - it resolves TWO equipment, each with its own
# independently-built context (including its own data_health), and is
# handled by its own per-entity logic rather than this single-equipment
# gate. FACTORY_SUMMARY is excluded because it never resolves a single
# equipment's telemetry at all (aggregate rankings only, no plant-wide
# Data Health evaluation - explicitly out of scope for this phase).
TELEMETRY_DEPENDENT_INTENTS = frozenset({
    HEALTH_EXPLANATION, MAINTENANCE_REVIEW, PERFORMANCE_REVIEW, ENERGY_REVIEW,
    ANOMALY_EXPLANATION, SAVINGS_STATUS, GENERAL_ENGINEERING_QUERY,
})

# A narrow, explicit override for genuinely static/reference phrasing -
# the SAME ordered-phrase-check architecture as _TRIGGER_PHRASES above,
# not a general NLU classifier ("do not introduce fragile keyword-only
# classification if a stronger existing mechanism exists" - the STRONGER
# mechanism here is the already-classified intent itself, used as the
# primary signal below; this list is only a narrow exception on top of
# it). A question matching one of these is treated as NOT requiring live
# telemetry confidence gating even if it was routed to a telemetry-
# dependent intent - e.g. a follow-up asking "what is the rated power of
# this pump?" needs configuration data, not current sensor readings, and
# must not be blocked by poor telemetry.
_STATIC_REFERENCE_PHRASES: tuple[str, ...] = (
    "what does the alarm mean", "what does this alarm mean",
    "what is the rated", "what is the configured",
    "when was this equipment installed", "when was it installed", "installation date",
    "what maintenance procedure", "what procedure applies",
    "explain what", "explain the term", "what does",
    "what sensor is configured", "which tag is configured", "what tags are configured",
    "what is the difference between", "how does",
)


def requires_telemetry_confidence(intent: str, question: str) -> bool:
    """
    True when this intent/question needs Phase 16 Data Health gating
    (see ai/context_builder.py's data_health attachment and
    app/ask.py's UNAVAILABLE short-circuit / DEGRADED-POOR prompt
    qualification). Reuses the already-classified intent as the primary
    signal; the static-phrase list above is a narrow, explicit exception,
    never a parallel classifier.
    """
    if intent not in TELEMETRY_DEPENDENT_INTENTS:
        return False

    normalized = f" {question.strip().lower()} "

    if any(phrase in normalized for phrase in _STATIC_REFERENCE_PHRASES):
        return False

    return True


def classify_interpretation_intent(question: str) -> str | None:
    """
    Returns a Phase 15 intent string if `question` matches a known
    domain-question phrase, else None (meaning: this is not a Phase 15
    question - the caller should fall back to the existing tag-level /
    root_cause pipeline, see module docstring's Route A/C).
    """
    normalized = f" {question.strip().lower()} "

    for intent, phrases in _TRIGGER_PHRASES:
        if any(phrase in normalized for phrase in phrases):
            return intent

    return None


# ---------------------------------------------------------------------------
# Phase 17.2d - COMPARISON domain-selectivity hint. A COMPARISON question
# is matched by _TRIGGER_PHRASES's own COMPARISON entry FIRST (ordered
# check, above), so a question like "compare the health of X and Y"
# never reaches HEALTH_EXPLANATION's own phrases in that loop. This
# function answers a NARROWER, second question - given a question
# already classified as COMPARISON, does it ALSO name one specific
# domain? - by reusing the exact same _TRIGGER_PHRASES phrase tuples
# (never a new keyword list, never a new classifier) plus one small,
# explicit Data Health phrase set (Data Health is cross-cutting, not
# one of the 9 intents, so it has no _TRIGGER_PHRASES entry of its own).
# ---------------------------------------------------------------------------

_COMPARISON_DOMAIN_HINTS: dict[str, tuple[str, ...]] = {
    HEALTH_EXPLANATION: ("health",),
    MAINTENANCE_REVIEW: ("maintenance_intelligence",),
    PERFORMANCE_REVIEW: ("asset_performance",),
    ENERGY_REVIEW: ("energy_opportunity",),
    SAVINGS_STATUS: ("savings_verification",),
}

_DATA_HEALTH_HINT_PHRASES: tuple[str, ...] = (
    "data health", "data quality", "data confidence", "telemetry quality", "telemetry trust",
)


def comparison_domain_hint(question: str) -> tuple[str, ...] | None:
    """
    Returns a narrowed comparison_facts domain tuple (e.g. ("health",))
    when `question` clearly names one specific comparison domain, else
    None - meaning "broad comparison, no narrowing signal" (the caller
    must then use its own default full domain set, never guess a
    narrower one from nothing). Used only to decide how much of the
    already-computed comparison_facts gets RENDERED to the LLM
    (ai/interpretation_prompt_builder.py) - never to decide what gets
    fetched/computed, so grounding always retains the full picture
    regardless of this hint.
    """
    normalized = f" {question.strip().lower()} "

    if any(phrase in normalized for phrase in _DATA_HEALTH_HINT_PHRASES):
        return ("data_health",)

    for intent, phrases in _TRIGGER_PHRASES:
        if intent in _COMPARISON_DOMAIN_HINTS and any(phrase in normalized for phrase in phrases):
            return _COMPARISON_DOMAIN_HINTS[intent]

    return None
