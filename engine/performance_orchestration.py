from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from database.database import DatabaseManager
from engine import performance_domain as dom
from engine import performance_engine as perf
from engine.health_evidence import equipment_id_for_instance
from engine.performance_targets import (
    PERFORMANCE_HEARTBEAT_MAX_INTERVAL_HOURS,
    PERFORMANCE_OBSERVATION_MATERIAL_CHANGE_THRESHOLD_PCT,
    STATE_DEGRADING,
    STATE_SIGNIFICANTLY_DEGRADING,
    SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS,
)

"""
Phase 14 - background orchestration: materiality gating (item J.3),
degradation-persistence tracking (item J.4), and per-target/per-
maintenance-event failure isolation. NO performance mathematics live
here - engine.performance_engine (pure calculate) is used exactly as
it already is, unmodified. Mirrors engine.health_orchestration.py's
has_material_change()/evaluate_and_maybe_persist() pattern and
engine.savings_verification_orchestration.py's has_materially_new_evidence()/
run_cycle() pattern - two prior, accepted implementations of this exact
shape in this codebase.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

REASON_INITIAL = "initial"
REASON_MATERIAL_CHANGE = "material_change"
REASON_HEARTBEAT = "heartbeat"


# ---------------------------------------------------------------------------
# Ongoing self-reference observations
# ---------------------------------------------------------------------------

def has_material_change(observation: perf.PerformanceObservation, last_persisted: dict[str, Any] | None) -> bool:
    """Deterministic comparison against the most recently PERSISTED
    observation - never worker run count or elapsed clock time alone
    (that is _should_heartbeat's separate job)."""
    if last_persisted is None:
        return True
    if observation.performance_state != last_persisted["performance_state"]:
        return True
    if observation.evidence_quality != last_persisted["evidence_quality"]:
        return True
    new_pct, old_pct = observation.percent_change, last_persisted["percent_change"]
    if (new_pct is None) != (old_pct is None):
        return True  # availability of a percentage itself changed
    if new_pct is not None and old_pct is not None and abs(new_pct - old_pct) >= PERFORMANCE_OBSERVATION_MATERIAL_CHANGE_THRESHOLD_PCT:
        return True
    return False


def _should_heartbeat(last_persisted: dict[str, Any] | None, now: datetime) -> bool:
    if last_persisted is None:
        return False
    try:
        last_time = datetime.strptime(last_persisted["computed_at"][:19], TIME_FORMAT)
    except (ValueError, TypeError):
        return True
    return (now - last_time).total_seconds() >= PERFORMANCE_HEARTBEAT_MAX_INTERVAL_HOURS * 3600


def compute_consecutive_degrading(
    config_database_path: str | Path, plant_id: int, instance_key: str, target_key: str, current_state: str,
) -> tuple[int, bool]:
    """Counts this new observation plus however many immediately-prior
    PERSISTED observations were also DEGRADING/SIGNIFICANTLY_DEGRADING,
    stopping at the first non-degrading one (item J.4 - consecutive
    evaluations, never a calendar-duration guess). Bounded lookback
    (SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS + a small
    buffer) - never an unbounded history scan."""
    if current_state not in (STATE_DEGRADING, STATE_SIGNIFICANTLY_DEGRADING):
        return 0, False

    recent = dom.recent_observations_for_consecutive_count(
        config_database_path, plant_id, instance_key, target_key,
        limit=SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS + 5,
    )
    count = 1  # this new (not-yet-persisted) observation itself
    for row in recent:  # most-recent-first
        if row["performance_state"] in (STATE_DEGRADING, STATE_SIGNIFICANTLY_DEGRADING):
            count += 1
        else:
            break
    sustained = count >= SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS
    return count, sustained


def evaluate_and_maybe_persist_observation(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    plant_id: int, target, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()

    observation = perf.calculate_performance_observation(config_database_path, machine_database_path, historian, plant_id, target, now=now)
    last_persisted = dom.get_latest_observation(config_database_path, plant_id, target.instance_key, target.target_key)

    consecutive, sustained = compute_consecutive_degrading(
        config_database_path, plant_id, target.instance_key, target.target_key, observation.performance_state,
    )

    if has_material_change(observation, last_persisted):
        change_reason = REASON_INITIAL if last_persisted is None else REASON_MATERIAL_CHANGE
    elif _should_heartbeat(last_persisted, now):
        change_reason = REASON_HEARTBEAT
    else:
        return {
            "instance_key": target.instance_key, "target_key": target.target_key, "action": "skipped",
            "reason": "no_material_change", "performance_state": observation.performance_state,
        }

    row_id = dom.persist_observation(
        config_database_path, observation, change_reason, consecutive_degrading_observations=consecutive,
        sustained_degradation=sustained, now=now,
    )
    return {
        "instance_key": target.instance_key, "target_key": target.target_key, "action": "persisted",
        "id": row_id, "performance_state": observation.performance_state, "change_reason": change_reason,
        "consecutive_degrading_observations": consecutive, "sustained_degradation": sustained,
    }


def run_observation_cycle(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    plant_code: str, now: datetime | None = None,
) -> dict[str, Any]:
    """One full pass over every discoverable performance target for one
    plant. Each target's processing is individually failure-isolated -
    one exception never aborts the rest of the cycle."""
    now = now or datetime.now()
    summary = {"evaluated": 0, "persisted": 0, "skipped": 0, "failed": 0, "sustained_degradation_count": 0}

    connection = sqlite3.connect(config_database_path)
    try:
        row = connection.execute("SELECT id FROM plants WHERE code = ?", (plant_code,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return summary
    plant_id = row[0]

    targets = perf.discover_performance_targets(config_database_path, plant_code)
    for target in targets:
        summary["evaluated"] += 1
        try:
            outcome = evaluate_and_maybe_persist_observation(config_database_path, machine_database_path, historian, plant_id, target, now=now)
        except Exception as error:
            summary["failed"] += 1
            print(f"asset_performance_worker: observation failed for {target.instance_key}.{target.target_key}: {error}")
            continue

        if outcome["action"] == "persisted":
            summary["persisted"] += 1
            if outcome.get("sustained_degradation"):
                summary["sustained_degradation_count"] += 1
        else:
            summary["skipped"] += 1

    return summary


# ---------------------------------------------------------------------------
# Maintenance-anchored before/after comparisons
# ---------------------------------------------------------------------------

def _most_recent_maintenance_log_per_equipment(config_database_path: str | Path) -> list[dict[str, Any]]:
    """One SQL query - the most recent maintenance_log row per
    equipment_id. Bounded by construction (one row per equipment, never
    an unbounded historical sweep)."""
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM maintenance_log WHERE id IN ("
            " SELECT MAX(id) FROM maintenance_log GROUP BY equipment_id"
            ")"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def has_materially_new_maintenance_evidence(comparison: perf.MaintenanceComparison, last_persisted: dict[str, Any] | None) -> bool:
    if last_persisted is None:
        return True
    if (comparison.post_sample_count or 0) > (last_persisted.get("post_sample_count") or 0):
        return True
    if comparison.evidence_quality != last_persisted.get("evidence_quality"):
        return True
    if comparison.effectiveness_result != last_persisted.get("effectiveness_result"):
        return True
    return False


def evaluate_maintenance_comparison(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    plant_id: int, target, maintenance_log_row: dict[str, Any], now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()

    comparison = perf.calculate_maintenance_comparison(config_database_path, machine_database_path, historian, plant_id, target, maintenance_log_row, now=now)
    last_persisted = dom.get_latest_maintenance_comparison(
        config_database_path, plant_id, target.instance_key, target.target_key, maintenance_log_row["id"],
    )

    if not has_materially_new_maintenance_evidence(comparison, last_persisted):
        return {
            "instance_key": target.instance_key, "target_key": target.target_key, "action": "skipped",
            "reason": "no_new_evidence",
        }

    row_id = dom.persist_maintenance_comparison(config_database_path, comparison, now=now)
    return {
        "instance_key": target.instance_key, "target_key": target.target_key, "action": "persisted", "id": row_id,
        "effectiveness_result": comparison.effectiveness_result,
    }


def run_maintenance_comparison_cycle(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    plant_code: str, now: datetime | None = None,
) -> dict[str, Any]:
    """One full pass: for every equipment with at least one maintenance_log
    entry, compare its dimensions around the MOST RECENT entry. Each
    equipment/dimension is individually failure-isolated."""
    now = now or datetime.now()
    summary = {"evaluated": 0, "persisted": 0, "skipped": 0, "failed": 0}

    connection = sqlite3.connect(config_database_path)
    try:
        row = connection.execute("SELECT id FROM plants WHERE code = ?", (plant_code,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return summary
    plant_id = row[0]

    targets_by_instance: dict[str, list] = {}
    for target in perf.discover_performance_targets(config_database_path, plant_code):
        targets_by_instance.setdefault(target.instance_key, []).append(target)

    # instance_key -> equipment_id, resolved ONCE per cycle (never by
    # name-string parsing) - reuses the same evidence-read helper
    # Phase 12/13 already use for this exact lookup.
    equipment_id_by_instance = {
        instance_key: equipment_id_for_instance(config_database_path, instance_key)
        for instance_key in targets_by_instance
    }

    for maintenance_log_row in _most_recent_maintenance_log_per_equipment(config_database_path):
        equipment_id = maintenance_log_row["equipment_id"]
        matched_instance = next(
            (instance_key for instance_key, eid in equipment_id_by_instance.items() if eid == equipment_id), None,
        )
        if matched_instance is None:
            continue  # not this plant, not a Phase 14-supported equipment type, or no longer exists

        for target in targets_by_instance[matched_instance]:
            summary["evaluated"] += 1
            try:
                outcome = evaluate_maintenance_comparison(config_database_path, machine_database_path, historian, plant_id, target, maintenance_log_row, now=now)
            except Exception as error:
                summary["failed"] += 1
                print(f"asset_performance_worker: maintenance comparison failed for {target.instance_key}.{target.target_key}: {error}")
                continue

            if outcome["action"] == "persisted":
                summary["persisted"] += 1
            else:
                summary["skipped"] += 1

    return summary


# ---------------------------------------------------------------------------
# Round-robin, one-work-item-per-tick worker support (mirrors
# app/baseline_worker.py's OWN established pattern exactly - not
# app/equipment_health_worker.py's full-sweep-per-cycle pattern. This
# matters: Phase 14's per-target cost is dominated by raw historian
# reads via engine.baseline_engine.compute_window_baseline() (same cost
# profile as baseline_worker itself, measured ~2.5-5s/target against
# the live historian - see FACTORY_AI_DEVELOPMENT_STATUS.md's Phase 14
# section), NOT Phase 12.2's cheap already-persisted-evidence reads. A
# full multi-plant sweep in one worker tick would keep this process
# "always busy" and compete with baseline_worker/anomaly_worker for
# historian bandwidth - exactly what baseline_worker's own docstring
# says to avoid ("never one big sweep"). run_cycle()/run_observation_cycle()/
# run_maintenance_comparison_cycle() above remain the right shape for
# the debug CLI and tests (an explicit "do it all now" convenience) -
# the actual worker process uses build_rotation()/process_one_work_item()
# instead.
# ---------------------------------------------------------------------------

@dataclass
class WorkItem:
    kind: str  # "observation" | "maintenance"
    plant_id: int
    plant_code: str
    target: Any
    maintenance_log_row: dict[str, Any] | None = None


def build_rotation(config_database_path: str | Path) -> list[WorkItem]:
    """Built ONCE at worker startup (or whenever the caller chooses to
    rebuild it) - a flat list of independent work items across every
    plant, mixing ongoing self-reference observation targets with
    maintenance-anchored comparison targets so both kinds of work get
    visited as the rotation cycles, exactly one item per tick."""
    rotation: list[WorkItem] = []
    for plant in _get_plants(config_database_path):
        for target in perf.discover_performance_targets(config_database_path, plant["code"]):
            rotation.append(WorkItem(kind="observation", plant_id=plant["id"], plant_code=plant["code"], target=target))

        targets_by_instance: dict[str, list] = {}
        for target in perf.discover_performance_targets(config_database_path, plant["code"]):
            targets_by_instance.setdefault(target.instance_key, []).append(target)
        equipment_id_by_instance = {
            instance_key: equipment_id_for_instance(config_database_path, instance_key)
            for instance_key in targets_by_instance
        }
        for maintenance_log_row in _most_recent_maintenance_log_per_equipment(config_database_path):
            matched_instance = next(
                (ik for ik, eid in equipment_id_by_instance.items() if eid == maintenance_log_row["equipment_id"]), None,
            )
            if matched_instance is None:
                continue
            for target in targets_by_instance[matched_instance]:
                rotation.append(WorkItem(
                    kind="maintenance", plant_id=plant["id"], plant_code=plant["code"], target=target,
                    maintenance_log_row=maintenance_log_row,
                ))

    return rotation


def process_one_work_item(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    work_item: WorkItem, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    if work_item.kind == "observation":
        return evaluate_and_maybe_persist_observation(config_database_path, machine_database_path, historian, work_item.plant_id, work_item.target, now=now)
    return evaluate_maintenance_comparison(config_database_path, machine_database_path, historian, work_item.plant_id, work_item.target, work_item.maintenance_log_row, now=now)


# ---------------------------------------------------------------------------
# Combined worker entry point - one DatabaseManager (historian) created
# ONCE per cycle, both plants, both observation modes. Mirrors
# engine.health_orchestration.run_cycle()'s own 2-arg shape so
# app/asset_performance_worker.py stays as thin as every prior worker.
# ---------------------------------------------------------------------------

def _get_plants(config_database_path: str | Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute("SELECT id, code FROM plants ORDER BY code")]
    finally:
        connection.close()


def run_cycle(config_database_path: str | Path, machine_database_path: str | Path, now: datetime | None = None) -> dict[str, Any]:
    """One full pass across every plant - observations, then maintenance
    comparisons. Each plant/target/equipment is individually failure-
    isolated by the two cycle functions above; this function only
    aggregates their summaries."""
    now = now or datetime.now()
    historian = DatabaseManager(db_path=machine_database_path)

    summary = {
        "observations_evaluated": 0, "observations_persisted": 0, "observations_skipped": 0, "observations_failed": 0,
        "sustained_degradation_count": 0,
        "maintenance_evaluated": 0, "maintenance_persisted": 0, "maintenance_skipped": 0, "maintenance_failed": 0,
    }

    for plant in _get_plants(config_database_path):
        obs_summary = run_observation_cycle(config_database_path, machine_database_path, historian, plant["code"], now=now)
        summary["observations_evaluated"] += obs_summary["evaluated"]
        summary["observations_persisted"] += obs_summary["persisted"]
        summary["observations_skipped"] += obs_summary["skipped"]
        summary["observations_failed"] += obs_summary["failed"]
        summary["sustained_degradation_count"] += obs_summary["sustained_degradation_count"]

        maint_summary = run_maintenance_comparison_cycle(config_database_path, machine_database_path, historian, plant["code"], now=now)
        summary["maintenance_evaluated"] += maint_summary["evaluated"]
        summary["maintenance_persisted"] += maint_summary["persisted"]
        summary["maintenance_skipped"] += maint_summary["skipped"]
        summary["maintenance_failed"] += maint_summary["failed"]

    return summary
