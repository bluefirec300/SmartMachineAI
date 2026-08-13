"""
Resolves which database files the app should use right now -
"simulation" (the demo/presentation dataset, safe to keep forever) or
"actual" (a real deployment against a real PLC, starting empty).

This is the single source of truth every other module should read
through, instead of hardcoding database/config.db or
database/machine_data.db directly - that's what makes switching
environments a one-line change here rather than a hunt through the
whole codebase.

The active environment is a plain marker file, deliberately living
outside both database/simulation/ and database/actual/ - it has to,
since it's what decides which of those two you're even looking at.

Like every other config change in this project, a process that already
imported this module (plc_logger, event_monitor, streamlit) keeps
using whatever was active when it started - switching environments
takes effect on that process's next restart, not live. This matches
the existing pattern (e.g. config/settings.ini changes, PLC connection
switches) rather than introducing a new one.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_ENVIRONMENT_FILE = PROJECT_ROOT / "config" / "active_environment.txt"

ENVIRONMENTS: dict[str, dict[str, Path]] = {
    "simulation": {
        "config_db": PROJECT_ROOT / "database" / "simulation" / "config.db",
        "machine_db": PROJECT_ROOT / "database" / "simulation" / "machine_data.db",
    },
    "actual": {
        "config_db": PROJECT_ROOT / "database" / "actual" / "config.db",
        "machine_db": PROJECT_ROOT / "database" / "actual" / "machine_data.db",
    },
}

DEFAULT_ENVIRONMENT = "simulation"

ENVIRONMENT_LABELS = {
    "simulation": "Simulation (demo dataset)",
    "actual": "Actual (real PLC deployment)",
}


def get_active_environment() -> str:
    if ACTIVE_ENVIRONMENT_FILE.exists():
        value = ACTIVE_ENVIRONMENT_FILE.read_text(encoding="utf-8").strip()
        if value in ENVIRONMENTS:
            return value

    return DEFAULT_ENVIRONMENT


def set_active_environment(name: str) -> None:
    if name not in ENVIRONMENTS:
        raise ValueError(f"Unknown environment: {name!r}. Must be one of {sorted(ENVIRONMENTS)}")

    ACTIVE_ENVIRONMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_ENVIRONMENT_FILE.write_text(name, encoding="utf-8")


def get_config_db_path() -> Path:
    return ENVIRONMENTS[get_active_environment()]["config_db"]


def get_machine_db_path() -> Path:
    return ENVIRONMENTS[get_active_environment()]["machine_db"]
