from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

"""
Phase 18.1a - generic, schema-agnostic SQLite backup helper. Uses
sqlite3's own Online Backup API (Connection.backup()), never a raw
file copy - a plain `cp`/`shutil.copy2` of a live database risks
capturing a half-written page while WAL-mode writes are in flight,
producing a corrupt or inconsistent backup file. The backup API reads
the source through SQLite's own page-level locking instead, so it is
safe to run against a database another process is actively writing to.

Deliberately NOT a method on DatabaseManager - DatabaseManager's
constructor creates plc_data/plc_text_data (the historian schema), so
using a DatabaseManager instance to back up config.db (a completely
different schema) would be a schema misuse. This module knows nothing
about any particular schema - it only knows how to safely copy one
SQLite file to another - and is used both by DatabaseManager.backup()
(for the historian) and directly for config.db (which has no manager
class of its own that owns a connection the same way).
"""


BACKUP_SKIPPED_INSUFFICIENT_DISK_SPACE = "BACKUP_SKIPPED_INSUFFICIENT_DISK_SPACE"


def check_backup_preflight(
    source_database_path: Path | str, destination_dir: Path | str, minimum_free_reserve_bytes: int,
) -> dict[str, Any]:
    """
    Backup readiness follow-up - a deterministic go/no-go check run
    BEFORE any backup attempt, never merely `free_space > database_size`
    (that alone could leave the destination filesystem nearly full the
    moment the backup finishes).

    Conservative rule (deliberately, per explicit direction - documented
    here rather than left implicit):

        required_free_bytes = database_size          (space for THIS backup's own file)
                             + database_size          (backup working capacity - budgeted as a
                                                        second full copy's worth, not merely the
                                                        destination file's own footprint, so a
                                                        completed backup never leaves the disk
                                                        within a hair of full)
                             + minimum_free_reserve_bytes  (the configured safety margin, on top
                                                             of the above - untouched headroom)

    i.e. roughly 2x the source database's current size plus a
    configured reserve. This is intentionally more conservative than
    the minimum the SQLite Online Backup API technically needs (which
    only grows the destination file, not the source) - it exists to
    keep this VM (or whatever future destination is configured) from
    ending up dangerously full immediately after a "successful" backup.

    Returns a dict with `ok: bool`; when `ok` is False, `reason` is
    always BACKUP_SKIPPED_INSUFFICIENT_DISK_SPACE - never partial, never
    a guess. Never deletes anything, never starts a backup, never
    touches the destination filesystem beyond `shutil.disk_usage()`
    (a pure read).
    """
    source_database_path = Path(source_database_path)
    destination_dir = Path(destination_dir)

    if not source_database_path.exists():
        raise FileNotFoundError(f"Source database does not exist: {source_database_path}")

    database_size_bytes = source_database_path.stat().st_size
    required_free_bytes = (database_size_bytes * 2) + minimum_free_reserve_bytes

    # disk_usage() is evaluated against the destination directory's
    # nearest existing ancestor, so this is safe to call even before
    # destination_dir itself has been created for the first time.
    usage_check_path = destination_dir
    while not usage_check_path.exists():
        usage_check_path = usage_check_path.parent

    free_bytes = shutil.disk_usage(usage_check_path).free
    ok = free_bytes >= required_free_bytes

    return {
        "ok": ok,
        "reason": None if ok else BACKUP_SKIPPED_INSUFFICIENT_DISK_SPACE,
        "database_size_bytes": database_size_bytes,
        "required_free_bytes": required_free_bytes,
        "free_bytes": free_bytes,
        "minimum_free_reserve_bytes": minimum_free_reserve_bytes,
    }


def backup_sqlite_database(source_path: Path | str, destination_path: Path | str) -> Path:
    source_path = Path(source_path).resolve()
    destination_path = Path(destination_path).resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    source_connection = sqlite3.connect(source_path)
    try:
        target_connection = sqlite3.connect(destination_path)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()

    return destination_path
