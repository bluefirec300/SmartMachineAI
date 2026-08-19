from __future__ import annotations

from dataclasses import dataclass

from engine.savings_verification_evidence import (
    EVIDENCE_CONTEXT_MATCHED,
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_LIMITED,
    EVIDENCE_STRONG,
)

"""
Phase 14 - Asset Performance & Reliability Analytics registry.
Deliberately introduces NO new measurement and NO new context-matching
logic - every dimension below is an EXISTING engine.baseline_targets
BaselineTarget (raw or derived), and evidence-quality vocabulary is
reused unmodified from Phase 11.2 (engine.savings_verification_evidence)
rather than re-invented (item J.2/K of the approval).

DIRECTION REGISTRY PROVENANCE (item J.2 - mandatory engineering review):
Every entry's `direction` is derived MECHANICALLY from an existing,
already-approved authoritative source - never guessed from the tag/
target name:
  1. Phase 9's engine.anomaly_targets.AnomalyRule.direction, where a
     rule exists for this (equipment_type, target_key):
       - "high"  (only exceeding the expected range is anomalous) -> LOWER_IS_BETTER
       - "low"   (only falling below the expected range is anomalous) -> HIGHER_IS_BETTER
       - "both"  (deviation either way is anomalous) -> TARGET_RANGE
  2. Phase 12.1's engine.health_targets.HEALTH_FACTOR_REGISTRY factor
     descriptions, used to CONFIRM (never override) the Phase 9 mapping
     where a Phase 12.1 factor also exists for the same target_key -
     every case checked was consistent (e.g. "vibration increased
     beyond normal" / "filter DP increasing" both independently confirm
     LOWER_IS_BETTER; "flow-per-kW deteriorating" confirms HIGHER_IS_BETTER
     for that drift rule's "low" direction).
  3. INFORMATIONAL_ONLY where NEITHER source defines a direction - never
     fabricated (item 4/30 of the Phase 14 approval).

This table is the actual, complete registry - not hidden inside code.
See FACTORY_AI_DEVELOPMENT_STATUS.md's Phase 14 section for the same
table reproduced for engineering review, and
tests/test_performance_targets.py for a test that mechanically
re-derives this table from engine.anomaly_targets and flags any drift.
"""

PERFORMANCE_MODEL_VERSION = "1.0"
PERFORMANCE_PROVENANCE = "SIMULATION_TUNING"

# ---------------------------------------------------------------------------
# Direction semantics (item 10 of the Phase 14 spec)
# ---------------------------------------------------------------------------

LOWER_IS_BETTER = "LOWER_IS_BETTER"
HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
TARGET_RANGE = "TARGET_RANGE"
INFORMATIONAL_ONLY = "INFORMATIONAL_ONLY"

DIRECTION_VALUES = (LOWER_IS_BETTER, HIGHER_IS_BETTER, TARGET_RANGE, INFORMATIONAL_ONLY)

SOURCE_ANOMALY_RULE = "PHASE_9_ANOMALY_RULE_DIRECTION"
SOURCE_HEALTH_FACTOR = "PHASE_12_HEALTH_FACTOR_DESCRIPTION"
SOURCE_NONE = "NO_AUTHORITATIVE_SOURCE"


@dataclass(frozen=True)
class PerformanceDimensionDefinition:
    equipment_type: str
    target_key: str
    direction: str
    description: str
    source: str                    # one of SOURCE_* above, or a combination string for docs
    participates_in_degradation: bool
    unit: str | None = None
    provenance: str = PERFORMANCE_PROVENANCE

    def __post_init__(self):
        assert self.direction in DIRECTION_VALUES, f"unknown direction {self.direction!r}"


def _dim(equipment_type: str, target_key: str, direction: str, description: str, source: str, unit: str | None = None) -> PerformanceDimensionDefinition:
    return PerformanceDimensionDefinition(
        equipment_type=equipment_type, target_key=target_key, direction=direction, description=description,
        source=source, participates_in_degradation=(direction != INFORMATIONAL_ONLY), unit=unit,
    )


# Mechanically derived per the docstring above - one entry per BaselineTarget
# actually produced by engine.baseline_targets.EQUIPMENT_TYPE_PROFILES
# (raw_targets + derived_targets, every equipment type). Reproduced in
# full in the Phase 14 implementation report for engineering review.
PERFORMANCE_DIMENSION_REGISTRY: dict[tuple[str, str], PerformanceDimensionDefinition] = {
    dim.equipment_type + "|" + dim.target_key: dim for dim in [
        # --- plant_energy ---
        _dim("plant_energy", "Power_kW", LOWER_IS_BETTER,
             "Plant-wide electrical demand.", SOURCE_ANOMALY_RULE, "kW"),
        _dim("plant_energy", "non_production_demand_kw", LOWER_IS_BETTER,
             "Electrical demand while no production batch is running (should be low).", SOURCE_ANOMALY_RULE, "kW"),

        # --- air_compressor ---
        _dim("air_compressor", "Power_kW", LOWER_IS_BETTER,
             "Compressor electrical power for comparable running/load state.", SOURCE_ANOMALY_RULE, "kW"),
        _dim("air_compressor", "Pressure", INFORMATIONAL_ONLY,
             "Compressor discharge pressure - no Phase 9 anomaly rule or Phase 12.1 health factor exists for this "
             "target; no defensible direction established.", SOURCE_NONE, "bar"),
        _dim("air_compressor", "OutletTemp", INFORMATIONAL_ONLY,
             "Compressor outlet temperature - no Phase 9 anomaly rule or Phase 12.1 health factor exists for this "
             "target; no defensible direction established.", SOURCE_NONE, "degC"),

        # --- air_header ---
        _dim("air_header", "Flow", LOWER_IS_BETTER,
             "Compressed-air header flow - Phase 9's header_flow_high rule only flags EXCESS flow (leak/excess "
             "demand) as anomalous.", SOURCE_ANOMALY_RULE, "Nm3/h"),
        _dim("air_header", "Pressure", TARGET_RANGE,
             "Compressed-air header pressure - deviation either way from expected is anomalous per Phase 9.",
             SOURCE_ANOMALY_RULE, "bar"),
        _dim("air_header", "compressed_air_specific_energy", LOWER_IS_BETTER,
             "Specific energy (kWh delivered per Nm3) - lower is more efficient; Phase 9's own drift rule only "
             "flags an INCREASE as deteriorating.", SOURCE_ANOMALY_RULE, "kWh/Nm3"),

        # --- chiller ---
        _dim("chiller", "Power_kW", LOWER_IS_BETTER,
             "Chiller electrical power for comparable load/ambient conditions.", SOURCE_ANOMALY_RULE, "kW"),
        _dim("chiller", "LoadPct", INFORMATIONAL_ONLY,
             "Chiller load percentage - a context/operating-state variable (used as a matching dimension), not "
             "itself a Phase 9/12.1-judged performance direction.", SOURCE_NONE, "%"),
        _dim("chiller", "SupplyTemp", TARGET_RANGE,
             "Chilled water supply temperature - deviation either way from expected is anomalous per Phase 9.",
             SOURCE_ANOMALY_RULE, "degC"),
        _dim("chiller", "ReturnTemp", TARGET_RANGE,
             "Chilled water return temperature - deviation either way from expected is anomalous per Phase 9.",
             SOURCE_ANOMALY_RULE, "degC"),
        _dim("chiller", "WaterFlow", TARGET_RANGE,
             "Chilled water flow - deviation either way from expected is anomalous per Phase 9.",
             SOURCE_ANOMALY_RULE, "m3/h"),
        _dim("chiller", "cop", HIGHER_IS_BETTER,
             "Coefficient of performance - Phase 9's chiller_cop_low rule only flags a DECREASE as concerning; "
             "confirmed by Phase 12.1's chl_cop_performance factor.", SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR),
        _dim("chiller", "cooling_output_kw", TARGET_RANGE,
             "Cooling output - deviation either way from expected is anomalous per Phase 9 (too little is a "
             "process problem, too much can indicate a control/measurement issue).", SOURCE_ANOMALY_RULE, "kW"),

        # --- chilled_water_pump ---
        _dim("chilled_water_pump", "Power_kW", LOWER_IS_BETTER,
             "Pump electrical power for comparable load conditions.", SOURCE_ANOMALY_RULE, "kW"),
        _dim("chilled_water_pump", "Flow", TARGET_RANGE,
             "Delivered flow - deviation either way from expected is anomalous per Phase 9.", SOURCE_ANOMALY_RULE, "m3/h"),
        _dim("chilled_water_pump", "Frequency", INFORMATIONAL_ONLY,
             "VFD drive frequency - an operating-state variable, no Phase 9/12.1 direction defined.", SOURCE_NONE, "Hz"),
        _dim("chilled_water_pump", "Vibration", LOWER_IS_BETTER,
             "Vibration - Phase 9's pump_vibration_increase rule and Phase 12.1's chwp_vibration_condition factor "
             "both treat an INCREASE as the condition concern.", SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR, "mm/s"),
        _dim("chilled_water_pump", "BearingTemp", LOWER_IS_BETTER,
             "Bearing temperature - Phase 9 and Phase 12.1 both treat an INCREASE as the condition concern.",
             SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR, "degC"),
        _dim("chilled_water_pump", "delta_p_bar", TARGET_RANGE,
             "Differential pressure - deviation either way from expected is anomalous per Phase 9.", SOURCE_ANOMALY_RULE, "bar"),
        _dim("chilled_water_pump", "flow_per_kw", HIGHER_IS_BETTER,
             "Flow delivered per kW (hydraulic efficiency proxy) - Phase 9's drift rule and Phase 12.1's "
             "chwp_flow_per_kw_condition factor both treat a DECREASE as deteriorating.",
             SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR, "m3/h per kW"),

        # --- water_supply_pump (identical rule shape to chilled_water_pump) ---
        _dim("water_supply_pump", "Power_kW", LOWER_IS_BETTER,
             "Pump electrical power for comparable load conditions.", SOURCE_ANOMALY_RULE, "kW"),
        _dim("water_supply_pump", "Flow", TARGET_RANGE,
             "Delivered flow - deviation either way from expected is anomalous per Phase 9.", SOURCE_ANOMALY_RULE, "m3/h"),
        _dim("water_supply_pump", "Frequency", INFORMATIONAL_ONLY,
             "VFD drive frequency - an operating-state variable, no Phase 9/12.1 direction defined.", SOURCE_NONE, "Hz"),
        _dim("water_supply_pump", "Vibration", LOWER_IS_BETTER,
             "Vibration - Phase 9 and Phase 12.1 both treat an INCREASE as the condition concern.",
             SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR, "mm/s"),
        _dim("water_supply_pump", "BearingTemp", LOWER_IS_BETTER,
             "Bearing temperature - Phase 9 and Phase 12.1 both treat an INCREASE as the condition concern.",
             SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR, "degC"),
        _dim("water_supply_pump", "delta_p_bar", TARGET_RANGE,
             "Differential pressure - deviation either way from expected is anomalous per Phase 9.", SOURCE_ANOMALY_RULE, "bar"),
        _dim("water_supply_pump", "flow_per_kw", HIGHER_IS_BETTER,
             "Flow delivered per kW (hydraulic efficiency proxy) - a DECREASE is deteriorating per Phase 9/12.1.",
             SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR, "m3/h per kW"),

        # --- ahu ---
        _dim("ahu", "FanPower", LOWER_IS_BETTER,
             "Fan electrical power for comparable conditions.", SOURCE_ANOMALY_RULE, "kW"),
        _dim("ahu", "FanFrequency", INFORMATIONAL_ONLY,
             "Fan drive frequency - an operating-state variable, no Phase 9/12.1 direction defined.", SOURCE_NONE, "Hz"),
        _dim("ahu", "FilterDP", LOWER_IS_BETTER,
             "Filter differential pressure - Phase 9's drift rule and Phase 12.1's ahu_filter_dp_condition factor "
             "both treat an INCREASE (filter loading) as deteriorating.", SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR, "Pa"),
        _dim("ahu", "SupplyAirTemp", TARGET_RANGE,
             "Supply air temperature - deviation either way from expected is anomalous per Phase 9.", SOURCE_ANOMALY_RULE, "degC"),
        _dim("ahu", "ReturnAirTemp", INFORMATIONAL_ONLY,
             "Return air temperature - no Phase 9 anomaly rule or Phase 12.1 health factor exists for this target.",
             SOURCE_NONE, "degC"),

        # --- cold_room ---
        _dim("cold_room", "RoomTemp", TARGET_RANGE,
             "Room temperature - deviation either way from the learned normal range is anomalous per Phase 9.",
             SOURCE_ANOMALY_RULE, "degC"),
        _dim("cold_room", "CompressorPower", LOWER_IS_BETTER,
             "Compressor duty - Phase 9 and Phase 12.1 both treat an INCREASE above expected as the concern.",
             SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR, "kW"),

        # --- production_process ---
        _dim("production_process", "MotorPower", LOWER_IS_BETTER,
             "Motor power for comparable product/running state.", SOURCE_ANOMALY_RULE, "kW"),
        _dim("production_process", "MotorCurrent", INFORMATIONAL_ONLY,
             "Motor current - no Phase 9 anomaly rule or Phase 12.1 health factor exists for this target.",
             SOURCE_NONE, "A"),
        _dim("production_process", "ProcessTemp", TARGET_RANGE,
             "Process temperature - deviation either way from expected is anomalous per Phase 9.", SOURCE_ANOMALY_RULE, "degC"),
        _dim("production_process", "Vibration", LOWER_IS_BETTER,
             "Vibration baseline - Phase 9's drift rule and Phase 12.1's prod_vibration_condition factor both "
             "treat an INCREASE as the wear/condition concern.", SOURCE_ANOMALY_RULE + "+" + SOURCE_HEALTH_FACTOR, "mm/s"),

        # --- filling ---
        _dim("filling", "Power_kW", LOWER_IS_BETTER,
             "Power for comparable product/running state.", SOURCE_ANOMALY_RULE, "kW"),
    ]
}


def dimension_for(equipment_type: str, target_key: str) -> PerformanceDimensionDefinition:
    """Never returns None - a target discoverable via
    engine.baseline_targets that has no registry entry (should not
    happen; covered by tests/test_performance_targets.py) falls back to
    a safe INFORMATIONAL_ONLY definition rather than crashing (item 30's
    'fail gracefully' requirement)."""
    key = f"{equipment_type}|{target_key}"
    found = PERFORMANCE_DIMENSION_REGISTRY.get(key)
    if found is not None:
        return found
    return PerformanceDimensionDefinition(
        equipment_type=equipment_type, target_key=target_key, direction=INFORMATIONAL_ONLY,
        description="No registry entry found for this dimension - treated as informational only, never fabricated.",
        source=SOURCE_NONE, participates_in_degradation=False,
    )


# ---------------------------------------------------------------------------
# Performance states (item 11)
# ---------------------------------------------------------------------------

STATE_IMPROVING = "IMPROVING"
STATE_STABLE = "STABLE"
STATE_DEGRADING = "DEGRADING"
STATE_SIGNIFICANTLY_DEGRADING = "SIGNIFICANTLY_DEGRADING"
STATE_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
STATE_NOT_CLASSIFIED = "NOT_CLASSIFIED"   # INFORMATIONAL_ONLY dimensions - distinct from INSUFFICIENT_EVIDENCE:
                                           # evidence may be excellent, there's just no direction to judge it by.

PERFORMANCE_STATE_VALUES = (
    STATE_IMPROVING, STATE_STABLE, STATE_DEGRADING, STATE_SIGNIFICANTLY_DEGRADING,
    STATE_INSUFFICIENT_EVIDENCE, STATE_NOT_CLASSIFIED,
)

# Illustrative SIMULATION_TUNING cut points on the SIGNED (direction-
# adjusted) percent change from reference. Below STATE_CHANGE_THRESHOLD_PCT
# in magnitude is treated as noise (STABLE), never a fabricated precise
# trend from statistical wobble. DEGRADING_THRESHOLD_PCT deliberately
# reuses the same 15% figure Phase 9's own deviation_threshold_pct uses
# for the overwhelming majority of its rules (a defensible, already-
# reviewed magnitude for "this deviation is real"), rather than a new
# unrelated number.
STATE_CHANGE_THRESHOLD_PCT = 5.0
DEGRADING_THRESHOLD_PCT = 15.0
SIGNIFICANTLY_DEGRADING_THRESHOLD_PCT = 30.0

# ---------------------------------------------------------------------------
# Degradation persistence (item J.4) - consecutive materially-degrading
# PERSISTED observations required before a DEGRADING/SIGNIFICANTLY_DEGRADING
# state is reported as "sustained" rather than "one abnormal observation".
# Reuses the SAME small-integer-consecutive-evaluations concept Phase 9
# already uses (AnomalyRule.persistence_periods_open, modal value 3-4
# across the registry) - not a calendar-duration guess. The UI derives
# any human-readable duration ("persisted for 8 days") from the actual
# persisted computed_at timestamps of the qualifying run, never by
# assuming a fixed cycle-to-calendar-day ratio.
# ---------------------------------------------------------------------------

SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS = 3

# ---------------------------------------------------------------------------
# Persistence materiality (item J.3) - when the asset_performance_worker
# should write a NEW asset_performance_observations row rather than
# treating the latest evidence as unchanged. Named, centralized,
# documented, tested (tests/test_performance_orchestration.py) -
# recalibratable later against real factory history without touching
# calculation code.
# ---------------------------------------------------------------------------

PERFORMANCE_OBSERVATION_MATERIAL_CHANGE_THRESHOLD_PCT = 5.0   # matches STATE_CHANGE_THRESHOLD_PCT deliberately -
                                                                # the same magnitude that would flip a displayed state
PERFORMANCE_HEARTBEAT_MAX_INTERVAL_HOURS = 24.0                # mirrors Phase 12.2's own HEARTBEAT_MAX_INTERVAL_HOURS

# ---------------------------------------------------------------------------
# Maintenance effectiveness (items 18/19)
# ---------------------------------------------------------------------------

EFFECTIVENESS_IMPROVED = "IMPROVED"
EFFECTIVENESS_NO_MEASURABLE_CHANGE = "NO_MEASURABLE_CHANGE"
EFFECTIVENESS_WORSENED = "WORSENED"
EFFECTIVENESS_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
EFFECTIVENESS_NOT_CLASSIFIED = "NOT_CLASSIFIED"

EFFECTIVENESS_VALUES = (
    EFFECTIVENESS_IMPROVED, EFFECTIVENESS_NO_MEASURABLE_CHANGE, EFFECTIVENESS_WORSENED,
    EFFECTIVENESS_INSUFFICIENT_EVIDENCE, EFFECTIVENESS_NOT_CLASSIFIED,
)

EFFECTIVENESS_CHANGE_THRESHOLD_PCT = STATE_CHANGE_THRESHOLD_PCT  # same "is this a real change at all" magnitude

# Post-maintenance evidence only becomes eligible from performed_at plus
# this many days - lets the equipment reach steady state before being
# compared, mirroring Phase 11.2's stabilization concept but with its
# own name/value since maintenance_log categories ("Preventive
# Maintenance"/"Repair"/"Replacement"/"Inspection"/"Calibration") are a
# different vocabulary from Phase 10/11's action_category.
MAINTENANCE_STABILIZATION_DAYS = 1
MAINTENANCE_PRE_WINDOW_DAYS_MAX = 30   # bounded lookback before the maintenance event - never an unbounded historian scan

# Re-export the SAME evidence-quality vocabulary Phase 11.2 already
# established - never a second, parallel vocabulary (item K).
__all_evidence__ = (EVIDENCE_STRONG, EVIDENCE_CONTEXT_MATCHED, EVIDENCE_LIMITED, EVIDENCE_INSUFFICIENT)

# ---------------------------------------------------------------------------
# Asset Attention Ranking (item I / the mandatory modification) - every
# weight explicit, capped, inspectable per-component. Evidence quality
# is DELIBERATELY ABSENT from this formula - it is reported as a
# separate confidence field/gate, never a score component (the
# mandatory correction: poor/missing evidence must never itself
# increase a degradation-attention score).
# ---------------------------------------------------------------------------

ATTENTION_DEGRADATION_MAGNITUDE_MAX = 40.0
ATTENTION_PERSISTENCE_MAX = 25.0
ATTENTION_CRITICALITY_MAX = 20.0
ATTENTION_MAINTENANCE_INEFFECTIVENESS_MAX = 15.0

# Same criticality vocabulary/values Phase 13 already established
# (ui/pages/13_Factory_Configuration.py's own dropdown) - never a
# competing criticality model (item 26).
ATTENTION_CRITICALITY_CONTRIBUTION: dict[str, float] = {
    "Critical": ATTENTION_CRITICALITY_MAX,
    "High": ATTENTION_CRITICALITY_MAX * (10.0 / 15.0),
    "Medium": ATTENTION_CRITICALITY_MAX * (5.0 / 15.0),
    "Low": 0.0,
}

# Persistence scaling - each qualifying consecutive-degrading
# observation beyond the sustained-degradation threshold adds a small
# amount, capped.
ATTENTION_PERSISTENCE_PER_OBSERVATION = ATTENTION_PERSISTENCE_MAX / (SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS + 5)
