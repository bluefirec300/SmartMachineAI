from __future__ import annotations

import fcntl
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path
from database.database import DatabaseManager
from engine.performance_orchestration import build_rotation, process_one_work_item

"""
Phase 14 - Asset Performance & Reliability Analytics worker. A separate
service (asset_performance_worker.service), following
app/baseline_worker.py's OWN established round-robin pattern exactly -
NOT app/equipment_health_worker.py's full-sweep-per-cycle pattern (see
engine/performance_orchestration.py's own docstring on this choice).
This matters because Phase 14's per-target cost is dominated by raw
historian reads via the same engine.baseline_engine.compute_window_baseline()
baseline_worker itself calls - measured ~2.5-5s/target against the live
historian (two windows per target, vs. baseline_worker's one) - NOT
Phase 12.2's cheap already-persisted-evidence reads. Processing the
full ~80-100 item rotation in one tick would keep this process "always
busy" and compete with baseline_worker/anomaly_worker for historian
bandwidth.

fcntl.flock() single-instance lock, Restart=always via systemd,
PYTHONUNBUFFERED=1 - the same conventions every prior worker follows.
All business logic (materiality gating, degradation-persistence
tracking, maintenance-comparison eligibility) lives in
engine/performance_orchestration.py - this file only builds the
rotation, loops one item per tick, logs a concise outcome, and sleeps.

TICK_INTERVAL_SECONDS = 5.0, matching baseline_worker's own value - the
same per-item cost profile justifies the same interval. A full
rotation (~80-100 items across both plants: ~82 observation targets +
however many maintenance-anchored comparisons currently exist) takes
several minutes, spread out - never one big sweep - consistent with
Phase 14's own backward-looking, non-time-critical nature (a reference
window spans days; nothing here needs sub-minute freshness).
"""

TICK_INTERVAL_SECONDS = 5.0

LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "asset_performance_worker.lock"

# How often the rotation itself is rebuilt (new equipment, new
# maintenance_log entries, newly-eligible targets) - independent of how
# often each individual item is processed.
ROTATION_REBUILD_INTERVAL_SECONDS = 3600.0


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another asset_performance_worker instance already holds {LOCK_FILE_PATH} - "
            "refusing to start a second one. Exiting."
        )
        sys.exit(1)

    return lock_file


def run_forever(config_database_path, machine_database_path, tick_interval: float = TICK_INTERVAL_SECONDS) -> None:
    print(f"Asset performance worker started. Config DB: {config_database_path}. Machine DB: {machine_database_path}. Tick interval: {tick_interval:g}s")
    historian = DatabaseManager(db_path=machine_database_path)

    rotation = build_rotation(config_database_path)
    print(f"Registered {len(rotation)} work item(s) across every plant.")
    last_rebuild = time.time()

    index = 0
    while True:
        if time.time() - last_rebuild >= ROTATION_REBUILD_INTERVAL_SECONDS:
            rotation = build_rotation(config_database_path)
            last_rebuild = time.time()
            index = 0
            print(f"Rotation rebuilt: {len(rotation)} work item(s).")

        if not rotation:
            time.sleep(tick_interval)
            continue

        work_item = rotation[index % len(rotation)]
        index += 1

        try:
            outcome = process_one_work_item(config_database_path, machine_database_path, historian, work_item, now=datetime.now())
            print(f"{work_item.kind}: {work_item.target.instance_key}.{work_item.target.target_key}: {outcome.get('action')}")
        except Exception as error:
            # One bad item must never kill the process permanently -
            # same discipline every prior worker follows.
            print(f"asset_performance_worker failed on {work_item.kind} {work_item.target.instance_key}.{work_item.target.target_key}: {error}")

        time.sleep(tick_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path(), get_machine_db_path())
    except KeyboardInterrupt:
        print("\nAsset performance worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
