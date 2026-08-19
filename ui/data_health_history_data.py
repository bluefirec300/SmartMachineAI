from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path
from engine import data_health_history as dhh
from ui.data_health_data import status_label

"""
Phase 16.5 - UI data-access/presentation layer for Data Health HISTORY.
Mirrors ui/data_health_data.py's/ui/data_health_fleet_data.py's own
convention: this module performs NO history calculation of any kind -
it calls engine.data_health_history (which itself only reads the
already-persisted data_health_snapshots table, written independently by
app/data_health_history_worker.py) and converts the result into plain,
presentation-ready dicts.

Caching: a short TTL is enough here (unlike the 120s fleet cache for
LIVE evaluation) - these are indexed reads against a small, already-
persisted table, not a 6-second live computation. 60s keeps the page
responsive to widget changes (window/equipment selection) without
re-querying on every single rerun.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CONFIG_DATABASE_PATH = get_config_db_path()
HISTORY_CACHE_TTL_SECONDS = 60

STATUS_COLORS: dict[str, str] = {
    "GOOD": "#b3ffb3", "DEGRADED": "#ffe6a3", "POOR": "#ffb3b3", "UNAVAILABLE": "#e6e6e6",
}


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _plant_id_for_code(plant_code: str) -> int | None:
    connection = _connect()
    try:
        row = connection.execute("SELECT id FROM plants WHERE code = ?", (plant_code,)).fetchone()
        return row["id"] if row else None
    finally:
        connection.close()


def default_window(days: float = 7, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now()
    start = (now - timedelta(days=days)).strftime(TIME_FORMAT)
    return start, now.strftime(TIME_FORMAT)


@st.cache_data(ttl=HISTORY_CACHE_TTL_SECONDS)
def first_recorded_at() -> str | None:
    """"History recorded since <this>" - None means the worker has not
    persisted its first snapshot yet (an honest, explicit state - never
    silently treated as 'no history exists' vs 'not started yet')."""
    return dhh.first_recorded_at(CONFIG_DATABASE_PATH)


@st.cache_data(ttl=HISTORY_CACHE_TTL_SECONDS)
def get_equipment_history(plant_code: str, instance_key: str, start: str, end: str) -> dict[str, Any]:
    """One equipment's full historical picture over [start, end] -
    chronological snapshot history, status-duration distribution,
    status transitions, recurring-issue counts, and component-score
    history for charting. Never recalculates Data Health itself - every
    field here is a plain read/aggregation over already-persisted rows."""
    plant_id = _plant_id_for_code(plant_code)
    if plant_id is None:
        return {
            "history": [], "duration": {"insufficient_history": True}, "transitions": [],
            "recurrence": {"insufficient_history": True, "occurrences": {}}, "component_history": [],
        }

    history = dhh.get_history(CONFIG_DATABASE_PATH, plant_id, instance_key, start=start, end=end)
    duration = dhh.status_duration(CONFIG_DATABASE_PATH, plant_id, instance_key, start, end)
    transitions = dhh.status_transitions(CONFIG_DATABASE_PATH, plant_id, instance_key, start, end)
    recurrence = dhh.recurring_issues(CONFIG_DATABASE_PATH, plant_id, instance_key, start, end)
    component_history = dhh.component_score_history(CONFIG_DATABASE_PATH, plant_id, instance_key, start=start, end=end)

    return {
        "history": history,
        "duration": duration,
        "duration_status_labels": {status_label(s): pct for s, pct in duration.get("percentages", {}).items()},
        "transitions": transitions,
        "recurrence": recurrence,
        "component_history": component_history,
    }


@st.cache_data(ttl=HISTORY_CACHE_TTL_SECONDS)
def get_fleet_history(start: str, end: str) -> dict[str, Any]:
    """Fleet-wide, hierarchy-grouped historical summary over [start, end]
    - see engine.data_health_history.fleet_status_summary() for the
    full contract. Distribution/count-based only, never a single
    averaged historical score."""
    return dhh.fleet_status_summary(CONFIG_DATABASE_PATH, start, end)


def force_refresh() -> None:
    first_recorded_at.clear()
    get_equipment_history.clear()
    get_fleet_history.clear()
