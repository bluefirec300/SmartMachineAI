from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from engine import health_domain as dom
from engine import health_evidence as ev
from engine import health_history as hist
from engine.health_engine import discover_health_targets
from engine.health_targets import FAMILY_ELECTRICAL_LOAD
from engine.maintenance_intelligence_targets import (
    CONDITION_PERFORMANCE_PROCESS_CONTRIBUTION_MAX,
    CONDITION_PERFORMANCE_PROCESS_DIVISOR,
    CONDITION_PERFORMANCE_PROCESS_FAMILIES,
    CONFIDENCE_HIGH,
    CONFIDENCE_INSUFFICIENT,
    CONFIDENCE_RANK,
    CRITICALITY_CONTRIBUTION,
    CRITICALITY_CONTRIBUTION_MAX,
    DETERMINISTIC_DIMENSION_CONFIDENCE,
    EVENTS_CONTRIBUTION_MAX,
    EVENTS_CONTRIBUTION_MULTIPLIER,
    HEALTH_BAND_CONTRIBUTION,
    HEALTH_BAND_CONTRIBUTION_MAX,
    MAINTENANCE_CONTRIBUTION_MAX,
    MAINTENANCE_CONTRIBUTION_MULTIPLIER,
    MAINTENANCE_INTELLIGENCE_MODEL_VERSION,
    MAINTENANCE_OVERDUE_FLOOR_PRIORITY,
    MAINTENANCE_OVERDUE_FLOOR_REASON,
    PRIORITY_NOT_ASSESSED,
    PRIORITY_PROVENANCE,
    PRIORITY_SORT_RANK,
    RECENT_MOVEMENT_CONTRIBUTION,
    RECENT_MOVEMENT_CONTRIBUTION_MAX,
    priority_band_for_score,
    recommended_check_for_factor,
)

"""
Phase 13 - Maintenance Intelligence deterministic engine. Read-only /
on-demand, exactly per the approved plan and addendum:
  - NO historian rescan, NO direct anomaly/event reinterpretation, NO
    duplicate health calculation - this module NEVER calls
    engine.health_engine.calculate_health(). It reads Phase 12's
    ALREADY-PERSISTED equipment_health_snapshots /
    equipment_health_factor_snapshots rows (via engine.health_domain /
    engine.health_history) as its evidence input.
  - The only OTHER read this module performs is
    engine.health_evidence.fetch_maintenance_evidence() - the same
    lightweight, already-existing evidence-fetch (no scoring math) Phase
    12.1 itself uses, reused here purely to surface next_due/days_overdue
    for display and for the overdue-confirmation gate (Correction 1/2) -
    never to recompute a penalty (the maintenance penalty ITSELF is
    reused 1:1 from the persisted factor snapshot, never re-derived).
  - ELECTRICAL_LOAD-family factors are excluded everywhere - that
    evidence belongs to Energy Opportunities (Phase 10) only.
  - No LLM, no prediction, no RUL, no automatic work orders, no
    automatic maintenance-interval changes anywhere in this module.

Addendum corrections implemented here:
  - CORRECTION 1: the overdue-maintenance REVIEW floor.
  - CORRECTION 2: NOT_ASSESSED when no dimension was assessed at all.
  - CORRECTION 3: confidence aggregated only over dimensions that
    materially supported the resulting recommendation.

Final semantic correction (this pass) - TWO DELIBERATELY SEPARATE
concepts, previously collapsed into one `contributing` list:
  - `assessed_evidence` - was there enough evidence to make ANY
    assessment at all? Drives ROUTINE vs NOT_ASSESSED ONLY. A real,
    adequately-covered Health assessment counts here even when its own
    penalty contribution is exactly zero ("assessed, clean" must never
    look identical to "never assessed").
  - `recommendation_contributors` - which of those assessed dimensions
    actually caused or materially supported THIS PARTICULAR
    recommendation? Drives `confidence_basis`/`recommendation_confidence`
    ONLY. health_condition counts here whenever it took the ordinary
    score-based path (its assessedness IS part of why that path landed
    where it did, even at a zero penalty) OR whenever it produced a
    real nonzero contribution - but NOT when a confirmed-overdue floor
    is the sole reason for the recommendation and health's own
    contribution was zero (the floor, not health, is what caused that
    recommendation - health's unrelated confidence must not leak in).
"""


@dataclass
class PriorityFactor:
    dimension: str
    contribution: float
    max_contribution: float | None
    source_factor_id: str | None
    reason: str
    provenance: str


@dataclass
class MaintenancePriorityResult:
    equipment_id: int | None
    plant_id: int
    plant_code: str
    instance_key: str
    equipment_type: str
    maintenance_priority: str
    priority_score: float
    floor_applied: bool
    floor_reason: str | None
    recommendation_confidence: str
    confidence_basis: list[str]
    health_score: float | None
    health_band: str | None
    health_confidence: str
    health_provisional: bool
    recent_movement: str
    criticality: str | None
    maintenance_status: dict[str, Any]
    # Phase 15 (read-only, additive) - exposes the assessed_evidence local
    # variable that already existed inside calculate_maintenance_priority()
    # (see the module docstring's "Final semantic correction" section) as
    # a field on the result, so a caller can distinguish "no evidence was
    # assessed at all" (NOT_ASSESSED) from "evidence was assessed but did
    # not drive this particular recommendation" (assessed_evidence contains
    # a dimension that confidence_basis does not). Purely exposition of an
    # already-computed value - does NOT change maintenance_priority,
    # priority_score, recommendation_confidence, confidence_basis, or any
    # other calculation/persistence semantics.
    assessed_evidence: list[str] = field(default_factory=list)
    priority_factors: list[PriorityFactor] = field(default_factory=list)
    recommended_checks: list[str] = field(default_factory=list)
    maintenance_context: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    model_version: str = MAINTENANCE_INTELLIGENCE_MODEL_VERSION
    generated_at: str = ""


def _maintenance_context(config_database_path: str | Path, equipment_id: int | None) -> dict[str, Any]:
    """Structured only (never free-text-parsed) - last_serviced_at plus a
    count of maintenance_log rows per category, mirroring the project's
    own preference for structured evidence over prose."""
    if equipment_id is None:
        return {"last_serviced_at": None, "recent_log_count_by_category": {}}
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        equipment_row = connection.execute(
            "SELECT last_serviced_at FROM equipment WHERE id = ?", (equipment_id,)
        ).fetchone()
        category_rows = connection.execute(
            "SELECT category, COUNT(*) AS n FROM maintenance_log WHERE equipment_id = ? GROUP BY category",
            (equipment_id,),
        ).fetchall()
    finally:
        connection.close()
    return {
        "last_serviced_at": equipment_row["last_serviced_at"] if equipment_row else None,
        "recent_log_count_by_category": {r["category"]: r["n"] for r in category_rows},
    }


def _condition_performance_process_factors(factor_snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        f for f in factor_snapshots
        if f["factor_family"] in CONDITION_PERFORMANCE_PROCESS_FAMILIES and f["status"] == "penalized"
    ]


def _events_factors(factor_snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in factor_snapshots if f["factor_family"] == "EVENTS" and f["status"] == "penalized"]


def _maintenance_factors(factor_snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in factor_snapshots if f["factor_family"] == "MAINTENANCE" and f["status"] == "penalized"]


def _recommended_checks(factor_snapshots: list[dict[str, Any]], overdue_confirmed: bool) -> list[str]:
    """Deduplicated, curated checks only - never a check for
    ELECTRICAL_LOAD evidence (Energy Opportunity territory, not
    Maintenance Intelligence)."""
    checks: list[str] = []
    for f in factor_snapshots:
        if f["status"] != "penalized" or f["factor_family"] == FAMILY_ELECTRICAL_LOAD:
            continue
        text = recommended_check_for_factor(f["factor_id"], f["factor_family"]).text
        if text not in checks:
            checks.append(text)
    if overdue_confirmed:
        from engine.maintenance_intelligence_targets import GENERIC_CHECK_BY_FAMILY
        overdue_check = GENERIC_CHECK_BY_FAMILY["MAINTENANCE"].text
        if overdue_check not in checks:
            checks.append(overdue_check)
    return checks


def calculate_maintenance_priority(
    config_database_path: str | Path, plant_id: int, plant_code: str, equipment_type: str, instance_key: str,
    now: datetime | None = None,
) -> MaintenancePriorityResult:
    now = now or datetime.now()
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S")

    equipment_id = ev.equipment_id_for_instance(config_database_path, instance_key)
    criticality = ev.equipment_criticality(config_database_path, equipment_id)

    latest_snapshot = dom.get_latest_snapshot(config_database_path, plant_id, instance_key)
    factor_snapshots = dom.get_factor_snapshots(config_database_path, latest_snapshot["id"]) if latest_snapshot else []

    if latest_snapshot is not None:
        health_score = latest_snapshot["health_score"]
        health_band = latest_snapshot["health_band"]
        health_confidence = latest_snapshot["assessment_confidence"]
        health_provisional = bool(latest_snapshot["provisional"])
    else:
        health_score = None
        health_band = None
        # No Equipment Health assessment has ever been persisted for
        # this equipment - honestly treated as zero available evidence,
        # never a fabricated confidence tier (item 16's "never fabricate"
        # discipline, applied to Phase 13's own inputs).
        health_confidence = CONFIDENCE_INSUFFICIENT
        health_provisional = False

    trend = hist.compute_trend(config_database_path, plant_id, instance_key, now=now)
    recent_movement = trend["direction"]

    maintenance_evidence = ev.fetch_maintenance_evidence(config_database_path, equipment_id, now)
    overdue_confirmed = bool(maintenance_evidence["has_schedule_data"] and (maintenance_evidence["days_overdue"] or 0) > 0)

    # --- Contribution assembly - every number below is reconstructable
    # from a persisted Phase 12 factor snapshot or a lightweight, already
    # -existing evidence read. Nothing here recomputes a Phase 12 penalty. ---
    health_evidence_usable = health_score is not None
    health_band_contribution = HEALTH_BAND_CONTRIBUTION.get(health_band, 0.0) if health_band else 0.0

    condition_factors = _condition_performance_process_factors(factor_snapshots)
    condition_penalty_sum = round(sum(f["penalty"] for f in condition_factors), 2)
    # Gated on health_evidence_usable, not just "is there a persisted
    # factor row" - if Phase 12 itself judged overall coverage too low
    # to publish a numeric Health Score, a partial factor penalty from
    # that same insufficient assessment is not cherry-picked as
    # trustworthy condition evidence here either (Correction 2's "no
    # evidence != healthy" principle, applied consistently to Phase 13's
    # own inputs).
    condition_contribution = round(condition_penalty_sum / CONDITION_PERFORMANCE_PROCESS_DIVISOR, 2) if health_evidence_usable else 0.0

    events_factors_penalized = _events_factors(factor_snapshots)
    events_penalty_sum = round(sum(f["penalty"] for f in events_factors_penalized), 2)
    events_contribution = round(events_penalty_sum * EVENTS_CONTRIBUTION_MULTIPLIER, 2)

    maintenance_factors_penalized = _maintenance_factors(factor_snapshots)
    maintenance_penalty_sum = round(sum(f["penalty"] for f in maintenance_factors_penalized), 2)
    maintenance_contribution = round(maintenance_penalty_sum * MAINTENANCE_CONTRIBUTION_MULTIPLIER, 2)

    movement_contribution = RECENT_MOVEMENT_CONTRIBUTION.get(recent_movement, 0.0)

    criticality_contribution = CRITICALITY_CONTRIBUTION.get(criticality, 0.0) if criticality else 0.0

    raw_score = (
        health_band_contribution + condition_contribution + events_contribution
        + maintenance_contribution + movement_contribution + criticality_contribution
    )
    priority_score = round(max(0.0, min(100.0, raw_score)), 1)

    # --- assessed_evidence (CORRECTION 2) - "was there enough evidence
    # to make ANY assessment at all?" Drives ROUTINE vs NOT_ASSESSED
    # ONLY - never used for confidence below. `overdue_confirmed` is the
    # SAME fact that drives the floor further down - independent of
    # whether a persisted factor snapshot exists. A real, adequately-
    # covered Health assessment counts here regardless of whether its
    # verdict happens to be a zero penalty - "adequate evidence + no
    # concern" (ROUTINE) must never be indistinguishable from "no
    # evidence at all" (NOT_ASSESSED). Gating on health_band_contribution/
    # condition_contribution being > 0 would incorrectly send a
    # genuinely HEALTHY, cleanly-assessed equipment to NOT_ASSESSED
    # merely because its penalty contributions are zero.
    assessed_evidence: list[str] = []
    if overdue_confirmed:
        assessed_evidence.append("maintenance_overdue")
    if health_evidence_usable:
        assessed_evidence.append("health_condition")
    if events_contribution > 0:
        assessed_evidence.append("events")
    if movement_contribution != 0 and health_confidence:
        assessed_evidence.append("recent_movement")
    if criticality_contribution > 0:
        assessed_evidence.append("criticality")

    limitations: list[str] = []
    floor_applied = False
    floor_reason: str | None = None

    if not assessed_evidence:
        # CORRECTION 2 - no assessed evidence at all (neither health/
        # condition, events, movement, criticality, NOR a confirmed
        # overdue schedule) - NOT_ASSESSED, never ROUTINE. Distinct from
        # "adequate evidence assessed as no concern" (see docstring of
        # engine.maintenance_intelligence_targets.PRIORITY_NOT_ASSESSED).
        maintenance_priority = PRIORITY_NOT_ASSESSED
        recommendation_confidence = CONFIDENCE_INSUFFICIENT
        confidence_basis: list[str] = []
        limitations.append("Condition assessment is limited by insufficient Equipment Health evidence.")
    else:
        computed_band = priority_band_for_score(priority_score)
        # CORRECTION 1 - confirmed overdue maintenance floors the
        # priority at REVIEW. Only overrides when the normally-computed
        # band would otherwise be BELOW the floor - never lowers a band
        # that already independently reached REVIEW or higher, and never
        # touches priority_score itself (reported unfloored above).
        if overdue_confirmed and PRIORITY_SORT_RANK[computed_band] < PRIORITY_SORT_RANK[MAINTENANCE_OVERDUE_FLOOR_PRIORITY]:
            maintenance_priority = MAINTENANCE_OVERDUE_FLOOR_PRIORITY
            floor_applied = True
            floor_reason = MAINTENANCE_OVERDUE_FLOOR_REASON
        else:
            maintenance_priority = computed_band

        # --- recommendation_contributors (CORRECTION 3, final pass) -
        # "which of the ASSESSED dimensions actually caused or materially
        # supported THIS recommendation?" Drives confidence ONLY, built
        # AFTER floor_applied is known so health/condition's own gate can
        # tell the two cases apart: the ordinary score-based path (where
        # its assessedness - even a zero penalty - is genuinely part of
        # why that path landed where it did) vs. a confirmed-overdue
        # floor that alone produced the recommendation while health
        # contributed (rounded) nothing to it. events/maintenance_overdue/
        # recent_movement/criticality are unchanged from before - each
        # already gated on real materiality (a nonzero rounded
        # contribution, or - for maintenance_overdue - the independent
        # deterministic overdue fact itself), never on assessedness. ---
        recommendation_contributors: list[tuple[str, str]] = []
        if overdue_confirmed:
            recommendation_contributors.append(("maintenance_overdue", DETERMINISTIC_DIMENSION_CONFIDENCE))
        if health_evidence_usable and (not floor_applied or health_band_contribution > 0 or condition_contribution > 0):
            # health_evidence_usable alone (assessed) is NOT enough here -
            # see module docstring. The exact rule: health/condition
            # participates in confidence whenever the ordinary score path
            # determined the recommendation (not floor_applied), OR
            # whenever it produced a real, already-rounded nonzero
            # contribution to priority_score even under a floor. It is
            # excluded ONLY when a floor is the sole reason for the
            # recommendation and health's own rounded contribution is
            # exactly zero - precisely the confirmed bug case.
            recommendation_contributors.append(("health_condition", health_confidence))
        if events_contribution > 0:
            recommendation_contributors.append(("events", DETERMINISTIC_DIMENSION_CONFIDENCE))
        if movement_contribution != 0 and health_confidence:
            recommendation_contributors.append(("recent_movement", health_confidence))
        if criticality_contribution > 0:
            recommendation_contributors.append(("criticality", DETERMINISTIC_DIMENSION_CONFIDENCE))

        if not recommendation_contributors:
            # Structurally unreachable today (floor_applied implies
            # overdue_confirmed implies maintenance_overdue is always
            # present; a non-floor assessed_evidence-non-empty case
            # always includes at least the dimension that made it
            # non-empty) - kept as an explicit, honest safety net rather
            # than a silent crash if that invariant is ever broken by a
            # future change.
            confidence_basis = []
            recommendation_confidence = CONFIDENCE_INSUFFICIENT
        else:
            confidence_basis = [dimension for dimension, _confidence in recommendation_contributors]
            recommendation_confidence = min(
                (confidence for _dimension, confidence in recommendation_contributors), key=lambda c: CONFIDENCE_RANK[c],
            )

    if health_score is None:
        limitations.append("No numeric Equipment Health score is currently available for this equipment.")

    priority_factors = [
        PriorityFactor(
            dimension="health_band", contribution=health_band_contribution, max_contribution=HEALTH_BAND_CONTRIBUTION_MAX,
            source_factor_id=None, provenance=PRIORITY_PROVENANCE,
            reason=f"Equipment Health band is {health_band or 'not assessed'} (contributes {health_band_contribution:.1f}).",
        ),
        PriorityFactor(
            dimension="condition_performance_process", contribution=condition_contribution,
            max_contribution=CONDITION_PERFORMANCE_PROCESS_CONTRIBUTION_MAX, source_factor_id=None,
            provenance=PRIORITY_PROVENANCE,
            reason=(
                f"{len(condition_factors)} condition/performance/process factor(s) penalized in the underlying Health "
                f"assessment, summed {condition_penalty_sum:.2f} / {CONDITION_PERFORMANCE_PROCESS_DIVISOR:.0f} = "
                f"{condition_contribution:.2f}."
            ),
        ),
        PriorityFactor(
            dimension="events", contribution=events_contribution, max_contribution=EVENTS_CONTRIBUTION_MAX,
            source_factor_id=None, provenance=PRIORITY_PROVENANCE,
            reason=f"{len(events_factors_penalized)} repeated-event factor(s) penalized, {events_penalty_sum:.2f} x {EVENTS_CONTRIBUTION_MULTIPLIER} = {events_contribution:.2f}.",
        ),
        PriorityFactor(
            dimension="maintenance_overdue", contribution=maintenance_contribution, max_contribution=MAINTENANCE_CONTRIBUTION_MAX,
            source_factor_id=None, provenance=PRIORITY_PROVENANCE,
            reason=(
                f"Maintenance overdue by {maintenance_evidence['days_overdue']} day(s) (was due {maintenance_evidence['next_due']})."
                if overdue_confirmed else "Maintenance is on schedule or no schedule data is configured."
            ),
        ),
        PriorityFactor(
            dimension="recent_movement", contribution=movement_contribution, max_contribution=RECENT_MOVEMENT_CONTRIBUTION_MAX,
            source_factor_id=None, provenance=PRIORITY_PROVENANCE,
            reason=f"Recent Health Score movement is {recent_movement} (contributes {movement_contribution:+.1f}) - descriptive only, never a prediction.",
        ),
        PriorityFactor(
            dimension="criticality", contribution=criticality_contribution, max_contribution=CRITICALITY_CONTRIBUTION_MAX,
            source_factor_id=None, provenance=PRIORITY_PROVENANCE,
            reason=f"Criticality is {criticality or 'unconfigured'} (contributes {criticality_contribution:.1f}).",
        ),
    ]
    if floor_applied:
        priority_factors.append(PriorityFactor(
            dimension="overdue_floor", contribution=0.0, max_contribution=None, source_factor_id=None,
            provenance=PRIORITY_PROVENANCE,
            reason=f"{MAINTENANCE_OVERDUE_FLOOR_REASON} Minimum Maintenance Priority raised to {MAINTENANCE_OVERDUE_FLOOR_PRIORITY}.",
        ))

    recommended_checks = _recommended_checks(factor_snapshots, overdue_confirmed) if assessed_evidence else []

    return MaintenancePriorityResult(
        equipment_id=equipment_id, plant_id=plant_id, plant_code=plant_code, instance_key=instance_key,
        equipment_type=equipment_type, maintenance_priority=maintenance_priority, priority_score=priority_score,
        floor_applied=floor_applied, floor_reason=floor_reason, recommendation_confidence=recommendation_confidence,
        confidence_basis=confidence_basis, health_score=health_score, health_band=health_band,
        health_confidence=health_confidence, health_provisional=health_provisional, recent_movement=recent_movement,
        criticality=criticality, maintenance_status=maintenance_evidence,
        assessed_evidence=list(assessed_evidence), priority_factors=priority_factors,
        recommended_checks=recommended_checks, maintenance_context=_maintenance_context(config_database_path, equipment_id),
        limitations=limitations,
        assumptions=[
            "Maintenance Priority is a deterministic engineering prioritization signal derived from already-persisted "
            "Equipment Health evidence and maintenance schedule data - not a predicted failure date, not Remaining "
            "Useful Life, not an automatically generated work order.",
            f"Maintenance Intelligence methodology version {MAINTENANCE_INTELLIGENCE_MODEL_VERSION} - all weights are "
            "SIMULATION_TUNING engineering judgment for this dev-stage system, not manufacturer limits.",
            "Electrical-load evidence is deliberately excluded - that evidence belongs to Energy Opportunities, never "
            "double-counted here as a maintenance concern.",
        ],
        model_version=MAINTENANCE_INTELLIGENCE_MODEL_VERSION, generated_at=generated_at,
    )


def calculate_all(config_database_path: str | Path, plant_code: str, now: datetime | None = None) -> list[MaintenancePriorityResult]:
    connection = sqlite3.connect(config_database_path)
    try:
        row = connection.execute("SELECT id FROM plants WHERE code = ?", (plant_code,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return []
    plant_id = row[0]

    targets = discover_health_targets(config_database_path, plant_code)
    return [
        calculate_maintenance_priority(config_database_path, plant_id, plant_code, equipment_type, instance_key, now=now)
        for equipment_type, instance_key in targets
    ]


# ---------------------------------------------------------------------------
# Debug CLI - read-only, mirrors engine/health_engine.py::main(). No UI.
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    from config.environment import get_config_db_path

    parser = argparse.ArgumentParser(description="Phase 13 Maintenance Intelligence - read-only debug CLI")
    parser.add_argument("--plant", default="p01")
    parser.add_argument("--instance", default=None, help="e.g. P01.WATER.WSP01 - if omitted, scores every eligible instance")
    args = parser.parse_args()

    config_db = get_config_db_path()

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
        result = calculate_maintenance_priority(config_db, plant_id, args.plant, equipment_type, instance_key)
        print(
            f"\n{instance_key} [{equipment_type}]  Priority: {result.maintenance_priority} "
            f"(score {result.priority_score}{', floor applied' if result.floor_applied else ''})  "
            f"Confidence: {result.recommendation_confidence} (basis: {', '.join(result.confidence_basis) or 'none'})"
        )
        for check in result.recommended_checks:
            print(f"    - {check}")


if __name__ == "__main__":
    main()
