from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from config.environment import get_config_db_path

"""
Phase 16.5 - Data Health persistence schema. One append-only table,
mirroring engine/health_migrator.py's own equipment_health_snapshots
convention exactly (same schema-creation/backup/idempotency discipline,
approved for reuse by the Phase 16.5 architecture checkpoint).

data_health_snapshots  "what was the Data Confidence assessment at this
                         point in time?" - APPEND-ONLY, one row per
                         persisted evaluation (initial, on material
                         change, or 24h heartbeat) - never updated in
                         place, never deleted (no retention/pruning is
                         implemented in this phase - see
                         engine/data_health_persistence_targets.py's
                         module docstring for why).

This migration creates schema ONLY - zero rows inserted. No existing
table is touched (no ALTER, no new column anywhere) - Phase 16.1-16.4's
tables and the engine that reads them are completely untouched.

Only DERIVED Data Health state is stored here - no raw plc_data value
is ever copied into this table (the historian remains the sole
authority for raw telemetry) and no free-form explanation prose is
stored as primary data (limitations/reasons stay derivable from the
structured counts already on the row).

Backup method: SQLite's own Online Backup API (safe against a database
being actively written to during the copy) - the same method
engine/health_migrator.py already uses for exactly this reason (many
concurrent writers on config.db today).
"""

DEFAULT_DATABASE_PATH = get_config_db_path()

CREATE_DATA_HEALTH_SNAPSHOTS_SQL = """
CREATE TABLE IF NOT EXISTS data_health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER,
    plant_id INTEGER NOT NULL,
    plant_code TEXT NOT NULL,
    instance_key TEXT NOT NULL,
    equipment_type TEXT,
    confidence_score REAL,
    confidence_status TEXT NOT NULL,
    freshness_score REAL,
    availability_score REAL,
    validity_score REAL,
    continuity_score REAL,
    required_tag_count INTEGER NOT NULL,
    available_tag_count INTEGER NOT NULL,
    fresh_tag_count INTEGER NOT NULL,
    missing_tag_count INTEGER NOT NULL,
    stale_tag_count INTEGER NOT NULL,
    invalid_tag_count INTEGER NOT NULL,
    gap_count INTEGER NOT NULL,
    timestamp_issue_count INTEGER NOT NULL,
    indeterminate_freshness_count INTEGER NOT NULL,
    frozen_candidate_count INTEGER NOT NULL,
    source_driver TEXT,
    data_health_model_version TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    evaluation_error TEXT,
    computed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plant_id) REFERENCES plants(id),
    FOREIGN KEY (equipment_id) REFERENCES equipment(id)
)
"""

# Supports "history for one equipment over a time range" (engine's
# get_history()/status_duration()/status_transitions()/recurring_issues()
# all issue this exact WHERE shape) and "latest snapshot for one
# equipment" (ORDER BY ... DESC LIMIT 1 on the same index).
CREATE_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_data_health_snapshots_instance_time
ON data_health_snapshots(plant_id, instance_key, computed_at)
"""

# Supports "plant-wide latest state" / "plant-wide history" without a
# per-instance loop - the same second index shape
# equipment_health_snapshots already uses for the identical reason.
CREATE_PLANT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_data_health_snapshots_plant_time
ON data_health_snapshots(plant_id, computed_at)
"""


def _backup_via_sqlite_api(database: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = database.with_name(f"{database.stem}_before_data_health_history_{stamp}.db")
    source = sqlite3.connect(database)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup_path


def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        backup_path = _backup_via_sqlite_api(database)
        print(f"Backup created (SQLite backup API): {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_DATA_HEALTH_SNAPSHOTS_SQL)
        connection.execute(CREATE_LOOKUP_INDEX_SQL)
        connection.execute(CREATE_PLANT_INDEX_SQL)
        connection.commit()
        print("data_health_snapshots table ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the data_health_snapshots table.")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
