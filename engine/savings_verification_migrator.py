from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# Phase 11.1 (Savings Verification domain model) - two tables, per your
# approved architecture adjustment:
#
#   savings_interventions        "what action did the engineer take?"
#   savings_verification_results "what did the verification engine
#                                  conclude at a particular evaluation?"
#                                  (append-only in concept - one row per
#                                  evaluation, never updated in place,
#                                  so an earlier evaluation's financial
#                                  evidence is never destroyed by a
#                                  later one)
#
# A partial unique index enforces "at most one ACTIVE (non-terminal)
# intervention per opportunity" while allowing unlimited terminal-state
# history - the same proven pattern as Phase 9's anomalies /
# Phase 10's energy_opportunities.
#
# This migration creates schema ONLY. It inserts zero rows - Phase 11.1
# explicitly does not generate real intervention or verification data;
# both tables are empty immediately after migration.
CREATE_INTERVENTIONS_SQL = """
CREATE TABLE IF NOT EXISTS savings_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    plant_id INTEGER NOT NULL,
    equipment_id INTEGER,
    instance_key TEXT NOT NULL,
    action_category TEXT NOT NULL,
    action_description TEXT NOT NULL,
    expected_effect TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    implemented_at TEXT,
    stabilization_days INTEGER,
    status TEXT NOT NULL DEFAULT 'PLANNED',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (opportunity_id) REFERENCES energy_opportunities(id),
    FOREIGN KEY (plant_id) REFERENCES plants(id),
    FOREIGN KEY (equipment_id) REFERENCES equipment(id)
)
"""

CREATE_UNIQUE_ACTIVE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_savings_interventions_one_active
ON savings_interventions(opportunity_id)
WHERE status IN ('PLANNED', 'IMPLEMENTED', 'VERIFICATION_PENDING', 'VERIFICATION_IN_PROGRESS')
"""

CREATE_INTERVENTIONS_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_savings_interventions_lookup
ON savings_interventions(plant_id, status)
"""

CREATE_VERIFICATION_RESULTS_SQL = """
CREATE TABLE IF NOT EXISTS savings_verification_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intervention_id INTEGER NOT NULL,
    evaluated_at TEXT NOT NULL,
    reference_period_start TEXT,
    reference_period_end TEXT,
    verification_period_start TEXT,
    verification_period_end TEXT,
    verification_method TEXT,
    result TEXT NOT NULL,
    reason TEXT,
    verified_energy_kwh REAL,
    verified_cost REAL,
    verified_cost_currency TEXT,
    tariff_provenance TEXT,
    confidence TEXT,
    evidence_json TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (intervention_id) REFERENCES savings_interventions(id)
)
"""

CREATE_VERIFICATION_RESULTS_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_savings_verification_results_lookup
ON savings_verification_results(intervention_id, evaluated_at)
"""


def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(
            f"{database.stem}_before_savings_verification_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_INTERVENTIONS_SQL)
        connection.execute(CREATE_UNIQUE_ACTIVE_INDEX_SQL)
        connection.execute(CREATE_INTERVENTIONS_LOOKUP_INDEX_SQL)
        connection.execute(CREATE_VERIFICATION_RESULTS_SQL)
        connection.execute(CREATE_VERIFICATION_RESULTS_LOOKUP_INDEX_SQL)
        connection.commit()
        print("savings_interventions / savings_verification_results tables ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the savings_interventions / savings_verification_results tables.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
