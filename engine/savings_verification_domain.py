from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.baseline_engine import TIME_FORMAT
from engine.savings_verification_targets import (
    ACTION_CATEGORY_VALUES,
    CONFIDENCE_VALUES,
    INTERVENTION_STATUS_IMPLEMENTED,
    INTERVENTION_STATUS_PLANNED,
    INTERVENTION_STATUS_VALUES,
    VERIFICATION_RESULT_VALUES,
)

"""
Phase 11.1 - plain data-access/domain helpers supporting the
savings_interventions / savings_verification_results schema. NO
calculation, NO scoring, NO automatic status progression - every
value written here is either a fixed vocabulary constant supplied by
the caller or a plain passthrough of caller-supplied data. Nothing in
this module is called automatically by Phase 11.1 itself; it exists so
the schema is exercisable by tests and by later phases (11.2+).

Timestamps use TIME_FORMAT (local, space-separated - the same
convention every analytics table since Phase 6 uses), explicitly NOT
audit_log's UTC/ISO-T convention - see engine/baseline_engine.py's
TIME_FORMAT, reused here rather than redefined.
"""


def _connect(config_database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    return connection


# ---------------------------------------------------------------------------
# savings_interventions
# ---------------------------------------------------------------------------

def create_intervention(
    config_database_path: str | Path, opportunity_id: int, plant_id: int, instance_key: str,
    action_category: str, action_description: str, recorded_by: str,
    equipment_id: int | None = None, expected_effect: str | None = None,
    stabilization_days: int | None = None, now: datetime | None = None,
) -> int:
    if action_category not in ACTION_CATEGORY_VALUES:
        raise ValueError(f"Invalid action_category: {action_category!r} - must be one of {ACTION_CATEGORY_VALUES}")
    if not action_description:
        raise ValueError("action_description is required")
    if not recorded_by:
        raise ValueError("recorded_by is required")

    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)

    fields = {
        "opportunity_id": opportunity_id, "plant_id": plant_id, "equipment_id": equipment_id,
        "instance_key": instance_key, "action_category": action_category, "action_description": action_description,
        "expected_effect": expected_effect, "recorded_by": recorded_by, "recorded_at": now_text,
        "implemented_at": None, "stabilization_days": stabilization_days,
        "status": INTERVENTION_STATUS_PLANNED, "created_at": now_text, "updated_at": now_text,
    }
    columns = list(fields.keys())
    placeholders = ", ".join("?" for _ in columns)

    connection = _connect(config_database_path)
    try:
        try:
            cursor = connection.execute(
                f"INSERT INTO savings_interventions ({', '.join(columns)}) VALUES ({placeholders})",
                [fields[c] for c in columns],
            )
            connection.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise ValueError(
                f"An active (non-terminal) intervention already exists for opportunity {opportunity_id} - "
                "resolve or complete it before recording a new one."
            ) from error
    finally:
        connection.close()


def mark_implemented(config_database_path: str | Path, intervention_id: int, implemented_at: datetime | None = None) -> None:
    existing = get_intervention(config_database_path, intervention_id)
    if existing is None:
        raise ValueError(f"No such intervention: {intervention_id}")
    if existing["status"] != INTERVENTION_STATUS_PLANNED:
        raise ValueError(f"Cannot mark implemented - intervention {intervention_id} is not PLANNED (status={existing['status']!r})")

    implemented_at = implemented_at or datetime.now()
    now_text = datetime.now().strftime(TIME_FORMAT)
    _write_intervention_fields(config_database_path, intervention_id, {
        "implemented_at": implemented_at.strftime(TIME_FORMAT),
        "status": INTERVENTION_STATUS_IMPLEMENTED,
        "updated_at": now_text,
    })


def update_intervention_status(config_database_path: str | Path, intervention_id: int, new_status: str) -> None:
    """Generic status transition - used by later phases (the future
    verification worker). Not called automatically by Phase 11.1."""
    if new_status not in INTERVENTION_STATUS_VALUES:
        raise ValueError(f"Invalid status: {new_status!r} - must be one of {INTERVENTION_STATUS_VALUES}")
    now_text = datetime.now().strftime(TIME_FORMAT)
    _write_intervention_fields(config_database_path, intervention_id, {"status": new_status, "updated_at": now_text})


def _write_intervention_fields(config_database_path: str | Path, intervention_id: int, fields: dict[str, Any]) -> None:
    connection = sqlite3.connect(config_database_path)
    try:
        set_clause = ", ".join(f"{c} = ?" for c in fields)
        connection.execute(
            f"UPDATE savings_interventions SET {set_clause} WHERE id = ?",
            [*fields.values(), intervention_id],
        )
        connection.commit()
    finally:
        connection.close()


def get_intervention(config_database_path: str | Path, intervention_id: int) -> dict[str, Any] | None:
    connection = _connect(config_database_path)
    try:
        row = connection.execute("SELECT * FROM savings_interventions WHERE id = ?", (intervention_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def find_active_intervention(config_database_path: str | Path, opportunity_id: int) -> dict[str, Any] | None:
    """The one ACTIVE (non-terminal) intervention for this opportunity,
    if any - matches the partial unique index exactly."""
    connection = _connect(config_database_path)
    try:
        row = connection.execute(
            "SELECT * FROM savings_interventions WHERE opportunity_id = ? "
            "AND status IN ('PLANNED', 'IMPLEMENTED', 'VERIFICATION_PENDING', 'VERIFICATION_IN_PROGRESS')",
            (opportunity_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def list_interventions(config_database_path: str | Path, plant_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
    connection = _connect(config_database_path)
    query = "SELECT * FROM savings_interventions WHERE 1 = 1"
    params: list[Any] = []
    if plant_id is not None:
        query += " AND plant_id = ?"
        params.append(plant_id)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY recorded_at DESC"
    try:
        return [dict(r) for r in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# savings_verification_results - APPEND-ONLY. No update/delete helper
# exists here by design; a later evaluation is always a new row, never
# a modification of a prior one.
# ---------------------------------------------------------------------------

def record_verification_result(
    config_database_path: str | Path, intervention_id: int, result: str,
    evidence_json: str, assumptions_json: str, limitations_json: str,
    reference_period_start: str | None = None, reference_period_end: str | None = None,
    verification_period_start: str | None = None, verification_period_end: str | None = None,
    verification_method: str | None = None, reason: str | None = None,
    verified_energy_kwh: float | None = None, verified_cost: float | None = None,
    verified_cost_currency: str | None = None, tariff_provenance: str | None = None,
    confidence: str | None = None, evaluated_at: datetime | None = None,
) -> int:
    if result not in VERIFICATION_RESULT_VALUES:
        raise ValueError(f"Invalid result: {result!r} - must be one of {VERIFICATION_RESULT_VALUES}")
    if confidence is not None and confidence not in CONFIDENCE_VALUES:
        raise ValueError(f"Invalid confidence: {confidence!r} - must be one of {CONFIDENCE_VALUES}")

    evaluated_at = evaluated_at or datetime.now()
    fields = {
        "intervention_id": intervention_id, "evaluated_at": evaluated_at.strftime(TIME_FORMAT),
        "reference_period_start": reference_period_start, "reference_period_end": reference_period_end,
        "verification_period_start": verification_period_start, "verification_period_end": verification_period_end,
        "verification_method": verification_method, "result": result, "reason": reason,
        "verified_energy_kwh": verified_energy_kwh, "verified_cost": verified_cost,
        "verified_cost_currency": verified_cost_currency, "tariff_provenance": tariff_provenance,
        "confidence": confidence, "evidence_json": evidence_json, "assumptions_json": assumptions_json,
        "limitations_json": limitations_json, "created_at": datetime.now().strftime(TIME_FORMAT),
    }
    columns = list(fields.keys())
    placeholders = ", ".join("?" for _ in columns)

    connection = sqlite3.connect(config_database_path)
    try:
        cursor = connection.execute(
            f"INSERT INTO savings_verification_results ({', '.join(columns)}) VALUES ({placeholders})",
            [fields[c] for c in columns],
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def get_latest_verification_result(config_database_path: str | Path, intervention_id: int) -> dict[str, Any] | None:
    connection = _connect(config_database_path)
    try:
        row = connection.execute(
            "SELECT * FROM savings_verification_results WHERE intervention_id = ? "
            "ORDER BY evaluated_at DESC, id DESC LIMIT 1",
            (intervention_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def list_verification_history(config_database_path: str | Path, intervention_id: int) -> list[dict[str, Any]]:
    connection = _connect(config_database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM savings_verification_results WHERE intervention_id = ? ORDER BY evaluated_at ASC, id ASC",
            (intervention_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Debug/inspection CLI - read-only, mirrors the project's existing
# debug-CLI convention (engine/anomaly_engine.py::main(),
# engine/opportunity_engine.py::main()). No evaluation logic exists
# yet, so this only lists what's in the tables.
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    from config.environment import get_config_db_path

    parser = argparse.ArgumentParser(description="Phase 11.1 Savings Verification - read-only inspection CLI")
    parser.add_argument("--config-database", default=str(get_config_db_path()))
    parser.add_argument("--plant-id", type=int, default=None)
    parser.add_argument("--status", default=None)
    args = parser.parse_args()

    interventions = list_interventions(args.config_database, plant_id=args.plant_id, status=args.status)
    print(f"{len(interventions)} intervention(s):")
    for row in interventions:
        print(f"  #{row['id']} opportunity={row['opportunity_id']} status={row['status']} category={row['action_category']} recorded_by={row['recorded_by']}")
        history = list_verification_history(args.config_database, row["id"])
        for result_row in history:
            print(f"      evaluation @ {result_row['evaluated_at']}: {result_row['result']} (confidence={result_row['confidence']})")


if __name__ == "__main__":
    main()
