from __future__ import annotations

import fcntl
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.config_manager import ConfigManager
from config.environment import get_config_db_path, get_machine_db_path, get_active_environment
from database.backup import backup_sqlite_database, check_backup_preflight
from database.database import DatabaseManager

"""
Phase 18.1a - historian retention + database backup, in one worker.
Two genuinely separate concerns kept in a single service deliberately
(not split into two, per the explicit "avoid unnecessarily complex
enterprise backup infrastructure" direction) - both are low-frequency,
low-risk maintenance operations against the same two database files,
and combining them avoids growing the systemd service count further
for what is operationally one job ("keep the databases healthy").

Retention: reuses DatabaseManager.cleanup() UNCHANGED - that method
already, correctly, only ever touches plc_data (raw historian
time-series rows). It has never deleted machine_events, anomalies,
baseline results, energy KPI summaries, energy opportunities,
maintenance records, audit records, or any configuration table, and
this worker does not change that scope in any way - it only finally
calls the method with a real, configurable retention_days value
instead of leaving it uncalled (see config/settings.ini's
[DATABASE] retention_days, now actually read via
config.config_manager.ConfigManager.historian_retention_days).

Backup: reuses database.backup.backup_sqlite_database() - the same
SQLite Online Backup API DatabaseManager.backup() already used, now
also applied directly to config.db (which DatabaseManager must never
touch - constructing a DatabaseManager against config.db would create
plc_data/plc_text_data tables in the wrong schema).

Restart-safety without spamming retention/backup on every restart:
both operations track "when did this last actually run" via a small
marker file under logs/ (the same plain-marker-file pattern
config/active_environment.txt already uses elsewhere in this project),
not in-memory state - a restart (or a crash-loop) never causes cleanup
or backup to run more often than its configured interval.

Single-instance guarantee, restart behavior, and failure isolation
follow the exact same conventions as every other worker in this
project (app/energy_kpi_worker.py, app/data_health_history_worker.py):
an exclusive non-blocking flock() for the process lifetime, and a
per-tick try/except so one bad cycle (e.g. a temporarily-locked
database, a full disk) is logged and retried next tick rather than
crashing the process - systemd's Restart=always is the second line of
defense, not the first. Because this is its own separate OS process,
a failure here (including in backup specifically, which is written to
never raise past its own try/except) cannot stop plc_logger,
event_monitor, streamlit, or any analytics worker.
"""

TICK_INTERVAL_SECONDS = 300.0  # 5 min - cheap "is it time yet?" check; actual work only runs on its own configured interval
LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "historian_maintenance_worker.lock"
RETENTION_MARKER_PATH = PROJECT_ROOT / "logs" / "historian_retention_last_run.txt"
BACKUP_MARKER_PATH = PROJECT_ROOT / "logs" / "historian_backup_last_run.txt"
# Backup readiness follow-up - a small, structured (JSON, not plain
# text) record of the LAST backup attempt's per-database outcome,
# written in addition to (never instead of) BACKUP_MARKER_PATH's
# simple timestamp (which only ever governs "is it due yet" timing -
# left completely unchanged). This is purely informational, read by
# database.backup_status.get_backup_status() for a future Data
# Management panel - genuinely persisted, not invented at read time.
BACKUP_SUMMARY_PATH = PROJECT_ROOT / "logs" / "historian_backup_last_summary.json"

MARKER_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another historian_maintenance_worker instance already holds {LOCK_FILE_PATH} - "
            "refusing to start a second one. Exiting."
        )
        sys.exit(1)

    return lock_file


def _read_last_run(marker_path: Path) -> datetime | None:
    if not marker_path.exists():
        return None

    text = marker_path.read_text(encoding="utf-8").strip()

    if not text:
        return None

    try:
        return datetime.strptime(text, MARKER_TIME_FORMAT)
    except ValueError:
        return None


def _write_last_run(marker_path: Path, when: datetime) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(when.strftime(MARKER_TIME_FORMAT), encoding="utf-8")


def _is_due(last_run: datetime | None, interval_hours: float, now: datetime) -> bool:
    if last_run is None:
        return True

    elapsed_hours = (now - last_run).total_seconds() / 3600.0
    return elapsed_hours >= interval_hours


def run_retention_if_due(
    historian: DatabaseManager, retention_days: int, interval_hours: float,
    marker_path: Path = RETENTION_MARKER_PATH, now: datetime | None = None,
) -> dict | None:
    """
    Runs DatabaseManager.cleanup(days=retention_days) - unmodified,
    plc_data-only - if the configured interval has elapsed since the
    last recorded run. Returns a summary dict when it actually ran,
    else None (nothing to log - not due yet).
    """
    now = now or datetime.now()
    last_run = _read_last_run(marker_path)

    if not _is_due(last_run, interval_hours, now):
        return None

    deleted_rows = historian.cleanup(days=retention_days)
    _write_last_run(marker_path, now)

    return {"retention_days": retention_days, "deleted_rows": deleted_rows, "ran_at": now.strftime(MARKER_TIME_FORMAT)}


def _prune_old_backups(directory: Path, prefix: str, keep_count: int) -> list[Path]:
    """
    Keeps only the `keep_count` most recent timestamped backup files
    matching `prefix*.db` in `directory` (sorted by filename, which
    sorts chronologically given the fixed YYYYmmdd_HHMMSS suffix
    format below) - deletes the rest. Returns the paths actually
    deleted, for logging.
    """
    if keep_count <= 0:
        raise ValueError("keep_count must be greater than zero.")

    matching = sorted(directory.glob(f"{prefix}*.db"))
    to_delete = matching[:-keep_count] if len(matching) > keep_count else []

    for path in to_delete:
        path.unlink(missing_ok=True)

    return to_delete


def _write_backup_summary(summary_path: Path, summary: dict) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def read_last_backup_summary(summary_path: Path = BACKUP_SUMMARY_PATH) -> dict | None:
    """Read-only accessor for database.backup_status.py - never invents
    a summary that wasn't genuinely written by a real backup attempt."""
    if not summary_path.exists():
        return None

    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def run_backup_if_due(
    config_database_path: Path, machine_database_path: Path, destination_dir: Path,
    interval_hours: float, retention_count: int, minimum_free_reserve_bytes: int,
    marker_path: Path = BACKUP_MARKER_PATH, summary_path: Path = BACKUP_SUMMARY_PATH,
    now: datetime | None = None,
) -> dict | None:
    """
    Backs up both config.db and machine_data.db (the two SQLite files
    this project's data lives in) via the safe Online Backup API, if
    the configured interval has elapsed. Before EACH database's backup
    attempt, runs database.backup.check_backup_preflight() - if there
    is not enough free space (2x that database's current size plus the
    configured reserve - see that function's docstring), the attempt is
    SKIPPED for that database (never destructive: no partial backup
    file, no pruning of existing good backups, no attempt at all) and
    recorded with reason=BACKUP_SKIPPED_INSUFFICIENT_DISK_SPACE.

    A failure or skip on EITHER database is caught/recorded here and
    never raised past this function, and never prevents the OTHER
    database's backup attempt - a backup problem must never be allowed
    to look like a worker crash to the caller's per-tick try/except,
    since that already provides its own isolation; this is a second,
    explicit layer specifically for backup, per the requirement that a
    backup failure must never affect anything else.
    """
    now = now or datetime.now()
    last_run = _read_last_run(marker_path)

    if not _is_due(last_run, interval_hours, now):
        return None

    timestamp = now.strftime("%Y%m%d_%H%M%S")
    destination_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict] = {}

    for label, source_path in (("config", config_database_path), ("machine_data", machine_database_path)):
        prefix = f"{label}_backup_"
        destination_path = destination_dir / f"{prefix}{timestamp}.db"

        try:
            preflight = check_backup_preflight(source_path, destination_dir, minimum_free_reserve_bytes)
            if not preflight["ok"]:
                results[label] = {"status": "skipped", "reason": preflight["reason"], "preflight": preflight}
                continue

            backup_sqlite_database(source_path, destination_path)
            pruned = _prune_old_backups(destination_dir, prefix, retention_count)
            results[label] = {"status": "ok", "path": str(destination_path), "pruned": [str(p) for p in pruned]}
        except Exception as error:
            results[label] = {"status": "failed", "error": str(error)}

    _write_last_run(marker_path, now)
    summary = {"ran_at": now.strftime(MARKER_TIME_FORMAT), "databases": results}
    _write_backup_summary(summary_path, summary)

    return summary


def run_forever(
    config_database_path: Path, machine_database_path: Path,
    tick_interval: float = TICK_INTERVAL_SECONDS,
) -> None:
    config = ConfigManager()
    historian = DatabaseManager(db_path=machine_database_path)
    backup_destination = config.historian_backup_destination_dir / get_active_environment()

    print(
        f"Historian maintenance worker started. Config DB: {config_database_path}. "
        f"Machine DB: {machine_database_path}. Tick interval: {tick_interval:g}s. "
        f"Retention: {config.historian_retention_days} day(s), checked every "
        f"{config.historian_retention_check_interval_hours:g}h. "
        f"Backup: {'enabled' if config.historian_backup_enabled else 'disabled'}, "
        f"every {config.historian_backup_interval_hours:g}h, "
        f"destination {backup_destination}, keep {config.historian_backup_retention_count}, "
        f"minimum free reserve {config.historian_backup_minimum_free_reserve_bytes / (1000 ** 3):.1f} GB."
    )

    while True:
        try:
            summary = run_retention_if_due(
                historian, config.historian_retention_days,
                config.historian_retention_check_interval_hours,
            )
            if summary is not None:
                print(f"retention: {summary}")
        except Exception as error:
            print(f"historian_maintenance_worker retention cycle failed: {error}")

        if config.historian_backup_enabled:
            try:
                summary = run_backup_if_due(
                    config_database_path, machine_database_path, backup_destination,
                    config.historian_backup_interval_hours, config.historian_backup_retention_count,
                    config.historian_backup_minimum_free_reserve_bytes,
                )
                if summary is not None:
                    print(f"backup: {summary}")
            except Exception as error:
                print(f"historian_maintenance_worker backup cycle failed: {error}")

        time.sleep(tick_interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_config_db_path(), get_machine_db_path())
    except KeyboardInterrupt:
        print("\nHistorian maintenance worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
