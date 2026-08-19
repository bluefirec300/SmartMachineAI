from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# Tariffs are append-only/versioned, never overwritten in place - see
# engine/energy_tariff.py's create_tariff() for how a "new" tariff is
# really "close out whichever row is currently open-ended, insert a
# new one starting where it left off". expiry_date IS NULL means
# "currently in effect"; historical calculations look up whichever row
# was open during the date in question, never today's row. mode is
# 'simple' (only currency + energy_rate matter) or 'advanced' (the
# peak/off-peak/demand-charge/surcharge/tax fields also apply) -
# stored as a plain marker rather than two different tables, since
# advanced mode is a superset of simple mode's fields, not a
# different shape.
CREATE_ENERGY_TARIFFS_SQL = """
CREATE TABLE IF NOT EXISTS energy_tariffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER,
    mode TEXT NOT NULL DEFAULT 'simple',
    currency TEXT,
    energy_rate REAL,
    peak_rate REAL,
    off_peak_rate REAL,
    peak_start TEXT,
    peak_end TEXT,
    maximum_demand_charge REAL,
    contract_maximum_demand REAL,
    fixed_monthly_charge REAL,
    surcharge_percent REAL,
    tax_percent REAL,
    billing_cycle TEXT,
    is_simulated INTEGER NOT NULL DEFAULT 0,
    effective_date TEXT NOT NULL,
    expiry_date TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    FOREIGN KEY (plant_id) REFERENCES plants(id)
)
"""


def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(
            f"{database.stem}_before_energy_tariff_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_ENERGY_TARIFFS_SQL)
        connection.commit()
        print("energy_tariffs table ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the energy_tariffs table.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
