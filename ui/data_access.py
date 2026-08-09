from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"
MACHINE_DATABASE_PATH = PROJECT_ROOT / "database" / "machine_data.db"


def get_enabled_tags() -> list[dict[str, Any]]:
    """
    All currently-enabled tags, grouped by equipment.

    This is the single source of truth every UI page uses for "what
    exists right now" - as more of the P01/P02/Phase 2/3 dataset gets
    enabled via engine.tag_dataset_importer, it shows up here with no
    code changes.
    """
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute(
            """
            SELECT
                t.id AS tag_id,
                t.tag_name,
                COALESCE(t.description, '') AS description,
                COALESCE(t.unit, '') AS unit,
                COALESCE(t.data_type, '') AS data_type,
                COALESCE(e.display_name, 'Ungrouped') AS equipment_display_name,
                COALESCE(e.name, '') AS equipment_key,
                e.id AS equipment_id
            FROM tags t
            LEFT JOIN equipment e ON e.id = t.equipment_id
            WHERE t.enabled = 1
            ORDER BY equipment_display_name, t.tag_name
            """
        ).fetchall()
    finally:
        connection.close()

    return [dict(row) for row in rows]


def get_equipment_list() -> list[dict[str, Any]]:
    """All equipment rows, including device/brand/model/maintenance metadata."""
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute(
            """
            SELECT id, name, display_name, device_number, brand, model,
                   service_interval_days, last_serviced_at, next_due_at
            FROM equipment
            ORDER BY display_name
            """
        ).fetchall()
    finally:
        connection.close()

    return [dict(row) for row in rows]


def add_maintenance_entry(
    equipment_id: int,
    category: str,
    description: str,
    performed_at: str,
    parts_replaced: str | None = None,
    performed_by: str | None = None,
    next_due_at: str | None = None,
) -> None:
    """
    Records a maintenance log entry and updates the equipment's
    last_serviced_at (always) and next_due_at (only if a new value was
    given - otherwise the previous schedule estimate is left alone).
    """
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        connection.execute(
            """
            INSERT INTO maintenance_log (
                equipment_id, category, description, parts_replaced,
                performed_by, performed_at, next_due_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                equipment_id,
                category,
                description,
                parts_replaced,
                performed_by,
                performed_at,
                next_due_at,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )

        if next_due_at:
            connection.execute(
                "UPDATE equipment SET last_serviced_at = ?, next_due_at = ? WHERE id = ?",
                (performed_at, next_due_at, equipment_id),
            )
        else:
            connection.execute(
                "UPDATE equipment SET last_serviced_at = ? WHERE id = ?",
                (performed_at, equipment_id),
            )

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_maintenance_history(equipment_id: int | None = None) -> list[dict[str, Any]]:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    query = """
        SELECT
            m.id, m.category, m.description, m.parts_replaced,
            m.performed_by, m.performed_at, m.next_due_at, m.created_at,
            e.display_name AS equipment_display_name
        FROM maintenance_log m
        JOIN equipment e ON e.id = m.equipment_id
    """
    params: tuple[Any, ...] = ()

    if equipment_id is not None:
        query += " WHERE m.equipment_id = ?"
        params = (equipment_id,)

    query += " ORDER BY m.performed_at DESC, m.id DESC"

    try:
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()

    return [dict(row) for row in rows]
