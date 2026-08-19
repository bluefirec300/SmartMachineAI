from __future__ import annotations

import random
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from simulator import plant_context

"""
Generates realistic simulated production activity (Phase 4 - Production
Context). Runs as part of the standalone production_simulator.service
process (see app/production_simulator.py), independent of the live
PLC-tag simulator (simulator/tag_dataset_model.py).

Deliberately a SEPARATE generator rather than reading that module's
existing BatchNumber/ProductCode/GoodCount/RejectCount tags: those are
legacy/cosmetic production signals today, confirmed directly by reading
tag_dataset_model.py - BatchNumber/ProductCode are STRING tags that
keep their initial random value forever (never change after simulator
startup), and GoodCount/RejectCount only increment on a rare per-cycle
chance with no batch concept behind them at all. They are NOT yet
synchronized with production_batches, and are explicitly left
unchanged in Phase 4 - production_batches is the authoritative
simulated production context from this phase onward. Reconciling the
two into one production reality (so those tags reflect this simulator's
actual state) is Phase 5 Simulation Realism's job, not this one's.

Every "is there a running batch for this equipment" check queries the
database directly, never in-memory state - this is what makes the
simulator safe across a service restart: a freshly started process
observes exactly the same reality a long-running one would, so it can
never accidentally start a second concurrent batch for equipment that
already has one in progress. No batch is ever left "stuck" across a
restart either, since a batch only ever exists in 'running' between
one tick and the next - completion/interruption/cancellation are all
decided and written within a single tick, never spread across many.

Batch outcomes are deliberately weighted so 'completed' is the
overwhelming majority - interrupted/cancelled are genuinely rare in
the live simulator, matching real production, not inflated just to
exercise every status. Tests exercise those rarer paths directly with
a seeded RNG instead.
"""

# Matches the same category fragments the tag-naming convention (and
# Phase 1's equipment_type backfill) already uses - these are the only
# 8 equipment instances (4 types x 2 plants) with BatchNumber/
# ProductCode/GoodCount/RejectCount tags today.
PRODUCTION_EQUIPMENT_NAME_FRAGMENTS = (
    "bead_mill", "filling_machine", "high_speed_disperser", "mixer",
)

START_BATCH_PROBABILITY = 0.20
PROGRESS_INCREMENT_RANGE = (0.05, 0.12)
INTERRUPTION_PROBABILITY = 0.004
CANCELLATION_PROBABILITY = 0.002
DOWNTIME_MINUTES_RANGE = (10.0, 60.0)
REJECT_FRACTION_RANGE = (0.0, 0.03)
BATCH_SIZE_VARIATION_RANGE = (0.85, 1.15)

DOWNTIME_REASONS = (
    "Material changeover", "Equipment adjustment", "Quality check failure",
    "Minor stoppage", "Unplanned equipment issue",
)


def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def get_production_equipment(database_path: str | Path) -> list[dict[str, Any]]:
    """The production-capable equipment instances, with their Phase-1-
    backfilled plant_id/area_id/system_id copied as-is - never
    re-derived from name-string parsing here."""
    connection = _connect(database_path)
    try:
        placeholders = " OR ".join("e.name LIKE ?" for _ in PRODUCTION_EQUIPMENT_NAME_FRAGMENTS)
        rows = connection.execute(
            f"""
            SELECT e.id, e.name, e.display_name, e.plant_id, e.area_id, e.system_id
            FROM equipment e
            WHERE e.plant_id IS NOT NULL AND ({placeholders})
            ORDER BY e.name
            """,
            tuple(f"%{fragment}%" for fragment in PRODUCTION_EQUIPMENT_NAME_FRAGMENTS),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def get_active_products(database_path: str | Path) -> list[dict[str, Any]]:
    connection = _connect(database_path)
    try:
        rows = connection.execute("SELECT * FROM products WHERE active = 1").fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def get_running_batch(database_path: str | Path, equipment_id: int) -> dict[str, Any] | None:
    connection = _connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM production_batches WHERE equipment_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
            (equipment_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def _resolve_shift_id(database_path: str | Path, plant_id: int, now: datetime) -> int | None:
    """Best-effort match against Phase 2's shift_definitions for this
    plant (falling back to a factory-wide default) covering the
    current time and weekday - reuses the existing shift system rather
    than inventing a second one. Returns None rather than guessing if
    nothing matches (e.g. no shifts configured yet)."""
    connection = _connect(database_path)
    try:
        day_name = now.strftime("%a")
        current_time = now.strftime("%H:%M")

        for scope_clause, params in (("plant_id = ?", (plant_id,)), ("plant_id IS NULL", ())):
            rows = connection.execute(
                f"SELECT * FROM shift_definitions WHERE {scope_clause} AND active = 1", params
            ).fetchall()

            for row in rows:
                days = [d.strip() for d in (row["days_of_week"] or "").split(",")]
                if day_name not in days:
                    continue

                start, end = row["start_time"], row["end_time"]
                if start <= end:
                    if start <= current_time <= end:
                        return row["id"]
                elif current_time >= start or current_time <= end:  # crosses midnight
                    return row["id"]

        return None
    finally:
        connection.close()


def start_batch(
    database_path: str | Path,
    equipment: dict[str, Any],
    products: list[dict[str, Any]],
    rng: random.Random,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now()
    product = rng.choice(products)

    variation = rng.uniform(*BATCH_SIZE_VARIATION_RANGE)
    planned_quantity = round((product["standard_batch_size"] or 100.0) * variation, 1)

    shift_id = _resolve_shift_id(database_path, equipment["plant_id"], now)

    name_parts = equipment["name"].split("_")
    # A trailing random suffix (not just the second-precision timestamp)
    # keeps batch_code collision-proof even if two ticks for the same
    # equipment ever land in the same second - confirmed necessary by a
    # real test failure during development (a restart-safety test that
    # deliberately called tick() repeatedly with a frozen `now`).
    batch_code = (
        f"{name_parts[0].upper()}-{name_parts[-1].upper()}-"
        f"{now.strftime('%Y%m%d%H%M%S')}-{rng.randint(1000, 9999)}"
    )
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")

    connection = _connect(database_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO production_batches (
                batch_code, product_id, plant_id, area_id, system_id, equipment_id,
                shift_id, status, start_time, end_time, planned_quantity,
                actual_quantity, good_quantity, reject_quantity, unit_of_measure,
                downtime_minutes, downtime_reason, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, NULL, ?, 0, 0, 0, ?, NULL, NULL, 'simulated', ?)
            """,
            (
                batch_code, product["id"], equipment["plant_id"], equipment["area_id"], equipment["system_id"],
                equipment["id"], shift_id, now_text, planned_quantity, product["unit_of_measure"], now_text,
            ),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def _finish_batch(
    database_path: str | Path, batch_id: int, status: str, now: datetime,
    downtime_minutes: float | None = None, downtime_reason: str | None = None,
) -> None:
    connection = _connect(database_path)
    try:
        connection.execute(
            "UPDATE production_batches SET status = ?, end_time = ?, downtime_minutes = ?, downtime_reason = ? WHERE id = ?",
            (status, now.strftime("%Y-%m-%d %H:%M:%S"), downtime_minutes, downtime_reason, batch_id),
        )
        connection.commit()
    finally:
        connection.close()


def progress_batch(database_path: str | Path, batch: dict[str, Any], rng: random.Random, now: datetime | None = None) -> str:
    """
    Advances one running batch by one tick. Returns the resulting
    status ('running', 'completed', 'interrupted', or 'cancelled').
    """
    now = now or datetime.now()
    planned = batch["planned_quantity"] or 0.0
    roll = rng.random()

    if roll < CANCELLATION_PROBABILITY:
        _finish_batch(database_path, batch["id"], "cancelled", now)
        return "cancelled"

    if roll < CANCELLATION_PROBABILITY + INTERRUPTION_PROBABILITY:
        downtime_minutes = round(rng.uniform(*DOWNTIME_MINUTES_RANGE), 1)
        reason = rng.choice(DOWNTIME_REASONS)
        _finish_batch(database_path, batch["id"], "interrupted", now, downtime_minutes, reason)
        return "interrupted"

    increment_fraction = rng.uniform(*PROGRESS_INCREMENT_RANGE)
    increment = planned * increment_fraction
    reject_fraction = rng.uniform(*REJECT_FRACTION_RANGE)

    new_actual = (batch["actual_quantity"] or 0.0) + increment
    new_reject = (batch["reject_quantity"] or 0.0) + increment * reject_fraction

    completed = new_actual >= planned > 0
    if completed:
        new_actual = planned

    new_good = max(new_actual - new_reject, 0.0)

    connection = _connect(database_path)
    try:
        connection.execute(
            "UPDATE production_batches SET actual_quantity = ?, good_quantity = ?, reject_quantity = ? WHERE id = ?",
            (new_actual, new_good, new_reject, batch["id"]),
        )
        connection.commit()
    finally:
        connection.close()

    if completed:
        _finish_batch(database_path, batch["id"], "completed", now)
        return "completed"

    return "running"


def tick(database_path: str | Path, rng: random.Random | None = None, now: datetime | None = None) -> dict[str, int]:
    """
    One simulation step across every production-capable equipment
    instance. Safe to call from a freshly started process with no
    in-memory state at all - every decision is based on what's
    actually in the database right now, which is exactly what makes
    this safe across a service restart (see module docstring).
    """
    rng = rng or random.Random()
    now = now or datetime.now()

    equipment_list = get_production_equipment(database_path)
    products = get_active_products(database_path)

    result = {"started": 0, "progressed": 0, "completed": 0, "interrupted": 0, "cancelled": 0}

    if not products:
        return result

    # Post-Phase-14 cleanup - realistic weekday/weekend production-hours
    # scheduling (one shift lookup per tick call, not per equipment -
    # this project has one shared factory-wide schedule, not per-plant
    # shifts). Reuses Phase 2's shift_definitions via
    # plant_context.is_within_active_shift() - see
    # engine/seed_shift_definitions.py for the seeded row.
    within_shift = plant_context.is_within_active_shift(database_path, now)
    start_probability = START_BATCH_PROBABILITY * plant_context.production_start_probability_factor(now, within_shift)

    for equipment in equipment_list:
        running = get_running_batch(database_path, equipment["id"])

        if running is None:
            if rng.random() < start_probability:
                start_batch(database_path, equipment, products, rng, now)
                result["started"] += 1
            continue

        outcome = progress_batch(database_path, running, rng, now)
        result["progressed"] += 1
        if outcome in ("completed", "interrupted", "cancelled"):
            result[outcome] += 1

    return result
