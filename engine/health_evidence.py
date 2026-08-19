from __future__ import annotations

import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

from engine import anomaly_engine as anomaly
from engine.energy_kpi_engine import TIME_FORMAT, is_stale
from engine.health_targets import ANOMALY_FEED_STALENESS_SECONDS, EVENTS_FEED_STALENESS_SECONDS, EVENTS_LOOKBACK_DAYS

"""
Phase 12.1 - Equipment Health evidence assembly. Pure READS against
EXISTING tables only (anomalies, equipment, maintenance_log,
machine_events) - no new schema, no historian rescan, no writes. This
module answers "what evidence exists?"; engine/health_engine.py alone
turns that evidence into a score.

Reuses, never reimplements:
  - engine.anomaly_engine._equipment_id_for_instance() / find_open_anomaly()
    / count_prior_occurrences() - the exact same equipment-resolution
    and recurrence-counting Phase 9 already proved correct.
  - engine.energy_kpi_engine.is_stale() - the exact same freshness
    primitive Phase 6 already proved correct, reused here for two
    system-wide FEED freshness gates (item 17/36), not per-tag reads.
"""

DATE_FORMAT = "%Y-%m-%d"


def _connect(config_database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    return connection


def equipment_id_for_instance(config_database_path: str | Path, instance_key: str) -> int | None:
    return anomaly._equipment_id_for_instance(config_database_path, instance_key)


def equipment_criticality(config_database_path: str | Path, equipment_id: int | None) -> str | None:
    """Never fabricated (item 16) - returns None when unconfigured,
    exactly like Phase 10's own engine.opportunity_engine._equipment_criticality()."""
    if equipment_id is None:
        return None
    connection = sqlite3.connect(config_database_path)
    try:
        row = connection.execute("SELECT criticality FROM equipment WHERE id = ?", (equipment_id,)).fetchone()
        return row[0] if row and row[0] else None
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# System-wide feed freshness (item 17/36) - "has this feed produced ANY
# row recently, anywhere?" is a DATA QUALITY question, answered once per
# calculation pass, never per-equipment. An equipment's own "nothing
# recent for me" is only trustworthy evidence when the feed itself is
# confirmed live.
# ---------------------------------------------------------------------------

def anomaly_feed_is_fresh(config_database_path: str | Path, now: datetime | None = None) -> bool:
    """Deliberately checks baseline_context_summary.computed_at, NOT
    anomalies.last_seen. A healthy equipment population can legitimately
    have ZERO anomaly rows forever (nothing ever crossed a threshold) -
    that's indistinguishable from 'the anomaly worker never ran' if
    anomalies itself were the only signal. baseline_context_summary is
    written on every baseline_worker cycle UNCONDITIONALLY (anomaly
    detection depends on it), so its own recency is the correct proxy
    for 'is the pipeline this factor family depends on actually alive'."""
    now = now or datetime.now()
    connection = sqlite3.connect(config_database_path)
    try:
        row = connection.execute("SELECT MAX(computed_at) FROM baseline_context_summary").fetchone()
    finally:
        connection.close()
    if not row or not row[0]:
        return False
    try:
        latest = datetime.strptime(row[0], TIME_FORMAT)
    except ValueError:
        return False
    return not is_stale(latest, now, ANOMALY_FEED_STALENESS_SECONDS)


def events_feed_is_fresh(machine_database_path: str | Path, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    try:
        connection = sqlite3.connect(machine_database_path)
        row = connection.execute("SELECT MAX(event_time) FROM machine_events").fetchone()
        connection.close()
    except sqlite3.OperationalError:
        return False
    if not row or not row[0]:
        return False
    try:
        latest = datetime.strptime(row[0][:19], TIME_FORMAT)
    except ValueError:
        return False
    return not is_stale(latest, now, EVENTS_FEED_STALENESS_SECONDS)


# ---------------------------------------------------------------------------
# Anomaly-sourced evidence (items 10, 11, 13)
# ---------------------------------------------------------------------------

def fetch_anomaly_evidence(
    config_database_path: str | Path, plant_id: int, instance_key: str, target_key: str, rule_key: str,
) -> dict[str, Any] | None:
    """The single OPEN anomaly for this rule if one exists, else the
    most recently RESOLVED one (for recency-decayed residual evidence),
    else None (no anomaly evidence exists for this factor at all - a
    distinct, honest case from 'confirmed clean'). Always attaches the
    real RESOLVED-row recurrence count via
    anomaly_engine.count_prior_occurrences() - never a bare counter."""
    open_row = anomaly.find_open_anomaly(config_database_path, plant_id, instance_key, target_key, rule_key)
    if open_row is not None:
        row = open_row
    else:
        connection = _connect(config_database_path)
        try:
            resolved = connection.execute(
                "SELECT * FROM anomalies WHERE plant_id = ? AND instance_key = ? AND target_key = ? AND rule_key = ? "
                "AND status = 'RESOLVED' ORDER BY resolved_at DESC LIMIT 1",
                (plant_id, instance_key, target_key, rule_key),
            ).fetchone()
        finally:
            connection.close()
        row = dict(resolved) if resolved else None

    if row is None:
        return None

    prior_occurrences = anomaly.count_prior_occurrences(config_database_path, plant_id, instance_key, target_key, rule_key)
    return {
        "status": row["status"],
        "severity": row["severity"],
        "confidence": row["baseline_confidence"],
        "last_seen": row["last_seen"],
        "resolved_at": row["resolved_at"],
        "occurrence_count": prior_occurrences,
        "engineering_limit_status": row["engineering_limit_status"],
        "actual_value": row["actual_value"],
        "expected_value": row["expected_value"],
        "anomaly_id": row["id"],
    }


def has_mature_baseline(config_database_path: str | Path, instance_key: str, target_key: str) -> bool:
    """Distinguishes 'confirmed clean - a mature baseline exists for
    this target and simply never triggered an anomaly' from 'no
    evidence either way - the baseline never reached maturity' (item
    19). Without this check, a target whose baseline never matured
    would look identical to one that's genuinely healthy - exactly the
    'no evidence -> Health Score 100' failure mode you explicitly ruled
    out. Checks the 'recent' baseline (the one anomaly detection
    actually gates on), not 'reference'."""
    connection = _connect(config_database_path)
    try:
        row = connection.execute(
            "SELECT 1 FROM baseline_context_summary WHERE instance_key = ? AND target_key = ? "
            "AND baseline_type = 'recent' AND baseline_status = 'mature' LIMIT 1",
            (instance_key, target_key),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Maintenance evidence (item 15) - mirrors ui/pages/4_Service_and_Maintenance.py's
# own _next_due() computation exactly (same formula: explicit next_due_at,
# else last_serviced_at + service_interval_days). Not imported from the
# UI page (wrong dependency direction for an engine/ module) - reproduced
# here as the same small, well-understood formula.
# ---------------------------------------------------------------------------

def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], DATE_FORMAT).date()
    except ValueError:
        return None


def fetch_maintenance_evidence(config_database_path: str | Path, equipment_id: int | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    today = now.date()

    if equipment_id is None:
        return {"has_schedule_data": False, "next_due": None, "days_overdue": None}

    connection = _connect(config_database_path)
    try:
        row = connection.execute(
            "SELECT service_interval_days, last_serviced_at, next_due_at FROM equipment WHERE id = ?", (equipment_id,)
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        return {"has_schedule_data": False, "next_due": None, "days_overdue": None}

    explicit = _parse_date(row["next_due_at"])
    next_due = explicit
    if next_due is None:
        last_serviced = _parse_date(row["last_serviced_at"])
        interval = row["service_interval_days"]
        if last_serviced is not None and interval:
            next_due = last_serviced + timedelta(days=int(interval))

    if next_due is None:
        # No usable schedule data at all - never fabricated (item 15).
        return {"has_schedule_data": False, "next_due": None, "days_overdue": None}

    days_overdue = (today - next_due).days
    return {
        "has_schedule_data": True,
        "next_due": next_due.strftime(DATE_FORMAT),
        "days_overdue": days_overdue if days_overdue > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Events evidence (item 14) - qualifying alarm/warning events for THIS
# equipment's own tags only (matched by real tag prefix, never the
# unreliable/legacy machine_events.equipment bucket label), within a
# bounded recent lookback window - never an unrelated plant-wide event
# (e.g. a fire-system alarm) and never an unbounded historian scan.
# ---------------------------------------------------------------------------

def fetch_events_evidence(machine_database_path: str | Path, instance_key: str, now: datetime | None = None) -> dict[str, Any]:
    """Counts DISTINCT (tag, condition) pairs, not raw event rows.
    event_monitor re-logs a new row roughly every polling cycle a tag
    REMAINS outside its threshold (confirmed by direct inspection - one
    persistently-breached tag/condition produced 300+ rows over the
    lookback window) - a raw row count would treat one enduring
    condition as hundreds of separate incidents, saturating this
    factor for nearly every piece of equipment and making it useless
    for prioritization. This mirrors the project's own established
    fix for the identical noise pattern (ai/prompt_builder.py's
    _condense_evidence(), documented in CLAUDE.md: 'collapses to one
    line per distinct (tag, condition) pair')."""
    now = now or datetime.now()
    window_start = (now - timedelta(days=EVENTS_LOOKBACK_DAYS)).strftime(TIME_FORMAT)

    try:
        connection = sqlite3.connect(machine_database_path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT COUNT(DISTINCT tag || '|' || condition) AS n, MAX(event_time) AS latest FROM machine_events "
            "WHERE tag LIKE ? AND severity IN ('alarm', 'warning') AND event_time >= ?",
            (f"{instance_key}.%", window_start),
        ).fetchone()
        connection.close()
    except sqlite3.OperationalError:
        return {"qualifying_event_count": 0, "latest_event_time": None}

    return {"qualifying_event_count": rows["n"] if rows else 0, "latest_event_time": rows["latest"] if rows else None}
