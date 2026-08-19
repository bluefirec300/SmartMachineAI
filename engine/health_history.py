from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine import health_domain as dom
from engine.health_persistence_targets import SCORE_CHANGE_THRESHOLD

"""
Phase 12.2 - Equipment Health historical retrieval / trend API. This IS
the "clean backend contract for Phase 12.3" (item 12) - a future
Streamlit page consumes THIS module (or a thin ui/health_data.py
wrapper matching every prior phase's own convention), never queries
equipment_health_snapshots/equipment_health_factor_snapshots directly.

No prediction, no RUL, no failure probability anywhere in this module
(item 37) - only descriptive, backward-looking arithmetic on already-
persisted numbers ("score changed from X to Y over N days"), never a
forward-looking claim.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

DIRECTION_IMPROVING = "IMPROVING"
DIRECTION_STABLE = "STABLE"
DIRECTION_DETERIORATING = "DETERIORATING"
DIRECTION_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


def _parse_json(text: str | None) -> Any:
    if not text:
        return []
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return []


def _enrich(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["provisional"] = bool(row["provisional"])
    row["limitations"] = _parse_json(row.get("limitations_json"))
    row["assumptions"] = _parse_json(row.get("assumptions_json"))
    row["missing_factors"] = _parse_json(row.get("missing_factors_json"))
    return row


def get_latest(config_database_path: str | Path, plant_id: int, instance_key: str) -> dict[str, Any] | None:
    row = dom.get_latest_snapshot(config_database_path, plant_id, instance_key)
    return _enrich(row) if row else None


def get_history(
    config_database_path: str | Path, plant_id: int, instance_key: str,
    hours: float | None = None, days: float | None = None, now: datetime | None = None,
    start: str | None = None, end: str | None = None, limit: int | None = None,
) -> list[dict[str, Any]]:
    """Chronological (oldest first) history, bounded either by an
    explicit [start, end] or by a convenience hours=/days=-back window
    from `now`. health_score is returned exactly as persisted -
    including None for INSUFFICIENT assessments - never coerced to 0,
    100, or the previous value (item 13)."""
    if hours is not None or days is not None:
        now = now or datetime.now()
        back = timedelta(hours=hours or 0, days=days or 0)
        start = (now - back).strftime(TIME_FORMAT)
        end = now.strftime(TIME_FORMAT)
    rows = dom.list_snapshots(config_database_path, plant_id, instance_key, start=start, end=end, limit=limit)
    return [_enrich(r) for r in rows]


def get_factor_detail(config_database_path: str | Path, health_snapshot_id: int) -> list[dict[str, Any]]:
    return dom.get_factor_snapshots(config_database_path, health_snapshot_id)


def compute_trend(config_database_path: str | Path, plant_id: int, instance_key: str, now: datetime | None = None) -> dict[str, Any]:
    """Purely descriptive, backward-looking trend - NOT a prediction
    (item 14/37). Compares the latest snapshot's score against the most
    recent PRIOR snapshot that also had a real numeric score (skipping
    over any INSUFFICIENT gaps, which have no score to compare)."""
    history = get_history(config_database_path, plant_id, instance_key, days=30, now=now, limit=None)
    if not history:
        return {
            "direction": DIRECTION_INSUFFICIENT_HISTORY, "latest_score": None, "previous_score": None,
            "absolute_change": None, "observation_period_start": None, "observation_period_end": None,
        }

    latest = history[-1]
    if latest["health_score"] is None:
        return {
            "direction": DIRECTION_INSUFFICIENT_HISTORY, "latest_score": None, "previous_score": None,
            "absolute_change": None, "observation_period_start": None, "observation_period_end": latest["computed_at"],
        }

    previous = None
    for row in reversed(history[:-1]):
        if row["health_score"] is not None:
            previous = row
            break

    if previous is None:
        return {
            "direction": DIRECTION_INSUFFICIENT_HISTORY, "latest_score": latest["health_score"], "previous_score": None,
            "absolute_change": None, "observation_period_start": None, "observation_period_end": latest["computed_at"],
        }

    change = round(latest["health_score"] - previous["health_score"], 1)
    if abs(change) < SCORE_CHANGE_THRESHOLD:
        direction = DIRECTION_STABLE
    elif change > 0:
        direction = DIRECTION_IMPROVING
    else:
        direction = DIRECTION_DETERIORATING

    return {
        "direction": direction, "latest_score": latest["health_score"], "previous_score": previous["health_score"],
        "absolute_change": change, "observation_period_start": previous["computed_at"], "observation_period_end": latest["computed_at"],
    }


def band_transitions(config_database_path: str | Path, plant_id: int, instance_key: str, days: float = 30, now: datetime | None = None) -> list[dict[str, Any]]:
    """Consecutive-pair band changes within the window - a health band
    transition, never an automatically-raised alarm (item 15)."""
    history = get_history(config_database_path, plant_id, instance_key, days=days, now=now)
    transitions = []
    for previous, current in zip(history, history[1:]):
        if previous["health_band"] != current["health_band"]:
            transitions.append({
                "from_band": previous["health_band"], "to_band": current["health_band"],
                "at": current["computed_at"],
            })
    return transitions


def confidence_transitions(config_database_path: str | Path, plant_id: int, instance_key: str, days: float = 30, now: datetime | None = None) -> list[dict[str, Any]]:
    """Confidence transitions are tracked entirely separately from score/
    band transitions (item 16) - a confidence change reflects evidence
    completeness, never a physical machine-condition change."""
    history = get_history(config_database_path, plant_id, instance_key, days=days, now=now)
    transitions = []
    for previous, current in zip(history, history[1:]):
        if previous["assessment_confidence"] != current["assessment_confidence"]:
            transitions.append({
                "from_confidence": previous["assessment_confidence"], "to_confidence": current["assessment_confidence"],
                "at": current["computed_at"],
            })
    return transitions
