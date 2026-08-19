from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# Phase 6 (Energy KPI Engine) - the only two KPIs judged to have a
# genuine architectural benefit from persistence (see
# engine/energy_kpi_engine.py's module docstring for the full
# reasoning). Every other KPI recomputes on demand from the historian -
# these tables are deliberately small rollups, not a duplicate
# historian.
CREATE_MAXIMUM_DEMAND_SQL = """
CREATE TABLE IF NOT EXISTS energy_kpi_maximum_demand (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER NOT NULL,
    billing_period_start TEXT NOT NULL,
    billing_period_end TEXT NOT NULL,
    demand_interval_minutes INTEGER NOT NULL,
    max_demand_kw REAL NOT NULL,
    occurred_at TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE(plant_id, billing_period_start, demand_interval_minutes),
    FOREIGN KEY (plant_id) REFERENCES plants(id)
)
"""

# One row per plant per finalized (past) calendar day. Never written
# for "today" - only once a day is fully over, so the row is a stable,
# final record rather than something that changes on every read.
CREATE_DAILY_SUMMARY_SQL = """
CREATE TABLE IF NOT EXISTS energy_kpi_daily_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER NOT NULL,
    summary_date TEXT NOT NULL,
    energy_kwh REAL,
    cost REAL,
    currency TEXT,
    production_energy_kwh REAL,
    non_production_energy_kwh REAL,
    avg_demand_kw REAL,
    max_demand_kw REAL,
    demand_interval_minutes INTEGER,
    computed_at TEXT NOT NULL,
    UNIQUE(plant_id, summary_date),
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
            f"{database.stem}_before_energy_kpi_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_MAXIMUM_DEMAND_SQL)
        connection.execute(CREATE_DAILY_SUMMARY_SQL)
        connection.commit()
        print("energy_kpi_maximum_demand/energy_kpi_daily_summary tables ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the energy_kpi_maximum_demand/energy_kpi_daily_summary tables.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
