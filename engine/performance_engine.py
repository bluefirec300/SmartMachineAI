from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from database.database import DatabaseManager
from engine import baseline_engine as base
from engine import health_evidence as hev
from engine.baseline_targets import BaselineTarget, discover_targets
from engine.performance_targets import (
    EFFECTIVENESS_CHANGE_THRESHOLD_PCT,
    EFFECTIVENESS_IMPROVED,
    EFFECTIVENESS_INSUFFICIENT_EVIDENCE,
    EFFECTIVENESS_NOT_CLASSIFIED,
    EFFECTIVENESS_NO_MEASURABLE_CHANGE,
    EFFECTIVENESS_WORSENED,
    HIGHER_IS_BETTER,
    INFORMATIONAL_ONLY,
    LOWER_IS_BETTER,
    MAINTENANCE_PRE_WINDOW_DAYS_MAX,
    MAINTENANCE_STABILIZATION_DAYS,
    DEGRADING_THRESHOLD_PCT,
    PERFORMANCE_MODEL_VERSION,
    SIGNIFICANTLY_DEGRADING_THRESHOLD_PCT,
    STATE_CHANGE_THRESHOLD_PCT,
    STATE_DEGRADING,
    STATE_IMPROVING,
    STATE_INSUFFICIENT_EVIDENCE,
    STATE_NOT_CLASSIFIED,
    STATE_SIGNIFICANTLY_DEGRADING,
    STATE_STABLE,
    TARGET_RANGE,
    dimension_for,
)
from engine.savings_verification_evidence import EVIDENCE_INSUFFICIENT, classify_evidence_quality

"""
Phase 14 - Asset Performance & Reliability Analytics deterministic
engine. Read-only / pure-calculate, exactly like Phase 12.1's
health_engine.py and Phase 11.3's savings_verification_engine.py
precedent: nothing in this module persists a result anywhere
(engine/performance_domain.py owns that, as a distinct step).

Reuses, never reimplements:
  - engine.baseline_engine.compute_window_baseline() / ._reference_recent_windows()
    / ._current_context() / ._maintenance_intervals() - Phase 8's own
    context-matching, robust statistics, and fault/maintenance-interval
    reconstruction, called directly exactly as engine.savings_verification_engine
    (Phase 11.3) already does across engine-module boundaries - an
    established, accepted reuse pattern in this codebase, not a new one.
  - engine.savings_verification_evidence.classify_evidence_quality() -
    the SAME STRONG/CONTEXT_MATCHED/LIMITED/INSUFFICIENT evidence-quality
    tiering Phase 11.2 already established, unmodified.
  - engine.health_evidence.equipment_id_for_instance() - the same
    equipment-resolution helper Phase 12/13 already use.

NEVER calls engine.health_engine.calculate_health() or
engine.maintenance_intelligence_engine.calculate_maintenance_priority() -
Phase 14 reads their PERSISTED outputs (via engine.health_domain /
engine.maintenance_intelligence_engine) only where explicitly needed
for the attention ranking (ui/asset_performance_data.py), never here.

No prediction, no RUL, no failure probability, no forecast anywhere in
this module - every calculation compares two ALREADY-ELAPSED windows.
"""

TIME_FORMAT = base.TIME_FORMAT


@dataclass
class PerformanceObservation:
    equipment_id: int | None
    plant_id: int
    plant_code: str
    instance_key: str
    equipment_type: str
    target_key: str
    direction: str
    unit: str | None
    observed_value: float | None
    reference_value: float | None
    absolute_change: float | None
    percent_change: float | None
    performance_state: str
    evidence_quality: str
    sample_count: int
    reference_sample_count: int
    participates_in_degradation: bool
    context_used: dict[str, str] = field(default_factory=dict)
    missing_context: list[str] = field(default_factory=list)
    reason: str = ""
    assumptions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    model_version: str = PERFORMANCE_MODEL_VERSION
    computed_at: str = ""


@dataclass
class MaintenanceComparison:
    equipment_id: int | None
    plant_id: int
    plant_code: str
    instance_key: str
    equipment_type: str
    target_key: str
    direction: str
    unit: str | None
    maintenance_log_id: int
    performed_at: str
    pre_window: tuple[str, str] | None
    post_window: tuple[str, str] | None
    pre_value: float | None
    post_value: float | None
    absolute_change: float | None
    percent_change: float | None
    pre_sample_count: int
    post_sample_count: int
    evidence_quality: str
    effectiveness_result: str
    reason: str = ""
    assumptions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    model_version: str = PERFORMANCE_MODEL_VERSION
    computed_at: str = ""


# ---------------------------------------------------------------------------
# Direction-aware change classification (item 10/11) - a positive
# "signed" value always means "moved in the WORSE direction relative to
# the reference", regardless of the raw sign of percent_change.
# TARGET_RANGE dimensions can never report IMPROVING - being AT the
# reference already IS the ideal, so there is no "better than normal"
# direction to move toward, only "stable" (near it) or "degrading"
# (away from it, either side) - documented, not an oversight.
# ---------------------------------------------------------------------------

def _signed_change(direction: str, percent_change: float) -> float | None:
    if direction == LOWER_IS_BETTER:
        return percent_change
    if direction == HIGHER_IS_BETTER:
        return -percent_change
    if direction == TARGET_RANGE:
        return abs(percent_change)
    return None  # INFORMATIONAL_ONLY


def classify_change(direction: str, percent_change: float | None) -> str:
    """Bands on `signed` (direction-adjusted, positive = worse):
    <= -STATE_CHANGE_THRESHOLD_PCT -> IMPROVING
    (-STATE_CHANGE_THRESHOLD_PCT, DEGRADING_THRESHOLD_PCT) -> STABLE
    [DEGRADING_THRESHOLD_PCT, SIGNIFICANTLY_DEGRADING_THRESHOLD_PCT) -> DEGRADING
    >= SIGNIFICANTLY_DEGRADING_THRESHOLD_PCT -> SIGNIFICANTLY_DEGRADING"""
    if direction == INFORMATIONAL_ONLY:
        return STATE_NOT_CLASSIFIED
    if percent_change is None:
        return STATE_INSUFFICIENT_EVIDENCE
    signed = _signed_change(direction, percent_change)
    if signed <= -STATE_CHANGE_THRESHOLD_PCT:
        return STATE_IMPROVING
    if signed >= SIGNIFICANTLY_DEGRADING_THRESHOLD_PCT:
        return STATE_SIGNIFICANTLY_DEGRADING
    if signed >= DEGRADING_THRESHOLD_PCT:
        return STATE_DEGRADING
    return STATE_STABLE


def classify_effectiveness(direction: str, percent_change: float | None) -> str:
    if direction == INFORMATIONAL_ONLY:
        return EFFECTIVENESS_NOT_CLASSIFIED
    if percent_change is None:
        return EFFECTIVENESS_INSUFFICIENT_EVIDENCE
    signed = _signed_change(direction, percent_change)
    if signed <= -EFFECTIVENESS_CHANGE_THRESHOLD_PCT:
        return EFFECTIVENESS_IMPROVED
    if signed < EFFECTIVENESS_CHANGE_THRESHOLD_PCT:
        return EFFECTIVENESS_NO_MEASURABLE_CHANGE
    return EFFECTIVENESS_WORSENED


# ---------------------------------------------------------------------------
# Percent-change arithmetic (item 9) - never a silent divide-by-zero,
# never a fabricated 0% from an undefined comparison.
# ---------------------------------------------------------------------------

_MIN_REFERENCE_MAGNITUDE = 1e-6


def _compute_change(reference_value: float | None, observed_value: float | None) -> tuple[float | None, float | None, list[str]]:
    limitations: list[str] = []
    if reference_value is None or observed_value is None:
        return None, None, limitations
    absolute_change = round(observed_value - reference_value, 4)
    if abs(reference_value) < _MIN_REFERENCE_MAGNITUDE:
        limitations.append("Reference value is too close to zero for a meaningful percentage - percent change is Unavailable (absolute change is still reported).")
        return absolute_change, None, limitations
    percent_change = round(absolute_change / reference_value * 100, 2)
    if reference_value < 0:
        limitations.append("Percent change is computed against a negative reference value - interpret with care.")
    return absolute_change, percent_change, limitations


# ---------------------------------------------------------------------------
# Ongoing self-reference observation (Section 5/7 default mode)
# ---------------------------------------------------------------------------

def calculate_performance_observation(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    plant_id: int, target: BaselineTarget, now: datetime | None = None,
) -> PerformanceObservation:
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)

    dimension = dimension_for(target.equipment_type, target.target_key)
    equipment_id = hev.equipment_id_for_instance(config_database_path, target.instance_key)

    reference_window, recent_window = base._reference_recent_windows(now, target.reference_window_days_max, target.recent_window_days)
    same_window = reference_window == recent_window

    representative_context_at = min(recent_window[1], now)
    current_context = base._current_context(target, historian, config_database_path, representative_context_at)

    recent_result = base.compute_window_baseline(target, historian, config_database_path, recent_window[0], recent_window[1], current_context)
    if same_window:
        reference_result = recent_result
    else:
        reference_result = base.compute_window_baseline(target, historian, config_database_path, reference_window[0], reference_window[1], current_context)

    shared_dims = set(reference_result["context_used"].keys()) & set(recent_result["context_used"].keys())
    evidence_quality = classify_evidence_quality(reference_result, recent_result, shared_dims, target)

    reference_value = reference_result["median"]
    observed_value = recent_result["median"]
    absolute_change, percent_change, limitations = _compute_change(reference_value, observed_value)

    if evidence_quality == EVIDENCE_INSUFFICIENT:
        performance_state = STATE_INSUFFICIENT_EVIDENCE if dimension.direction != INFORMATIONAL_ONLY else STATE_NOT_CLASSIFIED
    else:
        performance_state = classify_change(dimension.direction, percent_change)

    if same_window:
        limitations.append("Reference and recent windows use the same range - not enough modern history yet to split them.")

    assumptions = [
        "Reference and observed values are both robust, context-matched MEDIANS under Phase 8's own baseline "
        "engine - reused unmodified, not recalculated by Phase 14.",
        f"A change below {STATE_CHANGE_THRESHOLD_PCT:.0f}% (direction-adjusted) is treated as noise (STABLE), never "
        "reported as a precise trend.",
        "This is a backward-looking, already-elapsed comparison only - no prediction, no extrapolation, no future date.",
    ]

    reason = _explain_observation(dimension, evidence_quality, performance_state, percent_change, recent_result, reference_result)

    return PerformanceObservation(
        equipment_id=equipment_id, plant_id=plant_id, plant_code=target.plant_code, instance_key=target.instance_key,
        equipment_type=target.equipment_type, target_key=target.target_key, direction=dimension.direction,
        unit=dimension.unit, observed_value=observed_value, reference_value=reference_value,
        absolute_change=absolute_change, percent_change=percent_change, performance_state=performance_state,
        evidence_quality=evidence_quality, sample_count=recent_result["representative_sample_count"],
        reference_sample_count=reference_result["representative_sample_count"],
        participates_in_degradation=dimension.participates_in_degradation,
        context_used=recent_result["context_used"], missing_context=recent_result["missing_context"],
        reason=reason, assumptions=assumptions, limitations=limitations,
        computed_at=now_text,
    )


def _explain_observation(dimension, evidence_quality: str, performance_state: str, percent_change: float | None, recent_result: dict, reference_result: dict) -> str:
    if dimension.direction == INFORMATIONAL_ONLY:
        return f"{dimension.target_key} is informational only - no defensible engineering direction is established for it."
    if performance_state == STATE_INSUFFICIENT_EVIDENCE:
        return (
            f"Insufficient evidence to assess {dimension.target_key}: {recent_result['representative_sample_count']} "
            f"recent / {reference_result['representative_sample_count']} reference comparable sample(s)."
        )
    change_text = "Unavailable" if percent_change is None else f"{percent_change:+.1f}%"
    return (
        f"{performance_state} - {dimension.target_key} changed {change_text} relative to its established reference "
        f"({reference_result['representative_sample_count']} reference / {recent_result['representative_sample_count']} "
        f"recent comparable sample(s)), {evidence_quality} evidence."
    )


# ---------------------------------------------------------------------------
# Maintenance-anchored before/after comparison (Sections 16-19)
# ---------------------------------------------------------------------------

def compute_pre_window(performed_at: datetime, pre_window_days_max: int = MAINTENANCE_PRE_WINDOW_DAYS_MAX) -> tuple[datetime, datetime]:
    end = performed_at - timedelta(seconds=1)
    start = end - timedelta(days=pre_window_days_max)
    return start, end


def compute_post_window(performed_at: datetime, stabilization_days: int, now: datetime) -> tuple[datetime, datetime] | None:
    eligible_start = performed_at + timedelta(days=stabilization_days)
    if now < eligible_start:
        return None
    return eligible_start, now


def calculate_maintenance_comparison(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    plant_id: int, target: BaselineTarget, maintenance_log_row: dict[str, Any], now: datetime | None = None,
) -> MaintenanceComparison:
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)

    dimension = dimension_for(target.equipment_type, target.target_key)
    equipment_id = hev.equipment_id_for_instance(config_database_path, target.instance_key)
    maintenance_log_id = maintenance_log_row["id"]
    performed_at = datetime.strptime(maintenance_log_row["performed_at"][:19], "%Y-%m-%d %H:%M:%S") if len(maintenance_log_row["performed_at"]) > 10 \
        else datetime.strptime(maintenance_log_row["performed_at"], "%Y-%m-%d")

    pre_window = compute_pre_window(performed_at)
    post_window = compute_post_window(performed_at, MAINTENANCE_STABILIZATION_DAYS, now)

    if post_window is None:
        return MaintenanceComparison(
            equipment_id=equipment_id, plant_id=plant_id, plant_code=target.plant_code, instance_key=target.instance_key,
            equipment_type=target.equipment_type, target_key=target.target_key, direction=dimension.direction,
            unit=dimension.unit, maintenance_log_id=maintenance_log_id, performed_at=maintenance_log_row["performed_at"],
            pre_window=(pre_window[0].strftime(TIME_FORMAT), pre_window[1].strftime(TIME_FORMAT)), post_window=None,
            pre_value=None, post_value=None, absolute_change=None, percent_change=None,
            pre_sample_count=0, post_sample_count=0, evidence_quality=EVIDENCE_INSUFFICIENT,
            effectiveness_result=EFFECTIVENESS_INSUFFICIENT_EVIDENCE,
            reason=f"Stabilization period not finished (eligible from {(performed_at + timedelta(days=MAINTENANCE_STABILIZATION_DAYS)).strftime(TIME_FORMAT)}).",
            limitations=["Post-maintenance stabilization period has not yet elapsed."],
            computed_at=now_text,
        )

    representative_context_at = min(post_window[1], now)
    current_context = base._current_context(target, historian, config_database_path, representative_context_at)

    pre_result = base.compute_window_baseline(target, historian, config_database_path, pre_window[0], pre_window[1], current_context)
    post_result = base.compute_window_baseline(target, historian, config_database_path, post_window[0], post_window[1], current_context)

    shared_dims = set(pre_result["context_used"].keys()) & set(post_result["context_used"].keys())
    evidence_quality = classify_evidence_quality(pre_result, post_result, shared_dims, target)

    pre_value = pre_result["median"]
    post_value = post_result["median"]
    absolute_change, percent_change, limitations = _compute_change(pre_value, post_value)

    if evidence_quality == EVIDENCE_INSUFFICIENT:
        effectiveness_result = EFFECTIVENESS_INSUFFICIENT_EVIDENCE
    else:
        effectiveness_result = classify_effectiveness(dimension.direction, percent_change)

    reason = _explain_maintenance(dimension, evidence_quality, effectiveness_result, percent_change, pre_result, post_result)

    assumptions = [
        "Pre-maintenance window is frozen relative to the maintenance_log entry's performed_at and does not drift as time passes.",
        f"Post-maintenance evidence becomes eligible only from performed_at + {MAINTENANCE_STABILIZATION_DAYS} stabilization day(s).",
        "This reports 'performance changed following maintenance', never a causal claim that the maintenance itself caused the change.",
    ]

    return MaintenanceComparison(
        equipment_id=equipment_id, plant_id=plant_id, plant_code=target.plant_code, instance_key=target.instance_key,
        equipment_type=target.equipment_type, target_key=target.target_key, direction=dimension.direction,
        unit=dimension.unit, maintenance_log_id=maintenance_log_id, performed_at=maintenance_log_row["performed_at"],
        pre_window=(pre_window[0].strftime(TIME_FORMAT), pre_window[1].strftime(TIME_FORMAT)),
        post_window=(post_window[0].strftime(TIME_FORMAT), post_window[1].strftime(TIME_FORMAT)),
        pre_value=pre_value, post_value=post_value, absolute_change=absolute_change, percent_change=percent_change,
        pre_sample_count=pre_result["representative_sample_count"], post_sample_count=post_result["representative_sample_count"],
        evidence_quality=evidence_quality, effectiveness_result=effectiveness_result, reason=reason,
        assumptions=assumptions, limitations=limitations, computed_at=now_text,
    )


def _explain_maintenance(dimension, evidence_quality: str, effectiveness_result: str, percent_change: float | None, pre_result: dict, post_result: dict) -> str:
    if dimension.direction == INFORMATIONAL_ONLY:
        return f"{dimension.target_key} is informational only - no defensible engineering direction is established for it."
    if effectiveness_result == EFFECTIVENESS_INSUFFICIENT_EVIDENCE:
        return (
            f"Insufficient comparable evidence before/after maintenance for {dimension.target_key}: "
            f"{pre_result['representative_sample_count']} pre / {post_result['representative_sample_count']} post sample(s)."
        )
    change_text = "no measurable change" if percent_change is None else f"{percent_change:+.1f}% change"
    verb = {
        EFFECTIVENESS_IMPROVED: "Performance improved following maintenance",
        EFFECTIVENESS_NO_MEASURABLE_CHANGE: "No measurable performance change followed maintenance",
        EFFECTIVENESS_WORSENED: "Performance worsened following maintenance",
    }.get(effectiveness_result, "Performance changed following maintenance")
    return f"{verb} - {dimension.target_key} showed {change_text} ({pre_result['representative_sample_count']} pre / {post_result['representative_sample_count']} post comparable sample(s)), {evidence_quality} evidence."


# ---------------------------------------------------------------------------
# Target discovery - reuses Phase 8's own registry unmodified.
# ---------------------------------------------------------------------------

def discover_performance_targets(config_database_path: str | Path, plant_code: str) -> list[BaselineTarget]:
    return discover_targets(config_database_path, plant_code)


def _plant_id_for_code(config_database_path: str | Path, plant_code: str) -> int | None:
    connection = sqlite3.connect(config_database_path)
    try:
        row = connection.execute("SELECT id FROM plants WHERE code = ?", (plant_code,)).fetchone()
    finally:
        connection.close()
    return row[0] if row else None
