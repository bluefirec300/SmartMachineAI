from __future__ import annotations

import fcntl
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path
from database.database import DatabaseManager
from engine.energy_kpi_engine import (
    DEFAULT_DEMAND_INTERVAL_MINUTES,
    TIME_FORMAT,
    cost_for_period,
    energy_for_period,
    estimated_maximum_demand,
    production_non_production_split,
)

"""
Standalone background process for Phase 6's two persisted KPIs -
Maximum Demand tracking and the daily summary rollup (see
engine/energy_kpi_migrator.py and energy_kpi_engine.py's module
docstring for why only these two are persisted). Installed as its own
energy_kpi_worker.service, deliberately SEPARATE from
event_monitor.service per explicit direction - alarm/event processing
and energy accounting are kept as separate responsibilities, even
though both read plc_data continuously.

Single-instance guarantee and restart-safety follow the exact same
pattern as app/production_simulator.py: an exclusive non-blocking
flock() for the process lifetime, and every tick recomputes its
current state fresh from the database rather than trusting in-memory
state - a restart can never lose or duplicate a Maximum Demand record,
since each tick's "is this a new record?" check is a comparison
against what's already persisted, not an accumulation that depends on
having run continuously.
"""

TICK_INTERVAL_SECONDS = 60.0
LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "energy_kpi_worker.lock"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another energy_kpi_worker instance already holds {LOCK_FILE_PATH} - "
            "refusing to start a second one. Exiting."
        )
        sys.exit(1)

    return lock_file


def _get_plants(config_database_path) -> list[dict]:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute("SELECT id, code, name FROM plants")]
    finally:
        connection.close()


def _billing_period(now: datetime) -> tuple[datetime, datetime]:
    """Calendar-month billing period - a documented simplification, not
    a reproduction of any specific utility's actual billing-cycle start
    date (tariff.billing_cycle is a free-text label today, not a
    structured cycle-start-day the engine parses)."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        next_start = start.replace(year=start.year + 1, month=1)
    else:
        next_start = start.replace(month=start.month + 1)
    return start, next_start


def update_maximum_demand(
    config_database_path, historian: DatabaseManager, plant: dict, now: datetime,
    interval_minutes: int = DEFAULT_DEMAND_INTERVAL_MINUTES,
) -> None:
    """
    One tick's worth of Maximum Demand bookkeeping: recompute the
    highest rolling interval_minutes average seen so far THIS billing
    period, and persist it only if it's a new record. Deliberately
    rescans the whole billing-period-to-date each tick (via
    estimated_maximum_demand(), which is itself an O(n) sliding-window
    scan) rather than trying to maintain incremental state across
    ticks - simpler, and correct across a restart by construction,
    since there's no in-memory state to lose.
    """
    period_start, period_end = _billing_period(now)
    window_end = min(now, period_end)

    result = estimated_maximum_demand(historian, plant["code"], period_start, window_end, interval_minutes)
    if result["classification"] == "UNAVAILABLE":
        return

    connection = sqlite3.connect(config_database_path)
    try:
        existing = connection.execute(
            "SELECT max_demand_kw FROM energy_kpi_maximum_demand "
            "WHERE plant_id = ? AND billing_period_start = ? AND demand_interval_minutes = ?",
            (plant["id"], period_start.strftime(TIME_FORMAT), interval_minutes),
        ).fetchone()

        if existing is not None and existing[0] >= result["value"]:
            return  # not a new record - nothing to persist

        computed_at = now.strftime(TIME_FORMAT)
        connection.execute(
            """
            INSERT INTO energy_kpi_maximum_demand (
                plant_id, billing_period_start, billing_period_end, demand_interval_minutes,
                max_demand_kw, occurred_at, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plant_id, billing_period_start, demand_interval_minutes) DO UPDATE SET
                max_demand_kw = excluded.max_demand_kw,
                occurred_at = excluded.occurred_at,
                computed_at = excluded.computed_at
            """,
            (
                plant["id"], period_start.strftime(TIME_FORMAT), period_end.strftime(TIME_FORMAT), interval_minutes,
                result["value"], computed_at, computed_at,
            ),
        )
        connection.commit()
        print(f"[{plant['code']}] new Estimated Maximum Demand record: {result['value']} kW")
    finally:
        connection.close()


def finalize_daily_summaries(
    config_database_path, historian: DatabaseManager, plants: list[dict], now: datetime,
    interval_minutes: int = DEFAULT_DEMAND_INTERVAL_MINUTES,
) -> None:
    """
    Writes yesterday's summary row once it's fully over - never
    "today's" row, so a stored row is always a stable, finalized
    figure rather than something that changes on every read. Skips any
    plant/date pair that already has a row (idempotent - safe to call
    every tick without duplicating work or rows).
    """
    yesterday = (now - timedelta(days=1)).date()
    day_start = datetime(yesterday.year, yesterday.month, yesterday.day)
    day_end = day_start + timedelta(days=1)

    for plant in plants:
        connection = sqlite3.connect(config_database_path)
        try:
            already_done = connection.execute(
                "SELECT 1 FROM energy_kpi_daily_summary WHERE plant_id = ? AND summary_date = ?",
                (plant["id"], yesterday.isoformat()),
            ).fetchone()
        finally:
            connection.close()

        if already_done:
            continue

        energy = energy_for_period(historian, plant["code"], day_start, day_end)
        if energy["classification"] == "UNAVAILABLE":
            continue  # not enough data yet for yesterday - try again next tick

        cost = cost_for_period(config_database_path, historian, plant["id"], plant["code"], day_start, day_end)
        split = production_non_production_split(historian, config_database_path, plant["code"], plant["id"], day_start, day_end, now=day_end)
        demand = estimated_maximum_demand(historian, plant["code"], day_start, day_end, interval_minutes)

        def _value(kpi):
            return kpi["value"] if kpi["classification"] != "UNAVAILABLE" else None

        connection = sqlite3.connect(config_database_path)
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO energy_kpi_daily_summary (
                    plant_id, summary_date, energy_kwh, cost, currency,
                    production_energy_kwh, non_production_energy_kwh,
                    avg_demand_kw, max_demand_kw, demand_interval_minutes, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plant["id"], yesterday.isoformat(), energy["value"], _value(cost),
                    cost.get("unit") if cost["classification"] != "UNAVAILABLE" else None,
                    _value(split["production_energy_kwh"]), _value(split["non_production_energy_kwh"]),
                    round(energy["value"] / 24.0, 2), _value(demand), interval_minutes,
                    now.strftime(TIME_FORMAT),
                ),
            )
            connection.commit()
            print(f"[{plant['code']}] daily summary finalized for {yesterday.isoformat()}: {energy['value']} kWh")
        finally:
            connection.close()


def run_forever(config_database_path, machine_database_path, poll_interval: float = TICK_INTERVAL_SECONDS) -> None:
    print(f"Energy KPI worker started. Config DB: {config_database_path}. Machine DB: {machine_database_path}. Tick interval: {poll_interval:g}s")
    historian = DatabaseManager(db_path=machine_database_path)

    while True:
        try:
            now = datetime.now()
            plants = _get_plants(config_database_path)
            for plant in plants:
                update_maximum_demand(config_database_path, historian, plant, now)
            finalize_daily_summaries(config_database_path, historian, plants, now)
        except Exception as error:
            # One bad tick must never kill the process permanently -
            # same discipline app/plc_logger.py's per-tag try/except
            # and app/production_simulator.py's per-tick guard follow.
            print(f"energy_kpi_worker tick failed: {error}")

        time.sleep(poll_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path(), get_machine_db_path())
    except KeyboardInterrupt:
        print("\nEnergy KPI worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
