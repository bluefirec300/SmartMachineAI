from __future__ import annotations

import fcntl
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path
from simulator.production_batch_simulator import tick

"""
Standalone background process generating simulated production batch
activity (Phase 4 - Production Context), installed as
production_simulator.service (systemd) - always-on, independent of
whether anyone has the UI open, per explicit direction.

Single-instance guarantee: holds an exclusive, non-blocking flock() on
logs/production_simulator.lock for its entire lifetime. If a second
instance is ever started (e.g. someone runs this manually while the
systemd service is already up), the flock acquisition fails
immediately and the process exits rather than running a second
concurrent generator against the same database - flock is held by the
OS and is automatically released if the process dies for any reason
(crash, kill -9, `systemctl restart`), so there's no stale-lock-file
cleanup step needed the way a plain PID file would require.

Restart safety: simulator.production_batch_simulator.tick() never
relies on in-memory state - every "does this equipment already have a
running batch" decision queries the database directly. A fresh process
instance (after a restart) therefore sees exactly the same reality a
long-running one would and can never start a duplicate concurrent
batch - see that module's docstring for the full reasoning.
"""

TICK_INTERVAL_SECONDS = 30.0
LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "production_simulator.lock"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another production_simulator instance already holds {LOCK_FILE_PATH} - "
            "refusing to start a second one. Exiting."
        )
        sys.exit(1)

    return lock_file  # kept open/referenced for the life of the process - closing releases the lock


def run_forever(database_path: str | Path, poll_interval: float = TICK_INTERVAL_SECONDS) -> None:
    print(f"Production simulator started. Database: {database_path}. Tick interval: {poll_interval:g}s")

    while True:
        try:
            result = tick(database_path)
            if result["started"] or result["completed"] or result["interrupted"] or result["cancelled"]:
                print(
                    f"tick: started={result['started']} progressed={result['progressed']} "
                    f"completed={result['completed']} interrupted={result['interrupted']} "
                    f"cancelled={result['cancelled']}"
                )
        except Exception as error:
            # One bad tick must never kill the process permanently - same
            # discipline app/plc_logger.py's per-tag try/except already follows.
            print(f"production_simulator tick failed: {error}")

        time.sleep(poll_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path())
    except KeyboardInterrupt:
        print("\nProduction simulator stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
