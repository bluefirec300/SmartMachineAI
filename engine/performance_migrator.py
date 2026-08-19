from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from config.environment import get_config_db_path

"""
Phase 14 - Asset Performance & Reliability Analytics persistence
schema. Two tables, mirroring Phase 12.2's own split (a snapshot table
plus a purpose-specific comparison table):

  asset_performance_observations           APPEND-ONLY. One row per
                                            persisted self-reference
                                            (recent-vs-reference)
                                            performance observation for
                                            one equipment/dimension,
                                            written on material change
                                            or heartbeat - never
                                            updated in place.
  asset_performance_maintenance_comparisons APPEND-ONLY. One row per
                                            persisted before/after
                                            maintenance comparison for
                                            one equipment/dimension/
                                            maintenance_log entry.

This migration creates schema ONLY - zero rows inserted. No Phase 1-13
table/migration is touched (item 49 - additive, migration-safe).

Backup method: SQLite's own Online Backup API (sqlite3.Connection.backup()),
matching Phase 12.x's own precedent - config.db has many concurrent
writers today, so backup() (safe against a database being written to
during the copy) is the conservative choice, not a raw shutil.copy2.
"""

DEFAULT_DATABASE_PATH = get_config_db_path()

CREATE_OBSERVATIONS_SQL = """
CREATE TABLE IF NOT EXISTS asset_performance_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER,
    plant_id INTEGER NOT NULL,
    plant_code TEXT NOT NULL,
    instance_key TEXT NOT NULL,
    equipment_type TEXT NOT NULL,
    target_key TEXT NOT NULL,
    direction TEXT NOT NULL,
    unit TEXT,
    observed_value REAL,
    reference_value REAL,
    absolute_change REAL,
    percent_change REAL,
    performance_state TEXT NOT NULL,
    evidence_quality TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    reference_sample_count INTEGER NOT NULL,
    participates_in_degradation INTEGER NOT NULL,
    consecutive_degrading_observations INTEGER NOT NULL DEFAULT 0,
    sustained_degradation INTEGER NOT NULL DEFAULT 0,
    context_used_json TEXT NOT NULL,
    missing_context_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    model_version TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plant_id) REFERENCES plants(id),
    FOREIGN KEY (equipment_id) REFERENCES equipment(id)
)
"""

CREATE_OBSERVATIONS_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_perf_observations_instance_target_time
ON asset_performance_observations(plant_id, instance_key, target_key, computed_at)
"""

CREATE_OBSERVATIONS_PLANT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_perf_observations_plant_time
ON asset_performance_observations(plant_id, computed_at)
"""

CREATE_MAINTENANCE_COMPARISONS_SQL = """
CREATE TABLE IF NOT EXISTS asset_performance_maintenance_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER,
    plant_id INTEGER NOT NULL,
    plant_code TEXT NOT NULL,
    instance_key TEXT NOT NULL,
    equipment_type TEXT NOT NULL,
    target_key TEXT NOT NULL,
    direction TEXT NOT NULL,
    unit TEXT,
    maintenance_log_id INTEGER NOT NULL,
    performed_at TEXT NOT NULL,
    pre_window_start TEXT,
    pre_window_end TEXT,
    post_window_start TEXT,
    post_window_end TEXT,
    pre_value REAL,
    post_value REAL,
    absolute_change REAL,
    percent_change REAL,
    pre_sample_count INTEGER NOT NULL DEFAULT 0,
    post_sample_count INTEGER NOT NULL DEFAULT 0,
    evidence_quality TEXT NOT NULL,
    effectiveness_result TEXT NOT NULL,
    reason TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    model_version TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plant_id) REFERENCES plants(id),
    FOREIGN KEY (equipment_id) REFERENCES equipment(id),
    FOREIGN KEY (maintenance_log_id) REFERENCES maintenance_log(id)
)
"""

CREATE_MAINTENANCE_COMPARISONS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_perf_maint_comparisons_lookup
ON asset_performance_maintenance_comparisons(plant_id, instance_key, target_key, maintenance_log_id)
"""


def _backup_via_sqlite_api(database: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = database.with_name(f"{database.stem}_before_performance_{stamp}.db")
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
        connection.execute(CREATE_OBSERVATIONS_SQL)
        connection.execute(CREATE_OBSERVATIONS_LOOKUP_INDEX_SQL)
        connection.execute(CREATE_OBSERVATIONS_PLANT_INDEX_SQL)
        connection.execute(CREATE_MAINTENANCE_COMPARISONS_SQL)
        connection.execute(CREATE_MAINTENANCE_COMPARISONS_INDEX_SQL)
        connection.commit()
        print("asset_performance_observations / asset_performance_maintenance_comparisons tables ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the asset_performance_observations / asset_performance_maintenance_comparisons tables.",
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
