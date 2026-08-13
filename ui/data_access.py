from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path

CONFIG_DATABASE_PATH = get_config_db_path()
MACHINE_DATABASE_PATH = get_machine_db_path()


def get_person_in_charge_options() -> list[str]:
    """
    Display names of every active login account, for the "Person in
    Charge"/"Performed By" dropdowns on Service & Maintenance -
    replaces the earlier hardcoded placeholder list now that real
    accounts exist (see engine/seed_users.py).
    """
    from config.user_manager import UserManager

    manager = UserManager(database_path=CONFIG_DATABASE_PATH)
    return sorted(
        user["display_name"] for user in manager.get_users() if user["active"]
    )


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


def get_event_filter_options() -> dict[str, list[str]]:
    """Distinct equipment/tag values seen in machine_events, for filter dropdowns."""
    connection = sqlite3.connect(MACHINE_DATABASE_PATH)

    try:
        equipment = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT equipment FROM machine_events ORDER BY equipment"
            ).fetchall()
        ]
        tags = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT tag FROM machine_events ORDER BY tag"
            ).fetchall()
        ]
    finally:
        connection.close()

    return {"equipment": equipment, "tags": tags}


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


def add_service_entry(
    equipment_id: int,
    person_in_charge: str | None,
    description: str,
    performed_at: str,
) -> None:
    """
    Records a one-off service entry. Unlike maintenance_log, this has
    no next_due_at / schedule concept - service records are just a
    log for future recall, not something that drives the upcoming/
    overdue view. Still bumps equipment.last_serviced_at, since a
    service visit genuinely counts as "serviced" for schedule purposes.
    """
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        connection.execute(
            """
            INSERT INTO service_log (
                equipment_id, person_in_charge, description, performed_at, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                equipment_id,
                person_in_charge,
                description,
                performed_at,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
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


def get_service_history(equipment_id: int | None = None) -> list[dict[str, Any]]:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    query = """
        SELECT
            s.id, s.person_in_charge, s.description, s.performed_at, s.created_at,
            e.display_name AS equipment_display_name
        FROM service_log s
        JOIN equipment e ON e.id = s.equipment_id
    """
    params: tuple[Any, ...] = ()

    if equipment_id is not None:
        query += " WHERE s.equipment_id = ?"
        params = (equipment_id,)

    query += " ORDER BY s.performed_at DESC, s.id DESC"

    try:
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()

    return [dict(row) for row in rows]


def get_simulator_instances() -> list[dict[str, Any]]:
    """
    One row per physical simulated equipment instance (not per tag) -
    backs the temporary Simulator Control page's equipment picker.
    instance_key matches simulator.tag_dataset_model.instance_key()
    exactly, since that's what plc_logger's running TagDatasetSimulator
    looks up commands by.
    """
    from simulator.tag_dataset_model import instance_key as compute_instance_key

    seen: dict[str, dict[str, Any]] = {}

    for tag in get_enabled_tags():
        if tag["data_type"] == "STRING" or tag["tag_name"].count(".") < 3:
            continue

        key = compute_instance_key(tag["tag_name"])

        if key not in seen:
            seen[key] = {
                "instance_key": key,
                "equipment_display_name": tag["equipment_display_name"],
            }

    return sorted(seen.values(), key=lambda row: row["equipment_display_name"])


def send_simulator_command(instance_key: str, action: str) -> None:
    """
    Hands a fault-lifecycle command to whichever process is running
    plc_logger (a separate OS process from this Streamlit app) via a
    small polling table - see TagDatasetSimulator._apply_pending_commands.
    Self-provisions the table so this works even if plc_logger hasn't
    created it yet.
    """
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)

    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS simulator_fault_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_key TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                result TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO simulator_fault_commands (instance_key, action, created_at) VALUES (?, ?, ?)",
            (instance_key, action, datetime.utcnow().isoformat(timespec="seconds")),
        )
        connection.commit()
    finally:
        connection.close()


def get_simulator_command_log(limit: int = 20) -> list[dict[str, Any]]:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS simulator_fault_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_key TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                result TEXT
            )
            """
        )
        rows = connection.execute(
            "SELECT * FROM simulator_fault_commands ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    return [dict(row) for row in rows]
