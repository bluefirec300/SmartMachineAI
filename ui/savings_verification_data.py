from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path
from engine import savings_verification_domain as dom
from engine import savings_verification_evidence as ev
from engine.savings_verification_targets import (
    DEFAULT_STABILIZATION_DAYS,
    INTERVENTION_STATUS_COMPLETED,
    INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS,
    INTERVENTION_STATUS_VERIFICATION_PENDING,
    VERIFICATION_RESULT_INCONCLUSIVE,
    VERIFICATION_RESULT_REJECTED,
    VERIFICATION_RESULT_VERIFIED,
)

"""
Phase 11.5 - UI data-access layer for the engineer intervention/
verification workflow. Read/write separation is deliberate (item 19):

  - READS may query directly (mirrors ui/data_access.py's own
    established convention for get_anomalies()/get_opportunities() -
    presentation-layer joins against plants/energy_opportunities for
    display, not "domain logic").
  - WRITES delegate ENTIRELY to engine.savings_verification_domain's
    validated Phase 11.1 helpers (create_intervention/mark_implemented)
    - this module issues no INSERT/UPDATE of its own, and never sets
    VERIFICATION_PENDING/VERIFICATION_IN_PROGRESS/COMPLETED - those are
    exclusively Phase 11.4's orchestration layer's job.

No verification mathematics live here - stabilization boundaries reuse
engine.savings_verification_evidence's exact functions; nothing is
recalculated.
"""

CONFIG_DATABASE_PATH = get_config_db_path()
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

STATUS_LABELS = {
    "PLANNED": "Planned",
    "IMPLEMENTED": "Implemented",
    INTERVENTION_STATUS_VERIFICATION_PENDING: "Stabilizing / Awaiting Verification",
    INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS: "Verification In Progress",
    INTERVENTION_STATUS_COMPLETED: "Completed",
}

EVIDENCE_QUALITY_EXPLANATIONS = {
    "STRONG": "Both the before and after periods reached mature, well context-matched coverage - the strongest available evidence.",
    "CONTEXT_MATCHED": "Both periods reached real, comparable coverage - a solid but not the strongest possible match.",
    "LIMITED": "Evidence exists, but coverage or context matching is weak - not yet enough to support a defensible conclusion.",
    "INSUFFICIENT": "Not enough representative evidence exists yet to compare before/after conditions at all.",
}


def engineer_status_label(status: str, latest_outcome: str | None) -> str:
    """Presentation-only mapping - never changes backend vocabulary,
    never invents a new backend state (item 8)."""
    if status == INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS and latest_outcome == VERIFICATION_RESULT_INCONCLUSIVE:
        return "More Evidence Required"
    if status == INTERVENTION_STATUS_COMPLETED:
        if latest_outcome == VERIFICATION_RESULT_VERIFIED:
            return "Verified"
        if latest_outcome == VERIFICATION_RESULT_REJECTED:
            return "Not Verified"
        return "Completed"
    return STATUS_LABELS.get(status, status)


def _enrich(row: dict[str, Any]) -> dict[str, Any]:
    latest = dom.get_latest_verification_result(CONFIG_DATABASE_PATH, row["id"])
    row["latest_result"] = latest
    row["latest_outcome"] = latest["result"] if latest else None
    row["engineer_status"] = engineer_status_label(row["status"], row["latest_outcome"])
    return row


def get_interventions(status: str | None = None, plant_code: str | None = None, outcome: str | None = None) -> list[dict[str, Any]]:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    query = """
        SELECT si.*, p.code AS plant_code, p.name AS plant_name,
               eo.title AS opportunity_title, eo.category AS opportunity_category
        FROM savings_interventions si
        JOIN plants p ON p.id = si.plant_id
        LEFT JOIN energy_opportunities eo ON eo.id = si.opportunity_id
        WHERE 1 = 1
    """
    params: list[Any] = []
    if status:
        query += " AND si.status = ?"
        params.append(status)
    if plant_code:
        query += " AND p.code = ?"
        params.append(plant_code)
    query += " ORDER BY si.recorded_at DESC"

    try:
        rows = [dict(r) for r in connection.execute(query, params).fetchall()]
    finally:
        connection.close()

    rows = [_enrich(r) for r in rows]

    if outcome:
        rows = [r for r in rows if r["latest_outcome"] == outcome]

    return rows


def get_intervention(intervention_id: int) -> dict[str, Any] | None:
    row = dom.get_intervention(CONFIG_DATABASE_PATH, intervention_id)
    if row is None:
        return None

    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        plant = connection.execute("SELECT code, name FROM plants WHERE id = ?", (row["plant_id"],)).fetchone()
        opportunity = connection.execute("SELECT * FROM energy_opportunities WHERE id = ?", (row["opportunity_id"],)).fetchone()
    finally:
        connection.close()

    row["plant_code"] = plant["code"] if plant else None
    row["plant_name"] = plant["name"] if plant else None
    row["opportunity"] = dict(opportunity) if opportunity else None
    return _enrich(row)


def get_verification_history(intervention_id: int) -> list[dict[str, Any]]:
    return dom.list_verification_history(CONFIG_DATABASE_PATH, intervention_id)


def get_stabilization_info(intervention: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Reuses Phase 11.2's exact eligibility boundary
    (compute_after_window) - never a second stabilization formula."""
    now = now or datetime.now()

    if not intervention.get("implemented_at"):
        return {"implemented_at": None, "eligible_after_start": None, "eligible": False, "days_remaining": None}

    implemented_at = datetime.strptime(intervention["implemented_at"], TIME_FORMAT)
    stabilization_days = intervention.get("stabilization_days")
    if stabilization_days is None:
        stabilization_days = DEFAULT_STABILIZATION_DAYS.get(intervention.get("action_category"), 3)

    eligible_after_start = implemented_at + timedelta(days=stabilization_days)
    after_window = ev.compute_after_window(implemented_at, stabilization_days, now)

    days_remaining = None
    if after_window is None:
        remaining_seconds = (eligible_after_start - now).total_seconds()
        days_remaining = max(0, int(remaining_seconds // 86400) + (1 if remaining_seconds % 86400 > 0 else 0))

    return {
        "implemented_at": intervention["implemented_at"],
        "stabilization_days": stabilization_days,
        "eligible_after_start": eligible_after_start.strftime(TIME_FORMAT),
        "eligible": after_window is not None,
        "days_remaining": days_remaining,
    }


def get_active_intervention_for_opportunity(opportunity_id: int) -> dict[str, Any] | None:
    return dom.find_active_intervention(CONFIG_DATABASE_PATH, opportunity_id)


def record_and_implement_intervention(
    opportunity_id: int, plant_id: int, instance_key: str, action_category: str,
    action_description: str, recorded_by: str, expected_effect: str | None,
    implemented_at: datetime, stabilization_days: int,
) -> int:
    """The ONLY write path this page uses. Delegates entirely to Phase
    11.1's validated domain helpers - no direct SQL, and never sets any
    worker-owned lifecycle state (VERIFICATION_PENDING onward are
    exclusively Phase 11.4's job)."""
    intervention_id = dom.create_intervention(
        CONFIG_DATABASE_PATH, opportunity_id, plant_id, instance_key, action_category,
        action_description, recorded_by, expected_effect=expected_effect, stabilization_days=stabilization_days,
    )
    dom.mark_implemented(CONFIG_DATABASE_PATH, intervention_id, implemented_at=implemented_at)
    return intervention_id


def get_summary(interventions: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure aggregation over already-fetched, already-enriched rows -
    no new calculation semantics, no query of its own. Each intervention
    contributes AT MOST ONCE, via its latest (most recent) verification
    result only - a VERIFIED result is always the terminal COMPLETED
    outcome (Phase 11.4's own lifecycle matrix), so any earlier
    INCONCLUSIVE rows for the same intervention are structurally
    excluded from this sum, never double-counted (item 24)."""
    active = sum(1 for i in interventions if i["status"] != INTERVENTION_STATUS_COMPLETED)
    awaiting = sum(1 for i in interventions if i["status"] in (INTERVENTION_STATUS_VERIFICATION_PENDING, INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS))
    verified = [i for i in interventions if i["status"] == INTERVENTION_STATUS_COMPLETED and i["latest_outcome"] == VERIFICATION_RESULT_VERIFIED]
    more_evidence = sum(1 for i in interventions if i["status"] == INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS and i["latest_outcome"] == VERIFICATION_RESULT_INCONCLUSIVE)
    rejected = sum(1 for i in interventions if i["status"] == INTERVENTION_STATUS_COMPLETED and i["latest_outcome"] == VERIFICATION_RESULT_REJECTED)

    verified_savings_total = 0.0
    currency = None
    verified_without_cost_count = 0
    for i in verified:
        cost = i["latest_result"].get("verified_cost")
        if cost is not None:
            verified_savings_total += cost
            currency = i["latest_result"].get("verified_cost_currency") or currency
        else:
            verified_without_cost_count += 1

    return {
        "active": active,
        "awaiting_verification": awaiting,
        "verified": len(verified),
        "more_evidence_required": more_evidence,
        "rejected": rejected,
        "verified_savings_total": verified_savings_total,
        "verified_savings_currency": currency,
        # Distinguishes "no verified interventions exist yet" from "verified
        # interventions exist and genuinely sum to zero" (item 6) - the page
        # branches its KPI card wording on this count, not on the total alone.
        "verified_without_cost_count": verified_without_cost_count,
    }


def get_verified_savings_by_period(interventions: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """Pure aggregation over already-fetched, already-verified results,
    bucketed by verification_period_end - the real date the observed
    saving occurred - never evaluated_at (when the worker happened to
    run, which has no engineering meaning). This sums only already-
    final, already-persisted verified_cost values grouped by a real
    timestamp already on the row; it computes no new savings figure and
    performs no verification mathematics (item 23's dashboard-scope
    audit: 'may be implemented ONLY if persisted verified results have
    enough timestamp/period semantics to calculate it defensibly' -
    verification_period_end is exactly that semantics).

    Like get_summary(), each intervention contributes via its latest
    result only, so no double-counting across INCONCLUSIVE->VERIFIED
    history (item 24)."""
    now = now or datetime.now()
    this_month_total = 0.0
    this_year_total = 0.0
    this_month_count = 0
    this_year_count = 0
    currency = None
    for iv in interventions:
        if iv.get("latest_outcome") != VERIFICATION_RESULT_VERIFIED:
            continue
        latest = iv.get("latest_result")
        if not latest or latest.get("verified_cost") is None or not latest.get("verification_period_end"):
            continue
        period_end = datetime.strptime(latest["verification_period_end"], TIME_FORMAT)
        if period_end.year == now.year:
            this_year_total += latest["verified_cost"]
            this_year_count += 1
            currency = latest.get("verified_cost_currency") or currency
            if period_end.month == now.month:
                this_month_total += latest["verified_cost"]
                this_month_count += 1

    return {
        "this_month_total": this_month_total, "this_month_count": this_month_count,
        "this_year_total": this_year_total, "this_year_count": this_year_count,
        "currency": currency,
    }


def verified_saving_summary(intervention: dict[str, Any]) -> dict[str, str]:
    """Presentation-only resolution of the 'Verified Saving' figure for
    ONE intervention - distinguishes the different reasons a dollar
    figure isn't shown (item 5/6) instead of collapsing them all into
    one generic 'Unavailable'. Never recalculates anything - reads only
    the already-enriched latest_outcome/latest_result fields."""
    outcome = intervention.get("latest_outcome")
    latest = intervention.get("latest_result")

    if outcome == VERIFICATION_RESULT_VERIFIED and latest:
        if latest.get("verified_cost") is not None:
            return {
                "display": f"{latest.get('verified_cost_currency') or 'RM'} {latest['verified_cost']:,.2f}",
                "explanation": "Measured saving for this verification period only - never annualized, never estimated.",
            }
        return {
            "display": "Tariff unavailable",
            "explanation": "The energy saving was verified, but no tariff was available for this period to convert it to a cost figure.",
        }
    if outcome == VERIFICATION_RESULT_REJECTED:
        return {
            "display": "Not applicable",
            "explanation": "The most recent verification evaluation did not support a positive saving claim (Not Verified).",
        }
    if outcome == VERIFICATION_RESULT_INCONCLUSIVE:
        return {
            "display": "Waiting for verification",
            "explanation": "More evidence is required before a verified result can be reached - the system re-checks automatically.",
        }
    return {
        "display": "Waiting for verification",
        "explanation": "No evaluation has run yet - this begins automatically once stabilization has elapsed.",
    }
