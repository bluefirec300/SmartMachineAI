from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from database.database import DatabaseManager
from engine import baseline_engine as base
from engine import savings_verification_domain as dom
from engine import savings_verification_evidence as ev
from engine.energy_kpi_engine import effective_tariff
from engine.energy_tariff import tariff_provenance_label
from engine.savings_verification_targets import (
    VERIFICATION_RESULT_INCONCLUSIVE,
    VERIFICATION_RESULT_REJECTED,
    VERIFICATION_RESULT_VERIFIED,
)

"""
Phase 11.3 - deterministic verified energy/cost savings calculation.
First phase permitted to compute a financial verification claim.

CALCULATE is kept strictly separate from PERSIST (item 11):
calculate_verified_savings() is a pure, read-only function - it never
writes anywhere. persist_verification_result() is a distinct, always-
explicit second step that appends one row to
savings_verification_results (never updates/deletes a prior one) and
never touches savings_interventions.status.

Reuses, unmodified: engine.savings_verification_evidence's window/
context-matching (Phase 11.2), engine.baseline_engine's robust
statistics (_summarize), classification (_classify), day-diversity
(_distinct_days), maintenance-interval reconstruction
(_maintenance_intervals), and raw series fetch (_fetch_target_series).
Reuses engine.energy_kpi_engine.effective_tariff() for every tariff
lookup - never a second tariff calculator. Does NOT reuse
engine.anomaly_engine.bucket_excess_energy_cost() - that function
clamps to max(delta, 0), which is correct for Phase 9's "excess is
never negative" semantics but WRONG here: Phase 11.3 must preserve a
negative delta (the intervention made things worse) exactly as
computed, never clamped to zero.

NORMALIZATION (item 4): "context_matched_power_median" - before/after
values are both a robust MEDIAN POWER (kW) under Phase 8's own
context-matched conditions (same load/ambient/hour/production-state/
product bucket, as applicable to the target's equipment type) - this
sidesteps "different operating duration" pitfalls entirely, since kW
is duration-independent by construction, and Phase 8's existing
context-matching already provides the "comparable conditions" control
this phase needs, including for production equipment (product/
category/running-state matching). An energy-per-batch/per-quantity
normalization was considered and deliberately NOT built for this
initial implementation - see the Phase 11 status doc's "known
limitations" for why.
"""

TIME_FORMAT = base.TIME_FORMAT

MIN_POWER_DENOMINATOR_KW = 0.5  # below this, a percentage is not mathematically meaningful - reported as None, never a divide-by-near-zero artifact


# ---------------------------------------------------------------------------
# Maintenance exclusion + reclassification (item 2/12) - reuses the SAME
# Phase 8 primitives that produced the original evidence, applied to
# the SURVIVING (non-maintenance-contaminated) bucket subset.
# ---------------------------------------------------------------------------

def _exclude_maintenance_contaminated(
    matched_bucket_starts: list[datetime], maintenance_intervals: list[tuple[datetime, datetime]],
) -> tuple[list[datetime], int]:
    """Returns (surviving_buckets, excluded_count)."""
    surviving = [b for b in matched_bucket_starts if not base._excluded(b, maintenance_intervals)]
    return surviving, len(matched_bucket_starts) - len(surviving)


def _recompute_window_after_exclusion(
    window_result: dict[str, Any], surviving_buckets: list[datetime], surviving_values: list[float],
) -> dict[str, Any]:
    """Re-derives status/confidence/median/... from the SURVIVING
    (post-maintenance-exclusion) bucket subset only, reusing Phase 8's
    own _summarize/_classify/_distinct_days exactly as the original
    evidence computation did. diversity_dimensions and level are
    reused from the original (pre-exclusion) result - removing a
    handful of maintenance-contaminated buckets does not change WHICH
    context dimensions were achievable, only how many samples survive
    within them; recomputing that would require re-fetching the full
    context-by-bucket map a second time for no material benefit."""
    if not surviving_values:
        empty = base._empty_window_result(window_result["history_start"], window_result["history_end"])
        return empty

    distinct_days = base._distinct_days(surviving_buckets)
    status, confidence = base._classify(len(surviving_values), distinct_days, window_result["level"], window_result["diversity_dimensions"])

    if status == "unavailable":
        empty = base._empty_window_result(window_result["history_start"], window_result["history_end"])
        empty["representative_sample_count"] = len(surviving_values)
        empty["distinct_days"] = distinct_days
        return empty

    median, range_low, range_high, mad = base._summarize(surviving_values)
    return {
        "status": status, "confidence": confidence, "level": window_result["level"],
        "median": median, "range_low": range_low, "range_high": range_high, "mad": mad,
        "representative_sample_count": len(surviving_values), "raw_sample_count": window_result["raw_sample_count"],
        "distinct_days": distinct_days, "diversity_dimensions": window_result["diversity_dimensions"],
        "context_used": window_result["context_used"], "missing_context": window_result["missing_context"],
        "history_start": window_result["history_start"], "history_end": window_result["history_end"],
        "matched_bucket_starts": sorted(surviving_buckets),
    }


# ---------------------------------------------------------------------------
# Main calculation - PURE, read-only.
# ---------------------------------------------------------------------------

def calculate_verified_savings(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    intervention_id: int, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)

    evidence = ev.compute_verification_evidence(config_database_path, machine_database_path, historian, intervention_id, now=now)

    result = _base_result(intervention_id, evidence, now_text)

    if evidence["evidence_quality"] not in (ev.EVIDENCE_STRONG, ev.EVIDENCE_CONTEXT_MATCHED):
        # Item 9 - LIMITED/INSUFFICIENT can never support VERIFIED/REJECTED,
        # regardless of what a raw calculation might show.
        result["verification_result"] = VERIFICATION_RESULT_INCONCLUSIVE
        result["reason"] = f"INCONCLUSIVE - evidence quality is {evidence['evidence_quality']}, below the minimum ({ev.EVIDENCE_CONTEXT_MATCHED} or {ev.EVIDENCE_STRONG}) required to support a savings claim.\n{evidence['reason']}"
        result["limitations"] = list(evidence["limitations"])
        return result

    intervention = dom.get_intervention(config_database_path, intervention_id)
    target = ev.resolve_target_for_intervention(config_database_path, intervention)

    reference_window = (datetime.strptime(evidence["reference_start"], TIME_FORMAT), datetime.strptime(evidence["reference_end"], TIME_FORMAT))
    after_window = (datetime.strptime(evidence["eligible_after_start"], TIME_FORMAT), datetime.strptime(evidence["after_end"], TIME_FORMAT))

    # Re-fetch full target series for both windows (reused, not
    # duplicated, fetch logic) to get per-bucket VALUES for exactly the
    # matched bucket timestamps Phase 11.2/the additive change exposed.
    reference_series, _ = base._fetch_target_series(target, historian, config_database_path, reference_window[0], reference_window[1])
    after_series, _ = base._fetch_target_series(target, historian, config_database_path, after_window[0], after_window[1])

    reference_maintenance = base._maintenance_intervals(config_database_path, intervention["instance_key"])
    after_maintenance = reference_maintenance  # same equipment, same declared maintenance log - one lookup is sufficient and consistent

    # Reconstruct the ORIGINAL (pre-exclusion) window results' matched
    # bucket lists by recomputing them exactly as evidence.py did -
    # cheapest correct way to get them without changing evidence.py's
    # own return contract.
    representative_context_at = min(after_window[1], now)
    current_context = base._current_context(target, historian, config_database_path, representative_context_at)
    reference_result = base.compute_window_baseline(target, historian, config_database_path, reference_window[0], reference_window[1], current_context)
    after_result = base.compute_window_baseline(target, historian, config_database_path, after_window[0], after_window[1], current_context)

    reference_surviving_buckets, reference_excluded = _exclude_maintenance_contaminated(reference_result["matched_bucket_starts"], reference_maintenance)
    after_surviving_buckets, after_excluded = _exclude_maintenance_contaminated(after_result["matched_bucket_starts"], after_maintenance)

    reference_surviving_values = [reference_series[b] for b in reference_surviving_buckets if b in reference_series]
    after_surviving_values = [after_series[b] for b in after_surviving_buckets if b in after_series]

    reference_adjusted = _recompute_window_after_exclusion(reference_result, reference_surviving_buckets, reference_surviving_values)
    after_adjusted = _recompute_window_after_exclusion(after_result, after_surviving_buckets, after_surviving_values)

    adjusted_shared_dims = set(reference_adjusted["context_used"].keys()) & set(after_adjusted["context_used"].keys())
    adjusted_evidence_quality = ev.classify_evidence_quality(reference_adjusted, after_adjusted, adjusted_shared_dims, target)

    result["exclusion_summary"] = dict(evidence["exclusion_summary"])
    result["exclusion_summary"]["maintenance_contaminated_reference_buckets_excluded"] = reference_excluded
    result["exclusion_summary"]["maintenance_contaminated_after_buckets_excluded"] = after_excluded
    result["evidence_quality_after_exclusion"] = adjusted_evidence_quality
    result["usable_reference_sample_count"] = len(reference_surviving_values)
    result["usable_after_sample_count"] = len(after_surviving_values)

    if adjusted_evidence_quality not in (ev.EVIDENCE_STRONG, ev.EVIDENCE_CONTEXT_MATCHED):
        result["verification_result"] = VERIFICATION_RESULT_INCONCLUSIVE
        result["reason"] = (
            f"INCONCLUSIVE - after excluding {reference_excluded + after_excluded} maintenance-contaminated matched "
            f"bucket(s), evidence quality degraded to {adjusted_evidence_quality}, below the minimum required to "
            "support a savings claim."
        )
        result["limitations"] = list(evidence["limitations"]) + [
            f"Maintenance exclusion reduced usable evidence: {len(reference_surviving_values)} reference / "
            f"{len(after_surviving_values)} verification period(s) remained usable."
        ]
        return result

    # --- Normalization + energy/cost calculation ---
    before_normalized = reference_adjusted["median"]
    after_normalized = after_adjusted["median"]

    result["normalization_method"] = "context_matched_power_median"
    result["before_basis"] = before_normalized
    result["after_basis"] = after_normalized
    result["basis_unit"] = "kW"

    if abs(before_normalized) < MIN_POWER_DENOMINATOR_KW:
        result["energy_saving_percentage"] = None
        result["limitations"] = result.get("limitations", []) + [
            f"Reference median power ({before_normalized:.3f} kW) is too close to zero for a meaningful percentage - reported as Unavailable, not computed."
        ]
    else:
        result["energy_saving_percentage"] = round((before_normalized - after_normalized) / before_normalized * 100, 2)

    # Absolute energy/cost - interval-integrated per surviving AFTER
    # bucket against the (post-exclusion) reference median, tariff
    # applied per-bucket's own effective date. NEVER clamped to zero -
    # a negative value here is valid evidence the intervention made
    # things worse.
    total_energy_kwh = 0.0
    total_cost = 0.0
    priced_bucket_count = 0
    currency = None
    tariff_provenance = None
    for bucket_start in after_surviving_buckets:
        actual_kw = after_series.get(bucket_start)
        if actual_kw is None:
            continue
        kw_delta = before_normalized - actual_kw  # positive = used LESS than the comparable reference (a real saving)
        kwh_delta = kw_delta * (target.aggregation_minutes / 60.0)
        total_energy_kwh += kwh_delta

        tariff = effective_tariff(config_database_path, intervention["plant_id"], bucket_start)
        rate = tariff.get("energy_rate") if tariff else None
        if rate is not None:
            total_cost += kwh_delta * rate
            currency = tariff.get("currency")
            # Phase 18.1a.1 - same last-bucket-wins tracking already
            # used for currency above, not a new aggregation policy.
            tariff_provenance = tariff_provenance_label(tariff)
            priced_bucket_count += 1

    result["absolute_energy_saving_kwh"] = round(total_energy_kwh, 4)
    result["energy_saving_basis_bucket_count"] = len(after_surviving_buckets)

    if priced_bucket_count == 0:
        result["cost_saving"] = None
        result["cost_currency"] = None
        result["tariff_provenance"] = None
        result["limitations"] = result.get("limitations", []) + ["No tariff was configured for any contributing bucket - cost saving is Unavailable; energy saving remains valid."]
    else:
        result["cost_saving"] = round(total_cost, 4)
        result["cost_currency"] = currency
        result["tariff_provenance"] = tariff_provenance
        if priced_bucket_count < len(after_surviving_buckets):
            result["limitations"] = result.get("limitations", []) + [
                f"Cost reflects {priced_bucket_count} of {len(after_surviving_buckets)} contributing buckets with a configured tariff - a partial total, not silently presented as complete."
            ]

    # --- VERIFIED / REJECTED determination (item 8) ---
    if result["absolute_energy_saving_kwh"] > 0:
        result["verification_result"] = VERIFICATION_RESULT_VERIFIED
        result["reason"] = f"VERIFIED - {result['absolute_energy_saving_kwh']:.2f} kWh observed saving across {len(after_surviving_buckets)} comparable, maintenance-clean verification period(s), {adjusted_evidence_quality} evidence."
    else:
        result["verification_result"] = VERIFICATION_RESULT_REJECTED
        result["reason"] = f"REJECTED - observed change was {result['absolute_energy_saving_kwh']:.2f} kWh (not a positive saving) across {len(after_surviving_buckets)} comparable, maintenance-clean verification period(s), {adjusted_evidence_quality} evidence. This does not mean a software failure - the measured result did not support the savings claim."

    result["limitations"] = result.get("limitations", [])
    result["assumptions"] = list(evidence["assumptions"]) + [
        "Normalization is context-matched median power (kW), not kWh/day or kWh/batch - duration-independent by construction.",
        "Percentage saving compares before/after MEDIANS (a robust typical-change figure); absolute energy/cost saving is a full "
        "interval-integration across every usable AFTER bucket (a real total) - these are two distinct, complementary figures.",
        "Phase 9 statistical anomaly occurrences were not excluded from the calculation basis - only maintenance windows and fault "
        "periods (already excluded upstream) are treated as invalid data.",
    ]

    return result


def _base_result(intervention_id: int, evidence: dict[str, Any], now_text: str) -> dict[str, Any]:
    return {
        "intervention_id": intervention_id,
        "evaluated_at": now_text,
        "reference_start": evidence["reference_start"], "reference_end": evidence["reference_end"],
        "eligible_after_start": evidence["eligible_after_start"], "after_end": evidence["after_end"],
        "evidence_quality": evidence["evidence_quality"],
        "evidence_quality_after_exclusion": None,
        "matching_dimensions_used": evidence["matching_dimensions_used"],
        "exclusion_summary": dict(evidence["exclusion_summary"]),
        "usable_reference_sample_count": None, "usable_after_sample_count": None,
        "normalization_method": None, "before_basis": None, "after_basis": None, "basis_unit": None,
        "absolute_energy_saving_kwh": None, "energy_saving_percentage": None, "energy_saving_basis_bucket_count": None,
        "cost_saving": None, "cost_currency": None, "tariff_provenance": None,
        "estimated_saving": None, "realization_ratio": None,
        "verification_result": VERIFICATION_RESULT_INCONCLUSIVE,
        "assumptions": list(evidence.get("assumptions", [])),
        "limitations": list(evidence.get("limitations", [])),
        "reason": evidence.get("reason", ""),
    }


# ---------------------------------------------------------------------------
# Estimated-vs-verified comparison metadata (item 7) - a pure, read-
# only comparison utility. Never writes to energy_opportunities. Never
# called automatically.
# ---------------------------------------------------------------------------

def compute_realization_ratio(estimated_saving: float | None, verified_saving: float | None) -> float | None:
    if estimated_saving is None or verified_saving is None or estimated_saving <= 0:
        return None
    return round(verified_saving / estimated_saving, 4)


def attach_estimated_comparison(config_database_path: str | Path, calculation_result: dict[str, Any], opportunity_id: int) -> dict[str, Any]:
    """Read-only enrichment - looks up the linked opportunity's
    (currently always-Unavailable, per Phase 10) estimated potential
    saving purely for comparison metadata. Never modifies
    energy_opportunities."""
    import sqlite3

    connection = sqlite3.connect(config_database_path)
    try:
        row = connection.execute("SELECT estimated_potential_saving_period FROM energy_opportunities WHERE id = ?", (opportunity_id,)).fetchone()
    finally:
        connection.close()

    estimated = row[0] if row else None
    calculation_result = dict(calculation_result)
    calculation_result["estimated_saving"] = estimated
    calculation_result["realization_ratio"] = compute_realization_ratio(estimated, calculation_result.get("cost_saving"))
    return calculation_result


# ---------------------------------------------------------------------------
# Explicit, separate persistence step (item 11) - never auto-invoked by
# calculate_verified_savings(). Appends to savings_verification_results
# (append-only, never updates a prior row). Never touches
# savings_interventions.status.
# ---------------------------------------------------------------------------

def persist_verification_result(config_database_path: str | Path, calculation_result: dict[str, Any]) -> int:
    import json

    evidence_payload = {
        "matching_dimensions_used": calculation_result["matching_dimensions_used"],
        "exclusion_summary": calculation_result["exclusion_summary"],
        "usable_reference_sample_count": calculation_result["usable_reference_sample_count"],
        "usable_after_sample_count": calculation_result["usable_after_sample_count"],
        "normalization_method": calculation_result["normalization_method"],
        "before_basis": calculation_result["before_basis"], "after_basis": calculation_result["after_basis"],
        "basis_unit": calculation_result["basis_unit"],
        "energy_saving_percentage": calculation_result["energy_saving_percentage"],
        "energy_saving_basis_bucket_count": calculation_result["energy_saving_basis_bucket_count"],
        "estimated_saving": calculation_result.get("estimated_saving"),
        "realization_ratio": calculation_result.get("realization_ratio"),
    }

    return dom.record_verification_result(
        config_database_path, calculation_result["intervention_id"], calculation_result["verification_result"],
        evidence_json=json.dumps(evidence_payload, default=str),
        assumptions_json=json.dumps(calculation_result["assumptions"]),
        limitations_json=json.dumps(calculation_result["limitations"]),
        reference_period_start=calculation_result["reference_start"], reference_period_end=calculation_result["reference_end"],
        verification_period_start=calculation_result["eligible_after_start"], verification_period_end=calculation_result["after_end"],
        verification_method="context_matched_power_median_interval_integration",
        reason=calculation_result["reason"],
        verified_energy_kwh=calculation_result["absolute_energy_saving_kwh"],
        verified_cost=calculation_result["cost_saving"],
        verified_cost_currency=calculation_result["cost_currency"],
        tariff_provenance=calculation_result["tariff_provenance"],
        confidence=calculation_result["evidence_quality_after_exclusion"] or calculation_result["evidence_quality"],
        evaluated_at=datetime.strptime(calculation_result["evaluated_at"], TIME_FORMAT),
    )


# ---------------------------------------------------------------------------
# Read-only debug/inspection CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import json

    from config.environment import get_config_db_path, get_machine_db_path

    parser = argparse.ArgumentParser(description="Phase 11.3 savings-verification calculation - read-only inspection CLI")
    parser.add_argument("--config-database", default=str(get_config_db_path()))
    parser.add_argument("--machine-database", default=str(get_machine_db_path()))
    parser.add_argument("--intervention-id", type=int, required=True)
    parser.add_argument("--persist", action="store_true", help="Explicitly persist the result (append-only) - never done by default.")
    args = parser.parse_args()

    historian = DatabaseManager(db_path=args.machine_database)
    result = calculate_verified_savings(args.config_database, args.machine_database, historian, args.intervention_id)
    print(json.dumps(result, indent=2, default=str))

    if args.persist:
        row_id = persist_verification_result(args.config_database, result)
        print(f"Persisted as savings_verification_results.id={row_id}")


if __name__ == "__main__":
    main()
