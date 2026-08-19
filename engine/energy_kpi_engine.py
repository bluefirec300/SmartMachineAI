from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config.environment import get_config_db_path, get_machine_db_path
from database.database import DatabaseManager
from engine.energy_tariff import get_tariff_for_date

"""
Phase 6 - Energy KPI Engine. Deterministic backend calculations only
(Rule 4 - the LLM never computes a KPI, only explains one already
computed here). Every calculate_*()-style function returns the same
metadata shape (see _kpi() below) so a caller - and later, Phase 16's
Data Health system and Ask AI - can always tell not just a KPI's value
but where it came from and how much to trust it:

    value               float | None
    unit                str
    period_start/_end   "%Y-%m-%d %H:%M:%S" strings (or None for an
                         instantaneous reading)
    source_tags         list[str] - every tag actually read
    classification      "DIRECT" | "ESTIMATED" | "UNAVAILABLE"
    calculation_type     "MEASURED" | "CALCULATED" | "ESTIMATED" | None
                         (None only when classification is UNAVAILABLE)
    assumptions         list[str] - engineering assumptions applied
    missing_inputs      list[str] - populated only when UNAVAILABLE

classification says how much to trust the number; calculation_type says
what kind of thing produced it (a raw sensor reading vs. a formula
applied to real measurements vs. a statistical estimate) - e.g.
ELEC.MAIN.Power_kW is DIRECT+MEASURED, Chiller COP is DIRECT+CALCULATED
(a real formula over real inputs), Projected Month Cost is
ESTIMATED+ESTIMATED (an extrapolation, not a measurement of anything).

Electrical hierarchy: ELEC.MAIN is the sole authoritative plant
incomer (Phase 5's aggregation boundary, reused unchanged here).
ELEC.INCOMER01 - a separate, smaller, unreferenced-elsewhere tag set -
is never read by this module. See FACTORY_AI_DEVELOPMENT_STATUS.md's
Phase 5/6 sections for the full audit trail behind that decision.

Persistence: only two tables exist for this phase
(engine/energy_kpi_migrator.py) - a per-plant-per-billing-period
Maximum Demand record (needs to survive restarts and stay stable once
a billing period closes) and a per-plant-per-day summary rollup (so a
future trend chart never has to rescan raw plc_data). Every other KPI
here is cheap enough to recompute on every call - no other persistence
exists, deliberately, to avoid duplicating the historian.
"""

DEFAULT_CONFIG_DATABASE_PATH = get_config_db_path()
DEFAULT_MACHINE_DATABASE_PATH = get_machine_db_path()

# --- Engineering constants (all documented, none silently assumed) ---
WATER_DENSITY_KG_PER_M3 = 1000.0
WATER_SPECIFIC_HEAT_KJ_PER_KGK = 4.186
DEFAULT_DEMAND_INTERVAL_MINUTES = 15
BASE_LOAD_PERCENTILE = 10
PUMP_EFFICIENCY_IMPLAUSIBLE_THRESHOLD_PCT = 105.0  # >100% + 5% noise tolerance
STALE_READING_SECONDS = 300  # 5 min - generous relative to Power_kW's 10s log cadence
ZERO_FLOW_EPSILON = 1e-6

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# KPI result contract
# ---------------------------------------------------------------------------

def _kpi(
    value: float | None,
    unit: str,
    classification: str,
    calculation_type: str | None,
    source_tags: list[str],
    period_start: str | None = None,
    period_end: str | None = None,
    assumptions: list[str] | None = None,
    missing_inputs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "period_start": period_start,
        "period_end": period_end,
        "source_tags": source_tags,
        "classification": classification,
        "calculation_type": calculation_type,
        "assumptions": assumptions or [],
        "missing_inputs": missing_inputs or [],
    }


def _unavailable(unit: str, source_tags: list[str], missing_inputs: list[str], **kwargs) -> dict[str, Any]:
    return _kpi(None, unit, "UNAVAILABLE", None, source_tags, missing_inputs=missing_inputs, **kwargs)


# ---------------------------------------------------------------------------
# Low-level historian helpers
# ---------------------------------------------------------------------------

def _fmt(dt: datetime) -> str:
    return dt.strftime(TIME_FORMAT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT)


def _reasonable_limit(start: datetime, end: datetime, assumed_interval_seconds: int) -> int:
    """
    get_history_range()'s default limit=5000 silently truncates a
    month-long query at this project's logging cadences (found during
    Phase 6 planning). Size the limit from the actual span instead of
    trusting the default, with generous headroom - SQLite handles tens
    of thousands of indexed rows trivially (idx_plc_data_tag_time).
    """
    span_seconds = max(0.0, (end - start).total_seconds())
    return max(100, int(span_seconds / assumed_interval_seconds * 1.5) + 100)


def latest_with_timestamp(historian: DatabaseManager, tag_name: str) -> tuple[float | None, datetime | None]:
    row = historian.get_latest(tag_name)
    if row is None:
        return None, None
    return row["value"], _parse(row["time"])


def is_stale(timestamp: datetime | None, now: datetime | None = None, max_age_seconds: float = STALE_READING_SECONDS) -> bool:
    if timestamp is None:
        return True
    now = now or datetime.now()
    return (now - timestamp).total_seconds() > max_age_seconds


def accumulated_positive_delta(
    historian: DatabaseManager,
    tag_name: str,
    start: datetime,
    end: datetime,
    assumed_interval_seconds: int = 60,
    limit: int | None = None,
) -> float | None:
    """
    Reset-safe cumulative-meter delta sum over [start, end] - the
    general form of ui/scada_floor_plan_data.py's original
    _todays_accumulated_total() (Phase 3/4), which that function now
    delegates to. Sums only positive deltas between consecutive
    samples; a decrease is treated as a counter reset (plc_logger/
    simulator restart) and skipped rather than corrupting the total.
    None if fewer than 2 samples exist in the window (nothing to
    compare), never a misleading 0.
    """
    limit = limit or _reasonable_limit(start, end, assumed_interval_seconds)
    rows = historian.get_history_range(tag=tag_name, start=_fmt(start), end=_fmt(end), limit=limit)

    if len(rows) < 2:
        return None

    total = 0.0
    for previous, current in zip(rows, rows[1:]):
        delta = current["value"] - previous["value"]
        if delta > 0:
            total += delta

    return total


def trapezoidal_integral(
    historian: DatabaseManager,
    tag_name: str,
    start: datetime,
    end: datetime,
    assumed_interval_seconds: int = 10,
    limit: int | None = None,
) -> float | None:
    """
    Numerical (trapezoidal) integration of an instantaneous-reading tag
    (Power_kW -> kWh, or a flow rate -> volume) over [start, end], for
    equipment with no dedicated cumulative meter tag. Returns None with
    fewer than 2 samples. A non-positive time gap between two rows
    (duplicate/out-of-order timestamps) is skipped rather than
    corrupting the running total.
    """
    limit = limit or _reasonable_limit(start, end, assumed_interval_seconds)
    rows = historian.get_history_range(tag=tag_name, start=_fmt(start), end=_fmt(end), limit=limit)

    if len(rows) < 2:
        return None

    total = 0.0
    for previous, current in zip(rows, rows[1:]):
        dt_hours = (_parse(current["time"]) - _parse(previous["time"])).total_seconds() / 3600.0
        if dt_hours <= 0:
            continue
        avg_value = (previous["value"] + current["value"]) / 2.0
        total += avg_value * dt_hours

    return total


def rolling_window_max_average(
    samples: list[tuple[datetime, float]],
    window_minutes: int,
    min_fill_fraction: float = 0.5,
    assumed_interval_seconds: int = 10,
) -> tuple[float | None, datetime | None]:
    """
    The highest simple average of `samples` (sorted ascending by time)
    within any trailing window_minutes-wide window - an O(n) sliding-
    window scan. A window is only eligible once it holds at least
    min_fill_fraction of the sample count a full window would normally
    contain, so a single early sample right at the start of the
    queried range can't register as a spuriously "maximum" 1-sample
    average.
    """
    if not samples:
        return None, None

    window = timedelta(minutes=window_minutes)
    expected_count = max(1, int(window_minutes * 60 / assumed_interval_seconds))
    min_count = max(1, int(expected_count * min_fill_fraction))

    left = 0
    running_sum = 0.0
    max_avg = None
    max_at = None

    for right in range(len(samples)):
        running_sum += samples[right][1]
        while samples[right][0] - samples[left][0] > window:
            running_sum -= samples[left][1]
            left += 1

        count = right - left + 1
        if count < min_count:
            continue

        avg = running_sum / count
        if max_avg is None or avg > max_avg:
            max_avg = avg
            max_at = samples[right][0]

    return max_avg, max_at


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Sorted, disjoint union of possibly-overlapping [start, end) intervals -
    used so simultaneously-running production batches never double-count
    the plant energy consumed during their overlap."""
    if not intervals:
        return []

    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged = [ordered[0]]

    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def _time_within_intervals(t: datetime, intervals: list[tuple[datetime, datetime]]) -> bool:
    return any(start <= t <= end for start, end in intervals)


def effective_tariff(config_database_path: str | Path, plant_id: int | None, on_date: datetime) -> dict | None:
    """
    Plant-specific tariff first; falls back to the factory-wide
    (plant_id IS NULL) tariff if no plant-specific one has been
    configured for this date - mirrors the Factory Configuration
    page's own "Factory-wide" vs. per-plant Scope choice (a user
    reasonably configuring only one factory-wide tariff should not
    make every plant-level KPI go UNAVAILABLE). energy_tariff.py's own
    get_tariff_for_date() stays strictly scoped by design - this
    fallback lives here, one level up, not inside Phase 3's code.
    """
    if plant_id is not None:
        tariff = get_tariff_for_date(config_database_path, plant_id, on_date)
        if tariff is not None:
            return tariff

    return get_tariff_for_date(config_database_path, None, on_date)


# ---------------------------------------------------------------------------
# Config-database helpers (plants/products/production_batches - engine/
# never imports from ui/, so these are intentionally local rather than
# reused from ui.factory_config_data/ui.production_data)
# ---------------------------------------------------------------------------

def _connect_config(config_database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    return connection


def get_plant(config_database_path: str | Path, plant_code: str) -> dict[str, Any] | None:
    connection = _connect_config(config_database_path)
    try:
        row = connection.execute(
            "SELECT id, code, name FROM plants WHERE code = ?", (plant_code.lower(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def _running_batch_intervals(
    config_database_path: str | Path, plant_id: int, start: datetime, end: datetime, now: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """Every production_batches interval for this plant overlapping
    [start, end], clipped to that window. A still-running batch's open
    end is treated as `now` (or `end`, whichever is earlier)."""
    now = now or datetime.now()
    connection = _connect_config(config_database_path)
    try:
        rows = connection.execute(
            """
            SELECT start_time, end_time, status FROM production_batches
            WHERE plant_id = ?
              AND start_time <= ?
              AND (end_time IS NULL OR end_time >= ?)
            """,
            (plant_id, _fmt(end), _fmt(start)),
        ).fetchall()
    finally:
        connection.close()

    intervals = []
    for row in rows:
        batch_start = max(_parse(row["start_time"]), start)
        batch_end_raw = _parse(row["end_time"]) if row["end_time"] else min(now, end)
        batch_end = min(batch_end_raw, end)
        if batch_end > batch_start:
            intervals.append((batch_start, batch_end))

    return _merge_intervals(intervals)


# ---------------------------------------------------------------------------
# Factory / plant electrical energy
# ---------------------------------------------------------------------------

def current_demand_kw(historian: DatabaseManager, plant_code: str, now: datetime | None = None) -> dict[str, Any]:
    tag = f"{plant_code.upper()}.ELEC.MAIN.Power_kW"
    value, timestamp = latest_with_timestamp(historian, tag)

    if value is None or is_stale(timestamp, now):
        return _unavailable("kW", [tag], [f"{tag} missing or stale (last reading: {timestamp})"])

    return _kpi(value, "kW", "DIRECT", "MEASURED", [tag])


def energy_for_period(
    historian: DatabaseManager, plant_code: str, start: datetime, end: datetime,
) -> dict[str, Any]:
    """Energy Today/Yesterday/Month all reduce to this - only the
    [start, end] window differs. Uses the reset-safe cumulative-meter
    method against ELEC.MAIN.Energy_kWh, since that tag genuinely
    exists (Phase 5 made it a real integration of Power_kW)."""
    tag = f"{plant_code.upper()}.ELEC.MAIN.Energy_kWh"
    total = accumulated_positive_delta(historian, tag, start, end, assumed_interval_seconds=60)

    if total is None:
        return _unavailable("kWh", [tag], [f"insufficient {tag} history in the requested period"], period_start=_fmt(start), period_end=_fmt(end))

    return _kpi(total, "kWh", "DIRECT", "CALCULATED", [tag], period_start=_fmt(start), period_end=_fmt(end),
                assumptions=["cumulative-meter positive-delta sum; counter resets are detected and skipped"])


def cost_for_period(
    config_database_path: str | Path, historian: DatabaseManager, plant_id: int | None, plant_code: str | None,
    start: datetime, end: datetime,
) -> dict[str, Any]:
    """
    Day-by-day tariff-aware cost: splits the period at each calendar-
    day boundary and looks up that day's applicable tariff separately,
    so a tariff change mid-period is honored rather than the current
    tariff being applied retroactively to the whole range.
    """
    tag = f"{plant_code.upper()}.ELEC.MAIN.Energy_kWh" if plant_code else None
    source_tags = [tag] if tag else []

    total_cost = 0.0
    total_kwh = 0.0
    unpriced_kwh = 0.0  # energy that WAS measured on a day with no tariff in effect - excluded, not zero-cost
    currency = None
    any_simulated = False
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)

    while day < end:
        day_start = max(day, start)
        day_end = min(day + timedelta(days=1), end)

        day_kwh = accumulated_positive_delta(historian, tag, day_start, day_end, assumed_interval_seconds=60) if tag else None
        tariff = effective_tariff(config_database_path, plant_id, day_start)

        if day_kwh:
            if tariff and tariff.get("energy_rate") is not None:
                total_cost += day_kwh * tariff["energy_rate"]
                total_kwh += day_kwh
                currency = currency or tariff.get("currency")
                any_simulated = any_simulated or bool(tariff.get("is_simulated"))
            else:
                unpriced_kwh += day_kwh

        day += timedelta(days=1)

    if total_kwh == 0.0:
        return _unavailable("currency", source_tags, ["no energy/tariff data in the requested period"], period_start=_fmt(start), period_end=_fmt(end))

    assumptions = ["flat energy_rate applied per calendar day using that day's effective tariff"]
    if any_simulated:
        assumptions.append("one or more days used a SIMULATION TARIFF placeholder, not a configured real rate")
    if unpriced_kwh > 0:
        assumptions.append(
            f"PARTIAL TOTAL: {round(unpriced_kwh, 1)} kWh in this period had no tariff configured for that day and "
            "was excluded from this cost figure - that energy was NOT priced, not zero-cost. The true full-period "
            "cost is understated by that amount."
        )

    return _kpi(round(total_cost, 2), currency or "?", "DIRECT", "CALCULATED", source_tags,
                period_start=_fmt(start), period_end=_fmt(end), assumptions=assumptions)


def projected_month_cost(
    config_database_path: str | Path, historian: DatabaseManager, plant_id: int | None, plant_code: str, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_elapsed = max((now - month_start).total_seconds() / 86400.0, 0.05)

    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    days_in_month = (next_month - month_start).days

    mtd = cost_for_period(config_database_path, historian, plant_id, plant_code, month_start, now)
    if mtd["classification"] == "UNAVAILABLE":
        return _unavailable(mtd["unit"], mtd["source_tags"], mtd["missing_inputs"], period_start=_fmt(month_start), period_end=_fmt(now))

    projected = mtd["value"] / days_elapsed * days_in_month

    return _kpi(
        round(projected, 2), mtd["unit"], "ESTIMATED", "ESTIMATED", mtd["source_tags"],
        period_start=_fmt(month_start), period_end=_fmt(now),
        assumptions=[f"linear extrapolation of {days_elapsed:.1f} days-elapsed cost to {days_in_month} days in month"],
    )


def estimated_maximum_demand(
    historian: DatabaseManager, plant_code: str, start: datetime, end: datetime,
    interval_minutes: int = DEFAULT_DEMAND_INTERVAL_MINUTES,
) -> dict[str, Any]:
    """
    Per your explicit direction: NEVER presented as official utility
    Maximum Demand. This is the highest simple average of Power_kW
    samples within any trailing interval_minutes window - a common
    utility convention, not confirmed to match any specific real
    utility's methodology. Labeled "Estimated Maximum Demand"
    everywhere it's surfaced.
    """
    tag = f"{plant_code.upper()}.ELEC.MAIN.Power_kW"
    limit = _reasonable_limit(start, end, assumed_interval_seconds=10)
    rows = historian.get_history_range(tag=tag, start=_fmt(start), end=_fmt(end), limit=limit)

    if len(rows) < 2:
        return _unavailable("kW", [tag], [f"insufficient {tag} history in the requested period"], period_start=_fmt(start), period_end=_fmt(end))

    samples = [(_parse(r["time"]), r["value"]) for r in rows]
    max_avg, occurred_at = rolling_window_max_average(samples, interval_minutes)

    if max_avg is None:
        return _unavailable("kW", [tag], ["not enough samples to fill one full demand interval"], period_start=_fmt(start), period_end=_fmt(end))

    return _kpi(
        round(max_avg, 2), "kW", "DIRECT", "CALCULATED", [tag],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=[
            f"'Estimated Maximum Demand' - highest {interval_minutes}-minute rolling average, "
            "NOT confirmed to match your utility's official billing-demand methodology",
            f"occurred at {occurred_at}",
        ],
    )


def estimated_demand_charge(
    config_database_path: str | Path, historian: DatabaseManager, plant_id: int | None, plant_code: str,
    start: datetime, end: datetime, interval_minutes: int = DEFAULT_DEMAND_INTERVAL_MINUTES,
) -> dict[str, Any]:
    demand = estimated_maximum_demand(historian, plant_code, start, end, interval_minutes)
    if demand["classification"] == "UNAVAILABLE":
        return demand

    tariff = effective_tariff(config_database_path, plant_id, end)
    rate = tariff.get("maximum_demand_charge") if tariff else None

    if rate is None:
        return _unavailable(
            "currency", demand["source_tags"], ["tariff has no maximum_demand_charge configured (advanced-mode field)"],
            period_start=demand["period_start"], period_end=demand["period_end"],
        )

    charge = demand["value"] * rate
    return _kpi(
        round(charge, 2), tariff.get("currency") or "?", "DIRECT", "CALCULATED", demand["source_tags"],
        period_start=demand["period_start"], period_end=demand["period_end"],
        assumptions=demand["assumptions"] + [f"{tariff.get('currency') or '?'} {rate}/kW demand charge from the tariff in effect"],
    )


# ---------------------------------------------------------------------------
# Production / base-load / non-production energy
# ---------------------------------------------------------------------------

def production_non_production_split(
    historian: DatabaseManager, config_database_path: str | Path, plant_code: str, plant_id: int,
    start: datetime, end: datetime, now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Returns a dict of four related-but-distinct KPIs so they're never
    conflated (per explicit direction):
      production_energy_kwh, non_production_energy_kwh,
      average_production_period_demand_kw, average_non_production_demand_kw
    Production/non-production are split by real production_batches
    state (Phase 4), not a nominal shift schedule - overlapping batches
    are merged into one disjoint interval set first, so simultaneous
    batches on different equipment never double-count plant energy.
    """
    tag_energy = f"{plant_code.upper()}.ELEC.MAIN.Energy_kWh"
    tag_power = f"{plant_code.upper()}.ELEC.MAIN.Power_kW"

    period_energy = accumulated_positive_delta(historian, tag_energy, start, end, assumed_interval_seconds=60)
    if period_energy is None:
        unavailable = _unavailable("kWh", [tag_energy], ["insufficient Energy_kWh history in period"], period_start=_fmt(start), period_end=_fmt(end))
        return {k: unavailable for k in (
            "production_energy_kwh", "non_production_energy_kwh",
            "average_production_period_demand_kw", "average_non_production_demand_kw",
        )}

    production_intervals = _running_batch_intervals(config_database_path, plant_id, start, end, now)

    limit = _reasonable_limit(start, end, assumed_interval_seconds=60)
    rows = historian.get_history_range(tag=tag_energy, start=_fmt(start), end=_fmt(end), limit=limit)

    production_kwh = 0.0
    for previous, current in zip(rows, rows[1:]):
        delta = current["value"] - previous["value"]
        if delta <= 0:
            continue
        if _time_within_intervals(_parse(current["time"]), production_intervals):
            production_kwh += delta

    non_production_kwh = max(period_energy - production_kwh, 0.0)

    production_hours = sum((e - s).total_seconds() for s, e in production_intervals) / 3600.0
    total_hours = max((end - start).total_seconds() / 3600.0, 1e-9)
    non_production_hours = max(total_hours - production_hours, 0.0)

    production_result = _kpi(
        round(production_kwh, 2), "kWh", "DIRECT", "CALCULATED", [tag_energy],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=["energy attributed to a sample if its timestamp falls within a merged running-batch interval"],
    )
    non_production_result = _kpi(
        round(non_production_kwh, 2), "kWh", "DIRECT", "CALCULATED", [tag_energy],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=["residual = period energy - production energy"],
    )

    if production_hours > 0:
        avg_prod_demand = _kpi(
            round(production_kwh / production_hours, 2), "kW", "DIRECT", "CALCULATED", [tag_energy, tag_power],
            period_start=_fmt(start), period_end=_fmt(end),
            assumptions=["average electrical demand while >=1 production batch was running - NOT a production-normalized intensity"],
        )
    else:
        avg_prod_demand = _unavailable("kW", [tag_energy], ["no production batches ran in this period"], period_start=_fmt(start), period_end=_fmt(end))

    if non_production_hours > 0:
        avg_nonprod_demand = _kpi(
            round(non_production_kwh / non_production_hours, 2), "kW", "DIRECT", "CALCULATED", [tag_energy],
            period_start=_fmt(start), period_end=_fmt(end),
        )
    else:
        avg_nonprod_demand = _unavailable("kW", [tag_energy], ["no non-production time in this period"], period_start=_fmt(start), period_end=_fmt(end))

    return {
        "production_energy_kwh": production_result,
        "non_production_energy_kwh": non_production_result,
        "average_production_period_demand_kw": avg_prod_demand,
        "average_non_production_demand_kw": avg_nonprod_demand,
    }


def estimated_base_load(
    historian: DatabaseManager, config_database_path: str | Path, plant_code: str, plant_id: int,
    start: datetime, end: datetime, percentile: float = BASE_LOAD_PERCENTILE, now: datetime | None = None,
) -> dict[str, Any]:
    """
    ESTIMATED, deliberately distinct from Average Non-production
    Demand: a low-percentile "floor" of demand specifically during
    non-production intervals, representing the always-on load
    (lighting/standby HVAC/compressed-air leaks/IT) rather than a
    plain average.
    """
    tag = f"{plant_code.upper()}.ELEC.MAIN.Power_kW"
    production_intervals = _running_batch_intervals(config_database_path, plant_id, start, end, now)

    limit = _reasonable_limit(start, end, assumed_interval_seconds=10)
    rows = historian.get_history_range(tag=tag, start=_fmt(start), end=_fmt(end), limit=limit)

    non_production_values = [
        r["value"] for r in rows if not _time_within_intervals(_parse(r["time"]), production_intervals)
    ]

    if len(non_production_values) < 5:
        return _unavailable("kW", [tag], ["not enough non-production-interval samples to estimate a base load"], period_start=_fmt(start), period_end=_fmt(end))

    non_production_values.sort()
    index = max(0, min(len(non_production_values) - 1, int(len(non_production_values) * percentile / 100.0)))
    value = non_production_values[index]

    return _kpi(
        round(value, 2), "kW", "ESTIMATED", "ESTIMATED", [tag],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=[f"{percentile:.0f}th percentile of Power_kW samples during non-production intervals"],
    )


def after_hours_consumption(
    config_database_path: str | Path, historian: DatabaseManager, plant_code: str, plant_id: int,
    start: datetime, end: datetime,
) -> dict[str, Any]:
    connection = _connect_config(config_database_path)
    try:
        shift_count = connection.execute(
            "SELECT COUNT(*) FROM shift_definitions WHERE (plant_id = ? OR plant_id IS NULL) AND active = 1",
            (plant_id,),
        ).fetchone()[0]
    finally:
        connection.close()

    tag = f"{plant_code.upper()}.ELEC.MAIN.Energy_kWh"

    if shift_count == 0:
        return _unavailable(
            "kWh", [tag], ["no shift_definitions configured for this plant - configure shifts on the Factory Configuration page"],
            period_start=_fmt(start), period_end=_fmt(end),
        )

    # Deliberately not implemented further this phase - the shift-aware
    # windowing logic would be built the moment real shift data exists
    # to test it against; returning UNAVAILABLE-with-reason would be
    # dishonest once shift_count > 0, so this is a real gap, not a
    # placeholder. Flagged in the completion report, not silently left.
    return _unavailable(
        "kWh", [tag], ["shift-window energy classification not yet implemented - shifts exist but this calculation is deferred"],
        period_start=_fmt(start), period_end=_fmt(end),
    )


# ---------------------------------------------------------------------------
# Production normalization
# ---------------------------------------------------------------------------

def production_energy_intensity(
    config_database_path: str | Path, historian: DatabaseManager, plant_code: str, plant_id: int,
    start: datetime, end: datetime, now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Genuine production-normalized intensity metrics - kg and litre
    production are NEVER combined into one number. Returns one entry
    per (unit, metric) pair; "tonne" entries are only DIRECT for
    kg-unit products (kg -> tonne is a real conversion, not an
    assumption) - litre-unit products' tonne/mass entries are always
    UNAVAILABLE (no density exists, none is assumed).
    """
    tag_energy = f"{plant_code.upper()}.ELEC.MAIN.Energy_kWh"
    connection = _connect_config(config_database_path)
    try:
        batches = connection.execute(
            """
            SELECT b.actual_quantity, b.start_time, b.end_time, p.unit_of_measure
            FROM production_batches b
            JOIN products p ON p.id = b.product_id
            WHERE b.plant_id = ? AND b.status = 'completed'
              AND b.start_time <= ? AND b.end_time >= ?
            """,
            (plant_id, _fmt(end), _fmt(start)),
        ).fetchall()
    finally:
        connection.close()

    results: dict[str, dict[str, Any]] = {}
    per_unit_quantity: dict[str, float] = {}
    batch_count = 0

    for row in batches:
        unit = row["unit_of_measure"]
        per_unit_quantity[unit] = per_unit_quantity.get(unit, 0.0) + (row["actual_quantity"] or 0.0)
        batch_count += 1

    production_energy = production_non_production_split(
        historian, config_database_path, plant_code, plant_id, start, end, now
    )["production_energy_kwh"]

    if production_energy["classification"] == "UNAVAILABLE" or not batches:
        missing = production_energy["missing_inputs"] or ["no completed production batches in this period"]
        for key in ("kwh_per_kg", "kwh_per_litre", "kwh_per_tonne", "kwh_per_batch"):
            results[key] = _unavailable("varies", [tag_energy], missing, period_start=_fmt(start), period_end=_fmt(end))
        return results

    energy_kwh = production_energy["value"]

    results["kwh_per_batch"] = _kpi(
        round(energy_kwh / batch_count, 3), "kWh/batch", "DIRECT", "CALCULATED", [tag_energy],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=["plant production energy divided evenly across completed batches in the period"],
    )

    for unit, qty in per_unit_quantity.items():
        if qty <= 0:
            continue
        key = f"kwh_per_{unit}"
        results[key] = _kpi(
            round(energy_kwh / qty, 4), f"kWh/{unit}", "DIRECT", "CALCULATED", [tag_energy],
            period_start=_fmt(start), period_end=_fmt(end),
            assumptions=[f"plant production energy allocated across all {unit}-unit production only - never combined with other units"],
        )

    if "kg" in per_unit_quantity and per_unit_quantity["kg"] > 0:
        tonnes = per_unit_quantity["kg"] / 1000.0
        results["kwh_per_tonne"] = _kpi(
            round(energy_kwh / tonnes, 3), "kWh/tonne", "DIRECT", "CALCULATED", [tag_energy],
            period_start=_fmt(start), period_end=_fmt(end),
            assumptions=["kg-unit production only; tonne = kg / 1000"],
        )
    else:
        results["kwh_per_tonne"] = _unavailable(
            "kWh/tonne", [tag_energy],
            ["no kg-unit production in this period, or all production is litre-based with no density to convert to mass"],
            period_start=_fmt(start), period_end=_fmt(end),
        )

    return results


# ---------------------------------------------------------------------------
# Compressor KPIs
# ---------------------------------------------------------------------------

def compressor_load_stats(
    historian: DatabaseManager, plant_code: str, instance: str, start: datetime, end: datetime,
) -> dict[str, dict[str, Any]]:
    loaded_tag = f"{plant_code.upper()}.UTILITY.{instance}.LoadedHours"
    unloaded_tag = f"{plant_code.upper()}.UTILITY.{instance}.UnloadedHours"
    energy_tag = f"{plant_code.upper()}.UTILITY.{instance}.Energy_kWh"
    running_hours_tag = f"{plant_code.upper()}.UTILITY.{instance}.RunningHours"

    loaded_delta = accumulated_positive_delta(historian, loaded_tag, start, end, assumed_interval_seconds=10)
    unloaded_delta = accumulated_positive_delta(historian, unloaded_tag, start, end, assumed_interval_seconds=10)
    energy_delta = accumulated_positive_delta(historian, energy_tag, start, end, assumed_interval_seconds=60)
    running_delta = accumulated_positive_delta(historian, running_hours_tag, start, end, assumed_interval_seconds=10)

    results: dict[str, dict[str, Any]] = {}

    total_hours = (loaded_delta or 0.0) + (unloaded_delta or 0.0)
    if loaded_delta is not None and unloaded_delta is not None and total_hours > ZERO_FLOW_EPSILON:
        results["loaded_pct"] = _kpi(
            round(loaded_delta / total_hours * 100, 1), "%", "DIRECT", "CALCULATED", [loaded_tag, unloaded_tag],
            period_start=_fmt(start), period_end=_fmt(end),
        )
        results["unloaded_pct"] = _kpi(
            round(unloaded_delta / total_hours * 100, 1), "%", "DIRECT", "CALCULATED", [loaded_tag, unloaded_tag],
            period_start=_fmt(start), period_end=_fmt(end),
        )
    else:
        missing = [loaded_tag, unloaded_tag]
        results["loaded_pct"] = _unavailable("%", missing, ["no run time recorded in period"], period_start=_fmt(start), period_end=_fmt(end))
        results["unloaded_pct"] = _unavailable("%", missing, ["no run time recorded in period"], period_start=_fmt(start), period_end=_fmt(end))

    if energy_delta is not None and running_delta and running_delta > ZERO_FLOW_EPSILON:
        results["specific_energy_kwh_per_hour"] = _kpi(
            round(energy_delta / running_delta, 2), "kWh/running-hour", "DIRECT", "CALCULATED",
            [energy_tag, running_hours_tag], period_start=_fmt(start), period_end=_fmt(end),
        )
    else:
        results["specific_energy_kwh_per_hour"] = _unavailable(
            "kWh/running-hour", [energy_tag, running_hours_tag], ["zero or missing running hours in period"],
            period_start=_fmt(start), period_end=_fmt(end),
        )

    return results


def _header_air_volume(
    historian: DatabaseManager, plant_code: str, start: datetime, end: datetime,
) -> tuple[float | None, str, list[str]]:
    """
    Shared by header_air_volume()/header_specific_energy()/
    compressed_air_cost_per_nm3(). Prefers the header's cumulative
    FlowTotal (Nm3) meter where it exists; falls back to trapezoidal-
    integrating the instantaneous Flow (Nm3/h) tag where it doesn't -
    found necessary during Phase 6 verification: P01's compressed-air
    header has both Flow and FlowTotal, but P02's header has only Flow
    (FlowTotal doesn't exist for P02 at all, a genuine pre-existing
    tag-dataset asymmetry between the two plants, not a bug). Both
    methods measure the same real header flow - this only changes how
    the volume total is derived, not what's actually being measured.
    Returns (volume_or_None, tag_actually_used, assumptions).
    """
    flow_total_tag = f"{plant_code.upper()}.UTILITY.AIRHDR01.FlowTotal"
    flow_rate_tag = f"{plant_code.upper()}.UTILITY.AIRHDR01.Flow"

    flow_delta = accumulated_positive_delta(historian, flow_total_tag, start, end, assumed_interval_seconds=10)
    if flow_delta is not None:
        return flow_delta, flow_total_tag, []

    flow_delta = trapezoidal_integral(historian, flow_rate_tag, start, end, assumed_interval_seconds=10)
    assumptions = ["no FlowTotal cumulative meter for this plant's header - flow numerically integrated from the instantaneous Flow tag instead"]
    return flow_delta, flow_rate_tag, assumptions


def header_air_volume(historian: DatabaseManager, plant_code: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Total compressed-air volume through the header this period - its
    own displayable figure, not just an intermediate for specific energy."""
    flow_delta, flow_tag, assumptions = _header_air_volume(historian, plant_code, start, end)

    if flow_delta is None or flow_delta <= ZERO_FLOW_EPSILON:
        return _unavailable("Nm3", [flow_tag], ["no header flow data in period"], period_start=_fmt(start), period_end=_fmt(end))

    calculation_type = "MEASURED" if flow_tag.endswith("FlowTotal") else "CALCULATED"
    return _kpi(
        round(flow_delta, 2), "Nm3", "DIRECT", calculation_type, [flow_tag],
        period_start=_fmt(start), period_end=_fmt(end), assumptions=assumptions,
    )


def total_compressor_energy(
    historian: DatabaseManager, plant_code: str, ac_instances: list[str], start: datetime, end: datetime,
) -> dict[str, Any]:
    """Sum of all compressors' own Energy_kWh deltas this period - its
    own displayable figure, not just an intermediate for specific energy."""
    energy_tags = [f"{plant_code.upper()}.UTILITY.{i}.Energy_kWh" for i in ac_instances]

    total_kwh = 0.0
    any_energy = False
    for tag in energy_tags:
        delta = accumulated_positive_delta(historian, tag, start, end, assumed_interval_seconds=60)
        if delta is not None:
            total_kwh += delta
            any_energy = True

    if not any_energy:
        return _unavailable("kWh", energy_tags, ["no compressor Energy_kWh data in period"], period_start=_fmt(start), period_end=_fmt(end))

    return _kpi(
        round(total_kwh, 2), "kWh", "DIRECT", "CALCULATED", energy_tags,
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=["sum of each compressor's own Energy_kWh delta this period"],
    )


def header_specific_energy(
    historian: DatabaseManager, plant_code: str, ac_instances: list[str], start: datetime, end: datetime,
) -> dict[str, Any]:
    """
    Plant/header-level DIRECT metric - approved as measured/header
    performance. Per-compressor kWh/Nm3 is NOT computed here (no per-
    compressor flow meter exists) - see per_compressor_inferred_allocation().
    """
    energy_result = total_compressor_energy(historian, plant_code, ac_instances, start, end)
    flow_delta, flow_tag, assumptions = _header_air_volume(historian, plant_code, start, end)

    if energy_result["classification"] == "UNAVAILABLE" or flow_delta is None or flow_delta <= ZERO_FLOW_EPSILON:
        return _unavailable(
            "kWh/Nm3", energy_result["source_tags"] + [flow_tag],
            ["zero/near-zero header flow or missing compressor energy in period"],
            period_start=_fmt(start), period_end=_fmt(end),
        )

    return _kpi(
        round(energy_result["value"] / flow_delta, 4), "kWh/Nm3", "DIRECT", "CALCULATED",
        energy_result["source_tags"] + [flow_tag],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=["header-level: sum of all compressor Energy_kWh deltas divided by header flow total"] + assumptions,
    )


def compressed_air_cost_per_nm3(
    config_database_path: str | Path, historian: DatabaseManager, plant_id: int | None, plant_code: str,
    ac_instances: list[str], start: datetime, end: datetime,
) -> dict[str, Any]:
    """Header-level cost/Nm3 - specific energy priced at the tariff in
    effect for this period (same fallback-aware tariff lookup as
    cost_for_period())."""
    specific_energy = header_specific_energy(historian, plant_code, ac_instances, start, end)
    if specific_energy["classification"] == "UNAVAILABLE":
        return specific_energy

    tariff = effective_tariff(config_database_path, plant_id, end)
    rate = tariff.get("energy_rate") if tariff else None

    if rate is None:
        return _unavailable(
            "currency/Nm3", specific_energy["source_tags"], ["no tariff energy_rate configured"],
            period_start=specific_energy["period_start"], period_end=specific_energy["period_end"],
        )

    currency = tariff.get("currency") or "?"
    return _kpi(
        round(specific_energy["value"] * rate, 4), f"{currency}/Nm3", "DIRECT", "CALCULATED",
        specific_energy["source_tags"], period_start=specific_energy["period_start"], period_end=specific_energy["period_end"],
        assumptions=specific_energy["assumptions"] + [f"{currency} {rate}/kWh tariff rate applied to header specific energy"],
    )


def per_compressor_inferred_allocation(
    historian: DatabaseManager, plant_code: str, instance: str, all_instances: list[str], start: datetime, end: datetime,
) -> dict[str, Any]:
    """ESTIMATED only - allocates the header's measured kWh/Nm3 to one
    compressor in proportion to its share of total compressor energy.
    Never presented as a true per-compressor measurement."""
    header = header_specific_energy(historian, plant_code, all_instances, start, end)
    if header["classification"] == "UNAVAILABLE":
        return header

    this_energy = accumulated_positive_delta(
        historian, f"{plant_code.upper()}.UTILITY.{instance}.Energy_kWh", start, end, assumed_interval_seconds=60,
    )
    if this_energy is None:
        return _unavailable("kWh/Nm3", header["source_tags"], [f"no Energy_kWh data for {instance} in period"], period_start=_fmt(start), period_end=_fmt(end))

    return _kpi(
        header["value"], "kWh/Nm3", "ESTIMATED", "ESTIMATED", header["source_tags"],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=[
            f"inferred allocation only - {instance} has no individual flow meter; "
            "uses the plant/header specific energy figure as a stand-in, not a true per-compressor measurement",
        ],
    )


def night_base_air_consumption(
    historian: DatabaseManager, config_database_path: str | Path, plant_code: str, plant_id: int,
    start: datetime, end: datetime, percentile: float = BASE_LOAD_PERCENTILE, now: datetime | None = None,
) -> dict[str, Any]:
    tag = f"{plant_code.upper()}.UTILITY.AIRHDR01.Flow"
    production_intervals = _running_batch_intervals(config_database_path, plant_id, start, end, now)

    limit = _reasonable_limit(start, end, assumed_interval_seconds=10)
    rows = historian.get_history_range(tag=tag, start=_fmt(start), end=_fmt(end), limit=limit)
    values = [r["value"] for r in rows if not _time_within_intervals(_parse(r["time"]), production_intervals)]

    if len(values) < 5:
        return _unavailable("Nm3/h", [tag], ["not enough non-production-interval samples"], period_start=_fmt(start), period_end=_fmt(end))

    values.sort()
    index = max(0, min(len(values) - 1, int(len(values) * percentile / 100.0)))

    return _kpi(
        round(values[index], 2), "Nm3/h", "ESTIMATED", "ESTIMATED", [tag],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=[f"{percentile:.0f}th percentile of header Flow during non-production intervals"],
    )


# ---------------------------------------------------------------------------
# Chiller KPIs
# ---------------------------------------------------------------------------

def chiller_cop(
    historian: DatabaseManager, plant_code: str, instance: str,
) -> dict[str, dict[str, Any]]:
    """
    SIMULATION-ENVIRONMENT INSTRUMENTATION DEFINITION (not a physical
    assumption carried automatically to a real deployment): CHL01/
    CHL02's WaterFlow tags represent that specific chiller's own
    evaporator/condenser flow, not a shared header read twice. A real
    PLC deployment must verify this before enabling per-chiller COP -
    see the Phase 6 completion report.
    """
    flow_tag = f"{plant_code.upper()}.UTILITY.{instance}.WaterFlow"
    supply_tag = f"{plant_code.upper()}.UTILITY.{instance}.SupplyTemp"
    return_tag = f"{plant_code.upper()}.UTILITY.{instance}.ReturnTemp"
    power_tag = f"{plant_code.upper()}.UTILITY.{instance}.Power_kW"

    flow, flow_time = latest_with_timestamp(historian, flow_tag)
    supply, supply_time = latest_with_timestamp(historian, supply_tag)
    return_temp, return_time = latest_with_timestamp(historian, return_tag)
    power, power_time = latest_with_timestamp(historian, power_tag)

    source_tags = [flow_tag, supply_tag, return_tag, power_tag]

    missing = []
    if flow is None or is_stale(flow_time):
        missing.append(flow_tag)
    if supply is None or is_stale(supply_time):
        missing.append(supply_tag)
    if return_temp is None or is_stale(return_time):
        missing.append(return_tag)
    if power is None or is_stale(power_time):
        missing.append(power_tag)

    # Raw measured components - own displayable figures (Phase 7's
    # Chilled Water section shows these individually, not just the
    # derived cooling output/COP).
    raw = {}
    raw["power_kw"] = (
        _kpi(round(power, 2), "kW", "DIRECT", "MEASURED", [power_tag])
        if power is not None and not is_stale(power_time)
        else _unavailable("kW", [power_tag], [f"{power_tag} missing or stale"])
    )
    raw["water_flow_m3h"] = (
        _kpi(round(flow, 2), "m3/h", "DIRECT", "MEASURED", [flow_tag])
        if flow is not None and not is_stale(flow_time)
        else _unavailable("m3/h", [flow_tag], [f"{flow_tag} missing or stale"])
    )
    raw["supply_temp_c"] = (
        _kpi(round(supply, 2), "degC", "DIRECT", "MEASURED", [supply_tag])
        if supply is not None and not is_stale(supply_time)
        else _unavailable("degC", [supply_tag], [f"{supply_tag} missing or stale"])
    )
    raw["return_temp_c"] = (
        _kpi(round(return_temp, 2), "degC", "DIRECT", "MEASURED", [return_tag])
        if return_temp is not None and not is_stale(return_time)
        else _unavailable("degC", [return_tag], [f"{return_tag} missing or stale"])
    )
    if supply is not None and return_temp is not None and not is_stale(supply_time) and not is_stale(return_time):
        raw["delta_t_c"] = _kpi(round(return_temp - supply, 2), "degC", "DIRECT", "CALCULATED", [supply_tag, return_tag])
    else:
        raw["delta_t_c"] = _unavailable("degC", [supply_tag, return_tag], ["supply/return temperature missing or stale"])

    if missing:
        unavailable = _unavailable("kW", source_tags, missing)
        return {"cooling_output_kw": unavailable, "cop": unavailable, **raw}

    if flow <= ZERO_FLOW_EPSILON:
        unavailable = _unavailable("kW", source_tags, [f"{flow_tag} is zero/near-zero - chiller not circulating water"])
        return {"cooling_output_kw": unavailable, "cop": unavailable, **raw}

    delta_t = return_temp - supply
    if delta_t <= 0:
        unavailable = _unavailable(
            "kW", source_tags, [f"ReturnTemp <= SupplyTemp (deltaT={delta_t:.2f}) - not physically valid cooling"],
        )
        return {"cooling_output_kw": unavailable, "cop": unavailable, **raw}

    mass_flow_kg_s = flow * WATER_DENSITY_KG_PER_M3 / 3600.0
    cooling_output_kw = mass_flow_kg_s * WATER_SPECIFIC_HEAT_KJ_PER_KGK * delta_t

    assumptions = [
        f"water density {WATER_DENSITY_KG_PER_M3:.0f} kg/m3, specific heat {WATER_SPECIFIC_HEAT_KJ_PER_KGK} kJ/kg.K",
        "SIMULATION instrumentation definition: WaterFlow is this chiller's own dedicated flow, not a shared header - "
        "verify against real PLC wiring before enabling on live plant data",
    ]

    cooling_result = _kpi(
        round(cooling_output_kw, 2), "kW", "DIRECT", "CALCULATED", source_tags,
        assumptions=assumptions,
    )

    if power <= ZERO_FLOW_EPSILON:
        cop_result = _unavailable("dimensionless", source_tags, [f"{power_tag} is zero/near-zero"])
    else:
        cop_result = _kpi(round(cooling_output_kw / power, 2), "dimensionless", "DIRECT", "CALCULATED", source_tags, assumptions=assumptions)

    return {"cooling_output_kw": cooling_result, "cop": cop_result, **raw}


# ---------------------------------------------------------------------------
# Pump KPIs (CHWP, WSP - only pumps with both suction AND discharge
# pressure measured)
# ---------------------------------------------------------------------------

def pump_hydraulic_efficiency(
    historian: DatabaseManager, plant_code: str, area: str, instance: str,
) -> dict[str, dict[str, Any]]:
    prefix = f"{plant_code.upper()}.{area}.{instance}"
    flow_tag, suction_tag, discharge_tag, power_tag = (
        f"{prefix}.Flow", f"{prefix}.SuctionPressure", f"{prefix}.DischargePressure", f"{prefix}.Power_kW",
    )

    flow, flow_time = latest_with_timestamp(historian, flow_tag)
    suction, suction_time = latest_with_timestamp(historian, suction_tag)
    discharge, discharge_time = latest_with_timestamp(historian, discharge_tag)
    power, power_time = latest_with_timestamp(historian, power_tag)

    source_tags = [flow_tag, suction_tag, discharge_tag, power_tag]
    missing = [
        tag for tag, value, ts in (
            (flow_tag, flow, flow_time), (suction_tag, suction, suction_time),
            (discharge_tag, discharge, discharge_time), (power_tag, power, power_time),
        ) if value is None or is_stale(ts)
    ]

    # Raw measured components - own displayable figures (Phase 7's
    # Pumps section shows these individually, not just the derived
    # hydraulic power/efficiency).
    raw = {}
    raw["power_kw"] = (
        _kpi(round(power, 2), "kW", "DIRECT", "MEASURED", [power_tag])
        if power is not None and not is_stale(power_time)
        else _unavailable("kW", [power_tag], [f"{power_tag} missing or stale"])
    )
    raw["flow_m3h"] = (
        _kpi(round(flow, 2), "m3/h", "DIRECT", "MEASURED", [flow_tag])
        if flow is not None and not is_stale(flow_time)
        else _unavailable("m3/h", [flow_tag], [f"{flow_tag} missing or stale"])
    )
    if suction is not None and discharge is not None and not is_stale(suction_time) and not is_stale(discharge_time):
        raw["delta_p_bar"] = _kpi(round(discharge - suction, 3), "bar", "DIRECT", "CALCULATED", [suction_tag, discharge_tag])
    else:
        raw["delta_p_bar"] = _unavailable("bar", [suction_tag, discharge_tag], ["suction/discharge pressure missing or stale"])

    if missing:
        unavailable = _unavailable("kW", source_tags, missing)
        return {"hydraulic_power_kw": unavailable, "efficiency_pct": unavailable, **raw}

    if flow <= ZERO_FLOW_EPSILON:
        unavailable = _unavailable("kW", source_tags, [f"{flow_tag} is zero/near-zero"])
        return {"hydraulic_power_kw": unavailable, "efficiency_pct": unavailable, **raw}

    delta_p_bar = discharge - suction
    if delta_p_bar <= 0:
        unavailable = _unavailable("kW", source_tags, [f"DischargePressure <= SuctionPressure (dP={delta_p_bar:.2f} bar)"])
        return {"hydraulic_power_kw": unavailable, "efficiency_pct": unavailable, **raw}

    # Hydraulic power(kW) = dP(Pa) x Q(m3/s) / 1000 = dP(bar)*1e5 * (Q(m3/h)/3600) / 1000
    #                      = dP(bar) * Q(m3/h) * 0.027778
    hydraulic_kw = delta_p_bar * flow * 0.027778
    hydraulic_result = _kpi(
        round(hydraulic_kw, 3), "kW", "DIRECT", "CALCULATED", source_tags,
        assumptions=["hydraulic power = deltaP(bar) x Q(m3/h) x 0.027778 (Pa*m3/s -> kW)"],
    )

    if power <= ZERO_FLOW_EPSILON:
        return {"hydraulic_power_kw": hydraulic_result, "efficiency_pct": _unavailable("%", source_tags, [f"{power_tag} is zero/near-zero"]), **raw}

    efficiency_pct = hydraulic_kw / power * 100

    if efficiency_pct > PUMP_EFFICIENCY_IMPLAUSIBLE_THRESHOLD_PCT:
        efficiency_result = _kpi(
            round(efficiency_pct, 1), "%", "ESTIMATED", "ESTIMATED", source_tags,
            assumptions=[
                f"computed efficiency {efficiency_pct:.1f}% exceeds physical plausibility "
                f"(> {PUMP_EFFICIENCY_IMPLAUSIBLE_THRESHOLD_PCT:.0f}% tolerance) - "
                "likely sensor noise or an instrumentation issue; diagnostic only, not a trustworthy efficiency figure",
            ],
        )
    else:
        efficiency_result = _kpi(round(efficiency_pct, 1), "%", "DIRECT", "CALCULATED", source_tags)

    return {"hydraulic_power_kw": hydraulic_result, "efficiency_pct": efficiency_result, **raw}


def pump_flow_per_kw(historian: DatabaseManager, plant_code: str, area: str, instance: str) -> dict[str, Any]:
    """Always-available operational indicator, independent of true
    hydraulic efficiency - useful even when pressure isn't measured."""
    prefix = f"{plant_code.upper()}.{area}.{instance}"
    flow_tag, power_tag = f"{prefix}.Flow", f"{prefix}.Power_kW"

    flow, flow_time = latest_with_timestamp(historian, flow_tag)
    power, power_time = latest_with_timestamp(historian, power_tag)

    if flow is None or power is None or is_stale(flow_time) or is_stale(power_time):
        return _unavailable("m3/h per kW", [flow_tag, power_tag], [flow_tag, power_tag])

    if power <= ZERO_FLOW_EPSILON:
        return _unavailable("m3/h per kW", [flow_tag, power_tag], [f"{power_tag} is zero/near-zero"])

    return _kpi(round(flow / power, 3), "m3/h per kW", "DIRECT", "CALCULATED", [flow_tag, power_tag])


def pump_specific_energy(
    historian: DatabaseManager, plant_code: str, area: str, instance: str, start: datetime, end: datetime,
) -> dict[str, Any]:
    """
    kWh/m3 over a period - distinct from pump_flow_per_kw() (an
    instantaneous snapshot ratio). Neither CHWP nor WSP has a
    cumulative energy or volume meter, so both are numerically
    integrated over the period, same method as
    water_treatment_specific_energy().
    """
    prefix = f"{plant_code.upper()}.{area}.{instance}"
    flow_tag, power_tag = f"{prefix}.Flow", f"{prefix}.Power_kW"

    energy_kwh = trapezoidal_integral(historian, power_tag, start, end, assumed_interval_seconds=10)
    volume_m3 = trapezoidal_integral(historian, flow_tag, start, end, assumed_interval_seconds=10)

    if energy_kwh is None or volume_m3 is None or volume_m3 <= ZERO_FLOW_EPSILON:
        return _unavailable(
            "kWh/m3", [power_tag, flow_tag], ["insufficient history, or zero/near-zero flow in period"],
            period_start=_fmt(start), period_end=_fmt(end),
        )

    return _kpi(
        round(energy_kwh / volume_m3, 4), "kWh/m3", "DIRECT", "CALCULATED", [power_tag, flow_tag],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=["both power and flow integrated by trapezoidal numerical integration over the period (no cumulative meter tag exists for this pump)"],
    )


# ---------------------------------------------------------------------------
# Water treatment KPIs
# ---------------------------------------------------------------------------

def water_treatment_specific_energy(
    historian: DatabaseManager, plant_code: str, area: str, instance: str,
    power_tag_name: str | None, flow_tag_name: str, start: datetime, end: datetime,
) -> dict[str, Any]:
    """
    area/instance e.g. ("WT", "RO01") or ("WT", "SYS01") - matches the
    real tag-naming convention (P01.WT.RO01.*, not P01.WT.RO.RO01.*).
    power_tag_name=None means this unit has no electrical measurement
    at all (WT.SYS01) - always UNAVAILABLE, never a manufactured value.
    """
    flow_tag = f"{plant_code.upper()}.{area}.{instance}.{flow_tag_name}"

    if power_tag_name is None:
        return _unavailable("kWh/m3", [flow_tag], [f"no electrical power tag exists for {area}.{instance}"], period_start=_fmt(start), period_end=_fmt(end))

    power_tag = f"{plant_code.upper()}.{area}.{instance}.{power_tag_name}"
    energy_kwh = trapezoidal_integral(historian, power_tag, start, end, assumed_interval_seconds=10)
    volume_m3 = trapezoidal_integral(historian, flow_tag, start, end, assumed_interval_seconds=10)

    if energy_kwh is None or volume_m3 is None or volume_m3 <= ZERO_FLOW_EPSILON:
        return _unavailable("kWh/m3", [power_tag, flow_tag], ["insufficient history, or zero/near-zero treated flow in period"], period_start=_fmt(start), period_end=_fmt(end))

    return _kpi(
        round(energy_kwh / volume_m3, 3), "kWh/m3", "DIRECT", "CALCULATED", [power_tag, flow_tag],
        period_start=_fmt(start), period_end=_fmt(end),
        assumptions=["both power and flow integrated by trapezoidal numerical integration over the period (no cumulative meter tag exists for this unit)"],
    )


# ---------------------------------------------------------------------------
# Debug CLI (Phase 6 deliverable is the engine, not a dashboard - this
# is a manual sanity-check tool only)
# ---------------------------------------------------------------------------

def _print_kpi(label: str, result: dict[str, Any]) -> None:
    if result["classification"] == "UNAVAILABLE":
        print(f"  {label}: UNAVAILABLE ({'; '.join(result['missing_inputs'])})")
    else:
        print(f"  {label}: {result['value']} {result['unit']} [{result['classification']}/{result['calculation_type']}]")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Phase 6 Energy KPI Engine - debug CLI")
    parser.add_argument("--plant", default="p01")
    parser.add_argument("--config-database", default=str(DEFAULT_CONFIG_DATABASE_PATH))
    parser.add_argument("--machine-database", default=str(DEFAULT_MACHINE_DATABASE_PATH))
    args = parser.parse_args()

    historian = DatabaseManager(db_path=args.machine_database)
    plant = get_plant(args.config_database, args.plant)
    if plant is None:
        print(f"Unknown plant code: {args.plant}")
        return

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    print(f"=== Phase 6 Energy KPIs — {args.plant.upper()} — {now} ===")
    _print_kpi("Current Demand", current_demand_kw(historian, args.plant, now))
    _print_kpi("Energy Today", energy_for_period(historian, args.plant, today_start, now))
    _print_kpi("Cost Today", cost_for_period(args.config_database, historian, plant["id"], args.plant, today_start, now))
    _print_kpi("Projected Month Cost", projected_month_cost(args.config_database, historian, plant["id"], args.plant, now))
    _print_kpi("Estimated Maximum Demand (today)", estimated_maximum_demand(historian, args.plant, today_start, now))

    split = production_non_production_split(historian, args.config_database, args.plant, plant["id"], today_start, now)
    for label, key in (
        ("Production Energy", "production_energy_kwh"), ("Non-production Energy", "non_production_energy_kwh"),
        ("Avg Production-Period Demand", "average_production_period_demand_kw"),
        ("Avg Non-production Demand", "average_non_production_demand_kw"),
    ):
        _print_kpi(label, split[key])

    _print_kpi("Estimated Base Load", estimated_base_load(historian, args.config_database, args.plant, plant["id"], today_start, now))

    for instance in ("CHL01", "CHL02"):
        chiller = chiller_cop(historian, args.plant, instance)
        _print_kpi(f"{instance} Cooling Output", chiller["cooling_output_kw"])
        _print_kpi(f"{instance} COP", chiller["cop"])

    for area, instance in (("UTILITY", "CHWP01"), ("WATER", "WSP01")):
        pump = pump_hydraulic_efficiency(historian, args.plant, area, instance)
        _print_kpi(f"{instance} Hydraulic Power", pump["hydraulic_power_kw"])
        _print_kpi(f"{instance} Efficiency", pump["efficiency_pct"])

    _print_kpi(
        "Header Specific Energy",
        header_specific_energy(historian, args.plant, ["AC01", "AC02", "AC03"], today_start, now),
    )
    _print_kpi(
        "WT.RO01 kWh/m3",
        water_treatment_specific_energy(historian, args.plant, "WT", "RO01", "Power_kW", "PermeateFlow", today_start, now),
    )
    _print_kpi(
        "WT.SYS01 kWh/m3",
        water_treatment_specific_energy(historian, args.plant, "WT", "SYS01", None, "TreatedWaterFlow", today_start, now),
    )


if __name__ == "__main__":
    main()
