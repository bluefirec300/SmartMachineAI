from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

"""
Phase 8 - Baseline Engine target registry. A curated, explicit list of
what gets baselined and how, per your direction not to automatically
sweep all 625 tags. EquipmentTypeProfile defines context dimensions,
downsampling bucket width, and window defaults ONCE per equipment
type; discover_targets() expands each profile against whichever
instances of that type actually exist (queried from the live tags
table, never hardcoded per-instance) into concrete BaselineTarget
objects - so a future AC04 needs no registry change, only a new
instance to be enabled.

Explicitly excluded from any profile below (per your approval):
  - pump hydraulic efficiency (the derived KPI, not its raw inputs -
    Phase 5 doesn't couple pump pressure/flow/power well enough for
    that efficiency figure to represent meaningful normal behavior)
  - true per-compressor kWh/Nm3 (no per-compressor flow meter exists;
    header-level plant compressed-air specific energy is baselined
    instead)
"""


@dataclass(frozen=True)
class EquipmentTypeProfile:
    equipment_type: str
    area_code: str                       # canonical area+type prefix, e.g. "UTILITY.CHL"
    raw_targets: tuple[str, ...]          # tag suffixes baselined directly
    derived_targets: tuple[str, ...]      # names dispatched to engine.baseline_engine's DERIVED_CALCULATORS
    context_dimensions: tuple[str, ...]   # e.g. ("load_bucket", "ambient_bucket", "hour_bucket")
    aggregation_minutes: int              # downsampling bucket width (item 3)
    reference_window_days_max: int = 60
    recent_window_days: int = 7


@dataclass(frozen=True)
class BaselineTarget:
    plant_code: str
    instance_key: str          # e.g. "P01.UTILITY.CHL01"
    equipment_type: str
    target_key: str            # e.g. "Power_kW" or "cop"
    tag_name: str | None       # concrete tag to read; None for a derived target
    is_derived: bool
    context_dimensions: tuple[str, ...]
    aggregation_minutes: int
    reference_window_days_max: int
    recent_window_days: int


# Response-speed-informed downsampling: fast electrical/process signals
# get a 5-minute representative bucket; slower thermal signals (room/
# cold-room/chilled-water temperatures) get 15 minutes, since they
# genuinely change more slowly and a finer bucket would just re-count
# the same slow drift as more "observations" than it really is.
FAST_AGGREGATION_MINUTES = 5
SLOW_AGGREGATION_MINUTES = 15

EQUIPMENT_TYPE_PROFILES: dict[str, EquipmentTypeProfile] = {
    "plant_energy": EquipmentTypeProfile(
        equipment_type="plant_energy",
        area_code="ELEC.MAIN",
        raw_targets=("Power_kW",),
        # non_production_demand_kw - added specifically to support Phase 9's
        # "elevated non-production demand" anomaly rule (disclosed Phase 8
        # registry extension, narrowly scoped - see Phase 9's report).
        # ELEC.MAIN.Power_kW filtered to buckets where the plant had no
        # running production batch - a plain filter of the existing raw
        # signal by production_state context, not a new formula.
        derived_targets=("non_production_demand_kw",),
        # Ordered lowest-to-highest match priority (baseline_engine's
        # progressive matcher drops from the front when insufficient) -
        # production_state kept LAST/longest deliberately: whether the
        # plant is producing dominates total demand far more than the
        # hour or day type does, confirmed by a real bug this ordering
        # caused during Phase 9's live testing (dropping production_state
        # first blended non-production-heavy history into a live
        # production-state comparison, producing an wildly wrong
        # expected value - see FACTORY_AI_DEVELOPMENT_STATUS.md).
        context_dimensions=("day_type", "hour_bucket", "production_state"),
        aggregation_minutes=FAST_AGGREGATION_MINUTES,
    ),
    "air_compressor": EquipmentTypeProfile(
        equipment_type="air_compressor",
        area_code="UTILITY.AC",
        raw_targets=("Power_kW", "Pressure", "OutletTemp"),
        derived_targets=(),
        # A fixed-speed compressor's real load state is binary
        # (LoadStatus/UnloadStatus), not a continuous tercile - more
        # physically accurate than treating it like a VFD-driven load.
        # "day_type" added post-Phase-14: compressed-air demand now
        # genuinely differs weekday-vs-weekend (see
        # simulator/plant_context.py's schedule-aware realism update) -
        # without this, weekday and weekend samples would blend into
        # one context bucket and either mask a real anomaly or flag an
        # ordinary weekend reading as one, exactly the risk that change
        # was reviewed against.
        context_dimensions=("loaded_state", "hour_bucket", "day_type"),
        aggregation_minutes=FAST_AGGREGATION_MINUTES,
    ),
    "air_header": EquipmentTypeProfile(
        equipment_type="air_header",
        area_code="UTILITY.AIRHDR",
        raw_targets=("Flow", "Pressure"),
        derived_targets=("compressed_air_specific_energy",),
        context_dimensions=("production_state", "hour_bucket"),
        aggregation_minutes=FAST_AGGREGATION_MINUTES,
    ),
    "chiller": EquipmentTypeProfile(
        equipment_type="chiller",
        area_code="UTILITY.CHL",
        raw_targets=("Power_kW", "LoadPct", "SupplyTemp", "ReturnTemp", "WaterFlow"),
        derived_targets=("cop", "cooling_output_kw"),
        # "day_type" added post-Phase-14 - see the air_compressor
        # comment above; chiller/pump/AHU power now genuinely varies
        # weekday-vs-weekend too.
        context_dimensions=("load_bucket", "ambient_bucket", "hour_bucket", "day_type"),
        aggregation_minutes=FAST_AGGREGATION_MINUTES,
    ),
    "chilled_water_pump": EquipmentTypeProfile(
        equipment_type="chilled_water_pump",
        area_code="UTILITY.CHWP",
        raw_targets=("Power_kW", "Flow", "Frequency", "Vibration", "BearingTemp"),
        derived_targets=("delta_p_bar", "flow_per_kw"),
        context_dimensions=("load_bucket", "hour_bucket", "day_type"),
        aggregation_minutes=FAST_AGGREGATION_MINUTES,
    ),
    "water_supply_pump": EquipmentTypeProfile(
        equipment_type="water_supply_pump",
        area_code="WATER.WSP",
        raw_targets=("Power_kW", "Flow", "Frequency", "Vibration", "BearingTemp"),
        derived_targets=("delta_p_bar", "flow_per_kw"),
        context_dimensions=("load_bucket", "hour_bucket", "day_type"),
        aggregation_minutes=FAST_AGGREGATION_MINUTES,
    ),
    "ahu": EquipmentTypeProfile(
        equipment_type="ahu",
        area_code="HVAC.AHU",
        raw_targets=("FanPower", "FanFrequency", "FilterDP", "SupplyAirTemp", "ReturnAirTemp"),
        derived_targets=(),
        context_dimensions=("ambient_bucket", "hour_bucket", "day_type"),
        aggregation_minutes=FAST_AGGREGATION_MINUTES,
    ),
    "cold_room": EquipmentTypeProfile(
        equipment_type="cold_room",
        area_code="COLDROOM.CR",
        raw_targets=("RoomTemp", "CompressorPower"),
        derived_targets=(),
        context_dimensions=("hour_bucket", "day_type"),
        aggregation_minutes=SLOW_AGGREGATION_MINUTES,
    ),
    "production_process": EquipmentTypeProfile(
        equipment_type="production_process",
        area_code="PROD",  # matches PROD.DISP/PROD.MILL/PROD.MIX - see discover_targets()
        raw_targets=("MotorPower", "MotorCurrent", "ProcessTemp", "Vibration"),
        derived_targets=(),
        context_dimensions=("product_code", "product_category", "running_state"),
        aggregation_minutes=FAST_AGGREGATION_MINUTES,
    ),
    "filling": EquipmentTypeProfile(
        equipment_type="filling",
        area_code="FILL.FILL",
        raw_targets=("Power_kW",),
        derived_targets=(),
        context_dimensions=("product_code", "product_category", "running_state"),
        aggregation_minutes=FAST_AGGREGATION_MINUTES,
    ),
}

# production_process's area_code is a family prefix (PROD.DISP/PROD.MILL/
# PROD.MIX all share the same profile) - listed explicitly since it's
# the one profile that doesn't map to a single canonical area code.
PRODUCTION_PROCESS_AREA_CODES = ("PROD.DISP", "PROD.MILL", "PROD.MIX")


def _connect(config_database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    return connection


def _instances_for_area_code(connection: sqlite3.Connection, plant_code: str, area_code: str) -> list[str]:
    """Distinct instance keys (e.g. 'P01.UTILITY.CHL01') actually
    enabled for this plant+area, discovered from the tags table -
    never hardcoded."""
    prefix = f"{plant_code.upper()}.{area_code}"
    rows = connection.execute(
        "SELECT DISTINCT tag_name FROM tags WHERE tag_name LIKE ? AND enabled = 1 AND driver = 'simulator'",
        (f"{prefix}%",),
    ).fetchall()

    instances = set()
    for row in rows:
        tag_name = row["tag_name"]
        parts = tag_name.split(".")
        # area_code may itself be multi-segment (e.g. "UTILITY.CHL") -
        # the instance key is [plant, *area_tokens, instance_number],
        # i.e. 1 (plant) + area_token_count tokens, with the trailing
        # signal name (everything after) dropped.
        area_token_count = len(area_code.split("."))
        instance_token_count = 1 + area_token_count
        if len(parts) <= instance_token_count:
            continue
        instances.add(".".join(parts[:instance_token_count]))

    return sorted(instances)


def discover_targets(config_database_path: str | Path, plant_code: str) -> list[BaselineTarget]:
    connection = _connect(config_database_path)
    try:
        targets: list[BaselineTarget] = []

        for profile in EQUIPMENT_TYPE_PROFILES.values():
            if profile.equipment_type == "production_process":
                area_codes = PRODUCTION_PROCESS_AREA_CODES
            else:
                area_codes = (profile.area_code,)

            instances: list[str] = []
            for area_code in area_codes:
                instances.extend(_instances_for_area_code(connection, plant_code, area_code))

            for instance_key in instances:
                for suffix in profile.raw_targets:
                    tag_name = f"{instance_key}.{suffix}"
                    exists = connection.execute(
                        "SELECT 1 FROM tags WHERE tag_name = ? AND enabled = 1", (tag_name,)
                    ).fetchone()
                    if not exists:
                        continue
                    targets.append(BaselineTarget(
                        plant_code=plant_code, instance_key=instance_key, equipment_type=profile.equipment_type,
                        target_key=suffix, tag_name=tag_name, is_derived=False,
                        context_dimensions=profile.context_dimensions, aggregation_minutes=profile.aggregation_minutes,
                        reference_window_days_max=profile.reference_window_days_max, recent_window_days=profile.recent_window_days,
                    ))

                for derived_key in profile.derived_targets:
                    targets.append(BaselineTarget(
                        plant_code=plant_code, instance_key=instance_key, equipment_type=profile.equipment_type,
                        target_key=derived_key, tag_name=None, is_derived=True,
                        context_dimensions=profile.context_dimensions, aggregation_minutes=profile.aggregation_minutes,
                        reference_window_days_max=profile.reference_window_days_max, recent_window_days=profile.recent_window_days,
                    ))

        return targets
    finally:
        connection.close()
