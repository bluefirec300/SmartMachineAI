from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

import streamlit as st

from database.database import DatabaseManager
from engine import energy_kpi_engine as k
from simulator import plant_context
from ui.data_access import CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH

"""
Phase 7 - Energy Dashboard's data/caching layer. Mirrors the exact
architectural role ui/scada_floor_plan_data.py and ui/production_data.py
already play: a thin bridge between Streamlit and the deterministic
engine, adding caching but NEVER arithmetic. Every number this module
returns comes from calling engine.energy_kpi_engine - confirmed by
tests/test_energy_dashboard_data.py's source-inspection test, the same
technique tests/test_production_batch_simulator.py already uses to
prove simulator/production_batch_simulator.py never touches plc_data.

Caching strategy (tiered by how "live" a value is, not one blanket
TTL - a period ending at "now" needs to stay fresh; a period that has
already fully elapsed can never change again):
  - Current Demand / instantaneous readings (chiller/pump snapshots,
    equipment power breakdown): CURRENT_TTL (~12s) - Power_kW logs
    every 10s, so this rarely serves anything staler than one sample.
  - Anything ending at "now" (Today, month-to-date, live period KPIs):
    TODAY_TTL (30s) - a deliberate balance per your direction: fresh
    enough to feel current without re-querying the historian on every
    widget interaction/rerun.
  - A period that is entirely in the past (Yesterday, a completed
    Custom range): HISTORICAL_TTL (1 hour) - this data cannot change.
  - 7-day/30-day trend series read engine_kpi_daily_summary directly
    (a handful of indexed row lookups) instead of recomputing per day.
"""

CURRENT_TTL = 12
TODAY_TTL = 30
HISTORICAL_TTL = 3600

AC_INSTANCES = ("AC01", "AC02", "AC03")
CHL_INSTANCES = ("CHL01", "CHL02")
CHWP_INSTANCES = ("CHWP01", "CHWP02", "CHWP03")
WSP_INSTANCES = ("WSP01", "WSP02")

EQUIPMENT_LABELS: dict[str, str] = {
    "UTILITY.AC": "Air Compressor",
    "UTILITY.CHL": "Chiller",
    "UTILITY.CHWP": "Chilled Water Pump",
    "WATER.WSP": "Water Supply Pump",
    "COLDROOM.CR": "Cold Room",
    "HVAC.AHU": "AHU",
    "DUST.DC": "Dust Collector",
    "WT.RO": "RO/DI System",
    "WW.SYS": "Wastewater Treatment",
    "PROD.DISP": "High-Speed Disperser",
    "PROD.MILL": "Bead Mill",
    "PROD.MIX": "Mixer",
    "FILL.FILL": "Filling Machine",
    "LABORATORYQC.ENV": "Laboratory/QC",
    "WAREHOUSE.ENV": "Warehouse",
}


def _historian() -> DatabaseManager:
    return DatabaseManager(db_path=MACHINE_DATABASE_PATH)


def _fmt(dt: datetime) -> str:
    return dt.strftime(k.TIME_FORMAT)


def is_live_period(end: datetime, now: datetime | None = None) -> bool:
    """A period is 'live' if its end is at (or essentially at) now -
    anything else is a fully-elapsed, immutable historical window."""
    now = now or datetime.now()
    return end >= now - timedelta(minutes=1)


@st.cache_data(ttl=HISTORICAL_TTL)
def list_plants() -> list[dict[str, Any]]:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute("SELECT id, code, name FROM plants ORDER BY code")]
    finally:
        connection.close()


@st.cache_data(ttl=CURRENT_TTL)
def get_current_demand(plant_code: str) -> dict[str, Any]:
    return k.current_demand_kw(_historian(), plant_code)


@st.cache_data(ttl=60)
def get_active_tariff_provenance(plant_code: str) -> dict[str, Any] | None:
    """For the page-top simulation/tariff caption - not a full tariff
    object, just enough to know whether to show 'Simulated tariff'."""
    plant = k.get_plant(CONFIG_DATABASE_PATH, plant_code)
    plant_id = plant["id"] if plant else None
    tariff = k.effective_tariff(CONFIG_DATABASE_PATH, plant_id, datetime.now())
    if tariff is None:
        return None
    return {"is_simulated": bool(tariff.get("is_simulated")), "currency": tariff.get("currency"), "energy_rate": tariff.get("energy_rate")}


# ---------------------------------------------------------------------------
# Main dashboard - selected-period KPI bundle
# ---------------------------------------------------------------------------

def _compute_period_kpis(plant_code: str, start_iso: str, end_iso: str) -> dict[str, Any]:
    start, end = datetime.strptime(start_iso, k.TIME_FORMAT), datetime.strptime(end_iso, k.TIME_FORMAT)
    historian = _historian()
    plant = k.get_plant(CONFIG_DATABASE_PATH, plant_code)
    plant_id = plant["id"] if plant else None

    result: dict[str, Any] = {
        "energy": k.energy_for_period(historian, plant_code, start, end),
        "cost": k.cost_for_period(CONFIG_DATABASE_PATH, historian, plant_id, plant_code, start, end),
        "estimated_maximum_demand": k.estimated_maximum_demand(historian, plant_code, start, end),
        "estimated_demand_charge": k.estimated_demand_charge(CONFIG_DATABASE_PATH, historian, plant_id, plant_code, start, end),
        "estimated_base_load": k.estimated_base_load(historian, CONFIG_DATABASE_PATH, plant_code, plant_id, start, end),
        "after_hours": k.after_hours_consumption(CONFIG_DATABASE_PATH, historian, plant_code, plant_id, start, end),
    }
    result.update(k.production_non_production_split(historian, CONFIG_DATABASE_PATH, plant_code, plant_id, start, end))
    result["production_intensity"] = k.production_energy_intensity(CONFIG_DATABASE_PATH, historian, plant_code, plant_id, start, end)
    return result


@st.cache_data(ttl=TODAY_TTL)
def _get_live_period_kpis(plant_code: str, start_iso: str, end_iso: str) -> dict[str, Any]:
    return _compute_period_kpis(plant_code, start_iso, end_iso)


@st.cache_data(ttl=HISTORICAL_TTL)
def _get_historical_period_kpis(plant_code: str, start_iso: str, end_iso: str) -> dict[str, Any]:
    return _compute_period_kpis(plant_code, start_iso, end_iso)


def get_period_kpis(plant_code: str, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, Any]:
    """Dispatches to the short-TTL or long-TTL cached wrapper depending
    on whether `end` is "now" or a fully-elapsed past moment."""
    if is_live_period(end, now):
        return _get_live_period_kpis(plant_code, _fmt(start), _fmt(end))
    return _get_historical_period_kpis(plant_code, _fmt(start), _fmt(end))


@st.cache_data(ttl=TODAY_TTL)
def get_month_to_date_kpis(plant_code: str) -> dict[str, Any]:
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    historian = _historian()
    plant = k.get_plant(CONFIG_DATABASE_PATH, plant_code)
    plant_id = plant["id"] if plant else None
    return {
        "energy_month": k.energy_for_period(historian, plant_code, month_start, now),
        "cost_month": k.cost_for_period(CONFIG_DATABASE_PATH, historian, plant_id, plant_code, month_start, now),
        "projected_month_cost": k.projected_month_cost(CONFIG_DATABASE_PATH, historian, plant_id, plant_code, now),
    }


@st.cache_data(ttl=TODAY_TTL)
def get_billing_month_peak(plant_code: str, interval_minutes: int = k.DEFAULT_DEMAND_INTERVAL_MINUTES) -> dict[str, Any] | None:
    """Reads the PERSISTED energy_kpi_maximum_demand record directly -
    a distinct concept from get_period_kpis()'s live-computed Estimated
    Maximum Demand for whatever period the user has selected. None if
    energy_kpi_worker.service hasn't recorded a peak yet this billing
    period (not started, or hasn't run long enough)."""
    plant = k.get_plant(CONFIG_DATABASE_PATH, plant_code)
    if plant is None:
        return None

    now = datetime.now()
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM energy_kpi_maximum_demand WHERE plant_id = ? AND billing_period_start = ? AND demand_interval_minutes = ?",
            (plant["id"], period_start.strftime(k.TIME_FORMAT), interval_minutes),
        ).fetchone()
    finally:
        connection.close()

    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Compressed Air
# ---------------------------------------------------------------------------

def _compute_compressed_air_kpis(plant_code: str, start_iso: str, end_iso: str) -> dict[str, Any]:
    start, end = datetime.strptime(start_iso, k.TIME_FORMAT), datetime.strptime(end_iso, k.TIME_FORMAT)
    historian = _historian()
    plant = k.get_plant(CONFIG_DATABASE_PATH, plant_code)
    plant_id = plant["id"] if plant else None

    result: dict[str, Any] = {
        "total_compressor_energy": k.total_compressor_energy(historian, plant_code, list(AC_INSTANCES), start, end),
        "header_air_volume": k.header_air_volume(historian, plant_code, start, end),
        "header_specific_energy": k.header_specific_energy(historian, plant_code, list(AC_INSTANCES), start, end),
        "cost_per_nm3": k.compressed_air_cost_per_nm3(CONFIG_DATABASE_PATH, historian, plant_id, plant_code, list(AC_INSTANCES), start, end),
        "night_base_air_consumption": k.night_base_air_consumption(historian, CONFIG_DATABASE_PATH, plant_code, plant_id, start, end),
        "per_compressor": {
            instance: k.compressor_load_stats(historian, plant_code, instance, start, end)
            for instance in AC_INSTANCES
        },
    }
    return result


@st.cache_data(ttl=TODAY_TTL)
def _get_live_compressed_air_kpis(plant_code: str, start_iso: str, end_iso: str) -> dict[str, Any]:
    return _compute_compressed_air_kpis(plant_code, start_iso, end_iso)


@st.cache_data(ttl=HISTORICAL_TTL)
def _get_historical_compressed_air_kpis(plant_code: str, start_iso: str, end_iso: str) -> dict[str, Any]:
    return _compute_compressed_air_kpis(plant_code, start_iso, end_iso)


def get_compressed_air_kpis(plant_code: str, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, Any]:
    if is_live_period(end, now):
        return _get_live_compressed_air_kpis(plant_code, _fmt(start), _fmt(end))
    return _get_historical_compressed_air_kpis(plant_code, _fmt(start), _fmt(end))


# ---------------------------------------------------------------------------
# Chilled Water - chiller_cop() is a latest-value snapshot, not period-
# dependent, so it only needs the short "current" TTL.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=CURRENT_TTL)
def get_chilled_water_kpis(plant_code: str) -> dict[str, dict[str, Any]]:
    historian = _historian()
    return {instance: k.chiller_cop(historian, plant_code, instance) for instance in CHL_INSTANCES}


# ---------------------------------------------------------------------------
# Pumps - hydraulic efficiency/flow-per-kW are latest-value snapshots
# (short TTL); specific energy (kWh/m3) is period-integrated (tiered
# TTL like the main dashboard).
# ---------------------------------------------------------------------------

@st.cache_data(ttl=CURRENT_TTL)
def get_pump_snapshot_kpis(plant_code: str) -> dict[str, dict[str, Any]]:
    historian = _historian()
    result = {}
    for area, instances in (("UTILITY", CHWP_INSTANCES), ("WATER", WSP_INSTANCES)):
        for instance in instances:
            result[instance] = k.pump_hydraulic_efficiency(historian, plant_code, area, instance)
            result[instance]["flow_per_kw"] = k.pump_flow_per_kw(historian, plant_code, area, instance)
    return result


def _compute_pump_specific_energy(plant_code: str, start_iso: str, end_iso: str) -> dict[str, dict[str, Any]]:
    start, end = datetime.strptime(start_iso, k.TIME_FORMAT), datetime.strptime(end_iso, k.TIME_FORMAT)
    historian = _historian()
    result = {}
    for area, instances in (("UTILITY", CHWP_INSTANCES), ("WATER", WSP_INSTANCES)):
        for instance in instances:
            result[instance] = k.pump_specific_energy(historian, plant_code, area, instance, start, end)
    return result


@st.cache_data(ttl=TODAY_TTL)
def _get_live_pump_specific_energy(plant_code: str, start_iso: str, end_iso: str) -> dict[str, dict[str, Any]]:
    return _compute_pump_specific_energy(plant_code, start_iso, end_iso)


@st.cache_data(ttl=HISTORICAL_TTL)
def _get_historical_pump_specific_energy(plant_code: str, start_iso: str, end_iso: str) -> dict[str, dict[str, Any]]:
    return _compute_pump_specific_energy(plant_code, start_iso, end_iso)


def get_pump_specific_energy(plant_code: str, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    if is_live_period(end, now):
        return _get_live_pump_specific_energy(plant_code, _fmt(start), _fmt(end))
    return _get_historical_pump_specific_energy(plant_code, _fmt(start), _fmt(end))


# ---------------------------------------------------------------------------
# Water treatment
# ---------------------------------------------------------------------------

def _compute_water_treatment_kpis(plant_code: str, start_iso: str, end_iso: str) -> dict[str, dict[str, Any]]:
    start, end = datetime.strptime(start_iso, k.TIME_FORMAT), datetime.strptime(end_iso, k.TIME_FORMAT)
    historian = _historian()
    return {
        "RO01": k.water_treatment_specific_energy(historian, plant_code, "WT", "RO01", "Power_kW", "PermeateFlow", start, end),
        "SYS01": k.water_treatment_specific_energy(historian, plant_code, "WT", "SYS01", None, "TreatedWaterFlow", start, end),
    }


@st.cache_data(ttl=TODAY_TTL)
def _get_live_water_treatment_kpis(plant_code: str, start_iso: str, end_iso: str) -> dict[str, dict[str, Any]]:
    return _compute_water_treatment_kpis(plant_code, start_iso, end_iso)


@st.cache_data(ttl=HISTORICAL_TTL)
def _get_historical_water_treatment_kpis(plant_code: str, start_iso: str, end_iso: str) -> dict[str, dict[str, Any]]:
    return _compute_water_treatment_kpis(plant_code, start_iso, end_iso)


def get_water_treatment_kpis(plant_code: str, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    if is_live_period(end, now):
        return _get_live_water_treatment_kpis(plant_code, _fmt(start), _fmt(end))
    return _get_historical_water_treatment_kpis(plant_code, _fmt(start), _fmt(end))


# ---------------------------------------------------------------------------
# 7/30-day trend series - reads energy_kpi_daily_summary directly
# (Phase 6's persisted rollup), never recomputes per day from raw
# historian data.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def get_daily_summary_series(plant_code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """start_date/end_date: 'YYYY-MM-DD' strings, inclusive. Returns
    whatever rows exist - a gap (a day the worker hasn't finalized yet)
    is simply absent, never fabricated."""
    plant = k.get_plant(CONFIG_DATABASE_PATH, plant_code)
    if plant is None:
        return []

    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM energy_kpi_daily_summary WHERE plant_id = ? AND summary_date BETWEEN ? AND ? ORDER BY summary_date",
            (plant["id"], start_date, end_date),
        ).fetchall()
    finally:
        connection.close()

    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Equipment power breakdown - a current snapshot (Power Share, NOT
# Energy Share - see module docstring / Phase 7 approval item 5). Reuses
# Phase 5's existing Main Incomer aggregation boundary; computes no new
# aggregation of its own.
# ---------------------------------------------------------------------------

def _equipment_label(tag_name: str) -> str:
    canonical = plant_context.canonical_key(tag_name)
    instance_type = canonical.rsplit(".", 1)[0]
    prefix_label = EQUIPMENT_LABELS.get(instance_type, instance_type)
    instance = plant_context.instance_key(tag_name).split(".")[-1]
    return f"{prefix_label} {instance}"


@st.cache_data(ttl=CURRENT_TTL)
def get_equipment_power_breakdown(plant_code: str) -> list[dict[str, Any]]:
    """Current Power_kW snapshot per Main-Incomer-contributing tag,
    with each tag's % share of the plant's current total - a "Power
    Share" (instantaneous), never described as an accumulated energy
    contribution."""
    historian = _historian()

    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    try:
        all_tags = [r[0] for r in connection.execute("SELECT tag_name FROM tags WHERE tag_name LIKE ? AND enabled = 1", (f"{plant_code.upper()}.%",))]
    finally:
        connection.close()

    contributing = plant_context.main_incomer_contributing_tags(all_tags, plant_code)

    rows = []
    total = 0.0
    for tag in contributing:
        value, timestamp = k.latest_with_timestamp(historian, tag)
        if value is None or k.is_stale(timestamp):
            continue
        rows.append({"tag": tag, "label": _equipment_label(tag), "power_kw": value})
        total += value

    for row in rows:
        row["power_share_pct"] = round(row["power_kw"] / total * 100, 1) if total > k.ZERO_FLOW_EPSILON else None

    rows.sort(key=lambda r: r["power_kw"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Raw trend data for charts - NOT a KPI (no formula, no classification
# needed): plain historian samples for display, same as
# ui/pages/2_Live_Data.py already reads directly. Trend charts for
# completed 7/30-day ranges use get_daily_summary_series() above
# instead of this.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=TODAY_TTL)
def get_power_trend(plant_code: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    historian = _historian()
    tag = f"{plant_code.upper()}.ELEC.MAIN.Power_kW"
    span_seconds = (datetime.strptime(end_iso, k.TIME_FORMAT) - datetime.strptime(start_iso, k.TIME_FORMAT)).total_seconds()
    limit = max(100, int(span_seconds / 10 * 1.5) + 100)  # ~10s Power_kW cadence, generous headroom
    rows = historian.get_history_range(tag=tag, start=start_iso, end=end_iso, limit=limit)
    return [{"time": r["time"], "power_kw": r["value"]} for r in rows]
