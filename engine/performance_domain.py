from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.performance_engine import MaintenanceComparison, PerformanceObservation

"""
Phase 14 - plain data-access layer for asset_performance_observations /
asset_performance_maintenance_comparisons. NO change-detection, NO
scoring - this module only writes what it's given and reads it back.
Mirrors Phase 12.2's engine/health_domain.py split exactly: business
logic (when to persist) lives in engine/performance_orchestration.py,
not here.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _connect(config_database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


# ---------------------------------------------------------------------------
# asset_performance_observations
# ---------------------------------------------------------------------------

def persist_observation(
    config_database_path: str | Path, observation: PerformanceObservation, change_reason: str,
    consecutive_degrading_observations: int, sustained_degradation: bool, now: datetime | None = None,
) -> int:
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)

    connection = _connect(config_database_path)
    try:
        cursor = connection.execute(
            "INSERT INTO asset_performance_observations ("
            " equipment_id, plant_id, plant_code, instance_key, equipment_type, target_key, direction, unit,"
            " observed_value, reference_value, absolute_change, percent_change, performance_state, evidence_quality,"
            " sample_count, reference_sample_count, participates_in_degradation, consecutive_degrading_observations,"
            " sustained_degradation, context_used_json, missing_context_json, reason, assumptions_json,"
            " limitations_json, change_reason, model_version, computed_at, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                observation.equipment_id, observation.plant_id, observation.plant_code, observation.instance_key,
                observation.equipment_type, observation.target_key, observation.direction, observation.unit,
                observation.observed_value, observation.reference_value, observation.absolute_change,
                observation.percent_change, observation.performance_state, observation.evidence_quality,
                observation.sample_count, observation.reference_sample_count,
                1 if observation.participates_in_degradation else 0, consecutive_degrading_observations,
                1 if sustained_degradation else 0, json.dumps(observation.context_used),
                json.dumps(observation.missing_context), observation.reason, json.dumps(observation.assumptions),
                json.dumps(observation.limitations), change_reason, observation.model_version,
                observation.computed_at, now_text,
            ),
        )
        connection.commit()
        return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _enrich_observation(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["participates_in_degradation"] = bool(row["participates_in_degradation"])
    row["sustained_degradation"] = bool(row["sustained_degradation"])
    row["context_used"] = json.loads(row["context_used_json"]) if row.get("context_used_json") else {}
    row["missing_context"] = json.loads(row["missing_context_json"]) if row.get("missing_context_json") else []
    row["assumptions"] = json.loads(row["assumptions_json"]) if row.get("assumptions_json") else []
    row["limitations"] = json.loads(row["limitations_json"]) if row.get("limitations_json") else []
    return row


def get_latest_observation(config_database_path: str | Path, plant_id: int, instance_key: str, target_key: str) -> dict[str, Any] | None:
    connection = _connect(config_database_path)
    try:
        row = connection.execute(
            "SELECT * FROM asset_performance_observations WHERE plant_id = ? AND instance_key = ? AND target_key = ? "
            "ORDER BY computed_at DESC, id DESC LIMIT 1",
            (plant_id, instance_key, target_key),
        ).fetchone()
        return _enrich_observation(row) if row else None
    finally:
        connection.close()


def list_observations(
    config_database_path: str | Path, plant_id: int, instance_key: str, target_key: str,
    start: str | None = None, end: str | None = None, limit: int | None = None,
) -> list[dict[str, Any]]:
    """Chronological (oldest first) history for one equipment/dimension."""
    connection = _connect(config_database_path)
    query = "SELECT * FROM asset_performance_observations WHERE plant_id = ? AND instance_key = ? AND target_key = ?"
    params: list[Any] = [plant_id, instance_key, target_key]
    if start is not None:
        query += " AND computed_at >= ?"
        params.append(start)
    if end is not None:
        query += " AND computed_at <= ?"
        params.append(end)
    query += " ORDER BY computed_at ASC, id ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    try:
        return [_enrich_observation(dict(r)) for r in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def list_latest_observations_for_plant(config_database_path: str | Path, plant_id: int) -> list[dict[str, Any]]:
    """One row per (instance_key, target_key) - the most recent observation each has."""
    connection = _connect(config_database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM asset_performance_observations WHERE id IN ("
            " SELECT MAX(id) FROM asset_performance_observations WHERE plant_id = ? GROUP BY instance_key, target_key"
            ") ORDER BY instance_key, target_key",
            (plant_id,),
        ).fetchall()
        return [_enrich_observation(dict(r)) for r in rows]
    finally:
        connection.close()


def recent_observations_for_consecutive_count(
    config_database_path: str | Path, plant_id: int, instance_key: str, target_key: str, limit: int,
) -> list[dict[str, Any]]:
    """Most-recent-first, bounded - used ONLY to derive the consecutive-
    materially-degrading-observations count (item J.4), never an
    unbounded scan."""
    connection = _connect(config_database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM asset_performance_observations WHERE plant_id = ? AND instance_key = ? AND target_key = ? "
            "ORDER BY computed_at DESC, id DESC LIMIT ?",
            (plant_id, instance_key, target_key, limit),
        ).fetchall()
        return [_enrich_observation(dict(r)) for r in rows]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# asset_performance_maintenance_comparisons
# ---------------------------------------------------------------------------

def persist_maintenance_comparison(config_database_path: str | Path, comparison: MaintenanceComparison, now: datetime | None = None) -> int:
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)

    connection = _connect(config_database_path)
    try:
        cursor = connection.execute(
            "INSERT INTO asset_performance_maintenance_comparisons ("
            " equipment_id, plant_id, plant_code, instance_key, equipment_type, target_key, direction, unit,"
            " maintenance_log_id, performed_at, pre_window_start, pre_window_end, post_window_start, post_window_end,"
            " pre_value, post_value, absolute_change, percent_change, pre_sample_count, post_sample_count,"
            " evidence_quality, effectiveness_result, reason, assumptions_json, limitations_json, model_version,"
            " computed_at, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                comparison.equipment_id, comparison.plant_id, comparison.plant_code, comparison.instance_key,
                comparison.equipment_type, comparison.target_key, comparison.direction, comparison.unit,
                comparison.maintenance_log_id, comparison.performed_at,
                comparison.pre_window[0] if comparison.pre_window else None,
                comparison.pre_window[1] if comparison.pre_window else None,
                comparison.post_window[0] if comparison.post_window else None,
                comparison.post_window[1] if comparison.post_window else None,
                comparison.pre_value, comparison.post_value, comparison.absolute_change, comparison.percent_change,
                comparison.pre_sample_count, comparison.post_sample_count, comparison.evidence_quality,
                comparison.effectiveness_result, comparison.reason, json.dumps(comparison.assumptions),
                json.dumps(comparison.limitations), comparison.model_version, comparison.computed_at, now_text,
            ),
        )
        connection.commit()
        return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _enrich_comparison(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["assumptions"] = json.loads(row["assumptions_json"]) if row.get("assumptions_json") else []
    row["limitations"] = json.loads(row["limitations_json"]) if row.get("limitations_json") else []
    return row


def get_latest_maintenance_comparison(
    config_database_path: str | Path, plant_id: int, instance_key: str, target_key: str, maintenance_log_id: int,
) -> dict[str, Any] | None:
    connection = _connect(config_database_path)
    try:
        row = connection.execute(
            "SELECT * FROM asset_performance_maintenance_comparisons WHERE plant_id = ? AND instance_key = ? "
            "AND target_key = ? AND maintenance_log_id = ? ORDER BY computed_at DESC, id DESC LIMIT 1",
            (plant_id, instance_key, target_key, maintenance_log_id),
        ).fetchone()
        return _enrich_comparison(row) if row else None
    finally:
        connection.close()


def list_maintenance_comparisons_for_instance(config_database_path: str | Path, plant_id: int, instance_key: str) -> list[dict[str, Any]]:
    connection = _connect(config_database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM asset_performance_maintenance_comparisons WHERE plant_id = ? AND instance_key = ? "
            "ORDER BY performed_at DESC, id DESC",
            (plant_id, instance_key),
        ).fetchall()
        return [_enrich_comparison(dict(r)) for r in rows]
    finally:
        connection.close()


def list_latest_maintenance_comparisons_for_plant(config_database_path: str | Path, plant_id: int) -> list[dict[str, Any]]:
    """One row per (instance_key, target_key) - the most recent maintenance comparison each has."""
    connection = _connect(config_database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM asset_performance_maintenance_comparisons WHERE id IN ("
            " SELECT MAX(id) FROM asset_performance_maintenance_comparisons WHERE plant_id = ? GROUP BY instance_key, target_key"
            ") ORDER BY instance_key, target_key",
            (plant_id,),
        ).fetchall()
        return [_enrich_comparison(dict(r)) for r in rows]
    finally:
        connection.close()
