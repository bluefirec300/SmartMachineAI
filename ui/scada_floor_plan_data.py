from __future__ import annotations

import re
import sqlite3
from datetime import datetime

import streamlit as st

from ai.rule_engine import RuleEngine
from database.database import DatabaseManager
from ui.data_access import CONFIG_DATABASE_PATH


SEVERITY_COLOR = {
    "alarm": "#e5484d",
    "warning": "#f5a524",
    "normal": "#3dd68c",
    "not_evaluated": "#8a8f98",
    "no_data": "#4a4f58",
}
# "normal" must outrank "not_evaluated": _equipment_status()'s max()
# picks the first tag on a tie, and an equipment's alphabetically-first
# tag is often a STRING AlarmCode (always "not_evaluated" absent a
# fault) - if it tied with a real, in-range measurement tag, the
# AlarmCode would win the tie and the equipment would wrongly show
# "no threshold configured" despite having real configured limits.
SEVERITY_RANK = {"alarm": 4, "warning": 3, "normal": 2, "not_evaluated": 1, "no_data": 0}
SEVERITY_LABEL = {
    "alarm": "Alarm",
    "warning": "Warning",
    "normal": "Normal",
    "not_evaluated": "No threshold configured",
    "no_data": "No data yet",
}
SEVERITY_DOT = {"alarm": "\U0001F534", "warning": "\U0001F7E0", "normal": "\U0001F7E2", "not_evaluated": "⚪", "no_data": "⚫"}
SEVERITY_TEXT = {
    "alarm": "#ffffff",
    "warning": "#1a1206",
    "normal": "#062017",
    "not_evaluated": "#14181f",
    "no_data": "#e6edf3",
}

CATEGORY_ICON = {
    "air_compressor": "\U0001F4A8",
    "compressed_air_header": "\U0001F32C️",
    "chiller": "❄️",
    "chilled_water_pump": "\U0001F4A7",
    "water_supply_pump": "\U0001F6B0",
    "water_supply": "\U0001F6B0",
    "cold_room": "\U0001F9CA",
    "bead_mill": "⚙️",
    "high_speed_disperser": "\U0001F300",
    "mixer": "\U0001F504",
    "filling_machine": "\U0001F9F4",
    "dust_collector": "\U0001F32A️",
    "tank": "\U0001F6E2️",
    "solvent_transfer": "\U0001F9EA",
    "water_treatment_system": "\U0001F6BF",
    "ro_di_system": "\U0001F4A7",
    "effluent_treatment": "♻️",
    "main_incomer": "⚡",
    "building_incomer": "⚡",
    "transformer": "\U0001F50C",
    "ups": "\U0001F50B",
    "generator": "⛽",
    "ahu": "\U0001F32C️",
    "mcc_room": "\U0001F5A5️",
    "area_monitoring": "\U0001F321️",
    "weather_node": "☁️",
    "building_water_meter": "\U0001F4A6",
    "fire_water_system": "\U0001F9EF",
}
DEFAULT_ICON = "\U0001F3ED"

# Which equipment "category" (the p01_<category>_<instance> naming
# app/ask.py's _equipment_category_key() also relies on) belongs in
# which imagined room. Anything unmatched falls into an auto-added
# "Other" zone via group_into_zones(), so a newly-enabled equipment
# type never disappears from the map, it just starts out ungrouped
# until this dict is taught about it.
ZONES = [
    {
        "name": "Utilities Yard",
        "categories": {
            "air_compressor", "compressed_air_header", "chiller",
            "chilled_water_pump", "water_supply_pump", "water_supply",
        },
    },
    {
        "name": "Electrical Room",
        "categories": {
            "main_incomer", "building_incomer", "transformer", "ups", "generator",
        },
    },
    {"name": "Cold Storage", "categories": {"cold_room"}},
    {
        "name": "Production Floor",
        "categories": {
            "bead_mill", "high_speed_disperser", "mixer",
            "filling_machine", "dust_collector",
        },
    },
    {"name": "Tank Farm", "categories": {"tank", "solvent_transfer"}},
    {
        "name": "Water Treatment Plant",
        "categories": {"water_treatment_system", "ro_di_system", "effluent_treatment"},
    },
    {
        "name": "HVAC & Monitoring",
        "categories": {
            "ahu", "mcc_room", "area_monitoring", "weather_node", "building_water_meter",
        },
    },
    {"name": "Fire Safety", "categories": {"fire_water_system"}},
]


def _category_key(equipment_name: str) -> str:
    """p01_cold_room_cr01 -> cold_room (same convention app/ask.py's
    _equipment_category_key() uses)."""
    match = re.match(r"^p\d+_(.+)_[a-z0-9]+$", equipment_name)
    return match.group(1) if match else equipment_name


def _short_code(equipment_name: str) -> str:
    """p01_air_compressor_ac01 -> AC01, p01_main_incomer_main -> MAIN."""
    return equipment_name.rsplit("_", 1)[-1].upper()


@st.cache_data(ttl=5)
def _load_equipment(plant: str) -> list[dict]:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        equipment_rows = connection.execute(
            "SELECT id, name, display_name FROM equipment WHERE name LIKE ? ORDER BY display_name",
            (f"{plant}_%",),
        ).fetchall()

        equipment = []

        for row in equipment_rows:
            tag_rows = connection.execute(
                "SELECT tag_name, unit, data_type FROM tags "
                "WHERE equipment_id = ? AND enabled = 1 ORDER BY tag_name",
                (row["id"],),
            ).fetchall()

            if not tag_rows:
                continue

            category = _category_key(row["name"])
            equipment.append(
                {
                    "name": row["name"],
                    "display_name": row["display_name"],
                    "category": category,
                    "icon": CATEGORY_ICON.get(category, DEFAULT_ICON),
                    "code": _short_code(row["name"]),
                    "tags": [dict(t) for t in tag_rows],
                }
            )
    finally:
        connection.close()

    return equipment


def _tag_severity(tag_name: str, data_type: str, latest: dict, latest_text: dict, rule_engine: RuleEngine) -> str:
    if data_type == "STRING":
        text_reading = latest_text.get(tag_name)
        if text_reading is None:
            return "no_data"
        if tag_name.endswith(".AlarmCode") and text_reading["value"] not in (None, "None"):
            return "alarm"
        return "not_evaluated"

    reading = latest.get(tag_name)
    if reading is None:
        return "no_data"

    result = rule_engine.evaluate_summary({"tag": tag_name, "current": reading["value"]})

    if result["condition"] == "not_evaluated":
        return "not_evaluated"

    return result["severity"]


def _equipment_status(equipment: dict, latest: dict, latest_text: dict, rule_engine: RuleEngine) -> dict:
    per_tag = [
        {
            **tag,
            "severity": _tag_severity(tag["tag_name"], tag["data_type"], latest, latest_text, rule_engine),
            "value": (latest_text.get(tag["tag_name"], {}).get("value") if tag["data_type"] == "STRING" else (latest.get(tag["tag_name"], {}) or {}).get("value")),
            "updated": (latest_text.get(tag["tag_name"], {}).get("time") if tag["data_type"] == "STRING" else (latest.get(tag["tag_name"], {}) or {}).get("time")),
        }
        for tag in equipment["tags"]
    ]

    worst = max(per_tag, key=lambda t: SEVERITY_RANK[t["severity"]])["severity"] if per_tag else "no_data"

    return {**equipment, "tags": per_tag, "overall_severity": worst}


def group_into_zones(equipment_list: list[dict]) -> list[tuple[str, list[dict]]]:
    """
    Groups equipment (by their 'category' field) into the imagined
    room layout, sorted by display_name within each zone. Equipment
    whose category doesn't match any ZONES entry lands in one "Other"
    bucket at the end rather than disappearing. Works on either plain
    _load_equipment() output or _equipment_status()-evaluated output -
    only 'category'/'name'/'display_name' are used.
    """
    by_category: dict[str, list[dict]] = {}
    for eq in equipment_list:
        by_category.setdefault(eq["category"], []).append(eq)

    placed_names = set()
    zone_groups = []
    for zone in ZONES:
        zone_equipment = []
        for category in sorted(zone["categories"]):
            for eq in by_category.get(category, []):
                zone_equipment.append(eq)
                placed_names.add(eq["name"])
        if zone_equipment:
            zone_groups.append((zone["name"], sorted(zone_equipment, key=lambda e: e["display_name"])))

    leftover = [eq for eq in equipment_list if eq["name"] not in placed_names]
    if leftover:
        zone_groups.append(("Other", sorted(leftover, key=lambda e: e["display_name"])))

    return zone_groups


def _todays_energy_kwh(database: DatabaseManager, tag_name: str) -> float | None:
    """
    Energy_kWh is a monotonic running total (never resets), so "today's
    consumption" is the delta between its value at midnight and now.
    None if nothing's logged yet today, rather than a misleading 0.
    """
    now = datetime.now()
    hours_since_midnight = max(
        0.05, (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600
    )

    try:
        rows = database.get_history(tag=tag_name, hours=hours_since_midnight, limit=3000)
    except Exception:
        return None

    if len(rows) < 2:
        return None

    return rows[-1]["value"] - rows[0]["value"]


def build_equipment_snapshot(
    plant: str,
    database: DatabaseManager,
    rule_engine: RuleEngine,
    latest: dict | None = None,
    latest_text: dict | None = None,
) -> dict[str, dict]:
    """
    latest/latest_text are optional pre-fetched results of
    database.get_latest_all()/get_latest_text_all(). Neither query is
    filtered by plant (they return every tag's latest value across the
    whole system), so a caller building snapshots for multiple plants/
    both builder functions in the same tick can fetch them once and pass
    them in here to avoid redundant full-table scans. When omitted
    (the default), fetched internally exactly as before.
    """
    equipment_list = _load_equipment(plant)
    if latest is None:
        latest = database.get_latest_all()
    if latest_text is None:
        latest_text = database.get_latest_text_all()

    snapshot: dict[str, dict] = {}
    for equipment in equipment_list:
        evaluated = _equipment_status(equipment, latest, latest_text, rule_engine)
        snapshot[evaluated["name"]] = {
            "display_name": evaluated["display_name"],
            "code": evaluated["code"],
            "icon": evaluated["icon"],
            "category": evaluated["category"],
            "overall_severity": evaluated["overall_severity"],
            "tags": [
                {
                    "tag_name": tag["tag_name"],
                    "unit": tag.get("unit") or "",
                    "value": tag["value"],
                    "severity": tag["severity"],
                    "updated": tag["updated"],
                }
                for tag in evaluated["tags"]
            ],
        }
    return snapshot


def build_board_snapshot(
    plant: str,
    database: DatabaseManager,
    rule_engine: RuleEngine,
    latest: dict | None = None,
    latest_text: dict | None = None,
) -> dict:
    """
    latest/latest_text: see build_equipment_snapshot()'s docstring -
    same optional pre-fetched-results parameters, same reasoning.
    """
    equipment_list = _load_equipment(plant)
    if latest is None:
        latest = database.get_latest_all()
    if latest_text is None:
        latest_text = database.get_latest_text_all()
    evaluated = [_equipment_status(eq, latest, latest_text, rule_engine) for eq in equipment_list]

    incomer_prefix = f"{plant.upper()}.ELEC.MAIN"
    today_kwh = _todays_energy_kwh(database, f"{incomer_prefix}.Energy_kWh")
    power = latest.get(f"{incomer_prefix}.Power_kW")
    pf = latest.get(f"{incomer_prefix}.PF")
    freq = latest.get(f"{incomer_prefix}.Frequency")
    currents = [latest.get(f"{incomer_prefix}.Current_L{p}") for p in (1, 2, 3)]
    voltages = [
        latest.get(f"{incomer_prefix}.Voltage_L1L2"),
        latest.get(f"{incomer_prefix}.Voltage_L2L3"),
        latest.get(f"{incomer_prefix}.Voltage_L3L1"),
    ]

    air_pressure = latest.get(f"{plant.upper()}.UTILITY.AIRHDR01.Pressure")
    air_flow = latest.get(f"{plant.upper()}.UTILITY.AIRHDR01.Flow")
    water_pressure = latest.get(f"{plant.upper()}.WATER.SYS01.HeaderPressure")
    water_level = latest.get(f"{plant.upper()}.WATER.SYS01.TankLevel")

    alarm_count = sum(1 for e in evaluated if e["overall_severity"] == "alarm")
    warning_count = sum(1 for e in evaluated if e["overall_severity"] == "warning")
    normal_count = sum(1 for e in evaluated if e["overall_severity"] == "normal")
    not_evaluated_count = sum(1 for e in evaluated if e["overall_severity"] == "not_evaluated")
    no_data_count = sum(1 for e in evaluated if e["overall_severity"] == "no_data")

    return {
        "today_kwh": today_kwh,
        "power_kw": power["value"] if power else None,
        "pf": pf["value"] if pf else None,
        "frequency": freq["value"] if freq else None,
        "currents": [c["value"] if c else None for c in currents],
        "voltages": [v["value"] if v else None for v in voltages],
        "air_pressure": air_pressure["value"] if air_pressure else None,
        "air_flow": air_flow["value"] if air_flow else None,
        "water_pressure": water_pressure["value"] if water_pressure else None,
        "water_level": water_level["value"] if water_level else None,
        "alarm_count": alarm_count,
        "warning_count": warning_count,
        "normal_count": normal_count,
        "not_evaluated_count": not_evaluated_count,
        "no_data_count": no_data_count,
        "total": len(evaluated),
    }
