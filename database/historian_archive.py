from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from database.backup import check_backup_preflight

"""
Phase V1.2 - monthly historian archiving. Keeps the ACTIVE plc_data
table bounded to a configurable retention window (default 90 days,
config/settings.ini's [HISTORIAN_MAINTENANCE] retention_days, read via
config.config_manager.ConfigManager.historian_retention_days - UNCHANGED)
without discarding older raw data outright - it moves to a separate,
still-queryable plain SQLite file per calendar month instead.

Deliberately NOT a new database platform/engine, per explicit
direction - one small SQLite file per month, in an `archive/`
directory next to the live machine_data.db. One file per month (not
one ever-growing archive file, and not one file per day) keeps each
file small, keeps a query only ever opening the handful of files its
own date range actually spans, and keeps this the same "plain SQLite
file" mental model as the rest of this project.

Safety invariant, never violated: raw data is deleted from the live
plc_data table ONLY after verify_month_archive() has independently
re-counted AND boundary-checked (COUNT, MIN(time), MAX(time)) the
archived rows against the live table for that exact month and found
them to match EXACTLY. A failed or skipped verification never
triggers a delete - the month is simply left in the live table,
untouched, and retried on the next maintenance cycle.

Reuses database.backup.check_backup_preflight() (schema-agnostic, was
already written for the "don't fill the disk" concern) as the same
disk-space gate before creating an archive file - archiving is, at
the storage-safety level, the same kind of operation as a backup: copy
some data to a new file, need real headroom first.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
ARCHIVE_DIRNAME = "archive"
ARCHIVE_FILE_PREFIX = "plc_data_"

ARCHIVE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS plc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT NOT NULL,
        tag TEXT NOT NULL,
        address TEXT NOT NULL,
        value REAL NOT NULL
    )
"""
ARCHIVE_INDEX = "CREATE INDEX IF NOT EXISTS idx_plc_data_tag_time ON plc_data(tag, time)"


def archive_dir_for(machine_db_path: Path | str) -> Path:
    return Path(machine_db_path).resolve().parent / ARCHIVE_DIRNAME


def archive_path_for_month(archive_dir: Path, year: int, month: int) -> Path:
    return archive_dir / f"{ARCHIVE_FILE_PREFIX}{year:04d}-{month:02d}.db"


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """[start, end) as TIME_FORMAT strings - `end` is the first instant
    of the NEXT month, so a plain `time >= start AND time < end` is
    exact with no off-by-one-second boundary risk."""
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start.strftime(TIME_FORMAT), end.strftime(TIME_FORMAT)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _oldest_year_month(historian) -> tuple[int, int] | None:
    """
    Reads only the first 7 characters ("YYYY-MM") of MIN(time) rather
    than parsing a full datetime - this project's historian has a
    known handful of legacy rows using an ISO 'T' separator instead of
    the standard space (see CLAUDE.md's timestamp-format landmine);
    slicing the date portion is correct either way since the bug only
    ever affects the separator character, never the date digits.
    """
    with historian.get_connection() as connection:
        oldest = connection.execute("SELECT MIN(time) AS t FROM plc_data").fetchone()["t"]

    if oldest is None or len(oldest) < 7:
        return None

    return int(oldest[0:4]), int(oldest[5:7])


def months_eligible_for_archiving(historian, retention_days: int, now: datetime | None = None) -> list[tuple[int, int]]:
    """
    Every FULL calendar month whose data is entirely older than the
    retention cutoff (now - retention_days) - i.e. the month's own
    first-instant-of-next-month boundary is at or before the cutoff, so
    nothing in that month can still be inside the "always live" window.
    Oldest-first. Determined from the oldest row genuinely present in
    plc_data - never assumes/guesses a start date.
    """
    now = now or datetime.now()
    cutoff_text = (now - timedelta(days=retention_days)).strftime(TIME_FORMAT)

    oldest = _oldest_year_month(historian)
    if oldest is None:
        return []

    year, month = oldest
    eligible: list[tuple[int, int]] = []

    while True:
        _, month_end = _month_bounds(year, month)
        if month_end > cutoff_text:
            break
        eligible.append((year, month))
        year, month = _next_month(year, month)

    return eligible


def create_month_archive(
    historian, archive_dir: Path, year: int, month: int, minimum_free_reserve_bytes: int,
) -> dict[str, Any]:
    """
    Copies every plc_data row for the given calendar month into its own
    archive file. Never deletes anything from the live table - that
    only ever happens in delete_month_from_live(), and only after
    verify_month_archive() has passed. Gated by check_backup_preflight()
    first; a disk-space skip creates no file at all (never a partial
    archive).
    """
    start, end = _month_bounds(year, month)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_path_for_month(archive_dir, year, month)

    preflight = check_backup_preflight(historian.db_path, archive_dir, minimum_free_reserve_bytes)
    if not preflight["ok"]:
        return {"status": "skipped", "reason": preflight["reason"], "year": year, "month": month, "preflight": preflight}

    with historian.get_connection() as source:
        rows = source.execute(
            "SELECT time, tag, address, value FROM plc_data WHERE time >= ? AND time < ? ORDER BY time ASC",
            (start, end),
        ).fetchall()

    connection = sqlite3.connect(archive_path)
    try:
        connection.execute(ARCHIVE_SCHEMA)
        connection.execute(ARCHIVE_INDEX)
        connection.executemany(
            "INSERT INTO plc_data (time, tag, address, value) VALUES (?, ?, ?, ?)",
            [(row["time"], row["tag"], row["address"], row["value"]) for row in rows],
        )
        connection.commit()
    finally:
        connection.close()

    return {"status": "ok", "year": year, "month": month, "path": str(archive_path), "rows_copied": len(rows)}


def verify_month_archive(historian, archive_path: Path, year: int, month: int) -> dict[str, Any]:
    """
    Independently re-derives both sides rather than trusting
    create_month_archive()'s own return value - a real check, not a
    repeated assertion of the same in-memory count. Row count AND
    MIN/MAX(time) must all match exactly.
    """
    start, end = _month_bounds(year, month)

    with historian.get_connection() as source:
        live = source.execute(
            "SELECT COUNT(*) AS c, MIN(time) AS min_t, MAX(time) AS max_t FROM plc_data WHERE time >= ? AND time < ?",
            (start, end),
        ).fetchone()

    if not archive_path.exists():
        return {"verified": False, "reason": "archive file does not exist"}

    connection = sqlite3.connect(archive_path)
    connection.row_factory = sqlite3.Row
    try:
        archived = connection.execute(
            "SELECT COUNT(*) AS c, MIN(time) AS min_t, MAX(time) AS max_t FROM plc_data WHERE time >= ? AND time < ?",
            (start, end),
        ).fetchone()
    finally:
        connection.close()

    verified = (
        live["c"] == archived["c"]
        and live["min_t"] == archived["min_t"]
        and live["max_t"] == archived["max_t"]
    )

    return {
        "verified": verified,
        "live_count": live["c"], "archived_count": archived["c"],
        "live_min": live["min_t"], "archived_min": archived["min_t"],
        "live_max": live["max_t"], "archived_max": archived["max_t"],
    }


def delete_month_from_live(historian, year: int, month: int) -> int:
    """The ONLY function in this module that deletes from plc_data.
    Callers must have a passing verify_month_archive() result first -
    this function itself does not check, by design (it's the single,
    explicit point of no return, called from exactly one place below)."""
    start, end = _month_bounds(year, month)
    with historian.get_connection() as connection:
        cursor = connection.execute("DELETE FROM plc_data WHERE time >= ? AND time < ?", (start, end))
        return cursor.rowcount


def archive_and_purge_eligible_months(
    historian, archive_dir: Path, retention_days: int, minimum_free_reserve_bytes: int, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    The orchestrator - archive, verify, THEN (only then) delete, one
    eligible month at a time, oldest first. Stops at the first
    disk-space skip or verification failure rather than continuing to
    later months - keeps behavior simple and conservative (retry next
    cycle) rather than leaving a partially-processed, hard-to-reason-
    about state. Idempotent: if a month's archive file already exists
    (e.g. a prior cycle archived it but was interrupted before
    deleting), it's re-verified rather than re-copied.

    `months_eligible_for_archiving()` walks every CALENDAR month
    between the oldest row present and the retention cutoff - in
    normal continuous operation every one of those months genuinely
    has data, but a month with zero rows (e.g. after a genuine gap in
    logging) is silently skipped here rather than wastefully creating
    an empty archive file and reporting a no-op "processed" a month
    with nothing in it.
    """
    results: list[dict[str, Any]] = []

    for year, month in months_eligible_for_archiving(historian, retention_days, now):
        start, end = _month_bounds(year, month)
        with historian.get_connection() as connection:
            row_count = connection.execute(
                "SELECT COUNT(*) AS c FROM plc_data WHERE time >= ? AND time < ?", (start, end),
            ).fetchone()["c"]

        if row_count == 0:
            continue

        archive_path = archive_path_for_month(archive_dir, year, month)

        if archive_path.exists():
            verification = verify_month_archive(historian, archive_path, year, month)
        else:
            creation = create_month_archive(historian, archive_dir, year, month, minimum_free_reserve_bytes)
            if creation["status"] != "ok":
                results.append({"year": year, "month": month, "stage": "create", **creation})
                break
            verification = verify_month_archive(historian, archive_path, year, month)

        if not verification["verified"]:
            results.append({"year": year, "month": month, "stage": "verify", **verification})
            break

        deleted = delete_month_from_live(historian, year, month)
        results.append({"year": year, "month": month, "stage": "complete", "verified": True, "rows_deleted": deleted})

    return results


def query_archived_history(archive_dir: Path, tag: str, start: str, end: str, limit: int) -> list[dict[str, Any]]:
    """
    Merges rows from every monthly archive file overlapping [start, end]
    (inclusive) for one tag. Cheap no-op when no archive directory or no
    matching files exist (the common case - most queries never reach
    archived data at all). Never touches the live database.
    """
    if not archive_dir.exists():
        return []

    year, month = int(start[0:4]), int(start[5:7])
    end_year, end_month = int(end[0:4]), int(end[5:7])

    rows: list[dict[str, Any]] = []

    while (year, month) <= (end_year, end_month):
        archive_path = archive_path_for_month(archive_dir, year, month)

        if archive_path.exists():
            connection = sqlite3.connect(archive_path)
            connection.row_factory = sqlite3.Row
            try:
                month_rows = connection.execute(
                    "SELECT time, tag, address, value FROM plc_data WHERE tag = ? AND time >= ? AND time <= ? ORDER BY time ASC",
                    (tag, start, end),
                ).fetchall()
                rows.extend(dict(row) for row in month_rows)
            finally:
                connection.close()

        year, month = _next_month(year, month)

    rows.sort(key=lambda row: row["time"])
    return rows[:limit] if limit else rows
