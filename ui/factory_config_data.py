from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.configuration_manager import ConfigurationManager
from ui.data_access import CONFIG_DATABASE_PATH

"""
Read/write helpers for the Factory Configuration page (Phase 2) -
factory profile, plants, areas, systems, equipment engineering
metadata, shifts, and shutdown/maintenance windows. Kept separate
from ui/data_access.py the same way ui/scada_floor_plan_data.py is,
so that module doesn't grow unbounded as more pages are added.

Every write function takes `username` and records an audit-log entry
via the existing ConfigurationManager.write_audit_log() - same
mechanism Setpoints/Documentation already use, no new audit
infrastructure.
"""


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _audit(username: str, action: str, entity_type: str, entity_name: str, details: str) -> None:
    ConfigurationManager(database_path=CONFIG_DATABASE_PATH).write_audit_log(
        username=username,
        action=action,
        entity_type=entity_type,
        entity_name=entity_name,
        details=details,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

FACTORY_EDITABLE_FIELDS = ("name", "company", "country", "currency", "timezone", "factory_type", "floor_area_m2")


def get_factory() -> dict[str, Any] | None:
    connection = _connect()
    try:
        row = connection.execute("SELECT * FROM factory LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def update_factory(factory_id: int, username: str, **fields: Any) -> None:
    updates = {k: v for k, v in fields.items() if k in FACTORY_EDITABLE_FIELDS}
    if not updates:
        return

    connection = _connect()
    try:
        set_clause = ", ".join(f"{field} = ?" for field in updates)
        connection.execute(
            f"UPDATE factory SET {set_clause} WHERE id = ?",
            (*updates.values(), factory_id),
        )
        connection.commit()
    finally:
        connection.close()

    _audit(username, "update_factory_profile", "factory", str(factory_id), str(updates))


# ---------------------------------------------------------------------------
# Plants
# ---------------------------------------------------------------------------

PLANT_EDITABLE_FIELDS = ("name", "description", "floor_area_m2", "production_capacity", "active")


def get_plants() -> list[dict[str, Any]]:
    connection = _connect()
    try:
        return [dict(r) for r in connection.execute("SELECT * FROM plants ORDER BY code")]
    finally:
        connection.close()


def update_plant(plant_id: int, username: str, **fields: Any) -> None:
    updates = {k: v for k, v in fields.items() if k in PLANT_EDITABLE_FIELDS}
    if not updates:
        return

    connection = _connect()
    try:
        set_clause = ", ".join(f"{field} = ?" for field in updates)
        connection.execute(
            f"UPDATE plants SET {set_clause} WHERE id = ?",
            (*updates.values(), plant_id),
        )
        connection.commit()
    finally:
        connection.close()

    _audit(username, "update_plant", "plant", str(plant_id), str(updates))


# ---------------------------------------------------------------------------
# Areas / Systems
# ---------------------------------------------------------------------------
# Editing (or creating) a row here always sets source='confirmed' -
# an area/system a human typed in or corrected is no longer "merely
# inferred", per the explicit Phase 2 requirement. Only the original
# Phase 1 seeding ever writes 'inferred_from_simulation'.

def get_areas_for_plant(plant_id: int) -> list[dict[str, Any]]:
    connection = _connect()
    try:
        return [
            dict(r)
            for r in connection.execute(
                "SELECT * FROM areas WHERE plant_id = ? ORDER BY name", (plant_id,)
            )
        ]
    finally:
        connection.close()


def get_systems_for_area(area_id: int) -> list[dict[str, Any]]:
    connection = _connect()
    try:
        return [
            dict(r)
            for r in connection.execute(
                "SELECT * FROM systems WHERE area_id = ? ORDER BY name", (area_id,)
            )
        ]
    finally:
        connection.close()


def update_area(area_id: int, name: str, description: str | None, username: str) -> None:
    connection = _connect()
    try:
        connection.execute(
            "UPDATE areas SET name = ?, description = ?, source = 'confirmed' WHERE id = ?",
            (name, description, area_id),
        )
        connection.commit()
    finally:
        connection.close()

    _audit(username, "update_area", "area", name, "marked source=confirmed")


def create_area(plant_id: int, name: str, description: str | None, username: str) -> int:
    connection = _connect()
    try:
        cursor = connection.execute(
            "INSERT INTO areas (plant_id, name, description, source, created_at) VALUES (?, ?, ?, 'confirmed', ?)",
            (plant_id, name, description, _now()),
        )
        connection.commit()
        area_id = cursor.lastrowid
    finally:
        connection.close()

    _audit(username, "create_area", "area", name, f"plant_id={plant_id}")
    return area_id


def update_system(system_id: int, name: str, description: str | None, username: str) -> None:
    connection = _connect()
    try:
        connection.execute(
            "UPDATE systems SET name = ?, description = ?, source = 'confirmed' WHERE id = ?",
            (name, description, system_id),
        )
        connection.commit()
    finally:
        connection.close()

    _audit(username, "update_system", "system", name, "marked source=confirmed")


def create_system(area_id: int, name: str, description: str | None, username: str) -> int:
    connection = _connect()
    try:
        cursor = connection.execute(
            "INSERT INTO systems (area_id, name, description, source, created_at) VALUES (?, ?, ?, 'confirmed', ?)",
            (area_id, name, description, _now()),
        )
        connection.commit()
        system_id = cursor.lastrowid
    finally:
        connection.close()

    _audit(username, "create_system", "system", name, f"area_id={area_id}")
    return system_id


# ---------------------------------------------------------------------------
# Equipment engineering metadata
# ---------------------------------------------------------------------------

EQUIPMENT_METADATA_FIELDS = (
    "device_number", "equipment_type", "brand", "model", "serial_number",
    "installation_date", "commission_date", "rated_power", "rated_voltage",
    "rated_current", "rated_flow", "rated_pressure", "rated_capacity",
    "criticality", "replacement_cost", "expected_life_years",
    "normal_operating_hours", "operating_pattern",
)


def get_classified_equipment(plant_id: int | None = None) -> list[dict[str, Any]]:
    """Equipment with a real plant/area/system link (excludes the 7
    legacy/orphan rows left NULL in Phase 1 - they have no engineering
    metadata surface to edit here)."""
    connection = _connect()
    try:
        query = """
            SELECT e.*, p.code AS plant_code, a.name AS area_name, s.name AS system_name
            FROM equipment e
            JOIN plants p ON p.id = e.plant_id
            LEFT JOIN areas a ON a.id = e.area_id
            LEFT JOIN systems s ON s.id = e.system_id
            WHERE e.plant_id IS NOT NULL
        """
        params: tuple = ()
        if plant_id is not None:
            query += " AND e.plant_id = ?"
            params = (plant_id,)
        query += " ORDER BY e.display_name"

        return [dict(r) for r in connection.execute(query, params)]
    finally:
        connection.close()


def update_equipment_metadata(equipment_id: int, username: str, **fields: Any) -> None:
    updates = {k: v for k, v in fields.items() if k in EQUIPMENT_METADATA_FIELDS}
    if not updates:
        return

    connection = _connect()
    try:
        set_clause = ", ".join(f"{field} = ?" for field in updates)
        connection.execute(
            f"UPDATE equipment SET {set_clause} WHERE id = ?",
            (*updates.values(), equipment_id),
        )
        connection.commit()
    finally:
        connection.close()

    _audit(username, "update_equipment_metadata", "equipment", str(equipment_id), str(updates))


# ---------------------------------------------------------------------------
# Shift definitions
# ---------------------------------------------------------------------------

def get_shifts(plant_id: int | None) -> list[dict[str, Any]]:
    """plant_id=None returns factory-wide default shifts (plant_id IS NULL)."""
    connection = _connect()
    try:
        if plant_id is None:
            rows = connection.execute(
                "SELECT * FROM shift_definitions WHERE plant_id IS NULL ORDER BY start_time"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM shift_definitions WHERE plant_id = ? ORDER BY start_time", (plant_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def create_shift(
    plant_id: int | None, name: str, start_time: str, end_time: str,
    days_of_week: str, username: str,
) -> int:
    connection = _connect()
    try:
        cursor = connection.execute(
            """
            INSERT INTO shift_definitions (plant_id, name, start_time, end_time, days_of_week, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (plant_id, name, start_time, end_time, days_of_week, _now()),
        )
        connection.commit()
        shift_id = cursor.lastrowid
    finally:
        connection.close()

    _audit(username, "create_shift", "shift_definition", name, f"plant_id={plant_id}, {start_time}-{end_time}, {days_of_week}")
    return shift_id


def delete_shift(shift_id: int, username: str) -> None:
    connection = _connect()
    try:
        row = connection.execute("SELECT name FROM shift_definitions WHERE id = ?", (shift_id,)).fetchone()
        connection.execute("DELETE FROM shift_definitions WHERE id = ?", (shift_id,))
        connection.commit()
    finally:
        connection.close()

    _audit(username, "delete_shift", "shift_definition", row["name"] if row else str(shift_id), "")


# ---------------------------------------------------------------------------
# Non-production periods (shutdowns / maintenance windows / holidays)
# ---------------------------------------------------------------------------

def get_non_production_periods(plant_id: int | None) -> list[dict[str, Any]]:
    connection = _connect()
    try:
        if plant_id is None:
            rows = connection.execute(
                "SELECT * FROM non_production_periods WHERE plant_id IS NULL ORDER BY start_datetime DESC"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM non_production_periods WHERE plant_id = ? ORDER BY start_datetime DESC", (plant_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def create_non_production_period(
    plant_id: int | None, period_type: str, start_datetime: str, end_datetime: str,
    description: str | None, username: str,
) -> int:
    connection = _connect()
    try:
        cursor = connection.execute(
            """
            INSERT INTO non_production_periods (plant_id, period_type, start_datetime, end_datetime, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (plant_id, period_type, start_datetime, end_datetime, description, _now()),
        )
        connection.commit()
        period_id = cursor.lastrowid
    finally:
        connection.close()

    _audit(username, "create_non_production_period", "non_production_period", period_type, f"plant_id={plant_id}, {start_datetime} - {end_datetime}")
    return period_id


def delete_non_production_period(period_id: int, username: str) -> None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT period_type FROM non_production_periods WHERE id = ?", (period_id,)
        ).fetchone()
        connection.execute("DELETE FROM non_production_periods WHERE id = ?", (period_id,))
        connection.commit()
    finally:
        connection.close()

    _audit(username, "delete_non_production_period", "non_production_period", row["period_type"] if row else str(period_id), "")
