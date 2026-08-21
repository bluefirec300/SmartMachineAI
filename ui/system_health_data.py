"""
Phase V2.1 - System & Worker Health.

Answers one question for an engineer with no terminal access: "is
SmartFactoryAI's own software actually running correctly right now?"
Deliberately separate from Equipment Health / Data Health, which are
about factory telemetry quality, not this application's own processes.

Design constraints this module holds to:
- No sudo. Every subprocess call here is a read-only `systemctl`/
  `journalctl` query, which any user in the same group as the service
  (this app already runs as, per `streamlit.service`'s `User=`) can run
  without elevated privileges - confirmed live before writing this.
- No invented health information. Every field either comes from a real
  systemd property, a real timestamp already written by that service to
  a database table it owns, or an existing on-disk marker file written
  by `app/historian_maintenance_worker.py`. If a signal genuinely isn't
  available, the field is None/"Unavailable", never guessed.
- No duplicate monitoring. This reuses whatever timestamp each
  worker/service already produces as a side effect of its real job -
  it does not add a new heartbeat-writing mechanism to any worker.
- Every subprocess/database call is wrapped so a single unreachable
  service or missing table can never crash the page - the rest of the
  table still renders.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.historian_maintenance_worker import (
    ARCHIVE_MARKER_PATH,
    BACKUP_MARKER_PATH,
    MARKER_TIME_FORMAT,
    RETENTION_MARKER_PATH,
    read_last_backup_summary,
)
from ui.data_access import CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH

SYSTEMCTL_TIMEOUT_SECONDS = 5
JOURNALCTL_TIMEOUT_SECONDS = 5
JOURNAL_ERROR_SCAN_LINES = 200

# Every worker's routine "cycle: {...}" success line includes its own
# explicit 'errors': N / 'failed': N count (see e.g.
# data_health_history_worker's "cycle: {'eligible': 34, ... 'errors':
# 0, ...}") - a naive keyword search for "error"/"failed" would flag
# EVERY one of these healthy zero-error lines. Trust the worker's own
# reported count instead wherever this shape is present.
_CYCLE_SUMMARY_COUNT_PATTERN = re.compile(r"'(?:errors|failed)':\s*(\d+)")

# Fallback for any line that isn't a "cycle: {...}" summary (a raised
# exception, a "X worker failed on Y: ..." message, a bare Traceback).
# A soft signal, reported as "N error-looking line(s) found", never as
# a confirmed diagnosis.
_ERROR_KEYWORD_PATTERN = re.compile(r"(failed|error|traceback|exception)", re.IGNORECASE)


def _is_error_line(line: str) -> bool:
    counts = _CYCLE_SUMMARY_COUNT_PATTERN.findall(line)
    if counts:
        return any(int(count) > 0 for count in counts)
    return bool(_ERROR_KEYWORD_PATTERN.search(line))

CORE = "Core service"
WORKER = "Background worker"

# Tick intervals below are copied from each worker's own
# TICK_INTERVAL_SECONDS constant at the time this page was written -
# informational display only ("expected cadence"), not live-imported,
# so this module never pulls a worker's full engine/DB dependency
# chain into the Streamlit process just to read one number.
SERVICES: list[dict[str, Any]] = [
    {
        "unit": "plc_logger.service",
        "label": "PLC Data Logger",
        "category": CORE,
        "tick_interval_seconds": None,
        "activity_source": "plc_data_latest",
        "activity_label": "Most recent tag reading logged to the historian",
    },
    {
        "unit": "event_monitor.service",
        "label": "Event Monitor",
        "category": CORE,
        "tick_interval_seconds": None,
        "activity_source": "machine_events_latest",
        "activity_label": "Most recent event stored (quiet periods with no active alarms are normal - this is not a per-cycle heartbeat)",
    },
    {
        "unit": "streamlit.service",
        "label": "Web UI (Streamlit)",
        "category": CORE,
        "tick_interval_seconds": None,
        "activity_source": None,
        "activity_label": "Not applicable - request-driven, has no periodic tick of its own",
    },
    {
        "unit": "anomaly_worker.service",
        "label": "Anomaly Detection Worker",
        "category": WORKER,
        "tick_interval_seconds": 60.0,
        "activity_source": ("config", "anomalies", "updated_at"),
        "activity_label": "Most recent anomaly record updated",
    },
    {
        "unit": "asset_performance_worker.service",
        "label": "Asset Performance Worker",
        "category": WORKER,
        "tick_interval_seconds": 5.0,
        "activity_source": ("config", "asset_performance_observations", "computed_at"),
        "activity_label": "Most recent asset performance observation computed",
    },
    {
        "unit": "baseline_worker.service",
        "label": "Baseline Engine Worker",
        "category": WORKER,
        "tick_interval_seconds": 5.0,
        "activity_source": ("config", "baseline_context_summary", "computed_at"),
        "activity_label": "Most recent baseline context computed",
    },
    {
        "unit": "data_health_history_worker.service",
        "label": "Data Health History Worker",
        "category": WORKER,
        "tick_interval_seconds": 300.0,
        "activity_source": ("config", "data_health_snapshots", "computed_at"),
        "activity_label": "Most recent data health snapshot recorded",
    },
    {
        "unit": "energy_kpi_worker.service",
        "label": "Energy KPI Worker",
        "category": WORKER,
        "tick_interval_seconds": 60.0,
        "activity_source": ("config", "energy_kpi_daily_summary", "computed_at"),
        "activity_label": "Most recent daily energy summary computed (the separate peak-demand record only advances on a new record, so it's not used here as the activity signal)",
    },
    {
        "unit": "equipment_health_worker.service",
        "label": "Equipment Health Worker",
        "category": WORKER,
        "tick_interval_seconds": 300.0,
        "activity_source": ("config", "equipment_health_snapshots", "computed_at"),
        "activity_label": "Most recent equipment health snapshot recorded",
    },
    {
        "unit": "historian_maintenance_worker.service",
        "label": "Historian Retention & Backup Worker",
        "category": WORKER,
        "tick_interval_seconds": 300.0,
        "activity_source": "historian_markers",
        "activity_label": "Most recent retention/archive/backup run (from its own on-disk marker files)",
    },
    {
        "unit": "opportunity_worker.service",
        "label": "Energy Opportunity Worker",
        "category": WORKER,
        "tick_interval_seconds": 900.0,
        "activity_source": ("config", "energy_opportunities", "last_updated"),
        "activity_label": "Most recent energy opportunity record updated",
    },
    {
        "unit": "savings_verification_worker.service",
        "label": "Savings Verification Worker",
        "category": WORKER,
        "tick_interval_seconds": 3600.0,
        "activity_source": ("config", "savings_verification_results", "evaluated_at"),
        "activity_label": "Most recent verification evaluation recorded",
    },
    {
        "unit": "production_simulator.service",
        "label": "Production Batch Simulator",
        "category": WORKER,
        "tick_interval_seconds": 30.0,
        "activity_source": ("config", "production_batches", "created_at"),
        "activity_label": "Most recent batch started (only advances when a NEW batch begins, not on in-progress updates - a long gap does not by itself mean the worker stopped)",
    },
]


@dataclass
class ServiceHealth:
    unit: str
    label: str
    category: str
    activity_label: str
    tick_interval_seconds: float | None
    systemctl_available: bool
    unit_found: bool | None
    running: bool | None
    active_state: str | None
    sub_state: str | None
    since: datetime | None
    restart_count: int | None
    last_activity: datetime | None
    last_activity_note: str | None
    recent_error_count: int | None
    recent_error_sample: str | None
    journal_available: bool


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _parse_systemctl_timestamp(raw: str) -> datetime | None:
    # systemctl show's timestamp format, e.g. "Fri 2026-08-21 09:16:47 +08"
    # - drop the leading weekday and trailing timezone offset, which
    # datetime.strptime has no portable single directive for together.
    if not raw or raw in ("n/a", "0"):
        return None
    parts = raw.split()
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def get_systemd_state(unit: str) -> dict[str, Any]:
    """
    Real, unprivileged `systemctl show` query - the authoritative
    running/stopped signal for every service on this list. Returns
    systemctl_available=False (never a guessed state) if systemctl
    itself can't be reached at all.
    """
    result = _run(
        ["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState",
         "-p", "ActiveEnterTimestamp", "-p", "NRestarts", "-p", "LoadState"],
        SYSTEMCTL_TIMEOUT_SECONDS,
    )

    if result is None or result.returncode != 0:
        return {"available": False}

    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value

    if values.get("LoadState") == "not-found":
        return {"available": True, "found": False}

    active_state = values.get("ActiveState")
    restart_count = None
    if values.get("NRestarts", "").isdigit():
        restart_count = int(values["NRestarts"])

    return {
        "available": True,
        "found": True,
        "active_state": active_state,
        "sub_state": values.get("SubState"),
        "running": active_state == "active",
        "since": _parse_systemctl_timestamp(values.get("ActiveEnterTimestamp", "")),
        "restart_count": restart_count,
    }


def get_recent_journal_errors(unit: str, lines: int = JOURNAL_ERROR_SCAN_LINES) -> dict[str, Any]:
    """
    Scans the last `lines` journald lines for this unit for anything
    that looks like an error - a soft, honestly-scoped signal (may
    include old messages still inside that line window), never a
    confirmed diagnosis.
    """
    result = _run(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
        JOURNALCTL_TIMEOUT_SECONDS,
    )

    if result is None or result.returncode != 0:
        return {"available": False, "count": None, "sample": None}

    matches = [line for line in result.stdout.splitlines() if _is_error_line(line)]

    return {
        "available": True,
        "count": len(matches),
        "sample": matches[-1][:300] if matches else None,
    }


def _query_max_timestamp(db_key: str, table: str, column: str) -> datetime | None:
    path = CONFIG_DATABASE_PATH if db_key == "config" else MACHINE_DATABASE_PATH

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None

    if not row or row[0] is None:
        return None

    try:
        return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _read_marker(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        return datetime.strptime(text, MARKER_TIME_FORMAT) if text else None
    except (OSError, ValueError):
        return None


def get_activity(entry: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """Returns (timestamp, extra_note) - extra_note only ever adds
    honest context (e.g. a backup failure surfaced in its own summary
    file), never invents a timestamp that wasn't actually recorded."""
    source = entry["activity_source"]

    if source is None:
        return None, None

    if source == "plc_data_latest":
        return _query_max_timestamp("machine", "plc_data", "time"), None

    if source == "machine_events_latest":
        return _query_max_timestamp("machine", "machine_events", "event_time"), None

    if source == "historian_markers":
        timestamps = [
            _read_marker(RETENTION_MARKER_PATH),
            _read_marker(ARCHIVE_MARKER_PATH),
            _read_marker(BACKUP_MARKER_PATH),
        ]
        known = [t for t in timestamps if t is not None]
        latest = max(known) if known else None

        note = None
        summary = read_last_backup_summary()
        if summary:
            statuses = {
                name: info.get("status")
                for name, info in summary.get("databases", {}).items()
            }
            if any(status != "ok" for status in statuses.values()):
                note = f"Last backup summary reported a non-ok status: {statuses}"

        return latest, note

    db_key, table, column = source
    return _query_max_timestamp(db_key, table, column), None


def format_time_ago(timestamp: datetime | None, now: datetime | None = None) -> str:
    """Plain-language elapsed time (e.g. "3 min ago", "2 days ago") for
    a timestamp already retrieved above - never called with a guessed
    timestamp, only ever a real one or None."""
    if timestamp is None:
        return "Unavailable"

    now = now or datetime.now()
    elapsed = (now - timestamp).total_seconds()

    if elapsed < 0:
        return "Just now"
    if elapsed < 60:
        return f"{int(elapsed)} sec ago"
    if elapsed < 3600:
        return f"{int(elapsed // 60)} min ago"
    if elapsed < 86400:
        return f"{int(elapsed // 3600)} hr ago"
    return f"{int(elapsed // 86400)} day(s) ago"


def get_all_service_health() -> list[ServiceHealth]:
    results = []

    for entry in SERVICES:
        state = get_systemd_state(entry["unit"])
        errors = get_recent_journal_errors(entry["unit"])
        last_activity, activity_note = get_activity(entry)

        results.append(
            ServiceHealth(
                unit=entry["unit"],
                label=entry["label"],
                category=entry["category"],
                activity_label=entry["activity_label"],
                tick_interval_seconds=entry["tick_interval_seconds"],
                systemctl_available=state.get("available", False),
                unit_found=state.get("found"),
                running=state.get("running"),
                active_state=state.get("active_state"),
                sub_state=state.get("sub_state"),
                since=state.get("since"),
                restart_count=state.get("restart_count"),
                last_activity=last_activity,
                last_activity_note=activity_note,
                recent_error_count=errors.get("count"),
                recent_error_sample=errors.get("sample"),
                journal_available=errors.get("available", False),
            )
        )

    return results
