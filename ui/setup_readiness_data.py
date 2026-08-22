"""
Phase V2.7 - New-Factory Setup Readiness. Turns
docs/NEW_FACTORY_SETUP_CHECKLIST.md's 7 items (+ the roadmap's
explicit "equipment/tag setup completeness" addition) into structured
data for an in-app dashboard - reads existing, already-computed state
only. Adds no new confirmation mechanism, no new "reviewed" flags, and
proposes no real engineering/site values anywhere - this module can
only ever report what's already true in the database or config files,
never invent a number.

Each get_*_status() function returns a plain dict, never a Streamlit
widget - rendering is entirely the page's job, matching the same
separation engine/configuration_completeness.py already established.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from config.config_manager import ConfigManager
from config.environment import ENVIRONMENTS
from config.plc_connection_manager import PLCConnectionManager
from engine.configuration_completeness import calculate_overall_completeness
from ui.data_access import CONFIG_DATABASE_PATH, is_synthetic_reference_manual

# Endpoints/hosts commonly used by local test tooling (the OPC UA test
# simulator referenced throughout this project's own docs, or a bare
# loopback address) - flagged as a soft, honest hint only. This module
# never claims to KNOW a connection is fake; it only points out an
# address pattern worth double-checking against real site information.
_LOCAL_ADDRESS_HINTS = ("0.0.0.0", "127.0.0.1", "localhost")

SIMULATION_TUNING_REGISTRIES: tuple[dict[str, str], ...] = (
    {"domain": "Anomaly detection", "file": "engine/anomaly_targets.py", "defines": "Deviation thresholds, persistence windows, per-tag-type rules"},
    {"domain": "Equipment Health", "file": "engine/health_targets.py", "defines": "Factor weights, severity penalties, band cutoffs (0-100 score)"},
    {"domain": "Asset Performance", "file": "engine/performance_targets.py", "defines": "State-change cut points (Improving/Stable/Degrading)"},
    {"domain": "Maintenance Intelligence", "file": "engine/maintenance_intelligence_targets.py", "defines": "Priority band cutoffs"},
    {"domain": "Energy Opportunities", "file": "engine/opportunity_targets.py", "defines": "Which anomaly patterns qualify as an opportunity"},
)


def _connect(database_path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def get_area_system_status(database_path: Path | str = CONFIG_DATABASE_PATH) -> dict[str, Any]:
    connection = _connect(database_path)
    try:
        areas = connection.execute("SELECT source FROM areas").fetchall()
        systems = connection.execute("SELECT source FROM systems").fetchall()
    finally:
        connection.close()

    def _tally(rows) -> dict[str, int]:
        confirmed = sum(1 for r in rows if r["source"] != "inferred_from_simulation")
        return {"total": len(rows), "confirmed": confirmed, "unconfirmed": len(rows) - confirmed}

    area_tally = _tally(areas)
    system_tally = _tally(systems)
    total = area_tally["total"] + system_tally["total"]
    confirmed = area_tally["confirmed"] + system_tally["confirmed"]

    return {
        "areas": area_tally,
        "systems": system_tally,
        "percent_confirmed": round(100 * confirmed / total) if total else 100,
    }


def get_tariff_status(database_path: Path | str = CONFIG_DATABASE_PATH) -> dict[str, Any]:
    connection = _connect(database_path)
    try:
        rows = connection.execute(
            "SELECT plant_id, is_simulated, currency, energy_rate FROM energy_tariffs WHERE expiry_date IS NULL"
        ).fetchall()
    finally:
        connection.close()

    scopes = [
        {
            "scope": "Factory-wide" if row["plant_id"] is None else f"Plant {row['plant_id']}",
            "is_simulated": bool(row["is_simulated"]),
            "currency": row["currency"],
            "energy_rate": row["energy_rate"],
        }
        for row in rows
    ]
    real_count = sum(1 for s in scopes if not s["is_simulated"])

    return {"scopes": scopes, "total": len(scopes), "real_count": real_count}


def get_documentation_status(database_path: Path | str = CONFIG_DATABASE_PATH) -> dict[str, Any]:
    connection = _connect(database_path)
    try:
        rows = connection.execute(
            "SELECT equipment_id, file_path FROM equipment_documents WHERE equipment_id IS NULL"
        ).fetchall()
    finally:
        connection.close()

    reference_manuals = [dict(row) for row in rows]
    synthetic = [doc for doc in reference_manuals if is_synthetic_reference_manual(doc)]

    return {
        "total_reference_manuals": len(reference_manuals),
        "synthetic_count": len(synthetic),
        "real_count": len(reference_manuals) - len(synthetic),
    }


def get_plc_connection_status() -> dict[str, Any]:
    """Always checks the Actual environment's own plc_connections table
    specifically (not whichever environment happens to be active right
    now) - this item is about whether a real connection has EVER been
    configured for eventual production use."""
    actual_config_db = ENVIRONMENTS["actual"]["config_db"]

    if not Path(actual_config_db).exists():
        return {"available": False, "active_connection": None}

    manager = PLCConnectionManager(database_path=actual_config_db)
    active = manager.get_active()

    if active is None:
        return {"available": True, "active_connection": None}

    address = str(active.get("endpoint_url") or active.get("host") or "")
    looks_local = any(hint in address for hint in _LOCAL_ADDRESS_HINTS)

    return {
        "available": True,
        "active_connection": {
            "name": active["name"],
            "protocol": active["protocol"],
            "address": address,
            "looks_local": looks_local,
        },
    }


def get_user_account_status(database_path: Path | str = CONFIG_DATABASE_PATH) -> dict[str, Any]:
    connection = _connect(database_path)
    try:
        rows = connection.execute("SELECT username, role, active FROM users ORDER BY username").fetchall()
    finally:
        connection.close()

    accounts = [dict(row) for row in rows]
    return {
        "accounts": accounts,
        "total": len(accounts),
        "active_count": sum(1 for a in accounts if a["active"]),
        "admin_count": sum(1 for a in accounts if a["role"] == "admin" and a["active"]),
    }


def get_backup_status() -> dict[str, Any]:
    config = ConfigManager()
    destination = config.historian_backup_destination_dir
    project_root = Path(__file__).resolve().parent.parent
    looks_like_default_local_path = Path(destination).resolve() == (project_root / "backups").resolve()

    return {
        "enabled": config.historian_backup_enabled,
        "destination": str(destination),
        "looks_like_default_local_path": looks_like_default_local_path,
    }


def get_configuration_completeness_status(database_path: Path | str = CONFIG_DATABASE_PATH) -> dict[str, Any]:
    return calculate_overall_completeness(database_path)


def get_all_readiness_status(database_path: Path | str = CONFIG_DATABASE_PATH) -> dict[str, Any]:
    return {
        "area_system": get_area_system_status(database_path),
        "tariff": get_tariff_status(database_path),
        "documentation": get_documentation_status(database_path),
        "plc_connection": get_plc_connection_status(),
        "user_accounts": get_user_account_status(database_path),
        "backup": get_backup_status(),
        "configuration_completeness": get_configuration_completeness_status(database_path),
    }
