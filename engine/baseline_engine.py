from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from database.database import DatabaseManager
from engine.baseline_targets import BaselineTarget, discover_targets
from engine.energy_kpi_engine import TIME_FORMAT, WATER_DENSITY_KG_PER_M3, WATER_SPECIFIC_HEAT_KJ_PER_KGK

"""
Phase 8 - Baseline Engine. Deterministic/statistical only (Rule 4 -
no LLM). Answers exactly one question per target: "what would
normally be expected under comparable conditions, and how trustworthy
is that expectation?" - never a judgment about whether the current
value IS anomalous (Phase 9's job).

Pipeline per target (see compute_baseline_for_context() for the
orchestration):
  1. Fetch raw historian series for the target's own signal AND every
     context-dimension source signal, bounded to [MODERN_DATA_BOUNDARY, now]
     (item 14 - never reaches the pre-2026-08-09 tag dataset or the
     tiny 2026-07-25/26 legacy timestamp-format sliver).
  2. Exclude fault/abnormal intervals, reconstructed from the tag's own
     configured thresholds applied directly to raw values (item 8/9) -
     NOT from simulator internals (Phase 5's fault state is in-memory/
     per-process and was never meant to be depended on - this also
     makes the design work against a real plant with no "simulator" at
     all). machine_events is used only as a fast index into where to
     look, not as the source of truth for interval boundaries (it only
     records fault STARTS, confirmed via reading app/event_monitor.py -
     the "return to normal" transition writes nothing to the historian).
  3. Downsample into time buckets (item 3) - a representative median
     per bucket, not every raw 10s sample, so highly-correlated
     adjacent readings are never counted as that many independent
     operating observations.
  4. Context-match with progressive dimension dropping (items 9, 11):
     start with every context dimension for the target's equipment
     type, drop the lowest-priority one whenever coverage is
     insufficient, until either coverage is met or all dimensions are
     dropped (the ungrouped case = Level C). This single mechanism
     implements the A/B/C fallback hierarchy without three separate
     hand-coded paths, and naturally implements the production
     same-product -> category -> operating-state fallback too (item 10)
     since product context dimensions are simply first in the
     drop-priority order for production equipment.
  5. Classify into unavailable / bootstrap / mature+confidence (item 5)
     from representative sample count + distinct days + diversity -
     never from deviation magnitude, never from raw row count alone.
  6. Robust statistics: median (expected_value) + a percentile band
     (expected_low/high, default P10-P90 - see
     DEFAULT_NORMAL_RANGE_PERCENTILES, the one place this is defined)
     + MAD (variability).
  7. Both a reference (slow, long-window) and recent (fast, short-
     window) baseline are computed independently (item 2) - Phase 8
     exposes the gap between them, never interprets it.
"""

# ---------------------------------------------------------------------------
# Central, documented, overridable constants - referenced everywhere,
# never re-hardcoded (item 4, item 17 test requirement).
# ---------------------------------------------------------------------------

DEFAULT_NORMAL_RANGE_PERCENTILES = (10, 90)

# Item 14 - explicit modern-data boundary. This dev environment's live
# tag dataset went active 2026-08-09; a small 967-row legacy sliver
# with an incompatible ISO-'T' timestamp format exists from
# 2026-07-25/26 (~2 weeks earlier, confirmed via direct query - see
# FACTORY_AI_DEVELOPMENT_STATUS.md's Phase 8 section). A real
# deployment should replace this constant with its own actual
# historian-start boundary rather than depend on this hardcoded date
# forever - kept as one named constant specifically so that swap is a
# one-line change, not a scattered rewrite.
MODERN_DATA_BOUNDARY = datetime(2026, 8, 9, 0, 0, 0)

# Item 5/6 - single-pass status/confidence classification. A target
# only ever needs enough representative samples to compute a
# defensible median/range before it returns SOME value - "bootstrap"
# means exactly that: a real, usable, honestly-labeled provisional
# figure, not a placeholder.
MIN_REPRESENTATIVE_SAMPLES_FOR_ANY_BASELINE = 5
BOOTSTRAP_MIN_DISTINCT_DAYS = 3
BOOTSTRAP_MIN_REPRESENTATIVE_SAMPLES = 15
MEDIUM_MIN_DISTINCT_DAYS = 4
MEDIUM_MIN_REPRESENTATIVE_SAMPLES = 25
MEDIUM_MIN_DIVERSITY_DIMENSIONS = 2
HIGH_MIN_DISTINCT_DAYS = 7
HIGH_MIN_REPRESENTATIVE_SAMPLES = 50
HIGH_MIN_DIVERSITY_DIMENSIONS = 2

# Item 8/9 - small buffer around a reconstructed threshold excursion,
# large enough to catch the immediate ramp-in/ramp-out, small enough to
# never eat legitimate adjacent data (a merely-uncommon-but-valid
# operating condition must never be excluded).
FAULT_EXCLUSION_BUFFER_MINUTES = 2

HOUR_BLOCK_HOURS = 4  # 6 blocks/day - "reasonable hour blocks", item 11

# Item 2 - dual reference/recent windows. Once enough history exists
# they become disjoint (reference = day -60..-8, recent = day -7..now);
# while history is young both collapse to the same full available
# range (see _reference_recent_windows()).
DEFAULT_REFERENCE_WINDOW_DAYS_MAX = 60
DEFAULT_RECENT_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# Time bucketing - pure naive-datetime arithmetic on the historian's
# own local-time strings (never epoch/UTC conversion), so local
# day/hour boundaries are always respected regardless of the system's
# other UTC-timestamped tables (item 17 - see the timezone-mismatch
# finding in FACTORY_AI_DEVELOPMENT_STATUS.md's Phase 8 section).
# ---------------------------------------------------------------------------

def _bucket_start(t: datetime, bucket_minutes: int) -> datetime:
    total_minutes = t.hour * 60 + t.minute
    floored = (total_minutes // bucket_minutes) * bucket_minutes
    return t.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def _hour_bucket(t: datetime) -> str:
    block = (t.hour // HOUR_BLOCK_HOURS) * HOUR_BLOCK_HOURS
    return f"{block:02d}-{block + HOUR_BLOCK_HOURS:02d}"


def _day_type(t: datetime) -> str:
    return "weekend" if t.weekday() >= 5 else "weekday"


def _tercile_bucket(value: float, low: float, high: float) -> str:
    if high <= low:
        return "medium"
    frac = (value - low) / (high - low)
    if frac < 1 / 3:
        return "low"
    if frac < 2 / 3:
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# Interval merge helpers (generic algorithm, not domain logic - kept
# local rather than imported from simulator.plant_context, which
# solves a different problem with the same shape)
# ---------------------------------------------------------------------------

def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
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


def _excluded(t: datetime, intervals: list[tuple[datetime, datetime]]) -> bool:
    return any(start <= t <= end for start, end in intervals)


# ---------------------------------------------------------------------------
# Fault/abnormal-interval reconstruction (item 8, 9)
# ---------------------------------------------------------------------------

def _load_thresholds(config_database_path: str | Path, tag_name: str) -> dict[str, float] | None:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT low_alarm, low_warning, high_warning, high_alarm FROM thresholds WHERE tag_name = ?",
            (tag_name,),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()


def _abnormal_intervals_from_thresholds(
    rows: list[dict[str, Any]], thresholds: dict[str, float] | None,
) -> list[tuple[datetime, datetime]]:
    """
    Reconstructs abnormal (threshold-breaching) intervals directly from
    raw values + the tag's own configured limits - the general,
    real-plant-compatible method (item 8). Deliberately only flags a
    genuine excursion past a configured warning/alarm limit - a merely
    uncommon-but-in-limits value is never excluded (item 9).
    """
    if not thresholds or not rows:
        return []

    low_alarm, low_warning = thresholds.get("low_alarm"), thresholds.get("low_warning")
    high_warning, high_alarm = thresholds.get("high_warning"), thresholds.get("high_alarm")

    buffer = timedelta(minutes=FAULT_EXCLUSION_BUFFER_MINUTES)
    intervals = []
    for row in rows:
        value = row["value"]
        breached = (
            (low_alarm is not None and value <= low_alarm)
            or (low_warning is not None and value <= low_warning)
            or (high_warning is not None and value >= high_warning)
            or (high_alarm is not None and value >= high_alarm)
        )
        if breached:
            t = datetime.strptime(row["time"], TIME_FORMAT)
            intervals.append((t - buffer, t + buffer))

    return _merge_intervals(intervals)


def _maintenance_intervals(config_database_path: str | Path, instance_key: str, buffer_minutes: int = 30) -> list[tuple[datetime, datetime]]:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        parts = instance_key.split(".")
        plant, instance_code = parts[0].lower(), parts[-1].lower()
        equipment = connection.execute(
            "SELECT id FROM equipment WHERE name LIKE ? AND name LIKE ?",
            (f"{plant}_%", f"%_{instance_code}"),
        ).fetchone()
        if equipment is None:
            return []

        buffer = timedelta(minutes=buffer_minutes)
        intervals = []
        for table in ("service_log", "maintenance_log"):
            try:
                rows = connection.execute(f"SELECT performed_at FROM {table} WHERE equipment_id = ?", (equipment["id"],)).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                try:
                    t = datetime.strptime(row["performed_at"], TIME_FORMAT)
                except ValueError:
                    continue
                intervals.append((t - buffer, t + buffer))

        return _merge_intervals(intervals)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Context source resolution - which tag/table backs each context
# dimension, per equipment type. A dimension whose source tag equals
# the target's own tag is dropped automatically (self-reference guard,
# generalizing item 10's circular-baseline rule to every raw target,
# not just derived KPIs).
# ---------------------------------------------------------------------------

def _context_source_tag(target: BaselineTarget, dimension: str) -> str | None:
    instance = target.instance_key
    if dimension == "load_bucket":
        if target.equipment_type == "chiller":
            return f"{instance}.LoadPct"
        if target.equipment_type in ("chilled_water_pump", "water_supply_pump"):
            return f"{instance}.Frequency"
    if dimension == "ambient_bucket":
        plant = instance.split(".")[0]
        return f"{plant}.ENV01.Temperature"
    if dimension == "loaded_state":
        return f"{instance}.LoadStatus"
    return None  # hour_bucket/day_type/production_state/product_code/product_category/running_state - not tag-sourced


# ---------------------------------------------------------------------------
# Historian access
# ---------------------------------------------------------------------------

def _fetch_bounded(historian: DatabaseManager, tag_name: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    start = max(start, MODERN_DATA_BOUNDARY)
    if end <= start:
        return []
    span_seconds = (end - start).total_seconds()
    limit = max(200, int(span_seconds / 10 * 1.5) + 200)
    return historian.get_history_range(tag=tag_name, start=start.strftime(TIME_FORMAT), end=end.strftime(TIME_FORMAT), limit=limit)


def _downsample(rows: list[dict[str, Any]], bucket_minutes: int) -> dict[datetime, float]:
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for row in rows:
        t = datetime.strptime(row["time"], TIME_FORMAT)
        buckets[_bucket_start(t, bucket_minutes)].append(row["value"])
    return {bucket: statistics.median(values) for bucket, values in buckets.items()}


# ---------------------------------------------------------------------------
# Derived-target calculators - each takes ALREADY-DOWNSAMPLED, bucket-
# aligned raw series (one value per bucket per required raw signal) and
# produces a bucket->value series for the derived target. Reuses
# Phase 6's own constants/formulas rather than re-deriving them.
# ---------------------------------------------------------------------------

def _derived_cop(buckets_by_signal: dict[str, dict[datetime, float]]) -> dict[datetime, float]:
    flow, supply, ret, power = (buckets_by_signal.get(s, {}) for s in ("WaterFlow", "SupplyTemp", "ReturnTemp", "Power_kW"))
    result = {}
    for bucket in flow.keys() & supply.keys() & ret.keys() & power.keys():
        f, s, r, p = flow[bucket], supply[bucket], ret[bucket], power[bucket]
        delta_t = r - s
        if f <= 0 or delta_t <= 0 or p <= 0:
            continue
        cooling_kw = (f * WATER_DENSITY_KG_PER_M3 / 3600.0) * WATER_SPECIFIC_HEAT_KJ_PER_KGK * delta_t
        result[bucket] = cooling_kw / p
    return result


def _derived_cooling_output_kw(buckets_by_signal: dict[str, dict[datetime, float]]) -> dict[datetime, float]:
    flow, supply, ret = (buckets_by_signal.get(s, {}) for s in ("WaterFlow", "SupplyTemp", "ReturnTemp"))
    result = {}
    for bucket in flow.keys() & supply.keys() & ret.keys():
        f, s, r = flow[bucket], supply[bucket], ret[bucket]
        delta_t = r - s
        if f <= 0 or delta_t <= 0:
            continue
        result[bucket] = (f * WATER_DENSITY_KG_PER_M3 / 3600.0) * WATER_SPECIFIC_HEAT_KJ_PER_KGK * delta_t
    return result


def _derived_flow_per_kw(buckets_by_signal: dict[str, dict[datetime, float]]) -> dict[datetime, float]:
    flow, power = buckets_by_signal.get("Flow", {}), buckets_by_signal.get("Power_kW", {})
    return {b: flow[b] / power[b] for b in flow.keys() & power.keys() if power[b] > 0}


def _derived_delta_p_bar(buckets_by_signal: dict[str, dict[datetime, float]]) -> dict[datetime, float]:
    suction, discharge = buckets_by_signal.get("SuctionPressure", {}), buckets_by_signal.get("DischargePressure", {})
    return {b: discharge[b] - suction[b] for b in suction.keys() & discharge.keys()}


def _derived_non_production_demand(
    buckets_by_signal: dict[str, dict[datetime, float]], production_state_by_bucket: dict[datetime, str],
) -> dict[datetime, float]:
    """
    Phase 9 support (item 3 of the Phase 9 plan) - a plain FILTER of
    ELEC.MAIN.Power_kW to buckets where the plant had no running
    production batch, not a new formula. Buckets during production are
    simply absent from the result, exactly like any other target with
    a gap in its history - never zero-filled.
    """
    power = buckets_by_signal.get("Power_kW", {})
    return {
        bucket: value for bucket, value in power.items()
        if production_state_by_bucket.get(bucket) == "non_production"
    }


def _derived_compressed_air_specific_energy(
    buckets_by_signal: dict[str, dict[datetime, float]], ac_energy_series: dict[str, dict[datetime, float]],
) -> dict[datetime, float]:
    """Header-level only (item 1/§6) - sums downsampled compressor
    Power_kW across all AC instances as a load proxy for specific
    energy per bucket, divided by header Flow. Approximates true
    Energy_kWh/FlowTotal cumulative specific energy (Phase 6) at
    bucket granularity for baseline-matching purposes."""
    flow = buckets_by_signal.get("Flow", {})
    result = {}
    for bucket, flow_value in flow.items():
        if flow_value <= 0:
            continue
        total_power = sum(series.get(bucket, 0.0) for series in ac_energy_series.values())
        if total_power <= 0:
            continue
        result[bucket] = total_power / flow_value
    return result


DERIVED_REQUIRED_RAW_SIGNALS = {
    "cop": ("WaterFlow", "SupplyTemp", "ReturnTemp", "Power_kW"),
    "cooling_output_kw": ("WaterFlow", "SupplyTemp", "ReturnTemp"),
    "flow_per_kw": ("Flow", "Power_kW"),
    "delta_p_bar": ("SuctionPressure", "DischargePressure"),
    "compressed_air_specific_energy": ("Flow",),
    "non_production_demand_kw": ("Power_kW",),
}


# ---------------------------------------------------------------------------
# Config-database context lookups (production batches for product/
# category/running-state context)
# ---------------------------------------------------------------------------

def _plant_production_state_at(
    config_database_path: str | Path, plant_code: str, buckets: list[datetime],
) -> dict[datetime, str]:
    """Whether ANY production batch was running for this plant at each
    bucket - the plant-level "production_state" context dimension used
    by plant_energy/air_header targets."""
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        plant = connection.execute("SELECT id FROM plants WHERE code = ?", (plant_code.lower(),)).fetchone()
        if plant is None:
            return {b: "unknown" for b in buckets}

        rows = connection.execute(
            "SELECT start_time, end_time FROM production_batches WHERE plant_id = ?", (plant["id"],),
        ).fetchall()
    finally:
        connection.close()

    intervals = []
    for row in rows:
        start = datetime.strptime(row["start_time"], TIME_FORMAT)
        end = datetime.strptime(row["end_time"], TIME_FORMAT) if row["end_time"] else datetime.max
        intervals.append((start, end))
    merged = _merge_intervals(intervals)

    return {b: ("production" if _excluded(b, merged) else "non_production") for b in buckets}


def _production_context_at(
    config_database_path: str | Path, instance_key: str, buckets: list[datetime],
) -> dict[datetime, dict[str, str]]:
    """For production-linked instances only: product_code/product_category/
    running_state per bucket, from real production_batches rows (Phase 4/5) -
    genuine context, not inferred."""
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        parts = instance_key.split(".")
        plant, instance_code = parts[0].lower(), parts[-1].lower()
        equipment = connection.execute(
            "SELECT id FROM equipment WHERE name LIKE ? AND name LIKE ?",
            (f"{plant}_%", f"%_{instance_code}"),
        ).fetchone()
        if equipment is None:
            return {}

        rows = connection.execute(
            """
            SELECT b.start_time, b.end_time, p.product_code, p.category_id, c.name AS category_name
            FROM production_batches b
            JOIN products p ON p.id = b.product_id
            LEFT JOIN product_categories c ON c.id = p.category_id
            WHERE b.equipment_id = ?
            """,
            (equipment["id"],),
        ).fetchall()
    finally:
        connection.close()

    batches = []
    for row in rows:
        start = datetime.strptime(row["start_time"], TIME_FORMAT)
        end = datetime.strptime(row["end_time"], TIME_FORMAT) if row["end_time"] else datetime.max
        batches.append((start, end, row["product_code"], row["category_name"] or "uncategorized"))

    result = {}
    for bucket in buckets:
        running = next(((s, e, pc, cat) for s, e, pc, cat in batches if s <= bucket <= e), None)
        if running:
            result[bucket] = {"product_code": running[2], "product_category": running[3], "running_state": "running"}
        else:
            result[bucket] = {"product_code": "idle", "product_category": "idle", "running_state": "idle"}

    return result


# ---------------------------------------------------------------------------
# Core per-window computation
# ---------------------------------------------------------------------------

def _reference_recent_windows(
    now: datetime, reference_window_days_max: int, recent_window_days: int,
) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """Item 2 - adaptive, non-overlapping once enough history exists;
    both windows collapse to the same full available range while
    history is young (documented, not hidden)."""
    available_start = MODERN_DATA_BOUNDARY
    available_days = (now - available_start).days

    if available_days <= recent_window_days:
        full = (available_start, now)
        return full, full

    reference_end = now - timedelta(days=recent_window_days)
    reference_start = max(available_start, now - timedelta(days=reference_window_days_max))
    recent_start = reference_end

    return (reference_start, reference_end), (recent_start, now)


# ---------------------------------------------------------------------------
# Coverage classification (items 5, 6) - single-pass, so "enough
# samples but poor temporal diversity" always resolves to exactly one
# outcome (bootstrap), never a contradiction between two independent
# gates.
# ---------------------------------------------------------------------------

def _classify(representative_sample_count: int, distinct_days: int, level: str, diversity_dimensions: int) -> tuple[str, str | None]:
    if representative_sample_count < MIN_REPRESENTATIVE_SAMPLES_FOR_ANY_BASELINE:
        return "unavailable", None

    if distinct_days < BOOTSTRAP_MIN_DISTINCT_DAYS or representative_sample_count < BOOTSTRAP_MIN_REPRESENTATIVE_SAMPLES:
        return "bootstrap", "Low"

    if (
        distinct_days >= HIGH_MIN_DISTINCT_DAYS and representative_sample_count >= HIGH_MIN_REPRESENTATIVE_SAMPLES
        and level == "A" and diversity_dimensions >= HIGH_MIN_DIVERSITY_DIMENSIONS
    ):
        return "mature", "High"

    if (
        distinct_days >= MEDIUM_MIN_DISTINCT_DAYS and representative_sample_count >= MEDIUM_MIN_REPRESENTATIVE_SAMPLES
        and diversity_dimensions >= MEDIUM_MIN_DIVERSITY_DIMENSIONS
    ):
        return "mature", "Medium"

    return "mature", "Low"


def _summarize(values: list[float], percentiles: tuple[int, int] = DEFAULT_NORMAL_RANGE_PERCENTILES) -> tuple[float, float, float, float]:
    """Returns (median, range_low, range_high, mad)."""
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations) * 1.4826  # consistency constant, std-equivalent scale
    ordered = sorted(values)
    low_pct, high_pct = percentiles

    def _percentile(data: list[float], pct: float) -> float:
        if len(data) == 1:
            return data[0]
        rank = (pct / 100) * (len(data) - 1)
        lower, upper = int(rank), min(int(rank) + 1, len(data) - 1)
        fraction = rank - lower
        return data[lower] + (data[upper] - data[lower]) * fraction

    return median, _percentile(ordered, low_pct), _percentile(ordered, high_pct), mad


def context_bucket_key(context_used: dict) -> str:
    """The exact encoding app/baseline_worker.py persists
    baseline_context_summary rows under, and engine/anomaly_engine.py
    must use identically to look them back up - kept in one place so
    the two can never drift apart."""
    if not context_used:
        return "__all__"
    return "|".join(f"{k}={v}" for k, v in sorted(context_used.items()))


def _distinct_days(buckets: list[datetime]) -> int:
    return len({b.date() for b in buckets})


# ---------------------------------------------------------------------------
# Context matching - generic (independently-combinable dimensions,
# dropped lowest-priority-first) for most equipment types; a dedicated
# hierarchical matcher for production equipment, since product_code/
# product_category/running_state are mutually-exclusive refinements of
# one concept, not independent AND-able dimensions (item 10).
# ---------------------------------------------------------------------------

def _match_generic(
    values_by_bucket: dict[datetime, float],
    context_by_bucket: dict[datetime, dict[str, str]],
    dimension_priority_low_to_high: tuple[str, ...],
    current_context: dict[str, str],
) -> tuple[list[float], list[datetime], tuple[str, ...], str]:
    dims = list(dimension_priority_low_to_high)

    while True:
        if dims:
            matched_buckets = [
                b for b, ctx in context_by_bucket.items()
                if b in values_by_bucket and all(ctx.get(d) == current_context.get(d) for d in dims)
            ]
        else:
            matched_buckets = [b for b in context_by_bucket if b in values_by_bucket]

        matched_values = [values_by_bucket[b] for b in matched_buckets]
        days = _distinct_days(matched_buckets)

        enough = len(matched_values) >= MIN_REPRESENTATIVE_SAMPLES_FOR_ANY_BASELINE
        if enough or not dims:
            level = "A" if dims == list(dimension_priority_low_to_high) and dims else ("B" if dims else "C")
            return matched_values, matched_buckets, tuple(dims), level

        dims = dims[1:]  # drop lowest-priority remaining dimension


def _match_production(
    values_by_bucket: dict[datetime, float],
    production_context_by_bucket: dict[datetime, dict[str, str]],
    current_context: dict[str, str],
) -> tuple[list[float], list[datetime], tuple[str, ...], str]:
    stages = (
        ("product_code", "A"),
        ("product_category", "B"),
    )

    for dimension, level in stages:
        target_value = current_context.get(dimension)
        if target_value is None or target_value == "idle":
            continue
        matched_buckets = [
            b for b, ctx in production_context_by_bucket.items()
            if b in values_by_bucket and ctx.get(dimension) == target_value
        ]
        if len(matched_buckets) >= MIN_REPRESENTATIVE_SAMPLES_FOR_ANY_BASELINE:
            return [values_by_bucket[b] for b in matched_buckets], matched_buckets, (dimension,), level

    # Level C - running_state only (any product), or every sample if
    # even that yields nothing.
    running_buckets = [
        b for b, ctx in production_context_by_bucket.items()
        if b in values_by_bucket and ctx.get("running_state") == current_context.get("running_state", "running")
    ]
    if len(running_buckets) >= MIN_REPRESENTATIVE_SAMPLES_FOR_ANY_BASELINE:
        return [values_by_bucket[b] for b in running_buckets], running_buckets, ("running_state",), "C"

    all_buckets = [b for b in production_context_by_bucket if b in values_by_bucket]
    return [values_by_bucket[b] for b in all_buckets], all_buckets, (), "C"


def _diversity_dimensions(context_by_bucket: dict[datetime, dict[str, str]], buckets: list[datetime], dims: tuple[str, ...]) -> int:
    count = 0
    for dim in dims:
        values = {context_by_bucket[b].get(dim) for b in buckets if b in context_by_bucket}
        if len(values) > 1:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Target series fetching (raw or derived, always clean + downsampled)
# ---------------------------------------------------------------------------

def _fetch_clean_downsampled(
    historian: DatabaseManager, config_database_path: str | Path, tag_name: str,
    start: datetime, end: datetime, bucket_minutes: int, exclude_faults: bool = True,
) -> tuple[dict[datetime, float], int]:
    """
    Returns (downsampled buckets, raw row count before downsampling) -
    the raw count feeds the result contract's raw_sample_count field
    (distinct from representative_sample_count, which counts
    downsampled buckets - item 3/6).

    exclude_faults=True (the default, used for everything in THIS
    module) excludes threshold-breaching samples before downsampling -
    correct for LEARNING what's normal. Phase 9's anomaly engine passes
    exclude_faults=False when it needs to see the actual current
    reading being compared against that learned baseline, including a
    genuine fault reading - excluding it there would make an active
    fault invisible to the comparison, defeating the point.
    """
    rows = _fetch_bounded(historian, tag_name, start, end)
    if exclude_faults:
        thresholds = _load_thresholds(config_database_path, tag_name)
        abnormal = _abnormal_intervals_from_thresholds(rows, thresholds)
        clean_rows = [r for r in rows if not _excluded(datetime.strptime(r["time"], TIME_FORMAT), abnormal)]
    else:
        clean_rows = rows
    return _downsample(clean_rows, bucket_minutes), len(rows)


def _fetch_target_series(
    target: BaselineTarget, historian: DatabaseManager, config_database_path: str | Path,
    start: datetime, end: datetime, exclude_faults: bool = True,
) -> tuple[dict[datetime, float], int]:
    """Returns (bucket->value series, total raw row count involved). See
    _fetch_clean_downsampled()'s docstring for exclude_faults."""
    if not target.is_derived:
        return _fetch_clean_downsampled(historian, config_database_path, target.tag_name, start, end, target.aggregation_minutes, exclude_faults)

    required = DERIVED_REQUIRED_RAW_SIGNALS.get(target.target_key, ())
    buckets_by_signal = {}
    total_raw = 0
    for signal in required:
        buckets, raw_count = _fetch_clean_downsampled(
            historian, config_database_path, f"{target.instance_key}.{signal}", start, end, target.aggregation_minutes, exclude_faults,
        )
        buckets_by_signal[signal] = buckets
        total_raw += raw_count

    if target.target_key == "cop":
        return _derived_cop(buckets_by_signal), total_raw
    if target.target_key == "cooling_output_kw":
        return _derived_cooling_output_kw(buckets_by_signal), total_raw
    if target.target_key == "flow_per_kw":
        return _derived_flow_per_kw(buckets_by_signal), total_raw
    if target.target_key == "delta_p_bar":
        return _derived_delta_p_bar(buckets_by_signal), total_raw
    if target.target_key == "compressed_air_specific_energy":
        plant_prefix = target.instance_key.rsplit(".", 1)[0]  # e.g. "P01.UTILITY"
        ac_series = {}
        for instance in ("AC01", "AC02", "AC03"):
            buckets, raw_count = _fetch_clean_downsampled(
                historian, config_database_path, f"{plant_prefix}.{instance}.Power_kW", start, end, target.aggregation_minutes, exclude_faults,
            )
            ac_series[instance] = buckets
            total_raw += raw_count
        return _derived_compressed_air_specific_energy(buckets_by_signal, ac_series), total_raw
    if target.target_key == "non_production_demand_kw":
        plant_code = target.instance_key.split(".")[0]
        buckets = sorted(buckets_by_signal.get("Power_kW", {}).keys())
        production_state_by_bucket = _plant_production_state_at(config_database_path, plant_code, buckets)
        return _derived_non_production_demand(buckets_by_signal, production_state_by_bucket), total_raw

    return {}, total_raw


# ---------------------------------------------------------------------------
# Context series building
# ---------------------------------------------------------------------------

def _fetch_context_series(
    target: BaselineTarget, historian: DatabaseManager, config_database_path: str | Path,
    start: datetime, end: datetime, target_buckets: list[datetime],
) -> dict[datetime, dict[str, str]]:
    is_production = target.equipment_type in ("production_process", "filling")

    if is_production:
        return _production_context_at(config_database_path, target.instance_key, target_buckets)

    tag_sourced_dims = [d for d in target.context_dimensions if _context_source_tag(target, d) is not None]
    # Self-reference guard: never use a context dimension derived from
    # the target's own tag (generalizes item 10 to every raw target).
    tag_sourced_dims = [d for d in tag_sourced_dims if _context_source_tag(target, d) != target.tag_name]

    raw_series: dict[str, dict[datetime, float]] = {}
    for dim in tag_sourced_dims:
        source_tag = _context_source_tag(target, dim)
        rows = _fetch_bounded(historian, source_tag, start, end)
        raw_series[dim] = _downsample(rows, target.aggregation_minutes)

    tercile_bounds: dict[str, tuple[float, float]] = {}
    for dim, series in raw_series.items():
        if dim in ("load_bucket", "ambient_bucket") and series:
            values = list(series.values())
            tercile_bounds[dim] = (min(values), max(values))

    production_state_by_bucket: dict[datetime, str] = {}
    if "production_state" in target.context_dimensions:
        plant_code = target.instance_key.split(".")[0]
        production_state_by_bucket = _plant_production_state_at(config_database_path, plant_code, target_buckets)

    context_by_bucket: dict[datetime, dict[str, str]] = {}
    for bucket in target_buckets:
        ctx: dict[str, str] = {}
        for dim in target.context_dimensions:
            if dim == "hour_bucket":
                ctx[dim] = _hour_bucket(bucket)
            elif dim == "day_type":
                ctx[dim] = _day_type(bucket)
            elif dim == "production_state":
                ctx[dim] = production_state_by_bucket.get(bucket, "unknown")
            elif dim == "loaded_state":
                value = raw_series.get(dim, {}).get(bucket)
                ctx[dim] = "loaded" if value else "unloaded" if value is not None else "unknown"
            elif dim in ("load_bucket", "ambient_bucket"):
                value = raw_series.get(dim, {}).get(bucket)
                if value is None or dim not in tercile_bounds:
                    ctx[dim] = "unknown"
                else:
                    low, high = tercile_bounds[dim]
                    ctx[dim] = _tercile_bucket(value, low, high)
        context_by_bucket[bucket] = ctx

    return context_by_bucket


def _current_context(
    target: BaselineTarget, historian: DatabaseManager, config_database_path: str | Path, now: datetime,
) -> dict[str, str]:
    """
    The context vector 'right now' - matched against historical buckets
    during Level A/B matching. Uses the latest available raw reading
    per context tag directly (DatabaseManager.get_latest()) rather than
    requiring a sample to fall inside "now"'s exact bucket grid slot -
    a slower-cadence context tag (e.g. ENV01.Temperature at 300s) can
    easily miss an exact 5-minute bucket window by design, which isn't
    the same thing as the reading being unavailable.
    """
    is_production = target.equipment_type in ("production_process", "filling")
    if is_production:
        result = _production_context_at(config_database_path, target.instance_key, [now])
        return result.get(now, {"product_code": "idle", "product_category": "idle", "running_state": "idle"})

    ctx: dict[str, str] = {}
    for dim in target.context_dimensions:
        if dim == "hour_bucket":
            ctx[dim] = _hour_bucket(now)
        elif dim == "day_type":
            ctx[dim] = _day_type(now)
        elif dim == "production_state":
            state = _plant_production_state_at(config_database_path, target.instance_key.split(".")[0], [now])
            ctx[dim] = state.get(now, "unknown")
        else:
            source_tag = _context_source_tag(target, dim)
            if source_tag is None or source_tag == target.tag_name:
                continue

            latest = historian.get_latest(source_tag)
            if latest is None:
                ctx[dim] = "unknown"
                continue
            value = latest["value"]

            if dim == "loaded_state":
                ctx[dim] = "loaded" if value else "unloaded"
            elif dim in ("load_bucket", "ambient_bucket"):
                bound_rows = _fetch_bounded(historian, source_tag, now - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS), now)
                if not bound_rows:
                    ctx[dim] = "unknown"
                else:
                    bound_values = [r["value"] for r in bound_rows]
                    ctx[dim] = _tercile_bucket(value, min(bound_values), max(bound_values))

    return ctx


# ---------------------------------------------------------------------------
# Single-window computation (one of reference/recent)
# ---------------------------------------------------------------------------

def _empty_window_result(window_start: datetime, window_end: datetime) -> dict[str, Any]:
    return {
        "status": "unavailable", "confidence": None, "level": "D",
        "median": None, "range_low": None, "range_high": None, "mad": None,
        "representative_sample_count": 0, "raw_sample_count": 0,
        "distinct_days": 0, "diversity_dimensions": 0,
        "context_used": {}, "missing_context": [],
        "history_start": window_start, "history_end": window_end,
        "matched_bucket_starts": [],
    }


def compute_window_baseline(
    target: BaselineTarget, historian: DatabaseManager, config_database_path: str | Path,
    window_start: datetime, window_end: datetime, current_context: dict[str, str],
) -> dict[str, Any]:
    target_series, raw_sample_count = _fetch_target_series(target, historian, config_database_path, window_start, window_end)

    if not target_series:
        return _empty_window_result(window_start, window_end)

    target_buckets = sorted(target_series.keys())
    is_production = target.equipment_type in ("production_process", "filling")

    if is_production:
        context_by_bucket = _production_context_at(config_database_path, target.instance_key, target_buckets)
        matched_values, matched_buckets, dims_used, level = _match_production(target_series, context_by_bucket, current_context)
    else:
        context_by_bucket = _fetch_context_series(target, historian, config_database_path, window_start, window_end, target_buckets)
        dims_priority = tuple(d for d in target.context_dimensions if _context_source_tag(target, d) != target.tag_name)
        matched_values, matched_buckets, dims_used, level = _match_generic(target_series, context_by_bucket, dims_priority, current_context)

    if len(matched_values) < MIN_REPRESENTATIVE_SAMPLES_FOR_ANY_BASELINE:
        result = _empty_window_result(window_start, window_end)
        result["raw_sample_count"] = raw_sample_count
        return result

    distinct_days = _distinct_days(matched_buckets)
    diversity = _diversity_dimensions(context_by_bucket, matched_buckets, dims_used)
    status, confidence = _classify(len(matched_values), distinct_days, level, diversity)

    if status == "unavailable":
        result = _empty_window_result(window_start, window_end)
        result["raw_sample_count"] = raw_sample_count
        result["representative_sample_count"] = len(matched_values)
        result["distinct_days"] = distinct_days
        return result

    median, range_low, range_high, mad = _summarize(matched_values)
    missing_context = [d for d in target.context_dimensions if d not in dims_used]

    return {
        "status": status, "confidence": confidence, "level": level,
        "median": median, "range_low": range_low, "range_high": range_high, "mad": mad,
        "representative_sample_count": len(matched_values), "raw_sample_count": raw_sample_count,
        "distinct_days": distinct_days, "diversity_dimensions": diversity,
        "context_used": {d: current_context.get(d) for d in dims_used}, "missing_context": missing_context,
        "history_start": window_start, "history_end": window_end,
        # Phase 11.3 (savings verification) needs to identify which
        # specific matched buckets contaminate with a maintenance
        # window, to exclude them from a financial calculation basis -
        # additive only, every existing field/semantic above is
        # unchanged. Sorted for deterministic ordering.
        "matched_bucket_starts": sorted(matched_buckets),
    }


# ---------------------------------------------------------------------------
# Public entry point - dual reference/recent baseline for one target
# ---------------------------------------------------------------------------

def compute_baseline(
    target: BaselineTarget, historian: DatabaseManager, config_database_path: str | Path,
    actual_value: float | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    current_context = _current_context(target, historian, config_database_path, now)

    reference_window, recent_window = _reference_recent_windows(
        now, target.reference_window_days_max, target.recent_window_days,
    )
    same_window = reference_window == recent_window

    recent_result = compute_window_baseline(target, historian, config_database_path, recent_window[0], recent_window[1], current_context)
    if same_window:
        reference_result = recent_result
    else:
        reference_result = compute_window_baseline(target, historian, config_database_path, reference_window[0], reference_window[1], current_context)

    # The "primary" result surfaced as expected_value/expected_low/high
    # is the recent baseline (the more operationally relevant one) -
    # reference is exposed alongside for drift comparison, per item 2,
    # never interpreted here (Phase 9's job).
    primary = recent_result

    drift_pct = None
    if reference_result["median"] is not None and recent_result["median"] is not None and reference_result["median"] != 0:
        drift_pct = (recent_result["median"] - reference_result["median"]) / reference_result["median"] * 100

    deviation_absolute = None
    deviation_percent = None
    if actual_value is not None and primary["median"] is not None:
        deviation_absolute = actual_value - primary["median"]
        if primary["median"] != 0:
            deviation_percent = deviation_absolute / primary["median"] * 100

    assumptions = [
        f"downsampled to {target.aggregation_minutes}-minute representative-median buckets before statistics "
        "(avoids treating adjacent high-frequency samples as independent observations)",
        f"normal range = P{DEFAULT_NORMAL_RANGE_PERCENTILES[0]}-P{DEFAULT_NORMAL_RANGE_PERCENTILES[1]} of matched comparable samples",
        "fault periods excluded via the tag's own configured thresholds applied to raw historian values, not simulator internals",
    ]
    if same_window:
        assumptions.append(
            f"reference and recent baselines use the SAME window - not enough history yet "
            f"(only {(now - MODERN_DATA_BOUNDARY).days} modern-data day(s) available) to split them"
        )

    method_parts = [f"Level {primary['level']}"]
    if primary["context_used"]:
        method_parts.append(", ".join(f"{k}={v}" for k, v in primary["context_used"].items()))
    method = " - ".join(method_parts)

    return {
        "plant_code": target.plant_code,
        "instance_key": target.instance_key,
        "target_key": target.target_key,
        "equipment_type": target.equipment_type,
        "actual_value": actual_value,
        "expected_value": primary["median"],
        "expected_low": primary["range_low"],
        "expected_high": primary["range_high"],
        "deviation_absolute": deviation_absolute,
        "deviation_percent": deviation_percent,
        "variability": primary["mad"],
        "method": method,
        "baseline_level": primary["level"],
        "baseline_status": primary["status"],
        "confidence": primary["confidence"],
        "sample_count": primary["representative_sample_count"],
        "raw_sample_count": primary["raw_sample_count"],
        "distinct_days": primary["distinct_days"],
        "context_used": primary["context_used"],
        "missing_context": primary["missing_context"],
        "assumptions": assumptions,
        "history_window": {"start": primary["history_start"].strftime(TIME_FORMAT) if primary["history_start"] else None,
                            "end": primary["history_end"].strftime(TIME_FORMAT) if primary["history_end"] else None},
        "source_tags": [target.tag_name] if target.tag_name else [f"{target.instance_key}.{s}" for s in DERIVED_REQUIRED_RAW_SIGNALS.get(target.target_key, ())],
        "reference_vs_recent": {
            "reference_expected_value": reference_result["median"],
            "reference_status": reference_result["status"],
            "reference_confidence": reference_result["confidence"],
            "reference_distinct_days": reference_result["distinct_days"],
            "recent_expected_value": recent_result["median"],
            "recent_status": recent_result["status"],
            "recent_confidence": recent_result["confidence"],
            "recent_distinct_days": recent_result["distinct_days"],
            "drift_percent": drift_pct,
            "windows_overlap": same_window,
        },
        "computed_at": now.strftime(TIME_FORMAT),
    }


# ---------------------------------------------------------------------------
# Debug CLI (Phase 8 deliverable is the engine, not a dashboard)
# ---------------------------------------------------------------------------

def _print_baseline(result: dict[str, Any]) -> None:
    label = f"{result['instance_key']} {result['target_key']}"
    if result["baseline_status"] == "unavailable":
        print(f"{label}: Baseline unavailable (sample_count={result['sample_count']}, distinct_days={result['distinct_days']})")
        return

    print(f"{label}")
    print(f"  Actual: {result['actual_value']}")
    print(f"  Expected: {result['expected_value']:.3f}   Range: {result['expected_low']:.3f} - {result['expected_high']:.3f}   MAD: {result['variability']:.3f}")
    if result["deviation_percent"] is not None:
        print(f"  Deviation: {result['deviation_percent']:+.1f}%")
    print(f"  Method: {result['method']}   Status: {result['baseline_status']}   Confidence: {result['confidence']}")
    print(f"  History: {result['sample_count']} representative samples ({result['raw_sample_count']} raw) across {result['distinct_days']} distinct day(s)")
    rvr = result["reference_vs_recent"]
    if rvr["windows_overlap"]:
        print(f"  Reference vs Recent: same window (insufficient history to split)")
    else:
        drift = f"{rvr['drift_percent']:+.1f}%" if rvr["drift_percent"] is not None else "n/a"
        print(f"  Reference: {rvr['reference_expected_value']} ({rvr['reference_status']}/{rvr['reference_confidence']})  "
              f"Recent: {rvr['recent_expected_value']} ({rvr['recent_status']}/{rvr['recent_confidence']})  Drift: {drift}")


def main() -> None:
    import argparse
    from config.environment import get_config_db_path, get_machine_db_path

    parser = argparse.ArgumentParser(description="Phase 8 Baseline Engine - debug CLI")
    parser.add_argument("--plant", default="p01")
    parser.add_argument("--equipment-type", default=None, help="filter to one equipment type, e.g. chiller")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--config-database", default=str(get_config_db_path()))
    parser.add_argument("--machine-database", default=str(get_machine_db_path()))
    args = parser.parse_args()

    historian = DatabaseManager(db_path=args.machine_database)
    targets = discover_targets(args.config_database, args.plant)
    if args.equipment_type:
        targets = [t for t in targets if t.equipment_type == args.equipment_type]

    print(f"=== Phase 8 Baselines — {args.plant.upper()} — {len(targets)} targets (showing up to {args.limit}) ===")
    for target in targets[: args.limit]:
        actual = None
        if not target.is_derived:
            row = historian.get_latest(target.tag_name)
            actual = row["value"] if row else None
        result = compute_baseline(target, historian, args.config_database, actual_value=actual)
        _print_baseline(result)


if __name__ == "__main__":
    main()
