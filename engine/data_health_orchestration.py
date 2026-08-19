from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine import data_health_domain as dhdom
from engine.data_health_engine import EquipmentDataHealth, calculate_equipment_data_health
from engine.data_health_persistence_targets import (
    COMPONENT_SCORE_CHANGE_THRESHOLD,
    CONFIDENCE_SCORE_CHANGE_THRESHOLD,
    COUNT_CHANGE_FIELDS,
    HEARTBEAT_MAX_INTERVAL_HOURS,
)
from engine.data_health_targets import STATUS_UNAVAILABLE
from engine.health_engine import discover_health_targets

"""
Phase 16.5 - Data Health worker business logic: change detection,
persistence decisions, and the per-cycle orchestration loop. NO scoring
logic lives here (that's the accepted, unmodified Phase 16.1
engine.data_health_engine.calculate_equipment_data_health()) and NO raw
SQL writes happen here (that's engine.data_health_domain). Mirrors
engine/health_orchestration.py's own split exactly (the approved
Phase 16.5 architecture).
"""

DATE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _get_plants(config_database_path: str | Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute("SELECT id, code, name FROM plants ORDER BY code")]
    finally:
        connection.close()


def _failure_result(instance_key: str, equipment_type: str | None, computed_at: str) -> EquipmentDataHealth:
    """A synthetic UNAVAILABLE result for a genuine evaluation exception
    (never a fabricated GOOD/DEGRADED/POOR guess) - passed through the
    SAME change-detection/heartbeat logic as any real engine result, so
    a persistent failure gets exactly one snapshot (not one per cycle)
    until it recovers or the 24h heartbeat fires."""
    return EquipmentDataHealth(
        instance_key=instance_key, equipment_id=None, equipment_type=equipment_type,
        confidence_score=None, confidence_status=STATUS_UNAVAILABLE,
        component_scores={"freshness": None, "availability": None, "validity": None, "continuity": None},
        component_applicability={"freshness": False, "availability": False, "validity": False, "continuity": False},
        required_tag_count=0, available_tag_count=0, fresh_tag_count=0,
        source={}, computed_at=computed_at,
    )


def has_material_change(result: EquipmentDataHealth, latest_row: dict[str, Any] | None) -> tuple[bool, str]:
    """
    Deterministic change detection (mirrors
    engine.health_orchestration.has_material_change()'s own ordered-
    check style exactly). Returns (should_persist, reason). Checked in a
    fixed, documented order - the FIRST condition that fires is the
    recorded reason, even if several are true at once.
    """
    if latest_row is None:
        return True, "initial"

    if result.confidence_status != latest_row["confidence_status"]:
        return True, "status_changed"

    latest_score = latest_row["confidence_score"]
    current_score = result.confidence_score

    if current_score is None and latest_score is not None:
        return True, "became_unavailable"
    if current_score is not None and latest_score is None:
        return True, "recovered_from_unavailable"
    if current_score is not None and latest_score is not None:
        if abs(current_score - latest_score) >= CONFIDENCE_SCORE_CHANGE_THRESHOLD:
            return True, "confidence_score_changed"

    components = result.component_scores or {}
    for name in ("freshness", "availability", "validity", "continuity"):
        current = components.get(name)
        previous = latest_row[f"{name}_score"]
        if (current is None) != (previous is None):
            return True, "component_applicability_changed"
        if current is not None and previous is not None and abs(current - previous) >= COMPONENT_SCORE_CHANGE_THRESHOLD:
            return True, "component_score_changed"

    current_counts = {
        "missing_tag_count": len(result.missing_tags),
        "stale_tag_count": len(result.stale_tags),
        "invalid_tag_count": len(result.invalid_tags),
        "gap_count": len(result.gaps),
        "timestamp_issue_count": len(result.timestamp_issues),
        "indeterminate_freshness_count": len(result.indeterminate_freshness_tags),
        "frozen_candidate_count": len(result.frozen_candidates),
    }
    for field in COUNT_CHANGE_FIELDS:
        if current_counts[field] != latest_row[field]:
            return True, "issue_count_changed"

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
    evaluation_error: str | None = None

    try:
        result = calculate_equipment_data_health(config_database_path, machine_database_path, instance_key, now=now)
    except Exception as error:
        evaluation_error = str(error)
        result = _failure_result(instance_key, equipment_type, now.strftime(DATE_TIME_FORMAT))

    latest_row = dhdom.get_latest_snapshot(config_database_path, plant_id, instance_key)

    changed, reason = has_material_change(result, latest_row)
    if not changed and _should_heartbeat(latest_row, now):
        changed, reason = True, "heartbeat"

    outcome = {
        "instance_key": instance_key, "equipment_type": equipment_type,
        "confidence_status": result.confidence_status, "confidence_score": result.confidence_score,
        "action": "unchanged", "change_reason": reason, "snapshot_id": None, "evaluation_error": evaluation_error,
    }
    if changed:
        snapshot_id = dhdom.persist_snapshot(
            config_database_path, plant_id, plant_code, result, reason, now=now, evaluation_error=evaluation_error,
        )
        outcome["action"] = "persisted"
        outcome["snapshot_id"] = snapshot_id
    return outcome


def run_cycle(config_database_path: str | Path, machine_database_path: str | Path, now: datetime | None = None) -> dict[str, Any]:
    """One full pass over every eligible equipment instance across every
    plant (the SAME population engine.health_engine.discover_health_targets()
    and Phase 16.4's fleet layer already use). Each instance's
    processing is individually failure-isolated - one equipment's
    exception (at the evaluation layer, evaluate_and_maybe_persist()
    already turns it into a persisted UNAVAILABLE row; at the
    persistence/DB layer, here) never aborts the cycle."""
    now = now or datetime.now()
    start = time.time()

    summary = {"eligible": 0, "evaluated": 0, "persisted": 0, "unchanged": 0, "errors": 0}

    plants = _get_plants(config_database_path)
    for plant in plants:
        targets = discover_health_targets(config_database_path, plant["code"])
        summary["eligible"] += len(targets)
        for equipment_type, instance_key in targets:
            try:
                outcome = evaluate_and_maybe_persist(
                    config_database_path, machine_database_path, plant["id"], plant["code"],
                    equipment_type, instance_key, now=now,
                )
            except Exception as error:
                summary["errors"] += 1
                print(f"data_health_history_worker: failed on {instance_key} ({plant['code']}): {error}")
                continue

            summary["evaluated"] += 1
            if outcome["action"] == "persisted":
                summary["persisted"] += 1
            else:
                summary["unchanged"] += 1

    summary["duration_seconds"] = round(time.time() - start, 3)
    return summary
