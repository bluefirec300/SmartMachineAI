from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from database.database import DatabaseManager
from engine import anomaly_targets
from engine import baseline_engine as base
from engine import opportunity_targets
from engine import savings_verification_domain as dom
from engine.baseline_targets import BaselineTarget, discover_targets
from engine.savings_verification_targets import DEFAULT_STABILIZATION_DAYS

"""
Phase 11.2 - Reference-window + context-matching evidence-selection
engine. Deterministic, read-only, produces NO financial claim - only
"is there defensible BEFORE/AFTER evidence, and how comparable is it."
Reuses Phase 8's engine.baseline_engine.compute_window_baseline()
UNMODIFIED for both windows - it already accepts an arbitrary
[window_start, window_end) and a fixed context vector to match
against, which is exactly what an intervention-anchored (rather than
"now"-relative) comparison needs. No new context-matching, robust-
statistics, or fault-exclusion logic was written - all of that is
Phase 8's, reused as-is.

What IS new here: freezing the two windows relative to
implemented_at/stabilization_days (Phase 8's own reference/recent
split is relative to "now", which does not fit a fixed intervention
date), and combining both windows' independent results into one
evidence-quality verdict.

NON-NEGOTIABLE: this module never computes or writes a financial
value, never calls engine.savings_verification_domain.record_verification_result(),
and never marks an intervention COMPLETED/VERIFIED. It only reads.
"""

TIME_FORMAT = base.TIME_FORMAT

EVIDENCE_STRONG = "STRONG"
EVIDENCE_CONTEXT_MATCHED = "CONTEXT_MATCHED"
EVIDENCE_LIMITED = "LIMITED"
EVIDENCE_INSUFFICIENT = "INSUFFICIENT"


# ---------------------------------------------------------------------------
# Target resolution - reuses the existing opportunity/anomaly/baseline
# registries end to end; duplicates none of their matching logic.
# ---------------------------------------------------------------------------

def resolve_target_for_intervention(config_database_path: str | Path, intervention: dict[str, Any]) -> BaselineTarget | None:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        opportunity = connection.execute(
            "SELECT rule_key, plant_id FROM energy_opportunities WHERE id = ?", (intervention["opportunity_id"],),
        ).fetchone()
        plant_code_row = connection.execute("SELECT code FROM plants WHERE id = ?", (intervention["plant_id"],)).fetchone()
    finally:
        connection.close()

    if opportunity is None or plant_code_row is None:
        return None

    opportunity_rule = opportunity_targets.get_rule(opportunity["rule_key"])
    if opportunity_rule is None:
        return None
    anomaly_rule = anomaly_targets.get_rule(opportunity_rule.source_anomaly_rule_key)
    if anomaly_rule is None:
        return None

    plant_code = plant_code_row["code"]
    for target in discover_targets(config_database_path, plant_code):
        if target.instance_key == intervention["instance_key"] and target.target_key == anomaly_rule.baseline_target_key:
            return target
    return None


# ---------------------------------------------------------------------------
# Frozen window boundaries (item 2) - pure functions, independently testable.
# ---------------------------------------------------------------------------

def compute_reference_window(implemented_at: datetime, reference_window_days_max: int) -> tuple[datetime, datetime]:
    """BEFORE window - frozen relative to implemented_at, never drifts
    with 'now'. end is implemented_at MINUS one second, guaranteeing a
    sample recorded at the exact implementation instant can never leak
    into BEFORE (historian range queries are inclusive on both ends)."""
    end = implemented_at - timedelta(seconds=1)
    start = end - timedelta(days=reference_window_days_max)
    return start, end


def compute_after_window(implemented_at: datetime, stabilization_days: int, now: datetime) -> tuple[datetime, datetime] | None:
    """AFTER window - eligible evidence begins at implemented_at PLUS
    stabilization_days (inclusive), grows forward to 'now'. Returns
    None if stabilization hasn't finished yet (including the case
    where implemented_at is in the future)."""
    eligible_start = implemented_at + timedelta(days=stabilization_days)
    if now < eligible_start:
        return None
    return eligible_start, now


# ---------------------------------------------------------------------------
# Exclusion accounting (item 5) - informational counts, reusing
# Phase 8/9's own interval-reconstruction helpers unmodified. Real
# fault-period exclusion (threshold breaches) already happens INSIDE
# compute_window_baseline() via _fetch_clean_downsampled()'s default
# exclude_faults=True - not duplicated here. Maintenance-window overlap
# is reported here because compute_window_baseline() does not apply it
# internally (see Known Issues in the status doc / item Q below).
#
# Deliberately NOT excluded: Phase 9 statistical anomaly occurrences.
# Per your explicit direction, an anomaly's mere existence must never
# be grounds for automatic exclusion - some interventions exist
# specifically to correct the operation an anomaly flagged. These are
# reported as a count for context/explainability only, never subtracted
# from either window's sample basis.
# ---------------------------------------------------------------------------

def _maintenance_overlap_count(config_database_path: str | Path, instance_key: str, window: tuple[datetime, datetime]) -> int:
    intervals = base._maintenance_intervals(config_database_path, instance_key)
    start, end = window
    return sum(1 for (a, b) in intervals if a < end and b > start)


def _anomaly_occurrence_count(config_database_path: str | Path, plant_id: int, instance_key: str, target_key: str, window: tuple[datetime, datetime]) -> int:
    start, end = window
    connection = sqlite3.connect(config_database_path)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM anomalies WHERE plant_id = ? AND instance_key = ? AND target_key = ? "
            "AND first_detected <= ? AND last_seen >= ?",
            (plant_id, instance_key, target_key, end.strftime(TIME_FORMAT), start.strftime(TIME_FORMAT)),
        ).fetchone()[0]
        return count
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Evidence-quality classification (item 4) - deterministic tier, reusing
# Phase 8's own bootstrap/mature/level vocabulary rather than inventing
# a new numeric score. See module docstring - explicitly no percentage.
# ---------------------------------------------------------------------------

def classify_evidence_quality(reference_result: dict[str, Any], after_result: dict[str, Any], shared_dims: set[str], target: BaselineTarget) -> str:
    if reference_result["status"] == "unavailable" or after_result["status"] == "unavailable":
        return EVIDENCE_INSUFFICIENT
    if reference_result["status"] == "bootstrap" or after_result["status"] == "bootstrap":
        return EVIDENCE_LIMITED
    # Both windows independently "mature" from here on.
    if target.context_dimensions and not shared_dims:
        # Each side matched well individually, but not on any COMMON
        # dimension - a before/after comparison needs shared control,
        # not just two independently well-sampled windows.
        return EVIDENCE_LIMITED
    if reference_result["level"] in ("A", "B") and after_result["level"] in ("A", "B"):
        return EVIDENCE_STRONG
    return EVIDENCE_CONTEXT_MATCHED


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def compute_verification_evidence(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    intervention_id: int, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)

    intervention = dom.get_intervention(config_database_path, intervention_id)
    if intervention is None:
        return _result(intervention_id, EVIDENCE_INSUFFICIENT, "no such intervention", now_text)

    if not intervention["implemented_at"]:
        return _result(intervention_id, EVIDENCE_INSUFFICIENT, "intervention not yet implemented (implemented_at is not set)", now_text)

    target = resolve_target_for_intervention(config_database_path, intervention)
    if target is None:
        return _result(intervention_id, EVIDENCE_INSUFFICIENT, "could not resolve a baseline target for this intervention's opportunity", now_text)

    implemented_at = datetime.strptime(intervention["implemented_at"], TIME_FORMAT)
    stabilization_days = intervention["stabilization_days"]
    if stabilization_days is None:
        stabilization_days = DEFAULT_STABILIZATION_DAYS.get(intervention["action_category"], 3)

    reference_window = compute_reference_window(implemented_at, target.reference_window_days_max)
    after_window = compute_after_window(implemented_at, stabilization_days, now)

    if after_window is None:
        return _result(
            intervention_id, EVIDENCE_INSUFFICIENT,
            f"stabilization period not finished (eligible from {(implemented_at + timedelta(days=stabilization_days)).strftime(TIME_FORMAT)}, now is {now_text})",
            now_text, reference_window=reference_window,
        )

    # A single representative context, computed from the AFTER window
    # (what's actually happening post-intervention) and matched
    # identically against BOTH windows - the same mechanism Phase 8's
    # own compute_baseline() uses to compare its reference/recent
    # windows against one shared "current_context" vector.
    representative_context_at = min(after_window[1], now)
    current_context = base._current_context(target, historian, config_database_path, representative_context_at)

    reference_result = base.compute_window_baseline(target, historian, config_database_path, reference_window[0], reference_window[1], current_context)
    after_result = base.compute_window_baseline(target, historian, config_database_path, after_window[0], after_window[1], current_context)

    shared_dims = set(reference_result["context_used"].keys()) & set(after_result["context_used"].keys())
    evidence_quality = classify_evidence_quality(reference_result, after_result, shared_dims, target)

    maintenance_before = _maintenance_overlap_count(config_database_path, intervention["instance_key"], reference_window)
    maintenance_after = _maintenance_overlap_count(config_database_path, intervention["instance_key"], after_window)
    anomaly_overlap_after = _anomaly_occurrence_count(
        config_database_path, intervention["plant_id"], intervention["instance_key"], target.target_key, after_window,
    )

    assumptions = [
        "Reference (BEFORE) window is frozen relative to implemented_at and does not drift as time passes.",
        f"AFTER evidence becomes eligible only from implemented_at + {stabilization_days} stabilization day(s), inclusive.",
        "Fault periods (threshold breaches) are excluded from both windows automatically, via the same mechanism "
        "Phase 8's baseline engine already uses (reused, not duplicated).",
        "Statistical anomaly occurrences (Phase 9) are NOT excluded from either window - an anomaly's existence is "
        "not itself grounds for exclusion, since some interventions exist specifically to correct that operation.",
    ]
    limitations = list(reference_result.get("missing_context", []))
    if maintenance_before or maintenance_after:
        limitations.append(
            f"{maintenance_before} declared maintenance window(s) overlap the reference period, "
            f"{maintenance_after} overlap the verification period - not subtracted from the sample counts above "
            "(informational only; see Phase 11 status doc for why)."
        )
    if anomaly_overlap_after:
        limitations.append(f"{anomaly_overlap_after} statistical anomaly occurrence(s) overlapped the verification period (reported, not excluded).")

    reason = _explain(evidence_quality, reference_result, after_result, shared_dims, maintenance_before, maintenance_after, anomaly_overlap_after)

    return {
        "intervention_id": intervention_id,
        "target_key": target.target_key, "instance_key": target.instance_key, "equipment_type": target.equipment_type,
        "reference_start": reference_window[0].strftime(TIME_FORMAT), "reference_end": reference_window[1].strftime(TIME_FORMAT),
        "eligible_after_start": after_window[0].strftime(TIME_FORMAT), "after_end": after_window[1].strftime(TIME_FORMAT),
        "reference_sample_count": reference_result["representative_sample_count"],
        "after_sample_count": after_result["representative_sample_count"],
        "reference_status": reference_result["status"], "after_status": after_result["status"],
        "reference_level": reference_result["level"], "after_level": after_result["level"],
        "matching_dimensions_used": sorted(shared_dims),
        "exclusion_summary": {
            "maintenance_windows_overlapping_reference": maintenance_before,
            "maintenance_windows_overlapping_after": maintenance_after,
            "anomaly_occurrences_overlapping_after": anomaly_overlap_after,
        },
        "evidence_quality": evidence_quality,
        "assumptions": assumptions,
        "limitations": limitations,
        "reason": reason,
        "computed_at": now_text,
    }


def _explain(
    evidence_quality: str, reference_result: dict[str, Any], after_result: dict[str, Any], shared_dims: set[str],
    maintenance_before: int, maintenance_after: int, anomaly_overlap_after: int,
) -> str:
    parts = [f"{evidence_quality} because:"]
    if evidence_quality == EVIDENCE_INSUFFICIENT:
        if reference_result["status"] == "unavailable":
            parts.append(f"- only {reference_result['representative_sample_count']} eligible reference period(s) (below minimum)")
        if after_result["status"] == "unavailable":
            parts.append(f"- only {after_result['representative_sample_count']} eligible verification period(s) (below minimum)")
        return "\n".join(parts)

    parts.append(f"- {reference_result['representative_sample_count']} comparable reference period(s), {after_result['representative_sample_count']} comparable verification period(s)")
    if shared_dims:
        parts.append(f"- matched on: {', '.join(sorted(shared_dims))}")
    else:
        parts.append("- no shared context dimension achieved between the two windows")
    if reference_result["missing_context"]:
        parts.append(f"- unavailable context: {', '.join(reference_result['missing_context'])}")
    if maintenance_before or maintenance_after:
        parts.append(f"- {maintenance_before + maintenance_after} declared maintenance window(s) overlap the evaluation periods (not excluded from counts)")
    if anomaly_overlap_after:
        parts.append(f"- {anomaly_overlap_after} statistical anomaly occurrence(s) during the verification period (not excluded)")
    return "\n".join(parts)


def _result(intervention_id: int, evidence_quality: str, reason: str, computed_at: str, reference_window: tuple[datetime, datetime] | None = None) -> dict[str, Any]:
    return {
        "intervention_id": intervention_id, "target_key": None, "instance_key": None, "equipment_type": None,
        "reference_start": reference_window[0].strftime(TIME_FORMAT) if reference_window else None,
        "reference_end": reference_window[1].strftime(TIME_FORMAT) if reference_window else None,
        "eligible_after_start": None, "after_end": None,
        "reference_sample_count": 0, "after_sample_count": 0,
        "reference_status": "unavailable", "after_status": "unavailable",
        "reference_level": "D", "after_level": "D",
        "matching_dimensions_used": [], "exclusion_summary": {},
        "evidence_quality": evidence_quality, "assumptions": [], "limitations": [reason],
        "reason": f"{evidence_quality} because: {reason}", "computed_at": computed_at,
    }


# ---------------------------------------------------------------------------
# Read-only debug/inspection CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import json

    from config.environment import get_config_db_path, get_machine_db_path

    parser = argparse.ArgumentParser(description="Phase 11.2 evidence-selection - read-only inspection CLI")
    parser.add_argument("--config-database", default=str(get_config_db_path()))
    parser.add_argument("--machine-database", default=str(get_machine_db_path()))
    parser.add_argument("--intervention-id", type=int, required=True)
    args = parser.parse_args()

    historian = DatabaseManager(db_path=args.machine_database)
    result = compute_verification_evidence(args.config_database, args.machine_database, historian, args.intervention_id)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
