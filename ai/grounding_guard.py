from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

"""
Phase 15 - lightweight, deterministic post-generation grounding check
(the approved correction). Purpose: catch the LLM stating an
AUTHORITATIVE numeric/state claim that contradicts the Structured AI
Context it was given - never an attempt to prove/disprove a natural-
language hypothesis (explicitly out of scope, per the correction).

Deliberately narrow and pattern-based, not a general NLI/fact-checking
model: it only recognizes explicit "<thing> is <value>" claim phrasings
for a fixed, small set of authoritative fields (Health Score/State,
Maintenance Priority, Asset Performance state, Asset Attention Score,
Savings Verification result). A claim phrased indirectly ("this looks
concerning") is not checked - false negatives are accepted in exchange
for a low false-positive rate, since the caller's response to a
violation is to DISCARD the AI's phrasing entirely (prefer the
deterministic fallback), so a false positive has a real cost.

Known limitation (documented, not fixed here): COMPARISON and
FACTORY_SUMMARY answers reference two-or-more equipment in one block of
text, so a bare "<thing> is <value>" match cannot be safely attributed
to one side - grounding checking is skipped for those two intents.
"""

_ENUM_VOCAB: dict[str, set[str]] = {
    "health_band": {"HEALTHY", "MONITOR", "ATTENTION", "INVESTIGATE"},
    "maintenance_priority": {"NOT_ASSESSED", "ROUTINE", "REVIEW", "PRIORITY", "URGENT_REVIEW"},
    "performance_state": {
        "IMPROVING", "STABLE", "DEGRADING", "SIGNIFICANTLY_DEGRADING",
        "INSUFFICIENT_EVIDENCE", "NOT_CLASSIFIED",
    },
    "verification_result": {"VERIFIED", "REJECTED", "INCONCLUSIVE"},
    # Phase 9's own SEVERITY_LEVELS - a real, disclosed coverage gap
    # found during live commissioning (2026-08-17): the approved plan
    # explicitly required "anomaly severity/state" to be validated at
    # minimum, but it was missing from the first implementation.
    "anomaly_severity": {"INFORMATION", "ATTENTION", "WARNING", "HIGH"},
    # Phase 17.2b - engine.health_history's own DIRECTION_* constants.
    # A single verdict per equipment (like health_band), never a set.
    "health_trend_direction": {"IMPROVING", "STABLE", "DETERIORATING", "INSUFFICIENT_HISTORY"},
}

# Set-membership families: the claimed value is checked against the SET
# of values actually present (there can legitimately be several open
# anomalies/dimensions with different states/severities at once, unlike
# health_band/maintenance_priority/verification_result which are each
# a single verdict for the whole equipment).
_SET_MEMBERSHIP_FAMILIES = {"performance_state", "anomaly_severity"}

_ENUM_CLAIM_PATTERNS: dict[str, re.Pattern[str]] = {
    "health_band": re.compile(r"health\s+(?:state|band|status)\s+is\s+([A-Za-z_]+)", re.IGNORECASE),
    "maintenance_priority": re.compile(r"maintenance\s+priority\s+is\s+([A-Za-z_]+)", re.IGNORECASE),
    "performance_state": re.compile(r"performance\s+(?:state\s+)?is\s+([A-Za-z_]+)", re.IGNORECASE),
    "verification_result": re.compile(r"verif(?:ied|ication)\s+(?:result\s+)?(?:is|was)\s+([A-Za-z_]+)", re.IGNORECASE),
    # Broader than the other enum patterns on purpose - a REAL Ollama
    # completion observed during commissioning phrased this as "with
    # severity ATTENTION" (no "is"/"of" connector), which the original
    # "severity is/of X" pattern (matching the other enum families)
    # would have missed entirely.
    "anomaly_severity": re.compile(r"severity\s+(?:is|of|was)?\s*([A-Za-z_]+)", re.IGNORECASE),
    "health_trend_direction": re.compile(r"(?:health\s+)?(?:trend|direction)\s+(?:is|was)\s+([A-Za-z_]+)", re.IGNORECASE),
}

_NUMERIC_CLAIM_PATTERNS: dict[str, re.Pattern[str]] = {
    "health_score": re.compile(r"health\s+score\s+(?:is|of)\s+(-?\d+\.?\d*)", re.IGNORECASE),
    "attention_score": re.compile(r"attention\s+score\s+(?:is|of)\s+(-?\d+\.?\d*)", re.IGNORECASE),
    "priority_score": re.compile(r"priority\s+score\s+(?:is|of)\s+(-?\d+\.?\d*)", re.IGNORECASE),
}

# ---------------------------------------------------------------------------
# Phase 16.3 - Data Health / AI grounding adversarial checks. Data Health
# (telemetry trustworthiness) and Equipment Condition (what the telemetry
# indicates) are independent dimensions - the model must never conflate
# them. These are deliberately pattern-based like the checks above, not a
# general NLI model, and are gated on the actual Data Health context so
# they never fire on unrelated text.
# ---------------------------------------------------------------------------

_DATA_CONFIDENCE_HEALTHY_PATTERN = re.compile(
    r"(?:healthy|is\s+fine|is\s+ok(?:ay)?|is\s+normal|no\s+issues).{0,60}"
    r"data\s+confidence\s+(?:is|was)\s+good"
    r"|data\s+confidence\s+(?:is|was)\s+good.{0,60}"
    r"(?:healthy|is\s+fine|is\s+ok(?:ay)?|is\s+normal|no\s+issues)",
    re.IGNORECASE | re.DOTALL,
)

_DATA_CONFIDENCE_FAULTY_PATTERN = re.compile(
    r"(?:faulty|failing|failed|broken|malfunctioning).{0,60}"
    r"data\s+confidence\s+(?:is|was)\s+poor"
    r"|data\s+confidence\s+(?:is|was)\s+poor.{0,60}"
    r"(?:faulty|failing|failed|broken|malfunctioning)",
    re.IGNORECASE | re.DOTALL,
)

_FROZEN_PROMOTED_TO_FAILURE_PATTERN = re.compile(
    r"sensor\s+(?:has\s+)?failed|sensor\s+failure|frozen\s+sensor|"
    r"broken\s+(?:transmitter|sensor)|instrumentation\s+fault|faulty\s+sensor",
    re.IGNORECASE,
)

_CONNECTIVITY_CLAIM_PATTERN = re.compile(
    r"(?:PLC|OPC\s*UA|driver|connection|communication)\s+(?:is\s+)?(?:online|connected|healthy)",
    re.IGNORECASE,
)

_CONFIDENT_CONDITION_CLAIM_PATTERN = re.compile(
    r"\bis\s+(?:operating|running)\s+normally\b"
    r"|\bis\s+(?:currently\s+)?healthy\b"
    r"|\bis\s+(?:currently\s+)?in\s+(?:good|normal)\s+condition\b"
    r"|\bno\s+(?:current\s+)?issues?\s+(?:are\s+)?(?:detected|found|present)\b"
    r"|\beverything\s+(?:is\s+)?(?:looks?\s+)?(?:fine|normal|ok(?:ay)?)\b",
    re.IGNORECASE,
)


def _data_health_violations(answer_text: str, context: dict[str, Any]) -> list[str]:
    """
    Phase 16.3 - deterministic guardrails preventing the model from
    conflating Data Health (telemetry trustworthiness) with Equipment
    Condition, or promoting advisory-only Data Health findings into
    confirmed hardware/connectivity claims. Each check is gated on the
    actual Data Health context so it can never fire on an unrelated
    question or equipment with no Data Health context attached
    (COMPARISON/FACTORY_SUMMARY are each routed to their own dedicated
    handling in check_grounding() below, before this function is ever
    called - so this only ever runs for single-equipment contexts).
    """
    violations: list[str] = []
    data_health = context.get("data_health") or {}

    if not data_health.get("available"):
        return violations

    status = data_health.get("confidence_status")
    requires_confidence = context.get("request", {}).get("requires_telemetry_confidence", False)

    if status == "GOOD" and _DATA_CONFIDENCE_HEALTHY_PATTERN.search(answer_text):
        violations.append(
            "answer treats GOOD Data Health as proof the equipment itself is healthy - "
            "Data Health only describes telemetry trustworthiness, not equipment condition"
        )

    if status == "POOR" and _DATA_CONFIDENCE_FAULTY_PATTERN.search(answer_text):
        violations.append(
            "answer treats POOR Data Health as proof the equipment itself is faulty - "
            "Data Health only describes telemetry trustworthiness, not equipment condition"
        )

    if data_health.get("frozen_candidates") and _FROZEN_PROMOTED_TO_FAILURE_PATTERN.search(answer_text):
        violations.append(
            "answer promotes an advisory 'frozen candidate' tag into a confirmed sensor "
            "failure/broken transmitter/instrumentation fault"
        )

    if data_health.get("source_driver") and _CONNECTIVITY_CLAIM_PATTERN.search(answer_text):
        violations.append(
            "answer claims a live PLC/OPC UA/driver connection is online/connected/healthy "
            "from a configured data source alone - configured source is not connection-health evidence"
        )

    if (
        requires_confidence
        and status in ("POOR", "UNAVAILABLE")
        and _CONFIDENT_CONDITION_CLAIM_PATTERN.search(answer_text)
    ):
        violations.append(
            f"answer states a confident current-condition conclusion despite {status} Data "
            "Confidence on a question that depends on current telemetry"
        )

    return violations


# ---------------------------------------------------------------------------
# Phase 17.2b - historical evidence grounding (Phase 17.2a's
# data_health_history/health_history/asset_performance_history domains).
# Same discipline as the Data Health checks above: deterministic,
# pattern-based, gated on the real historical context so nothing fires
# on unrelated text or an equipment with no historical domains
# requested (history_domains defaults to none - see
# ai/context_builder.py - so these checks are inert for every existing
# current-state-only question, exactly like _data_health_violations()
# is inert when data_health itself is unavailable).
# ---------------------------------------------------------------------------

_DATA_HEALTH_HISTORY_STATUS_PERCENT_PATTERN = re.compile(
    r"\b(GOOD|DEGRADED|POOR|UNAVAILABLE)\b\s+(?:for|was|is)\s+(?:approximately\s+|about\s+)?(\d+\.?\d*)\s*%",
    re.IGNORECASE,
)

_NO_HISTORY_PERCENT_PATTERN = re.compile(
    r"no[\s-](?:recorded\s+)?history\s+(?:for|was|is)?\s*(?:approximately\s+|about\s+)?(\d+\.?\d*)\s*%",
    re.IGNORECASE,
)

_HEALTH_SCORE_CHANGE_PATTERN = re.compile(
    r"(?:health\s+)?score\s+(?:moved|decreased|increased|changed|went)\s+from\s+(-?\d+\.?\d*)\s+to\s+(-?\d+\.?\d*)",
    re.IGNORECASE,
)

# Deliberately narrow ("change of/is/was N%", not any bare "N%") to keep
# the false-positive rate low - only checked when asset_performance_history
# has real observations to compare against (see the gate below).
_ASSET_PERFORMANCE_PERCENT_CHANGE_PATTERN = re.compile(
    r"\bchange\s+(?:of|is|was)\s+(-?\d+\.?\d*)\s*%", re.IGNORECASE,
)

_MAINTENANCE_CAUSAL_CLAIM_PATTERN = re.compile(
    r"maintenance\s+(?:caused|led\s+to|resulted\s+in|is\s+responsible\s+for)"
    r"|caused\s+by\s+(?:the\s+)?maintenance"
    r"|(?:due\s+to|because\s+of|as\s+a\s+result\s+of)\s+(?:the\s+)?maintenance",
    re.IGNORECASE,
)

_FORECASTING_LANGUAGE_PATTERN = re.compile(
    r"\b(?:will|is\s+likely\s+to|expected\s+to|going\s+to)\s+(?:continue\s+)?(?:deteriorat|fail|degrad|worsen|improv)\w*"
    r"|\bprobability\s+of\s+failure\b"
    r"|\bremaining\s+useful\s+life\b"
    r"|\bfailure\s+(?:within|in)\s+\d+"
    r"|\bpredicted\s+to\s+(?:fail|deteriorat)\w*",
    re.IGNORECASE,
)


def _historical_evidence_violations(answer_text: str, context: dict[str, Any]) -> list[str]:
    """
    Deterministic guardrails for the NEW historical evidence claims:
    fabricated/altered percentages and scores, no-history mislabeled as
    a status, unauthorized forecasting/RUL language, and unauthorized
    maintenance causality. `health_trend_direction` (e.g. DETERIORATING
    claimed as IMPROVING) is checked separately, via the existing
    single-value enum mechanism in check_grounding() below - not
    duplicated here.
    """
    violations: list[str] = []

    data_health_history = context.get("data_health_history") or {}
    health_history = context.get("health_history") or {}
    asset_performance_history = context.get("asset_performance_history") or {}

    any_history_available = bool(
        data_health_history.get("available")
        or health_history.get("available")
        or asset_performance_history.get("available")
    )

    if data_health_history.get("available"):
        percentages = data_health_history["status_duration"]["percentages"]
        no_history_percentage = data_health_history["status_duration"].get("no_history_percentage")

        for match in _DATA_HEALTH_HISTORY_STATUS_PERCENT_PATTERN.finditer(answer_text):
            status = match.group(1).upper()
            try:
                claimed_value = float(match.group(2))
            except ValueError:
                continue
            actual_value = percentages.get(status)
            if actual_value is not None and abs(claimed_value - actual_value) > max(0.5, abs(actual_value) * 0.05):
                violations.append(
                    f"claimed historical Data Health {status} percentage of {claimed_value}% does not match "
                    f"the persisted value {actual_value}% (no-recorded-history time must never be counted "
                    "into a status percentage)"
                )

        for match in _NO_HISTORY_PERCENT_PATTERN.finditer(answer_text):
            try:
                claimed_value = float(match.group(1))
            except ValueError:
                continue
            if no_history_percentage is not None and abs(claimed_value - no_history_percentage) > max(0.5, abs(no_history_percentage) * 0.05):
                violations.append(
                    f"claimed no-recorded-history percentage of {claimed_value}% does not match "
                    f"the persisted value {no_history_percentage}%"
                )

    if health_history.get("available"):
        trend = health_history["trend"]

        for match in _HEALTH_SCORE_CHANGE_PATTERN.finditer(answer_text):
            try:
                claimed_previous, claimed_latest = float(match.group(1)), float(match.group(2))
            except ValueError:
                continue

            actual_previous, actual_latest = trend.get("previous_score"), trend.get("latest_score")

            if actual_previous is not None and abs(claimed_previous - actual_previous) > max(0.5, abs(actual_previous) * 0.02):
                violations.append(
                    f"claimed historical previous Health Score of {claimed_previous} does not match the "
                    f"persisted value {actual_previous}"
                )
            if actual_latest is not None and abs(claimed_latest - actual_latest) > max(0.5, abs(actual_latest) * 0.02):
                violations.append(
                    f"claimed historical latest Health Score of {claimed_latest} does not match the "
                    f"persisted value {actual_latest}"
                )

    if asset_performance_history.get("available") and asset_performance_history.get("recent_observations"):
        actual_percent_changes = {
            round(o["percent_change"], 2)
            for o in asset_performance_history["recent_observations"]
            if o.get("percent_change") is not None
        }

        for match in _ASSET_PERFORMANCE_PERCENT_CHANGE_PATTERN.finditer(answer_text):
            try:
                claimed_value = round(float(match.group(1)), 2)
            except ValueError:
                continue
            if actual_percent_changes and not any(
                abs(claimed_value - actual) <= max(0.5, abs(actual) * 0.05) for actual in actual_percent_changes
            ):
                violations.append(
                    f"claimed historical Asset Performance percent change of {claimed_value}% does not match "
                    f"any recorded observation ({sorted(actual_percent_changes)})"
                )

    if asset_performance_history.get("maintenance_comparisons") and _MAINTENANCE_CAUSAL_CLAIM_PATTERN.search(answer_text):
        violations.append(
            "answer states the recorded maintenance CAUSED a change - maintenance comparisons show "
            "before/after association only, never proven causation"
        )

    if any_history_available and _FORECASTING_LANGUAGE_PATTERN.search(answer_text):
        violations.append(
            "answer uses forecasting/prediction language (e.g. 'will', 'likely to', 'expected to', "
            "Remaining Useful Life) - historical evidence is backward-looking only and never authorizes a prediction"
        )

    return violations


# ---------------------------------------------------------------------------
# Phase 17.2c - deterministic COMPARISON grounding
# (ai.context_builder._build_comparison_facts()). Unlike the single-
# entity checks above, comparison claims ARE checkable here because the
# renderer explicitly labels every deterministic fact "Entity A"/
# "Entity B" (ai/interpretation_prompt_builder.py's
# _render_comparison_facts()) - the original Phase 15 concern ("a bare
# claim cannot be safely attributed to one side") no longer applies to
# THESE specific, explicitly-labeled claim families. Every check here
# requires an explicit "Entity A"/"Entity B" label in the matched text
# - an unlabeled claim is a false negative, never guessed.
#
# Phase 17.2e - this fragment used to be `\b(?:entity\s+)?([AB])\b`
# (the "Entity " prefix was OPTIONAL). Under re.IGNORECASE that let the
# bare, ordinary English article "a" satisfy `\b([AB])\b` on its own
# with no "Entity" anywhere nearby - a live, reproduced false rejection
# (Phase 17.2D Scenario 4: "...has a Data Health score of 91.0" was
# misread as "Entity A ... Health Score ... 91.0", which then correctly
# -but-wrongly conflicted with Entity A's real Health Score of 66.2). A
# prior version of this comment described supporting a "bare A/B"
# label too (no "Entity" word) - that path is removed here: under
# IGNORECASE a bare letter can never distinguish a deliberate capital-
# letter label from an ordinary lowercase article, or from a
# capitalized word that only happens to start a sentence ("A lower
# score was observed..."). Every real, observed model answer in this
# project's live validation already writes "Entity A"/"Entity B"
# explicitly (the prompt renders and instructs exactly that form), so
# requiring the literal word "Entity" is the smallest fix that
# eliminates the whole ambiguity class rather than trading one false-
# positive shape for another.
#
# Phase 17.2e - a second, distinct boundary bug was found adversarially
# while fixing the first: the lazy "any characters" gap between the
# entity letter and the tracked phrase (`.{0,40}?`/`.{0,30}?`) could
# skip PAST an entire second "Entity A"/"Entity B" mention to reach a
# phrase that actually belongs to the OTHER entity - e.g. "Entity A
# Health Score is 66.2... Entity B Health Score is 77.6" let the
# pattern anchor on "Entity A" but capture 77.6 (Entity B's real
# value), producing a false mismatch against Entity A's real score.
# `_entity_safe_gap()` below is the same lazy gap, but refuses to match
# through a second Entity mention, so a value can never be attributed
# across entities this way.
# ---------------------------------------------------------------------------

_ENTITY_LETTER = r"\bEntity\s+([AB])\b"


def _entity_safe_gap(max_chars: int) -> str:
    return rf"(?:(?!Entity\s+[AB]).){{0,{max_chars}}}?"


_COMPARISON_ENTITY_VALUE_PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    # `(?<!data\s)` guards against a THIRD boundary bug found the same
    # way: "health score" is a plain substring of "Data Health score",
    # so without this guard a sentence like "Entity B Data Health score
    # is 88.0" would ALSO satisfy this pattern and misattribute a Data
    # Health value as a Health Score claim (checked separately,
    # correctly, by data_health_score below). No such guard is needed
    # on the ordering patterns further down - their extra required
    # words ("lower"/"higher"/"poorer") never appear inside the phrase
    # "data health", so they cannot collide with it the same way.
    "health_score": (
        re.compile(_ENTITY_LETTER + _entity_safe_gap(40) + r"(?<!data\s)health\s+score\s+(?:is|of|was)\s+(-?\d+\.?\d*)", re.IGNORECASE),
        "health",
    ),
    "data_health_score": (
        re.compile(
            _ENTITY_LETTER + _entity_safe_gap(40) + r"data\s+(?:confidence|health)\s+(?:score\s+)?(?:is|of|was)\s+(-?\d+\.?\d*)",
            re.IGNORECASE,
        ),
        "data_health",
    ),
}

# family -> (pattern, (comparison_facts domain key, "which entity" fact key))
_COMPARISON_ORDERING_PATTERNS: dict[str, tuple[re.Pattern[str], tuple[str, str]]] = {
    "lower health score": (
        re.compile(_ENTITY_LETTER + _entity_safe_gap(30) + r"(?:has\s+the\s+)?lower\s+(?:current\s+)?health\s+score", re.IGNORECASE),
        ("health", "lower_score_entity"),
    ),
    "higher maintenance priority": (
        re.compile(_ENTITY_LETTER + _entity_safe_gap(30) + r"(?:has\s+the\s+)?higher\s+(?:maintenance\s+)?priority", re.IGNORECASE),
        ("maintenance_intelligence", "higher_priority_entity"),
    ),
    "poorer data health": (
        re.compile(_ENTITY_LETTER + _entity_safe_gap(30) + r"(?:has\s+)?poorer\s+(?:data\s+health|telemetry|data\s+confidence)", re.IGNORECASE),
        ("data_health", "poorer_data_health_entity"),
    ),
    "higher observed excess cost": (
        re.compile(_ENTITY_LETTER + _entity_safe_gap(30) + r"(?:has\s+the\s+)?higher\s+observed\s+excess\s+cost", re.IGNORECASE),
        ("energy_opportunity", "higher_observed_excess_cost_entity"),
    ),
    "larger historical Health decline": (
        re.compile(_ENTITY_LETTER + _entity_safe_gap(30) + r"(?:has\s+)?(?:the\s+)?larger\s+(?:health\s+)?decline", re.IGNORECASE),
        ("health_history", "larger_decline_entity"),
    ),
}

_COMPARISON_AGGREGATE_VERDICT_PATTERN = re.compile(
    r"\boverall\b.{0,40}?\b(?:winner|better|worse|condition\s+index|risk\s+score)\b"
    r"|\b(?:better|worse)\s+equipment\s+overall\b"
    r"|\bcombined\s+(?:score|comparison\s+score)\b"
    r"|\bequipment\s+risk\s+score\b"
    r"|\bAI\s+winner\b",
    re.IGNORECASE,
)


def _comparison_violations(answer_text: str, comparison_facts: dict[str, Any]) -> list[str]:
    """
    Deterministic guardrails for the new comparison facts (item 9): a
    claimed A/B numeric value or ordering that contradicts (or has no
    support in, because the dimension is NOT_COMPARABLE) the
    deterministic comparison_facts dict, plus a blanket check against
    any invented overall/combined/aggregate verdict.
    """
    violations: list[str] = []

    for field, (pattern, domain) in _COMPARISON_ENTITY_VALUE_PATTERNS.items():
        domain_facts = comparison_facts.get(domain) or {}

        for match in pattern.finditer(answer_text):
            letter = match.group(1).upper()
            try:
                claimed_value = float(match.group(2))
            except ValueError:
                continue

            if not domain_facts.get("comparable"):
                violations.append(
                    f"claimed {field.replace('_', ' ')} for Entity {letter} but this dimension is "
                    "NOT_COMPARABLE between the two equipment"
                )
                continue

            side_key = "entity_a" if letter == "A" else "entity_b"
            actual_value = domain_facts.get(side_key, {}).get("score")

            if actual_value is not None and abs(claimed_value - actual_value) > max(0.5, abs(actual_value) * 0.02):
                violations.append(
                    f"claimed {field.replace('_', ' ')} of {claimed_value} for Entity {letter} does not match "
                    f"the persisted value {actual_value}"
                )

    for family, (pattern, (domain, fact_key)) in _COMPARISON_ORDERING_PATTERNS.items():
        domain_facts = comparison_facts.get(domain)

        if domain_facts is None:
            continue  # domain not present in this comparison at all (e.g. history wasn't requested)

        match = pattern.search(answer_text)

        if not match:
            continue

        claimed_letter = match.group(1).upper()

        if not domain_facts.get("comparable"):
            violations.append(
                f"claimed {family} is Entity {claimed_letter}, but this dimension is NOT_COMPARABLE "
                "between the two equipment"
            )
            continue

        actual_letter = domain_facts.get(fact_key)

        if actual_letter is not None and claimed_letter != actual_letter:
            violations.append(
                f"claimed {family} is Entity {claimed_letter}, but the deterministic comparison fact says "
                f"Entity {actual_letter}"
            )

    if _COMPARISON_AGGREGATE_VERDICT_PATTERN.search(answer_text):
        violations.append(
            "answer states an invented overall/combined/aggregate verdict (e.g. 'overall winner', "
            "'equipment risk score') - comparison domains remain separate, never blended into one verdict"
        )

    return violations


@dataclass
class GroundingResult:
    grounded: bool
    violations: list[str] = field(default_factory=list)
    checked: bool = True


def _actual_enum_values(context: dict[str, Any]) -> dict[str, str]:
    actual: dict[str, str] = {}

    health = context.get("health", {})
    if health.get("available"):
        actual["health_band"] = health["health_band"]

    maintenance = context.get("maintenance_intelligence", {})
    if maintenance.get("available"):
        actual["maintenance_priority"] = maintenance["maintenance_priority"]

    savings = context.get("savings_verification", {})
    if savings.get("available") and savings.get("latest_result"):
        actual["verification_result"] = savings["latest_result"]["result"]

    health_history = context.get("health_history", {})
    if health_history.get("available"):
        actual["health_trend_direction"] = health_history["trend"]["direction"]

    return actual


def _actual_numeric_values(context: dict[str, Any]) -> dict[str, float | None]:
    health = context.get("health", {})
    performance = context.get("asset_performance", {})
    maintenance = context.get("maintenance_intelligence", {})

    return {
        "health_score": health.get("health_score") if health.get("available") else None,
        "attention_score": performance.get("attention_score") if performance.get("available") else None,
        "priority_score": maintenance.get("priority_score") if maintenance.get("available") else None,
    }


def check_grounding(answer_text: str, context: dict[str, Any], intent: str) -> GroundingResult:
    """
    Returns GroundingResult.grounded=False (with human-readable
    violations) when the answer text makes an authoritative enum/numeric
    claim that contradicts (or has no support in) the supplied context.

    checked=False for FACTORY_SUMMARY (unchanged Phase 15 limitation -
    a ranked list of many equipment can't be safely claim-attributed).

    COMPARISON (Phase 17.2c): checked=True ONLY when the context
    actually carries comparison_facts (ai.context_builder._build_comparison_facts())
    - the ordered, explicitly-labeled "Entity A"/"Entity B" deterministic
    facts that make comparison claims safely attributable for the first
    time. A COMPARISON context with no comparison_facts (e.g. an old-
    style/synthetic test context) remains checked=False, exactly as
    Phase 15 originally established - this is never weakened, only
    additively extended for real comparison contexts, which always
    carry comparison_facts once built via build_comparison_ai_context().
    """
    if intent == "FACTORY_SUMMARY":
        return GroundingResult(grounded=True, checked=False)

    if intent == "COMPARISON":
        comparison_facts = context.get("comparison_facts")

        if not comparison_facts:
            return GroundingResult(grounded=True, checked=False)

        violations = _comparison_violations(answer_text, comparison_facts)
        return GroundingResult(grounded=not violations, violations=violations, checked=True)

    violations: list[str] = []

    actual_enums = _actual_enum_values(context)
    performance = context.get("asset_performance", {})
    dimension_states = (
        {d["performance_state"] for d in performance["dimensions"]}
        if performance.get("available")
        else set()
    )

    # Phase 17.2b - a claimed performance_state is checked against the
    # UNION of current-state dimensions AND historical observations
    # (the model may legitimately be describing either), reusing this
    # existing set-membership mechanism rather than adding a second one
    # for asset_performance_history specifically.
    asset_performance_history = context.get("asset_performance_history", {})
    performance_state_available = performance.get("available", False)
    if asset_performance_history.get("available"):
        dimension_states |= {
            o["performance_state"] for o in asset_performance_history.get("recent_observations", [])
        }
        performance_state_available = performance_state_available or bool(asset_performance_history.get("recent_observations"))

    anomalies = context.get("anomalies", {})
    open_severities = (
        {a["severity"] for a in anomalies["open_anomalies"]}
        if anomalies.get("available")
        else set()
    )

    _set_membership_values = {
        "performance_state": dimension_states,
        "anomaly_severity": open_severities,
    }
    _set_membership_available = {
        "performance_state": performance_state_available,
        "anomaly_severity": anomalies.get("available", False),
    }

    for family, pattern in _ENUM_CLAIM_PATTERNS.items():
        match = pattern.search(answer_text)

        if not match:
            continue

        claimed = match.group(1).upper()

        if claimed not in _ENUM_VOCAB[family]:
            # Not one of the known vocabulary values for this family -
            # e.g. "health state is fine" - not a checkable authoritative
            # claim, skip rather than guess.
            continue

        if family in _SET_MEMBERSHIP_FAMILIES:
            # There can legitimately be several open anomalies/dimensions
            # with different states/severities at once - the claim is
            # checked against the SET actually present, not a single
            # "the" value.
            if _set_membership_available[family] and claimed not in _set_membership_values[family]:
                violations.append(
                    f"claimed {family} '{claimed}' does not match any observed "
                    f"value ({sorted(_set_membership_values[family]) or 'none available'})"
                )
            continue

        actual_value = actual_enums.get(family)

        if actual_value is not None and claimed != actual_value:
            violations.append(
                f"claimed {family} '{claimed}' does not match the persisted value '{actual_value}'"
            )

    actual_numbers = _actual_numeric_values(context)

    for key, pattern in _NUMERIC_CLAIM_PATTERNS.items():
        match = pattern.search(answer_text)

        if not match:
            continue

        try:
            claimed_value = float(match.group(1))
        except ValueError:
            continue

        actual_value = actual_numbers.get(key)

        if actual_value is None:
            violations.append(
                f"claimed {key} of {claimed_value} but no authoritative value is available "
                "for this equipment (context marks it unavailable/insufficient evidence)"
            )
        elif abs(claimed_value - actual_value) > max(0.5, abs(actual_value) * 0.02):
            violations.append(
                f"claimed {key} of {claimed_value} does not match the persisted value {actual_value}"
            )

    violations.extend(_data_health_violations(answer_text, context))
    violations.extend(_historical_evidence_violations(answer_text, context))

    return GroundingResult(grounded=not violations, violations=violations)
