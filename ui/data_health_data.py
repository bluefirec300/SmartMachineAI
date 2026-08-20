from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_config_db_path, get_machine_db_path
from engine import data_health_engine as dhe
from engine import data_health_targets as dht
from ui import table_style as ts

"""
Phase 16.2 - UI data-access/presentation layer for Data Health. Mirrors
ui/health_data.py's / ui/maintenance_intelligence_data.py's own
convention exactly: calls engine.data_health_engine directly (Phase 16.1
has no persisted table of its own - same on-demand architecture Phase
13 already established) and converts the result into a plain,
presentation-ready dict. This module performs NO Data Health
calculation, NO LLM call, and NO database write of any kind - read-only,
exactly like every sibling *_data.py module.

Every function here is defensive: a genuine exception from the engine
(e.g. an unreachable database) is caught and converted into a safe,
honestly-labeled UNAVAILABLE result - never silently treated as GOOD,
and never allowed to crash the page (item P).
"""

CONFIG_DATABASE_PATH = get_config_db_path()
MACHINE_DATABASE_PATH = get_machine_db_path()

# UI polish phase - which semantic tier (ui.table_style) each existing
# deterministic status maps to for display. Reinforces the engine's
# own GOOD/DEGRADED/POOR/UNAVAILABLE classification - introduces no
# new status, only presentation.
STATUS_TIERS: dict[str, str] = {
    dht.STATUS_GOOD: ts.POSITIVE,
    dht.STATUS_DEGRADED: ts.CAUTION,
    dht.STATUS_POOR: ts.WARNING,
    dht.STATUS_UNAVAILABLE: ts.NEUTRAL,
}

STATUS_LABELS: dict[str, str] = {
    dht.STATUS_GOOD: "Good",
    dht.STATUS_DEGRADED: "Degraded",
    dht.STATUS_POOR: "Poor",
    dht.STATUS_UNAVAILABLE: "Unavailable",
}

# Presentation-only - the canonical FRESHNESS_*/TAG_DISPLAY_* values from
# engine.data_health_targets remain fully visible in the tag-level detail
# table alongside these labels (never silently renamed away, matching
# Phase 12.3A's own FACTOR_LABELS discipline).
FRESHNESS_LABELS: dict[str, str] = {
    dht.FRESHNESS_FRESH: "Fresh",
    dht.FRESHNESS_STALE: "Stale",
    dht.FRESHNESS_INDETERMINATE: "Indeterminate (change-only tag)",
}

DISPLAY_STATE_LABELS: dict[str, str] = {
    dht.TAG_DISPLAY_MISSING: "Missing",
    dht.TAG_DISPLAY_INVALID: "Invalid",
    dht.TAG_DISPLAY_STALE: "Stale",
    dht.TAG_DISPLAY_FROZEN_CANDIDATE: "Frozen candidate (advisory)",
    dht.TAG_DISPLAY_FRESH: "Fresh",
    dht.TAG_DISPLAY_INDETERMINATE: "Indeterminate (change-only tag)",
}

# Configured-source display only - deliberately never implies a live
# connection-health claim (item L / item H's carried-forward limitation).
_DRIVER_LABELS: dict[str, str] = {
    "simulator": "Simulator",
    "modbus": "Modbus",
    "opcua": "OPC UA",
    "s7": "S7",
    "fins": "FINS",
}


def status_label(status: str | None) -> str:
    if not status:
        return "-"
    return STATUS_LABELS.get(status, status)


def status_badge_html(status: str | None) -> str:
    return ts.badge_html(status_label(status), STATUS_TIERS.get(status))


def confidence_score_text(score: float | None) -> str:
    if score is None:
        return "Not Available"
    return f"{score:.0f} / 100"


def component_score_text(score: float | None, applicable: bool) -> str:
    if not applicable or score is None:
        return "N/A"
    return f"{score:.0f}"


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 0:
        return f"{abs(seconds):.0f}s in the future"
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _tag_descriptions(config_database_path: str | Path, tag_names: tuple[str, ...]) -> dict[str, str]:
    """Presentation-layer enrichment only (a plain read) - mirrors the
    same pattern ui/health_data.py's own _equipment_hierarchy() uses for
    display-only context, never a Data Health calculation."""
    if not tag_names:
        return {}
    connection = sqlite3.connect(config_database_path)
    try:
        placeholders = ",".join("?" for _ in tag_names)
        rows = connection.execute(
            f"SELECT tag_name, description FROM tags WHERE tag_name IN ({placeholders})", tag_names,
        ).fetchall()
        return {row[0]: (row[1] or "") for row in rows}
    finally:
        connection.close()


def _format_tag(tag: dhe.TagDataHealth, description: str) -> dict[str, Any]:
    if tag.continuity_applicable:
        continuity_label = f"Gap detected ({tag.gap_seconds:.0f}s)" if tag.has_gap else "No gap detected"
    else:
        continuity_label = "N/A (change-only tag)"

    return {
        "tag_name": tag.tag_name,
        "description": description,
        "last_value": "-" if tag.last_value is None else f"{tag.last_value}",
        "last_time": tag.last_time or "Never logged",
        "age": _format_age(tag.seconds_since_update),
        "logging_interval": f"{int(tag.logging_interval_seconds)}s" if tag.logging_interval_seconds else "-",
        "logging_mode": "Log on change" if tag.log_on_change else "Fixed interval",
        "availability": "Available" if tag.available else "Missing (never logged)",
        "freshness": FRESHNESS_LABELS.get(tag.freshness, "-") if tag.freshness else "-",
        "validity": (
            "Valid" if tag.validity == dht.VALIDITY_VALID
            else (f"Invalid ({tag.invalid_reason})" if tag.validity == dht.VALIDITY_INVALID else "-")
        ),
        "continuity": continuity_label,
        "frozen_candidate": "Frozen candidate (advisory)" if tag.frozen_candidate else "-",
        "timestamp_issue": (
            "Future timestamp" if tag.future_timestamp
            else ("Non-monotonic" if tag.non_monotonic else "-")
        ),
        "display_state": DISPLAY_STATE_LABELS.get(tag.display_state, tag.display_state),
    }


def _empty_result(instance_key: str, reason: str) -> dict[str, Any]:
    return {
        "instance_key": instance_key, "equipment_type": None, "error": None,
        "confidence_score": None, "confidence_status": dht.STATUS_UNAVAILABLE,
        "confidence_score_text": confidence_score_text(None),
        "confidence_status_label": status_label(dht.STATUS_UNAVAILABLE),
        "component_scores": {"freshness": None, "availability": None, "validity": None, "continuity": None},
        "component_applicability": {"freshness": False, "availability": False, "validity": False, "continuity": False},
        "component_display": {"freshness": "N/A", "availability": "N/A", "validity": "N/A", "continuity": "N/A"},
        "required_tag_count": 0, "available_tag_count": 0, "fresh_tag_count": 0,
        "stale_tags": [], "missing_tags": [], "invalid_tags": [], "indeterminate_freshness_tags": [],
        "frozen_candidates": [], "gaps": [], "timestamp_issues": [],
        "source_driver": None, "source_label": "Unknown", "source_note": "",
        "tags": [], "limitations": [reason], "reasons": [reason],
        "model_version": dht.DATA_HEALTH_MODEL_VERSION, "computed_at": "",
    }


def get_equipment_data_health(instance_key: str, now=None) -> dict[str, Any]:
    """
    The one entry point this page uses. Calls the deterministic Phase
    16.1 engine directly - performs NO calculation itself - and returns
    a plain, presentation-ready dict (never a raw dataclass leaking into
    Streamlit code, so a future engine-internal shape change can't
    silently break rendering).

    Never raises - any genuine failure (unreachable database, malformed
    engine result) is caught and converted into a safe, honestly-labeled
    UNAVAILABLE result with the real error message preserved for
    diagnosis, never silently presented as GOOD (item P).
    """
    if not instance_key:
        return _empty_result(instance_key or "", "No equipment selected.")

    try:
        result = dhe.calculate_equipment_data_health(
            CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH, instance_key, now=now,
        )
    except Exception as error:
        return _empty_result(instance_key, f"Data Health could not be evaluated: {error}")

    descriptions = _tag_descriptions(CONFIG_DATABASE_PATH, tuple(tag.tag_name for tag in result.tags))

    driver = result.source.get("configured_driver") if result.source else None

    return {
        "instance_key": result.instance_key,
        "equipment_type": result.equipment_type,
        "error": None,
        "confidence_score": result.confidence_score,
        "confidence_status": result.confidence_status,
        "confidence_score_text": confidence_score_text(result.confidence_score),
        "confidence_status_label": status_label(result.confidence_status),
        "component_scores": result.component_scores,
        "component_applicability": result.component_applicability,
        "component_display": {
            name: component_score_text(result.component_scores.get(name), result.component_applicability.get(name, False))
            for name in ("freshness", "availability", "validity", "continuity")
        },
        "required_tag_count": result.required_tag_count,
        "available_tag_count": result.available_tag_count,
        "fresh_tag_count": result.fresh_tag_count,
        "stale_tags": result.stale_tags,
        "missing_tags": result.missing_tags,
        "invalid_tags": result.invalid_tags,
        "indeterminate_freshness_tags": result.indeterminate_freshness_tags,
        "frozen_candidates": result.frozen_candidates,
        "gaps": result.gaps,
        "timestamp_issues": result.timestamp_issues,
        "source_driver": driver,
        "source_label": _DRIVER_LABELS.get(driver, driver or "Unknown"),
        "source_note": (result.source or {}).get("note", ""),
        "tags": [_format_tag(tag, descriptions.get(tag.tag_name, "")) for tag in result.tags],
        "limitations": result.limitations,
        "reasons": result.reasons,
        "model_version": result.model_version,
        "computed_at": result.computed_at,
    }
