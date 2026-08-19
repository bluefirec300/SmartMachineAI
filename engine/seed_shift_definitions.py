"""
Post-Phase-14 cleanup - seeds the "Weekday Production" shift row into
Phase 2's shift_definitions table (schema-defined since Phase 2, never
actually populated until now - confirmed 0 rows in the live simulation
database before this script runs).

This is the SAME table production_batch_simulator._resolve_shift_id()
and simulator.plant_context.is_within_active_shift() already read (or
now read, respectively) - seeding it here is a data change using the
EXISTING, correct-layer scheduling mechanism, not a new one.

One factory-wide (plant_id=NULL) row, matching this project's single
shared production schedule (both plants operate the same hours) -
_resolve_shift_id()'s own fallback already checks "plant_id IS NULL"
after a plant-specific match fails, so a NULL-scoped row applies to
both P01 and P02 identically without needing two rows.

Idempotent - only creates the row if no shift definition already
exists for this plant scope, safe to re-run.
"""

from __future__ import annotations

from datetime import datetime

from config.environment import get_config_db_path
from engine.operating_schedule_migrator import migrate as migrate_operating_schedule

import sqlite3

WEEKDAY_PRODUCTION_SHIFT = {
    "name": "Weekday Production",
    "start_time": "09:00",
    "end_time": "18:00",
    "days_of_week": "Mon,Tue,Wed,Thu,Fri",
}


def seed(database_path=None) -> dict[str, list[str]]:
    database_path = database_path or get_config_db_path()
    migrate_operating_schedule(database_path, backup=False)  # ensures the table exists, additive/idempotent

    connection = sqlite3.connect(database_path)
    created: list[str] = []
    skipped: list[str] = []

    try:
        existing = connection.execute(
            "SELECT 1 FROM shift_definitions WHERE plant_id IS NULL AND name = ?",
            (WEEKDAY_PRODUCTION_SHIFT["name"],),
        ).fetchone()

        if existing is not None:
            skipped.append(WEEKDAY_PRODUCTION_SHIFT["name"])
        else:
            connection.execute(
                "INSERT INTO shift_definitions (plant_id, name, start_time, end_time, days_of_week, active, created_at) "
                "VALUES (NULL, ?, ?, ?, ?, 1, ?)",
                (
                    WEEKDAY_PRODUCTION_SHIFT["name"], WEEKDAY_PRODUCTION_SHIFT["start_time"],
                    WEEKDAY_PRODUCTION_SHIFT["end_time"], WEEKDAY_PRODUCTION_SHIFT["days_of_week"],
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            connection.commit()
            created.append(WEEKDAY_PRODUCTION_SHIFT["name"])
    finally:
        connection.close()

    return {"created": created, "skipped": skipped}


if __name__ == "__main__":
    result = seed()

    if result["created"]:
        print("Created:")
        for entry in result["created"]:
            print(f"  {entry}")

    if result["skipped"]:
        print("Already existed, skipped:")
        for entry in result["skipped"]:
            print(f"  {entry}")

    if not result["created"] and not result["skipped"]:
        print("Nothing to do.")
