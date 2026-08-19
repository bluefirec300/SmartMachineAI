from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine import health_domain as dom
from engine import health_engine as he
from engine.health_persistence_targets import HEARTBEAT_MAX_INTERVAL_HOURS, SCORE_CHANGE_THRESHOLD

"""
Phase 12.2 - Equipment Health worker business logic: change detection,
persistence decisions, and the per-cycle orchestration loop. NO scoring
logic lives here (that's the accepted Phase 12.1 engine/health_engine.py,
called unmodified) and NO raw SQL writes happen here (that's
engine/health_domain.py). This module only decides WHETHER today's
calculation is worth writing to history, and drives the multi-plant/
multi-equipment loop with per-item failure isolation (item 18) -
exactly the same split Phase 11.4's engine/savings_verification_orchestration.py
established for the savings-verification worker.
"""

DATE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _get_plants(config_database_path: str | Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute("SELECT id, code, name FROM plants ORDER BY code")]
    finally:
        connection.close()


def _penalized_factor_ids(factor_results) -> frozenset[str]:
    return frozenset(f.factor_id for f in factor_results if f.status == "penalized")


def has_material_change(
    config_database_path: str | Path, result: he.HealthResult, latest_row: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Deterministic change detection (item 7). Returns (should_persist,
    reason). Checked in a fixed, documented order - the FIRST condition
    that fires is the recorded reason, even if several are true at once."""
    if latest_row is None:
        return True, "initial"

    latest_score = latest_row["health_score"]
    current_score = result.health_score

    if current_score is None and latest_score is not None:
        return True, "became_insufficient"
    if current_score is not None and latest_score is None:
        return True, "recovered_from_insufficient"
    if current_score is not None and latest_score is not None:
        if abs(current_score - latest_score) >= SCORE_CHANGE_THRESHOLD:
            return True, "score_changed"

    if result.health_band != latest_row["health_band"]:
        return True, "band_changed"
    if result.assessment_confidence != latest_row["assessment_confidence"]:
        return True, "confidence_changed"
    if result.coverage_status != latest_row["coverage_status"]:
        return True, "coverage_changed"
    if bool(result.provisional) != bool(latest_row["provisional"]):
        return True, "provisional_changed"

    current_penalized = _penalized_factor_ids(result.factor_results)
    previous_factors = dom.get_factor_snapshots(config_database_path, latest_row["id"])
    previous_penalized = frozenset(f["factor_id"] for f in previous_factors if f["status"] == "penalized")
    if current_penalized != previous_penalized:
        return True, "factor_composition_changed"

    return False, "unchanged"


def _should_heartbeat(latest_row: dict[str, Any] | None, now: datetime) -> bool:
    if latest_row is None:
        return False  # "initial" already covers this case
    try:
        last_computed = datetime.strptime(latest_row["computed_at"], DATE_TIME_FORMAT)
    except (ValueError, TypeError):
        return True  # unreadable timestamp - safer to refresh than to silently skip forever
    return (now - last_computed) >= timedelta(hours=HEARTBEAT_MAX_INTERVAL_HOURS)


def evaluate_and_maybe_persist(
    config_database_path: str | Path, machine_database_path: str | Path, plant_id: int, plant_code: str,
    equipment_type: str, instance_key: str, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    result = he.calculate_health(config_database_path, machine_database_path, plant_id, plant_code, equipment_type, instance_key, now=now)
    latest_row = dom.get_latest_snapshot(config_database_path, plant_id, instance_key)

    changed, reason = has_material_change(config_database_path, result, latest_row)
    if not changed and _should_heartbeat(latest_row, now):
        changed, reason = True, "heartbeat"

    outcome = {
        "instance_key": instance_key, "equipment_type": equipment_type,
        "health_score": result.health_score, "coverage_status": result.coverage_status,
        "action": "unchanged", "change_reason": reason, "snapshot_id": None,
    }
    if changed:
        snapshot_id = dom.persist_snapshot(config_database_path, result, reason, now=now)
        outcome["action"] = "persisted"
        outcome["snapshot_id"] = snapshot_id
    return outcome


def run_cycle(config_database_path: str | Path, machine_database_path: str | Path, now: datetime | None = None) -> dict[str, Any]:
    """One full pass over every eligible equipment instance across every
    plant. Each instance's processing is individually failure-isolated
    (item 18) - one equipment's exception never aborts the cycle."""
    now = now or datetime.now()
    start = time.time()

    summary = {
        "eligible": 0, "evaluated": 0, "scored": 0, "insufficient": 0,
        "persisted": 0, "unchanged": 0, "errors": 0,
    }

    plants = _get_plants(config_database_path)
    for plant in plants:
        targets = he.discover_health_targets(config_database_path, plant["code"])
        summary["eligible"] += len(targets)
        for equipment_type, instance_key in targets:
            try:
                outcome = evaluate_and_maybe_persist(
                    config_database_path, machine_database_path, plant["id"], plant["code"],
                    equipment_type, instance_key, now=now,
                )
            except Exception as error:
                summary["errors"] += 1
                print(f"equipment_health_worker: failed on {instance_key} ({plant['code']}): {error}")
                continue

            summary["evaluated"] += 1
            if outcome["health_score"] is None:
                summary["insufficient"] += 1
            else:
                summary["scored"] += 1
            if outcome["action"] == "persisted":
                summary["persisted"] += 1
            else:
                summary["unchanged"] += 1

    summary["duration_seconds"] = round(time.time() - start, 3)
    return summary
