from __future__ import annotations

import fcntl
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path
from database.database import DatabaseManager
from engine.savings_verification_orchestration import run_cycle

"""
Phase 11.4 - Savings Verification worker. A separate, low-frequency
service (savings_verification_worker.service), matching every prior
worker's exact conventions (fcntl.flock() single-instance lock,
Restart=always via systemd, PYTHONUNBUFFERED=1). All business logic
(eligibility, materially-new-evidence gating, retry policy, lifecycle
transitions) lives in engine/savings_verification_orchestration.py -
this file only obtains the lock, loops, calls run_cycle(), logs a
concise summary, and sleeps.

TICK_INTERVAL_SECONDS is set from real measurement (see
FACTORY_AI_DEVELOPMENT_STATUS.md's Phase 11.4 section) - not a guess,
matching every prior worker's discipline. Verification evidence
matures over hours/days (stabilization_days is measured in whole
days), so this worker's cadence is intentionally much slower than
anomaly_worker/energy_kpi_worker's near-real-time needs - a starting
default of 1 hour, corrected by measurement.
"""

TICK_INTERVAL_SECONDS = 3600.0  # 1 hour - see module docstring; corrected by real measurement below

LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "savings_verification_worker.lock"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another savings_verification_worker instance already holds {LOCK_FILE_PATH} - "
            "refusing to start a second one. Exiting."
        )
        sys.exit(1)

    return lock_file


def run_forever(config_database_path, machine_database_path, tick_interval: float = TICK_INTERVAL_SECONDS) -> None:
    print(f"Savings verification worker started. Config DB: {config_database_path}. Machine DB: {machine_database_path}. Tick interval: {tick_interval:g}s")
    historian = DatabaseManager(db_path=machine_database_path)

    while True:
        try:
            start = time.time()
            summary = run_cycle(config_database_path, machine_database_path, historian)
            elapsed = time.time() - start
            noteworthy = {k: v for k, v in summary.items() if k in ("evaluated", "verified", "rejected", "inconclusive", "failed") and v}
            if noteworthy:
                print(f"cycle: {elapsed:.2f}s, {summary}")
        except Exception as error:
            print(f"savings_verification_worker cycle failed: {error}")

        time.sleep(tick_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path(), get_machine_db_path())
    except KeyboardInterrupt:
        print("\nSavings verification worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
