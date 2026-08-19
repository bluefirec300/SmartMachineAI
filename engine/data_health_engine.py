from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from engine import health_evidence as hev
from engine.baseline_targets import EQUIPMENT_TYPE_PROFILES, PRODUCTION_PROCESS_AREA_CODES
from engine.data_health_targets import (
    CONTINUITY_GAP_INTERVAL_MULTIPLIER,
    CONTINUITY_LOOKBACK_HOURS,
    COMPONENT_WEIGHTS,
    DATA_HEALTH_MODEL_VERSION,
    FRESHNESS_FALLBACK_SECONDS,
    FRESHNESS_FRESH,
    FRESHNESS_INDETERMINATE,
    FRESHNESS_INDETERMINATE_SCORE,
    FRESHNESS_INTERVAL_MULTIPLIER,
    FRESHNESS_STALE,
    FROZEN_INELIGIBLE_DATA_TYPES,
    FROZEN_MIN_CONSECUTIVE_SAMPLES,
    FROZEN_MIN_WINDOW_INTERVAL_MULTIPLIER,
    FUTURE_TIMESTAMP_TOLERANCE_SECONDS,
    STATUS_UNAVAILABLE,
    TAG_DISPLAY_FRESH,
    TAG_DISPLAY_FROZEN_CANDIDATE,
    TAG_DISPLAY_INDETERMINATE,
    TAG_DISPLAY_INVALID,
    TAG_DISPLAY_MISSING,
    TAG_DISPLAY_STALE,
    VALIDITY_INVALID,
    VALIDITY_VALID,
    confidence_status_for_score,
)

"""
Phase 16.1 - Data Health core engine. Pure, deterministic, on-demand
calculation over already-persisted plc_data/tags/equipment rows - no LLM,
no writes, no new persistence table (mirrors Phase 13's own architecture
decision for the identical reason: cheap to recompute from already-
indexed evidence).

Data Health is completely separate from Equipment Health
(engine/health_engine.py) - this module never reads equipment_health_
snapshots and health_engine.py never reads this module's output. See
engine/data_health_targets.py's module docstring for the full rationale.

Every historian read here is BOUNDED - either a single indexed "latest
row for this tag" lookup, or a time-windowed (CONTINUITY_LOOKBACK_HOURS/
FROZEN_MIN_WINDOW_INTERVAL_MULTIPLIER-bounded), LIMIT-capped scan. No
function in this module ever scans plc_data without a WHERE tag = ? and
either LIMIT 1 or a bounded time window.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Result dataclasses (item L - presentation-independent; no Streamlit
# formatting anywhere in this module).
# ---------------------------------------------------------------------------

@dataclass
class TagDataHealth:
    tag_name: str
    available: bool
    last_value: Any = None
    last_time: str | None = None
    seconds_since_update: float | None = None

    # Independent evidence dimensions (item B) - never derived from
    # display_state, and display_state is never derived from anything
    # except these already-independently-computed dimensions.
    freshness: str | None = None       # FRESH | STALE | INDETERMINATE | None (unavailable)
    validity: str | None = None        # VALID | INVALID | None (unavailable)
    invalid_reason: str | None = None
    continuity_applicable: bool = False
    has_gap: bool = False
    gap_seconds: float | None = None
    gap_start: str | None = None
    gap_end: str | None = None
    frozen_candidate: bool = False
    frozen_reason: str | None = None
    future_timestamp: bool = False
    non_monotonic: bool = False

    log_on_change: bool = False
    logging_interval_seconds: float | None = None

    # Human-readable, mutually-exclusive summary - presentation only,
    # never fed back into any component score (item B/L).
    display_state: str = TAG_DISPLAY_MISSING


@dataclass
class EquipmentDataHealth:
    instance_key: str
    equipment_id: int | None
    equipment_type: str | None
    confidence_score: float | None
    confidence_status: str
    component_scores: dict[str, float | None]
    component_applicability: dict[str, bool]
    required_tag_count: int
    available_tag_count: int
    fresh_tag_count: int
    stale_tags: list[str] = field(default_factory=list)
    missing_tags: list[str] = field(default_factory=list)
    invalid_tags: list[str] = field(default_factory=list)
    indeterminate_freshness_tags: list[str] = field(default_factory=list)
    frozen_candidates: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    timestamp_issues: list[dict[str, Any]] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)
    tags: list[TagDataHealth] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    model_version: str = DATA_HEALTH_MODEL_VERSION
    computed_at: str = ""


# ---------------------------------------------------------------------------
# Bounded, indexed reads only.
# ---------------------------------------------------------------------------

def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value[:19], TIME_FORMAT)


def _latest_reading(machine_database_path: str | Path, tag_name: str) -> dict[str, Any] | None:
    connection = _connect(machine_database_path)
    try:
        row = connection.execute(
            "SELECT time, value FROM plc_data WHERE tag = ? ORDER BY time DESC, id DESC LIMIT 1",
            (tag_name,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def _recent_readings(
    machine_database_path: str | Path, tag_name: str, window_start: datetime, limit: int = 500,
) -> list[dict[str, Any]]:
    connection = _connect(machine_database_path)
    try:
        rows = connection.execute(
            "SELECT time, value FROM plc_data WHERE tag = ? AND time >= ? ORDER BY time ASC, id ASC LIMIT ?",
            (tag_name, window_start.strftime(TIME_FORMAT), limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def _recent_readings_by_id_order(
    machine_database_path: str | Path, tag_name: str, window_start: datetime, limit: int = 500,
) -> list[dict[str, Any]]:
    """Ordered by id (write/insertion order), NOT time - needed
    specifically for non-monotonic-timestamp detection, which is
    meaningless if the rows are pre-sorted by the very column being
    checked."""
    connection = _connect(machine_database_path)
    try:
        rows = connection.execute(
            "SELECT id, time FROM plc_data WHERE tag = ? AND time >= ? ORDER BY id ASC LIMIT ?",
            (tag_name, window_start.strftime(TIME_FORMAT), limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Required-tag resolution (item D).
# ---------------------------------------------------------------------------

def resolve_required_tags(
    config_database_path: str | Path, instance_key: str, equipment_type: str | None,
) -> tuple[str, ...] | None:
    """
    Concrete, enabled tag_names required by engine.baseline_targets'
    EQUIPMENT_TYPE_PROFILES.raw_targets for this equipment instance - the
    tags that genuinely feed the existing Phase 8-14 analytics pipeline.

    Returns None if no profile exists at all for this equipment_type (an
    explicit "no applicable registry" case - never a silently-empty
    tuple standing in for "not applicable"). Returns an empty tuple if a
    profile exists but none of its expected tags currently exist/are
    enabled for this specific instance (a distinct, also-disclosed case).
    """
    if equipment_type is None:
        return None

    profile = EQUIPMENT_TYPE_PROFILES.get(equipment_type)
    if profile is None:
        return None

    connection = _connect(config_database_path)
    try:
        tag_names: list[str] = []
        for suffix in profile.raw_targets:
            tag_name = f"{instance_key}.{suffix}"
            exists = connection.execute(
                "SELECT 1 FROM tags WHERE tag_name = ? AND enabled = 1", (tag_name,),
            ).fetchone()
            if exists:
                tag_names.append(tag_name)
        return tuple(tag_names)
    finally:
        connection.close()


def _area_codes_for_type(equipment_type: str, profile) -> tuple[str, ...]:
    if equipment_type == "production_process":
        return PRODUCTION_PROCESS_AREA_CODES
    return (profile.area_code,)


def _equipment_type_for_instance(instance_key: str) -> str | None:
    """
    Resolves the canonical, snake_case equipment_type key (e.g.
    "water_supply_pump") that engine.baseline_targets.EQUIPMENT_TYPE_PROFILES
    and engine.health_targets.HEALTH_FACTOR_REGISTRY both key on, given
    only an instance_key.

    Deliberately does NOT read equipment.equipment_type - confirmed live
    that column stores a human-readable label ("Water Supply Pump", "Air
    Compressor"), not the registries' snake_case keys, so a naive lookup
    there would silently never match anything (a real bug caught during
    this implementation's own live testing).

    Instead mirrors the exact reverse of
    engine.baseline_targets._instances_for_area_code()'s own token-
    matching logic (same area_code token counts, same trailing-digit
    stripping on the final token) so the two can never drift apart.
    """
    parts = instance_key.split(".")
    if len(parts) < 2:
        return None

    tail_parts = [p.upper() for p in parts[1:]]  # drop plant prefix

    for equipment_type, profile in EQUIPMENT_TYPE_PROFILES.items():
        for area_code in _area_codes_for_type(equipment_type, profile):
            area_tokens = area_code.upper().split(".")

            if len(tail_parts) != len(area_tokens):
                continue

            candidate_tokens = list(tail_parts)
            candidate_tokens[-1] = re.sub(r"\d+$", "", candidate_tokens[-1])

            if candidate_tokens == area_tokens:
                return equipment_type

    return None


def _tag_configs(config_database_path: str | Path, tag_names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if not tag_names:
        return {}
    connection = _connect(config_database_path)
    try:
        placeholders = ",".join("?" for _ in tag_names)
        rows = connection.execute(
            f"SELECT tag_name, logging_interval_seconds, log_on_change, data_type "
            f"FROM tags WHERE tag_name IN ({placeholders})",
            tag_names,
        ).fetchall()
        return {row["tag_name"]: dict(row) for row in rows}
    finally:
        connection.close()


def _current_source() -> dict[str, Any]:
    """
    System-wide CURRENT-STATE fact only (item H) - which driver is
    presently configured active. plc_data carries no per-row provenance
    column, and no live connection-health signal exists anywhere in this
    codebase today (confirmed by audit) - so this NEVER claims
    CONNECTED/HEALTHY CONNECTION/PLC ONLINE, only "this is what's
    currently configured."
    """
    try:
        from plc.driver_factory import resolve_driver_name
        driver_name = resolve_driver_name()
    except Exception:
        driver_name = None

    return {
        "configured_driver": driver_name,
        "note": (
            "Reflects the currently CONFIGURED data source only - not a "
            "live connection-health check (no such signal exists in this "
            "system yet)."
        ),
    }


# ---------------------------------------------------------------------------
# Per-tag evidence (item B - each dimension computed independently).
# ---------------------------------------------------------------------------

def _classify_validity(raw_value: Any) -> tuple[str, str | None]:
    """
    Objective invalidity only (item E) - None/NaN/+-inf/non-numeric.
    Deliberately NEVER reinterprets alarm/warning/target thresholds as a
    validity range: a process value can be abnormal/alarming and still
    be VALID DATA. No configured physical-validity-range concept exists
    anywhere in this project today (confirmed by audit), so no
    range-based invalidation is attempted - if one is added later, it
    must be a genuine, explicitly-configured validity range, never
    alarm/warning limits repurposed.
    """
    if raw_value is None:
        return VALIDITY_INVALID, "value is null"

    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return VALIDITY_INVALID, f"non-numeric/malformed value: {raw_value!r}"

    if math.isnan(value):
        return VALIDITY_INVALID, "value is NaN"

    if math.isinf(value):
        sign = "positive" if value > 0 else "negative"
        return VALIDITY_INVALID, f"value is {sign} infinity"

    return VALIDITY_VALID, None


def _classify_freshness(tag_config: dict[str, Any], seconds_since_update: float) -> str:
    """
    Item I. log_on_change tags: absence of a new row does not mean
    communication failure - freshness is reported INDETERMINATE rather
    than guessed STALE. Fixed-interval tags: STALE if the elapsed time
    since the last sample exceeds THIS TAG'S OWN configured
    logging_interval_seconds x FRESHNESS_INTERVAL_MULTIPLIER (never one
    universal threshold across tags configured for different cadences).
    """
    if bool(tag_config.get("log_on_change")):
        return FRESHNESS_INDETERMINATE

    interval = tag_config.get("logging_interval_seconds")
    threshold = float(interval) * FRESHNESS_INTERVAL_MULTIPLIER if interval else FRESHNESS_FALLBACK_SECONDS

    if seconds_since_update > threshold:
        return FRESHNESS_STALE

    return FRESHNESS_FRESH


def _detect_gap(
    machine_database_path: str | Path, tag_name: str, interval_seconds: float, now: datetime,
) -> dict[str, Any] | None:
    """
    Item J. Bounded lookback only (CONTINUITY_LOOKBACK_HOURS). Only
    called when continuity is already known to be applicable (fixed-
    interval, non-log_on_change tag) - see _evaluate_tag(). Considers the
    gap from the window start to the first reading, between consecutive
    readings, and from the last reading to `now` - a tag that stopped
    reporting partway through the window is caught the same way as one
    with an internal gap.
    """
    threshold_seconds = interval_seconds * CONTINUITY_GAP_INTERVAL_MULTIPLIER
    window_start = now - timedelta(hours=CONTINUITY_LOOKBACK_HOURS)

    readings = _recent_readings(machine_database_path, tag_name, window_start)

    boundary_points = (
        [window_start] + [_parse_time(r["time"]) for r in readings] + [now]
    )

    worst_gap_seconds = 0.0
    worst_start: datetime | None = None
    worst_end: datetime | None = None

    for earlier, later in zip(boundary_points, boundary_points[1:]):
        gap_seconds = (later - earlier).total_seconds()
        if gap_seconds > worst_gap_seconds:
            worst_gap_seconds, worst_start, worst_end = gap_seconds, earlier, later

    if worst_gap_seconds > threshold_seconds:
        return {
            "gap_seconds": round(worst_gap_seconds, 1),
            "gap_start": worst_start.strftime(TIME_FORMAT),
            "gap_end": worst_end.strftime(TIME_FORMAT),
        }

    return None


def _detect_non_monotonic(machine_database_path: str | Path, tag_name: str, now: datetime) -> bool:
    """Item K. Insertion-order (id-ordered), not time-ordered - a query
    pre-sorted by time can never reveal a timestamp that moved
    backwards relative to when it was actually written."""
    window_start = now - timedelta(hours=CONTINUITY_LOOKBACK_HOURS)
    rows = _recent_readings_by_id_order(machine_database_path, tag_name, window_start)

    previous_time: datetime | None = None
    for row in rows:
        current_time = _parse_time(row["time"])
        if previous_time is not None and current_time < previous_time:
            return True
        previous_time = current_time

    return False


def _detect_frozen_candidate(
    machine_database_path: str | Path, tag_name: str, tag_config: dict[str, Any], now: datetime,
) -> tuple[bool, str | None]:
    """
    Item G - ADVISORY evidence only, gathered here but NOT yet applied
    to any confidence component (see engine/data_health_targets.py's
    module docstring for the full eligibility/corroboration rationale).
    Eligibility is checked here explicitly and defensively, regardless
    of what tags happen to be passed in by the caller.
    """
    if bool(tag_config.get("log_on_change")):
        return False, None

    data_type = (tag_config.get("data_type") or "").upper()
    if data_type in FROZEN_INELIGIBLE_DATA_TYPES:
        return False, None

    interval = tag_config.get("logging_interval_seconds")
    if not interval:
        return False, None

    min_window_seconds = float(interval) * FROZEN_MIN_WINDOW_INTERVAL_MULTIPLIER

    # Query a WIDER window than the minimum required span (2x headroom)
    # - min_window_seconds is the minimum PROVEN duration the last N
    # identical samples must cover, not the query bound itself. Querying
    # exactly min_window_seconds back can clip off the earliest of the N
    # needed readings whenever they don't land exactly on that boundary
    # (a real bug caught by this module's own test suite) - still fully
    # bounded (indexed WHERE tag=?/time>=?, LIMIT-capped), never a full
    # historian scan.
    query_window_start = now - timedelta(seconds=min_window_seconds * 2)

    readings = _recent_readings(
        machine_database_path, tag_name, query_window_start, limit=FROZEN_MIN_CONSECUTIVE_SAMPLES * 3,
    )

    if len(readings) < FROZEN_MIN_CONSECUTIVE_SAMPLES:
        return False, None

    last_n = readings[-FROZEN_MIN_CONSECUTIVE_SAMPLES:]
    distinct_values = {r["value"] for r in last_n}

    if len(distinct_values) != 1:
        return False, None

    span_seconds = (_parse_time(last_n[-1]["time"]) - _parse_time(last_n[0]["time"])).total_seconds()

    if span_seconds < min_window_seconds:
        return False, None

    held_value = next(iter(distinct_values))

    return True, (
        f"{FROZEN_MIN_CONSECUTIVE_SAMPLES} consecutive identical readings "
        f"({held_value}) spanning {span_seconds:.0f}s"
    )


def _display_state(tag: TagDataHealth) -> str:
    """Item L - a single, mutually-exclusive, worst-first summary
    computed AFTER every independent dimension is already known. Never
    consulted by any component score calculation."""
    if not tag.available:
        return TAG_DISPLAY_MISSING
    if tag.validity == VALIDITY_INVALID:
        return TAG_DISPLAY_INVALID
    if tag.freshness == FRESHNESS_STALE:
        return TAG_DISPLAY_STALE
    if tag.frozen_candidate:
        return TAG_DISPLAY_FROZEN_CANDIDATE
    if tag.freshness == FRESHNESS_INDETERMINATE:
        return TAG_DISPLAY_INDETERMINATE
    return TAG_DISPLAY_FRESH


def evaluate_tag_data_health(
    machine_database_path: str | Path, tag_name: str, tag_config: dict[str, Any], now: datetime,
) -> TagDataHealth:
    """
    Item M's single-tag entry point - every dimension computed
    independently (item B), never derived from each other.
    """
    log_on_change = bool(tag_config.get("log_on_change"))
    interval = tag_config.get("logging_interval_seconds")

    latest = _latest_reading(machine_database_path, tag_name)

    if latest is None:
        return TagDataHealth(
            tag_name=tag_name, available=False,
            log_on_change=log_on_change, logging_interval_seconds=interval,
            display_state=TAG_DISPLAY_MISSING,
        )

    last_time_str = latest["time"]
    last_time = _parse_time(last_time_str)
    seconds_since_update = (now - last_time).total_seconds()
    future_timestamp = seconds_since_update < -FUTURE_TIMESTAMP_TOLERANCE_SECONDS

    freshness = _classify_freshness(tag_config, seconds_since_update)
    validity, invalid_reason = _classify_validity(latest["value"])

    continuity_applicable = (not log_on_change) and bool(interval)
    has_gap = False
    gap_seconds = gap_start = gap_end = None

    if continuity_applicable:
        gap = _detect_gap(machine_database_path, tag_name, float(interval), now)
        if gap is not None:
            has_gap = True
            gap_seconds, gap_start, gap_end = gap["gap_seconds"], gap["gap_start"], gap["gap_end"]

    frozen_candidate, frozen_reason = _detect_frozen_candidate(machine_database_path, tag_name, tag_config, now)
    non_monotonic = _detect_non_monotonic(machine_database_path, tag_name, now)

    tag_health = TagDataHealth(
        tag_name=tag_name, available=True, last_value=latest["value"], last_time=last_time_str,
        seconds_since_update=seconds_since_update,
        freshness=freshness, validity=validity, invalid_reason=invalid_reason,
        continuity_applicable=continuity_applicable, has_gap=has_gap,
        gap_seconds=gap_seconds, gap_start=gap_start, gap_end=gap_end,
        frozen_candidate=frozen_candidate, frozen_reason=frozen_reason,
        future_timestamp=future_timestamp, non_monotonic=non_monotonic,
        log_on_change=log_on_change, logging_interval_seconds=interval,
    )
    tag_health.display_state = _display_state(tag_health)
    return tag_health


# ---------------------------------------------------------------------------
# Equipment-level aggregation (items C, primary aggregation tier per the
# approved architecture).
# ---------------------------------------------------------------------------

def _unavailable_result(
    instance_key: str, equipment_id: int | None, equipment_type: str | None,
    limitations: list[str], reasons: list[str], computed_at: str,
) -> EquipmentDataHealth:
    return EquipmentDataHealth(
        instance_key=instance_key, equipment_id=equipment_id, equipment_type=equipment_type,
        confidence_score=None, confidence_status=STATUS_UNAVAILABLE,
        component_scores={"freshness": None, "availability": None, "validity": None, "continuity": None},
        component_applicability={"freshness": False, "availability": False, "validity": False, "continuity": False},
        required_tag_count=0, available_tag_count=0, fresh_tag_count=0,
        source=_current_source(), limitations=limitations, reasons=reasons,
        computed_at=computed_at,
    )


def calculate_equipment_data_health(
    config_database_path: str | Path, machine_database_path: str | Path, instance_key: str,
    now: datetime | None = None,
) -> EquipmentDataHealth:
    """
    The primary Phase 16.1 entry point - equipment-level Data Confidence.
    Never calculates Equipment Health, never writes anything, never scans
    the full historian.
    """
    now = now or datetime.now()
    computed_at = now.strftime(TIME_FORMAT)

    equipment_id = hev.equipment_id_for_instance(config_database_path, instance_key)
    equipment_type = _equipment_type_for_instance(instance_key)

    required_tags = resolve_required_tags(config_database_path, instance_key, equipment_type)

    if required_tags is None:
        return _unavailable_result(
            instance_key, equipment_id, equipment_type,
            limitations=[
                f"No Data Health telemetry registry is defined for equipment_type "
                f"{equipment_type!r} - this equipment type is not covered by "
                f"engine.baseline_targets.EQUIPMENT_TYPE_PROFILES."
            ],
            reasons=["No applicable required-tag registry - Data Confidence cannot be calculated."],
            computed_at=computed_at,
        )

    if not required_tags:
        return _unavailable_result(
            instance_key, equipment_id, equipment_type,
            limitations=[
                f"A Data Health telemetry registry exists for equipment_type "
                f"{equipment_type!r}, but none of its expected tags currently "
                f"exist/are enabled for this specific instance ({instance_key})."
            ],
            reasons=["Zero resolvable required tags for this instance - Data Confidence cannot be calculated."],
            computed_at=computed_at,
        )

    tag_configs = _tag_configs(config_database_path, required_tags)
    tags = [
        evaluate_tag_data_health(machine_database_path, tag_name, tag_configs.get(tag_name, {}), now)
        for tag_name in required_tags
    ]

    # Corroboration pass (item G) - a frozen-candidate is only kept if at
    # least one OTHER required tag on this same equipment is confirmed
    # FRESH. If every other tag is also stale/indeterminate, the whole
    # equipment/source has likely gone quiet together - that is a
    # freshness/continuity story, never an isolated "this one sensor is
    # stuck" diagnosis.
    suppressed_frozen: list[str] = []
    for tag in tags:
        if not tag.frozen_candidate:
            continue
        other_tags_fresh = any(
            other.freshness == FRESHNESS_FRESH for other in tags if other.tag_name != tag.tag_name
        )
        if not other_tags_fresh:
            suppressed_frozen.append(tag.tag_name)
            tag.frozen_candidate = False
            tag.frozen_reason = None
            tag.display_state = _display_state(tag)

    required_tag_count = len(tags)
    available_tag_count = sum(1 for t in tags if t.available)
    fresh_tag_count = sum(1 for t in tags if t.freshness == FRESHNESS_FRESH)

    missing_tags = [t.tag_name for t in tags if not t.available]
    stale_tags = [t.tag_name for t in tags if t.freshness == FRESHNESS_STALE]
    indeterminate_freshness_tags = [t.tag_name for t in tags if t.freshness == FRESHNESS_INDETERMINATE]
    invalid_tags = [t.tag_name for t in tags if t.validity == VALIDITY_INVALID]
    frozen_candidates = [t.tag_name for t in tags if t.frozen_candidate]

    gaps = [
        {"tag": t.tag_name, "gap_seconds": t.gap_seconds, "gap_start": t.gap_start, "gap_end": t.gap_end}
        for t in tags if t.has_gap
    ]
    timestamp_issues = [
        {"tag": t.tag_name, "future_timestamp": t.future_timestamp, "non_monotonic": t.non_monotonic}
        for t in tags if t.future_timestamp or t.non_monotonic
    ]

    # --- Component scores (item B/C - each from independent evidence,
    # never from tag.display_state, full required_tag_count denominator
    # unless a component is genuinely inapplicable to a tag - item C's
    # explicit "do not silently shrink the denominator to inflate the
    # score"). ---

    availability_score = (available_tag_count / required_tag_count) * 100.0

    freshness_total = 0.0
    for t in tags:
        if t.freshness == FRESHNESS_FRESH:
            freshness_total += 100.0
        elif t.freshness == FRESHNESS_INDETERMINATE:
            freshness_total += FRESHNESS_INDETERMINATE_SCORE
        # STALE or unavailable (freshness is None) -> +0.0
    freshness_score = freshness_total / required_tag_count

    validity_total = sum(100.0 for t in tags if t.validity == VALIDITY_VALID)
    validity_score = validity_total / required_tag_count

    continuity_eligible = [t for t in tags if t.continuity_applicable]
    if continuity_eligible:
        continuity_score = sum(0.0 if t.has_gap else 100.0 for t in continuity_eligible) / len(continuity_eligible)
        continuity_applicable = True
    else:
        continuity_score = None
        continuity_applicable = False

    component_scores = {
        "freshness": round(freshness_score, 1),
        "availability": round(availability_score, 1),
        "validity": round(validity_score, 1),
        "continuity": round(continuity_score, 1) if continuity_score is not None else None,
    }
    component_applicability = {
        "freshness": True, "availability": True, "validity": True, "continuity": continuity_applicable,
    }

    applicable_weight_sum = sum(
        COMPONENT_WEIGHTS[name] for name, applicable in component_applicability.items() if applicable
    )

    confidence_score: float | None
    if available_tag_count == 0:
        # Item C's explicit gate: zero of the required tags have EVER
        # produced a value - there is no evidence to compute FROM, never
        # a fabricated low number pretending to be a real measurement.
        confidence_score = None
    elif applicable_weight_sum <= 0:
        confidence_score = None
    else:
        weighted_sum = sum(
            COMPONENT_WEIGHTS[name] * component_scores[name]
            for name, applicable in component_applicability.items()
            if applicable and component_scores[name] is not None
        )
        confidence_score = round(weighted_sum / applicable_weight_sum, 1)

    confidence_status = confidence_status_for_score(confidence_score)

    limitations: list[str] = []
    if not continuity_applicable:
        limitations.append(
            "Continuity is not applicable for this equipment - none of its required tags log on a "
            "fixed interval (all are log_on_change) - Data Confidence was reweighted across the "
            "remaining components."
        )
    if indeterminate_freshness_tags:
        limitations.append(
            f"{len(indeterminate_freshness_tags)} required tag(s) log on change only - freshness is "
            f"reported as indeterminate (neither counted fresh nor stale) rather than guessed: "
            f"{', '.join(indeterminate_freshness_tags)}."
        )
    if suppressed_frozen:
        limitations.append(
            f"Frozen-candidate evidence was found for {len(suppressed_frozen)} tag(s) but suppressed "
            f"because no sibling required tag on this equipment is confirmed fresh (likely a broader "
            f"interruption, not an isolated stuck sensor): {', '.join(suppressed_frozen)}."
        )
    if available_tag_count == 0:
        limitations.append(
            f"None of the {required_tag_count} required tag(s) have ever produced a value - Data "
            f"Confidence cannot be calculated from zero evidence."
        )

    reasons = [
        f"{fresh_tag_count}/{required_tag_count} required tags fresh",
        f"{available_tag_count}/{required_tag_count} required tags have ever reported data",
        f"{len(invalid_tags)} invalid value(s)" if invalid_tags else "0 invalid values",
    ]
    if continuity_applicable:
        gap_count = sum(1 for t in continuity_eligible if t.has_gap)
        reasons.append(
            f"{gap_count} sampling gap(s) in the last {CONTINUITY_LOOKBACK_HOURS}h "
            f"(of {len(continuity_eligible)} continuity-eligible tag(s))"
        )
    if frozen_candidates:
        reasons.append(f"{len(frozen_candidates)} frozen-candidate tag(s) (advisory only, not scored)")

    return EquipmentDataHealth(
        instance_key=instance_key, equipment_id=equipment_id, equipment_type=equipment_type,
        confidence_score=confidence_score, confidence_status=confidence_status,
        component_scores=component_scores, component_applicability=component_applicability,
        required_tag_count=required_tag_count, available_tag_count=available_tag_count,
        fresh_tag_count=fresh_tag_count,
        stale_tags=stale_tags, missing_tags=missing_tags, invalid_tags=invalid_tags,
        indeterminate_freshness_tags=indeterminate_freshness_tags, frozen_candidates=frozen_candidates,
        gaps=gaps, timestamp_issues=timestamp_issues,
        source=_current_source(), tags=tags,
        limitations=limitations, reasons=reasons,
        model_version=DATA_HEALTH_MODEL_VERSION, computed_at=computed_at,
    )
