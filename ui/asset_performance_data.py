from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path
from engine import health_evidence as hev
from engine import performance_domain as dom
from engine import performance_ranking as rank
from engine.performance_targets import PERFORMANCE_DIMENSION_REGISTRY
from ui import health_data as hd
from ui import table_style as ts

"""
Phase 14 - UI data-access layer for Asset Performance & Reliability
Analytics. Read-only, mirrors ui/health_data.py's / ui/maintenance_intelligence_data.py's
own convention exactly. Every read here goes through engine.performance_domain
(already-persisted rows) or engine.performance_ranking (a pure
composition over already-fetched rows) - never recalculates a
performance observation itself (that is engine.performance_engine's job,
run only by the worker).

Strict vocabulary/color separation: performance states use their own
IMPROVING/STABLE/DEGRADING/SIGNIFICANTLY_DEGRADING/INSUFFICIENT_EVIDENCE/
NOT_CLASSIFIED palette, deliberately distinct from both the alarm
palette and ui.health_data.BAND_BADGE_COLORS - Health, Maintenance
Priority, and Asset Performance are three separate concepts.
"""

CONFIG_DATABASE_PATH = get_config_db_path()

# UI polish phase - which semantic tier (ui.table_style) each existing
# deterministic state maps to for display. Reinforces the engine's own
# IMPROVING/STABLE/DEGRADING/SIGNIFICANTLY_DEGRADING/
# INSUFFICIENT_EVIDENCE/NOT_CLASSIFIED classification - introduces no
# new state, only presentation.
STATE_TIERS: dict[str, str] = {
    "IMPROVING": ts.POSITIVE,
    "STABLE": ts.STABLE,
    "DEGRADING": ts.CAUTION,
    "SIGNIFICANTLY_DEGRADING": ts.WARNING,
    "INSUFFICIENT_EVIDENCE": ts.NEUTRAL,
    "NOT_CLASSIFIED": ts.NEUTRAL,
}

STATE_LABELS: dict[str, str] = {
    "IMPROVING": "Improving",
    "STABLE": "Stable",
    "DEGRADING": "Degrading",
    "SIGNIFICANTLY_DEGRADING": "Significantly Degrading",
    "INSUFFICIENT_EVIDENCE": "Insufficient Evidence",
    "NOT_CLASSIFIED": "Not Classified (informational)",
}

EFFECTIVENESS_LABELS: dict[str, str] = {
    "IMPROVED": "Improved",
    "NO_MEASURABLE_CHANGE": "No Measurable Change",
    "WORSENED": "Worsened",
    "INSUFFICIENT_EVIDENCE": "Insufficient Evidence",
    "NOT_CLASSIFIED": "Not Classified",
}

# Presentation-only target_key labels - canonical target_key remains
# fully visible alongside (mirrors Phase 12.3A's FACTOR_LABELS discipline).
_TARGET_KEY_LABELS: dict[str, str] = {
    "Power_kW": "Power", "cop": "COP (Coefficient of Performance)", "cooling_output_kw": "Cooling Output",
    "flow_per_kw": "Flow per kW", "delta_p_bar": "Differential Pressure", "non_production_demand_kw": "Non-production Demand",
    "compressed_air_specific_energy": "Specific Energy (kWh/Nm3)", "FanPower": "Fan Power", "FilterDP": "Filter Differential Pressure",
    "MotorPower": "Motor Power", "MotorCurrent": "Motor Current", "CompressorPower": "Compressor Power",
}


def target_key_label(target_key: str | None) -> str:
    if not target_key:
        return "-"
    return _TARGET_KEY_LABELS.get(target_key, target_key.replace("_", " ").title())


def state_label(state: str | None) -> str:
    if not state:
        return "-"
    return STATE_LABELS.get(state, state.replace("_", " ").title())


def effectiveness_label(result: str | None) -> str:
    if not result:
        return "-"
    return EFFECTIVENESS_LABELS.get(result, result.replace("_", " ").title())


def state_badge_html(state: str | None) -> str:
    return ts.badge_html(state_label(state), STATE_TIERS.get(state))


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def get_plants() -> list[dict[str, Any]]:
    return hd.get_plants()


def _equipment_hierarchy(equipment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not equipment_ids:
        return {}
    connection = _connect()
    placeholders = ",".join("?" for _ in equipment_ids)
    try:
        rows = connection.execute(
            f"""
            SELECT e.id AS equipment_id, e.display_name, e.criticality,
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


def get_overview(plant_code: str | None = None) -> list[dict[str, Any]]:
    """Latest persisted observation for every (equipment, dimension) pair
    - reads ONLY what the worker already persisted, never recalculates."""
    plants = get_plants()
    if plant_code:
        plants = [p for p in plants if p["code"] == plant_code]

    rows: list[dict[str, Any]] = []
    for plant in plants:
        rows.extend(dom.list_latest_observations_for_plant(CONFIG_DATABASE_PATH, plant["id"]))

    if not rows:
        return []

    equipment_ids = [r["equipment_id"] for r in rows if r["equipment_id"] is not None]
    hierarchy = _equipment_hierarchy(equipment_ids)

    enriched = []
    for r in rows:
        context = hierarchy.get(r["equipment_id"], {})
        enriched.append({
            **r,
            "display_name": context.get("display_name") or r["instance_key"],
            "area_name": context.get("area_name"),
            "system_name": context.get("system_name"),
            "criticality": context.get("criticality"),
        })
    return enriched


def get_filter_options(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "areas": sorted({r["area_name"] for r in rows if r["area_name"]}),
        "systems": sorted({r["system_name"] for r in rows if r["system_name"]}),
        "equipment_types": sorted({r["equipment_type"] for r in rows if r["equipment_type"]}),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {s: 0 for s in STATE_LABELS}
    for r in rows:
        if r["performance_state"] in counts:
            counts[r["performance_state"]] += 1
    return {
        "total": len(rows),
        "improving": counts["IMPROVING"], "stable": counts["STABLE"], "degrading": counts["DEGRADING"],
        "significantly_degrading": counts["SIGNIFICANTLY_DEGRADING"],
        "insufficient_evidence": counts["INSUFFICIENT_EVIDENCE"], "not_classified": counts["NOT_CLASSIFIED"],
    }


def get_detail(plant_id: int, instance_key: str, target_key: str, days: float = 90) -> dict[str, Any] | None:
    latest = dom.get_latest_observation(CONFIG_DATABASE_PATH, plant_id, instance_key, target_key)
    if latest is None:
        return None
    history = dom.list_observations(CONFIG_DATABASE_PATH, plant_id, instance_key, target_key, limit=None)
    context = _equipment_hierarchy([latest["equipment_id"]]).get(latest["equipment_id"], {}) if latest["equipment_id"] else {}
    maintenance_comparisons = [
        c for c in dom.list_maintenance_comparisons_for_instance(CONFIG_DATABASE_PATH, plant_id, instance_key)
        if c["target_key"] == target_key
    ]
    return {"latest": latest, "history": history, "context": context, "maintenance_comparisons": maintenance_comparisons}


def get_maintenance_comparisons_for_instance(plant_id: int, instance_key: str) -> list[dict[str, Any]]:
    return dom.list_maintenance_comparisons_for_instance(CONFIG_DATABASE_PATH, plant_id, instance_key)


# ---------------------------------------------------------------------------
# Asset Attention Ranking (item 25/35) - one bulk-fetch pass per plant,
# grouped by equipment, delegated to engine.performance_ranking for the
# actual (evidence-quality-separated) composition.
# ---------------------------------------------------------------------------

def get_attention_ranking(plant_code: str | None = None) -> list[dict[str, Any]]:
    plants = get_plants()
    if plant_code:
        plants = [p for p in plants if p["code"] == plant_code]

    entries = []
    for plant in plants:
        observations = dom.list_latest_observations_for_plant(CONFIG_DATABASE_PATH, plant["id"])
        maintenance_rows = dom.list_latest_maintenance_comparisons_for_plant(CONFIG_DATABASE_PATH, plant["id"])

        by_instance: dict[str, list[dict[str, Any]]] = {}
        for row in observations:
            by_instance.setdefault(row["instance_key"], []).append(row)

        maintenance_by_instance: dict[str, list[dict[str, Any]]] = {}
        for row in maintenance_rows:
            maintenance_by_instance.setdefault(row["instance_key"], []).append(row)

        equipment_ids = [r["equipment_id"] for r in observations if r["equipment_id"] is not None]
        hierarchy = _equipment_hierarchy(equipment_ids)

        for instance_key, instance_observations in by_instance.items():
            equipment_id = instance_observations[0]["equipment_id"]
            equipment_type = instance_observations[0]["equipment_type"]
            context = hierarchy.get(equipment_id, {})
            criticality = context.get("criticality") if equipment_id is not None else hev.equipment_criticality(CONFIG_DATABASE_PATH, equipment_id)

            entry = rank.compute_attention_entry(
                equipment_id, plant["id"], plant["code"], instance_key, equipment_type,
                instance_observations, maintenance_by_instance.get(instance_key, []), criticality,
            )
            entries.append({
                **entry.__dict__,
                "display_name": context.get("display_name") or instance_key,
                "area_name": context.get("area_name"),
                "system_name": context.get("system_name"),
            })

    return entries


def sort_attention_ranking(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """RANKED entries first (highest attention_score first), then
    INSUFFICIENT_EVIDENCE entries - never interleaved as if comparable
    (the mandatory separation: missing evidence is not itself a reason
    to rank above/below a scored equipment)."""
    ranked = sorted(
        [e for e in entries if e["attention_state"] == rank.ATTENTION_STATE_RANKED],
        key=lambda e: e["attention_score"], reverse=True,
    )
    insufficient = [e for e in entries if e["attention_state"] != rank.ATTENTION_STATE_RANKED]
    return ranked + insufficient
