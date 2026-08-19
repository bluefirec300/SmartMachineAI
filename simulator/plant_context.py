from __future__ import annotations

import math
import random
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

"""
Phase 5 (Simulation Realism) - shared context the PLC-tag simulator
(simulator/tag_dataset_model.py) consults once per update cycle so
equipment no longer behaves as fully independent per-tag noise: real
production state, a diurnal ambient environment, per-instance stable
efficiency/deterioration, and an explicit Main Incomer aggregation
boundary.

Deliberately a plain module of pure functions + small lookup tables,
reading production_batches directly from config.db once per cycle - no
JSON snapshot layer, per explicit direction (only worth adding later if
performance testing actually shows a need).

canonical_key()/instance_key() live here (moved from tag_dataset_model,
which now imports and re-exports them) since this module needs them for
the Main Incomer aggregation boundary and this keeps tag-naming parsing
in one place rather than duplicated.
"""


# ---------------------------------------------------------------------------
# Tag-naming helpers (moved here from tag_dataset_model.py - see module
# docstring)
# ---------------------------------------------------------------------------

def canonical_key(tag_name: str) -> str:
    """
    Strip the plant prefix (P01/P02) and instance numbers from a tag
    name, keeping the equipment-type code and signal name - e.g.
    "P01.UTILITY.AC01.OutletTemp" -> "UTILITY.AC.OutletTemp".
    """
    tokens = tag_name.split(".")[1:]

    if not tokens:
        return tag_name

    stripped = [
        re.sub(r"\d+$", "", token) if index < len(tokens) - 1 else token
        for index, token in enumerate(tokens)
    ]

    return ".".join(stripped)


def instance_key(tag_name: str) -> str:
    """
    The specific physical unit a tag belongs to - e.g.
    "P01.UTILITY.AC01.OutletTemp" -> "P01.UTILITY.AC01".
    """
    parts = tag_name.split(".")
    return ".".join(parts[:-1]) if len(parts) >= 2 else tag_name


def plant_of(instance_key_value: str) -> str:
    """"P01.UTILITY.AC01" -> "p01". Falls back to the first token
    lowercased for anything unexpectedly shaped."""
    return instance_key_value.split(".")[0].lower()


# ---------------------------------------------------------------------------
# Item 8 - response-speed tiers. Different physical quantities settle
# toward a new baseline at genuinely different rates in real equipment;
# without this every tag reverted at the same speed regardless of what it
# measured. Multiplies the tag's configured profile["revert"] - >1 means
# faster settling (electrical), <1 means slower/more inertia (room
# temperature). Matched by measurement first, with a handful of
# canonical-key overrides for cases the same measurement name covers
# multiple real response speeds (e.g. "temperature" spans a chiller's
# fast-ish supply temp and a cold room's slow-moving air mass).
# ---------------------------------------------------------------------------

RESPONSE_SPEED_BY_MEASUREMENT: dict[str, float] = {
    "power": 3.0, "current": 3.0, "voltage": 3.0, "frequency": 2.5, "pf": 2.5,
    "pressure": 1.6, "flow": 1.2,
    "vibration": 2.2,
    "temperature": 0.6,
    "humidity": 0.5,
}
DEFAULT_RESPONSE_SPEED_MULTIPLIER = 1.0

RESPONSE_SPEED_CANONICAL_OVERRIDES: dict[str, float] = {
    "UTILITY.CHL.SupplyTemp": 0.35,
    "UTILITY.CHL.ReturnTemp": 0.35,
    "COLDROOM.CR.RoomTemp": 0.12,
    "MCCROOM.ENV.Temperature": 0.2,
    "LABORATORYQC.ENV.Temperature": 0.2,
    "WAREHOUSE.ENV.Temperature": 0.25,
    "ENV.Temperature": 0.3,
}


def response_speed_multiplier(tag_name: str, measurement: str) -> float:
    override = RESPONSE_SPEED_CANONICAL_OVERRIDES.get(canonical_key(tag_name))
    if override is not None:
        return override

    return RESPONSE_SPEED_BY_MEASUREMENT.get(measurement, DEFAULT_RESPONSE_SPEED_MULTIPLIER)


# ---------------------------------------------------------------------------
# Environmental model - diurnal outdoor ambient temperature/humidity
# (item 5). Outdoor temperature is the dominant driver; humidity is a
# modest additional HVAC-demand influence, not full psychrometrics, per
# explicit scope confirmation. Peaks mid-afternoon, troughs before dawn -
# a plausible tropical-climate profile matching this project's existing
# ENV.Temperature band (24-34 degC).
# ---------------------------------------------------------------------------

AMBIENT_BASE_TEMP_C = 29.0
AMBIENT_DIURNAL_TEMP_AMPLITUDE_C = 4.5
AMBIENT_BASE_HUMIDITY_PCT = 75.0
AMBIENT_DIURNAL_HUMIDITY_AMPLITUDE_PCT = 12.0
AMBIENT_PEAK_HOUR = 15.0  # 3pm


def _diurnal_phase(now: datetime) -> float:
    hour = now.hour + now.minute / 60.0
    return (hour - AMBIENT_PEAK_HOUR) / 24.0 * 2 * math.pi


def ambient_target_temperature(now: datetime) -> float:
    return AMBIENT_BASE_TEMP_C + AMBIENT_DIURNAL_TEMP_AMPLITUDE_C * math.cos(_diurnal_phase(now))


def ambient_target_humidity(now: datetime) -> float:
    # Humidity runs opposite temperature (drier during the hot afternoon).
    return AMBIENT_BASE_HUMIDITY_PCT - AMBIENT_DIURNAL_HUMIDITY_AMPLITUDE_PCT * math.cos(_diurnal_phase(now))


def hvac_demand_push_fraction(outdoor_temp_c: float, outdoor_humidity_pct: float) -> float:
    """
    0.0+ multiplier-style factor nudging AHU fan power's baseline up as
    outdoor conditions get more demanding. Outdoor temperature dominates;
    humidity only adds a modest extra nudge (item 5 - no full
    psychrometric calculation).
    """
    temp_component = max(0.0, (outdoor_temp_c - 26.0) / 10.0)
    humidity_component = max(0.0, (outdoor_humidity_pct - 60.0) / 100.0)
    return min(1.5, temp_component * 0.8 + humidity_component * 0.3)


# ---------------------------------------------------------------------------
# Item 2 - plant differentiation. ONE small structural modifier (an
# unmetered base-load difference - see Main Incomer section below), plus
# a stable, independently-seeded per-instance efficiency jitter that
# applies identically regardless of plant. Because the jitter is
# independent per instance, a specific P02 unit can land better than its
# P01 counterpart even while P02's aggregate trends worse overall from
# the base-load difference.
# ---------------------------------------------------------------------------

EFFICIENCY_JITTER_RANGE = (0.93, 1.07)


def efficiency_factor(instance_key_value: str) -> float:
    rng = random.Random(f"efficiency::{instance_key_value}")
    return rng.uniform(*EFFICIENCY_JITTER_RANGE)


# ---------------------------------------------------------------------------
# Items 3 & 9 - deterioration. Only a fixed, seeded subset of instances
# (chosen once, never re-rolled) ever deteriorate; the rest stay stable
# forever. DETERIORATION_TIME_ACCELERATION_FACTOR is SIMULATION-ONLY: it
# scales only how fast the internal wear accumulator advances, never
# real historian timestamps or any other tag's timing - see
# tag_dataset_model._InstanceFaultState.advance_deterioration().
# ---------------------------------------------------------------------------

DETERIORATION_ELIGIBLE_FRACTION = 0.18

# SIMULATION-ONLY aging multiplier - configurable. 500x means each real
# second of simulator uptime counts as ~500 simulated seconds of wear, so
# an eligible instance's full 0->1 deterioration arc (see
# DETERIORATION_FULL_LIFE_SECONDS below, ~60 simulated days) plays out
# over roughly 60*86400/500 =~ 10,368 real seconds (~2.9 real hours) of
# continuous uptime - weeks of wear demonstrated in hours, as requested.
# Change this single constant to speed up/slow down deterioration
# demonstrations; it never touches plc_data/machine_events timestamps.
DETERIORATION_TIME_ACCELERATION_FACTOR = 500.0

DETERIORATION_FULL_LIFE_SECONDS = 60.0 * 24 * 3600  # ~60 simulated days
DETERIORATION_MAX_DRIFT_FRACTION = 0.5  # at level=1.0, up to half a band-width of extra drift

# Tag-name suffixes wear realistically shows up in (vibration + bearing
# temperature + current draw creeping up) - not every REAL tag on a
# deteriorating instance, just its genuine wear indicators.
WEAR_SENSITIVE_SUFFIXES = ("Vibration", "BearingTemp", "MotorCurrent")


def deterioration_eligible(instance_key_value: str) -> bool:
    rng = random.Random(f"deterioration_eligible::{instance_key_value}")
    return rng.random() < DETERIORATION_ELIGIBLE_FRACTION


def is_wear_sensitive_tag(tag_name: str) -> bool:
    return tag_name.endswith(WEAR_SENSITIVE_SUFFIXES)


# ---------------------------------------------------------------------------
# Item 6 - Main Incomer aggregation boundary. An EXPLICIT include list
# (never "sum every Power_kW tag") plus one unmetered base-load term, so
# sub-metering/pass-through points and non-electrical/backup equipment
# can never be double-counted.
#
# Included (genuine, non-overlapping end loads):
#   Air Compressors, Chillers, Chilled Water Pumps, Water Supply Pumps,
#   Cold Room compressors, AHU fan, Dust Collector fan, RO/DI system,
#   Wastewater blower, production equipment motors (Disperser/Mill/
#   Mixer), Filling Machine, small monitoring-room loads.
#
# Explicitly EXCLUDED (would double-count or isn't a real Main Incomer
# draw at all):
#   - Generator (ELEC.GEN) - a backup GENERATION source, not a load.
#   - Transformer (ELEC.TR LoadPct) - a sub-metering/pass-through view of
#     load already counted via the end loads above, not an independent
#     additive draw.
#   - UPS (IT.UPS LoadPct) - protects a subset of loads already counted,
#     same reasoning as the transformer.
#   - Fire Water System - diesel-driven standby pump, no material
#     electrical draw on the Main Incomer.
#
# Everything not individually tag-metered (tank agitators, solvent
# transfer pumps, general lighting/building services/losses) is folded
# into the single unmetered base-load term below instead of being
# itemized - documented here so future AI answers can honestly describe
# this as inferred/allocated rather than individually measured (item 7).
# ---------------------------------------------------------------------------

MAIN_INCOMER_INCLUDED_PREFIXES = (
    "UTILITY.AC", "UTILITY.CHL", "UTILITY.CHWP", "WATER.WSP",
    "COLDROOM.CR", "HVAC.AHU", "DUST.DC", "WT.RO", "WW.SYS",
    "PROD.DISP", "PROD.MILL", "PROD.MIX", "FILL.FILL",
    "LABORATORYQC.ENV", "WAREHOUSE.ENV",
)

MAIN_INCOMER_POWER_SUFFIXES = (
    "Power_kW", "CompressorPower", "FanPower", "MotorPower", "BlowerPower",
)

# Unmetered/base-load term (kW) per plant - lighting, small unmetered
# motors, general building services and losses. This is the ONE small
# plant-level structural modifier (item 2): P02's older general
# infrastructure runs a higher base load than P01's, rather than a
# blanket efficiency penalty applied to every piece of equipment.
PLANT_BASE_LOAD_KW = {"p01": 8.0, "p02": 9.6}
DEFAULT_PLANT_BASE_LOAD_KW = 8.0
BASE_LOAD_NOISE_KW = 0.3


def main_incomer_contributing_tags(all_tag_names: list[str], plant: str) -> list[str]:
    """
    Which tags' current values sum into `plant`'s Main Incomer Power_kW
    this cycle. `plant` is lowercase ("p01"/"p02"); tag names are the
    stored uppercase-plant form ("P01...").
    """
    prefix = f"{plant.upper()}."
    result = []

    for tag_name in all_tag_names:
        if not tag_name.startswith(prefix):
            continue
        if ".ELEC.MAIN." in tag_name or ".ELEC.INCOMER." in tag_name:
            continue  # never sum the Main Incomer into itself
        if not tag_name.endswith(MAIN_INCOMER_POWER_SUFFIXES):
            continue

        instance_type_key = canonical_key(tag_name).rsplit(".", 1)[0]
        if instance_type_key.startswith(MAIN_INCOMER_INCLUDED_PREFIXES):
            result.append(tag_name)

    return result


def plant_base_load_kw(plant: str, now: datetime, rng: random.Random) -> float:
    """
    The unmetered baseload term for `plant` at `now` - a small,
    production-hours-shaped variation around the plant's fixed base,
    plus a little noise so it isn't a dead-flat number. Weekend factor
    added alongside the existing day/night split (item: general
    building services/lighting are lighter on non-working days too) -
    this term already had day/night in it, so this is one small
    additive extension, not a new mechanism.
    """
    base = PLANT_BASE_LOAD_KW.get(plant, DEFAULT_PLANT_BASE_LOAD_KW)
    hour = now.hour
    daytime = 1.0 if 6 <= hour < 22 else 0.6  # lighting/services lighter overnight
    weekday = now.weekday()
    weekend_factor = 1.0 if weekday < 5 else (0.75 if weekday == 5 else 0.6)
    noise = rng.uniform(-BASE_LOAD_NOISE_KW, BASE_LOAD_NOISE_KW)
    return round(base * daytime * weekend_factor + noise, 2)


# ---------------------------------------------------------------------------
# Item 1 & 4 - production synchronization. Direct DB read of
# production_batches (Phase 4's authoritative production state), once
# per tag-simulator update cycle - no JSON snapshot layer.
# ---------------------------------------------------------------------------

# Equipment names follow Phase 1's "p0X_<type>_<instance>" convention
# (e.g. "p01_bead_mill_mill01") - the plant prefix and trailing instance
# code are the exact same tokens the tag-naming convention uses
# ("P01"/"MILL01"), by shared Phase-1 design, not coincidence.
_PRODUCTION_AREA_BY_NAME_FRAGMENT = (
    ("filling", "FILL"),
    ("bead_mill", "PROD"),
    ("high_speed_disperser", "PROD"),
    ("mixer", "PROD"),
)


def _equipment_name_to_instance_key(equipment_name: str) -> str | None:
    parts = equipment_name.split("_")
    if len(parts) < 2:
        return None

    plant = parts[0].upper()
    instance_code = parts[-1].upper()

    for fragment, area in _PRODUCTION_AREA_BY_NAME_FRAGMENT:
        if fragment in equipment_name:
            return f"{plant}.{area}.{instance_code}"

    return None


def get_production_state(database_path: str | Path) -> dict[str, dict[str, Any]]:
    """
    Currently-running production_batches rows, keyed by the equipment's
    tag-naming instance_key (e.g. "P01.PROD.MILL01"). Returns {} rather
    than raising if production_batches/products don't exist yet on this
    database (e.g. database/actual/config.db before Phase 4's migrator
    has been run there) - production sync degrades to "no context"
    instead of crashing the whole tag simulator over an optional,
    later-phase dependency.
    """
    try:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}

    try:
        rows = connection.execute(
            """
            SELECT b.equipment_id, e.name AS equipment_name, b.batch_code,
                   b.actual_quantity, b.good_quantity, b.reject_quantity,
                   p.product_code
            FROM production_batches b
            JOIN equipment e ON e.id = b.equipment_id
            JOIN products p ON p.id = b.product_id
            WHERE b.status = 'running'
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        connection.close()

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _equipment_name_to_instance_key(row["equipment_name"])
        if key is None:
            continue
        result[key] = {
            "batch_code": row["batch_code"],
            "product_code": row["product_code"],
            "actual_quantity": row["actual_quantity"] or 0.0,
            "good_quantity": row["good_quantity"] or 0.0,
            "reject_quantity": row["reject_quantity"] or 0.0,
        }

    return result


PRODUCTION_LINKED_PREFIXES = ("PROD.", "FILL.")


def is_production_linked_instance(instance_key_value: str) -> bool:
    """True for any instance whose tags live under the PROD.*/FILL.*
    area codes (the same 8 instances production_batches tracks)."""
    parts = instance_key_value.split(".")
    return len(parts) >= 2 and any(f"{parts[1]}.".startswith(p) for p in PRODUCTION_LINKED_PREFIXES)


IDLE_LOAD_FACTOR = 0.15  # fraction of the normal band a production
# instance's power/current/speed tags relax toward when it has no
# running batch, instead of sitting at full-load values while idle.
PRODUCTION_LOAD_SENSITIVE_MEASUREMENTS = {"power", "current", "speed"}


# ---------------------------------------------------------------------------
# Post-Phase-14 cleanup - realistic weekday/weekend operating-hours
# scheduling. Reuses Phase 2's EXISTING shift_definitions table (already
# schema-defined, already read by production_batch_simulator._resolve_shift_id()
# for batch metadata, but never actually seeded or used to GATE anything
# until now) rather than inventing a second, competing schedule
# mechanism - "the correct layer" per explicit direction. See
# engine/seed_shift_definitions.py for the seeded row this reads.
#
# Equipment is categorized by REAL function, never a single global
# multiplier:
#   - production_process/filling (PROD.*/FILL.*) - already fully
#     schedule-driven via production_batches/is_production_linked_instance
#     (unchanged); this section only changes HOW OFTEN new batches start
#     (see production_start_probability_factor()), not the existing
#     idle/production_push mechanic itself.
#   - air_compressor (UTILITY.AC) - compressed-air demand is almost
#     entirely production-driven -> genuinely STOPS most of the time
#     outside production hours (SCHEDULE_STOPPABLE_PREFIXES), not just a
#     reduced value, matching real practice (no point running a
#     compressor against no demand).
#   - chiller/chilled_water_pump/water_supply_pump/cold_room (UTILITY.CHL,
#     UTILITY.CHWP, WATER.WSP, COLDROOM.CR) - genuinely continuous-duty
#     systems (refrigeration thermal mass, hygiene/fire-reserve water
#     pressure) - RunStatus is UNCHANGED (stays in the existing ~99.8%
#     "always on" branch, never stopped), only their power/current
#     BASELINE relaxes to a lower (never zero) fraction outside
#     production hours, via the SAME production_push-style mechanic
#     already used for production equipment (see schedule_load_factor()).
#   - ahu (HVAC.AHU) - stays on (building envelope/frost protection),
#     reduced ventilation demand outside production hours.
#   - air_header (compressed-air network Flow/Pressure) is a KNOWN,
#     DELIBERATE gap: Flow/Pressure are not in
#     PRODUCTION_LOAD_SENSITIVE_MEASUREMENTS (schedule_load_factor() is
#     only wired into power/current/speed tags), so the header's own
#     Flow/Pressure baseline does not yet shift with the compressors
#     that feed it - flagged as a disclosed follow-up, not silently
#     forgotten (kept out of this pass to keep the change surgical).
#   - No fire/life-safety/security equipment TYPE exists in the current
#     tag-simulated dataset (the "Fire Water System" mentioned in the
#     Main Incomer boundary above is diesel-driven and explicitly
#     excluded from electrical simulation already) - there is nothing
#     here that this section could inadvertently disable, by
#     construction (every category below is an explicit opt-in prefix
#     list, never a sweep).
# ---------------------------------------------------------------------------

SATURDAY_RELATIVE_FACTOR = 0.75   # applied ON TOP OF an equipment's own off-hours fraction
SUNDAY_RELATIVE_FACTOR = 0.45     # lower than Saturday's, per explicit requirement

# Off-hours (outside the active weekday shift) load fraction each
# equipment type's power/current baseline relaxes toward, on an
# ordinary weekday night - never zero (standby/base load preserved).
# Saturday/Sunday multiply this further via *_RELATIVE_FACTOR above.
SCHEDULE_OFF_HOURS_LOAD_FACTOR: dict[str, float] = {
    "UTILITY.CHL": 0.55,     # chiller - continuous refrigeration duty, lower average outside hours
    "UTILITY.CHWP": 0.55,    # chilled water pump - follows the chiller circuit's own demand
    "WATER.WSP": 0.30,       # water supply pump - hygiene/fire-reserve pressure only outside hours
    "HVAC.AHU": 0.45,        # AHU - reduced ventilation for unoccupied space, envelope protection retained
    "COLDROOM.CR": 0.75,     # cold room compressor - well-insulated thermal mass, modest reduction only
    # Follow-up fix - air_compressor is in SCHEDULE_STOPPABLE_PREFIXES
    # below (its RunStatus genuinely goes to 0 most of the time outside
    # hours), but Power_kW/Current had NO corresponding reduction at
    # all - a genuine cross-equipment inconsistency (compressor showing
    # near-full-load power while its own RunStatus is nominally
    # stopped), caught during the consistency review this entry fixes.
    # Deliberately the lowest fraction of the group - compressed-air
    # demand is almost entirely production-driven (see
    # SCHEDULE_STOPPABLE_PREFIXES's own comment). This is a schedule-
    # AVERAGE reduction (not per-cycle coupled to the stochastic
    # RunStatus outcome - see the module's own "do not tightly couple
    # cycle-by-cycle" guidance), the same averaging relationship every
    # other entry in this dict already has with its own RunStatus. Uses
    # the WIDE anchor (see SCHEDULE_WIDE_ANCHOR_PREFIXES below) - its
    # own TAG_PROFILES band (22-30 kW) is too narrow relative to its
    # low bound for the plain low-anchor blend to produce a real drop.
    "UTILITY.AC": 0.08,
}

# Equipment whose TAG_PROFILES band is narrow relative to its own low
# bound (low/high ratio too close to 1) - blending toward profile["low"]
# alone (as the equipment above this comment already does, UNCHANGED)
# would only ever produce a shallow ~15-20% dip for these, not a real
# "substantially lower" one. These instead blend toward the WIDER
# anchor (profile["low"] - band, floored at 0 - the same floor the
# existing trend clamp already respects) - still bounded, never zero,
# genuinely lower. See simulator/tag_dataset_model.py's
# _schedule_blended_baseline().
SCHEDULE_WIDE_ANCHOR_PREFIXES = ("UTILITY.AC", "UTILITY.AIRHDR")

# Equipment that genuinely STOPS (not just reduces) most of the time
# outside production hours - RunStatus itself follows a schedule-aware
# probability instead of the generic ~99.8%-always-on branch.
SCHEDULE_STOPPABLE_PREFIXES = ("UTILITY.AC",)


def is_schedule_stoppable_canonical(canonical: str) -> bool:
    """`canonical` is canonical_key(tag_name) - e.g. 'UTILITY.AC.RunStatus'
    - callers already have this value computed, so this takes it
    directly rather than re-deriving it from an instance_key (whose
    trailing token is an instance NUMBER, not a signal name -
    canonical_key() would not strip it correctly for that shape)."""
    return canonical.startswith(SCHEDULE_STOPPABLE_PREFIXES)


def is_within_active_shift(database_path: str | Path, now: datetime) -> bool:
    """Direct read of Phase 2's shift_definitions (mirrors
    production_batch_simulator._resolve_shift_id()'s own matching logic,
    factory-wide only - this project has one shared production schedule,
    not per-plant shifts). Returns False (never guesses "in shift") if
    no active shift row matches, exactly like _resolve_shift_id()'s own
    no-match behavior."""
    try:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return False

    try:
        rows = connection.execute("SELECT start_time, end_time, days_of_week FROM shift_definitions WHERE active = 1").fetchall()
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()

    day_name = now.strftime("%a")
    current_time = now.strftime("%H:%M")
    for row in rows:
        days = [d.strip() for d in (row["days_of_week"] or "").split(",")]
        if day_name not in days:
            continue
        start, end = row["start_time"], row["end_time"]
        if start <= end:
            if start <= current_time <= end:
                return True
        elif current_time >= start or current_time <= end:  # crosses midnight
            return True
    return False


def schedule_load_factor(canonical: str, now: datetime, within_shift: bool) -> float | None:
    """1.0 = full normal load. None = this canonical tag-type is not
    schedule-sensitive at all (production-linked equipment uses its own
    separate production_push mechanic; most tag types - temperature,
    vibration, pressure sensors etc. - are simply unaffected)."""
    base_off_hours_fraction = None
    for prefix, fraction in SCHEDULE_OFF_HOURS_LOAD_FACTOR.items():
        if canonical.startswith(prefix):
            base_off_hours_fraction = fraction
            break
    if base_off_hours_fraction is None:
        return None

    weekday = now.weekday()
    if weekday < 5:
        return 1.0 if within_shift else base_off_hours_fraction
    if weekday == 5:
        return base_off_hours_fraction * SATURDAY_RELATIVE_FACTOR
    return base_off_hours_fraction * SUNDAY_RELATIVE_FACTOR


# Baseline weekday-off-hours on-probability for STOPPABLE equipment
# (air compressors) - deliberately NOT zero (occasional after-hours/
# weekend production, maintenance testing, natural variation rather
# than a rigid timer). Saturday/Sunday scale further down via the same
# *_RELATIVE_FACTOR constants used for the continuous-duty group above.
SCHEDULE_STOPPABLE_IN_SHIFT_ON_PROBABILITY = 0.98
SCHEDULE_STOPPABLE_OFF_HOURS_ON_PROBABILITY = 0.12


def schedule_stoppable_on_probability(now: datetime, within_shift: bool) -> float:
    weekday = now.weekday()
    if weekday < 5:
        return SCHEDULE_STOPPABLE_IN_SHIFT_ON_PROBABILITY if within_shift else SCHEDULE_STOPPABLE_OFF_HOURS_ON_PROBABILITY
    if weekday == 5:
        return SCHEDULE_STOPPABLE_OFF_HOURS_ON_PROBABILITY * SATURDAY_RELATIVE_FACTOR
    return SCHEDULE_STOPPABLE_OFF_HOURS_ON_PROBABILITY * SUNDAY_RELATIVE_FACTOR


# Multiplier on production_batch_simulator.START_BATCH_PROBABILITY -
# 1.0 inside an active weekday shift, sharply (but not completely)
# reduced outside it. Occasional off-hours/weekend batches remain
# possible (overtime, changeovers) - natural variation, never a rigid
# on/off switch.
PRODUCTION_START_IN_SHIFT_FACTOR = 1.0
PRODUCTION_START_OFF_HOURS_WEEKDAY_FACTOR = 0.08
PRODUCTION_START_SATURDAY_FACTOR = 0.30
PRODUCTION_START_SUNDAY_FACTOR = 0.08


def production_start_probability_factor(now: datetime, within_shift: bool) -> float:
    weekday = now.weekday()
    if weekday < 5:
        return PRODUCTION_START_IN_SHIFT_FACTOR if within_shift else PRODUCTION_START_OFF_HOURS_WEEKDAY_FACTOR
    if weekday == 5:
        return PRODUCTION_START_SATURDAY_FACTOR
    return PRODUCTION_START_SUNDAY_FACTOR


# ---------------------------------------------------------------------------
# Follow-up - air_header (compressed-air network) schedule awareness, the
# disclosed gap from the previous pass: Flow/Pressure are not "power"/
# "current"/"speed" measurements, so schedule_load_factor() above never
# engaged for them. A DEDICATED, SEPARATE function (not a change to
# schedule_load_factor()'s existing signature/behavior - zero risk to
# already-tested equipment) because air_header needs TWO DIFFERENT
# fractions under the SAME canonical prefix: Flow tracks compressor
# demand closely (consistent with air_compressor now mostly stopping
# outside hours - the header must not keep showing production-level
# consumption once its compressors are off), Pressure is actively
# regulated and stays close to its controlled setpoint even as flow
# demand varies, per real compressed-air system behavior.
# ---------------------------------------------------------------------------

AIR_HEADER_PREFIX = "UTILITY.AIRHDR"
AIR_HEADER_SCHEDULE_SENSITIVE_MEASUREMENTS = {"flow", "pressure"}

AIR_HEADER_OFF_HOURS_FACTOR_BY_MEASUREMENT: dict[str, float] = {
    "flow": 0.20,       # substantially lower - consistent with compressors mostly stopped
    "pressure": 0.92,   # stays close to setpoint - regulated, not demand-proportional
}

# Weekend relative scaling applied ON TOP OF the weekday off-hours
# fraction above - deliberately measurement-specific rather than reusing
# the shared SATURDAY_RELATIVE_FACTOR/SUNDAY_RELATIVE_FACTOR: those were
# calibrated for equipment whose off-hours fraction is already a
# moderate reduction (0.3-0.75), where a further ~25-55% relative cut
# still lands somewhere reasonable. Applying the SAME relative cut to
# pressure's already-small 0.92 off-hours fraction would compound into
# a 31-59% pressure SAG on Sat/Sun - directly contradicting "pressure
# should not simply collapse, stays around a realistic controlled/
# setpoint range". Flow reuses the shared, larger swing (a sharp
# weekend throughput drop is realistic); pressure gets its own much
# gentler pair.
AIR_HEADER_WEEKEND_RELATIVE_FACTOR_BY_MEASUREMENT: dict[str, tuple[float, float]] = {
    "flow": (SATURDAY_RELATIVE_FACTOR, SUNDAY_RELATIVE_FACTOR),
    "pressure": (0.97, 0.93),
}


def air_header_schedule_factor(measurement: str, now: datetime, within_shift: bool) -> float | None:
    """1.0 = full normal load. None = not one of Flow/Pressure (e.g.
    DewPoint, an environmental/dryer-performance signal, not a demand
    signal - deliberately excluded)."""
    base_off_hours_fraction = AIR_HEADER_OFF_HOURS_FACTOR_BY_MEASUREMENT.get(measurement)
    if base_off_hours_fraction is None:
        return None

    weekday = now.weekday()
    if weekday < 5:
        return 1.0 if within_shift else base_off_hours_fraction

    saturday_relative, sunday_relative = AIR_HEADER_WEEKEND_RELATIVE_FACTOR_BY_MEASUREMENT[measurement]
    if weekday == 5:
        return base_off_hours_fraction * saturday_relative
    return base_off_hours_fraction * sunday_relative
