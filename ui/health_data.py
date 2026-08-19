from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path
from engine import health_domain as dom
from engine import health_history as hist
from engine.health_persistence_targets import SCORE_CHANGE_THRESHOLD

"""
Phase 12.3 - UI data-access layer for Equipment Health. Read-only
(this page never writes anything - health is worker-computed and
worker-persisted, per Phase 12.2's own architecture). Mirrors
ui/savings_verification_data.py's established convention: reads query
directly, using engine.health_domain/engine.health_history exactly as
they exist today - no scoring, no persistence-threshold, and no
change-detection logic is reimplemented here.

Overview reads are all bulk queries (never one query per equipment) so
the page stays fast regardless of how many equipment instances exist -
item 8's explicit performance requirement. The overview NEVER calls
engine.health_engine.calculate_health() - it only reads what the
worker has already persisted.
"""

CONFIG_DATABASE_PATH = get_config_db_path()
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

HEALTH_BAND_ORDER = ("INVESTIGATE", "ATTENTION", "MONITOR", "HEALTHY")
CONFIDENCE_ORDER = ("INSUFFICIENT", "LOW", "MEDIUM", "HIGH")

# Phase 12.4 - the ONE shared color/text contract every page uses (item
# 9/11) - HEALTHY/MONITOR/ATTENTION/INVESTIGATE only, deliberately
# distinct from ai/rule_engine.py's alarm/warning/normal severity
# palette (item 13 - Equipment Health is never presented with alarm
# colors/vocabulary). "Not Assessed" (no snapshot exists at all) is
# kept visually distinct from "Insufficient Data" (a snapshot exists,
# coverage was too low for a score) - two different honest states.
BAND_BADGE_COLORS: dict[str, str] = {
    "HEALTHY": "#b3ffb3",
    "MONITOR": "#d6e9ff",
    "ATTENTION": "#ffe6a3",
    "INVESTIGATE": "#ffb3b3",
    "Insufficient Data": "#e6e6e6",
    "Not Assessed": "#f0f0f0",
}

TREND_LABELS = {
    "IMPROVING": "Improving",
    "STABLE": "Stable",
    "DETERIORATING": "Declining",
    "INSUFFICIENT_HISTORY": "Insufficient history",
}

# ---------------------------------------------------------------------------
# Phase 12.3A - UI-ONLY presentation labels. Centralized here (item 5:
# "do not scatter label replacements throughout the page") so every
# rendering spot uses the same mapping. Backend canonical values
# (engine.health_targets.HEALTH_FACTOR_REGISTRY's factor_id/family,
# engine.health_engine's equipment_type strings) are NEVER renamed,
# NEVER stored, and remain fully visible in the page's technical-detail
# expander - this dict only controls what the MAIN view shows.
# ---------------------------------------------------------------------------

EQUIPMENT_TYPE_LABELS: dict[str, str] = {
    "air_compressor": "Air Compressor",
    "chiller": "Chiller",
    "chilled_water_pump": "Chilled Water Pump",
    "water_supply_pump": "Water Supply Pump",
    "ahu": "AHU",
    "cold_room": "Cold Room",
    "production_process": "Production Equipment",
    "filling": "Filling Machine",
}

# Audited directly against the live engine.health_targets.HEALTH_FACTOR_REGISTRY
# (every factor_id currently defined, across all 8 supported equipment
# types) - not guessed from naming convention alone.
FACTOR_LABELS: dict[str, str] = {
    # Air Compressor
    "ac_power_load": "Electrical Load / Power Behaviour",
    "air_compressor_maintenance_overdue": "Maintenance Status",
    "air_compressor_repeated_events": "Repeated Abnormal Events",
    # Chiller
    "chl_cop_performance": "Coefficient of Performance",
    "chl_power_load": "Electrical Load / Power Behaviour",
    "chl_cooling_output_process": "Cooling Output Condition",
    "chl_supply_temp_process": "Supply Temperature Condition",
    "chl_return_temp_process": "Return Temperature Condition",
    "chl_waterflow_process": "Water Flow Condition",
    "chiller_maintenance_overdue": "Maintenance Status",
    "chiller_repeated_events": "Repeated Abnormal Events",
    # Chilled Water Pump
    "chwp_vibration_condition": "Vibration Condition",
    "chwp_bearing_temp_condition": "Bearing Temperature Condition",
    "chwp_flow_per_kw_condition": "Flow Efficiency (Flow per kW)",
    "chwp_power_load": "Electrical Load / Power Behaviour",
    "chwp_flow_process": "Flow / Process Condition",
    "chwp_delta_p_process": "Differential Pressure Condition",
    "chilled_water_pump_maintenance_overdue": "Maintenance Status",
    "chilled_water_pump_repeated_events": "Repeated Abnormal Events",
    # Water Supply Pump
    "wsp_vibration_condition": "Vibration Condition",
    "wsp_bearing_temp_condition": "Bearing Temperature Condition",
    "wsp_flow_per_kw_condition": "Flow Efficiency (Flow per kW)",
    "wsp_power_load": "Electrical Load / Power Behaviour",
    "wsp_flow_process": "Flow / Process Condition",
    "wsp_delta_p_process": "Differential Pressure Condition",
    "water_supply_pump_maintenance_overdue": "Maintenance Status",
    "water_supply_pump_repeated_events": "Repeated Abnormal Events",
    # AHU
    "ahu_filter_dp_condition": "Filter Differential Pressure Condition",
    "ahu_fan_power_load": "Fan Electrical Load",
    "ahu_supply_air_temp_process": "Supply Air Temperature Condition",
    "ahu_maintenance_overdue": "Maintenance Status",
    "ahu_repeated_events": "Repeated Abnormal Events",
    # Cold Room
    "cr_room_temp_process": "Room Temperature Condition",
    "cr_compressor_power_load": "Compressor Electrical Load",
    "cold_room_maintenance_overdue": "Maintenance Status",
    "cold_room_repeated_events": "Repeated Abnormal Events",
    # Production Equipment
    "prod_vibration_condition": "Vibration Condition",
    "prod_motor_power_load": "Motor Electrical Load",
    "prod_process_temp_process": "Process Temperature Condition",
    "production_process_maintenance_overdue": "Maintenance Status",
    "production_process_repeated_events": "Repeated Abnormal Events",
    # Filling Machine
    "fill_power_load": "Electrical Load / Power Behaviour",
    "filling_maintenance_overdue": "Maintenance Status",
    "filling_repeated_events": "Repeated Abnormal Events",
}

FAMILY_LABELS: dict[str, str] = {
    "CONDITION": "Condition",
    "ELECTRICAL_LOAD": "Electrical Load",
    "PERFORMANCE": "Performance",
    "PROCESS": "Process",
    "EVENTS": "Events",
    "MAINTENANCE": "Maintenance",
}


def equipment_type_label(canonical: str | None) -> str:
    """Presentation-only - never renames the stored value. Unknown
    (future) types fall back to a cleaned version rather than failing."""
    if not canonical:
        return "-"
    return EQUIPMENT_TYPE_LABELS.get(canonical, canonical.replace("_", " ").title())


def factor_label(factor_id: str | None) -> str:
    """Presentation-only - the canonical factor_id itself remains fully
    visible in the technical-detail expander (item 6). Unknown (future)
    factors fall back to a cleaned version rather than failing (item 5)."""
    if not factor_id:
        return "-"
    return FACTOR_LABELS.get(factor_id, factor_id.replace("_", " ").title())


def family_label(family: str | None) -> str:
    if not family:
        return "-"
    return FAMILY_LABELS.get(family, family.replace("_", " ").title())


def format_change_delta(delta_entry: dict[str, Any]) -> str:
    """One human-readable line for a single factor's change (item 11) -
    grounded entirely in the already-computed delta, never a new
    calculation. 'Improved'/'worsened' is a wording choice only: a
    SMALLER penalty (negative delta) is an improvement in that factor's
    contribution to the score; a LARGER penalty (positive delta) is not."""
    verb = "improved" if delta_entry["delta"] < 0 else "worsened"
    magnitude = abs(delta_entry["delta"])
    return f"{factor_label(delta_entry['factor_id'])} {verb} by {magnitude:.1f} health point(s)."


def format_timestamp(value: str | None) -> str:
    """Consistent readable local timestamp (item 15) - e.g. '16 Aug 2026
    17:20:47'. Never alters the stored value, only how it's displayed;
    falls back to the raw string if it doesn't match the expected format."""
    if not value:
        return "-"
    try:
        return datetime.strptime(value[:19], TIME_FORMAT).strftime("%d %b %Y %H:%M:%S")
    except ValueError:
        return value


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def get_plants() -> list[dict[str, Any]]:
    connection = _connect()
    try:
        return [dict(r) for r in connection.execute("SELECT id, code, name FROM plants ORDER BY code")]
    finally:
        connection.close()


def _equipment_hierarchy(equipment_ids: list[int]) -> dict[int, dict[str, Any]]:
    """One bulk query for plant/area/system/display_name context for a
    batch of equipment ids - never one lookup per row."""
    if not equipment_ids:
        return {}
    connection = _connect()
    placeholders = ",".join("?" for _ in equipment_ids)
    try:
        rows = connection.execute(
            f"""
            SELECT e.id AS equipment_id, e.display_name, e.name, e.criticality,
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


def _trend_directions(snapshot_ids_by_instance: dict[str, int]) -> dict[str, str]:
    """One window-function query across ALL equipment's recent scored
    history, instead of engine.health_history.compute_trend() called
    once per equipment (which would be a real N+1 query pattern for an
    overview table)."""
    if not snapshot_ids_by_instance:
        return {}
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT instance_key, health_score, computed_at,
                   ROW_NUMBER() OVER (PARTITION BY instance_key ORDER BY computed_at DESC) AS rn
            FROM equipment_health_snapshots
            WHERE instance_key IN ({}) AND health_score IS NOT NULL
            """.format(",".join("?" for _ in snapshot_ids_by_instance)),
            list(snapshot_ids_by_instance),
        ).fetchall()
    finally:
        connection.close()

    latest: dict[str, float] = {}
    previous: dict[str, float] = {}
    for row in rows:
        if row["rn"] == 1:
            latest[row["instance_key"]] = row["health_score"]
        elif row["rn"] == 2:
            previous[row["instance_key"]] = row["health_score"]

    directions: dict[str, str] = {}
    for instance_key in snapshot_ids_by_instance:
        if instance_key not in latest or instance_key not in previous:
            directions[instance_key] = "INSUFFICIENT_HISTORY"
            continue
        change = latest[instance_key] - previous[instance_key]
        if abs(change) < SCORE_CHANGE_THRESHOLD:
            directions[instance_key] = "STABLE"
        elif change > 0:
            directions[instance_key] = "IMPROVING"
        else:
            directions[instance_key] = "DETERIORATING"
    return directions


def _dominant_factors(snapshot_ids: list[int]) -> dict[int, dict[str, Any]]:
    """One bulk query for the single highest-penalty 'penalized' factor
    per snapshot - never one query per equipment row."""
    if not snapshot_ids:
        return {}
    connection = _connect()
    placeholders = ",".join("?" for _ in snapshot_ids)
    try:
        rows = connection.execute(
            f"""
            SELECT * FROM equipment_health_factor_snapshots
            WHERE health_snapshot_id IN ({placeholders}) AND status = 'penalized'
            ORDER BY health_snapshot_id, penalty DESC
            """,
            snapshot_ids,
        ).fetchall()
    finally:
        connection.close()

    dominant: dict[int, dict[str, Any]] = {}
    for row in rows:
        sid = row["health_snapshot_id"]
        if sid not in dominant:  # first row per snapshot_id is the highest penalty, thanks to ORDER BY
            dominant[sid] = dict(row)
    return dominant


def get_overview(plant_code: str | None = None) -> list[dict[str, Any]]:
    """Latest persisted snapshot for every currently-eligible equipment
    instance, enriched with hierarchy/trend/dominant-factor context.
    Reads ONLY what the worker already persisted - no live recalculation."""
    plants = get_plants()
    if plant_code:
        plants = [p for p in plants if p["code"] == plant_code]

    snapshots: list[dict[str, Any]] = []
    for plant in plants:
        snapshots.extend(dom.list_latest_snapshots_for_plant(CONFIG_DATABASE_PATH, plant["id"]))

    if not snapshots:
        return []

    equipment_ids = [s["equipment_id"] for s in snapshots if s["equipment_id"] is not None]
    hierarchy = _equipment_hierarchy(equipment_ids)

    snapshot_ids_by_instance = {s["instance_key"]: s["id"] for s in snapshots}
    trends = _trend_directions(snapshot_ids_by_instance)
    dominant = _dominant_factors([s["id"] for s in snapshots])

    rows = []
    for s in snapshots:
        context = hierarchy.get(s["equipment_id"], {})
        top_factor = dominant.get(s["id"])
        rows.append({
            **s,
            "display_name": context.get("display_name") or s["instance_key"],
            "area_name": context.get("area_name"),
            "system_name": context.get("system_name"),
            "trend": trends.get(s["instance_key"], "INSUFFICIENT_HISTORY"),
            "dominant_factor_id": top_factor["factor_id"] if top_factor else None,
            "dominant_factor_penalty": top_factor["penalty"] if top_factor else None,
            "dominant_factor_reason": top_factor["reason"] if top_factor else None,
        })
    return rows


def get_filter_options(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Distinct values actually present in the CURRENT overview rows -
    never a hardcoded list, mirrors every prior page's filter-options convention."""
    return {
        "areas": sorted({r["area_name"] for r in rows if r["area_name"]}),
        "systems": sorted({r["system_name"] for r in rows if r["system_name"]}),
        "equipment_types": sorted({r["equipment_type"] for r in rows if r["equipment_type"]}),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure aggregation over already-fetched rows - no new query, no calculation."""
    total = len(rows)
    band_counts = {band: 0 for band in HEALTH_BAND_ORDER}
    confidence_counts = {c: 0 for c in CONFIDENCE_ORDER}
    scored_values = []

    for r in rows:
        if r["health_band"] in band_counts:
            band_counts[r["health_band"]] += 1
        if r["assessment_confidence"] in confidence_counts:
            confidence_counts[r["assessment_confidence"]] += 1
        if r["health_score"] is not None:
            scored_values.append(r["health_score"])

    insufficient = sum(1 for r in rows if r["health_score"] is None)
    average_score = round(sum(scored_values) / len(scored_values), 1) if scored_values else None

    return {
        "total": total,
        "healthy": band_counts["HEALTHY"],
        "monitor": band_counts["MONITOR"],
        "attention": band_counts["ATTENTION"],
        "investigate": band_counts["INVESTIGATE"],
        "insufficient": insufficient,
        "average_score": average_score,
        "average_score_count": len(scored_values),
        "confidence_counts": confidence_counts,
    }


def get_detail(plant_id: int, instance_key: str, history_days: float = 30, now: datetime | None = None) -> dict[str, Any] | None:
    """`now` defaults to real wall-clock time in production - exposed as
    an explicit parameter (matching ui/savings_verification_data.py's
    own get_stabilization_info(now=None) convention) so a fixed test
    anchor produces deterministic, reproducible history windows instead
    of silently depending on whatever moment the test happens to run."""
    now = now or datetime.now()
    latest = hist.get_latest(CONFIG_DATABASE_PATH, plant_id, instance_key)
    if latest is None:
        return None

    context = _equipment_hierarchy([latest["equipment_id"]]).get(latest["equipment_id"], {}) if latest["equipment_id"] else {}
    factors = hist.get_factor_detail(CONFIG_DATABASE_PATH, latest["id"])
    history = hist.get_history(CONFIG_DATABASE_PATH, plant_id, instance_key, days=history_days, now=now)
    trend = hist.compute_trend(CONFIG_DATABASE_PATH, plant_id, instance_key, now=now)
    band_transitions = hist.band_transitions(CONFIG_DATABASE_PATH, plant_id, instance_key, days=history_days, now=now)
    confidence_transitions = hist.confidence_transitions(CONFIG_DATABASE_PATH, plant_id, instance_key, days=history_days, now=now)

    change_explanation = _explain_latest_change(history, plant_id, instance_key)

    return {
        "latest": latest, "context": context, "factors": factors, "history": history,
        "trend": trend, "band_transitions": band_transitions, "confidence_transitions": confidence_transitions,
        "change_explanation": change_explanation,
    }


def _explain_latest_change(history: list[dict[str, Any]], plant_id: int, instance_key: str) -> dict[str, Any] | None:
    """Grounded, deterministic explanation of the most recent persisted
    change - diffs the two most recent snapshots' own stored factor
    rows. No LLM, no invented reasoning (item 5) - every line here is a
    direct read of already-persisted evidence."""
    if len(history) < 2:
        return None

    latest, previous = history[-1], history[-2]
    latest_factors = {f["factor_id"]: f for f in hist.get_factor_detail(CONFIG_DATABASE_PATH, latest["id"])}
    previous_factors = {f["factor_id"]: f for f in hist.get_factor_detail(CONFIG_DATABASE_PATH, previous["id"])}

    deltas = []
    for factor_id, current in latest_factors.items():
        prior = previous_factors.get(factor_id)
        prior_penalty = prior["penalty"] if prior else 0.0
        delta = round(current["penalty"] - prior_penalty, 2)
        if abs(delta) >= 0.05:
            deltas.append({
                "factor_id": factor_id, "family": current["factor_family"], "delta": delta,
                "from_status": prior["status"] if prior else "missing", "to_status": current["status"],
                "reason": current["reason"],
            })

    deltas.sort(key=lambda d: abs(d["delta"]), reverse=True)
    return {
        "from_computed_at": previous["computed_at"], "to_computed_at": latest["computed_at"],
        "from_score": previous["health_score"], "to_score": latest["health_score"],
        "factor_deltas": deltas,
    }


# ---------------------------------------------------------------------------
# Phase 12.4 - the ONE shared presentation contract (item 9). Every page
# that shows a health score/state/confidence - the main Equipment
# Health page included - calls these same three functions, so there is
# never more than one interpretation of what a given persisted record
# means on screen. Moved here from ui/pages/19_Equipment_Health.py
# (Phase 12.3/12.3A) with IDENTICAL logic - a relocation, not a
# behavior change.
# ---------------------------------------------------------------------------

def score_text(record: dict[str, Any] | None) -> str:
    if record is None or record.get("health_score") is None:
        return "Unavailable"
    return f"{record['health_score']:.1f}"


def state_text(record: dict[str, Any] | None) -> str:
    if record is None:
        return "Not Assessed"
    return record.get("health_band") or "Insufficient Data"


def confidence_text(record: dict[str, Any] | None) -> str:
    if record is None:
        return "-"
    confidence = record.get("assessment_confidence")
    if not confidence:
        return "-"
    return f"{confidence} · Provisional" if record.get("provisional") else confidence


def state_badge_html(state: str) -> str:
    """Compact colored chip - the SAME band-color mapping as the
    overview table's row tinting, reused by every integration point
    (SCADA/Service & Maintenance/Equipment Config/Home) so a given
    health state always looks the same color everywhere."""
    color = BAND_BADGE_COLORS.get(state, "#e6e6e6")
    return f"<span style='background-color:{color}; color:#1a1a1a; padding:2px 10px; border-radius:4px; font-weight:600;'>{state}</span>"


# ---------------------------------------------------------------------------
# Phase 12.4 - cross-page read helpers, all keyed by the stable
# equipment.id (never display-name string parsing - item 5). Every one
# reads ONLY already-persisted equipment_health_snapshots rows - none
# of them import engine.health_engine or recalculate anything.
# ---------------------------------------------------------------------------

def get_latest_health_by_equipment_id(equipment_id: int | None) -> dict[str, Any] | None:
    """Single-equipment lookup - the integration point for a page that
    already has exactly ONE selected equipment (Service & Maintenance,
    Equipment & Tag Configuration). Returns None when no snapshot has
    ever been persisted for this equipment ("Not Assessed"), distinct
    from a real row with health_score=None ("Insufficient Data")."""
    if equipment_id is None:
        return None
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM equipment_health_snapshots WHERE equipment_id = ? ORDER BY computed_at DESC, id DESC LIMIT 1",
            (equipment_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def get_latest_health_by_equipment_ids(equipment_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Bulk lookup for MANY equipment at once, in exactly one query -
    the integration point for a page rendering a whole list/grid
    (SCADA Floor Plan). Never call get_latest_health_by_equipment_id()
    in a loop (item 10's N+1 guard)."""
    ids = [e for e in equipment_ids if e is not None]
    if not ids:
        return {}
    connection = _connect()
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = connection.execute(
            f"""
            SELECT * FROM equipment_health_snapshots WHERE id IN (
                SELECT MAX(id) FROM equipment_health_snapshots
                WHERE equipment_id IN ({placeholders}) GROUP BY equipment_id
            )
            """,
            ids,
        ).fetchall()
    finally:
        connection.close()
    return {r["equipment_id"]: dict(r) for r in rows}


def get_health_summary_counts() -> dict[str, Any]:
    """Lightweight factory-wide counts ONLY - the Home page's
    integration point. Deliberately skips the hierarchy/trend/dominant-
    factor enrichment get_overview() does (Home doesn't need equipment
    names/areas), so this is one small query, not the ~3.4s full
    overview cost. Reuses summarize() - the SAME aggregation Home and
    the Equipment Health overview both rely on, never a second
    definition of what counts as "Attention" etc."""
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT health_score, health_band, assessment_confidence FROM equipment_health_snapshots "
            "WHERE id IN (SELECT MAX(id) FROM equipment_health_snapshots GROUP BY plant_id, instance_key)"
        ).fetchall()
    finally:
        connection.close()
    return summarize([dict(r) for r in rows])


def compact_health_context(equipment_id: int | None) -> dict[str, Any]:
    """Pre-formatted, ready-to-render strings for a ONE-equipment
    compact health block (Service & Maintenance / Equipment & Tag
    Configuration / SCADA Floor Plan detail panel) - built from the
    same score_text()/state_text()/confidence_text() every other page
    uses, so there is exactly one interpretation of a given record."""
    record = get_latest_health_by_equipment_id(equipment_id)
    return {
        "assessed": record is not None,
        "score": score_text(record),
        "state": state_text(record),
        "confidence": confidence_text(record),
        "last_assessed": format_timestamp(record["computed_at"]) if record else "-",
        "record": record,
    }
