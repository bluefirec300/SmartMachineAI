from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# plant_id nullable = a factory-wide default template, not tied to one
# plant - mirrors the same nullable-plant convention used for
# non_production_periods below. days_of_week is a plain comma-
# separated string ("Mon,Tue,Wed,Thu,Fri") rather than a bitmask -
# there's no query that needs bitwise filtering yet, and a plain
# string is trivial to read back in the UI without a decode step.
CREATE_SHIFT_DEFINITIONS_SQL = """
CREATE TABLE IF NOT EXISTS shift_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER,
    name TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    days_of_week TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plant_id) REFERENCES plants(id)
)
"""

# Covers planned shutdowns, maintenance windows, and holidays under
# one table (period_type distinguishes them) rather than three - all
# three share the exact same shape (a labeled time range, optionally
# plant-scoped), so a separate table per type would just be the same
# columns three times over.
CREATE_NON_PRODUCTION_PERIODS_SQL = """
CREATE TABLE IF NOT EXISTS non_production_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER,
    period_type TEXT NOT NULL,
    start_datetime TEXT NOT NULL,
    end_datetime TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plant_id) REFERENCES plants(id)
)
"""

# Nullable by design, same discipline as every other additive
# migration in this project. Distinguishes "this equipment is expected
# to run continuously regardless of shift schedule" (utilities like
# chillers/UPS/fire water systems) from equipment that follows its
# plant's shift_definitions - deliberately NOT a per-equipment shift
# assignment (that would be real scheduling-engine complexity out of
# scope for Phase 2, which only captures the data shape needed for
# later after-hours-energy analytics, per the Phase 2 plan).
NEW_EQUIPMENT_COLUMNS = {
    "operating_pattern": "TEXT",
}


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


def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(
            f"{database.stem}_before_operating_schedule_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_SHIFT_DEFINITIONS_SQL)
        connection.execute(CREATE_NON_PRODUCTION_PERIODS_SQL)
        added = _ensure_equipment_columns(connection)
        connection.commit()

        print("shift_definitions/non_production_periods tables ready.")

        if added:
            print(f"Added equipment columns: {', '.join(added)}")
        else:
            print("All operating-schedule equipment columns already exist.")

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create shift_definitions/non_production_periods tables and the equipment.operating_pattern column.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
