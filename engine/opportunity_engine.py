from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from database.database import DatabaseManager
from engine import anomaly_engine as anomaly
from engine import baseline_engine as base
from engine.energy_kpi_engine import TIME_FORMAT, effective_tariff
from engine.energy_tariff import tariff_provenance_label
from engine.opportunity_targets import (
    SAVING_BASIS_UNAVAILABLE,
    STATUS_DISMISSED,
    STATUS_NEW,
    DISMISSAL_REASONS,
    OpportunityRule,
)

"""
Phase 10 - Energy Opportunity Engine. Deterministic only (Rule 4 - no
LLM). Consumes Phase 9's PERSISTED anomaly rows directly - never
recomputes Phase 9's excess-energy/cost figures, never rescans the
historian.

Per your Phase 10 corrections:
  - A context-matched (Level A/B) Phase 9 baseline makes the OBSERVED
    excess more CREDIBLE, but never automatically RECOVERABLE. Every
    rule in the initial registry declares saving_basis="unavailable" -
    Potential Saving is genuinely Unavailable for every opportunity
    this build can produce, and that is shown explicitly, never hidden
    or silently defaulted to the observed figure.
  - Annualization requires either a configured schedule (not yet
    supported - none configured anywhere in this database) or real
    recurrence+duration evidence (>= MIN_OCCURRENCES_FOR_ANNUALIZATION
    distinct anomaly occurrences AND >= MIN_OBSERVATION_SPAN_DAYS of
    span) - two occurrences alone is never enough.
  - Priority ("Engineering Investigation Priority", not a savings
    ranking) keeps a potential-saving component (weight x3, zero when
    unavailable) SEPARATE from an observed-impact component (weight
    x1, always populated from real observed cost) - an opportunity
    with unknown potential saving can never out-rank one with a real
    computed saving purely on observed cost.
"""

TIME_FORMAT = base.TIME_FORMAT

MIN_OCCURRENCES_FOR_ANNUALIZATION = 5
MIN_OBSERVATION_SPAN_DAYS_FOR_ANNUALIZATION = 30

_BASELINE_CONFIDENCE_SCORE = {"High": 3, "Medium": 2, "Low": 1, "Insufficient": 0}
_SEVERITY_SCORE = {"INFORMATION": 0, "ATTENTION": 1, "WARNING": 2, "HIGH": 3}
_DIFFICULTY_SCORE = {"LOW": 1, "MEDIUM": 0, "HIGH": -1, "UNKNOWN": 0}

# Financial bucket thresholds (RM) - used identically for the
# potential-saving component (when available) and the observed-impact
# component (always available for a qualifying opportunity). Tunable
# judgment, disclosed - not physics.
_FINANCIAL_BUCKETS = (20.0, 100.0, 500.0)  # -> component 0/1/2/3

PRIORITY_HIGH_THRESHOLD = 10
PRIORITY_MEDIUM_THRESHOLD = 5


# ---------------------------------------------------------------------------
# Qualification - reads Phase 9's anomalies table directly, never
# recomputes excess energy/cost.
# ---------------------------------------------------------------------------

def _fetch_source_anomalies(config_database_path: str | Path, plant_id: int, source_anomaly_rule_key: str) -> list[dict[str, Any]]:
    """OPEN + RESOLVED anomalies for this exact Phase 9 rule/plant with a
    real, positive Phase-9-computed excess energy figure - the hard
    qualification gate (item: energy_relevant rules only, and only
    when Phase 9 actually populated a number, never fabricated here)."""
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM anomalies WHERE plant_id = ? AND rule_key = ? "
            "AND estimated_excess_energy_kwh IS NOT NULL AND estimated_excess_energy_kwh > 0 "
            "ORDER BY first_detected ASC",
            (plant_id, source_anomaly_rule_key),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def _anomalies_by_id(config_database_path: str | Path, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(f"SELECT * FROM anomalies WHERE id IN ({placeholders})", ids).fetchall()
        return {r["id"]: dict(r) for r in rows}
    finally:
        connection.close()


def _predominantly_non_production(config_database_path: str | Path, plant_code: str, anomaly_row: dict[str, Any]) -> bool:
    """Point check at the anomaly's own last_bucket_start - reuses
    Phase 8's production-state lookup, no new instrumentation."""
    if not anomaly_row.get("last_bucket_start"):
        return False
    try:
        t = datetime.strptime(anomaly_row["last_bucket_start"], TIME_FORMAT)
    except ValueError:
        return False
    state = base._plant_production_state_at(config_database_path, plant_code, [t])
    return state.get(t) == "non_production"


# ---------------------------------------------------------------------------
# Confidence - evidence quality of the FINDING, independent of whether
# a saving figure could be derived.
# ---------------------------------------------------------------------------

def compute_confidence(representative_anomaly: dict[str, Any]) -> str:
    base_score = _BASELINE_CONFIDENCE_SCORE.get(representative_anomaly.get("baseline_confidence"), 0)
    if representative_anomaly.get("baseline_level") == "C":
        base_score -= 1
    base_score = max(0, min(3, base_score))
    return {3: "High", 2: "Medium", 1: "Low", 0: "Insufficient"}[base_score]


# ---------------------------------------------------------------------------
# Potential saving (item 1's correction) - every rule in this build
# declares SAVING_BASIS_UNAVAILABLE; the dispatch structure exists for
# forward-compatibility (a future rule with a configured engineering
# target, or a properly-built throughput-based drift calculation)
# without needing a schema change here.
# ---------------------------------------------------------------------------

def compute_potential_saving(rule: OpportunityRule) -> tuple[str, float | None, str | None]:
    """Returns (saving_basis_used, potential_saving_period, unavailable_reason)."""
    if rule.saving_basis == SAVING_BASIS_UNAVAILABLE:
        return SAVING_BASIS_UNAVAILABLE, None, (
            "This opportunity type has no configured engineering target and its context-matched baseline "
            "is not automatically treated as an achievable target - see engine/opportunity_targets.py's "
            "saving_basis documentation. Observed excess energy/cost is shown as evidence only."
        )
    # Reserved for future rules - not reachable by any rule in the
    # current registry (see module docstring).
    return SAVING_BASIS_UNAVAILABLE, None, "Saving basis declared but no supporting configuration/calculation exists yet."


# ---------------------------------------------------------------------------
# Annualization (item 2's correction) - configurable minimum evidence,
# never a bare ">= 2 occurrences".
# ---------------------------------------------------------------------------

def compute_annualization(
    occurrence_count: int, first_identified: str, last_updated: str, schedule_available: bool,
    potential_saving_period: float | None,
) -> tuple[str, float | None, float | None]:
    """Returns (annualization_method, estimated_monthly_saving, estimated_annual_saving).
    Monthly/annual are only ever non-None when potential_saving_period
    itself is a real number - annualizing an Unavailable figure makes
    no sense, so this correctly stays Unavailable for every opportunity
    in this build (every rule's saving_basis is unavailable today)."""
    if potential_saving_period is None:
        return "unavailable_conservative", None, None

    if schedule_available:
        # Not yet supported - no rule maps an opportunity type to a
        # schedule-derived recurrence/runtime basis (shift_definitions/
        # non_production_periods are both empty in this database).
        # Architecture ready; falls through honestly rather than
        # fabricating a schedule-based rate.
        pass

    span_days = (datetime.strptime(last_updated, TIME_FORMAT) - datetime.strptime(first_identified, TIME_FORMAT)).days
    if occurrence_count >= MIN_OCCURRENCES_FOR_ANNUALIZATION and span_days >= MIN_OBSERVATION_SPAN_DAYS_FOR_ANNUALIZATION:
        daily_rate = potential_saving_period / max(span_days, 1)
        return "observed_recurrence_rate", round(daily_rate * 30, 2), round(daily_rate * 365, 2)

    return "unavailable_conservative", None, None


# ---------------------------------------------------------------------------
# Priority ("Engineering Investigation Priority") - potential-saving
# and observed-impact kept as separate, separately-weighted components
# (item 3's correction).
# ---------------------------------------------------------------------------

def _financial_bucket(amount: float | None) -> int:
    if amount is None:
        return 0
    for i, threshold in enumerate(_FINANCIAL_BUCKETS):
        if amount < threshold:
            return i
    return len(_FINANCIAL_BUCKETS)


def _recurrence_score(occurrence_count: int) -> int:
    if occurrence_count <= 1:
        return 0
    if occurrence_count <= 4:
        return 1
    return 2


def compute_priority(
    potential_saving_period: float | None, observed_excess_cost: float, confidence: str, occurrence_count: int,
    severity: str, implementation_difficulty: str, equipment_criticality: str | None,
) -> tuple[str, float, dict[str, Any]]:
    potential_saving_component = _financial_bucket(potential_saving_period)  # 0 whenever saving is Unavailable
    observed_impact_component = _financial_bucket(observed_excess_cost)
    confidence_component = _BASELINE_CONFIDENCE_SCORE.get(confidence, 0)
    recurrence_component = _recurrence_score(occurrence_count)
    severity_component = _SEVERITY_SCORE.get(severity, 0)
    difficulty_component = _DIFFICULTY_SCORE.get(implementation_difficulty, 0)
    criticality_component = 1 if equipment_criticality else 0

    score = (
        potential_saving_component * 3 + observed_impact_component * 1 + confidence_component * 1
        + recurrence_component * 1 + severity_component * 1 + difficulty_component * 1 + criticality_component * 1
    )

    if score >= PRIORITY_HIGH_THRESHOLD:
        priority = "HIGH"
    elif score >= PRIORITY_MEDIUM_THRESHOLD:
        priority = "MEDIUM"
    else:
        priority = "LOW"

    breakdown = {
        "potential_saving_component": potential_saving_component,
        "observed_impact_component": observed_impact_component,
        "confidence_component": confidence_component,
        "recurrence_component": recurrence_component,
        "severity_component": severity_component,
        "difficulty_component": difficulty_component,
        "criticality_component": criticality_component,
        "note": "potential_saving_component is 0 whenever Potential Saving is Unavailable - observed_impact_component "
                "(weighted lower) reflects real observed abnormal operation, kept explicitly distinct.",
    }
    return priority, float(score), breakdown


# ---------------------------------------------------------------------------
# anomalies -> current severity for a set of linked ids (uses each
# anomaly's OWN current severity field - no recomputation).
# ---------------------------------------------------------------------------

def _max_severity(anomaly_rows: list[dict[str, Any]]) -> str:
    order = ("INFORMATION", "ATTENTION", "WARNING", "HIGH")
    best = "INFORMATION"
    for row in anomaly_rows:
        sev = row.get("severity")
        if sev in order and order.index(sev) > order.index(best):
            best = sev
    return best


# ---------------------------------------------------------------------------
# energy_opportunities read/write
# ---------------------------------------------------------------------------

def find_new_opportunity(config_database_path: str | Path, plant_id: int, instance_key: str, rule_key: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM energy_opportunities WHERE plant_id = ? AND instance_key = ? AND rule_key = ? AND status = 'NEW'",
            (plant_id, instance_key, rule_key),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def find_latest_dismissed_opportunity(config_database_path: str | Path, plant_id: int, instance_key: str, rule_key: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM energy_opportunities WHERE plant_id = ? AND instance_key = ? AND rule_key = ? AND status = 'DISMISSED' "
            "ORDER BY dismissed_at DESC LIMIT 1",
            (plant_id, instance_key, rule_key),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def _write_opportunity_row(config_database_path: str | Path, row_id: int | None, fields: dict[str, Any]) -> int:
    connection = sqlite3.connect(config_database_path)
    try:
        if row_id is None:
            columns = list(fields.keys())
            placeholders = ", ".join("?" for _ in columns)
            cursor = connection.execute(
                f"INSERT INTO energy_opportunities ({', '.join(columns)}) VALUES ({placeholders})",
                [fields[c] for c in columns],
            )
            connection.commit()
            return cursor.lastrowid
        else:
            set_clause = ", ".join(f"{c} = ?" for c in fields)
            connection.execute(
                f"UPDATE energy_opportunities SET {set_clause} WHERE id = ?",
                [*fields.values(), row_id],
            )
            connection.commit()
            return row_id
    finally:
        connection.close()


def dismiss_opportunity(config_database_path: str | Path, opportunity_id: int, reason: str, comment: str | None, now: datetime | None = None) -> None:
    if reason not in DISMISSAL_REASONS:
        raise ValueError(f"Invalid dismissal reason: {reason!r} - must be one of {DISMISSAL_REASONS}")
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)
    _write_opportunity_row(config_database_path, opportunity_id, {
        "status": STATUS_DISMISSED, "dismissed_at": now_text, "dismissal_reason": reason,
        "dismissal_comment": comment, "updated_at": now_text,
    })


# ---------------------------------------------------------------------------
# Main evaluation - one opportunity rule, one plant.
# ---------------------------------------------------------------------------

def evaluate_opportunity_rule(
    rule: OpportunityRule, config_database_path: str | Path, machine_database_path: str | Path,
    historian: DatabaseManager, plant_id: int, plant_code: str, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Evaluates one opportunity rule against every instance currently
    showing qualifying Phase 9 evidence for this plant. Returns one
    result dict per instance_key touched."""
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)

    candidates = _fetch_source_anomalies(config_database_path, plant_id, rule.source_anomaly_rule_key)
    if rule.non_production_only:
        candidates = [c for c in candidates if _predominantly_non_production(config_database_path, plant_code, c)]

    by_instance: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        by_instance.setdefault(c["instance_key"], []).append(c)

    results = []
    for instance_key, anomalies_for_instance in by_instance.items():
        results.append(_evaluate_instance(
            rule, config_database_path, plant_id, plant_code, instance_key, anomalies_for_instance, now, now_text,
        ))
    return results


def _evaluate_instance(
    rule: OpportunityRule, config_database_path: str | Path, plant_id: int, plant_code: str, instance_key: str,
    anomalies_for_instance: list[dict[str, Any]], now: datetime, now_text: str,
) -> dict[str, Any]:
    existing_new = find_new_opportunity(config_database_path, plant_id, instance_key, rule.rule_key)
    dismissed = find_latest_dismissed_opportunity(config_database_path, plant_id, instance_key, rule.rule_key)

    already_linked_ids: set[int] = set()
    if existing_new:
        already_linked_ids = set(json.loads(existing_new["source_anomaly_ids"]))
    elif dismissed:
        already_linked_ids = set(json.loads(dismissed["source_anomaly_ids"]))

    if existing_new:
        eligible = [a for a in anomalies_for_instance]  # any qualifying anomaly may keep contributing
    elif dismissed:
        # Only genuinely NEW evidence (first_detected after the dismissal, and never previously linked)
        # may create a fresh row - old evidence stays with the dismissed row, never resurrected.
        dismissed_at = dismissed["dismissed_at"]
        eligible = [
            a for a in anomalies_for_instance
            if a["id"] not in already_linked_ids and a["first_detected"] > dismissed_at
        ]
    else:
        eligible = list(anomalies_for_instance)

    if not eligible:
        return {"action": "skipped", "reason": "no qualifying anomaly evidence", "instance_key": instance_key}

    linked_ids = sorted(set(a["id"] for a in eligible) | (already_linked_ids if existing_new else set()))
    linked_rows_map = _anomalies_by_id(config_database_path, linked_ids)
    linked_rows = [linked_rows_map[i] for i in linked_ids if i in linked_rows_map]

    observed_excess_energy = sum(r["estimated_excess_energy_kwh"] or 0.0 for r in linked_rows)
    observed_excess_cost = sum(r["estimated_excess_cost"] or 0.0 for r in linked_rows if r["estimated_excess_cost"] is not None)
    currency = next((r["estimated_excess_cost_currency"] for r in linked_rows if r["estimated_excess_cost_currency"]), None)
    period_start = min(r["first_detected"] for r in linked_rows)
    period_end = max(r["last_seen"] for r in linked_rows)

    if observed_excess_cost < rule.minimum_meaningful_impact_cost and not existing_new:
        return {"action": "below_minimum_impact", "instance_key": instance_key}

    representative = max(linked_rows, key=lambda r: r["last_seen"])
    confidence = compute_confidence(representative)
    saving_basis, potential_saving_period, saving_reason = compute_potential_saving(rule)
    annualization_method, monthly, annual = compute_annualization(
        len(linked_rows), period_start, period_end, schedule_available=False, potential_saving_period=potential_saving_period,
    )
    severity = _max_severity(linked_rows)
    equipment_id = anomaly._equipment_id_for_instance(config_database_path, instance_key)
    criticality = _equipment_criticality(config_database_path, instance_key)
    implementation_difficulty = rule.default_implementation_difficulty or "UNKNOWN"
    priority, priority_score, breakdown = compute_priority(
        potential_saving_period, observed_excess_cost, confidence, len(linked_rows), severity, implementation_difficulty, criticality,
    )

    if existing_new and set(linked_ids) == already_linked_ids and existing_new["observed_excess_cost"] == round(observed_excess_cost, 6):
        return {"action": "unchanged", "id": existing_new["id"], "instance_key": instance_key}

    evidence = {
        "linked_anomaly_ids": linked_ids,
        "linked_anomaly_rule_key": rule.source_anomaly_rule_key,
        "representative_baseline_level": representative.get("baseline_level"),
        "representative_baseline_confidence": representative.get("baseline_confidence"),
    }
    assumptions = list(rule.assumptions) + [
        "Observed excess energy/cost is Phase 9's own interval-integrated, tariff-applied figure, summed only "
        "across the linked anomaly occurrence(s) above - never recomputed or re-derived here.",
    ]
    limitations = list(rule.exclusions)
    if saving_reason:
        limitations.append(saving_reason)

    # Phase 18.1a.1 - the observed cost/currency above came from
    # already-persisted Phase 9 anomaly rows, never recomputed here -
    # but no provenance travelled with them, so it's resolved fresh
    # against the same effective_tariff() lookup anomaly_engine.py
    # itself uses, at the period's own end date (never "today", so a
    # tariff entered later can't retroactively relabel an old
    # opportunity's provenance). None (no tariff resolvable for that
    # date/scope) stays None - never guessed as either label.
    try:
        period_end_date = datetime.strptime(period_end, TIME_FORMAT)
    except (TypeError, ValueError):
        period_end_date = None
    tariff = effective_tariff(config_database_path, plant_id, period_end_date) if period_end_date else None
    tariff_provenance = tariff_provenance_label(tariff)

    title = rule.title_template.format(instance=instance_key)
    fields = {
        "plant_id": plant_id, "equipment_id": equipment_id, "instance_key": instance_key, "rule_key": rule.rule_key,
        "category": rule.category, "title": title, "status": STATUS_NEW, "priority": priority,
        "priority_score": priority_score, "priority_breakdown_json": json.dumps(breakdown), "confidence": confidence,
        "source_anomaly_ids": json.dumps(linked_ids), "source_anomaly_rule_key": rule.source_anomaly_rule_key,
        "last_updated": now_text, "occurrence_count": len(linked_rows),
        "observed_period_start": period_start, "observed_period_end": period_end,
        "observed_excess_energy_kwh": observed_excess_energy, "observed_excess_cost": round(observed_excess_cost, 6),
        "observed_cost_currency": currency, "saving_basis": saving_basis, "saving_unavailable_reason": saving_reason,
        "estimated_potential_saving_period": potential_saving_period, "estimated_monthly_saving": monthly,
        "estimated_annual_saving": annual, "annualization_method": annualization_method, "saving_currency": currency,
        "tariff_provenance": tariff_provenance, "implementation_difficulty": implementation_difficulty,
        "equipment_criticality": criticality, "recommendation": rule.recommendation_template.format(instance=instance_key),
        "rule_provenance": rule.provenance, "evidence_json": json.dumps(evidence),
        "assumptions_json": json.dumps(assumptions), "limitations_json": json.dumps(limitations), "updated_at": now_text,
    }

    if existing_new:
        row_id = _write_opportunity_row(config_database_path, existing_new["id"], fields)
        return {"action": "updated", "id": row_id, "instance_key": instance_key}

    fields["first_identified"] = now_text
    fields["created_at"] = now_text
    row_id = _write_opportunity_row(config_database_path, None, fields)
    return {"action": "opened", "id": row_id, "instance_key": instance_key}


def _equipment_criticality(config_database_path: str | Path, instance_key: str) -> str | None:
    equipment_id = anomaly._equipment_id_for_instance(config_database_path, instance_key)
    if equipment_id is None:
        return None
    connection = sqlite3.connect(config_database_path)
    try:
        row = connection.execute("SELECT criticality FROM equipment WHERE id = ?", (equipment_id,)).fetchone()
        return row[0] if row and row[0] else None
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Debug CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    from config.environment import get_config_db_path, get_machine_db_path
    from engine.opportunity_targets import OPPORTUNITY_RULES

    parser = argparse.ArgumentParser(description="Phase 10 Opportunity Engine - debug CLI")
    parser.add_argument("--plant", default="p01")
    parser.add_argument("--config-database", default=str(get_config_db_path()))
    parser.add_argument("--machine-database", default=str(get_machine_db_path()))
    args = parser.parse_args()

    historian = DatabaseManager(db_path=args.machine_database)
    connection = sqlite3.connect(args.config_database)
    plant = connection.execute("SELECT id FROM plants WHERE code = ?", (args.plant.lower(),)).fetchone()
    connection.close()
    if plant is None:
        print(f"Unknown plant: {args.plant}")
        return
    plant_id = plant[0]

    for rule in OPPORTUNITY_RULES:
        results = evaluate_opportunity_rule(rule, args.config_database, args.machine_database, historian, plant_id, args.plant.lower())
        for r in results:
            print(f"{rule.rule_key} [{r.get('instance_key')}]: {r}")


if __name__ == "__main__":
    main()
