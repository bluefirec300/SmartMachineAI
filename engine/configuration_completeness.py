from __future__ import annotations

import sqlite3
from pathlib import Path

"""
Weighted "how complete is our configuration" scoring - deterministic,
no LLM involved (per the roadmap's "LLM is not the calculation
engine" principle). Built to be reusable by later phases (Data
Health, AI Analysis Confidence), so every function here returns
plain structured data, never HTML/Streamlit widgets - rendering is
entirely the caller's job.

A field contributes its configured WEIGHT to the total only when it
is genuinely filled in (not None/empty string). "Weight" is not a
field count - it reflects how engineering-significant that field is,
per the explicit design direction that important engineering
information should count for more than administrative bookkeeping
fields, and that different equipment types care about different
fields (an air compressor's rated pressure matters far more than a
weather station's).
"""


# Currency/timezone weighted above the purely administrative fields
# since Phase 3 (tariff/cost) and any schedule-based analytics will
# depend on them being correct - not just nice-to-have bookkeeping.
FACTORY_FIELD_WEIGHTS: dict[str, int] = {
    "name": 1,
    "company": 1,
    "country": 1,
    "currency": 2,
    "timezone": 2,
    "factory_type": 1,
    "floor_area_m2": 1,
}

# "code" is always populated by the Phase 1 migration and "active"
# always has a DEFAULT of 1 - neither can ever genuinely be "missing",
# so neither is included here.
PLANT_FIELD_WEIGHTS: dict[str, int] = {
    "name": 1,
    "description": 1,
    "floor_area_m2": 1,
    "production_capacity": 2,
}

# Baseline weights for every equipment type - EQUIPMENT_TYPE_WEIGHT_OVERRIDES
# below layers equipment-type-specific adjustments on top (both boosting
# genuinely important fields for that type, and zeroing out fields that
# don't physically apply, e.g. a weather station has no rated_power).
DEFAULT_EQUIPMENT_FIELD_WEIGHTS: dict[str, int] = {
    "equipment_type": 1,
    "brand": 1,
    "model": 1,
    "serial_number": 1,
    "installation_date": 1,
    "commission_date": 1,
    "criticality": 3,
    "rated_power": 2,
    "rated_voltage": 1,
    "rated_current": 1,
    "rated_flow": 2,
    "rated_pressure": 2,
    "rated_capacity": 2,
    "replacement_cost": 1,
    "expected_life_years": 1,
    "normal_operating_hours": 2,
}

# Keyed by the exact equipment_type strings the Phase 1 migration
# already wrote to the database (engine/factory_structure_migrator.py's
# _prettify_category() - title-cased from the tag-naming convention,
# including its as-written quirks like "Ahu"/"Ups"/"Mcc Room"). A type
# not listed here just uses DEFAULT_EQUIPMENT_FIELD_WEIGHTS unchanged.
#
# rated_capacity is deliberately reused for a chiller's cooling
# capacity, a tank's volume, a UPS's backup capacity, etc. rather than
# adding dedicated columns per equipment type - see the Phase 2 plan's
# explicit note on this trade-off.
EQUIPMENT_TYPE_WEIGHT_OVERRIDES: dict[str, dict[str, int]] = {
    "Air Compressor": {"rated_power": 4, "rated_pressure": 4, "rated_flow": 4},
    "Compressed Air Header": {"rated_pressure": 4, "rated_flow": 4, "rated_power": 0},
    "Chiller": {"rated_power": 4, "rated_capacity": 4},
    "Chilled Water Pump": {"rated_flow": 4, "rated_pressure": 3, "rated_power": 3},
    "Water Supply Pump": {"rated_flow": 4, "rated_pressure": 3, "rated_power": 3},
    "Water Supply": {"rated_flow": 3, "rated_pressure": 3, "rated_power": 0},
    "Cold Room": {"rated_capacity": 4, "criticality": 4, "rated_power": 3},
    "Bead Mill": {"rated_power": 3, "rated_capacity": 3},
    "High Speed Disperser": {"rated_power": 3},
    "Mixer": {"rated_power": 3, "rated_capacity": 3},
    "Filling Machine": {"rated_capacity": 3, "normal_operating_hours": 3},
    "Dust Collector": {"rated_flow": 3, "rated_power": 3},
    "Tank": {"rated_capacity": 4, "rated_power": 0, "rated_pressure": 0},
    "Solvent Transfer": {"rated_flow": 3, "rated_pressure": 3},
    "Water Treatment System": {"rated_flow": 3, "rated_capacity": 3},
    "Ro Di System": {"rated_flow": 3, "rated_capacity": 3},
    "Effluent Treatment": {"rated_flow": 3, "rated_capacity": 3},
    "Main Incomer": {"rated_power": 4, "rated_voltage": 4, "rated_current": 3},
    "Building Incomer": {"rated_power": 4, "rated_voltage": 4, "rated_current": 3},
    "Transformer": {"rated_power": 4, "rated_voltage": 4},
    "Ups": {"rated_power": 4, "rated_capacity": 3},
    "Generator": {"rated_power": 4, "rated_voltage": 3},
    "Ahu": {"rated_flow": 3, "rated_power": 3},
    "Mcc Room": {"criticality": 2, "rated_power": 0, "rated_pressure": 0, "rated_flow": 0},
    "Area Monitoring": {
        "rated_power": 0, "rated_voltage": 0, "rated_current": 0,
        "rated_flow": 0, "rated_pressure": 0, "criticality": 1,
    },
    "Weather Node": {
        "rated_power": 0, "rated_voltage": 0, "rated_current": 0,
        "rated_flow": 0, "rated_pressure": 0, "rated_capacity": 0,
    },
    "Building Water Meter": {"rated_flow": 3, "rated_power": 0},
    "Fire Water System": {"rated_flow": 4, "rated_pressure": 4, "criticality": 4},
}


def _score(row: dict, weights: dict[str, int]) -> dict:
    total_weight = sum(w for w in weights.values() if w > 0)
    filled_weight = 0
    missing: list[tuple[str, int]] = []

    for field, weight in weights.items():
        if weight <= 0:
            continue

        value = row.get(field)

        if value not in (None, ""):
            filled_weight += weight
        else:
            missing.append((field, weight))

    missing.sort(key=lambda item: -item[1])

    percent = round(100 * filled_weight / total_weight) if total_weight else 100

    return {
        "percent": percent,
        "filled_weight": filled_weight,
        "total_weight": total_weight,
        "missing": missing,
    }


def calculate_factory_completeness(factory_row: dict) -> dict:
    return _score(factory_row, FACTORY_FIELD_WEIGHTS)


def calculate_plant_completeness(plant_row: dict) -> dict:
    return _score(plant_row, PLANT_FIELD_WEIGHTS)


def equipment_field_weights(equipment_type: str | None) -> dict[str, int]:
    """The effective field->weight map for one equipment type - the
    default weights with that type's overrides layered on top. Exposed
    separately (not just used internally) so the UI can show *why* a
    field matters for a given equipment type, not just whether it's
    missing."""
    weights = dict(DEFAULT_EQUIPMENT_FIELD_WEIGHTS)
    weights.update(EQUIPMENT_TYPE_WEIGHT_OVERRIDES.get(equipment_type or "", {}))
    return weights


def calculate_equipment_completeness(equipment_row: dict) -> dict:
    weights = equipment_field_weights(equipment_row.get("equipment_type"))
    return _score(equipment_row, weights)


def calculate_overall_completeness(database_path: str | Path) -> dict:
    """
    Blends factory + every plant + every classified equipment instance
    into one headline percentage, plus a single ranked list of the
    most important missing information across all of them.

    Each record contributes its own weight budget to the blend (a
    factory's total_weight, a plant's, an individual equipment row's),
    rather than averaging percentages - this way a single equipment
    row is not treated as equally significant as the entire factory
    profile, but 76 real equipment rows collectively still meaningfully
    outweigh two plant rows, appropriately.

    Equipment rows with no plant_id (the legacy/orphan rows identified
    in the Phase 1 audit, left NULL on purpose) are excluded entirely -
    they are not part of the real classified equipment set and would
    only ever read as permanently incomplete noise.
    """
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    try:
        factory_row = connection.execute("SELECT * FROM factory LIMIT 1").fetchone()
        plant_rows = connection.execute("SELECT * FROM plants ORDER BY code").fetchall()
        equipment_rows = connection.execute(
            "SELECT * FROM equipment WHERE plant_id IS NOT NULL ORDER BY display_name"
        ).fetchall()
    finally:
        connection.close()

    total_filled = 0
    total_possible = 0
    all_missing: list[dict] = []

    factory_section = None
    if factory_row:
        result = calculate_factory_completeness(dict(factory_row))
        factory_section = {
            "scope": "factory",
            "id": factory_row["id"],
            "label": factory_row["name"] or "Factory",
            **result,
        }
        total_filled += result["filled_weight"]
        total_possible += result["total_weight"]
        for field, weight in result["missing"]:
            all_missing.append({"scope": "factory", "label": factory_section["label"], "field": field, "weight": weight})

    plant_sections = []
    for plant in plant_rows:
        result = calculate_plant_completeness(dict(plant))
        label = plant["name"] or plant["code"]
        plant_sections.append({"scope": "plant", "id": plant["id"], "label": label, **result})
        total_filled += result["filled_weight"]
        total_possible += result["total_weight"]
        for field, weight in result["missing"]:
            all_missing.append({"scope": "plant", "label": label, "field": field, "weight": weight})

    equipment_sections = []
    for equipment in equipment_rows:
        result = calculate_equipment_completeness(dict(equipment))
        label = equipment["display_name"] or equipment["name"]
        equipment_sections.append({"scope": "equipment", "id": equipment["id"], "label": label, **result})
        total_filled += result["filled_weight"]
        total_possible += result["total_weight"]
        for field, weight in result["missing"]:
            all_missing.append({"scope": "equipment", "label": label, "field": field, "weight": weight})

    all_missing.sort(key=lambda item: -item["weight"])

    overall_percent = round(100 * total_filled / total_possible) if total_possible else 100

    return {
        "overall_percent": overall_percent,
        "factory": factory_section,
        "plants": plant_sections,
        "equipment": equipment_sections,
        "top_missing": all_missing[:15],
    }
