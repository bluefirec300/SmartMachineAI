from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine import data_health_domain as dhdom
from engine import health_evidence as hev
from engine.data_health_persistence_targets import RECURRING_ISSUE_FIELDS
from engine.data_health_targets import CONFIDENCE_STATUS_VALUES
from engine.health_engine import discover_health_targets

"""
Phase 16.5 - Data Health historical retrieval / trend API. This is the
clean backend contract the UI consumes - a future Streamlit page/adapter
reads THIS module, never data_health_snapshots directly (mirrors
engine/health_history.py's own established convention).

No prediction, no RUL, no failure probability anywhere in this module -
only descriptive, backward-looking arithmetic on already-persisted
snapshot rows ("status was GOOD for 97% of the observed window"), never
a forward-looking claim. History is authoritative only from the moment
persistence began (see first_recorded_at()) - periods before that are
NEVER fabricated, always reported as an explicit "no history" gap.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_DIFF_FIELDS: tuple[str, ...] = (
    "confidence_score", "freshness_score", "availability_score", "validity_score", "continuity_score",
    "missing_tag_count", "stale_tag_count", "invalid_tag_count", "gap_count",
    "timestamp_issue_count", "indeterminate_freshness_count", "frozen_candidate_count",
)


def _parse(value: str) -> datetime:
    return datetime.strptime(value[:19], TIME_FORMAT)


def _window(now: datetime | None, days: float | None, hours: float | None, start: str | None, end: str | None) -> tuple[str, str]:
    if start is not None and end is not None:
        return start, end
    now = now or datetime.now()
    back = timedelta(hours=hours or 0, days=days or (7 if hours is None else 0))
    return (now - back).strftime(TIME_FORMAT), now.strftime(TIME_FORMAT)


# ---------------------------------------------------------------------------
# Per-equipment history
# ---------------------------------------------------------------------------

def get_latest(config_database_path: str | Path, plant_id: int, instance_key: str) -> dict[str, Any] | None:
    return dhdom.get_latest_snapshot(config_database_path, plant_id, instance_key)


def get_history(
    config_database_path: str | Path, plant_id: int, instance_key: str,
    hours: float | None = None, days: float | None = None, now: datetime | None = None,
    start: str | None = None, end: str | None = None, limit: int | None = None,
) -> list[dict[str, Any]]:
    """Chronological (oldest first) history, bounded either by an
    explicit [start, end] or by a convenience hours=/days=-back window
    from `now` - mirrors engine.health_history.get_history()'s own
    convention exactly."""
    if hours is not None or days is not None:
        now = now or datetime.now()
        back = timedelta(hours=hours or 0, days=days or 0)
        start = (now - back).strftime(TIME_FORMAT)
        end = now.strftime(TIME_FORMAT)
    return dhdom.list_snapshots(config_database_path, plant_id, instance_key, start=start, end=end, limit=limit)


def first_recorded_at(config_database_path: str | Path) -> str | None:
    """"History recorded since <this>" - the approved honest-disclosure
    wording. None means no Data Health snapshot has ever been persisted
    yet (the worker has not completed its first cycle)."""
    return dhdom.first_snapshot_time(config_database_path)


def component_score_history(
    config_database_path: str | Path, plant_id: int, instance_key: str,
    start: str | None = None, end: str | None = None, days: float | None = None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """A thin charting-ready projection of get_history() - Data
    Confidence plus the four independent component scores, nothing
    else. Never smoothed/interpolated - exactly the persisted values,
    including a short POOR/UNAVAILABLE dip if one was recorded."""
    history = get_history(config_database_path, plant_id, instance_key, start=start, end=end, days=days, now=now)
    return [
        {
            "computed_at": row["computed_at"],
            "confidence_score": row["confidence_score"], "confidence_status": row["confidence_status"],
            "freshness_score": row["freshness_score"], "availability_score": row["availability_score"],
            "validity_score": row["validity_score"], "continuity_score": row["continuity_score"],
        }
        for row in history
    ]


def status_duration(config_database_path: str | Path, plant_id: int, instance_key: str, start: str, end: str) -> dict[str, Any]:
    """
    Time-based (never sample-count-based) status-duration distribution
    over [start, end].

    Each persisted snapshot's status is assumed to hold from its own
    computed_at until the NEXT snapshot's computed_at - the standard
    event-sourced/right-censored convention, valid here specifically
    because a snapshot is only ever written on a real material change
    or a 24h heartbeat (engine.data_health_orchestration), so silence
    between two snapshots genuinely means "the last known state
    persisted," never an unknown gap.

    Final interval: the last snapshot at-or-before `end` is assumed to
    hold through `end` itself ("still believed to be in this status as
    of the query boundary, unless proven otherwise") - always reported
    via final_interval_treatment="extended_to_end" so a caller/UI can
    disclose this rather than silently assume it.

    Leading gap: if `start` predates the earliest snapshot this
    equipment actually has, that leading portion is reported as an
    explicit no_history_seconds/no_history_percentage bucket - NEVER
    fabricated as any real status (the approved historical contract).
    Uses the one snapshot immediately before `start` (the "carry-in"),
    when one exists, so the window's leading edge is correctly
    attributed to whatever state was already true at `start`.
    """
    carry_in = dhdom.get_snapshot_before(config_database_path, plant_id, instance_key, start)
    window_rows = dhdom.list_snapshots(config_database_path, plant_id, instance_key, start=start, end=end)

    if carry_in is None and not window_rows:
        return {
            "insufficient_history": True,
            "durations_seconds": {}, "percentages": {},
            "no_history_seconds": None, "no_history_percentage": None,
            "observed_seconds": 0.0, "final_interval_treatment": "extended_to_end",
        }

    start_dt, end_dt = _parse(start), _parse(end)
    total_seconds = (end_dt - start_dt).total_seconds()

    sequence: list[dict[str, Any]] = []
    no_history_seconds = 0.0

    if carry_in is not None:
        sequence.append({"status": carry_in["confidence_status"], "effective_at": start_dt})
    else:
        first_at = _parse(window_rows[0]["computed_at"])
        if first_at > start_dt:
            no_history_seconds += (first_at - start_dt).total_seconds()
        sequence.append({"status": window_rows[0]["confidence_status"], "effective_at": max(first_at, start_dt)})
        window_rows = window_rows[1:]

    for row in window_rows:
        sequence.append({"status": row["confidence_status"], "effective_at": _parse(row["computed_at"])})

    durations = {status: 0.0 for status in CONFIDENCE_STATUS_VALUES}
    for current, nxt in zip(sequence, sequence[1:]):
        span = max((nxt["effective_at"] - current["effective_at"]).total_seconds(), 0.0)
        if current["status"] in durations:
            durations[current["status"]] += span

    last = sequence[-1]
    final_span = max((end_dt - last["effective_at"]).total_seconds(), 0.0)
    if last["status"] in durations:
        durations[last["status"]] += final_span

    percentages = {
        status: (round((seconds / total_seconds) * 100.0, 1) if total_seconds > 0 else None)
        for status, seconds in durations.items()
    }
    no_history_percentage = round((no_history_seconds / total_seconds) * 100.0, 1) if total_seconds > 0 else None

    return {
        "insufficient_history": False,
        "durations_seconds": {k: round(v, 1) for k, v in durations.items()},
        "percentages": percentages,
        "no_history_seconds": round(no_history_seconds, 1),
        "no_history_percentage": no_history_percentage,
        "observed_seconds": round(total_seconds, 1),
        "final_interval_treatment": "extended_to_end",
    }


def _field_diffs(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"field": field, "from": previous[field], "to": current[field]}
        for field in _DIFF_FIELDS if previous[field] != current[field]
    ]


def status_transitions(config_database_path: str | Path, plant_id: int, instance_key: str, start: str, end: str) -> list[dict[str, Any]]:
    """Consecutive-pair confidence_status transitions within [start, end]
    - mirrors engine.health_history.band_transitions()'s own style. Each
    transition's `changes` field is a deterministic list of structured
    field-level differences (persisted counts/scores only) - never an
    AI-generated narrative, and never a specific tag name (only the
    aggregate counts this schema actually persists)."""
    carry_in = dhdom.get_snapshot_before(config_database_path, plant_id, instance_key, start)
    window_rows = dhdom.list_snapshots(config_database_path, plant_id, instance_key, start=start, end=end)
    sequence = ([carry_in] if carry_in is not None else []) + window_rows

    transitions = []
    for previous, current in zip(sequence, sequence[1:]):
        if previous["confidence_status"] != current["confidence_status"]:
            transitions.append({
                "from_status": previous["confidence_status"], "to_status": current["confidence_status"],
                "at": current["computed_at"], "changes": _field_diffs(previous, current),
            })
    return transitions


def recurring_issues(config_database_path: str | Path, plant_id: int, instance_key: str, start: str, end: str) -> dict[str, Any]:
    """
    'inactive -> active = new occurrence' counting (the approved
    methodology) - a fresh onset of an issue (its persisted count going
    from 0 to >0) counts once; the issue remaining active across
    subsequent snapshots, including 24h-heartbeat rows, never adds a
    further occurrence. Uses the same carry-in technique as
    status_duration()/status_transitions() so a transition AT the very
    start of the window is measured against the true prior state, never
    miscounted as a fresh onset merely because it's the first row seen.

    The very first observed row (carry-in if one exists, else the
    window's own first snapshot) never counts its own already-nonzero
    counts as a fresh occurrence - we do not know when that pre-existing
    condition actually began, so it is deliberately NOT credited as
    "starting" inside this window (never fabricated history).
    """
    carry_in = dhdom.get_snapshot_before(config_database_path, plant_id, instance_key, start)
    window_rows = dhdom.list_snapshots(config_database_path, plant_id, instance_key, start=start, end=end)
    sequence = ([carry_in] if carry_in is not None else []) + window_rows

    if not sequence:
        return {"insufficient_history": True, "occurrences": {}}

    occurrences = {issue: 0 for issue in RECURRING_ISSUE_FIELDS}
    previous_counts = {issue: 0 for issue in RECURRING_ISSUE_FIELDS}
    first = True
    for row in sequence:
        for issue, field in RECURRING_ISSUE_FIELDS.items():
            current_count = row[field]
            if not first and previous_counts[issue] == 0 and current_count > 0:
                occurrences[issue] += 1
            previous_counts[issue] = current_count
        first = False

    return {"insufficient_history": False, "occurrences": occurrences}


# ---------------------------------------------------------------------------
# Fleet-wide, hierarchy-grouped historical summary (item 21). Mirrors
# engine.data_health_fleet's own enumeration/aggregation conventions -
# same equipment population (discover_health_targets()), same
# aggregate-in-Python-over-already-fetched-rows discipline, same "never
# a single averaged historical score" rule.
# ---------------------------------------------------------------------------

def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def _plant_codes(config_database_path: str | Path) -> list[str]:
    connection = _connect(config_database_path)
    try:
        return [r["code"] for r in connection.execute("SELECT code FROM plants ORDER BY code")]
    finally:
        connection.close()


def _plant_id_for_code(config_database_path: str | Path, plant_code: str) -> int | None:
    connection = _connect(config_database_path)
    try:
        row = connection.execute("SELECT id FROM plants WHERE code = ?", (plant_code,)).fetchone()
        return row["id"] if row else None
    finally:
        connection.close()


def _equipment_hierarchy(config_database_path: str | Path, equipment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not equipment_ids:
        return {}
    connection = _connect(config_database_path)
    placeholders = ",".join("?" for _ in equipment_ids)
    try:
        rows = connection.execute(
            f"""
            SELECT e.id AS equipment_id, e.display_name,
                   p.code AS plant_code, a.name AS area_name, s.name AS system_name
            FROM equipment e
            LEFT JOIN plants p ON p.id = e.plant_id
            LEFT JOIN areas a ON a.id = e.area_id
            LEFT JOIN systems s ON s.id = e.system_id
            WHERE e.id IN ({placeholders})
            """,
            equipment_ids,
        ).fetchall()
    finally:
        connection.close()
    return {r["equipment_id"]: dict(r) for r in rows}


def fleet_status_summary(
    config_database_path: str | Path, start: str, end: str, plant_codes: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """
    One row per currently-eligible equipment instance: its
    status_duration()/status_transitions()/recurring_issues() over
    [start, end], enriched with plant/area/system hierarchy - plus
    group summaries by plant/area/system/equipment_type. Deliberately
    NEVER a single averaged historical score - only transparent
    per-status time (seconds/percentages), transition counts, and
    recurrence counts, exactly mirroring the "distribution, not a
    score" rule Phase 16.4's current-state fleet view already follows.
    """
    codes = plant_codes if plant_codes is not None else tuple(_plant_codes(config_database_path))

    equipment_rows: list[dict[str, Any]] = []
    for plant_code in codes:
        plant_id = _plant_id_for_code(config_database_path, plant_code)
        if plant_id is None:
            continue
        for equipment_type, instance_key in discover_health_targets(config_database_path, plant_code):
            equipment_id = hev.equipment_id_for_instance(config_database_path, instance_key)
            duration = status_duration(config_database_path, plant_id, instance_key, start, end)
            transitions = status_transitions(config_database_path, plant_id, instance_key, start, end)
            recurrence = recurring_issues(config_database_path, plant_id, instance_key, start, end)
            equipment_rows.append({
                "instance_key": instance_key, "equipment_id": equipment_id, "equipment_type": equipment_type,
                "plant_code": plant_code, "duration": duration,
                "transition_count": len(transitions), "recurrence": recurrence["occurrences"],
                "insufficient_history": duration["insufficient_history"] or recurrence["insufficient_history"],
            })

    equipment_ids = [r["equipment_id"] for r in equipment_rows if r["equipment_id"] is not None]
    hierarchy = _equipment_hierarchy(config_database_path, equipment_ids)
    for row in equipment_rows:
        context = hierarchy.get(row["equipment_id"], {})
        row["display_name"] = context.get("display_name") or row["instance_key"]
        row["area_name"] = context.get("area_name")
        row["system_name"] = context.get("system_name")

    def _empty_group() -> dict[str, Any]:
        return {"equipment": 0, "seconds": {s: 0.0 for s in CONFIDENCE_STATUS_VALUES}, "transitions": 0}

    by_plant: dict[str, dict[str, Any]] = {}
    by_area: dict[str, dict[str, Any]] = {}
    by_system: dict[str, dict[str, Any]] = {}
    by_equipment_type: dict[str, dict[str, Any]] = {}

    def _accumulate(groups: dict[str, dict[str, Any]], key: str | None, row: dict[str, Any]) -> None:
        if not key or row["insufficient_history"]:
            return
        bucket = groups.setdefault(key, _empty_group())
        bucket["equipment"] += 1
        for status, seconds in row["duration"]["durations_seconds"].items():
            bucket["seconds"][status] += seconds
        bucket["transitions"] += row["transition_count"]

    for row in equipment_rows:
        _accumulate(by_plant, row["plant_code"], row)
        _accumulate(by_area, row["area_name"], row)
        _accumulate(by_system, row["system_name"], row)
        _accumulate(by_equipment_type, row["equipment_type"], row)

    return {
        "start": start, "end": end,
        "equipment": equipment_rows,
        "by_plant": by_plant, "by_area": by_area, "by_system": by_system, "by_equipment_type": by_equipment_type,
    }
