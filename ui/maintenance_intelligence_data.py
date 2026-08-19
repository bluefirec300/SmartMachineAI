from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path
from engine import maintenance_intelligence_engine as mie
from engine.maintenance_intelligence_targets import PRIORITY_SORT_RANK
from ui import health_data as hd

"""
Phase 13 - UI data-access layer for Maintenance Intelligence. Read-only,
mirrors ui/health_data.py's own convention exactly. Unlike Equipment
Health, Phase 13 has NO persisted snapshot table of its own (the
approved plan's on-demand/read-only architecture) - every read here
calls engine.maintenance_intelligence_engine directly, which itself
only reads Phase 12's ALREADY-persisted evidence. No scoring, no
change-detection, no persistence-threshold logic lives in this module.

Strict vocabulary/color separation (mirrors ui/health_data.py's own
BAND_BADGE_COLORS discipline): Maintenance Priority uses its own
NOT_ASSESSED/ROUTINE/REVIEW/PRIORITY/URGENT_REVIEW palette, deliberately
distinct from both ai/rule_engine.py's alarm palette AND
ui.health_data.BAND_BADGE_COLORS - Health and Maintenance Priority are
separate concepts and must never look interchangeable on screen.
"""

CONFIG_DATABASE_PATH = get_config_db_path()

PRIORITY_BADGE_COLORS: dict[str, str] = {
    "URGENT_REVIEW": "#ffb3b3",
    "PRIORITY": "#ffd699",
    "REVIEW": "#ffe6a3",
    "ROUTINE": "#b3ffb3",
    "NOT_ASSESSED": "#e6e6e6",
}

PRIORITY_LABELS: dict[str, str] = {
    "URGENT_REVIEW": "Urgent Review",
    "PRIORITY": "Priority",
    "REVIEW": "Review",
    "ROUTINE": "Routine",
    "NOT_ASSESSED": "Not Assessed",
}

CONFIDENCE_LABELS: dict[str, str] = {
    "HIGH": "High",
    "MEDIUM": "Medium",
    "LOW": "Low",
    "INSUFFICIENT": "Insufficient",
}


def priority_label(priority: str | None) -> str:
    if not priority:
        return "-"
    return PRIORITY_LABELS.get(priority, priority.replace("_", " ").title())


def priority_badge_html(priority: str | None) -> str:
    color = PRIORITY_BADGE_COLORS.get(priority, "#e6e6e6")
    return (
        f"<span style='background-color:{color}; color:#1a1a1a; padding:2px 10px; "
        f"border-radius:4px; font-weight:600;'>{priority_label(priority)}</span>"
    )


def confidence_label(confidence: str | None) -> str:
    if not confidence:
        return "-"
    return CONFIDENCE_LABELS.get(confidence, confidence.title())


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _equipment_hierarchy(equipment_ids: list[int]) -> dict[int, dict[str, Any]]:
    """One bulk query for plant/area/system/display_name context - same
    shape as ui.health_data's own private helper, kept as a small local
    copy rather than reaching into another module's underscore-prefixed
    function."""
    if not equipment_ids:
        return {}
    connection = _connect()
    placeholders = ",".join("?" for _ in equipment_ids)
    try:
        rows = connection.execute(
            f"""
            SELECT e.id AS equipment_id, e.display_name, e.name, e.brand, e.model,
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


def get_plants() -> list[dict[str, Any]]:
    return hd.get_plants()


def get_overview(plant_code: str | None = None) -> list[dict[str, Any]]:
    """Every currently-eligible equipment instance's Maintenance Priority,
    calculated on-demand (no persisted overview table - the approved
    Phase 13 architecture). Bounded by the same equipment population
    Equipment Health itself covers (~80 instances at dev-stage scale) -
    a per-instance calculate_maintenance_priority() call each, mirroring
    the existing Service & Maintenance page's own per-equipment loop
    convention rather than a premature bulk-query optimization for a
    dataset this size."""
    plants = get_plants()
    if plant_code:
        plants = [p for p in plants if p["code"] == plant_code]

    priority_results = []
    for plant in plants:
        priority_results.extend(mie.calculate_all(CONFIG_DATABASE_PATH, plant["code"]))

    if not priority_results:
        return []

    equipment_ids = [r.equipment_id for r in priority_results if r.equipment_id is not None]
    hierarchy = _equipment_hierarchy(equipment_ids)

    rows = []
    for r in priority_results:
        context = hierarchy.get(r.equipment_id, {})
        rows.append({
            "equipment_id": r.equipment_id,
            "plant_id": r.plant_id,
            "plant_code": r.plant_code,
            "instance_key": r.instance_key,
            "equipment_type": r.equipment_type,
            "display_name": context.get("display_name") or r.instance_key,
            "area_name": context.get("area_name"),
            "system_name": context.get("system_name"),
            "maintenance_priority": r.maintenance_priority,
            "priority_score": r.priority_score,
            "floor_applied": r.floor_applied,
            "recommendation_confidence": r.recommendation_confidence,
            "health_score": r.health_score,
            "health_band": r.health_band,
            "recent_movement": r.recent_movement,
            "criticality": r.criticality,
            "maintenance_status": r.maintenance_status,
            "recommended_checks": r.recommended_checks,
        })
    return rows


def get_filter_options(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "areas": sorted({r["area_name"] for r in rows if r["area_name"]}),
        "systems": sorted({r["system_name"] for r in rows if r["system_name"]}),
        "equipment_types": sorted({r["equipment_type"] for r in rows if r["equipment_type"]}),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure aggregation over already-fetched rows - no new calculation."""
    counts = {p: 0 for p in PRIORITY_LABELS}
    for r in rows:
        if r["maintenance_priority"] in counts:
            counts[r["maintenance_priority"]] += 1
    return {
        "total": len(rows),
        "urgent_review": counts["URGENT_REVIEW"],
        "priority": counts["PRIORITY"],
        "review": counts["REVIEW"],
        "routine": counts["ROUTINE"],
        "not_assessed": counts["NOT_ASSESSED"],
    }


def sort_overview(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Default sort - most-needs-attention first (addendum item 1):
    URGENT_REVIEW > PRIORITY > REVIEW > NOT_ASSESSED > ROUTINE, then by
    priority_score descending within the same class."""
    return sorted(
        rows, key=lambda r: (PRIORITY_SORT_RANK.get(r["maintenance_priority"], 0), r["priority_score"]), reverse=True,
    )


def get_detail(plant_id: int, plant_code: str, equipment_type: str, instance_key: str) -> dict[str, Any]:
    """Single-equipment, on-demand - the Maintenance Intelligence detail
    view's data source. Calls the engine directly (cheap for one
    equipment) - never reads from get_overview()'s already-fetched rows,
    since a detail view legitimately wants the FULL result contract
    (priority_factors/recommended_checks/limitations), not the
    overview's flattened row shape."""
    result = mie.calculate_maintenance_priority(CONFIG_DATABASE_PATH, plant_id, plant_code, equipment_type, instance_key)
    context = _equipment_hierarchy([result.equipment_id]).get(result.equipment_id, {}) if result.equipment_id else {}
    return {"result": result, "context": context}


def compact_priority_context(plant_id: int, plant_code: str, equipment_type: str, instance_key: str) -> dict[str, Any]:
    """Pre-formatted, ready-to-render strings for a ONE-equipment compact
    Maintenance Priority block - the Equipment Health page's cross-link
    integration point (Phase 13 plan item 27)."""
    result = mie.calculate_maintenance_priority(CONFIG_DATABASE_PATH, plant_id, plant_code, equipment_type, instance_key)
    return {
        "priority": result.maintenance_priority,
        "priority_label": priority_label(result.maintenance_priority),
        "confidence": confidence_label(result.recommendation_confidence),
        "floor_applied": result.floor_applied,
        "floor_reason": result.floor_reason,
    }
