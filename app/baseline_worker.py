from __future__ import annotations

import fcntl
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path
from database.database import DatabaseManager
from engine.baseline_engine import TIME_FORMAT, compute_baseline, context_bucket_key
from engine.baseline_targets import discover_targets

"""
Phase 8 - Baseline Engine worker. A separate service
(baseline_worker.service), per explicit direction - kept apart from
event_monitor.service/energy_kpi_worker.service, same "one
responsibility per process" principle Phase 6 established.

Per explicit direction: the dashboard/request path must NEVER trigger
a multi-million-row baseline rebuild. This worker processes exactly
ONE target per tick (round-robin across every plant's registered
targets), each tick computing a fresh compute_baseline() call (which
internally bounds its own historian reads - see
engine/baseline_engine.py's MODERN_DATA_BOUNDARY and per-window sample
limits) and persisting the result. A full rotation through the entire
registry takes many ticks, spread out - never one big sweep.

Because each cycle computes "the baseline for whatever context is
current right now," and context (hour-of-day, load level, ambient
band, running product...) naturally varies tick to tick, the
persisted baseline_context_summary table accumulates coverage of
different context buckets organically over time, rather than needing
a separate exhaustive-enumeration pass up front.

TICK_INTERVAL_SECONDS is set from a REAL measured pass timing against
the live historian (see FACTORY_AI_DEVELOPMENT_STATUS.md's Phase 8
section for the actual measurement) - not an assumed/guessed value.
"""

# Measured live against the real ~7.35M-row historian before picking
# this value (per explicit direction, not a guess): a full pass over
# one plant's 84 registered targets took 213.3s (2.54s/target average,
# 0 errors) - confirming a full sweep genuinely cannot run on any
# request path. 5s per tick leaves headroom above that average so the
# process isn't continuously pegged, while a full two-plant rotation
# (168 targets) still completes in ~14 minutes, keeping "recent"
# baselines reasonably fresh without hammering the historian.
TICK_INTERVAL_SECONDS = 5.0

LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "baseline_worker.lock"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another baseline_worker instance already holds {LOCK_FILE_PATH} - "
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


def _persist(config_database_path, plant_id: int, result: dict, baseline_type: str, window: dict, now: datetime) -> None:
    import json

    bucket_key = context_bucket_key(result["context_used"])
    context_payload = json.dumps({
        "context_used": result["context_used"],
        "missing_context": result["missing_context"],
        "assumptions": result["assumptions"],
    })

    connection = sqlite3.connect(config_database_path)
    try:
        connection.execute(
            """
            INSERT INTO baseline_context_summary (
                plant_id, instance_key, target_key, equipment_type, baseline_type, context_bucket_key,
                baseline_level, baseline_status, confidence, median_value, range_low, range_high, mad,
                range_low_pct, range_high_pct, representative_sample_count, raw_sample_count, distinct_days,
                diversity_dimensions, history_start, history_end, aggregation_minutes, context_json, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plant_id, instance_key, target_key, baseline_type, context_bucket_key) DO UPDATE SET
                baseline_level = excluded.baseline_level, baseline_status = excluded.baseline_status,
                confidence = excluded.confidence, median_value = excluded.median_value,
                range_low = excluded.range_low, range_high = excluded.range_high, mad = excluded.mad,
                representative_sample_count = excluded.representative_sample_count,
                raw_sample_count = excluded.raw_sample_count, distinct_days = excluded.distinct_days,
                diversity_dimensions = excluded.diversity_dimensions, history_start = excluded.history_start,
                history_end = excluded.history_end, context_json = excluded.context_json, computed_at = excluded.computed_at
            """,
            (
                plant_id, result["instance_key"], result["target_key"], result["equipment_type"], baseline_type, bucket_key,
                result["baseline_level"], result["baseline_status"], result["confidence"], result["expected_value"],
                result["expected_low"], result["expected_high"], result["variability"],
                None, None,  # range_low_pct/range_high_pct - informational only, engine uses one central default
                result["sample_count"], result["raw_sample_count"], result["distinct_days"],
                0, window.get("start"), window.get("end"), None, context_payload, now.strftime(TIME_FORMAT),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def process_one_target(config_database_path, historian, target, plant_id, now: datetime) -> None:
    result = compute_baseline(target, historian, config_database_path, actual_value=None, now=now)

    _persist(config_database_path, plant_id, result, "recent", result["history_window"], now)

    rvr = result["reference_vs_recent"]
    if not rvr["windows_overlap"]:
        # A distinct reference-window result exists - persist it too,
        # under its own baseline_type, using the reference window's
        # own bounds (not the recent window's).
        reference_like = dict(result)
        reference_like["expected_value"] = rvr["reference_expected_value"]
        reference_like["baseline_status"] = rvr["reference_status"]
        reference_like["confidence"] = rvr["reference_confidence"]
        reference_like["distinct_days"] = rvr["reference_distinct_days"]
        _persist(config_database_path, plant_id, reference_like, "reference", result["history_window"], now)


def run_forever(config_database_path, machine_database_path, tick_interval: float = TICK_INTERVAL_SECONDS) -> None:
    print(f"Baseline worker started. Config DB: {config_database_path}. Machine DB: {machine_database_path}. Tick interval: {tick_interval:g}s")
    historian = DatabaseManager(db_path=machine_database_path)

    plants = _get_plants(config_database_path)
    rotation: list[tuple[dict, Any]] = []
    for plant in plants:
        for target in discover_targets(config_database_path, plant["code"]):
            rotation.append((plant, target))

    print(f"Registered {len(rotation)} (plant, target) pairs across {len(plants)} plant(s).")

    index = 0
    while True:
        if not rotation:
            time.sleep(tick_interval)
            continue

        plant, target = rotation[index % len(rotation)]
        index += 1

        try:
            process_one_target(config_database_path, historian, target, plant["id"], datetime.now())
        except Exception as error:
            # One bad target must never kill the process permanently -
            # same discipline every prior worker follows.
            print(f"baseline_worker failed on {target.instance_key}.{target.target_key}: {error}")

        time.sleep(tick_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path(), get_machine_db_path())
    except KeyboardInterrupt:
        print("\nBaseline worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
