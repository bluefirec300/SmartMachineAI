from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

"""
Tariff versioning + cost calculation - deterministic, no LLM (per
Rule 4). Tariffs are append-only: create_tariff() never UPDATEs an
existing row's rate fields - it closes out (sets expiry_date on)
whichever tariff was previously open-ended for that scope, then
inserts a new row starting where the old one left off. This
guarantees a historical calculation for, say, October always uses
whatever tariff was actually in effect during October, even after a
newer tariff is entered later - "never overwrite historical tariffs" /
"historical calculations must use the tariff valid during that
period", per the roadmap.
"""

SIMULATED_TARIFF_NOTICE = (
    "SIMULATION TARIFF — Configure actual utility tariff for accurate financial calculations."
)

# Used only to auto-generate a placeholder when NO tariff has ever
# been entered for a scope, so the UI/cost calculations always have a
# real number rather than silently showing nothing. RM 0.50/kWh is the
# roadmap's own worked "Simple tariff" example, not an arbitrary guess.
SIMULATED_TARIFF_DEFAULTS: dict[str, Any] = {
    "mode": "simple",
    "currency": "MYR",
    "energy_rate": 0.50,
}


TARIFF_PROVENANCE_CONFIGURED = "CONFIGURED"
TARIFF_PROVENANCE_SIMULATION = "SIMULATION"


def tariff_provenance_label(tariff: dict | None) -> str | None:
    """
    Phase 18.1a.1 - maps an already-resolved tariff row's own
    is_simulated flag (set once, at creation time - see
    create_tariff()/get_current_tariff() above) to a small, explicit
    provenance label. This is the SAME flag Factory Configuration's
    tariff form already sets/clears - never a second, parallel
    provenance system, and never inferred from currency or any other
    proxy (a MYR tariff is not automatically "simulation", and a
    non-MYR tariff is not automatically "real"). `tariff=None` (no
    tariff could be resolved for that scope/date at all) returns None,
    never a guessed label - callers that get None here should leave
    their own cost/provenance fields Unavailable, matching the existing
    "never fabricate" discipline.
    """
    if tariff is None:
        return None

    return TARIFF_PROVENANCE_SIMULATION if tariff.get("is_simulated") else TARIFF_PROVENANCE_CONFIGURED


def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def get_tariff_for_date(database_path: str | Path, plant_id: int | None, on_date) -> dict | None:
    """
    on_date: 'YYYY-MM-DD' string, or any object with .strftime (date/
    datetime). Returns the tariff row that was in effect on that date
    for the given scope (plant_id, or None for the factory-wide
    tariff), or None if nothing has ever been configured for it.
    """
    if hasattr(on_date, "strftime"):
        on_date = on_date.strftime("%Y-%m-%d")

    connection = _connect(database_path)
    try:
        if plant_id is None:
            query = """
                SELECT * FROM energy_tariffs
                WHERE plant_id IS NULL
                  AND effective_date <= ?
                  AND (expiry_date IS NULL OR expiry_date > ?)
                ORDER BY effective_date DESC
                LIMIT 1
            """
            params: tuple = (on_date, on_date)
        else:
            query = """
                SELECT * FROM energy_tariffs
                WHERE plant_id = ?
                  AND effective_date <= ?
                  AND (expiry_date IS NULL OR expiry_date > ?)
                ORDER BY effective_date DESC
                LIMIT 1
            """
            params = (plant_id, on_date, on_date)

        row = connection.execute(query, params).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def create_tariff(
    database_path: str | Path,
    plant_id: int | None,
    effective_date: str,
    username: str,
    mode: str = "simple",
    currency: str | None = None,
    energy_rate: float | None = None,
    peak_rate: float | None = None,
    off_peak_rate: float | None = None,
    peak_start: str | None = None,
    peak_end: str | None = None,
    maximum_demand_charge: float | None = None,
    contract_maximum_demand: float | None = None,
    fixed_monthly_charge: float | None = None,
    surcharge_percent: float | None = None,
    tax_percent: float | None = None,
    billing_cycle: str | None = None,
    is_simulated: bool = False,
) -> int:
    """
    Closes out whichever tariff was previously open-ended for this
    scope (its rate fields are never touched, only expiry_date is set,
    so the old row remains a complete, accurate historical record),
    then inserts this as the new open-ended (expiry_date NULL) tariff.
    """
    connection = _connect(database_path)
    try:
        if plant_id is None:
            previous = connection.execute(
                "SELECT id FROM energy_tariffs WHERE plant_id IS NULL AND expiry_date IS NULL"
            ).fetchone()
        else:
            previous = connection.execute(
                "SELECT id FROM energy_tariffs WHERE plant_id = ? AND expiry_date IS NULL",
                (plant_id,),
            ).fetchone()

        if previous:
            connection.execute(
                "UPDATE energy_tariffs SET expiry_date = ? WHERE id = ?",
                (effective_date, previous["id"]),
            )

        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor = connection.execute(
            """
            INSERT INTO energy_tariffs (
                plant_id, mode, currency, energy_rate, peak_rate, off_peak_rate,
                peak_start, peak_end, maximum_demand_charge, contract_maximum_demand,
                fixed_monthly_charge, surcharge_percent, tax_percent, billing_cycle,
                is_simulated, effective_date, expiry_date, created_at, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                plant_id, mode, currency, energy_rate, peak_rate, off_peak_rate,
                peak_start, peak_end, maximum_demand_charge, contract_maximum_demand,
                fixed_monthly_charge, surcharge_percent, tax_percent, billing_cycle,
                1 if is_simulated else 0, effective_date, created_at, username,
            ),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def get_current_tariff(database_path: str | Path, plant_id: int | None, username: str | None = None) -> dict:
    """
    Returns the tariff currently in effect for this scope, auto-
    generating and persisting a SIMULATION TARIFF (see
    SIMULATED_TARIFF_DEFAULTS) if none has ever been entered - so a
    caller always gets a real, usable tariff back, clearly flagged via
    is_simulated=1 rather than having to separately handle "no tariff
    configured yet" as its own case.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    tariff = get_tariff_for_date(database_path, plant_id, today)

    if tariff is not None:
        return tariff

    create_tariff(
        database_path,
        plant_id=plant_id,
        effective_date=today,
        username=username or "system",
        is_simulated=True,
        **SIMULATED_TARIFF_DEFAULTS,
    )
    return get_tariff_for_date(database_path, plant_id, today)


def get_tariff_history(database_path: str | Path, plant_id: int | None) -> list[dict]:
    connection = _connect(database_path)
    try:
        if plant_id is None:
            rows = connection.execute(
                "SELECT * FROM energy_tariffs WHERE plant_id IS NULL ORDER BY effective_date DESC"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM energy_tariffs WHERE plant_id = ? ORDER BY effective_date DESC",
                (plant_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def calculate_energy_cost(kwh: float | None, tariff: dict) -> float | None:
    """
    Flat-rate cost using the tariff's base energy_rate - correct for
    simple-mode tariffs, and a reasonable current approximation for
    advanced-mode ones too. Splitting cost by actual peak/off-peak kWh
    needs per-interval consumption data that isn't computed anywhere
    yet (that's Phase 6 Energy KPI Engine's job) - advanced-mode
    tariffs already store peak_rate/off_peak_rate/demand-charge fields
    now so that future work has them available without another schema
    change. Returns None if kwh or the tariff's rate is unavailable,
    never a fabricated number.
    """
    rate = tariff.get("energy_rate")

    if rate is None or kwh is None:
        return None

    return round(kwh * rate, 2)
