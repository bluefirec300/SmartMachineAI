from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# Phase 10 (Energy Opportunity Engine) - one row per opportunity
# CONDITION (plant/instance/rule), not per anomaly occurrence and not
# per worker evaluation. A partial unique index enforces "at most one
# NEW row per exact condition" while allowing unlimited DISMISSED rows
# (dismissal history is preserved, never deleted) - mirrors Phase 9's
# anomalies table exactly, same proven pattern.
CREATE_OPPORTUNITIES_SQL = """
CREATE TABLE IF NOT EXISTS energy_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER NOT NULL,
    equipment_id INTEGER,
    instance_key TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    priority TEXT NOT NULL,
    priority_score REAL NOT NULL,
    priority_breakdown_json TEXT NOT NULL,
    confidence TEXT NOT NULL,
    source_anomaly_ids TEXT NOT NULL,
    source_anomaly_rule_key TEXT NOT NULL,
    first_identified TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    dismissed_at TEXT,
    dismissal_reason TEXT,
    dismissal_comment TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    observed_period_start TEXT,
    observed_period_end TEXT,
    observed_excess_energy_kwh REAL,
    observed_excess_cost REAL,
    observed_cost_currency TEXT,
    saving_basis TEXT NOT NULL,
    saving_unavailable_reason TEXT,
    estimated_potential_saving_period REAL,
    estimated_monthly_saving REAL,
    estimated_annual_saving REAL,
    annualization_method TEXT,
    saving_currency TEXT,
    tariff_provenance TEXT,
    implementation_difficulty TEXT NOT NULL DEFAULT 'UNKNOWN',
    equipment_criticality TEXT,
    recommendation TEXT NOT NULL,
    rule_provenance TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (plant_id) REFERENCES plants(id)
)
"""

CREATE_UNIQUE_NEW_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_energy_opportunities_one_new
ON energy_opportunities(plant_id, instance_key, rule_key)
WHERE status = 'NEW'
"""

CREATE_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_energy_opportunities_lookup
ON energy_opportunities(plant_id, status, priority)
"""


def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(
            f"{database.stem}_before_opportunity_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_OPPORTUNITIES_SQL)
        connection.execute(CREATE_UNIQUE_NEW_INDEX_SQL)
        connection.execute(CREATE_LOOKUP_INDEX_SQL)
        connection.commit()
        print("energy_opportunities table ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the energy_opportunities table.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
