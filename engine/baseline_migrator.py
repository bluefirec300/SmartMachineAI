from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# Phase 8 (Baseline Engine) - per-context-bucket summaries only, never
# one row per raw historian sample. One row per
# (plant, target, context bucket, reference-vs-recent). See
# engine/baseline_engine.py's module docstring for the full design.
CREATE_BASELINE_CONTEXT_SUMMARY_SQL = """
CREATE TABLE IF NOT EXISTS baseline_context_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER NOT NULL,
    instance_key TEXT NOT NULL,
    target_key TEXT NOT NULL,
    equipment_type TEXT NOT NULL,
    baseline_type TEXT NOT NULL,          -- 'reference' | 'recent'
    context_bucket_key TEXT NOT NULL,
    baseline_level TEXT NOT NULL,         -- 'A' | 'B' | 'C' | 'D'
    baseline_status TEXT NOT NULL,        -- 'unavailable' | 'bootstrap' | 'mature'
    confidence TEXT,                      -- 'Insufficient' | 'Low' | 'Medium' | 'High'
    median_value REAL,
    range_low REAL,
    range_high REAL,
    mad REAL,
    range_low_pct INTEGER,
    range_high_pct INTEGER,
    representative_sample_count INTEGER,
    raw_sample_count INTEGER,
    distinct_days INTEGER,
    diversity_dimensions INTEGER,
    history_start TEXT,
    history_end TEXT,
    aggregation_minutes INTEGER,
    context_json TEXT,                    -- context dict + assumptions + missing_context
    computed_at TEXT NOT NULL,
    UNIQUE(plant_id, instance_key, target_key, baseline_type, context_bucket_key),
    FOREIGN KEY (plant_id) REFERENCES plants(id)
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_baseline_context_summary_lookup
ON baseline_context_summary(plant_id, instance_key, target_key, baseline_type)
"""


def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(
            f"{database.stem}_before_baseline_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_BASELINE_CONTEXT_SUMMARY_SQL)
        connection.execute(CREATE_INDEX_SQL)
        connection.commit()
        print("baseline_context_summary table ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the baseline_context_summary table.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
