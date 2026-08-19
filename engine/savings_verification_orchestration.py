from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from database.database import DatabaseManager
from engine import savings_verification_domain as dom
from engine import savings_verification_engine as calc
from engine import savings_verification_evidence as ev
from engine.savings_verification_targets import (
    INTERVENTION_STATUS_COMPLETED,
    INTERVENTION_STATUS_IMPLEMENTED,
    INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS,
    INTERVENTION_STATUS_VERIFICATION_PENDING,
    VERIFICATION_RESULT_REJECTED,
    VERIFICATION_RESULT_VERIFIED,
)

"""
Phase 11.4 - background orchestration: eligibility, materially-new-
evidence gating, retry policy, and lifecycle transitions. NO savings
mathematics live here - engine.savings_verification_evidence (Phase
11.2) and engine.savings_verification_engine (Phase 11.3) are used
exactly as they already are, unmodified.

Lifecycle ownership, kept non-contradictory with Phase 11.1's own
design intent (see savings_verification_targets.py's module docstring):
  - savings_interventions.status is a COARSE workflow-phase rollup,
    owned entirely by this module's transitions.
  - savings_verification_results.result is the fine-grained, append-
    only OUTCOME of one evaluation attempt - this module only ever
    INSERTs via engine.savings_verification_engine.persist_verification_result()
    (itself calling Phase 11.1's append-only record_verification_result()),
    never UPDATEs/DELETEs a prior row.

PLANNED -> not touched at all (implemented_at is None, nothing to evaluate).
IMPLEMENTED -> VERIFICATION_PENDING the first time the worker sees it
    (a safe, evidence-independent transition - just means "now under
    the verification workflow's watch").
VERIFICATION_PENDING -> VERIFICATION_IN_PROGRESS only once stabilization
    has genuinely elapsed AND a real evaluation attempt begins.
VERIFICATION_IN_PROGRESS -> stays VERIFICATION_IN_PROGRESS across any
    number of INCONCLUSIVE evaluations (item 4 - INCONCLUSIVE is never
    terminal).
VERIFICATION_IN_PROGRESS -> COMPLETED ONLY after a VERIFIED or REJECTED
    result has been SUCCESSFULLY persisted (persist-then-transition
    ordering - a failed persist call raises before the transition code
    ever runs, so the lifecycle can never be "falsely completed").
COMPLETED -> terminal; filtered out before any evaluation is attempted.
"""

SKIP_NOT_ELIGIBLE = "not_eligible"
SKIP_NO_NEW_EVIDENCE = "no_new_evidence"


def _candidate_interventions(config_database_path: str | Path) -> list[dict[str, Any]]:
    """Non-terminal, genuinely implemented interventions - the coarse
    pre-filter. Stabilization timing is NOT checked here (it depends on
    'now' at call time, which is per-cycle, not a static DB property)."""
    return [
        i for i in dom.list_interventions(config_database_path)
        if i["status"] != INTERVENTION_STATUS_COMPLETED and i["implemented_at"] is not None
    ]


def has_materially_new_evidence(evidence: dict[str, Any], last_result: dict[str, Any] | None) -> bool:
    """Deterministic comparison against the most recently PERSISTED
    evaluation's stored evidence metadata (item 5) - never worker run
    count or elapsed clock time alone.

    Compares Phase 11.2's raw (pre-maintenance-exclusion) sample counts
    against Phase 11.3's persisted (post-exclusion) 'usable_*' counts -
    a deliberate, disclosed simplification: these are slightly
    different quantities, but both grow together in practice (more raw
    historian data -> more matched buckets -> more usable-after-
    exclusion buckets, since maintenance frequency doesn't change per
    cycle), and using them avoids re-running Phase 11.3's heavier
    maintenance-recomputation just to decide whether a re-evaluation is
    warranted at all."""
    if last_result is None:
        return True

    last_meta = json.loads(last_result["evidence_json"]) if last_result.get("evidence_json") else {}

    if (evidence.get("after_sample_count") or 0) > (last_meta.get("usable_after_sample_count") or 0):
        return True
    if (evidence.get("reference_sample_count") or 0) != (last_meta.get("usable_reference_sample_count") or 0):
        return True
    if evidence.get("evidence_quality") != last_result.get("confidence"):
        return True

    return False


def evaluate_intervention(
    config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager,
    intervention_id: int, now: datetime | None = None,
) -> dict[str, Any]:
    """Processes ONE intervention through eligibility -> materially-new-
    evidence gating -> calculate -> persist -> lifecycle transition.
    Returns a plain outcome dict for expected paths (not eligible, no
    new evidence, evaluated); RAISES for genuine failures (calculation/
    persistence errors) - the caller (run_cycle) owns the try/except
    that isolates one intervention's failure from the rest of the cycle."""
    now = now or datetime.now()

    intervention = dom.get_intervention(config_database_path, intervention_id)
    if intervention is None:
        return {"intervention_id": intervention_id, "action": "skipped", "skip_category": SKIP_NOT_ELIGIBLE, "reason": "no such intervention"}

    if intervention["status"] == INTERVENTION_STATUS_COMPLETED:
        return {"intervention_id": intervention_id, "action": "skipped", "skip_category": SKIP_NOT_ELIGIBLE, "reason": "already COMPLETED (terminal)"}

    if intervention["implemented_at"] is None:
        return {"intervention_id": intervention_id, "action": "skipped", "skip_category": SKIP_NOT_ELIGIBLE, "reason": "still PLANNED - not implemented yet"}

    if intervention["status"] == INTERVENTION_STATUS_IMPLEMENTED:
        dom.update_intervention_status(config_database_path, intervention_id, INTERVENTION_STATUS_VERIFICATION_PENDING)
        intervention["status"] = INTERVENTION_STATUS_VERIFICATION_PENDING

    # Reuses the EXACT SAME boundary Phase 11.2 already defines
    # (implemented_at + stabilization_days) - never a second copy of
    # this arithmetic.
    evidence = ev.compute_verification_evidence(config_database_path, machine_database_path, historian, intervention_id, now=now)
    if evidence["eligible_after_start"] is None:
        return {"intervention_id": intervention_id, "action": "skipped", "skip_category": SKIP_NOT_ELIGIBLE, "reason": "stabilization not finished / future implemented_at"}

    last_result = dom.get_latest_verification_result(config_database_path, intervention_id)
    if not has_materially_new_evidence(evidence, last_result):
        return {"intervention_id": intervention_id, "action": "skipped", "skip_category": SKIP_NO_NEW_EVIDENCE, "reason": "no materially new evidence since the last evaluation"}

    if intervention["status"] == INTERVENTION_STATUS_VERIFICATION_PENDING:
        dom.update_intervention_status(config_database_path, intervention_id, INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS)

    try:
        result = calc.calculate_verified_savings(config_database_path, machine_database_path, historian, intervention_id, now=now)
    except Exception as error:
        raise RuntimeError(
            f"calculation stage failed for intervention_id={intervention_id} "
            f"(opportunity_id={intervention['opportunity_id']}, instance_key={intervention['instance_key']}): {error}"
        ) from error

    # Enrichment, not a Phase 11.3 change: when the calculation is
    # gated at its FIRST evidence-quality check (before the maintenance-
    # exclusion recomputation ever runs), calculate_verified_savings()
    # correctly leaves usable_reference/after_sample_count as None -
    # that stage genuinely never executed. Left as None, a future
    # has_materially_new_evidence() comparison against this persisted
    # row would treat "unknown" as "zero", making every subsequent
    # cycle look like materially new evidence even when nothing
    # changed - exactly the repeated-duplicate-INCONCLUSIVE-row problem
    # this phase must prevent. Filling in Phase 11.2's own raw (pre-
    # exclusion) counts here - already computed, already in scope -
    # keeps the comparison meaningful without touching Phase 11.3's
    # calculation logic at all.
    if result["usable_reference_sample_count"] is None and result["usable_after_sample_count"] is None:
        result = dict(result)
        result["usable_reference_sample_count"] = evidence["reference_sample_count"]
        result["usable_after_sample_count"] = evidence["after_sample_count"]

    try:
        row_id = calc.persist_verification_result(config_database_path, result)
    except Exception as error:
        raise RuntimeError(
            f"persistence stage failed for intervention_id={intervention_id} "
            f"(opportunity_id={intervention['opportunity_id']}, instance_key={intervention['instance_key']}): {error}"
        ) from error

    # Lifecycle transition ONLY after persistence has genuinely
    # succeeded (item 12's ordering requirement) - a raised exception
    # above means this line is never reached, so the lifecycle can
    # never be left "falsely completed".
    if result["verification_result"] in (VERIFICATION_RESULT_VERIFIED, VERIFICATION_RESULT_REJECTED):
        dom.update_intervention_status(config_database_path, intervention_id, INTERVENTION_STATUS_COMPLETED)

    return {"intervention_id": intervention_id, "action": "evaluated", "result": result["verification_result"], "verification_result_id": row_id}


def run_cycle(config_database_path: str | Path, machine_database_path: str | Path, historian: DatabaseManager, now: datetime | None = None) -> dict[str, Any]:
    """One full pass over every candidate intervention. Each
    intervention's processing is individually failure-isolated - one
    exception never aborts the rest of the cycle (item 9)."""
    now = now or datetime.now()

    summary = {
        "evaluated": 0, "persisted": 0, "verified": 0, "rejected": 0, "inconclusive": 0,
        "skipped_no_new_evidence": 0, "skipped_not_eligible": 0, "failed": 0,
    }

    candidates = _candidate_interventions(config_database_path)
    for intervention in candidates:
        try:
            outcome = evaluate_intervention(config_database_path, machine_database_path, historian, intervention["id"], now=now)
        except Exception as error:
            summary["failed"] += 1
            print(f"savings_verification_worker: {error}")
            continue

        if outcome["action"] == "evaluated":
            summary["evaluated"] += 1
            summary["persisted"] += 1
            summary[outcome["result"].lower()] += 1
        elif outcome["skip_category"] == SKIP_NO_NEW_EVIDENCE:
            summary["skipped_no_new_evidence"] += 1
        else:
            summary["skipped_not_eligible"] += 1

    # eligible_interventions = every candidate that passed the coarse +
    # stabilization gate, whether or not it then had new evidence to
    # evaluate - a natural derived count, not a second tracked counter.
    summary["eligible_interventions"] = summary["evaluated"] + summary["skipped_no_new_evidence"]

    return summary
