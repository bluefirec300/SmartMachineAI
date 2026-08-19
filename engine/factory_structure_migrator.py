from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# Roadmap Phase 1 hierarchy: Factory -> Plant -> Area -> System ->
# Equipment -> Tag. Tags/tag_addresses/thresholds and machine_data.db
# are never touched by this migrator - Area/System/Plant are added as
# EQUIPMENT-level classification only, so tags inherit the new
# structure automatically via the existing tags.equipment_id link,
# with zero changes to the tag/historian layer.

CREATE_FACTORY_SQL = """
CREATE TABLE IF NOT EXISTS factory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    company TEXT,
    country TEXT,
    currency TEXT,
    timezone TEXT,
    factory_type TEXT,
    floor_area_m2 REAL,
    created_at TEXT NOT NULL
)
"""

# Deliberately NOT constrained to a single row (no singleton pattern,
# no fixed id=1 assumption) - modeled as a normal entity so multiple
# factories/sites can be added later without a schema change. Phase 1
# seeds exactly one row because exactly one factory currently exists,
# not because the schema requires it.
CREATE_PLANTS_SQL = """
CREATE TABLE IF NOT EXISTS plants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factory_id INTEGER NOT NULL,
    code TEXT NOT NULL UNIQUE,
    name TEXT,
    description TEXT,
    floor_area_m2 REAL,
    production_capacity TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (factory_id) REFERENCES factory(id)
)
"""

# plant_id is part of the identity here (UNIQUE(plant_id, name)) so
# Plant 1's "Utilities Yard" and Plant 2's "Utilities Yard" are two
# distinct rows, never one row shared/ambiguous across plants - per
# explicit direction, systems must never be globally shared between
# plants.
CREATE_AREAS_SQL = """
CREATE TABLE IF NOT EXISTS areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    source TEXT NOT NULL DEFAULT 'inferred_from_simulation',
    created_at TEXT NOT NULL,
    FOREIGN KEY (plant_id) REFERENCES plants(id),
    UNIQUE (plant_id, name)
)
"""

CREATE_SYSTEMS_SQL = """
CREATE TABLE IF NOT EXISTS systems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    source TEXT NOT NULL DEFAULT 'inferred_from_simulation',
    created_at TEXT NOT NULL,
    FOREIGN KEY (area_id) REFERENCES areas(id),
    UNIQUE (area_id, name)
)
"""

# All nullable by design, same discipline as the existing
# equipment_metadata_migrator.py - every consumer must treat "not
# recorded" as normal, not an error. asset_id is deliberately NOT
# duplicated here - equipment.device_number (e.g. "EQ-0001") already
# serves that purpose.
NEW_EQUIPMENT_COLUMNS = {
    "plant_id": "INTEGER REFERENCES plants(id)",
    "area_id": "INTEGER REFERENCES areas(id)",
    "system_id": "INTEGER REFERENCES systems(id)",
    "equipment_type": "TEXT",
    "serial_number": "TEXT",
    "installation_date": "TEXT",
    "commission_date": "TEXT",
    "rated_power": "REAL",
    "rated_voltage": "REAL",
    "rated_current": "REAL",
    "rated_flow": "REAL",
    "rated_pressure": "REAL",
    "rated_capacity": "REAL",
    "criticality": "TEXT",
    "replacement_cost": "REAL",
    "expected_life_years": "REAL",
    "normal_operating_hours": "REAL",
}

# (area_name, system_name) per equipment category - derived from
# ui/scada_floor_plan_data.py's ZONES grouping (the room layout
# already shown on the live SCADA Floor Plan page), with each area's
# categories additionally split into narrower "systems" matching the
# roadmap's own System examples (Compressed Air, Chilled Water, Water
# Supply, Electrical, HVAC, ...).
#
# IMPORTANT - PROVENANCE: this is INFERRED classification, built for
# simulation/demo purposes from the tag-naming convention and the
# SCADA page's existing room layout. It is NOT a confirmed real-
# factory engineering structure. Every areas/systems row seeded from
# this mapping is stamped source='inferred_from_simulation' precisely
# so it can be found and reviewed by a plant engineer later (e.g.
# `SELECT * FROM areas WHERE source = 'inferred_from_simulation'`) -
# do not treat these area/system names as authoritative without that
# review.
CATEGORY_TO_AREA_SYSTEM: dict[str, tuple[str, str]] = {
    "air_compressor": ("Utilities Yard", "Compressed Air"),
    "compressed_air_header": ("Utilities Yard", "Compressed Air"),
    "chiller": ("Utilities Yard", "Chilled Water"),
    "chilled_water_pump": ("Utilities Yard", "Chilled Water"),
    "water_supply_pump": ("Utilities Yard", "Water Supply"),
    "water_supply": ("Utilities Yard", "Water Supply"),
    "main_incomer": ("Electrical Room", "Electrical"),
    "building_incomer": ("Electrical Room", "Electrical"),
    "transformer": ("Electrical Room", "Electrical"),
    "ups": ("Electrical Room", "Electrical"),
    "generator": ("Electrical Room", "Electrical"),
    "cold_room": ("Cold Storage", "Refrigeration"),
    "bead_mill": ("Production Floor", "Production"),
    "high_speed_disperser": ("Production Floor", "Production"),
    "mixer": ("Production Floor", "Production"),
    "filling_machine": ("Production Floor", "Production"),
    "dust_collector": ("Production Floor", "Production"),
    "tank": ("Tank Farm", "Solvent & Material Storage"),
    "solvent_transfer": ("Tank Farm", "Solvent & Material Storage"),
    "water_treatment_system": ("Water Treatment Plant", "Water Treatment"),
    "ro_di_system": ("Water Treatment Plant", "Water Treatment"),
    "effluent_treatment": ("Water Treatment Plant", "Wastewater"),
    "ahu": ("HVAC & Monitoring", "HVAC"),
    "mcc_room": ("HVAC & Monitoring", "Environmental Monitoring"),
    "area_monitoring": ("HVAC & Monitoring", "Environmental Monitoring"),
    "weather_node": ("HVAC & Monitoring", "Environmental Monitoring"),
    "building_water_meter": ("HVAC & Monitoring", "Water Supply"),
    "fire_water_system": ("Fire Safety", "Fire Protection"),
}

# Fallback for any equipment category not present in the mapping above
# (e.g. a future new equipment type added before this dict is updated)
# - never crashes the migration, just lands somewhere clearly labeled
# as needing review rather than silently mis-filed under an unrelated
# area/system.
FALLBACK_AREA_SYSTEM = ("Unclassified", "Unclassified")


def _category_key(equipment_name: str) -> str:
    """p01_cold_room_cr01 -> cold_room (same convention already used
    in ui/scada_floor_plan_data.py and app/ask.py - duplicated here as
    a small local regex rather than importing those modules, which
    pull in streamlit/database dependencies unnecessary for a
    migration script)."""
    match = re.match(r"^p\d+_(.+)_[a-z0-9]+$", equipment_name)
    return match.group(1) if match else equipment_name


def _plant_code(equipment_name: str) -> str | None:
    match = re.match(r"^(p\d+)_", equipment_name)
    return match.group(1) if match else None


def _prettify_category(category: str) -> str:
    """air_compressor -> Air Compressor, for the human-facing
    equipment_type column."""
    return category.replace("_", " ").title()


def _ensure_equipment_columns(connection: sqlite3.Connection) -> list[str]:
    existing = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(equipment)")
    }

    added = []

    for name, sql_type in NEW_EQUIPMENT_COLUMNS.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE equipment ADD COLUMN {name} {sql_type}"
            )
            added.append(name)

    return added


def _seed_factory(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT id FROM factory LIMIT 1").fetchone()

    if row:
        return row["id"]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor = connection.execute(
        """
        INSERT INTO factory (name, company, country, currency, timezone, factory_type, floor_area_m2, created_at)
        VALUES (?, NULL, NULL, NULL, NULL, NULL, NULL, ?)
        """,
        ("MMG Smart Factory", now),
    )
    return cursor.lastrowid


def _seed_plants(connection: sqlite3.Connection, factory_id: int, plant_codes: set[str]) -> dict[str, int]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plant_ids: dict[str, int] = {}

    for code in sorted(plant_codes):
        row = connection.execute(
            "SELECT id FROM plants WHERE code = ?", (code,)
        ).fetchone()

        if row:
            plant_ids[code] = row["id"]
            continue

        cursor = connection.execute(
            """
            INSERT INTO plants (factory_id, code, name, description, floor_area_m2, production_capacity, active, created_at)
            VALUES (?, ?, ?, NULL, NULL, NULL, 1, ?)
            """,
            (factory_id, code, code.upper(), now),
        )
        plant_ids[code] = cursor.lastrowid

    return plant_ids


def _get_or_create_area(connection: sqlite3.Connection, plant_id: int, name: str, now: str) -> int:
    row = connection.execute(
        "SELECT id FROM areas WHERE plant_id = ? AND name = ?", (plant_id, name)
    ).fetchone()

    if row:
        return row["id"]

    cursor = connection.execute(
        """
        INSERT INTO areas (plant_id, name, description, source, created_at)
        VALUES (?, ?, NULL, 'inferred_from_simulation', ?)
        """,
        (plant_id, name, now),
    )
    return cursor.lastrowid


def _get_or_create_system(connection: sqlite3.Connection, area_id: int, name: str, now: str) -> int:
    row = connection.execute(
        "SELECT id FROM systems WHERE area_id = ? AND name = ?", (area_id, name)
    ).fetchone()

    if row:
        return row["id"]

    cursor = connection.execute(
        """
        INSERT INTO systems (area_id, name, description, source, created_at)
        VALUES (?, ?, NULL, 'inferred_from_simulation', ?)
        """,
        (area_id, name, now),
    )
    return cursor.lastrowid


def _backfill_equipment(connection: sqlite3.Connection, plant_ids: dict[str, int]) -> tuple[int, int]:
    """
    Populates plant_id/area_id/system_id/equipment_type for every
    p0N_-prefixed equipment row by parsing its name ONCE here - every
    future read of this classification uses the new columns, not
    name-parsing. Equipment rows with no recognizable p0N_ prefix (the
    7 legacy/orphan rows identified in the system audit) are left with
    NULL plant/area/system, per explicit direction - not guessed at.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = connection.execute("SELECT id, name FROM equipment").fetchall()

    updated = 0
    skipped = 0

    for row in rows:
        plant_code = _plant_code(row["name"])

        if plant_code is None or plant_code not in plant_ids:
            skipped += 1
            continue

        category = _category_key(row["name"])
        area_name, system_name = CATEGORY_TO_AREA_SYSTEM.get(category, FALLBACK_AREA_SYSTEM)

        plant_id = plant_ids[plant_code]
        area_id = _get_or_create_area(connection, plant_id, area_name, now)
        system_id = _get_or_create_system(connection, area_id, system_name, now)

        connection.execute(
            """
            UPDATE equipment
            SET plant_id = ?, area_id = ?, system_id = ?, equipment_type = ?
            WHERE id = ?
            """,
            (plant_id, area_id, system_id, _prettify_category(category), row["id"]),
        )
        updated += 1

    return updated, skipped


def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(
            f"{database.stem}_before_factory_structure_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_FACTORY_SQL)
        connection.execute(CREATE_PLANTS_SQL)
        connection.execute(CREATE_AREAS_SQL)
        connection.execute(CREATE_SYSTEMS_SQL)
        added_columns = _ensure_equipment_columns(connection)
        connection.commit()
        print("factory/plants/areas/systems tables ready.")

        if added_columns:
            print(f"Added equipment columns: {', '.join(added_columns)}")
        else:
            print("All factory-structure equipment columns already exist.")

        factory_id = _seed_factory(connection)
        connection.commit()
        print(f"Factory row id={factory_id} ready.")

        equipment_names = [
            row["name"] for row in connection.execute("SELECT name FROM equipment")
        ]
        plant_codes = {
            code
            for code in (_plant_code(name) for name in equipment_names)
            if code is not None
        }
        plant_ids = _seed_plants(connection, factory_id, plant_codes)
        connection.commit()
        print(f"Plants ready: {plant_ids}")

        updated, skipped = _backfill_equipment(connection, plant_ids)
        connection.commit()
        print(f"Backfilled plant/area/system/equipment_type on {updated} equipment row(s).")
        print(f"Left {skipped} equipment row(s) with no p0N_ prefix unclassified (NULL), as directed.")

        area_count = connection.execute("SELECT COUNT(*) AS c FROM areas").fetchone()["c"]
        system_count = connection.execute("SELECT COUNT(*) AS c FROM systems").fetchone()["c"]
        print(f"Total areas: {area_count} | Total systems: {system_count}")

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add Factory/Plant/Area/System tables and backfill equipment classification.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
