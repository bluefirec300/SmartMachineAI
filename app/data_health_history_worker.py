from __future__ import annotations

import fcntl
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path
from engine.data_health_orchestration import run_cycle
from engine.data_health_persistence_targets import TICK_INTERVAL_SECONDS

"""
Phase 16.5 - Data Health History worker. A separate, low/moderate-
frequency service (data_health_history_worker.service), matching
app/equipment_health_worker.py's exact conventions (fcntl.flock()
single-instance lock, Restart=always via systemd, PYTHONUNBUFFERED=1) -
the reuse approved by the Phase 16.5 architecture checkpoint. All
business logic (change detection, heartbeat, persistence decisions)
lives in engine/data_health_orchestration.py - this file only obtains
the lock, loops, calls run_cycle(), logs a concise summary, and sleeps.
The accepted Phase 16.1 engine (engine/data_health_engine.py) is called
unmodified, several layers down - this worker exists ONLY to decide
WHEN to persist its output, never to recalculate it differently.

This worker does not depend on Streamlit or any page being open - it
runs independently of ui/data_health_fleet_data.py's 120s request-scoped
cache, which remains a SEPARATE, purely presentational cache for
CURRENT-state display. That cache is never used as persistence
infrastructure.

TICK_INTERVAL_SECONDS: see engine/data_health_persistence_targets.py
for the value (300s / 5 min) and its full justification - the same
reasoning app/equipment_health_worker.py's own docstring already
establishes (a full-fleet calculation pass, measured at 6.1-6.2s across
34 equipment in Phase 16.4, is roughly two orders of magnitude below
this interval), with the additional confirmation that
data_health_orchestration.evaluate_and_maybe_persist() only writes on a
real material change or the 24h heartbeat - so this tick rate does not
translate into meaningful database write pressure (see Phase 16.5's own
measured writes/day in FACTORY_AI_DEVELOPMENT_STATUS.md).
"""

LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "data_health_history_worker.lock"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another data_health_history_worker instance already holds {LOCK_FILE_PATH} - "
            "refusing to start a second one. Exiting."
        )
        sys.exit(1)

    return lock_file


def run_forever(config_database_path, machine_database_path, tick_interval: float = TICK_INTERVAL_SECONDS) -> None:
    print(f"Data Health history worker started. Config DB: {config_database_path}. Machine DB: {machine_database_path}. Tick interval: {tick_interval:g}s")

    while True:
        try:
            summary = run_cycle(config_database_path, machine_database_path)
            print(f"cycle: {summary}")
        except Exception as error:
            print(f"data_health_history_worker cycle failed: {error}")

        time.sleep(tick_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path(), get_machine_db_path())
    except KeyboardInterrupt:
        print("\nData Health history worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
