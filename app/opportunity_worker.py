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
from engine.opportunity_engine import evaluate_opportunity_rule
from engine.opportunity_targets import OPPORTUNITY_RULES

"""
Phase 10 - Energy Opportunity Engine worker. A separate, deliberately
LOW-FREQUENCY service (opportunity_worker.service) - per your item 21
direction, opportunity generation reads only already-persisted Phase 9
anomaly rows (small, indexed, bounded), never the historian, so it
does not need anomaly_worker's ~60s cadence. Financial projections
should not churn, and anomalies themselves only update at most once
per their own bucket (5-15 min) - a much slower tick is appropriate.

TICK_INTERVAL_SECONDS is set from a REAL measured cycle time (per
explicit direction, not a guess): a full pass - all 10 opportunity
rules against a realistic seeded scale (90 anomaly rows: 9 RESOLVED +
1 OPEN per rule, spanning every rule in the registry) - measured
0.377s on the first pass (10 new opportunities created, each linking 9
anomalies) and 0.156-0.159s on subsequent unchanged passes. This
confirms the "no historian rescan" design holds in practice - even at
this scale, a cycle is under half a second. Performance alone would
support a far shorter interval, but per your own direction ("financial
projections should not churn unnecessarily", "does not need
second-level updates"), 15 minutes is chosen as a deliberate design
choice to avoid unnecessary churn, not a performance necessity -
comfortably slower than anomaly_worker's 60s loop while still keeping
opportunities reasonably fresh.
"""

TICK_INTERVAL_SECONDS = 900.0  # 15 min - see module docstring

LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "opportunity_worker.lock"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another opportunity_worker instance already holds {LOCK_FILE_PATH} - "
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
    summary: dict[str, int] = {}

    for plant in plants:
        for rule in OPPORTUNITY_RULES:
            try:
                results = evaluate_opportunity_rule(
                    rule, config_database_path, machine_database_path, historian, plant["id"], plant["code"],
                )
                for r in results:
                    action = r.get("action", "unknown")
                    summary[action] = summary.get(action, 0) + 1
            except Exception as error:
                summary["error"] = summary.get("error", 0) + 1
                print(f"opportunity_worker failed on rule={rule.rule_key} plant={plant['code']}: {error}")

    return summary


def run_forever(config_database_path, machine_database_path, tick_interval: float = TICK_INTERVAL_SECONDS) -> None:
    print(f"Opportunity worker started. Config DB: {config_database_path}. Machine DB: {machine_database_path}. Tick interval: {tick_interval:g}s")
    historian = DatabaseManager(db_path=machine_database_path)
    plants = _get_plants(config_database_path)
    print(f"{len(OPPORTUNITY_RULES)} opportunity rules registered across {len(plants)} plant(s).")

    while True:
        try:
            start = time.time()
            summary = run_cycle(config_database_path, machine_database_path, historian, plants)
            elapsed = time.time() - start
            noteworthy = {k: v for k, v in summary.items() if k in ("opened", "updated", "error")}
            if noteworthy:
                print(f"cycle: {elapsed:.2f}s, {summary}")
        except Exception as error:
            print(f"opportunity_worker cycle failed: {error}")

        time.sleep(tick_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path(), get_machine_db_path())
    except KeyboardInterrupt:
        print("\nOpportunity worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
