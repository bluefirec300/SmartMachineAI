from __future__ import annotations

import fcntl
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path
from database.database import DatabaseManager
from engine.anomaly_engine import evaluate_rule
from engine.anomaly_targets import ANOMALY_RULES
from engine.baseline_targets import discover_targets

"""
Phase 9 - Anomaly Detection Engine worker. A separate service
(anomaly_worker.service), per explicit direction - keeps engineering-
intelligence evaluation apart from event_monitor.service's alarm
responsibility and baseline_worker.service's baseline-recalculation
responsibility.

MUST NOT call engine.baseline_engine.compute_baseline() (the expensive
live-recompute path) - every read here is either DatabaseManager.get_latest()
(O(1) indexed), a small indexed baseline_context_summary/thresholds
lookup, or a bounded few-bucket historian fetch (never the full
multi-million-row historian). Drift rules self-throttle (they skip
immediately if the underlying persisted baseline snapshot hasn't
changed since the last evaluation), so no separate slow-cadence loop
is needed for them - see engine/anomaly_engine.py's evaluate_drift_rule().

TICK_INTERVAL_SECONDS is set from a REAL measured cycle time against
the live system (per explicit direction, not a guess): a full pass -
every rule (33) against every matching discovered target, across BOTH
plants (134 rule x target evaluations total) - measured 16.4s and
17.7s on two separate runs against the live ~7.35M-row historian
(read-only) paired first with a throwaway config DB copy carrying real
pre-existing baseline_context_summary rows, then with the live
database/simulation/config.db itself (0 baselines persisted yet, since
baseline_worker.service is not installed in this environment - see
task #46). ~0.12-0.13s/evaluation average, 0 errors both runs. A 60s
tick leaves comfortable headroom above the ~17s measured average (as
Phase 8's baseline_worker did for its own tick interval), so the
process is never continuously pegged. See
FACTORY_AI_DEVELOPMENT_STATUS.md's Phase 9 section for the full
measurement writeup.
"""

TICK_INTERVAL_SECONDS = 60.0

LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "anomaly_worker.lock"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another anomaly_worker instance already holds {LOCK_FILE_PATH} - "
            "refusing to start a second one. Exiting."
        )
        sys.exit(1)

    return lock_file


def _get_plants(config_database_path) -> list[dict]:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute("SELECT id, code, name FROM plants ORDER BY code")]
    finally:
        connection.close()


def run_cycle(config_database_path, machine_database_path, historian: DatabaseManager, plants: list[dict]) -> dict[str, int]:
    """One full evaluation pass - every rule against every matching
    target, for every plant. Returns a summary of actions taken, for
    logging/measurement."""
    summary: dict[str, int] = {}

    for plant in plants:
        targets = discover_targets(config_database_path, plant["code"])
        # Multiple instances share (equipment_type, target_key) - build
        # a list, not a single dict entry.
        targets_by_type_and_key: dict[tuple[str, str], list] = {}
        for t in targets:
            targets_by_type_and_key.setdefault((t.equipment_type, t.target_key), []).append(t)

        for rule in ANOMALY_RULES:
            matching = targets_by_type_and_key.get((rule.equipment_type, rule.baseline_target_key), [])
            for target in matching:
                try:
                    result = evaluate_rule(rule, target, config_database_path, machine_database_path, historian, plant["id"])
                    action = result.get("action", "unknown")
                except Exception as error:
                    action = "error"
                    print(f"anomaly_worker failed on {target.instance_key}.{target.target_key} [{rule.rule_key}]: {error}")
                summary[action] = summary.get(action, 0) + 1

    return summary


def run_forever(config_database_path, machine_database_path, tick_interval: float = TICK_INTERVAL_SECONDS) -> None:
    print(f"Anomaly worker started. Config DB: {config_database_path}. Machine DB: {machine_database_path}. Tick interval: {tick_interval:g}s")
    historian = DatabaseManager(db_path=machine_database_path)
    plants = _get_plants(config_database_path)
    rule_count = len(ANOMALY_RULES)
    print(f"{rule_count} rules registered across {len(plants)} plant(s).")

    while True:
        try:
            start = time.time()
            summary = run_cycle(config_database_path, machine_database_path, historian, plants)
            elapsed = time.time() - start
            noteworthy = {k: v for k, v in summary.items() if k in ("opened", "resolved", "updated", "error")}
            if noteworthy:
                print(f"cycle: {elapsed:.2f}s, {summary} ")
        except Exception as error:
            print(f"anomaly_worker cycle failed: {error}")

        time.sleep(tick_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path(), get_machine_db_path())
    except KeyboardInterrupt:
        print("\nAnomaly worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
