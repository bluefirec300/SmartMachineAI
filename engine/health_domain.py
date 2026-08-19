from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.health_engine import HealthResult
from engine.health_targets import RECENCY_DECAY_DAYS

"""
Phase 12.2 - plain data-access layer for equipment_health_snapshots /
equipment_health_factor_snapshots. NO change-detection, NO scoring -
this module only writes what it's given and reads it back. Mirrors
Phase 11.1's engine/savings_verification_domain.py split exactly:
business logic (when to persist) lives in engine/health_orchestration.py,
not here.

Timestamps use "%Y-%m-%d %H:%M:%S" (local, space-separated) - the same
convention every analytics table since Phase 6 uses, matching
HealthResult.computed_at's own format.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _connect(config_database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _finding_counts(result: HealthResult) -> tuple[int, int]:
    """active_finding_count = factors currently contributing a penalty.
    recent_finding_count = factors with evidence timestamped within
    Phase 12.1's own RECENCY_DECAY_DAYS window, regardless of whether
    they're still penalizing right now (captures findings that mattered
    recently even if fully decayed to 0 by now)."""
    active = sum(1 for f in result.factor_results if f.status == "penalized")

    recent = 0
    try:
        computed_at = datetime.strptime(result.computed_at, TIME_FORMAT)
    except ValueError:
        return active, 0
    for f in result.factor_results:
        if not f.evidence_timestamp:
            continue
        try:
            ts = datetime.strptime(f.evidence_timestamp[:19], TIME_FORMAT)
        except ValueError:
            continue
        if (computed_at - ts).total_seconds() <= RECENCY_DECAY_DAYS * 86400:
            recent += 1
    return active, recent


def persist_snapshot(config_database_path: str | Path, result: HealthResult, change_reason: str, now: datetime | None = None) -> int:
    """Atomically inserts one equipment_health_snapshots row plus its
    equipment_health_factor_snapshots children, in a single transaction
    (item 9) - a failure partway through never leaves a parent row
    without its factors, or vice versa."""
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)
    active_count, recent_count = _finding_counts(result)

    connection = _connect(config_database_path)
    try:
        cursor = connection.execute(
            "INSERT INTO equipment_health_snapshots ("
            " equipment_id, plant_id, plant_code, instance_key, equipment_type, health_score, health_band,"
            " provisional, assessment_confidence, coverage_status, applicable_factor_count, usable_factor_count,"
            " active_finding_count, recent_finding_count, criticality, health_model_version, change_reason,"
            " limitations_json, assumptions_json, missing_factors_json, computed_at, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.equipment_id, result.plant_id, result.plant_code, result.instance_key, result.equipment_type,
                result.health_score, result.health_band, 1 if result.provisional else 0, result.assessment_confidence,
                result.coverage_status, result.applicable_factor_count, result.usable_factor_count,
                active_count, recent_count, result.criticality, result.health_model_version, change_reason,
                json.dumps(result.limitations), json.dumps(result.assumptions), json.dumps(result.missing_factors),
                result.computed_at, now_text,
            ),
        )
        snapshot_id = cursor.lastrowid

        for factor in result.factor_results:
            connection.execute(
                "INSERT INTO equipment_health_factor_snapshots ("
                " health_snapshot_id, equipment_id, factor_id, factor_family, status, penalty, maximum_penalty,"
                " evidence_source, evidence_value, evidence_timestamp, evidence_confidence, reason, provenance, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id, result.equipment_id, factor.factor_id, factor.family, factor.status,
                    factor.contribution, factor.max_penalty, factor.evidence_source,
                    None if factor.evidence_value is None else str(factor.evidence_value),
                    factor.evidence_timestamp, factor.confidence, factor.reason, factor.provenance, now_text,
                ),
            )

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
            "SELECT * FROM equipment_health_snapshots WHERE plant_id = ? AND instance_key = ? "
            "ORDER BY computed_at DESC, id DESC LIMIT 1",
            (plant_id, instance_key),
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
    query = "SELECT * FROM equipment_health_snapshots WHERE plant_id = ? AND instance_key = ?"
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


def get_factor_snapshots(config_database_path: str | Path, health_snapshot_id: int) -> list[dict[str, Any]]:
    connection = _connect(config_database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM equipment_health_factor_snapshots WHERE health_snapshot_id = ? ORDER BY id ASC",
            (health_snapshot_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def list_latest_snapshots_for_plant(config_database_path: str | Path, plant_id: int) -> list[dict[str, Any]]:
    """One row per instance_key - the most recent snapshot each has -
    for a future plant-wide 'current state' view (Phase 12.3)."""
    connection = _connect(config_database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM equipment_health_snapshots WHERE id IN ("
            " SELECT MAX(id) FROM equipment_health_snapshots WHERE plant_id = ? GROUP BY instance_key"
            ") ORDER BY instance_key",
            (plant_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()
