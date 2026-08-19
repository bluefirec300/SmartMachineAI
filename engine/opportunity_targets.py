from __future__ import annotations

from dataclasses import dataclass

from engine.anomaly_targets import THRESHOLD_PROVENANCE_VALUES

"""
Phase 10 - Energy Opportunity Engine rule registry. Mirrors Phase 9's
engine/anomaly_targets.py discipline exactly: curated, not automatic -
every rule maps to an EXISTING Phase 9 anomaly rule (Phase 10 invents
no new measurements, no new instrumentation, and never independently
recreates Phase 9's excess-energy calculation).

SAVING_BASIS - per your Phase 10 correction, a context-matched (Level
A/B) baseline making Phase 9's observed excess more CREDIBLE does NOT
make it automatically RECOVERABLE. A rule may only declare a
non-"unavailable" saving basis when it has an individually-justified,
defensible method for the avoidable/achievable portion:
  - "engineering_target_delta" - a real, user-configured engineering
    target exists for the tag (e.g. a target header pressure). No such
    configuration mechanism exists anywhere in this schema yet (only
    warning/alarm thresholds do, which are safety limits, not
    efficiency targets) - so no rule uses this today. Kept as a valid
    enum value so a future rule/config addition can use it without a
    schema change here.
  - "justified_baseline_target" - a rule-specific, individually-argued
    case where the learned baseline itself IS the achievable target
    (e.g. comparing current specific energy against this SAME
    equipment's own historically-demonstrated performance via a
    mature drift comparison). Considered for the compressed-air
    specific-energy drift rule, but deliberately NOT implemented in
    this initial rule set - it would require a new Nm3-throughput
    integration to convert a kWh/Nm3 delta into a real kWh figure,
    which is real new physics that hasn't been built or verified yet.
    Rather than ship an unverified calculation, that rule is deferred
    (see "deferred" note in ANOMALY_TO_OPPORTUNITY notes below) and
    this enum value stays available, unused, for when it's built properly.
  - "unavailable" - the honest, correct default for every rule in this
    initial build: Phase 9 shows the excess ENERGY/COST that occurred
    (kept as evidence), Phase 10 cannot yet defensibly say how much of
    it is avoidable.
"""

SAVING_BASIS_ENGINEERING_TARGET = "engineering_target_delta"
SAVING_BASIS_JUSTIFIED_BASELINE = "justified_baseline_target"
SAVING_BASIS_UNAVAILABLE = "unavailable"

SAVING_BASIS_VALUES = (SAVING_BASIS_ENGINEERING_TARGET, SAVING_BASIS_JUSTIFIED_BASELINE, SAVING_BASIS_UNAVAILABLE)

DISMISSAL_REASONS = ("NOT_ACTIONABLE", "EXPECTED_OPERATION", "KNOWN_ISSUE", "INSUFFICIENT_EVIDENCE", "OTHER")

CATEGORY_COMPRESSED_AIR = "COMPRESSED_AIR"
CATEGORY_CHILLER = "CHILLER"
CATEGORY_PUMP = "PUMP"
CATEGORY_AHU = "AHU"
CATEGORY_COLD_ROOM = "COLD_ROOM"
CATEGORY_PLANT = "PLANT"
CATEGORY_PRODUCTION = "PRODUCTION"

STATUS_NEW = "NEW"
STATUS_DISMISSED = "DISMISSED"


@dataclass(frozen=True)
class OpportunityRule:
    rule_key: str
    source_anomaly_rule_key: str        # exactly one Phase 9 rule_key - keeps qualification unambiguous
    equipment_type: str                 # matches Phase 9's/Phase 8's equipment_type
    category: str
    title_template: str                 # "{instance}" substituted at evaluation time
    saving_basis: str                   # one of SAVING_BASIS_VALUES - the METHOD this rule is designed to use
    minimum_meaningful_impact_cost: float
    non_production_only: bool           # True: only qualifies while the plant was in non-production state
    default_implementation_difficulty: str | None   # None = UNKNOWN, never fabricated per-instance
    recommendation_template: str
    assumptions: tuple[str, ...]
    exclusions: tuple[str, ...]
    provenance: str


def _rule(**kwargs) -> OpportunityRule:
    kwargs.setdefault("provenance", "SIMULATION_TUNING")
    kwargs.setdefault("non_production_only", False)
    kwargs.setdefault("default_implementation_difficulty", None)
    assert kwargs["provenance"] in THRESHOLD_PROVENANCE_VALUES
    assert kwargs["saving_basis"] in SAVING_BASIS_VALUES
    return OpportunityRule(**kwargs)


OPPORTUNITY_RULES: list[OpportunityRule] = [
    _rule(
        rule_key="plant_high_demand_opportunity", source_anomaly_rule_key="plant_high_demand",
        equipment_type="plant_energy", category=CATEGORY_PLANT,
        title_template="{instance} Elevated Plant Demand - Investigation Opportunity",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        default_implementation_difficulty=None,
        recommendation_template="Review plant demand at {instance} against comparable historical conditions - "
                                 "investigate whether the elevated demand reflects a known, expected cause "
                                 "(higher production, ambient load) or an avoidable one.",
        assumptions=("Observed excess is Phase 9's own interval-integrated, tariff-applied figure - not recomputed here.",),
        exclusions=("Does not attempt to separate production-driven demand from base-load demand - see the "
                     "non-production-specific rule for that distinction.",),
    ),
    _rule(
        rule_key="plant_non_production_demand_opportunity", source_anomaly_rule_key="plant_elevated_non_production_demand",
        equipment_type="plant_energy", category=CATEGORY_PLANT,
        title_template="{instance} Elevated Non-production Demand - Investigation Opportunity",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        recommendation_template="Review non-production base load at {instance} - inspect for equipment left "
                                 "running unnecessarily during non-production periods.",
        assumptions=("The source anomaly's own target (non_production_demand_kw) already restricts to buckets "
                     "where the plant was in a non-production state (Phase 8 derived target).",),
        exclusions=("A baseline match, even Level A/B, does not by itself prove the excess is avoidable - some "
                     "non-production load may be legitimate (e.g. refrigeration that must stay on).",),
    ),
    _rule(
        rule_key="compressor_non_production_air_opportunity", source_anomaly_rule_key="compressor_power_deviation",
        equipment_type="air_compressor", category=CATEGORY_COMPRESSED_AIR,
        title_template="{instance} Potential Compressed-air Loss During Non-production",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        non_production_only=True,
        recommendation_template="Inspect compressed-air distribution and sequencing for {instance} for "
                                 "unintended demand/unloaded operation during non-production periods.",
        assumptions=("Only qualifies when the contributing anomaly buckets occurred predominantly during a "
                     "non-production plant state - checked via the same production-context lookup Phase 8/9 use.",),
        exclusions=("Never claims a confirmed leak - compressor power legitimately tracks production air demand "
                     "when production IS running, which is exactly why this rule is scoped to non-production only.",
                     "Individual compressor airflow is not measured (only header-level flow) - no Nm3 figure is claimed."),
    ),
    _rule(
        rule_key="chiller_power_opportunity", source_anomaly_rule_key="chiller_power_deviation",
        equipment_type="chiller", category=CATEGORY_CHILLER,
        title_template="{instance} Power Above Expected - Investigation Opportunity",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        recommendation_template="Investigate why input power at {instance} is elevated at comparable cooling "
                                 "load/ambient conditions - review with the responsible engineer.",
        assumptions=("A co-occurring 'COP below expected' finding on the same instance/window, if present, is "
                     "attached as corroborating evidence only - never as the source of a dollar figure.",),
        exclusions=("Does not diagnose condenser fouling, refrigerant charge, or any other specific cause.",),
    ),
    _rule(
        rule_key="chwp_power_opportunity", source_anomaly_rule_key="pump_power_deviation",
        equipment_type="chilled_water_pump", category=CATEGORY_PUMP,
        title_template="{instance} Power Deviation - Investigation Opportunity",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        recommendation_template="Review pump speed/sequencing at {instance} against required flow - excess power "
                                 "at comparable flow may indicate an avoidable operating point.",
        assumptions=("Excludes the known-unrealistic pump hydraulic-efficiency KPI, carried forward from Phase 8/9.",),
        exclusions=("Does not diagnose impeller wear, cavitation, or any other specific mechanical cause.",),
    ),
    _rule(
        rule_key="wsp_power_opportunity", source_anomaly_rule_key="wsp_power_deviation",
        equipment_type="water_supply_pump", category=CATEGORY_PUMP,
        title_template="{instance} Power Deviation - Investigation Opportunity",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        recommendation_template="Review pump speed/sequencing at {instance} against required flow - excess power "
                                 "at comparable flow may indicate an avoidable operating point.",
        assumptions=("Excludes the known-unrealistic pump hydraulic-efficiency KPI, carried forward from Phase 8/9.",),
        exclusions=("Does not diagnose impeller wear, cavitation, or any other specific mechanical cause.",),
    ),
    _rule(
        rule_key="ahu_fan_power_opportunity", source_anomaly_rule_key="ahu_fan_power_deviation",
        equipment_type="ahu", category=CATEGORY_AHU,
        title_template="{instance} Fan Power Deviation - Investigation Opportunity",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        recommendation_template="Inspect {instance}'s filter condition and verify actual airflow requirement - "
                                 "do not assume a dirty filter without direct evidence.",
        assumptions=("A co-occurring filter-DP drift finding on the same instance, if present, is attached as "
                     "corroborating evidence only.",),
        exclusions=("Does not automatically diagnose a dirty filter or any other specific cause.",),
    ),
    _rule(
        rule_key="coldroom_compressor_opportunity", source_anomaly_rule_key="coldroom_compressor_power_deviation",
        equipment_type="cold_room", category=CATEGORY_COLD_ROOM,
        title_template="{instance} Compressor Duty Above Expected - Investigation Opportunity",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        recommendation_template="Review door-open frequency/duration and after-hours operation at {instance} - "
                                 "avoid treating legitimate product-loading activity as waste.",
        assumptions=("Phase 9's door-open grace period already prevents legitimate short recoveries from ever "
                     "becoming the source anomaly in the first place.",),
        exclusions=("Does not distinguish avoidable door-open behaviour from legitimate loading activity beyond "
                     "what Phase 9's grace period already filters.",),
    ),
    _rule(
        rule_key="production_motor_power_opportunity", source_anomaly_rule_key="production_motor_power_deviation",
        equipment_type="production_process", category=CATEGORY_PRODUCTION,
        title_template="{instance} Motor Power Above Expected - Investigation Opportunity",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        recommendation_template="Review {instance}'s operating point/load against the product currently running - "
                                 "excess power at a comparable product/state may warrant investigation.",
        assumptions=("Source anomaly's baseline is product-context-matched where history allows (Phase 8's "
                     "same-product > category > running-state fallback).",),
        exclusions=("Does not diagnose mechanical wear or process-setting causes.",),
    ),
    _rule(
        rule_key="filling_power_opportunity", source_anomaly_rule_key="filling_power_deviation",
        equipment_type="filling", category=CATEGORY_PRODUCTION,
        title_template="{instance} Power Above Expected - Investigation Opportunity",
        saving_basis=SAVING_BASIS_UNAVAILABLE, minimum_meaningful_impact_cost=1.0,
        recommendation_template="Review {instance}'s operating point against the product currently running.",
        assumptions=(),
        exclusions=("Does not diagnose mechanical wear or process-setting causes.",),
    ),
]

# Deliberately deferred, not built: an opportunity rule sourced from
# "compressed_air_specific_energy_drift" (Phase 9's one energy_relevant
# DRIFT rule). Drift rules never get a Phase-9-computed excess energy
# figure (interval integration doesn't apply to a point-in-time
# reference-vs-recent comparison) - a real opportunity here would need
# a NEW Nm3-throughput calculation to convert a kWh/Nm3 delta into an
# actual kWh figure, which hasn't been built or verified. Rather than
# ship unverified physics, this is left for a future rule addition.
DEFERRED_SOURCE_RULE_KEYS = ("compressed_air_specific_energy_drift",)


def get_rule(rule_key: str) -> OpportunityRule | None:
    for rule in OPPORTUNITY_RULES:
        if rule.rule_key == rule_key:
            return rule
    return None


def rules_for_source_anomaly_rule_key(source_anomaly_rule_key: str) -> list[OpportunityRule]:
    return [r for r in OPPORTUNITY_RULES if r.source_anomaly_rule_key == source_anomaly_rule_key]
