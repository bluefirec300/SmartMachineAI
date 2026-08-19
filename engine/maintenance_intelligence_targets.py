from __future__ import annotations

from dataclasses import dataclass

from engine.health_targets import (
    FAMILY_CONDITION,
    FAMILY_ELECTRICAL_LOAD,
    FAMILY_EVENTS,
    FAMILY_MAINTENANCE,
    FAMILY_MAX_PENALTY,
    FAMILY_PERFORMANCE,
    FAMILY_PROCESS,
)

"""
Phase 13 - Maintenance Intelligence registry. Mirrors engine/health_targets.py's
own discipline exactly: centralized numeric policy, mandatory provenance,
and a curated (never automatic) recommended-check registry keyed to
Phase 12.1's OWN factor_id vocabulary - Phase 13 introduces no new
condition/performance/process measurement of its own.

This is a deterministic ENGINEERING PRIORITIZATION TOOL, not predictive
maintenance:
  - No automatic work orders, no automatic maintenance-interval changes,
    no Remaining Useful Life, no failure probability (Phase 13 plan
    items 33-36).
  - Recommended checks use "Inspect"/"Review"/"Verify" wording only -
    never a named root cause.
  - ELECTRICAL_LOAD-family factors are explicitly EXCLUDED from
    Maintenance Priority everywhere in this module - that evidence
    belongs to Energy Opportunities (Phase 10) only, never duplicated
    here (Phase 13 plan item 21).

Addendum corrections (approved) implemented via the constants below:
  - CORRECTION 1: MAINTENANCE_OVERDUE_FLOOR_PRIORITY - a confirmed
    overdue maintenance schedule floors Maintenance Priority at REVIEW,
    regardless of the computed score.
  - CORRECTION 2: PRIORITY_NOT_ASSESSED - a distinct outcome, never
    presented as ROUTINE, for equipment with no contributing evidence
    at all.
  - CORRECTION 3: CONFIDENCE_RANK - confidence is aggregated only over
    dimensions that actually contributed to the conclusion (engine
    module owns the aggregation logic; this module only supplies the
    ranking table).
"""

MAINTENANCE_INTELLIGENCE_MODEL_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Priority states (item 1 of the addendum) - NOT_ASSESSED is a 5th,
# distinct outcome. ROUTINE means "evidence was assessed and does not
# currently warrant elevated attention" - NOT_ASSESSED means "there is
# not enough contributing evidence to make an assessment at all". These
# must never be conflated (Phase 13 plan item 6 / addendum "IMPLEMENTATION
# CHECK").
# ---------------------------------------------------------------------------

PRIORITY_NOT_ASSESSED = "NOT_ASSESSED"
PRIORITY_ROUTINE = "ROUTINE"
PRIORITY_REVIEW = "REVIEW"
PRIORITY_PRIORITY = "PRIORITY"
PRIORITY_URGENT_REVIEW = "URGENT_REVIEW"

PRIORITY_VALUES = (
    PRIORITY_NOT_ASSESSED, PRIORITY_ROUTINE, PRIORITY_REVIEW, PRIORITY_PRIORITY, PRIORITY_URGENT_REVIEW,
)

# Illustrative/SIMULATION_TUNING bands (Phase 13 plan section 9, approved
# unchanged by the addendum) - half-open on the low end exactly like
# engine.health_targets.HEALTH_BANDS, so every float in [0, 100] lands in
# exactly one band. NOT_ASSESSED is never a member of this table - it is
# a separate outcome the engine selects BEFORE this mapping ever runs
# (see engine/maintenance_intelligence_engine.py).
PRIORITY_BANDS: tuple[tuple[float, float, str], ...] = (
    (75.0, 100.0, PRIORITY_URGENT_REVIEW),
    (50.0, 75.0, PRIORITY_PRIORITY),
    (25.0, 50.0, PRIORITY_REVIEW),
    (0.0, 25.0, PRIORITY_ROUTINE),
)

# Default sort order for the Maintenance Intelligence table (addendum
# item 1): most-needs-attention first. NOT_ASSESSED ("we genuinely don't
# know") ranks above a CONFIRMED ROUTINE ("checked, currently fine") but
# below any priority class backed by real contributing evidence.
PRIORITY_SORT_RANK: dict[str, int] = {
    PRIORITY_URGENT_REVIEW: 4,
    PRIORITY_PRIORITY: 3,
    PRIORITY_REVIEW: 2,
    PRIORITY_NOT_ASSESSED: 1,
    PRIORITY_ROUTINE: 0,
}


def priority_band_for_score(score: float) -> str:
    if not (0.0 <= score <= 100.0):
        raise ValueError(f"score {score!r} outside the defined 0-100 band range")
    for low, _high, label in PRIORITY_BANDS:
        if score >= low:
            return label
    raise ValueError(f"score {score!r} outside the defined 0-100 band range")


# ---------------------------------------------------------------------------
# CORRECTION 1 - overdue-maintenance REVIEW floor. Applied AFTER the
# normal score -> band mapping, never folded invisibly into the score
# itself - the raw priority_score is always reported unfloored so the
# breakdown stays honest ("REVIEW (floor applied: maintenance overdue)",
# never a silently rewritten score).
# ---------------------------------------------------------------------------

MAINTENANCE_OVERDUE_FLOOR_PRIORITY = PRIORITY_REVIEW
MAINTENANCE_OVERDUE_FLOOR_REASON = "Scheduled maintenance is overdue."

# ---------------------------------------------------------------------------
# CORRECTION 3 - evidence-aware confidence aggregation. recommendation_
# confidence is the WORST (lowest-ranked) confidence among only the
# dimensions that actually contributed a nonzero amount (or the overdue
# floor) to the conclusion - never an unconditional min() across every
# dimension regardless of whether it contributed (addendum section 4).
# ---------------------------------------------------------------------------

CONFIDENCE_INSUFFICIENT = "INSUFFICIENT"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_HIGH = "HIGH"

CONFIDENCE_RANK: dict[str, int] = {
    CONFIDENCE_INSUFFICIENT: 0,
    CONFIDENCE_LOW: 1,
    CONFIDENCE_MEDIUM: 2,
    CONFIDENCE_HIGH: 3,
}

# Non-statistical dimensions (maintenance-overdue, events, criticality)
# carry an inherently HIGH confidence tier - they are deterministic facts
# (a real overdue date, a real event count, a user-configured value), not
# statistical estimates. This reuses engine.health_engine._coverage_status()'s
# own established convention ("no statistical evidence used at all -
# deterministic-only, unconstrained -> HIGH"), not a new idea.
DETERMINISTIC_DIMENSION_CONFIDENCE = CONFIDENCE_HIGH

# ---------------------------------------------------------------------------
# Priority-score formula weights (Phase 13 plan section 9, approved
# unchanged). Every weight below is SIMULATION_TUNING - a reasonable
# dev-stage engineering judgment call, matching engine.health_targets'
# own provenance vocabulary and disclosure discipline.
# ---------------------------------------------------------------------------

PRIORITY_PROVENANCE = "SIMULATION_TUNING"

HEALTH_BAND_CONTRIBUTION: dict[str, float] = {
    "HEALTHY": 0.0,
    "MONITOR": 10.0,
    "ATTENTION": 25.0,
    "INVESTIGATE": 40.0,
}
HEALTH_BAND_CONTRIBUTION_MAX = 40.0

# Condition/Performance/Process penalties from the equipment's OWN
# already-persisted, already-family-capped Phase 12 health factor
# snapshots, summed and divided by 3 - inherits Phase 12.1's own family
# caps rather than re-deriving a second penalty ceiling.
CONDITION_PERFORMANCE_PROCESS_DIVISOR = 3.0
CONDITION_PERFORMANCE_PROCESS_FAMILIES = (FAMILY_CONDITION, FAMILY_PERFORMANCE, FAMILY_PROCESS)
CONDITION_PERFORMANCE_PROCESS_CONTRIBUTION_MAX = round(
    sum(FAMILY_MAX_PENALTY[f] for f in CONDITION_PERFORMANCE_PROCESS_FAMILIES) / CONDITION_PERFORMANCE_PROCESS_DIVISOR, 2,
)

EVENTS_CONTRIBUTION_MULTIPLIER = 0.5
EVENTS_CONTRIBUTION_MAX = round(FAMILY_MAX_PENALTY[FAMILY_EVENTS] * EVENTS_CONTRIBUTION_MULTIPLIER, 2)

MAINTENANCE_CONTRIBUTION_MULTIPLIER = 1.0
MAINTENANCE_CONTRIBUTION_MAX = round(FAMILY_MAX_PENALTY[FAMILY_MAINTENANCE] * MAINTENANCE_CONTRIBUTION_MULTIPLIER, 2)

# Recent Movement (Phase 12.2's own descriptive, backward-looking trend)
# is USABLE as a priority input but never interpreted as a prediction -
# it only reflects already-observed historical score movement.
RECENT_MOVEMENT_CONTRIBUTION: dict[str, float] = {
    "DETERIORATING": 8.0,
    "STABLE": 0.0,
    "IMPROVING": -4.0,
    "INSUFFICIENT_HISTORY": 0.0,
}
RECENT_MOVEMENT_CONTRIBUTION_MAX = 8.0

# Criticality never fabricated (mirrors engine.health_evidence.equipment_criticality()'s
# own guarantee) - contributes 0 whenever unconfigured. Keyed on the
# exact strings the Factory Configuration page's own dropdown uses
# (ui/pages/13_Factory_Configuration.py) - "" is deliberately absent
# (falls through the .get() default of 0.0, same as None).
CRITICALITY_CONTRIBUTION: dict[str, float] = {
    "Critical": 15.0,
    "High": 10.0,
    "Medium": 5.0,
    "Low": 0.0,
}
CRITICALITY_CONTRIBUTION_MAX = 15.0


# ---------------------------------------------------------------------------
# Recommended-check registry (Phase 13 plan section 18) - curated,
# "Inspect"/"Review"/"Verify" wording only, keyed to Phase 12.1's OWN
# factor_id vocabulary so there is exactly one place a factor's meaning
# is defined. Audited directly against the live
# engine.health_targets.HEALTH_FACTOR_REGISTRY, CONDITION/PERFORMANCE/
# PROCESS entries only (ELECTRICAL_LOAD is excluded everywhere in this
# module - see module docstring). EVENTS/MAINTENANCE checks are generic
# by FAMILY, since their wording doesn't depend on the specific
# equipment type.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecommendedCheck:
    text: str
    provenance: str = PRIORITY_PROVENANCE


RECOMMENDED_CHECK_BY_FACTOR_ID: dict[str, RecommendedCheck] = {
    # Performance
    "chl_cop_performance": RecommendedCheck("Verify chiller performance (coefficient of performance) against design/rated conditions."),
    # Process
    "chl_cooling_output_process": RecommendedCheck("Inspect chiller cooling output against expected process conditions."),
    "chl_supply_temp_process": RecommendedCheck("Verify chilled water supply temperature instrumentation and setpoint tracking."),
    "chl_return_temp_process": RecommendedCheck("Verify chilled water return temperature instrumentation and setpoint tracking."),
    "chl_waterflow_process": RecommendedCheck("Inspect chilled water flow rate and associated valves/strainers."),
    "chwp_flow_process": RecommendedCheck("Inspect pump delivered flow against expected process conditions."),
    "chwp_delta_p_process": RecommendedCheck("Inspect pump differential pressure and associated piping/valves."),
    "wsp_flow_process": RecommendedCheck("Inspect pump delivered flow against expected process conditions."),
    "wsp_delta_p_process": RecommendedCheck("Inspect pump differential pressure and associated piping/valves."),
    "ahu_supply_air_temp_process": RecommendedCheck("Verify AHU supply air temperature control and setpoint tracking."),
    "cr_room_temp_process": RecommendedCheck("Inspect cold room temperature control and door/seal condition."),
    "prod_process_temp_process": RecommendedCheck("Verify process temperature instrumentation and control."),
    # Condition
    "chwp_vibration_condition": RecommendedCheck("Inspect pump for vibration - check alignment, coupling, and bearing condition."),
    "chwp_bearing_temp_condition": RecommendedCheck("Inspect pump bearings and lubrication."),
    "chwp_flow_per_kw_condition": RecommendedCheck("Inspect pump hydraulic condition (flow-per-kW efficiency trend) - check for wear/fouling."),
    "wsp_vibration_condition": RecommendedCheck("Inspect pump for vibration - check alignment, coupling, and bearing condition."),
    "wsp_bearing_temp_condition": RecommendedCheck("Inspect pump bearings and lubrication."),
    "wsp_flow_per_kw_condition": RecommendedCheck("Inspect pump hydraulic condition (flow-per-kW efficiency trend) - check for wear/fouling."),
    "ahu_filter_dp_condition": RecommendedCheck("Inspect/replace AHU filters - differential pressure trend indicates loading."),
    "prod_vibration_condition": RecommendedCheck("Inspect equipment for vibration - check alignment, coupling, and mounting."),
}

GENERIC_CHECK_BY_FAMILY: dict[str, RecommendedCheck] = {
    FAMILY_EVENTS: RecommendedCheck("Review recent alarm/warning event history for this equipment."),
    FAMILY_MAINTENANCE: RecommendedCheck("Review overdue scheduled maintenance."),
}

# Safe fallback for any future factor_id not yet curated above - mirrors
# Phase 12.3A's own FACTOR_LABELS fallback discipline (never crash on an
# unmapped factor, never silently omit its check).
_FALLBACK_CHECK = RecommendedCheck("Review condition evidence for this factor.")


def recommended_check_for_factor(factor_id: str, family: str) -> RecommendedCheck:
    if family == FAMILY_ELECTRICAL_LOAD:
        raise ValueError(
            "ELECTRICAL_LOAD-family factors are excluded from Maintenance Intelligence by design "
            "(Energy Opportunity evidence only) - this should never be called for one."
        )
    if factor_id in RECOMMENDED_CHECK_BY_FACTOR_ID:
        return RECOMMENDED_CHECK_BY_FACTOR_ID[factor_id]
    if family in GENERIC_CHECK_BY_FAMILY:
        return GENERIC_CHECK_BY_FAMILY[family]
    return _FALLBACK_CHECK
