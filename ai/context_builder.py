from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ai.event_store import EventStore
from ai.interpretation_intent import comparison_domain_hint, requires_telemetry_confidence
from engine import data_health_engine as dhe
from engine import data_health_history as dhh
from engine import data_health_targets as dht
from engine import health_evidence as hev
from engine import health_history as hist
from engine.health_engine import discover_health_targets
from engine.maintenance_intelligence_engine import calculate_maintenance_priority
from engine.maintenance_intelligence_targets import PRIORITY_SORT_RANK
from simulator import plant_context
from ui import asset_performance_data as apd
from ui import data_access
from ui import health_data as hd
from ui import savings_verification_data as sv

"""
Phase 15 - Structured AI Context Layer.

This module NEVER calculates a deterministic engineering result itself -
every field returned here is either a direct database read, or a call
into an already-frozen Phase 6-14 read/compute function (calculate_health()
is never called here; calculate_maintenance_priority() IS called, because
Phase 13's own approved architecture has no persisted table of its own -
see engine/maintenance_intelligence_engine.py's module docstring - so
"reading Phase 13's current state" and "calling
calculate_maintenance_priority()" are the same operation for that phase
only. No table is ever written to from this module.

Per the approved Phase 15 correction "INTENT-SELECTIVE CONTEXT
RETRIEVAL": build_equipment_ai_context() only fetches the domains listed
for the given intent in DOMAINS_BY_INTENT - it does not fetch every
Phase 6-14 domain for every question.

Every domain sub-dict is either {"available": True, ...fields...} or
{"available": False, "reason": "..."} - never silently omitted, and
never a fabricated placeholder value.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Per-intent bounded domain retrieval (Correction 2). Deliberately a
# plain dict, not a fallback-to-everything default - an intent not
# listed here must be passed an explicit focus_domains list by the
# caller (ai/interpretation_intent.py never emits an intent that isn't
# a key of this dict, and tests assert that invariant).
DOMAINS_BY_INTENT: dict[str, tuple[str, ...]] = {
    "HEALTH_EXPLANATION": ("health", "anomalies", "events"),
    "MAINTENANCE_REVIEW": ("maintenance_intelligence", "health", "maintenance_history", "events"),
    "PERFORMANCE_REVIEW": ("asset_performance", "health", "maintenance_intelligence"),
    "ENERGY_REVIEW": ("energy_opportunity", "anomalies", "savings_verification"),
    "SAVINGS_STATUS": ("savings_verification", "energy_opportunity"),
    "GENERAL_ENGINEERING_QUERY": (
        "health", "maintenance_intelligence", "asset_performance", "anomalies",
        "energy_opportunity", "savings_verification", "events", "maintenance_history",
        "production_context",
    ),
    # Phase 17.2d - COMPARISON gets its OWN, narrower domain set (5, not
    # the 9 GENERAL_ENGINEERING_QUERY fetches) - exactly the domains
    # _build_comparison_facts() below actually has a deterministic
    # comparison contract for. anomalies/events/maintenance_history/
    # production_context have no comparison_facts contract and were
    # never used by any comparison answer - dropping them from the
    # fetch is a genuine, real DB-query reduction, not just a rendering
    # change (data_health stays separately/always cross-cutting-attached,
    # as it already is for every intent).
    "COMPARISON": ("health", "maintenance_intelligence", "asset_performance", "energy_opportunity", "savings_verification"),
}

# Worst-first ordering for Asset Performance dimensions - used only to
# pick which 3 dimensions are worth including in a bounded context, never
# to compute a score.
_PERFORMANCE_STATE_SEVERITY = {
    "SIGNIFICANTLY_DEGRADING": 4,
    "DEGRADING": 3,
    "INSUFFICIENT_EVIDENCE": 2,
    "STABLE": 1,
    "IMPROVING": 0,
    "NOT_CLASSIFIED": -1,
}


def _connect(config_database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    return connection


def _equipment_type_for_instance(
    config_database_path: str | Path, plant_code: str, instance_key: str
) -> str | None:
    for equipment_type, candidate_instance_key in discover_health_targets(config_database_path, plant_code):
        if candidate_instance_key == instance_key:
            return equipment_type
    return None


def _equipment_identity(config_database_path: str | Path, instance_key: str) -> dict[str, Any]:
    """
    Canonical equipment identity for the prompt/UI - never re-derives
    instance_key itself (the caller, i.e. entity resolution, already
    produced it). criticality is None (rendered "Unconfigured" by the
    prompt builder) for essentially all equipment today - a known,
    disclosed data-completeness gap, not a bug in this function.
    """
    equipment_id = hev.equipment_id_for_instance(config_database_path, instance_key)
    plant_code = instance_key.split(".")[0].lower() if instance_key else ""

    plants_by_code = {p["code"]: p for p in data_access.get_plants()}
    plant = plants_by_code.get(plant_code)
    plant_id = plant["id"] if plant else None

    display_name = instance_key
    area_name = None
    system_name = None
    brand = None
    model = None

    if equipment_id is not None:
        connection = _connect(config_database_path)
        try:
            row = connection.execute(
                """
                SELECT e.display_name, e.brand, e.model,
                       a.name AS area_name, s.name AS system_name
                FROM equipment e
                LEFT JOIN areas a ON a.id = e.area_id
                LEFT JOIN systems s ON s.id = e.system_id
                WHERE e.id = ?
                """,
                (equipment_id,),
            ).fetchone()
        finally:
            connection.close()

        if row is not None:
            display_name = row["display_name"] or display_name
            area_name = row["area_name"]
            system_name = row["system_name"]
            brand = row["brand"]
            model = row["model"]

    equipment_type = (
        _equipment_type_for_instance(config_database_path, plant_code, instance_key)
        if plant is not None
        else None
    )

    return {
        "instance_key": instance_key,
        "equipment_id": equipment_id,
        "display_name": display_name,
        "plant_code": plant_code,
        "plant_id": plant_id,
        "area_name": area_name,
        "system_name": system_name,
        "equipment_type": equipment_type,
        "criticality": hev.equipment_criticality(config_database_path, equipment_id),
        "brand": brand,
        "model": model,
    }


def _health_domain(config_database_path: str | Path, equipment: dict[str, Any]) -> dict[str, Any]:
    record = hd.get_latest_health_by_equipment_id(equipment["equipment_id"])

    if record is None:
        return {"available": False, "reason": "No Equipment Health assessment has ever been persisted for this equipment."}

    factors = hist.get_factor_detail(config_database_path, record["id"])
    top_factors = sorted(
        (f for f in factors if f["status"] == "penalized"),
        key=lambda f: f["penalty"],
        reverse=True,
    )[:3]

    return {
        "available": True,
        "health_score": record["health_score"],
        "health_band": record["health_band"] or "Insufficient Data",
        "assessment_confidence": record["assessment_confidence"],
        "provisional": bool(record["provisional"]),
        "coverage_status": record["coverage_status"],
        "top_factors": [
            {
                "factor_id": f["factor_id"],
                "family": f["factor_family"],
                "penalty": f["penalty"],
                "reason": f["reason"],
            }
            for f in top_factors
        ],
        "as_of": record["computed_at"],
    }


def _maintenance_intelligence_domain(
    config_database_path: str | Path, equipment: dict[str, Any], now: datetime
) -> dict[str, Any]:
    if equipment["plant_id"] is None or equipment["equipment_type"] is None:
        return {"available": False, "reason": "This equipment type is not covered by Maintenance Intelligence."}

    result = calculate_maintenance_priority(
        config_database_path, equipment["plant_id"], equipment["plant_code"],
        equipment["equipment_type"], equipment["instance_key"], now=now,
    )

    return {
        "available": True,
        "maintenance_priority": result.maintenance_priority,
        "priority_score": result.priority_score,
        "floor_applied": result.floor_applied,
        "floor_reason": result.floor_reason,
        "recommendation_confidence": result.recommendation_confidence,
        "confidence_basis": list(result.confidence_basis),
        "assessed_evidence": list(result.assessed_evidence),
        "recommended_checks": list(result.recommended_checks)[:3],
        "maintenance_status": dict(result.maintenance_status),
        "recent_movement": result.recent_movement,
        "as_of": result.generated_at,
    }


def _asset_performance_domain(
    config_database_path: str | Path, equipment: dict[str, Any]
) -> dict[str, Any]:
    if equipment["plant_code"] is None:
        return {"available": False, "reason": "No plant could be resolved for this equipment."}

    overview_rows = [
        r for r in apd.get_overview(equipment["plant_code"])
        if r["instance_key"] == equipment["instance_key"]
    ]

    if not overview_rows:
        return {"available": False, "reason": "No Asset Performance observation has ever been persisted for this equipment."}

    overview_rows.sort(
        key=lambda r: _PERFORMANCE_STATE_SEVERITY.get(r["performance_state"], -1),
        reverse=True,
    )
    top_dimensions = overview_rows[:3]

    ranking_entry = next(
        (
            e for e in apd.get_attention_ranking(equipment["plant_code"])
            if e["instance_key"] == equipment["instance_key"]
        ),
        None,
    )

    return {
        "available": True,
        "attention_state": ranking_entry["attention_state"] if ranking_entry else None,
        "attention_score": ranking_entry["attention_score"] if ranking_entry else None,
        "dimensions": [
            {
                "target_key": r["target_key"],
                "performance_state": r["performance_state"],
                "evidence_quality": r["evidence_quality"],
                "reference_value": r["reference_value"],
                "observed_value": r["observed_value"],
                "percent_change": r["percent_change"],
                "sustained_degradation": bool(r["sustained_degradation"]),
            }
            for r in top_dimensions
        ],
        "as_of": top_dimensions[0]["computed_at"] if top_dimensions else None,
    }


def _anomalies_domain(config_database_path: str | Path, equipment: dict[str, Any]) -> dict[str, Any]:
    if equipment["plant_code"] is None:
        return {"available": False, "reason": "No plant could be resolved for this equipment."}

    rows = [
        a for a in data_access.get_anomalies(status="OPEN", plant_code=equipment["plant_code"])
        if a["instance_key"] == equipment["instance_key"]
    ]

    if not rows:
        return {"available": False, "reason": "No open statistical anomaly is currently recorded for this equipment."}

    rows.sort(key=lambda a: a["last_seen"], reverse=True)
    top = rows[:3]

    return {
        "available": True,
        "open_anomalies": [
            {
                "target_key": a["target_key"],
                "severity": a["severity"],
                "confidence": a["confidence"],
                "provisional": bool(a["provisional"]),
                "anomaly_type": a["anomaly_type"],
                "deviation_percent": a["deviation_percent"],
                "last_seen": a["last_seen"],
            }
            for a in top
        ],
        "as_of": top[0]["last_seen"] if top else None,
    }


def _energy_opportunity_domain(config_database_path: str | Path, equipment: dict[str, Any]) -> dict[str, Any]:
    if equipment["plant_code"] is None:
        return {"available": False, "reason": "No plant could be resolved for this equipment."}

    rows = [
        o for o in data_access.get_opportunities(status="NEW", plant_code=equipment["plant_code"])
        if o["instance_key"] == equipment["instance_key"]
    ]

    if not rows:
        return {"available": False, "reason": "No open Energy Opportunity is currently recorded for this equipment."}

    rows.sort(key=lambda o: o["priority_score"], reverse=True)
    top = rows[:2]

    return {
        "available": True,
        "opportunities": [
            {
                "title": o["title"],
                "priority": o["priority"],
                "confidence": o["confidence"],
                "observed_excess_cost": o["observed_excess_cost"],
                "observed_excess_energy_kwh": o["observed_excess_energy_kwh"],
                "estimated_potential_saving_period": o["estimated_potential_saving_period"],
                "saving_unavailable_reason": o["saving_unavailable_reason"],
                "opportunity_id": o["id"],
            }
            for o in top
        ],
        "as_of": top[0]["last_updated"] if top else None,
    }


def _savings_verification_domain(config_database_path: str | Path, equipment: dict[str, Any]) -> dict[str, Any]:
    if equipment["plant_code"] is None:
        return {"available": False, "reason": "No plant could be resolved for this equipment."}

    interventions = [
        i for i in sv.get_interventions(plant_code=equipment["plant_code"])
        if i["instance_key"] == equipment["instance_key"]
    ]

    if not interventions:
        return {"available": False, "reason": "No energy-saving intervention has been recorded for this equipment."}

    interventions.sort(key=lambda i: i["recorded_at"], reverse=True)
    latest = interventions[0]
    latest_result = latest.get("latest_result")

    return {
        "available": True,
        "action_category": latest["action_category"],
        "action_description": latest["action_description"],
        "status": latest["status"],
        "engineer_status": latest["engineer_status"],
        "latest_outcome": latest.get("latest_outcome"),
        "latest_result": (
            {
                "result": latest_result["result"],
                "evidence_quality": latest_result["evidence_quality"],
                "verified_energy_kwh": latest_result["verified_energy_kwh"],
                "verified_cost": latest_result["verified_cost"],
                "verified_cost_currency": latest_result["verified_cost_currency"],
                "reason": latest_result["reason"],
                "evaluated_at": latest_result["evaluated_at"],
            }
            if latest_result
            else None
        ),
        "as_of": latest_result["evaluated_at"] if latest_result else latest["recorded_at"],
    }


def _events_domain(machine_database_path: str | Path, equipment: dict[str, Any]) -> dict[str, Any]:
    store = EventStore(database_path=machine_database_path)
    events = store.get_recent_events(equipment=equipment["display_name"], limit=5)

    if not events:
        return {"available": False, "reason": "No recorded alarm/warning event exists for this equipment."}

    return {
        "available": True,
        "recent_events": [
            {
                "event_time": e["event_time"],
                "tag": e["tag"],
                "severity": e["severity"],
                "condition": e["condition"],
                "message": e.get("message"),
            }
            for e in events
        ],
    }


def _maintenance_history_domain(config_database_path: str | Path, equipment: dict[str, Any]) -> dict[str, Any]:
    if equipment["equipment_id"] is None:
        return {"available": False, "reason": "No equipment record could be matched for maintenance history."}

    rows = data_access.get_maintenance_history(equipment["equipment_id"])[:3]

    if not rows:
        return {"available": False, "reason": "No maintenance log entries exist for this equipment."}

    return {
        "available": True,
        "recent_notes": [
            {
                "performed_at": r["performed_at"],
                "category": r["category"],
                "description": r["description"],
                "parts_replaced": r.get("parts_replaced"),
            }
            for r in rows
        ],
    }


def _production_context_domain(config_database_path: str | Path, equipment: dict[str, Any], now: datetime) -> dict[str, Any]:
    state = plant_context.get_production_state(config_database_path)
    running_batch = state.get(equipment["instance_key"])
    within_shift = plant_context.is_within_active_shift(config_database_path, now)

    return {
        "available": True,
        "within_active_shift": within_shift,
        "running_batch": running_batch,
    }


def _data_health_domain(
    config_database_path: str | Path, machine_database_path: str | Path, equipment: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """
    Phase 16.3 - a COMPACT, deterministic summary of Phase 16.1's Data
    Health result for this equipment. ALWAYS attached to the context
    (never intent-gated by DOMAINS_BY_INTENT) - telemetry trustworthiness
    is a cross-cutting concern relevant to interpreting every other
    domain, not a peer domain itself.

    Calls engine.data_health_engine directly (never ui/data_health_data.py
    - that module is UI-presentation-only, per the approved architecture
    boundary) and never recalculates anything itself.

    Deliberately excludes the full per-tag TagDataHealth list (that would
    be a large, low-value raw dump per the "keep the context compact"
    requirement) - only the already-compact summary lists
    (engine/data_health_engine.py's own tag-NAME lists, not full objects).

    "available" here means "did this read succeed" (True even when the
    resulting confidence_status is UNAVAILABLE - that is itself a valid,
    successfully-computed deterministic answer) - distinct from a
    genuine exception (available=False), which must never be silently
    treated as GOOD.
    """
    try:
        result = dhe.calculate_equipment_data_health(
            config_database_path, machine_database_path, equipment["instance_key"], now=now,
        )
    except Exception as error:
        return {
            "available": False,
            "confidence_status": dht.STATUS_UNAVAILABLE,
            "confidence_score": None,
            "reason": f"Data Health could not be evaluated: {error}",
        }

    return {
        "available": True,
        "confidence_status": result.confidence_status,
        "confidence_score": result.confidence_score,
        "component_scores": dict(result.component_scores),
        "component_applicability": dict(result.component_applicability),
        "required_tag_count": result.required_tag_count,
        "available_tag_count": result.available_tag_count,
        "fresh_tag_count": result.fresh_tag_count,
        "stale_tags": list(result.stale_tags),
        "missing_tags": list(result.missing_tags),
        "invalid_tags": list(result.invalid_tags),
        "indeterminate_freshness_tags": list(result.indeterminate_freshness_tags),
        "frozen_candidates": list(result.frozen_candidates),
        "gap_count": len(result.gaps),
        "timestamp_issue_count": len(result.timestamp_issues),
        "source_driver": (result.source or {}).get("configured_driver"),
        "reasons": list(result.reasons),
        "limitations": list(result.limitations),
        "as_of": result.computed_at,
    }


# ---------------------------------------------------------------------------
# Phase 17.2a - historical evidence domains. Each is a SEPARATE top-level
# sibling of its current-state counterpart ("data_health_history" next
# to "data_health", "health_history" next to "health",
# "asset_performance_history" next to "asset_performance") - never
# merged into it, so current-state and historical evidence stay
# structurally distinguishable at every call site (the Phase 17.1
# design's explicit requirement).
#
# Every function here is a pure ASSEMBLER over an already-authoritative,
# already-tested read model (engine.data_health_history for Data Health,
# engine.health_history for Equipment Health, ui.asset_performance_data/
# engine.performance_domain for Asset Performance) - none of them
# recalculates duration, trend, transition, or recurrence math itself,
# and none of them queries a raw snapshot/observation table directly
# from this module. Maintenance Intelligence deliberately has NO
# "maintenance_intelligence_history" counterpart - Phase 17.1's audit
# confirmed that domain has no persisted snapshot layer to read from at
# all (engine/maintenance_intelligence_engine.py computes fresh every
# call, with nothing to page through historically) - inventing one here
# would mean this module quietly becoming an analytics engine, which is
# explicitly out of scope.
# ---------------------------------------------------------------------------

DEFAULT_HISTORY_WINDOW_DAYS = 7.0
MAX_DATA_HEALTH_TRANSITIONS = 5
MAX_HEALTH_BAND_TRANSITIONS = 5
MAX_ASSET_PERFORMANCE_OBSERVATIONS_TOTAL = 3
MAX_ASSET_PERFORMANCE_MAINTENANCE_COMPARISONS = 3


def _data_health_history_domain(
    config_database_path: str | Path, equipment: dict[str, Any], now: datetime,
    window_days: float = DEFAULT_HISTORY_WINDOW_DAYS,
) -> dict[str, Any]:
    """
    Phase 17.2a - historical Data Health evidence, sourced ENTIRELY from
    engine.data_health_history (Phase 16.5's own tested read model).
    Never queries data_health_snapshots directly, never recalculates
    status_duration()/status_transitions()/recurring_issues() itself,
    and never calls engine.data_health_engine with a historical `now` -
    the Phase 16.5 architecture checkpoint established that retrospective
    reconstruction from raw telemetry is unsafe (unbounded-above
    historian reads + unversioned tag configuration), so persisted
    snapshots are the only authoritative historical source.
    """
    if equipment["plant_id"] is None:
        return {
            "available": False, "reason": "No plant could be resolved for this equipment.",
            "recorded_since": None, "window_days": window_days,
        }

    recorded_since = dhh.first_recorded_at(config_database_path)
    if recorded_since is None:
        return {
            "available": False,
            "reason": "No Data Health history has been recorded yet - the data_health_history_worker service "
                       "has not persisted its first snapshot.",
            "recorded_since": None, "window_days": window_days,
        }

    start = (now - timedelta(days=window_days)).strftime(TIME_FORMAT)
    end = now.strftime(TIME_FORMAT)

    duration = dhh.status_duration(config_database_path, equipment["plant_id"], equipment["instance_key"], start, end)

    if duration["insufficient_history"]:
        return {
            "available": False,
            "reason": "No Data Health history has been recorded yet for this equipment in the selected window.",
            "recorded_since": recorded_since, "window_days": window_days,
        }

    transitions = dhh.status_transitions(config_database_path, equipment["plant_id"], equipment["instance_key"], start, end)
    recurrence = dhh.recurring_issues(config_database_path, equipment["plant_id"], equipment["instance_key"], start, end)

    return {
        "available": True,
        "recorded_since": recorded_since,
        "window_days": window_days,
        "status_duration": duration,
        "recent_transitions": transitions[-MAX_DATA_HEALTH_TRANSITIONS:],
        "recurring_issues": recurrence["occurrences"],
        "reason": None,
    }


def _health_history_domain(config_database_path: str | Path, equipment: dict[str, Any], now: datetime) -> dict[str, Any]:
    """
    Phase 17.2a - historical Equipment Health evidence, sourced ENTIRELY
    from engine.health_history (Phase 12.2's own tested read model,
    already imported above as `hist`). Never recalculates trend/band-
    transition logic itself.
    """
    if equipment["plant_id"] is None:
        return {"available": False, "reason": "No plant could be resolved for this equipment."}

    trend = hist.compute_trend(config_database_path, equipment["plant_id"], equipment["instance_key"], now=now)

    if trend["direction"] == hist.DIRECTION_INSUFFICIENT_HISTORY:
        return {"available": False, "reason": "No Equipment Health history has been recorded yet for this equipment."}

    transitions = hist.band_transitions(config_database_path, equipment["plant_id"], equipment["instance_key"], days=30, now=now)

    return {
        "available": True,
        "trend": trend,
        "recent_band_transitions": transitions[-MAX_HEALTH_BAND_TRANSITIONS:],
        "reason": None,
    }


def _asset_performance_history_domain(config_database_path: str | Path, equipment: dict[str, Any]) -> dict[str, Any]:
    """
    Phase 17.2a (compacted in 17.2a.1) - historical Asset Performance
    evidence, reusing the existing ui.asset_performance_data /
    engine.performance_domain read model unchanged (apd.get_detail(),
    apd.get_maintenance_comparisons_for_instance()) - never a second
    Asset Performance history engine.

    `recent_observations` is bounded to 3 TOTAL for the whole domain
    (never per dimension - the original 17.2a draft applied the "<=3"
    bound per dimension, which could yield up to 9 entries across 3
    dimensions and was flagged/corrected in 17.2a.1). Selection reuses
    the two orderings that already exist rather than inventing a new
    one: dimensions are taken in the SAME top-severity order the
    current-state "asset_performance" domain already establishes
    (_PERFORMANCE_STATE_SEVERITY, worst-first), and within each
    dimension only its single MOST RECENT observation is taken
    (apd.get_detail()'s own `history` field is chronological, oldest
    first - the bound is applied by slicing the already-fetched result,
    never by asking the read model to calculate anything new). With at
    most 3 dimensions selected and at most 1 observation taken from
    each, the total is mechanically bounded to <=3 regardless of how
    many dimensions this equipment actually has.
    """
    if equipment["plant_id"] is None or equipment["plant_code"] is None:
        return {
            "available": False, "reason": "No plant could be resolved for this equipment.",
            "recent_observations": [], "maintenance_comparisons": [],
        }

    overview_rows = [
        r for r in apd.get_overview(equipment["plant_code"])
        if r["instance_key"] == equipment["instance_key"]
    ]

    if not overview_rows:
        return {
            "available": False,
            "reason": "No Asset Performance observation has ever been persisted for this equipment.",
            "recent_observations": [], "maintenance_comparisons": [],
        }

    overview_rows.sort(key=lambda r: _PERFORMANCE_STATE_SEVERITY.get(r["performance_state"], -1), reverse=True)
    top_target_keys = [r["target_key"] for r in overview_rows[:3]]

    recent_observations: list[dict[str, Any]] = []
    for target_key in top_target_keys[:MAX_ASSET_PERFORMANCE_OBSERVATIONS_TOTAL]:
        detail = apd.get_detail(equipment["plant_id"], equipment["instance_key"], target_key, days=90)
        if detail is None or not detail["history"]:
            continue
        observation = detail["history"][-1]  # most recent only - see docstring
        recent_observations.append({
            "target_key": target_key,
            "computed_at": observation["computed_at"],
            "performance_state": observation["performance_state"],
            "observed_value": observation["observed_value"],
            "percent_change": observation["percent_change"],
            "evidence_quality": observation["evidence_quality"],
        })

    maintenance_comparisons_raw = apd.get_maintenance_comparisons_for_instance(
        equipment["plant_id"], equipment["instance_key"],
    )[:MAX_ASSET_PERFORMANCE_MAINTENANCE_COMPARISONS]

    maintenance_comparisons = [
        {
            "target_key": c["target_key"],
            "performed_at": c["performed_at"],
            "pre_value": c["pre_value"],
            "post_value": c["post_value"],
            "percent_change": c["percent_change"],
            "evidence_quality": c["evidence_quality"],
            "effectiveness_result": c["effectiveness_result"],
        }
        for c in maintenance_comparisons_raw
    ]

    if not recent_observations and not maintenance_comparisons:
        return {
            "available": False,
            "reason": "No Asset Performance history is available for this equipment's tracked dimensions.",
            "recent_observations": [], "maintenance_comparisons": [],
        }

    return {
        "available": True,
        "recent_observations": recent_observations,
        "maintenance_comparisons": maintenance_comparisons,
        "reason": None,
    }


# Explicit, opt-in only (item 8) - deliberately NOT wired into
# DOMAINS_BY_INTENT, so no existing intent's current-state retrieval
# changes at all. A caller (or a test) must name a historical domain
# here to get it; the natural-language "does this question need
# history" classifier is Phase 17.2e's job, not this one's.
_VALID_HISTORY_DOMAINS = frozenset({"data_health_history", "health_history", "asset_performance_history"})


_DOMAIN_LIMITATIONS = {
    "energy_opportunity": "Estimated potential saving is unavailable for essentially every current rule in this build - do not treat 'Not yet estimable' as zero.",
    "asset_performance": "Asset Performance evidence quality is a separate signal from the observed percent change - LIMITED evidence should be treated as directional, not confirmed.",
}


def build_equipment_ai_context(
    config_database_path: str | Path,
    machine_database_path: str | Path,
    instance_key: str,
    intent: str,
    question: str = "",
    focus_domains: tuple[str, ...] | None = None,
    now: datetime | None = None,
    history_domains: tuple[str, ...] | None = None,
    history_window_days: float = DEFAULT_HISTORY_WINDOW_DAYS,
) -> dict[str, Any]:
    """
    The Structured AI Context Layer's main entry point. Deterministic,
    LLM-free, fully testable on its own (Correction 6/item 43) - build
    this once and you can assert exactly what facts were supplied to the
    LLM, independent of how it later phrases them.

    Only fetches the domains DOMAINS_BY_INTENT[intent] lists (Correction
    2) - focus_domains, when given, further narrows that set (never
    widens it) for a caller that already knows it needs less.

    `history_domains` (Phase 17.2a) is a SEPARATE, explicit opt-in list
    for historical evidence ("data_health_history"/"health_history"/
    "asset_performance_history") - deliberately NOT driven by
    DOMAINS_BY_INTENT/intent, so no existing intent's current-state
    retrieval changes even slightly. Defaults to None (no historical
    domain fetched, matching every existing caller's current behavior
    unchanged). An unrecognized name is silently ignored rather than
    raising - callers pass a fixed, known vocabulary
    (_VALID_HISTORY_DOMAINS). Deciding WHICH questions should pass which
    history_domains (the natural-language classifier) is Phase 17.2e's
    job, not this function's.
    """
    now = now or datetime.now()
    equipment = _equipment_identity(config_database_path, instance_key)

    domains_to_fetch = DOMAINS_BY_INTENT.get(intent, ())

    if focus_domains is not None:
        domains_to_fetch = tuple(d for d in domains_to_fetch if d in focus_domains)

    requested_history_domains = tuple(
        d for d in (history_domains or ()) if d in _VALID_HISTORY_DOMAINS
    )

    context: dict[str, Any] = {
        "request": {
            "user_question": question,
            "resolved_intent": intent,
            "domains_fetched": list(domains_to_fetch),
            "requires_telemetry_confidence": requires_telemetry_confidence(intent, question),
            "history_domains_fetched": list(requested_history_domains),
        },
        "equipment": equipment,
    }

    # Phase 16.3 - cross-cutting, always attached regardless of intent/
    # domains_fetched (telemetry trustworthiness is relevant to
    # interpreting every other domain, not a peer domain gated by intent).
    context["data_health"] = _data_health_domain(config_database_path, machine_database_path, equipment, now)

    fetchers = {
        "health": lambda: _health_domain(config_database_path, equipment),
        "maintenance_intelligence": lambda: _maintenance_intelligence_domain(config_database_path, equipment, now),
        "asset_performance": lambda: _asset_performance_domain(config_database_path, equipment),
        "anomalies": lambda: _anomalies_domain(config_database_path, equipment),
        "energy_opportunity": lambda: _energy_opportunity_domain(config_database_path, equipment),
        "savings_verification": lambda: _savings_verification_domain(config_database_path, equipment),
        "events": lambda: _events_domain(machine_database_path, equipment),
        "maintenance_history": lambda: _maintenance_history_domain(config_database_path, equipment),
        "production_context": lambda: _production_context_domain(config_database_path, equipment, now),
    }

    for domain in domains_to_fetch:
        context[domain] = fetchers[domain]()

    # Phase 17.2a - historical evidence domains, separate top-level
    # siblings of their current-state counterparts (item 2). Fetched
    # ONLY when explicitly requested via history_domains - never as a
    # side effect of intent/focus_domains, so every existing call site
    # (which never passes history_domains) is byte-for-byte unaffected.
    history_fetchers = {
        "data_health_history": lambda: _data_health_history_domain(config_database_path, equipment, now, history_window_days),
        "health_history": lambda: _health_history_domain(config_database_path, equipment, now),
        "asset_performance_history": lambda: _asset_performance_history_domain(config_database_path, equipment),
    }

    for domain in requested_history_domains:
        context[domain] = history_fetchers[domain]()

    data_limitations = [
        _DOMAIN_LIMITATIONS[domain]
        for domain in domains_to_fetch
        if domain in _DOMAIN_LIMITATIONS and context.get(domain, {}).get("available")
    ]
    context["data_limitations"] = data_limitations

    context["provenance"] = {
        "context_built_at": now.strftime(TIME_FORMAT),
        "source_modules": (
            [f"ai.context_builder.{d}" for d in domains_to_fetch]
            + [f"ai.context_builder.{d}" for d in requested_history_domains]
        ),
    }

    return context


# ---------------------------------------------------------------------------
# Phase 17.2c - deterministic COMPARISON facts. Every function here reads
# ONLY from the two already-built entity_a/entity_b contexts (themselves
# built by the unmodified build_equipment_ai_context()) - nothing here
# recalculates a domain value, and nothing here invents a new combined/
# aggregate score. A comparison is only ever produced where the two
# sides are genuinely comparable (same contract, same vocabulary, or -
# for Asset Performance - the same target_key dimension); otherwise the
# fact is marked "comparable": False and the prompt renders it as
# NOT_COMPARABLE, never guessed.
# ---------------------------------------------------------------------------

def _lower_value_entity(value_a: float | None, value_b: float | None) -> str | None:
    """Returns 'A'/'B' for whichever numeric value is LOWER, or None if
    either is unavailable or they are equal - never guesses an ordering
    from missing/tied data."""
    if value_a is None or value_b is None:
        return None
    if value_a < value_b:
        return "A"
    if value_b < value_a:
        return "B"
    return None


def _health_comparison_facts(entity_a: dict[str, Any], entity_b: dict[str, Any]) -> dict[str, Any]:
    health_a = entity_a.get("health", {}) or {}
    health_b = entity_b.get("health", {}) or {}
    score_a = health_a.get("health_score") if health_a.get("available") else None
    score_b = health_b.get("health_score") if health_b.get("available") else None
    comparable = score_a is not None and score_b is not None

    def _side(health: dict[str, Any]) -> dict[str, Any]:
        if not health.get("available"):
            return {"available": False, "score": None, "band": None, "assessment_confidence": None, "provisional": None}
        return {
            "available": True, "score": health.get("health_score"), "band": health.get("health_band"),
            "assessment_confidence": health.get("assessment_confidence"), "provisional": health.get("provisional"),
        }

    return {
        "comparable": comparable,
        "entity_a": _side(health_a), "entity_b": _side(health_b),
        "score_difference": round(score_b - score_a, 1) if comparable else None,
        "lower_score_entity": _lower_value_entity(score_a, score_b) if comparable else None,
    }


def _maintenance_comparison_facts(entity_a: dict[str, Any], entity_b: dict[str, Any]) -> dict[str, Any]:
    maintenance_a = entity_a.get("maintenance_intelligence", {}) or {}
    maintenance_b = entity_b.get("maintenance_intelligence", {}) or {}
    comparable = bool(maintenance_a.get("available") and maintenance_b.get("available"))

    def _side(maintenance: dict[str, Any]) -> dict[str, Any]:
        if not maintenance.get("available"):
            return {"available": False, "priority": None, "recommendation_confidence": None, "recommended_checks": []}
        return {
            "available": True, "priority": maintenance.get("maintenance_priority"),
            "recommendation_confidence": maintenance.get("recommendation_confidence"),
            # Phase 17.2d - bounded to 2 (tighter than the current-state
            # domain's own [:3]) - the minimum supporting evidence the
            # compact envelope needs for a useful "what to check next"
            # answer (item 19), never the full curated list.
            "recommended_checks": list(maintenance.get("recommended_checks", []))[:2],
        }

    higher_priority_entity = None
    if comparable:
        # PRIORITY_SORT_RANK is the SAME accepted ordering
        # ui.maintenance_intelligence_data.sort_overview() already uses
        # for its own "most-needs-attention-first" sort - never a new
        # ordering invented here.
        rank_a = PRIORITY_SORT_RANK.get(maintenance_a["maintenance_priority"])
        rank_b = PRIORITY_SORT_RANK.get(maintenance_b["maintenance_priority"])
        if rank_a is not None and rank_b is not None and rank_a != rank_b:
            higher_priority_entity = "A" if rank_a > rank_b else "B"

    return {
        "comparable": comparable,
        "entity_a": _side(maintenance_a), "entity_b": _side(maintenance_b),
        "higher_priority_entity": higher_priority_entity,
    }


def _asset_performance_comparison_facts(entity_a: dict[str, Any], entity_b: dict[str, Any]) -> dict[str, Any]:
    performance_a = entity_a.get("asset_performance", {}) or {}
    performance_b = entity_b.get("asset_performance", {}) or {}
    shared_dimensions: list[dict[str, Any]] = []

    if performance_a.get("available") and performance_b.get("available"):
        dims_a = {d["target_key"]: d for d in performance_a.get("dimensions", [])}
        dims_b = {d["target_key"]: d for d in performance_b.get("dimensions", [])}
        # Comparability gate (item 5/12): only target_keys present on
        # BOTH entities are compared - a chiller's "cop" and a pump's
        # "flow_per_kw" never share a key, so they are never compared.
        # percent_change is each entity's OWN self-referential deviation
        # (Phase 8/14's own established contract), so it - unlike the
        # raw observed_value, which stays informational-only, never
        # ranked - is safe to compare in magnitude across two different
        # physical machines of the same dimension.
        for target_key in sorted(set(dims_a) & set(dims_b)):
            dim_a, dim_b = dims_a[target_key], dims_b[target_key]
            change_a, change_b = dim_a.get("percent_change"), dim_b.get("percent_change")
            shared_dimensions.append({
                "target_key": target_key,
                "entity_a": {
                    "performance_state": dim_a.get("performance_state"), "evidence_quality": dim_a.get("evidence_quality"),
                    "observed_value": dim_a.get("observed_value"), "percent_change": change_a,
                },
                "entity_b": {
                    "performance_state": dim_b.get("performance_state"), "evidence_quality": dim_b.get("evidence_quality"),
                    "observed_value": dim_b.get("observed_value"), "percent_change": change_b,
                },
                "percent_change_difference": round(change_b - change_a, 2) if change_a is not None and change_b is not None else None,
            })

    return {
        "comparable": bool(shared_dimensions),
        "shared_dimensions": shared_dimensions,
        "entity_a_attention_score": performance_a.get("attention_score") if performance_a.get("available") else None,
        "entity_b_attention_score": performance_b.get("attention_score") if performance_b.get("available") else None,
    }


def _data_health_comparison_facts(entity_a: dict[str, Any], entity_b: dict[str, Any]) -> dict[str, Any]:
    data_health_a = entity_a.get("data_health", {}) or {}
    data_health_b = entity_b.get("data_health", {}) or {}
    score_a = data_health_a.get("confidence_score") if data_health_a.get("available") else None
    score_b = data_health_b.get("confidence_score") if data_health_b.get("available") else None
    comparable = score_a is not None and score_b is not None

    def _side(data_health: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": data_health.get("confidence_status") if data_health.get("available") else None,
            "score": data_health.get("confidence_score") if data_health.get("available") else None,
        }

    return {
        "comparable": comparable,
        "entity_a": _side(data_health_a), "entity_b": _side(data_health_b),
        "score_difference": round(score_b - score_a, 1) if comparable else None,
        # "poorer" = lower Data Confidence score - the same 0-100 scale
        # regardless of equipment type (Phase 16.1's own contract), so
        # this is always a fair comparison when both sides have a score.
        "poorer_data_health_entity": _lower_value_entity(score_a, score_b) if comparable else None,
    }


def _energy_comparison_facts(entity_a: dict[str, Any], entity_b: dict[str, Any]) -> dict[str, Any]:
    energy_a = entity_a.get("energy_opportunity", {}) or {}
    energy_b = entity_b.get("energy_opportunity", {}) or {}

    def _side(energy: dict[str, Any]) -> dict[str, Any]:
        if not energy.get("available") or not energy.get("opportunities"):
            return {"has_opportunity": False, "observed_excess_cost": None}
        return {"has_opportunity": True, "observed_excess_cost": energy["opportunities"][0].get("observed_excess_cost")}

    side_a, side_b = _side(energy_a), _side(energy_b)
    cost_a, cost_b = side_a["observed_excess_cost"], side_b["observed_excess_cost"]
    comparable = cost_a is not None and cost_b is not None

    higher_cost_entity = None
    if comparable and cost_a != cost_b:
        higher_cost_entity = "A" if cost_a > cost_b else "B"

    return {
        "comparable": comparable,
        "entity_a": side_a, "entity_b": side_b,
        "higher_observed_excess_cost_entity": higher_cost_entity,
    }


def _savings_verification_comparison_facts(entity_a: dict[str, Any], entity_b: dict[str, Any]) -> dict[str, Any]:
    savings_a = entity_a.get("savings_verification", {}) or {}
    savings_b = entity_b.get("savings_verification", {}) or {}

    def _side(savings: dict[str, Any]) -> dict[str, Any]:
        if not savings.get("available") or not savings.get("latest_result"):
            return {"result": None}
        return {"result": savings["latest_result"].get("result")}

    side_a, side_b = _side(savings_a), _side(savings_b)

    # Deliberately no "which is better" ordering, ever (item 3) - a
    # VERIFIED/REJECTED/INCONCLUSIVE verdict for one intervention is not
    # a ranked quantity, and evidence between two equipment can genuinely
    # differ without one being "worse."
    return {"comparable": side_a["result"] is not None and side_b["result"] is not None, "entity_a": side_a, "entity_b": side_b}


def _data_health_history_comparison_facts(entity_a: dict[str, Any], entity_b: dict[str, Any]) -> dict[str, Any] | None:
    """None when history wasn't requested for (at least) one side -
    historical comparison stays entirely opt-in, never auto-added."""
    history_a, history_b = entity_a.get("data_health_history"), entity_b.get("data_health_history")

    if history_a is None or history_b is None:
        return None

    def _side(history: dict[str, Any]) -> dict[str, Any]:
        if not history.get("available"):
            return {"available": False, "good_percentage": None, "no_history_percentage": None}
        percentages = history["status_duration"]["percentages"]
        return {
            "available": True, "good_percentage": percentages.get("GOOD"),
            "no_history_percentage": history["status_duration"].get("no_history_percentage"),
        }

    return {"comparable": bool(history_a.get("available") and history_b.get("available")), "entity_a": _side(history_a), "entity_b": _side(history_b)}


def _health_history_comparison_facts(entity_a: dict[str, Any], entity_b: dict[str, Any]) -> dict[str, Any] | None:
    history_a, history_b = entity_a.get("health_history"), entity_b.get("health_history")

    if history_a is None or history_b is None:
        return None

    def _side(history: dict[str, Any]) -> dict[str, Any]:
        if not history.get("available"):
            return {"available": False, "direction": None, "absolute_change": None}
        return {"available": True, "direction": history["trend"]["direction"], "absolute_change": history["trend"]["absolute_change"]}

    side_a, side_b = _side(history_a), _side(history_b)
    change_a, change_b = side_a["absolute_change"], side_b["absolute_change"]
    comparable = change_a is not None and change_b is not None

    return {
        "comparable": comparable, "entity_a": side_a, "entity_b": side_b,
        # "larger decline" = the lower (more negative) absolute_change -
        # already-computed by engine.health_history.compute_trend(),
        # never recalculated here.
        "larger_decline_entity": _lower_value_entity(change_a, change_b) if comparable else None,
    }


def _build_comparison_facts(entity_a: dict[str, Any], entity_b: dict[str, Any]) -> dict[str, Any]:
    facts = {
        "health": _health_comparison_facts(entity_a, entity_b),
        "maintenance_intelligence": _maintenance_comparison_facts(entity_a, entity_b),
        "asset_performance": _asset_performance_comparison_facts(entity_a, entity_b),
        "data_health": _data_health_comparison_facts(entity_a, entity_b),
        "energy_opportunity": _energy_comparison_facts(entity_a, entity_b),
        "savings_verification": _savings_verification_comparison_facts(entity_a, entity_b),
    }

    data_health_history_facts = _data_health_history_comparison_facts(entity_a, entity_b)
    if data_health_history_facts is not None:
        facts["data_health_history"] = data_health_history_facts

    health_history_facts = _health_history_comparison_facts(entity_a, entity_b)
    if health_history_facts is not None:
        facts["health_history"] = health_history_facts

    return facts


def build_comparison_ai_context(
    config_database_path: str | Path,
    machine_database_path: str | Path,
    instance_key_a: str,
    instance_key_b: str,
    intent: str = "COMPARISON",
    question: str = "",
    now: datetime | None = None,
    history_domains: tuple[str, ...] | None = None,
    history_window_days: float = DEFAULT_HISTORY_WINDOW_DAYS,
) -> dict[str, Any]:
    """
    Two independent equipment context packages, side by side - never
    blended into one number (see ai/interpretation_prompt_builder.py's
    COMPARISON template). `history_domains` (Phase 17.2c), when given,
    is passed through UNCHANGED to both entities' own
    build_equipment_ai_context() calls - each side still gets its own
    independent availability, exactly like current-state domains
    already do. Defaults to None (no history for either side, matching
    every existing caller's behavior unchanged).

    `comparison_facts` (Phase 17.2c) is a deterministic, read-only diff
    over the two already-built entity contexts - see
    _build_comparison_facts() above for the full per-domain contract.
    Historical comparison facts (data_health_history/health_history) are
    only included when history_domains caused BOTH sides to actually
    carry that domain.

    Phase 17.2d - `intent` now defaults to "COMPARISON" (its own
    DOMAINS_BY_INTENT entry - 5 domains, not GENERAL_ENGINEERING_QUERY's
    9) rather than being silently overridden to
    "GENERAL_ENGINEERING_QUERY" as before - a real, direct DB-query
    reduction (anomalies/events/maintenance_history/production_context
    were fetched for every comparison but had no comparison_facts
    contract and were never used by one). `comparison_facts` is always
    computed from whatever domains WERE fetched per entity - this
    remains the full, authoritative source the grounding guard reads;
    only the PROMPT RENDERING (ai/interpretation_prompt_builder.py) may
    choose to show a narrower subset of it for a domain-specific
    question - grounding always sees the complete set regardless of
    what was rendered (item 12's "richer grounding context" design).
    `comparison_domain_hint` records that narrowing signal (reused from
    ai/interpretation_intent.py's existing per-intent trigger phrases -
    never a new classifier) for the renderer to consume.
    """
    now = now or datetime.now()

    entity_a = build_equipment_ai_context(
        config_database_path, machine_database_path, instance_key_a, intent, question,
        now=now, history_domains=history_domains, history_window_days=history_window_days,
    )
    entity_b = build_equipment_ai_context(
        config_database_path, machine_database_path, instance_key_b, intent, question,
        now=now, history_domains=history_domains, history_window_days=history_window_days,
    )

    return {
        "request": {
            "user_question": question, "resolved_intent": "COMPARISON",
            "comparison_domain_hint": comparison_domain_hint(question),
        },
        "entity_a": entity_a,
        "entity_b": entity_b,
        "comparison_facts": _build_comparison_facts(entity_a, entity_b),
    }


def build_factory_ai_context(
    config_database_path: str | Path,
    machine_database_path: str | Path,
    plant_code: str | None = None,
    question: str = "",
    now: datetime | None = None,
    top_n: int = 5,
) -> dict[str, Any]:
    """
    Factory-wide "what should engineering look at today" digest - built
    ENTIRELY from each domain's own existing authoritative ranking
    function (never a new AI-computed ranking, per the hard "no single
    AI factory score" rule). Four separate, never-blended sections.
    """
    now = now or datetime.now()

    health_rows = sorted(
        (r for r in hd.get_overview(plant_code) if r["health_score"] is not None),
        key=lambda r: r["health_score"],
    )[:top_n]

    from ui import maintenance_intelligence_data as mid

    maintenance_rows = mid.sort_overview(mid.get_overview(plant_code))[:top_n]

    performance_entries = apd.sort_attention_ranking(apd.get_attention_ranking(plant_code))
    performance_rows = [
        e for e in performance_entries if e["attention_state"] == "RANKED"
    ][:top_n]

    opportunity_rows = data_access.get_opportunities(status="NEW", plant_code=plant_code)[:top_n]

    return {
        "request": {"user_question": question, "resolved_intent": "FACTORY_SUMMARY", "plant_code": plant_code},
        "health_attention": [
            {"instance_key": r["instance_key"], "display_name": r["display_name"], "health_score": r["health_score"], "health_band": r["health_band"]}
            for r in health_rows
        ],
        "maintenance_attention": [
            {"instance_key": r["instance_key"], "display_name": r["display_name"], "maintenance_priority": r["maintenance_priority"], "priority_score": r["priority_score"]}
            for r in maintenance_rows
        ],
        "performance_attention": [
            {"instance_key": r["instance_key"], "display_name": r["display_name"], "attention_score": r["attention_score"], "worst_dimension_target_key": r["worst_dimension_target_key"], "worst_dimension_state": r["worst_dimension_state"]}
            for r in performance_rows
        ],
        "energy_opportunities": [
            {"instance_key": r["instance_key"], "title": r["title"], "priority": r["priority"], "observed_excess_cost": r["observed_excess_cost"]}
            for r in opportunity_rows
        ],
        "provenance": {"context_built_at": now.strftime(TIME_FORMAT)},
    }
