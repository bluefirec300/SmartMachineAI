from __future__ import annotations

import fcntl
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path
from engine.health_orchestration import run_cycle

"""
Phase 12.2 - Equipment Health worker. A separate, low/moderate-frequency
service (equipment_health_worker.service), matching every prior
worker's exact conventions (fcntl.flock() single-instance lock,
Restart=always via systemd, PYTHONUNBUFFERED=1). All business logic
(change detection, heartbeat, persistence decisions) lives in
engine/health_orchestration.py - this file only obtains the lock,
loops, calls run_cycle(), logs a concise summary, and sleeps. The
accepted Phase 12.1 scoring engine (engine/health_engine.py) is called
unmodified, several layers down.

TICK_INTERVAL_SECONDS: health is a derived, aggregated signal built
from anomaly/maintenance/event evidence (Phase 8/9 data), not itself
time-critical - anomaly_worker (the fastest evidence producer this
depends on) already runs on a 60s cycle, and individual anomaly state
transitions are gated by multi-bucket persistence periods (5-15 min
each), so scores genuinely cannot change meaningfully faster than
that. 300s (5 min) is chosen as a deliberate middle ground between
opportunity_worker's 900s ("financial projections should not churn")
and anomaly_worker's 60s ("catch new anomalies close to real-time") -
health sits conceptually closer to the former. A full live calculation
pass across all 34 currently-enabled equipment measured 4.3-6.2s (see
FACTORY_AI_DEVELOPMENT_STATUS.md's Phase 12.1/12.2 sections) - two
orders of magnitude below this interval, and persistence itself only
writes on a real material change or the 24h heartbeat, so this tick
rate does not translate into database write pressure.
"""

TICK_INTERVAL_SECONDS = 300.0  # 5 min - see module docstring

LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "equipment_health_worker.lock"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another equipment_health_worker instance already holds {LOCK_FILE_PATH} - "
            "refusing to start a second one. Exiting."
        )
        sys.exit(1)

    return lock_file


def run_forever(config_database_path, machine_database_path, tick_interval: float = TICK_INTERVAL_SECONDS) -> None:
    print(f"Equipment health worker started. Config DB: {config_database_path}. Machine DB: {machine_database_path}. Tick interval: {tick_interval:g}s")

    while True:
        try:
            summary = run_cycle(config_database_path, machine_database_path)
            print(f"cycle: {summary}")
        except Exception as error:
            print(f"equipment_health_worker cycle failed: {error}")

        time.sleep(tick_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path(), get_machine_db_path())
    except KeyboardInterrupt:
        print("\nEquipment health worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
