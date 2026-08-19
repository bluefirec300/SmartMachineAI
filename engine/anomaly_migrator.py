from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# Phase 9 (Anomaly Detection Engine) - one row per anomaly OCCURRENCE,
# never one row per worker evaluation (item 20/26). A partial unique
# index enforces "at most one OPEN row per exact condition" while
# allowing unlimited RESOLVED rows for the same condition, so
# recurrence has real history (item 14/11) rather than a mutable
# counter as the sole record.
CREATE_ANOMALIES_SQL = """
CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER NOT NULL,
    equipment_id INTEGER,
    instance_key TEXT NOT NULL,
    target_key TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    anomaly_type TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence TEXT NOT NULL,
    provisional INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    first_detected TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    open_persistence_periods INTEGER NOT NULL DEFAULT 0,
    resolve_persistence_periods INTEGER NOT NULL DEFAULT 0,
    last_bucket_start TEXT,
    actual_value REAL,
    expected_value REAL,
    expected_low REAL,
    expected_high REAL,
    deviation_absolute REAL,
    deviation_percent REAL,
    normalized_deviation REAL,
    baseline_type TEXT,
    baseline_level TEXT,
    baseline_confidence TEXT,
    engineering_limit_status TEXT NOT NULL,
    engineering_limit_event_id INTEGER,
    estimated_excess_energy_kwh REAL,
    estimated_excess_cost REAL,
    estimated_excess_cost_currency TEXT,
    threshold_provenance TEXT NOT NULL,
    source_tags TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    data_limitations_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (plant_id) REFERENCES plants(id)
)
"""

CREATE_UNIQUE_OPEN_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_anomalies_one_open
ON anomalies(plant_id, instance_key, target_key, rule_key)
WHERE status = 'OPEN'
"""

CREATE_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_anomalies_lookup
ON anomalies(plant_id, status, last_seen)
"""


def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(
            f"{database.stem}_before_anomaly_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_ANOMALIES_SQL)
        connection.execute(CREATE_UNIQUE_OPEN_INDEX_SQL)
        connection.execute(CREATE_LOOKUP_INDEX_SQL)
        connection.commit()
        print("anomalies table ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the anomalies table.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
