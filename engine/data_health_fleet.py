from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.data_health_engine import calculate_equipment_data_health
from engine.data_health_targets import (
    CONFIDENCE_STATUS_VALUES,
    DATA_HEALTH_MODEL_VERSION,
    FRESHNESS_INDETERMINATE,
    FRESHNESS_STALE,
    STATUS_DEGRADED,
    STATUS_GOOD,
    STATUS_POOR,
    STATUS_UNAVAILABLE,
    VALIDITY_INVALID,
)
from engine.health_engine import discover_health_targets

"""
Phase 16.4 - deterministic FLEET aggregation over Phase 16.1's
per-equipment Data Health engine. This module NEVER recalculates
Freshness/Availability/Validity/Continuity, frozen-candidate detection,
required-tag resolution, or log_on_change handling - it only calls
engine.data_health_engine.calculate_equipment_data_health() once per
eligible equipment instance and aggregates the ALREADY-COMPUTED,
authoritative results (item 7 of the approved plan).

Equipment population: reuses engine.health_engine.discover_health_targets()
- the SAME (equipment_type, instance_key) enumeration Phase 12's
Equipment Health already relies on, since both are keyed on the identical
engine.baseline_targets.EQUIPMENT_TYPE_PROFILES/SUPPORTED_EQUIPMENT_TYPES
registry. No second equipment-population definition is introduced.

No schema change, no persistence, no background worker - see this
phase's architecture audit (measured full-fleet cost: ~6.1s for the 34
currently-eligible equipment instances across both plants, ~180ms
average per equipment). The UI layer (ui/data_health_fleet_data.py) is
responsible for caching this result across Streamlit reruns; this
module itself is a plain, cache-unaware function - identical to how
engine/data_health_engine.py has no Streamlit dependency either.

One equipment's evaluation failing is isolated (item 29) - it is
recorded as an UNAVAILABLE row with the real exception message, never
silently dropped and never silently GOOD.
"""

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Worst-first (item 10) - UNAVAILABLE is the state that most urgently
# needs engineering attention (nothing can currently be assessed at
# all), GOOD needs none. Deliberately a plain lookup table, not a
# invented weighted "Data Health Priority Score".
_STATUS_ATTENTION_RANK: dict[str, int] = {
    STATUS_UNAVAILABLE: 0,
    STATUS_POOR: 1,
    STATUS_DEGRADED: 2,
    STATUS_GOOD: 3,
}


@dataclass
class FleetDataHealthResult:
    as_of: str
    equipment_count: int
    assessed_count: int
    good_count: int
    degraded_count: int
    poor_count: int
    unavailable_count: int

    by_plant: dict[str, dict[str, int]] = field(default_factory=dict)
    by_area: dict[str, dict[str, int]] = field(default_factory=dict)
    by_system: dict[str, dict[str, int]] = field(default_factory=dict)
    by_equipment_type: dict[str, dict[str, int]] = field(default_factory=dict)

    missing_tag_count: int = 0
    stale_tag_count: int = 0
    invalid_tag_count: int = 0
    gap_count: int = 0
    timestamp_issue_count: int = 0
    indeterminate_freshness_count: int = 0
    frozen_candidate_count: int = 0

    equipment: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    model_version: str = DATA_HEALTH_MODEL_VERSION


def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def _plant_codes(config_database_path: str | Path) -> list[str]:
    connection = _connect(config_database_path)
    try:
        return [r["code"] for r in connection.execute("SELECT code FROM plants ORDER BY code")]
    finally:
        connection.close()


def _eligible_instances(config_database_path: str | Path, plant_codes: tuple[str, ...] | None) -> list[tuple[str, str]]:
    """(equipment_type, instance_key) pairs across the requested plants -
    reuses engine.health_engine.discover_health_targets() unchanged, one
    call per plant (it is itself already a bounded, indexed set of
    queries, never a full-table scan)."""
    codes = plant_codes if plant_codes is not None else tuple(_plant_codes(config_database_path))
    pairs: list[tuple[str, str]] = []
    for plant_code in codes:
        pairs.extend(discover_health_targets(config_database_path, plant_code))
    return pairs


def _equipment_hierarchy(config_database_path: str | Path, equipment_ids: list[int]) -> dict[int, dict[str, Any]]:
    """One bulk query for plant/area/system/display_name context - mirrors
    ui/health_data.py's own _equipment_hierarchy() bulk-lookup convention
    exactly (never one query per equipment row)."""
    if not equipment_ids:
        return {}
    connection = _connect(config_database_path)
    placeholders = ",".join("?" for _ in equipment_ids)
    try:
        rows = connection.execute(
            f"""
            SELECT e.id AS equipment_id, e.display_name,
                   p.code AS plant_code, a.name AS area_name, s.name AS system_name
            FROM equipment e
            LEFT JOIN plants p ON p.id = e.plant_id
            LEFT JOIN areas a ON a.id = e.area_id
            LEFT JOIN systems s ON s.id = e.system_id
            WHERE e.id IN ({placeholders})
            """,
            equipment_ids,
        ).fetchall()
    finally:
        connection.close()
    return {r["equipment_id"]: dict(r) for r in rows}


def attention_sort_key(equipment_row: dict[str, Any]) -> tuple:
    """
    Item 10 - a deterministic sort tuple, worst-first, documented here
    rather than left implicit at each call site:

      1. Data Confidence status: UNAVAILABLE, then POOR, then DEGRADED,
         then GOOD.
      2. Lowest Data Confidence score first (an UNAVAILABLE/no-score
         equipment sorts as if its score were below every real score -
         never fabricated as 0).
      3. Largest missing-tag count first.
      4. Any invalid-value tag present, before none.
      5. Any timestamp issue present, before none.
      6. Largest continuity-gap count first.
      7. Largest stale-tag count first.
      8. instance_key, for full determinism on an exact tie.

    Deliberately a plain tuple, never a new weighted "Data Health
    Priority Score".
    """
    status = equipment_row["confidence_status"]
    score = equipment_row["confidence_score"]
    score_rank = -1.0 if score is None else score

    return (
        _STATUS_ATTENTION_RANK.get(status, len(_STATUS_ATTENTION_RANK)),
        score_rank,
        -len(equipment_row["missing_tags"]),
        0 if equipment_row["invalid_tags"] else 1,
        0 if equipment_row["timestamp_issues"] else 1,
        -len(equipment_row["gaps"]),
        -len(equipment_row["stale_tags"]),
        equipment_row["instance_key"],
    )


def _empty_group_counts() -> dict[str, int]:
    return {
        "equipment": 0, "GOOD": 0, "DEGRADED": 0, "POOR": 0, "UNAVAILABLE": 0,
        "missing_tags": 0, "stale_tags": 0, "invalid_tags": 0, "gaps": 0,
    }


def _accumulate_group(groups: dict[str, dict[str, int]], key: str | None, row: dict[str, Any]) -> None:
    if not key:
        return
    bucket = groups.setdefault(key, _empty_group_counts())
    bucket["equipment"] += 1
    status = row["confidence_status"]
    if status in bucket:
        bucket[status] += 1
    bucket["missing_tags"] += len(row["missing_tags"])
    bucket["stale_tags"] += len(row["stale_tags"])
    bucket["invalid_tags"] += len(row["invalid_tags"])
    bucket["gaps"] += len(row["gaps"])


def _issue_rows_for_equipment(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Flattens the equipment's own already-computed per-tag evidence
    (engine.data_health_engine.TagDataHealth, via the EquipmentDataHealth
    it came from) into one issue row per (tag, issue) - pure
    re-presentation of facts already decided by the authoritative
    per-tag engine, never a new classification."""
    issues: list[dict[str, Any]] = []
    for tag in row["tags"]:
        common = {
            "tag_name": tag.tag_name,
            "instance_key": row["instance_key"],
            "display_name": row["display_name"],
            "plant_code": row["plant_code"],
            "last_value": tag.last_value,
            "last_time": tag.last_time,
            "logging_mode": "Log on change" if tag.log_on_change else "Fixed interval",
            "source_driver": row["source_driver"],
        }
        if not tag.available:
            issues.append({**common, "issue": "MISSING"})
            continue
        if tag.validity == VALIDITY_INVALID:
            issues.append({**common, "issue": "INVALID", "detail": tag.invalid_reason})
        if tag.freshness == FRESHNESS_STALE:
            issues.append({**common, "issue": "STALE"})
        if tag.freshness == FRESHNESS_INDETERMINATE:
            issues.append({**common, "issue": "INDETERMINATE_CHANGE_ONLY"})
        if tag.has_gap:
            issues.append({**common, "issue": "CONTINUITY_GAP", "detail": f"{tag.gap_seconds:.0f}s gap"})
        if tag.future_timestamp or tag.non_monotonic:
            issues.append({
                **common, "issue": "TIMESTAMP_ISSUE",
                "detail": "future timestamp" if tag.future_timestamp else "non-monotonic",
            })
        if tag.frozen_candidate:
            issues.append({**common, "issue": "FROZEN_CANDIDATE", "detail": tag.frozen_reason})
    return issues


def calculate_fleet_data_health(
    config_database_path: str | Path,
    machine_database_path: str | Path,
    plant_codes: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> FleetDataHealthResult:
    """
    The Phase 16.4 entry point. Evaluates every eligible equipment
    instance's Data Health via the unchanged Phase 16.1 engine, then
    aggregates the results. Bounded to the currently-eligible equipment
    population (engine.health_engine.discover_health_targets()) - never
    a full-table/unbounded scan.

    One equipment's evaluation raising is isolated (item 29): it is
    recorded as an UNAVAILABLE row with the real error message in
    `evaluation_error` and included in `failures`, never silently
    dropped, never silently GOOD.
    """
    now = now or datetime.now()
    computed_at = now.strftime(TIME_FORMAT)

    pairs = _eligible_instances(config_database_path, plant_codes)

    equipment_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for equipment_type, instance_key in pairs:
        try:
            result = calculate_equipment_data_health(config_database_path, machine_database_path, instance_key, now=now)
            evaluation_error = None
        except Exception as error:
            failures.append({"instance_key": instance_key, "equipment_type": equipment_type, "error": str(error)})
            equipment_rows.append({
                "instance_key": instance_key,
                "equipment_id": None,
                "equipment_type": equipment_type,
                "confidence_score": None,
                "confidence_status": STATUS_UNAVAILABLE,
                "component_scores": {"freshness": None, "availability": None, "validity": None, "continuity": None},
                "component_applicability": {"freshness": False, "availability": False, "validity": False, "continuity": False},
                "required_tag_count": 0, "available_tag_count": 0, "fresh_tag_count": 0,
                "missing_tags": [], "stale_tags": [], "invalid_tags": [],
                "indeterminate_freshness_tags": [], "frozen_candidates": [],
                "gaps": [], "timestamp_issues": [], "tags": [],
                "source_driver": None, "reasons": [], "limitations": [f"Evaluation failed: {error}"],
                "as_of": computed_at, "evaluation_error": str(error),
                "equipment_id_for_hierarchy": None,
            })
            continue

        equipment_rows.append({
            "instance_key": result.instance_key,
            "equipment_id": result.equipment_id,
            "equipment_type": result.equipment_type or equipment_type,
            "confidence_score": result.confidence_score,
            "confidence_status": result.confidence_status,
            "component_scores": dict(result.component_scores),
            "component_applicability": dict(result.component_applicability),
            "required_tag_count": result.required_tag_count,
            "available_tag_count": result.available_tag_count,
            "fresh_tag_count": result.fresh_tag_count,
            "missing_tags": list(result.missing_tags),
            "stale_tags": list(result.stale_tags),
            "invalid_tags": list(result.invalid_tags),
            "indeterminate_freshness_tags": list(result.indeterminate_freshness_tags),
            "frozen_candidates": list(result.frozen_candidates),
            "gaps": list(result.gaps),
            "timestamp_issues": list(result.timestamp_issues),
            "tags": list(result.tags),
            "source_driver": (result.source or {}).get("configured_driver"),
            "reasons": list(result.reasons),
            "limitations": list(result.limitations),
            "as_of": result.computed_at,
            "evaluation_error": None,
        })

    # --- bulk hierarchy enrichment (one query, never one per row) ---
    equipment_ids = [r["equipment_id"] for r in equipment_rows if r["equipment_id"] is not None]
    hierarchy = _equipment_hierarchy(config_database_path, equipment_ids)

    for row in equipment_rows:
        context = hierarchy.get(row["equipment_id"], {})
        row["display_name"] = context.get("display_name") or row["instance_key"]
        row["plant_code"] = context.get("plant_code") or row["instance_key"].split(".")[0].lower()
        row["area_name"] = context.get("area_name")
        row["system_name"] = context.get("system_name")

    # --- aggregation (pure, over already-fetched rows - no new query) ---
    by_plant: dict[str, dict[str, int]] = {}
    by_area: dict[str, dict[str, int]] = {}
    by_system: dict[str, dict[str, int]] = {}
    by_equipment_type: dict[str, dict[str, int]] = {}

    status_counts = {status: 0 for status in CONFIDENCE_STATUS_VALUES}
    missing_tag_count = stale_tag_count = invalid_tag_count = gap_count = 0
    timestamp_issue_count = indeterminate_freshness_count = frozen_candidate_count = 0
    issues: list[dict[str, Any]] = []

    for row in equipment_rows:
        status_counts[row["confidence_status"]] = status_counts.get(row["confidence_status"], 0) + 1

        _accumulate_group(by_plant, row["plant_code"], row)
        _accumulate_group(by_area, row["area_name"], row)
        _accumulate_group(by_system, row["system_name"], row)
        _accumulate_group(by_equipment_type, row["equipment_type"], row)

        missing_tag_count += len(row["missing_tags"])
        stale_tag_count += len(row["stale_tags"])
        invalid_tag_count += len(row["invalid_tags"])
        gap_count += len(row["gaps"])
        timestamp_issue_count += len(row["timestamp_issues"])
        indeterminate_freshness_count += len(row["indeterminate_freshness_tags"])
        frozen_candidate_count += len(row["frozen_candidates"])

        issues.extend(_issue_rows_for_equipment(row))

    equipment_rows.sort(key=attention_sort_key)

    return FleetDataHealthResult(
        as_of=computed_at,
        equipment_count=len(equipment_rows),
        assessed_count=sum(1 for r in equipment_rows if r["confidence_status"] != STATUS_UNAVAILABLE),
        good_count=status_counts.get(STATUS_GOOD, 0),
        degraded_count=status_counts.get(STATUS_DEGRADED, 0),
        poor_count=status_counts.get(STATUS_POOR, 0),
        unavailable_count=status_counts.get(STATUS_UNAVAILABLE, 0),
        by_plant=by_plant, by_area=by_area, by_system=by_system, by_equipment_type=by_equipment_type,
        missing_tag_count=missing_tag_count, stale_tag_count=stale_tag_count, invalid_tag_count=invalid_tag_count,
        gap_count=gap_count, timestamp_issue_count=timestamp_issue_count,
        indeterminate_freshness_count=indeterminate_freshness_count, frozen_candidate_count=frozen_candidate_count,
        equipment=equipment_rows, issues=issues, failures=failures,
    )
