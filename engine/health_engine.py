from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from engine import health_evidence as ev
from engine.baseline_targets import EQUIPMENT_TYPE_PROFILES, PRODUCTION_PROCESS_AREA_CODES, _instances_for_area_code
from engine.health_targets import (
    CONFIDENCE_MULTIPLIER,
    COVERAGE_HIGH,
    COVERAGE_INSUFFICIENT,
    COVERAGE_LOW,
    COVERAGE_MEDIUM,
    COVERAGE_RATIO_HIGH_MIN,
    COVERAGE_RATIO_LOW_MIN,
    COVERAGE_RATIO_MEDIUM_MIN,
    EVENTS_LOOKBACK_DAYS,
    EVENTS_MAX_QUALIFYING_EVENTS_SCALED,
    EVENTS_PENALTY_PER_QUALIFYING_EVENT,
    FAMILY_MAX_PENALTY,
    FAMILY_VALUES,
    HEALTH_MODEL_VERSION,
    MAINTENANCE_OVERDUE_BASE_PENALTY,
    MAINTENANCE_OVERDUE_MAX_DAYS_SCALED,
    MAINTENANCE_OVERDUE_PER_DAY,
    PROVISIONAL_COVERAGE_STATUSES,
    RECENCY_DECAY_DAYS,
    RECURRENCE_MAX_MULTIPLIER,
    RECURRENCE_STEP,
    SEVERITY_BASE_PENALTY,
    UNAVAILABLE_COVERAGE_STATUSES,
    HealthFactorDefinition,
    SUPPORTED_EQUIPMENT_TYPES,
    factors_for_equipment_type,
    health_band_for_score,
)

"""
Phase 12.1 - Equipment Health deterministic scoring engine. Turns
engine.health_evidence's raw evidence into a HealthResult using ONLY
engine.health_targets' centralized registry/constants - no numeric
weight is defined in this file.

This is an ENGINEERING PRIORITIZATION TOOL:
  - Health Score answers "how much engineering attention does
    available evidence suggest?" (item 3).
  - Assessment Confidence/Coverage answers "how complete/trustworthy
    is this assessment?" - a SEPARATE output, never blended into the
    score itself (item 3).
  - No Remaining Useful Life, failure probability, or predicted
    failure date is ever computed (item 26).
  - No root-cause/named-fault diagnosis is ever produced - only
    evidence (item 27).
  - No LLM call anywhere in this module (item 28). No PLC/control
    write anywhere (item 29) - this module only reads.

CALCULATE-only, exactly like Phase 11.3's engine/savings_verification_engine.py
precedent: nothing in this module persists a result anywhere. Phase
12.1 deliberately adds NO new schema/table (item 25) - a worker/history
table is Phase 12.2's job, only if actually needed then.
"""


# ---------------------------------------------------------------------------
# Result contract (item 21)
# ---------------------------------------------------------------------------

@dataclass
class FactorResult:
    factor_id: str
    family: str
    status: str                 # "penalized" | "clean" | "missing"
    contribution: float         # penalty actually applied to the score (0 for clean/missing)
    max_penalty: float
    evidence_source: str        # "anomaly" | "maintenance" | "event"
    evidence_value: Any
    evidence_timestamp: str | None
    confidence: str | None
    reason: str
    provenance: str


@dataclass
class HealthResult:
    equipment_id: int | None
    plant_id: int
    plant_code: str
    instance_key: str
    equipment_type: str
    health_model_version: str
    computed_at: str
    health_score: float | None            # None means Unavailable (item 19)
    health_band: str | None               # None when health_score is None
    provisional: bool                     # True for LOW-coverage numeric scores
    assessment_confidence: str            # HIGH | MEDIUM | LOW | INSUFFICIENT
    coverage_status: str                  # same vocabulary as assessment_confidence (item 18)
    applicable_factor_count: int
    usable_factor_count: int
    factor_results: list[FactorResult] = field(default_factory=list)
    missing_factors: list[str] = field(default_factory=list)
    criticality: str | None = None        # informational only - NEVER alters health_score (item 16)
    limitations: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Target discovery (mirrors engine.baseline_targets.discover_targets()'s
# own instance-discovery loop exactly, restricted to the equipment types
# health_targets.SUPPORTED_EQUIPMENT_TYPES actually covers - item 30:
# equipment-level only, never factory/plant/area/system).
# ---------------------------------------------------------------------------

def discover_health_targets(config_database_path: str | Path, plant_code: str) -> list[tuple[str, str]]:
    """Returns (equipment_type, instance_key) pairs for every real,
    currently-enabled equipment instance of a supported type."""
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        pairs: list[tuple[str, str]] = []
        for equipment_type in SUPPORTED_EQUIPMENT_TYPES:
            profile = EQUIPMENT_TYPE_PROFILES.get(equipment_type)
            if profile is None:
                continue
            area_codes = PRODUCTION_PROCESS_AREA_CODES if equipment_type == "production_process" else (profile.area_code,)
            instances: list[str] = []
            for area_code in area_codes:
                instances.extend(_instances_for_area_code(connection, plant_code, area_code))
            for instance_key in instances:
                pairs.append((equipment_type, instance_key))
        return pairs
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Per-factor penalty computation (items 10, 11, 12, 13)
# ---------------------------------------------------------------------------

def _recency_multiplier(evidence: dict[str, Any], now: datetime) -> float:
    if evidence["status"] == "OPEN":
        return 1.0
    resolved_at = evidence.get("resolved_at")
    if not resolved_at:
        return 1.0
    try:
        resolved = datetime.strptime(resolved_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 1.0
    days_since = (now - resolved).total_seconds() / 86400.0
    if days_since <= 0:
        return 1.0
    if days_since >= RECENCY_DECAY_DAYS:
        return 0.0
    return max(0.0, 1.0 - (days_since / RECENCY_DECAY_DAYS))


def _recurrence_multiplier(occurrence_count: int) -> float:
    return min(1.0 + RECURRENCE_STEP * max(0, occurrence_count), RECURRENCE_MAX_MULTIPLIER)


def _score_anomaly_factor(
    factor_def: HealthFactorDefinition, config_database_path: str | Path, plant_id: int, instance_key: str,
    baseline_target_key: str, now: datetime,
) -> FactorResult:
    evidence = ev.fetch_anomaly_evidence(config_database_path, plant_id, instance_key, baseline_target_key, factor_def.source_rule_key)

    if evidence is None:
        # No OPEN/RESOLVED anomaly row exists at all for this rule. Only
        # trustworthy as "confirmed clean" if a mature baseline actually
        # exists to have detected one (item 19) - otherwise it's missing
        # evidence, not health evidence.
        if ev.has_mature_baseline(config_database_path, instance_key, baseline_target_key):
            return FactorResult(
                factor_id=factor_def.factor_id, family=factor_def.family, status="clean", contribution=0.0,
                max_penalty=factor_def.max_penalty, evidence_source="anomaly", evidence_value=None,
                evidence_timestamp=None, confidence=None, provenance=factor_def.provenance,
                reason="No open or recent anomaly against a mature baseline - no current evidence of a problem.",
            )
        return FactorResult(
            factor_id=factor_def.factor_id, family=factor_def.family, status="missing", contribution=0.0,
            max_penalty=factor_def.max_penalty, evidence_source="anomaly", evidence_value=None,
            evidence_timestamp=None, confidence=None, provenance=factor_def.provenance,
            reason="No mature baseline yet for this measurement - insufficient history to evaluate this factor.",
        )

    confidence = evidence["confidence"]
    confidence_multiplier = CONFIDENCE_MULTIPLIER.get(confidence)
    if confidence_multiplier is None:
        # Statistically insufficient confidence - never used as health evidence (item 13).
        return FactorResult(
            factor_id=factor_def.factor_id, family=factor_def.family, status="missing", contribution=0.0,
            max_penalty=factor_def.max_penalty, evidence_source="anomaly", evidence_value=evidence.get("actual_value"),
            evidence_timestamp=evidence.get("last_seen"), confidence=confidence, provenance=factor_def.provenance,
            reason=f"Anomaly evidence exists but its statistical confidence ({confidence!r}) is too weak to use.",
        )

    base_penalty = SEVERITY_BASE_PENALTY.get(evidence["severity"], 0.0)
    recency = _recency_multiplier(evidence, now)
    recurrence = _recurrence_multiplier(evidence["occurrence_count"])
    raw = base_penalty * confidence_multiplier * recency * recurrence
    contribution = min(raw, factor_def.max_penalty)

    if contribution <= 0.0:
        return FactorResult(
            factor_id=factor_def.factor_id, family=factor_def.family, status="clean", contribution=0.0,
            max_penalty=factor_def.max_penalty, evidence_source="anomaly", evidence_value=evidence.get("actual_value"),
            evidence_timestamp=evidence.get("last_seen") or evidence.get("resolved_at"), confidence=confidence,
            provenance=factor_def.provenance,
            reason="A past finding exists but has fully decayed (long-resolved) - no current contribution.",
        )

    status_word = "an open" if evidence["status"] == "OPEN" else "a recently resolved"
    return FactorResult(
        factor_id=factor_def.factor_id, family=factor_def.family, status="penalized", contribution=round(contribution, 2),
        max_penalty=factor_def.max_penalty, evidence_source="anomaly", evidence_value=evidence.get("actual_value"),
        evidence_timestamp=evidence.get("last_seen") or evidence.get("resolved_at"), confidence=confidence,
        provenance=factor_def.provenance,
        reason=(
            f"{evidence['severity'].title()} severity from {status_word} anomaly "
            f"(occurrence #{evidence['occurrence_count'] + 1} for this factor, {confidence} statistical confidence)."
        ),
    )


def _score_maintenance_factor(
    factor_def: HealthFactorDefinition, config_database_path: str | Path, equipment_id: int | None, now: datetime,
) -> FactorResult:
    evidence = ev.fetch_maintenance_evidence(config_database_path, equipment_id, now)
    if not evidence["has_schedule_data"]:
        return FactorResult(
            factor_id=factor_def.factor_id, family=factor_def.family, status="missing", contribution=0.0,
            max_penalty=factor_def.max_penalty, evidence_source="maintenance", evidence_value=None,
            evidence_timestamp=None, confidence=None, provenance=factor_def.provenance,
            reason="No maintenance schedule data configured for this equipment - not fabricated, simply unavailable.",
        )

    days_overdue = evidence["days_overdue"]
    if days_overdue <= 0:
        return FactorResult(
            factor_id=factor_def.factor_id, family=factor_def.family, status="clean", contribution=0.0,
            max_penalty=factor_def.max_penalty, evidence_source="maintenance", evidence_value=evidence["next_due"],
            evidence_timestamp=evidence["next_due"], confidence=None, provenance=factor_def.provenance,
            reason=f"Maintenance is on schedule (next due {evidence['next_due']}).",
        )

    scaled_days = min(days_overdue, MAINTENANCE_OVERDUE_MAX_DAYS_SCALED)
    raw = MAINTENANCE_OVERDUE_BASE_PENALTY + MAINTENANCE_OVERDUE_PER_DAY * scaled_days
    contribution = round(min(raw, factor_def.max_penalty), 2)
    return FactorResult(
        factor_id=factor_def.factor_id, family=factor_def.family, status="penalized", contribution=contribution,
        max_penalty=factor_def.max_penalty, evidence_source="maintenance", evidence_value=evidence["next_due"],
        evidence_timestamp=evidence["next_due"], confidence=None, provenance=factor_def.provenance,
        reason=f"Maintenance overdue by {days_overdue} day(s) (was due {evidence['next_due']}) - a risk/attention factor, not evidence of physical degradation.",
    )


def _score_events_factor(
    factor_def: HealthFactorDefinition, machine_database_path: str | Path, instance_key: str, now: datetime, feed_fresh: bool,
) -> FactorResult:
    if not feed_fresh:
        return FactorResult(
            factor_id=factor_def.factor_id, family=factor_def.family, status="missing", contribution=0.0,
            max_penalty=factor_def.max_penalty, evidence_source="event", evidence_value=None,
            evidence_timestamp=None, confidence=None, provenance=factor_def.provenance,
            reason="The engineering-event feed itself has produced no recent rows system-wide - a data quality gap, not confirmed-clean equipment evidence.",
        )

    evidence = ev.fetch_events_evidence(machine_database_path, instance_key, now)
    count = evidence["qualifying_event_count"]
    if count <= 0:
        return FactorResult(
            factor_id=factor_def.factor_id, family=factor_def.family, status="clean", contribution=0.0,
            max_penalty=factor_def.max_penalty, evidence_source="event", evidence_value=0,
            evidence_timestamp=None, confidence=None, provenance=factor_def.provenance,
            reason="No qualifying alarm/warning events for this equipment in the recent window.",
        )

    scaled_count = min(count, EVENTS_MAX_QUALIFYING_EVENTS_SCALED)
    raw = EVENTS_PENALTY_PER_QUALIFYING_EVENT * scaled_count
    contribution = round(min(raw, factor_def.max_penalty), 2)
    return FactorResult(
        factor_id=factor_def.factor_id, family=factor_def.family, status="penalized", contribution=contribution,
        max_penalty=factor_def.max_penalty, evidence_source="event", evidence_value=count,
        evidence_timestamp=evidence["latest_event_time"], confidence=None, provenance=factor_def.provenance,
        reason=f"{count} qualifying alarm/warning event(s) for this equipment's own tags in the last {EVENTS_LOOKBACK_DAYS} day(s).",
    )


# ---------------------------------------------------------------------------
# Family capping (item 9) - the core double-count prevention mechanism.
# When a family's raw total exceeds its cap, every penalized factor in
# that family is scaled down PROPORTIONALLY so the displayed
# contributions still sum exactly to what was actually deducted from
# the score - "the final score must be reconstructable from its
# components" (item 5) would otherwise be false the moment a cap
# actually engages (each factor would still show its pre-cap value
# while the score reflected a smaller total).
# ---------------------------------------------------------------------------

def _apply_family_caps(factor_results: list[FactorResult]) -> float:
    family_totals: dict[str, float] = {family: 0.0 for family in FAMILY_VALUES}
    for result in factor_results:
        family_totals[result.family] += result.contribution

    total_penalty = 0.0
    for family, raw_total in family_totals.items():
        cap = FAMILY_MAX_PENALTY[family]
        if raw_total <= cap or raw_total <= 0:
            total_penalty += raw_total
            continue
        scale = cap / raw_total
        for result in factor_results:
            if result.family == family and result.contribution > 0:
                result.contribution = round(result.contribution * scale, 2)
        total_penalty += cap
    return total_penalty


# ---------------------------------------------------------------------------
# Coverage / assessment confidence (item 18)
# ---------------------------------------------------------------------------

_CONFIDENCE_RANK = {"High": 3, "Medium": 2, "Low": 1}


def _coverage_status(factor_results: list[FactorResult], applicable_count: int) -> str:
    usable = [r for r in factor_results if r.status != "missing"]
    if applicable_count == 0:
        return COVERAGE_INSUFFICIENT

    ratio = len(usable) / applicable_count
    if ratio < COVERAGE_RATIO_LOW_MIN:
        return COVERAGE_INSUFFICIENT

    # Cap the ratio-based tier down when the usable evidence itself is
    # statistically weak - a numerically-complete-looking assessment
    # built entirely on Low-confidence baselines is never HIGH (item 13).
    statistical = [r for r in usable if r.confidence is not None]
    if statistical:
        worst_rank = min(_CONFIDENCE_RANK.get(r.confidence, 1) for r in statistical)
    else:
        worst_rank = 3  # no statistical (anomaly-sourced) evidence used at all - deterministic-only, unconstrained

    if ratio >= COVERAGE_RATIO_HIGH_MIN:
        ratio_tier = COVERAGE_HIGH
    elif ratio >= COVERAGE_RATIO_MEDIUM_MIN:
        ratio_tier = COVERAGE_MEDIUM
    else:
        ratio_tier = COVERAGE_LOW

    confidence_tier = {3: COVERAGE_HIGH, 2: COVERAGE_MEDIUM, 1: COVERAGE_LOW}[worst_rank]

    tier_rank = {COVERAGE_HIGH: 3, COVERAGE_MEDIUM: 2, COVERAGE_LOW: 1, COVERAGE_INSUFFICIENT: 0}
    return min(ratio_tier, confidence_tier, key=lambda t: tier_rank[t])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def calculate_health(
    config_database_path: str | Path, machine_database_path: str | Path, plant_id: int, plant_code: str,
    equipment_type: str, instance_key: str, now: datetime | None = None,
) -> HealthResult:
    now = now or datetime.now()
    computed_at = now.strftime("%Y-%m-%d %H:%M:%S")

    factor_defs = factors_for_equipment_type(equipment_type)
    equipment_id = ev.equipment_id_for_instance(config_database_path, instance_key)
    criticality = ev.equipment_criticality(config_database_path, equipment_id)  # informational only (item 16)

    anomaly_fresh = ev.anomaly_feed_is_fresh(config_database_path, now)
    events_fresh = ev.events_feed_is_fresh(machine_database_path, now)

    factor_results: list[FactorResult] = []
    limitations: list[str] = []
    if not anomaly_fresh:
        limitations.append("Anomaly detection feed has not produced a recent update - anomaly-sourced factors may be stale.")
    if not events_fresh:
        limitations.append("Engineering event feed has not produced a recent update - event-sourced factors are unavailable.")

    for factor_def in factor_defs:
        if factor_def.source_type == "anomaly":
            if not anomaly_fresh:
                factor_results.append(FactorResult(
                    factor_id=factor_def.factor_id, family=factor_def.family, status="missing", contribution=0.0,
                    max_penalty=factor_def.max_penalty, evidence_source="anomaly", evidence_value=None,
                    evidence_timestamp=None, confidence=None, provenance=factor_def.provenance,
                    reason="Anomaly detection feed is stale system-wide - this factor cannot be evaluated right now.",
                ))
                continue
            baseline_target_key = _baseline_target_key_for_rule(factor_def.source_rule_key)
            factor_results.append(_score_anomaly_factor(
                factor_def, config_database_path, plant_id, instance_key, baseline_target_key, now,
            ))
        elif factor_def.source_type == "maintenance":
            factor_results.append(_score_maintenance_factor(factor_def, config_database_path, equipment_id, now))
        elif factor_def.source_type == "event":
            factor_results.append(_score_events_factor(factor_def, machine_database_path, instance_key, now, events_fresh))

    total_penalty = _apply_family_caps(factor_results)
    raw_score = 100.0 - total_penalty
    clamped_score = max(0.0, min(100.0, raw_score))

    applicable_count = len(factor_defs)
    usable_count = sum(1 for r in factor_results if r.status != "missing")
    missing_factors = [r.factor_id for r in factor_results if r.status == "missing"]

    coverage_status = _coverage_status(factor_results, applicable_count)
    provisional = coverage_status in PROVISIONAL_COVERAGE_STATUSES
    unavailable = coverage_status in UNAVAILABLE_COVERAGE_STATUSES

    health_score = None if unavailable else round(clamped_score, 1)
    health_band = None if unavailable else health_band_for_score(health_score)

    if unavailable:
        limitations.append(f"Only {usable_count} of {applicable_count} applicable factors have usable evidence - too few to support a numeric score.")

    return HealthResult(
        equipment_id=equipment_id, plant_id=plant_id, plant_code=plant_code, instance_key=instance_key,
        equipment_type=equipment_type, health_model_version=HEALTH_MODEL_VERSION, computed_at=computed_at,
        health_score=health_score, health_band=health_band, provisional=provisional,
        assessment_confidence=coverage_status, coverage_status=coverage_status,
        applicable_factor_count=applicable_count, usable_factor_count=usable_count,
        factor_results=factor_results, missing_factors=missing_factors, criticality=criticality,
        limitations=limitations,
        assumptions=[
            "Health Score is an engineering prioritization signal derived from existing anomaly/maintenance/event "
            "evidence - not proof of impending failure, not Remaining Useful Life, not a safety classification.",
            f"Health scoring methodology version {HEALTH_MODEL_VERSION} - all weights are SIMULATION_TUNING "
            "engineering judgment for this dev-stage system, not manufacturer limits.",
        ],
    )


def _baseline_target_key_for_rule(rule_key: str) -> str:
    """The AnomalyRule.baseline_target_key for a given rule_key - reuses
    Phase 9's own registry rather than duplicating the mapping."""
    from engine.anomaly_targets import get_rule
    rule = get_rule(rule_key)
    if rule is None:
        raise ValueError(f"Unknown anomaly rule_key referenced by health registry: {rule_key!r}")
    return rule.baseline_target_key


def calculate_all(config_database_path: str | Path, machine_database_path: str | Path, plant_code: str, now: datetime | None = None) -> list[HealthResult]:
    connection = sqlite3.connect(config_database_path)
    try:
        row = connection.execute("SELECT id FROM plants WHERE code = ?", (plant_code,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return []
    plant_id = row[0]

    targets = discover_health_targets(config_database_path, plant_code)
    results = []
    for equipment_type, instance_key in targets:
        results.append(calculate_health(
            config_database_path, machine_database_path, plant_id, plant_code, equipment_type, instance_key, now=now,
        ))
    return results


# ---------------------------------------------------------------------------
# Debug CLI - read-only, mirrors the project's established debug-CLI
# convention (engine/anomaly_engine.py::main(), etc.). No UI (item 40).
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    from config.environment import get_config_db_path, get_machine_db_path

    parser = argparse.ArgumentParser(description="Phase 12.1 Equipment Health - read-only debug CLI")
    parser.add_argument("--plant", default="p01")
    parser.add_argument("--instance", default=None, help="e.g. P01.WATER.WSP01 - if omitted, scores every eligible instance")
    args = parser.parse_args()

    config_db = get_config_db_path()
    machine_db = get_machine_db_path()

    if args.instance:
        targets = [(t, i) for t, i in discover_health_targets(config_db, args.plant) if i == args.instance]
    else:
        targets = discover_health_targets(config_db, args.plant)

    connection = sqlite3.connect(config_db)
    plant_row = connection.execute("SELECT id FROM plants WHERE code = ?", (args.plant,)).fetchone()
    connection.close()
    if plant_row is None:
        print(f"Unknown plant code: {args.plant}")
        return
    plant_id = plant_row[0]

    for equipment_type, instance_key in targets:
        result = calculate_health(config_db, machine_db, plant_id, args.plant, equipment_type, instance_key)
        score_text = "Unavailable" if result.health_score is None else f"{result.health_score} ({result.health_band}{' - provisional' if result.provisional else ''})"
        print(f"\n{instance_key} [{equipment_type}]  Score: {score_text}  Confidence: {result.assessment_confidence}  "
              f"Usable: {result.usable_factor_count}/{result.applicable_factor_count}")
        for factor in result.factor_results:
            if factor.status == "penalized":
                print(f"    -{factor.contribution:>5.1f}  {factor.factor_id:<30} {factor.reason}")
        if result.missing_factors:
            print(f"    missing: {', '.join(result.missing_factors)}")


if __name__ == "__main__":
    main()
