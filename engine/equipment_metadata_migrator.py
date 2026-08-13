from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# Nullable by design - existing equipment rows have none of this info
# yet, and every consumer must treat "not recorded" as a normal state,
# not an error.
NEW_EQUIPMENT_COLUMNS = {
    "device_number": "TEXT",
    "brand": "TEXT",
    "model": "TEXT",
    "service_interval_days": "INTEGER",
    "last_serviced_at": "TEXT",
}


def _ensure_columns(connection: sqlite3.Connection) -> list[str]:
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
            f"{database.stem}_before_equipment_metadata_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        added = _ensure_columns(connection)
        connection.commit()

        if added:
            print(f"Added equipment columns: {', '.join(added)}")
        else:
            print("All equipment metadata columns already exist.")

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add device/brand/model/maintenance columns to equipment.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
