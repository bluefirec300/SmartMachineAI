from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"

MAINTENANCE_CATEGORIES = (
    "Preventive Maintenance",
    "Repair",
    "Replacement",
    "Inspection",
    "Calibration",
)

CREATE_MAINTENANCE_LOG_SQL = """
CREATE TABLE IF NOT EXISTS maintenance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    parts_replaced TEXT,
    performed_by TEXT,
    performed_at TEXT NOT NULL,
    next_due_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (equipment_id)
        REFERENCES equipment(id)
        ON DELETE CASCADE
)
"""

# Nullable override - when a logged entry specifies a next-due date
# directly (e.g. "next oil change in 6 months"), it takes priority
# over the fixed service_interval_days + last_serviced_at estimate.
NEW_EQUIPMENT_COLUMNS = {
    "next_due_at": "TEXT",
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
            f"{database.stem}_before_maintenance_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_MAINTENANCE_LOG_SQL)
        added = _ensure_equipment_columns(connection)
        connection.commit()

        print("maintenance_log table ready.")

        if added:
            print(f"Added equipment columns: {', '.join(added)}")
        else:
            print("All maintenance-related equipment columns already exist.")

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the maintenance_log table and supporting equipment columns.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
