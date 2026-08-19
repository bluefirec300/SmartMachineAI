from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from config.environment import get_config_db_path

"""
Phase 12.2 - Equipment Health persistence schema. Two tables, mirroring
Phase 11.1's savings_interventions/savings_verification_results split:

  equipment_health_snapshots         "what was the assessment at this
                                       point in time?" - APPEND-ONLY,
                                       one row per persisted assessment
                                       (initial, on material change, or
                                       heartbeat) - never updated in place.
  equipment_health_factor_snapshots  "what evidence made up that
                                       assessment?" - one row per
                                       registry factor, linked via
                                       health_snapshot_id, written in
                                       the SAME transaction as its
                                       parent (item 9 - atomic).

This migration creates schema ONLY - zero rows inserted. Phase 12.1's
tables/registry are completely untouched (no ALTER, no new column on
any existing table).

Backup method: this migration deliberately uses SQLite's own Online
Backup API (sqlite3.Connection.backup()) rather than the project's
usual shutil.copy2() snapshot - your explicit instruction for Phase
12.x migrations ("use the SQLite backup API... do not raw-copy an
actively written DB"). config.db has many more concurrent writers
today (8 workers + Streamlit) than when the shutil.copy2 convention
was established, so backup() - which is safe against a database being
written to during the copy - is the more conservative choice here.
"""

DEFAULT_DATABASE_PATH = get_config_db_path()

CREATE_HEALTH_SNAPSHOTS_SQL = """
CREATE TABLE IF NOT EXISTS equipment_health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER,
    plant_id INTEGER NOT NULL,
    plant_code TEXT NOT NULL,
    instance_key TEXT NOT NULL,
    equipment_type TEXT NOT NULL,
    health_score REAL,
    health_band TEXT,
    provisional INTEGER NOT NULL DEFAULT 0,
    assessment_confidence TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    applicable_factor_count INTEGER NOT NULL,
    usable_factor_count INTEGER NOT NULL,
    active_finding_count INTEGER NOT NULL,
    recent_finding_count INTEGER NOT NULL,
    criticality TEXT,
    health_model_version TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    missing_factors_json TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plant_id) REFERENCES plants(id),
    FOREIGN KEY (equipment_id) REFERENCES equipment(id)
)
"""

# Supports "latest state per equipment" and "history for one equipment
# over a time range" - the two query shapes engine/health_history.py
# actually issues.
CREATE_SNAPSHOTS_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_health_snapshots_instance_time
ON equipment_health_snapshots(plant_id, instance_key, computed_at)
"""

# Supports "plant-wide latest state" listings (every equipment's most
# recent row) without a per-instance loop.
CREATE_SNAPSHOTS_PLANT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_health_snapshots_plant_time
ON equipment_health_snapshots(plant_id, computed_at)
"""

CREATE_FACTOR_SNAPSHOTS_SQL = """
CREATE TABLE IF NOT EXISTS equipment_health_factor_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    health_snapshot_id INTEGER NOT NULL,
    equipment_id INTEGER,
    factor_id TEXT NOT NULL,
    factor_family TEXT NOT NULL,
    status TEXT NOT NULL,
    penalty REAL NOT NULL,
    maximum_penalty REAL NOT NULL,
    evidence_source TEXT NOT NULL,
    evidence_value TEXT,
    evidence_timestamp TEXT,
    evidence_confidence TEXT,
    reason TEXT NOT NULL,
    provenance TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (health_snapshot_id) REFERENCES equipment_health_snapshots(id)
)
"""

# The only query this table serves: "give me every factor row for
# snapshot N" (a selected-snapshot detail view).
CREATE_FACTOR_SNAPSHOTS_PARENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_health_factor_snapshots_parent
ON equipment_health_factor_snapshots(health_snapshot_id)
"""


def _backup_via_sqlite_api(database: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = database.with_name(f"{database.stem}_before_health_persistence_{stamp}.db")
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
        connection.execute(CREATE_HEALTH_SNAPSHOTS_SQL)
        connection.execute(CREATE_SNAPSHOTS_LOOKUP_INDEX_SQL)
        connection.execute(CREATE_SNAPSHOTS_PLANT_INDEX_SQL)
        connection.execute(CREATE_FACTOR_SNAPSHOTS_SQL)
        connection.execute(CREATE_FACTOR_SNAPSHOTS_PARENT_INDEX_SQL)
        connection.commit()
        print("equipment_health_snapshots / equipment_health_factor_snapshots tables ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the equipment_health_snapshots / equipment_health_factor_snapshots tables.",
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
