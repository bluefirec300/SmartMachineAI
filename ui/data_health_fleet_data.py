from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path
from engine.data_health_fleet import FleetDataHealthResult, calculate_fleet_data_health
from ui.data_health_data import component_score_text, confidence_score_text, status_label
from ui.health_data import equipment_type_label

"""
Phase 16.4 - UI data-access/presentation layer for FLEET Data Health.
Mirrors ui/data_health_data.py's own convention exactly: this module
performs NO Data Health calculation of any kind - it calls
engine.data_health_fleet.calculate_fleet_data_health() (which itself
only aggregates engine.data_health_engine's already-authoritative,
per-equipment results - see that module's own docstring) and converts
the result into plain, presentation-ready dicts.

Caching architecture (the item-3/item-18/item-19 decision, measured
before choosing it - see this phase's architecture-audit report):
measured full-fleet evaluation cost against the real live databases was
~6.1s for the 34 currently-eligible equipment instances (~180ms
average per equipment, worst single equipment ~300ms). That is far too
slow to recompute on every Streamlit rerun (a rerun fires on every
widget interaction, e.g. changing a filter), but Data Health does not
need sub-minute freshness either (Phase 16.1's own telemetry-staleness
thresholds are themselves measured in multiples of each tag's logging
interval, generally minutes). st.cache_data(ttl=FLEET_CACHE_TTL_SECONDS)
is the smallest safe architecture that satisfies both constraints -
already the established pattern in this codebase (see
ui/energy_dashboard_data.py's CURRENT_TTL/TODAY_TTL/HISTORICAL_TTL and
ui/scada_floor_plan_data.py's own @st.cache_data(ttl=5)) - so no new
caching mechanism, no background worker, and no persistence were
introduced. The cache is process-wide (Streamlit's own cache_data
semantics), so every browser session/user shares one fleet result and
one 6-second cost per refresh window, not one per session.

One equipment's evaluation failing is isolated at the engine layer
(engine.data_health_fleet.calculate_fleet_data_health) - never
silently dropped, never silently GOOD - and surfaces here unchanged in
the `failures` list and as an UNAVAILABLE equipment row.
"""

CONFIG_DATABASE_PATH = get_config_db_path()
MACHINE_DATABASE_PATH = get_machine_db_path()

# 2 minutes: ~19x headroom over the measured ~6.1s full-fleet cost, and
# well within "Data Health does not require 2-second freshness" - see
# this module's docstring for the full measurement/rationale.
FLEET_CACHE_TTL_SECONDS = 120


def _tag_to_dict(tag) -> dict[str, Any]:
    return {
        "tag_name": tag.tag_name,
        "available": tag.available,
        "last_value": tag.last_value,
        "last_time": tag.last_time,
        "freshness": tag.freshness,
        "validity": tag.validity,
        "log_on_change": tag.log_on_change,
        "frozen_candidate": tag.frozen_candidate,
        "display_state": tag.display_state,
    }


def _equipment_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Drops the raw engine.data_health_engine.TagDataHealth objects from
    `tags` (never leaked into Streamlit code, matching
    ui/data_health_data.py's own discipline) - the fleet-level `issues`
    list (see engine.data_health_fleet._issue_rows_for_equipment) already
    carries every per-tag finding in a presentation-ready shape, so
    nothing is lost."""
    result = {k: v for k, v in row.items() if k != "tags"}
    result["equipment_type_label"] = equipment_type_label(row["equipment_type"])
    result["confidence_score_text"] = confidence_score_text(row["confidence_score"])
    result["confidence_status_label"] = status_label(row["confidence_status"])
    result["component_display"] = {
        name: component_score_text(row["component_scores"].get(name), row["component_applicability"].get(name, False))
        for name in ("freshness", "availability", "validity", "continuity")
    }
    return result


def _result_to_dict(result: FleetDataHealthResult) -> dict[str, Any]:
    return {
        "as_of": result.as_of,
        "equipment_count": result.equipment_count,
        "assessed_count": result.assessed_count,
        "good_count": result.good_count,
        "degraded_count": result.degraded_count,
        "poor_count": result.poor_count,
        "unavailable_count": result.unavailable_count,
        "by_plant": result.by_plant,
        "by_area": result.by_area,
        "by_system": result.by_system,
        "by_equipment_type": result.by_equipment_type,
        "missing_tag_count": result.missing_tag_count,
        "stale_tag_count": result.stale_tag_count,
        "invalid_tag_count": result.invalid_tag_count,
        "gap_count": result.gap_count,
        "timestamp_issue_count": result.timestamp_issue_count,
        "indeterminate_freshness_count": result.indeterminate_freshness_count,
        "frozen_candidate_count": result.frozen_candidate_count,
        "equipment": [_equipment_to_dict(row) for row in result.equipment],
        "issues": list(result.issues),
        "failures": list(result.failures),
        "model_version": result.model_version,
    }


@st.cache_data(ttl=FLEET_CACHE_TTL_SECONDS)
def get_fleet_data_health(plant_codes: tuple[str, ...] | None = None) -> dict[str, Any]:
    """
    The one entry point the fleet page uses. `plant_codes=None` means
    "every plant" - the SAME cache entry is naturally reused by both the
    fleet page and Home.py's summary block as long as they call this
    with the same arguments (Streamlit's cache_data keys on the
    function + its arguments), so Home never triggers a second,
    independent fleet evaluation.
    """
    result = calculate_fleet_data_health(CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH, plant_codes=plant_codes)
    return _result_to_dict(result)


def force_refresh() -> None:
    """Explicit engineer-requested refresh - bypasses the TTL cache for
    THIS function only (never a global Streamlit cache clear, which
    would also evict every other page's unrelated cached data)."""
    get_fleet_data_health.clear()


def filter_options(equipment_rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Distinct values actually present in the CURRENT fleet result -
    mirrors ui/health_data.py's own get_filter_options() convention."""
    return {
        "plants": sorted({r["plant_code"] for r in equipment_rows if r["plant_code"]}),
        "areas": sorted({r["area_name"] for r in equipment_rows if r["area_name"]}),
        "systems": sorted({r["system_name"] for r in equipment_rows if r["system_name"]}),
        "equipment_types": sorted({r["equipment_type"] for r in equipment_rows if r["equipment_type"]}),
    }
