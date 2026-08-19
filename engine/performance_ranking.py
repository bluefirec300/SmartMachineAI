from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engine import performance_engine as pe
from engine.performance_targets import (
    ATTENTION_CRITICALITY_CONTRIBUTION,
    ATTENTION_MAINTENANCE_INEFFECTIVENESS_MAX,
    ATTENTION_PERSISTENCE_MAX,
    ATTENTION_PERSISTENCE_PER_OBSERVATION,
    ATTENTION_DEGRADATION_MAGNITUDE_MAX,
    EFFECTIVENESS_NO_MEASURABLE_CHANGE,
    EFFECTIVENESS_WORSENED,
    SIGNIFICANTLY_DEGRADING_THRESHOLD_PCT,
    STATE_DEGRADING,
    STATE_NOT_CLASSIFIED,
    STATE_SIGNIFICANTLY_DEGRADING,
)
from engine.savings_verification_evidence import EVIDENCE_INSUFFICIENT

"""
Phase 14 - Asset Attention Ranking. Pure, read-only composition over
ALREADY-FETCHED asset_performance_observations / _maintenance_comparisons
rows plus equipment criticality - no new query pattern, no persistence
(the ranking itself is a derived, on-demand view, mirroring Phase 13's
own get_overview() convention).

MANDATORY SEPARATION (the approved correction to the original design):
evidence quality NEVER increases attention_score. A dimension whose
evidence_quality is INSUFFICIENT is excluded entirely from the
degradation/persistence calculation - poor or missing evidence is not
evidence of poor performance. If EVERY dimension for an equipment is
excluded this way, the equipment gets an explicit INSUFFICIENT_EVIDENCE
attention_state (attention_score=None), never a fabricated low/neutral
score. Evidence quality is reported as its own companion field
(`evidence_quality`) describing confidence in the DRIVING dimension,
never folded into the score itself.
"""

ATTENTION_STATE_RANKED = "RANKED"
ATTENTION_STATE_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass
class AttentionRankingEntry:
    equipment_id: int | None
    plant_id: int
    plant_code: str
    instance_key: str
    equipment_type: str
    attention_state: str
    attention_score: float | None
    degradation_component: float
    persistence_component: float
    criticality_component: float
    maintenance_ineffectiveness_component: float
    worst_dimension_target_key: str | None
    worst_dimension_state: str | None
    worst_dimension_percent_change: float | None
    evidence_quality: str | None
    criticality: str | None
    maintenance_effectiveness: str | None
    reason: str
    considered_dimension_count: int = 0
    excluded_insufficient_dimension_count: int = 0


def _usable_observations(observations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Only dimensions that (a) have a defensible direction and (b) are
    NOT insufficient-evidence participate in the score at all. Returns
    (usable, excluded_count) - the exclusion count is disclosed, never
    silently dropped."""
    usable = [
        o for o in observations
        if o.get("participates_in_degradation") and o.get("evidence_quality") != EVIDENCE_INSUFFICIENT
        and o.get("performance_state") != STATE_NOT_CLASSIFIED
    ]
    excluded = sum(
        1 for o in observations
        if o.get("participates_in_degradation") and o.get("evidence_quality") == EVIDENCE_INSUFFICIENT
    )
    return usable, excluded


def compute_attention_entry(
    equipment_id: int | None, plant_id: int, plant_code: str, instance_key: str, equipment_type: str,
    observations: list[dict[str, Any]], maintenance_comparisons: list[dict[str, Any]], criticality: str | None,
) -> AttentionRankingEntry:
    """`observations` = the LATEST observation row per target_key for this
    one equipment (already fetched, one bulk query upstream).
    `maintenance_comparisons` = this equipment's maintenance comparisons,
    most-recent performed_at first (already fetched)."""
    usable, excluded_insufficient = _usable_observations(observations)

    if not usable:
        return AttentionRankingEntry(
            equipment_id=equipment_id, plant_id=plant_id, plant_code=plant_code, instance_key=instance_key,
            equipment_type=equipment_type, attention_state=ATTENTION_STATE_INSUFFICIENT_EVIDENCE, attention_score=None,
            degradation_component=0.0, persistence_component=0.0, criticality_component=0.0,
            maintenance_ineffectiveness_component=0.0, worst_dimension_target_key=None, worst_dimension_state=None,
            worst_dimension_percent_change=None, evidence_quality=None, criticality=criticality,
            maintenance_effectiveness=None,
            reason="No dimension currently has adequate, usable evidence to assess engineering attention - "
                   "this is a data-investigation case, never interpreted as poor equipment performance.",
            considered_dimension_count=0, excluded_insufficient_dimension_count=excluded_insufficient,
        )

    def _signed(observation: dict[str, Any]) -> float:
        signed = pe._signed_change(observation["direction"], observation["percent_change"])
        return signed if signed is not None else -1e9

    worst = max(usable, key=_signed)
    worst_signed = _signed(worst)

    # Gated on the ALREADY-CLASSIFIED performance_state, not raw percent
    # magnitude alone - a dimension the engine itself classified STABLE
    # (change below STATE_CHANGE_THRESHOLD_PCT, noise) must contribute
    # ZERO here, never a small nonzero score from the same noise the
    # engine already decided not to treat as a real change.
    degradation_component = 0.0
    if worst["performance_state"] in (STATE_DEGRADING, STATE_SIGNIFICANTLY_DEGRADING) and worst_signed > 0:
        degradation_component = round(min(ATTENTION_DEGRADATION_MAGNITUDE_MAX, (worst_signed / SIGNIFICANTLY_DEGRADING_THRESHOLD_PCT) * ATTENTION_DEGRADATION_MAGNITUDE_MAX), 2)

    consecutive = worst.get("consecutive_degrading_observations") or 0
    persistence_component = round(min(ATTENTION_PERSISTENCE_MAX, consecutive * ATTENTION_PERSISTENCE_PER_OBSERVATION), 2)

    criticality_component = round(ATTENTION_CRITICALITY_CONTRIBUTION.get(criticality, 0.0), 2) if criticality else 0.0

    maintenance_effectiveness = maintenance_comparisons[0]["effectiveness_result"] if maintenance_comparisons else None
    maintenance_ineffectiveness_component = 0.0
    if maintenance_effectiveness == EFFECTIVENESS_WORSENED:
        maintenance_ineffectiveness_component = ATTENTION_MAINTENANCE_INEFFECTIVENESS_MAX
    elif maintenance_effectiveness == EFFECTIVENESS_NO_MEASURABLE_CHANGE:
        maintenance_ineffectiveness_component = round(ATTENTION_MAINTENANCE_INEFFECTIVENESS_MAX * 0.3, 2)
    # IMPROVED / INSUFFICIENT_EVIDENCE / NOT_CLASSIFIED contribute 0 - an
    # effective or unassessed intervention is never itself a reason for
    # engineering attention.

    attention_score = round(
        degradation_component + persistence_component + criticality_component + maintenance_ineffectiveness_component, 1,
    )

    reason = (
        f"Driven primarily by {worst['target_key']} ({worst['performance_state']}, "
        f"{worst['percent_change']:+.1f}% vs. reference, {consecutive} consecutive degrading observation(s), "
        f"{worst['evidence_quality']} evidence)."
    )
    if excluded_insufficient:
        reason += f" {excluded_insufficient} other dimension(s) excluded from this score - insufficient evidence, not counted as poor performance."

    return AttentionRankingEntry(
        equipment_id=equipment_id, plant_id=plant_id, plant_code=plant_code, instance_key=instance_key,
        equipment_type=equipment_type, attention_state=ATTENTION_STATE_RANKED, attention_score=attention_score,
        degradation_component=degradation_component, persistence_component=persistence_component,
        criticality_component=criticality_component, maintenance_ineffectiveness_component=maintenance_ineffectiveness_component,
        worst_dimension_target_key=worst["target_key"], worst_dimension_state=worst["performance_state"],
        worst_dimension_percent_change=worst["percent_change"], evidence_quality=worst["evidence_quality"],
        criticality=criticality, maintenance_effectiveness=maintenance_effectiveness, reason=reason,
        considered_dimension_count=len(usable), excluded_insufficient_dimension_count=excluded_insufficient,
    )
