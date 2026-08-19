from __future__ import annotations

from dataclasses import dataclass

"""
Phase 12.1 - Equipment Health scoring registry. Mirrors the proven
Phase 8/9/10/11 discipline exactly: a curated, explicit list (never an
automatic sweep), centralized numeric policy (never scattered across
files), and mandatory provenance on every weight (item 6/31).

This is an ENGINEERING PRIORITIZATION TOOL, not a failure predictor:
  - No Remaining Useful Life, no failure probability, no predicted
    failure date, no ML classifier (item 26).
  - No root-cause diagnosis - factors state EVIDENCE, never a named
    mechanical fault like "bearing failure" (item 27).
  - Health Score answers "how much engineering attention does
    available evidence suggest?" - Assessment Confidence/Coverage
    answers "how complete/trustworthy is this assessment?" - these are
    two SEPARATE outputs, never collapsed into one (item 3).

Every factor below maps to an EXISTING Phase 8 baseline target via an
EXISTING Phase 9 anomaly rule_key, or to EXISTING maintenance/event
data - Phase 12.1 introduces no new raw measurement, no new anomaly
detection logic, and no new instrumentation (item 1's "do not
duplicate anomaly/baseline/maintenance/event systems").

Deliberately excluded (audited, not overlooked):
  - air_compressor has NO condition-category anomaly rule today (no
    vibration/discharge-temp anomaly is baselined for compressors -
    engine/anomaly_targets.py only defines compressor_power_deviation).
    A missing CONDITION factor for air_compressor is disclosed via
    missing_factors, never fabricated.
  - cold_room similarly has no CONDITION-category rule (only PROCESS/
    UTILITY) - same honest disclosure.
  - filling has only a Power_kW anomaly rule - no CONDITION or PROCESS
    factor exists for it.
  - plant_energy and air_header are explicitly OUT of Phase 12.1 scope
    (item 30: health is equipment-level, not a shared utility line or
    factory-wide entity).
  - The known-invalid pump hydraulic-efficiency relationship (Phase 8's
    own documented exclusion) is not reintroduced here.
"""

HEALTH_MODEL_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Provenance - identical vocabulary to Phase 9's threshold_provenance
# (engine/anomaly_targets.py) so a reader never has to learn two
# parallel provenance systems. Every factor below is SIMULATION_TUNING:
# a reasonable dev-stage engineering judgment call, NOT a manufacturer
# limit or a real commissioned plant's statistically-derived figure.
# ---------------------------------------------------------------------------

HEALTH_FACTOR_PROVENANCE_VALUES = (
    "USER_CONFIGURED", "ENGINEERING_RULE", "MANUFACTURER_REFERENCE", "STATISTICAL", "SIMULATION_TUNING",
)

# ---------------------------------------------------------------------------
# Factor families - each has ONE centralized penalty ceiling (item 9's
# double-count prevention). Multiple factors within the same family may
# each contribute evidence and may strengthen each other, but their
# COMBINED contribution can never exceed the family cap - this is what
# stops "vibration deviation + vibration drift + vibration engineering
# warning" (or any correlated cluster) from stacking into an unbounded
# penalty for what is really one underlying physical signal.
# ---------------------------------------------------------------------------

FAMILY_CONDITION = "CONDITION"
FAMILY_ELECTRICAL_LOAD = "ELECTRICAL_LOAD"
FAMILY_PERFORMANCE = "PERFORMANCE"
FAMILY_PROCESS = "PROCESS"
FAMILY_EVENTS = "EVENTS"
FAMILY_MAINTENANCE = "MAINTENANCE"

FAMILY_VALUES = (
    FAMILY_CONDITION, FAMILY_ELECTRICAL_LOAD, FAMILY_PERFORMANCE, FAMILY_PROCESS, FAMILY_EVENTS, FAMILY_MAINTENANCE,
)

# Deliberately sums to 95, not 100 - even if every family maxed out
# simultaneously (which family caps make very unlikely in practice),
# a floor of 5 remains, avoiding a hard-to-explain "penalties alone can
# drive any equipment all the way to a bare 0" edge case, while still
# leaving CONDITION (genuine mechanical evidence) the dominant weight.
FAMILY_MAX_PENALTY: dict[str, float] = {
    FAMILY_CONDITION: 30.0,
    FAMILY_PERFORMANCE: 18.0,
    FAMILY_PROCESS: 15.0,
    FAMILY_ELECTRICAL_LOAD: 12.0,
    FAMILY_EVENTS: 12.0,
    FAMILY_MAINTENANCE: 8.0,
}

# ---------------------------------------------------------------------------
# Anomaly severity -> base penalty (item 12: severity feeds the score,
# but is NEVER simply mapped 1:1 - confidence/recency/recurrence all
# still apply on top of this base). Keys match
# engine.anomaly_engine.SEVERITY_LEVELS exactly.
# ---------------------------------------------------------------------------

SEVERITY_BASE_PENALTY: dict[str, float] = {
    "INFORMATION": 2.0,
    "ATTENTION": 6.0,
    "WARNING": 12.0,
    "HIGH": 20.0,
}

# ---------------------------------------------------------------------------
# Baseline/anomaly statistical confidence -> contribution multiplier
# (item 13). Matches Phase 8's own _classify() confidence vocabulary
# exactly ("High"/"Medium"/"Low"). A confidence value NOT in this dict
# (i.e. no baseline at all) means the anomaly is excluded from health
# evidence entirely - "Insufficient: do not use as statistical health
# evidence" is enforced by simple absence from this table, not a
# separate suppression flag.
# ---------------------------------------------------------------------------

CONFIDENCE_MULTIPLIER: dict[str, float] = {
    "High": 1.0,
    "Medium": 0.75,
    "Low": 0.4,
}

# ---------------------------------------------------------------------------
# Recency (item 10). An OPEN anomaly is current by definition - full
# multiplier. A RESOLVED anomaly's residual influence decays LINEARLY
# from 1.0 (just resolved) to 0.0 at RECENCY_DECAY_DAYS - old, resolved
# findings stop mattering to the CURRENT score, without being erased
# from history (recurrence, below, is what keeps their memory alive).
# ---------------------------------------------------------------------------

RECENCY_DECAY_DAYS = 30

# ---------------------------------------------------------------------------
# Recurrence (item 11). Uses anomaly_engine.count_prior_occurrences()'s
# real RESOLVED-row count, never a bare mutable counter. Strengthens a
# factor's contribution up to a hard cap - recurrence sharpens
# engineering attention, it does not let one factor grow without bound.
# ---------------------------------------------------------------------------

RECURRENCE_STEP = 0.06
RECURRENCE_MAX_MULTIPLIER = 1.6

# ---------------------------------------------------------------------------
# Data-feed freshness gates (item 17/36). Distinct from an equipment's
# own "no recent finding" (which is legitimate good evidence) - this
# asks "has the underlying feed produced ANY row recently, system-wide?"
# If not, the whole family is UNAVAILABLE for every equipment (a data
# quality problem), never silently read as "clean."
# ---------------------------------------------------------------------------

EVENTS_FEED_STALENESS_SECONDS = 72 * 3600     # 3 days - generous vs. the feed's historical multi-times-per-day cadence
ANOMALY_FEED_STALENESS_SECONDS = 48 * 3600    # 2 days - anomaly_worker runs far more often than this

# ---------------------------------------------------------------------------
# Maintenance-overdue penalty shape (item 15). Deliberately modest and
# capped by FAMILY_MAINTENANCE - overdue maintenance is a RISK/ATTENTION
# factor, never presented as evidence the machine is physically failing.
# ---------------------------------------------------------------------------

MAINTENANCE_OVERDUE_BASE_PENALTY = 3.0
MAINTENANCE_OVERDUE_PER_DAY = 0.15
MAINTENANCE_OVERDUE_MAX_DAYS_SCALED = 30   # additional per-day penalty stops accruing beyond this many overdue days

# ---------------------------------------------------------------------------
# Events-family penalty shape (item 14). Counts qualifying alarm/warning
# machine_events for this equipment's own tags within a lookback window
# - NOT every unrelated plant-wide event (a fire-system alarm must never
# become generic mechanical-health evidence for an unrelated pump).
# ---------------------------------------------------------------------------

EVENTS_LOOKBACK_DAYS = 30
EVENTS_PENALTY_PER_QUALIFYING_EVENT = 2.0
EVENTS_MAX_QUALIFYING_EVENTS_SCALED = 6     # additional per-event penalty stops accruing beyond this many events

# ---------------------------------------------------------------------------
# Health bands (item 4) - centralized, single source of truth. Avoid
# CRITICAL/FAILURE IMMINENT/ABOUT TO FAIL language entirely, per your
# explicit instruction - this is a prioritization tool, not a safety or
# predictive-maintenance claim.
# ---------------------------------------------------------------------------

# Half-open on the low end (score >= low), except the bottom band which
# also accepts 0 - a float score (e.g. 69.7) must land in exactly one
# band, so boundaries are real cut points, not integer buckets that
# leave gaps between e.g. 69 and 70.
HEALTH_BANDS: tuple[tuple[float, float, str], ...] = (
    (85.0, 100.0, "HEALTHY"),
    (70.0, 85.0, "MONITOR"),
    (50.0, 70.0, "ATTENTION"),
    (0.0, 50.0, "INVESTIGATE"),
)


def health_band_for_score(score: float) -> str:
    if not (0.0 <= score <= 100.0):
        raise ValueError(f"score {score!r} outside the defined 0-100 band range")
    # HEALTH_BANDS is ordered highest-to-lowest - the first band whose
    # low bound the score meets or exceeds is its band. This makes the
    # top band's upper bound (100.0) inclusive without a special case,
    # and guarantees every float in [0, 100] lands in exactly one band.
    for low, _high, label in HEALTH_BANDS:
        if score >= low:
            return label
    raise ValueError(f"score {score!r} outside the defined 0-100 band range")


# ---------------------------------------------------------------------------
# Coverage/assessment-confidence tiers (item 18/19). HIGH/MEDIUM/LOW are
# fixed, deterministic labels derived from real applicable-vs-usable
# factor counts plus the statistical confidence of the evidence actually
# used - never an opaque AI-style percentage.
# ---------------------------------------------------------------------------

COVERAGE_HIGH = "HIGH"
COVERAGE_MEDIUM = "MEDIUM"
COVERAGE_LOW = "LOW"
COVERAGE_INSUFFICIENT = "INSUFFICIENT"

COVERAGE_STATUS_VALUES = (COVERAGE_HIGH, COVERAGE_MEDIUM, COVERAGE_LOW, COVERAGE_INSUFFICIENT)

# Score availability gate (item 19): INSUFFICIENT coverage means no
# numeric score is shown at all (Unavailable) rather than a misleading
# precise number. LOW coverage still allows a numeric score, but it
# MUST be labeled provisional - never presented with the same weight as
# a HIGH/MEDIUM-confidence result.
PROVISIONAL_COVERAGE_STATUSES = (COVERAGE_LOW,)
UNAVAILABLE_COVERAGE_STATUSES = (COVERAGE_INSUFFICIENT,)

# Minimum fraction of applicable factors that must have usable evidence
# for each coverage tier - the ratio gate. A confidence-based gate
# (see engine/health_engine.py) additionally CAPS the tier down when the
# usable evidence itself is statistically weak (Low baseline
# confidence), so a numerically-complete-looking assessment built
# entirely on Low-confidence baselines is never reported as HIGH.
COVERAGE_RATIO_HIGH_MIN = 0.85
COVERAGE_RATIO_MEDIUM_MIN = 0.60
COVERAGE_RATIO_LOW_MIN = 0.34   # below this -> INSUFFICIENT regardless of confidence


@dataclass(frozen=True)
class HealthFactorDefinition:
    factor_id: str                  # unique within its equipment_type, e.g. "chwp_vibration_condition"
    equipment_type: str             # matches Phase 8's equipment_type / Phase 9's AnomalyRule.equipment_type
    family: str                     # one of FAMILY_VALUES
    source_type: str                # "anomaly" | "maintenance" | "event"
    source_rule_key: str | None     # engine.anomaly_targets.AnomalyRule.rule_key, for source_type == "anomaly"
    max_penalty: float              # this factor's own individual cap (family cap still applies on top)
    description: str                # plain-English, shown in factor_results - never AI prose
    provenance: str = "SIMULATION_TUNING"

    def __post_init__(self):
        assert self.family in FAMILY_VALUES, f"unknown family {self.family!r}"
        assert self.source_type in ("anomaly", "maintenance", "event"), f"unknown source_type {self.source_type!r}"
        assert self.provenance in HEALTH_FACTOR_PROVENANCE_VALUES, f"unknown provenance {self.provenance!r}"


def _maintenance_factor(equipment_type: str) -> HealthFactorDefinition:
    return HealthFactorDefinition(
        factor_id=f"{equipment_type}_maintenance_overdue", equipment_type=equipment_type,
        family=FAMILY_MAINTENANCE, source_type="maintenance", source_rule_key=None,
        max_penalty=FAMILY_MAX_PENALTY[FAMILY_MAINTENANCE],
        description="Scheduled maintenance is overdue (risk/attention factor - not evidence of physical degradation).",
    )


def _events_factor(equipment_type: str) -> HealthFactorDefinition:
    return HealthFactorDefinition(
        factor_id=f"{equipment_type}_repeated_events", equipment_type=equipment_type,
        family=FAMILY_EVENTS, source_type="event", source_rule_key=None,
        max_penalty=FAMILY_MAX_PENALTY[FAMILY_EVENTS],
        description="Repeated engineering alarm/warning events for this equipment's own tags in the recent window.",
    )


HEALTH_FACTOR_REGISTRY: dict[str, list[HealthFactorDefinition]] = {
    "air_compressor": [
        HealthFactorDefinition(
            factor_id="ac_power_load", equipment_type="air_compressor", family=FAMILY_ELECTRICAL_LOAD,
            source_type="anomaly", source_rule_key="compressor_power_deviation", max_penalty=12.0,
            description="Power draw above expected for the same running/load state (context-matched, not a raw increase).",
        ),
        _maintenance_factor("air_compressor"),
        _events_factor("air_compressor"),
    ],
    "chiller": [
        HealthFactorDefinition(
            factor_id="chl_cop_performance", equipment_type="chiller", family=FAMILY_PERFORMANCE,
            source_type="anomaly", source_rule_key="chiller_cop_low", max_penalty=18.0,
            description="Coefficient of performance below expected for comparable load/ambient conditions.",
        ),
        HealthFactorDefinition(
            factor_id="chl_power_load", equipment_type="chiller", family=FAMILY_ELECTRICAL_LOAD,
            source_type="anomaly", source_rule_key="chiller_power_deviation", max_penalty=10.0,
            description="Power draw above expected for comparable load/ambient conditions.",
        ),
        HealthFactorDefinition(
            factor_id="chl_cooling_output_process", equipment_type="chiller", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="chiller_cooling_output_deviation", max_penalty=10.0,
            description="Cooling output abnormal relative to expected.",
        ),
        HealthFactorDefinition(
            factor_id="chl_supply_temp_process", equipment_type="chiller", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="chiller_supply_temp_deviation", max_penalty=8.0,
            description="Chilled water supply temperature abnormal.",
        ),
        HealthFactorDefinition(
            factor_id="chl_return_temp_process", equipment_type="chiller", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="chiller_return_temp_deviation", max_penalty=8.0,
            description="Chilled water return temperature abnormal.",
        ),
        HealthFactorDefinition(
            factor_id="chl_waterflow_process", equipment_type="chiller", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="chiller_waterflow_deviation", max_penalty=8.0,
            description="Chilled water flow abnormal.",
        ),
        _maintenance_factor("chiller"),
        _events_factor("chiller"),
    ],
    "chilled_water_pump": [
        HealthFactorDefinition(
            factor_id="chwp_vibration_condition", equipment_type="chilled_water_pump", family=FAMILY_CONDITION,
            source_type="anomaly", source_rule_key="pump_vibration_increase", max_penalty=15.0,
            description="Vibration increased beyond the learned normal range.",
        ),
        HealthFactorDefinition(
            factor_id="chwp_bearing_temp_condition", equipment_type="chilled_water_pump", family=FAMILY_CONDITION,
            source_type="anomaly", source_rule_key="pump_bearing_temp_increase", max_penalty=15.0,
            description="Bearing temperature increased beyond the learned normal range.",
        ),
        HealthFactorDefinition(
            factor_id="chwp_flow_per_kw_condition", equipment_type="chilled_water_pump", family=FAMILY_CONDITION,
            source_type="anomaly", source_rule_key="pump_flow_per_kw_drift", max_penalty=12.0,
            description="Flow delivered per kW deteriorating over time (efficiency/wear indicator).",
        ),
        HealthFactorDefinition(
            factor_id="chwp_power_load", equipment_type="chilled_water_pump", family=FAMILY_ELECTRICAL_LOAD,
            source_type="anomaly", source_rule_key="pump_power_deviation", max_penalty=10.0,
            description="Power draw above expected for comparable load conditions.",
        ),
        HealthFactorDefinition(
            factor_id="chwp_flow_process", equipment_type="chilled_water_pump", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="pump_flow_deviation", max_penalty=8.0,
            description="Delivered flow abnormal relative to expected.",
        ),
        HealthFactorDefinition(
            factor_id="chwp_delta_p_process", equipment_type="chilled_water_pump", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="pump_delta_p_deviation", max_penalty=8.0,
            description="Differential pressure abnormal relative to expected.",
        ),
        _maintenance_factor("chilled_water_pump"),
        _events_factor("chilled_water_pump"),
    ],
    "water_supply_pump": [
        HealthFactorDefinition(
            factor_id="wsp_vibration_condition", equipment_type="water_supply_pump", family=FAMILY_CONDITION,
            source_type="anomaly", source_rule_key="wsp_vibration_increase", max_penalty=15.0,
            description="Vibration increased beyond the learned normal range.",
        ),
        HealthFactorDefinition(
            factor_id="wsp_bearing_temp_condition", equipment_type="water_supply_pump", family=FAMILY_CONDITION,
            source_type="anomaly", source_rule_key="wsp_bearing_temp_increase", max_penalty=15.0,
            description="Bearing temperature increased beyond the learned normal range.",
        ),
        HealthFactorDefinition(
            factor_id="wsp_flow_per_kw_condition", equipment_type="water_supply_pump", family=FAMILY_CONDITION,
            source_type="anomaly", source_rule_key="wsp_flow_per_kw_drift", max_penalty=12.0,
            description="Flow delivered per kW deteriorating over time (efficiency/wear indicator).",
        ),
        HealthFactorDefinition(
            factor_id="wsp_power_load", equipment_type="water_supply_pump", family=FAMILY_ELECTRICAL_LOAD,
            source_type="anomaly", source_rule_key="wsp_power_deviation", max_penalty=10.0,
            description="Power draw above expected for comparable load conditions.",
        ),
        HealthFactorDefinition(
            factor_id="wsp_flow_process", equipment_type="water_supply_pump", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="wsp_flow_deviation", max_penalty=8.0,
            description="Delivered flow abnormal relative to expected.",
        ),
        HealthFactorDefinition(
            factor_id="wsp_delta_p_process", equipment_type="water_supply_pump", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="wsp_delta_p_deviation", max_penalty=8.0,
            description="Differential pressure abnormal relative to expected.",
        ),
        _maintenance_factor("water_supply_pump"),
        _events_factor("water_supply_pump"),
    ],
    "ahu": [
        HealthFactorDefinition(
            factor_id="ahu_filter_dp_condition", equipment_type="ahu", family=FAMILY_CONDITION,
            source_type="anomaly", source_rule_key="ahu_filter_dp_drift", max_penalty=15.0,
            description="Filter differential pressure increasing over time (filter loading/condition indicator).",
        ),
        HealthFactorDefinition(
            factor_id="ahu_fan_power_load", equipment_type="ahu", family=FAMILY_ELECTRICAL_LOAD,
            source_type="anomaly", source_rule_key="ahu_fan_power_deviation", max_penalty=10.0,
            description="Fan power draw above expected.",
        ),
        HealthFactorDefinition(
            factor_id="ahu_supply_air_temp_process", equipment_type="ahu", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="ahu_supply_air_temp_deviation", max_penalty=8.0,
            description="Supply air temperature abnormal relative to expected.",
        ),
        _maintenance_factor("ahu"),
        _events_factor("ahu"),
    ],
    "cold_room": [
        HealthFactorDefinition(
            factor_id="cr_room_temp_process", equipment_type="cold_room", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="coldroom_room_temp_abnormal", max_penalty=12.0,
            description="Room temperature outside the learned normal range (temperature-behavior proxy).",
        ),
        HealthFactorDefinition(
            factor_id="cr_compressor_power_load", equipment_type="cold_room", family=FAMILY_ELECTRICAL_LOAD,
            source_type="anomaly", source_rule_key="coldroom_compressor_power_deviation", max_penalty=10.0,
            description="Compressor duty above expected.",
        ),
        _maintenance_factor("cold_room"),
        _events_factor("cold_room"),
    ],
    "production_process": [
        HealthFactorDefinition(
            factor_id="prod_vibration_condition", equipment_type="production_process", family=FAMILY_CONDITION,
            source_type="anomaly", source_rule_key="production_vibration_drift", max_penalty=18.0,
            description="Vibration baseline increasing over time (wear indicator).",
        ),
        HealthFactorDefinition(
            factor_id="prod_motor_power_load", equipment_type="production_process", family=FAMILY_ELECTRICAL_LOAD,
            source_type="anomaly", source_rule_key="production_motor_power_deviation", max_penalty=10.0,
            description="Motor power above expected for comparable product/running state.",
        ),
        HealthFactorDefinition(
            factor_id="prod_process_temp_process", equipment_type="production_process", family=FAMILY_PROCESS,
            source_type="anomaly", source_rule_key="production_process_temp_deviation", max_penalty=8.0,
            description="Process temperature abnormal relative to expected.",
        ),
        _maintenance_factor("production_process"),
        _events_factor("production_process"),
    ],
    "filling": [
        HealthFactorDefinition(
            factor_id="fill_power_load", equipment_type="filling", family=FAMILY_ELECTRICAL_LOAD,
            source_type="anomaly", source_rule_key="filling_power_deviation", max_penalty=10.0,
            description="Power draw above expected for comparable product/running state.",
        ),
        _maintenance_factor("filling"),
        _events_factor("filling"),
    ],
}

SUPPORTED_EQUIPMENT_TYPES: tuple[str, ...] = tuple(HEALTH_FACTOR_REGISTRY.keys())


def factors_for_equipment_type(equipment_type: str) -> list[HealthFactorDefinition]:
    return list(HEALTH_FACTOR_REGISTRY.get(equipment_type, []))
