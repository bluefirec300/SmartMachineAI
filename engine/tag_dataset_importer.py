from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATASET_PATH = PROJECT_ROOT / "config" / "master_tag_list.json"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"

NEW_TAG_COLUMNS = {
    "logging_interval_seconds": "INTEGER",
    "log_on_change": "INTEGER",
}


def _ensure_columns(connection: sqlite3.Connection) -> None:
    """Add logging-cadence columns to tags if they aren't there yet."""
    existing = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(tags)")
    }

    for name, sql_type in NEW_TAG_COLUMNS.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE tags ADD COLUMN {name} {sql_type}"
            )


def _parse_logging_interval(text: str) -> tuple[int | None, int]:
    """
    Convert a dataset "Logging Interval" string into
    (logging_interval_seconds, log_on_change).

    "change" (used for most BOOL/STRING tags) means: only write a new
    historian row when the value actually differs from the last one
    logged, regardless of elapsed time. A parseable duration means:
    write at most once per that many seconds. Anything unparseable
    falls back to "log every poll", matching current behavior.
    """
    text = str(text or "").strip().lower()

    if not text or text == "change":
        return None, 1

    match = re.match(r"([\d.]+)\s*(s|sec|second|min|minute|h|hour)", text)

    if not match:
        return None, 0

    value = float(match.group(1))
    unit = match.group(2)

    if unit.startswith("min"):
        seconds = value * 60
    elif unit.startswith("h"):
        seconds = value * 3600
    else:
        seconds = value

    return int(seconds), 0


def _tag_instance(tag_name: str) -> str:
    """
    Extract the instance token from a Tag Name.

    e.g. "P01.UTILITY.AC01.BearingTemp" -> "AC01"
    """
    parts = tag_name.split(".")
    return parts[-2] if len(parts) >= 3 else ""


def _equipment_key(plant: str, equipment: str, instance: str) -> str:
    slug = re.sub(
        r"[^a-z0-9]+",
        "_",
        f"{plant}_{equipment}_{instance}".lower(),
    ).strip("_")

    return slug


def _instance_aliases(equipment_name: str, instance: str) -> set[str]:
    """
    Build specific, non-colliding aliases for one equipment instance.

    Deliberately does NOT include a bare "compressor"/"chiller"/etc.
    alias - those are already owned by the original demo equipment
    rows, and reusing them here would make previously-working
    questions like "what is the compressor pressure" ambiguous.
    """
    aliases = {
        instance.lower(),
        f"{equipment_name} {instance}".lower(),
    }

    number_match = re.search(r"(\d+)$", instance)

    if number_match:
        number = str(int(number_match.group(1)))
        aliases.add(f"{equipment_name} {number}".lower())

    return aliases


def _upsert_equipment(
    connection: sqlite3.Connection,
    name: str,
    display_name: str,
    description: str,
) -> int:
    row = connection.execute(
        "SELECT id FROM equipment WHERE name = ?",
        (name,),
    ).fetchone()

    if row:
        return row["id"]

    cursor = connection.execute(
        """
        INSERT INTO equipment (name, display_name, description)
        VALUES (?, ?, ?)
        """,
        (name, display_name, description),
    )

    return cursor.lastrowid


def _upsert_tag_address(
    connection: sqlite3.Connection,
    tag_id: int,
    driver: str,
    address: str,
) -> None:
    row = connection.execute(
        "SELECT id FROM tag_addresses WHERE tag_id = ? AND driver = ?",
        (tag_id, driver),
    ).fetchone()

    if row:
        connection.execute(
            "UPDATE tag_addresses SET address = ?, enabled = 1 WHERE id = ?",
            (address, row["id"]),
        )
    else:
        connection.execute(
            """
            INSERT INTO tag_addresses (tag_id, driver, address, enabled)
            VALUES (?, ?, ?, 1)
            """,
            (tag_id, driver, address),
        )


def import_dataset(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    dataset_path: str | Path = DEFAULT_DATASET_PATH,
    enable_plant: str | None = None,
    enable_phase: str | None = None,
) -> dict[str, int]:
    """
    Import the master tag list into config.db.

    Every tag in the dataset is inserted/updated as a row (so the full
    inventory exists), but only rows matching enable_plant/enable_phase
    get enabled=1. Running this again later with a different
    plant/phase never disables a tag that a previous run already
    enabled - enabling is additive across runs, so phasing in more of
    the dataset is just "run it again with a wider filter."

    If both enable_plant and enable_phase are omitted, enabled state
    is left untouched entirely (new tags default to disabled) - this
    lets the dataset be re-imported (e.g. after editing
    master_tag_list.json) without silently enabling anything.
    """
    with open(dataset_path, encoding="utf-8") as file:
        dataset_tags = json.load(file)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    _ensure_columns(connection)

    filters_specified = enable_plant is not None or enable_phase is not None

    equipment_cache: dict[str, int] = {}
    tags_created = 0
    tags_updated = 0
    equipment_created = 0
    enabled_count = 0

    for row in dataset_tags:
        plant = row["Plant"]
        equipment_name = row["Equipment"]
        tag_name = row["Tag Name"]
        instance = _tag_instance(tag_name)
        equipment_key = _equipment_key(plant, equipment_name, instance)

        if equipment_key not in equipment_cache:
            before_count = connection.execute(
                "SELECT COUNT(*) AS c FROM equipment WHERE name = ?",
                (equipment_key,),
            ).fetchone()["c"]

            equipment_id = _upsert_equipment(
                connection,
                name=equipment_key,
                display_name=f"{equipment_name} {instance} ({plant})".strip(),
                description=(
                    f"{row['Area/System']} - {equipment_name} "
                    f"instance {instance} on {plant}."
                ),
            )

            equipment_cache[equipment_key] = equipment_id

            if before_count == 0:
                equipment_created += 1

                for alias in _instance_aliases(equipment_name, instance):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO equipment_aliases
                        (equipment_id, alias) VALUES (?, ?)
                        """,
                        (equipment_id, alias),
                    )
        else:
            equipment_id = equipment_cache[equipment_key]

        matches_filter = filters_specified and (
            (enable_plant is None or plant == enable_plant)
            and (enable_phase is None or row["Phase"] == enable_phase)
        )

        logging_interval_seconds, log_on_change = _parse_logging_interval(
            row.get("Logging Interval")
        )

        existing = connection.execute(
            "SELECT id, enabled FROM tags WHERE tag_name = ?",
            (tag_name,),
        ).fetchone()

        if existing:
            should_enable = 1 if (existing["enabled"] or matches_filter) else 0

            connection.execute(
                """
                UPDATE tags SET
                    equipment_id = ?, description = ?, driver = ?,
                    address = ?, data_type = ?, unit = ?, enabled = ?,
                    logging_interval_seconds = ?, log_on_change = ?
                WHERE id = ?
                """,
                (
                    equipment_id, row["Description"], "simulator",
                    tag_name, row["Data Type"], row["Unit"],
                    should_enable, logging_interval_seconds,
                    log_on_change, existing["id"],
                ),
            )

            tag_id = existing["id"]
            tags_updated += 1
        else:
            should_enable = 1 if matches_filter else 0

            cursor = connection.execute(
                """
                INSERT INTO tags
                (equipment_id, tag_name, description, driver, address,
                 data_type, unit, enabled, logging_interval_seconds,
                 log_on_change)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    equipment_id, tag_name, row["Description"],
                    "simulator", tag_name, row["Data Type"], row["Unit"],
                    should_enable, logging_interval_seconds, log_on_change,
                ),
            )

            tag_id = cursor.lastrowid
            tags_created += 1

        _upsert_tag_address(
            connection,
            tag_id=tag_id,
            driver="simulator",
            address=tag_name,
        )

        if should_enable:
            enabled_count += 1

    connection.commit()
    connection.close()

    return {
        "tags_created": tags_created,
        "tags_updated": tags_updated,
        "equipment_created": equipment_created,
        "enabled_total": enabled_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import the master tag list dataset into config.db.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH))
    parser.add_argument("--enable-plant", default=None)
    parser.add_argument("--enable-phase", default=None)

    args = parser.parse_args()

    result = import_dataset(
        database_path=args.database,
        dataset_path=args.dataset,
        enable_plant=args.enable_plant,
        enable_phase=args.enable_phase,
    )

    print(result)


if __name__ == "__main__":
    main()
