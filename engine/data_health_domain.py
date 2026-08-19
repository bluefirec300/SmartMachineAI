from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.data_health_engine import EquipmentDataHealth
from engine.data_health_targets import DATA_HEALTH_MODEL_VERSION

"""
Phase 16.5 - plain data-access layer for data_health_snapshots. NO
change-detection, NO scoring - this module only writes what it's given
and reads it back. Mirrors engine/health_domain.py's own split exactly:
business logic (when to persist) lives in
engine/data_health_orchestration.py, not here.

Timestamps use "%Y-%m-%d %H:%M:%S" (local, space-separated) - the same
convention every analytics table since Phase 6 uses, matching
EquipmentDataHealth.computed_at's own format.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _connect(config_database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def persist_snapshot(
    config_database_path: str | Path, plant_id: int, plant_code: str,
    result: EquipmentDataHealth, change_reason: str,
    now: datetime | None = None, evaluation_error: str | None = None,
) -> int:
    """
    Inserts one data_health_snapshots row. `evaluation_error` is set
    only when this snapshot represents a genuine evaluation FAILURE
    (distinct from a legitimate UNAVAILABLE result the engine itself
    returned cleanly - e.g. "no applicable registry") - never fabricated,
    never silently coerced into a different status.
    """
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)
    components = result.component_scores or {}

    connection = _connect(config_database_path)
    try:
        cursor = connection.execute(
            "INSERT INTO data_health_snapshots ("
            " equipment_id, plant_id, plant_code, instance_key, equipment_type,"
            " confidence_score, confidence_status,"
            " freshness_score, availability_score, validity_score, continuity_score,"
            " required_tag_count, available_tag_count, fresh_tag_count,"
            " missing_tag_count, stale_tag_count, invalid_tag_count, gap_count,"
            " timestamp_issue_count, indeterminate_freshness_count, frozen_candidate_count,"
            " source_driver, data_health_model_version, change_reason, evaluation_error,"
            " computed_at, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.equipment_id, plant_id, plant_code, result.instance_key, result.equipment_type,
                result.confidence_score, result.confidence_status,
                components.get("freshness"), components.get("availability"),
                components.get("validity"), components.get("continuity"),
                result.required_tag_count, result.available_tag_count, result.fresh_tag_count,
                len(result.missing_tags), len(result.stale_tags), len(result.invalid_tags), len(result.gaps),
                len(result.timestamp_issues), len(result.indeterminate_freshness_tags), len(result.frozen_candidates),
                (result.source or {}).get("configured_driver"),
                result.model_version or DATA_HEALTH_MODEL_VERSION, change_reason, evaluation_error,
                result.computed_at, now_text,
            ),
        )
        snapshot_id = cursor.lastrowid
        connection.commit()
        return snapshot_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_latest_snapshot(config_database_path: str | Path, plant_id: int, instance_key: str) -> dict[str, Any] | None:
    connection = _connect(config_database_path)
    try:
        row = connection.execute(
            "SELECT * FROM data_health_snapshots WHERE plant_id = ? AND instance_key = ? "
            "ORDER BY computed_at DESC, id DESC LIMIT 1",
            (plant_id, instance_key),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def get_snapshot_before(config_database_path: str | Path, plant_id: int, instance_key: str, before: str) -> dict[str, Any] | None:
    """The single most recent snapshot strictly before `before` -
    engine.data_health_history.py's "carry-in" state for a windowed
    query, so a window's leading edge is never treated as unknown when
    an earlier snapshot genuinely establishes what the state was."""
    connection = _connect(config_database_path)
    try:
        row = connection.execute(
            "SELECT * FROM data_health_snapshots WHERE plant_id = ? AND instance_key = ? AND computed_at < ? "
            "ORDER BY computed_at DESC, id DESC LIMIT 1",
            (plant_id, instance_key, before),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def list_snapshots(
    config_database_path: str | Path, plant_id: int, instance_key: str,
    start: str | None = None, end: str | None = None, limit: int | None = None,
) -> list[dict[str, Any]]:
    """Chronological (oldest first) history for one equipment, optionally
    bounded to [start, end] (inclusive, same TIME_FORMAT strings)."""
    connection = _connect(config_database_path)
    query = "SELECT * FROM data_health_snapshots WHERE plant_id = ? AND instance_key = ?"
    params: list[Any] = [plant_id, instance_key]
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
        return [dict(r) for r in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def list_latest_snapshots_for_plant(config_database_path: str | Path, plant_id: int) -> list[dict[str, Any]]:
    """One row per instance_key - the most recent snapshot each has."""
    connection = _connect(config_database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM data_health_snapshots WHERE id IN ("
            " SELECT MAX(id) FROM data_health_snapshots WHERE plant_id = ? GROUP BY instance_key"
            ") ORDER BY instance_key",
            (plant_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def first_snapshot_time(config_database_path: str | Path) -> str | None:
    """The earliest computed_at across the WHOLE table - "history
    recorded since <this>" (the approved honest-disclosure wording for
    the UI), never fabricated for periods before persistence began."""
    connection = _connect(config_database_path)
    try:
        row = connection.execute("SELECT MIN(computed_at) AS earliest FROM data_health_snapshots").fetchone()
        return row["earliest"] if row else None
    finally:
        connection.close()
