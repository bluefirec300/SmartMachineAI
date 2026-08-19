from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

"""
Backup readiness follow-up - a pure, read-only status model for a
future Data Management / System Administration panel. Never triggers a
backup, never deletes anything, never invents data that wasn't
genuinely computed/persisted elsewhere - every field here is either a
live filesystem measurement (sizes, free space, counts) or read from
app/historian_maintenance_worker.py's own small marker/summary files
(the SAME files that worker itself writes - this module never
duplicates that persistence, only reads it).

Deliberately does NOT import from app/historian_maintenance_worker.py
(database/ is a lower-level module than app/ throughout this project;
importing "up" would invert that) - it reads the same conventional
marker file paths independently instead, which are otherwise-plain
data files, not code.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_MARKER_PATH = PROJECT_ROOT / "logs" / "historian_backup_last_run.txt"
DEFAULT_BACKUP_SUMMARY_PATH = PROJECT_ROOT / "logs" / "historian_backup_last_summary.json"


def _directory_db_files_stats(directory: Path) -> tuple[int, int]:
    """Returns (total_size_bytes, file_count) for every *.db file
    directly inside `directory`. (0, 0) if the directory doesn't exist
    yet - never an error, since "backup never run" is a normal state."""
    if not directory.exists():
        return 0, 0

    files = list(directory.glob("*.db"))
    return sum(f.stat().st_size for f in files), len(files)


def _free_disk_space_bytes(path: Path) -> int:
    existing_ancestor = path
    while not existing_ancestor.exists():
        existing_ancestor = existing_ancestor.parent

    return shutil.disk_usage(existing_ancestor).free


def _read_last_backup_attempt_at(marker_path: Path) -> str | None:
    if not marker_path.exists():
        return None

    text = marker_path.read_text(encoding="utf-8").strip()
    return text or None


def _read_last_backup_summary(summary_path: Path) -> dict | None:
    if not summary_path.exists():
        return None

    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def get_backup_status(
    config_db_path: Path | str,
    machine_db_path: Path | str,
    destination_dir: Path | str,
    backup_enabled: bool,
    retention_count: int,
    minimum_free_reserve_bytes: int,
    marker_path: Path | str = DEFAULT_BACKUP_MARKER_PATH,
    summary_path: Path | str = DEFAULT_BACKUP_SUMMARY_PATH,
) -> dict[str, Any]:
    """
    A single, deterministic snapshot of backup readiness/state -
    everything a future Data Management panel needs, computed fresh
    each call (no caching, no side effects). `capability` is "READY"
    whenever the backup mechanism itself is present and callable
    (always true in this codebase - the check exists mainly so a
    future caller has a single field to display rather than inferring
    readiness from the absence of an error).
    """
    config_db_path = Path(config_db_path)
    machine_db_path = Path(machine_db_path)
    destination_dir = Path(destination_dir)

    config_db_size = config_db_path.stat().st_size if config_db_path.exists() else None
    machine_db_size = machine_db_path.stat().st_size if machine_db_path.exists() else None

    backup_directory_size_bytes, backup_file_count = _directory_db_files_stats(destination_dir)

    # A simple, explicitly-approximate estimate: each retained backup
    # is assumed to be roughly today's database size - real historical
    # backups will vary (the historian grows over time), so this is
    # deliberately re-derived from CURRENT sizes on every call, never
    # cached, matching "storage requirements should be recalculated
    # from the current database size" before enabling automatic backup.
    current_total_size = (config_db_size or 0) + (machine_db_size or 0)
    estimated_retention_storage_bytes = current_total_size * retention_count

    return {
        "capability": "READY",
        "automatic_backup_enabled": backup_enabled,
        "destination_dir": str(destination_dir),
        "config_db_size_bytes": config_db_size,
        "machine_db_size_bytes": machine_db_size,
        "backup_directory_exists": destination_dir.exists(),
        "backup_directory_size_bytes": backup_directory_size_bytes,
        "backup_file_count": backup_file_count,
        "free_disk_space_bytes": _free_disk_space_bytes(destination_dir),
        "configured_retention_count": retention_count,
        "estimated_retention_storage_bytes": estimated_retention_storage_bytes,
        "minimum_free_reserve_bytes": minimum_free_reserve_bytes,
        "last_backup_attempt_at": _read_last_backup_attempt_at(Path(marker_path)),
        "last_backup_summary": _read_last_backup_summary(Path(summary_path)),
    }
