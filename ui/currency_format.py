from __future__ import annotations

"""
Phase 18.1a - shared money-display formatting. The Phase 18 audit found
`RM` (Malaysian Ringgit's display symbol) hardcoded as a literal string
in ui/pages/17_Energy_Opportunities.py and ui/pages/18_Savings_Verification.py's
money formatters, even though the underlying data (energy_opportunities'
observed_cost_currency/saving_currency, savings_verification_results'
verified_cost_currency, energy_tariffs.currency) already stores the
real ISO 4217 currency code per row/tariff - the hardcoding was purely
a display-layer bug, not a missing data model.

This module does NOT change how currency is entered or stored (still a
free-text ISO-4217-code field on energy_tariffs, unconstrained - no
schema change) and performs NO foreign-exchange conversion - it only
maps a stored code to a display symbol for a nicer label, falling back
to the bare code itself for anything not in the map so a new
deployment in an unlisted currency still displays correctly (just
without a special symbol) rather than defaulting to RM.
"""

CURRENCY_SYMBOLS: dict[str, str] = {
    "MYR": "RM",
    "SGD": "S$",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "AUD": "A$",
    "JPY": "¥",
    "THB": "฿",
    "IDR": "Rp",
}

# Matches this project's current demo/simulation configuration
# (Malaysia, MYR) - used only when no currency code is available at
# all (e.g. a legacy row predating currency tracking), never as a
# silent override of a real stored code.
DEFAULT_CURRENCY_CODE = "MYR"


def currency_symbol(currency_code: str | None) -> str:
    code = (currency_code or DEFAULT_CURRENCY_CODE).strip().upper()
    return CURRENCY_SYMBOLS.get(code, code)


def format_money(value: float | int | None, currency_code: str | None = None) -> str | None:
    """
    Returns None (never a fabricated "0.00") when `value` is None, so
    every call site keeps its own existing "Unavailable"/"Not yet
    estimable"/etc. wording instead of this shared helper picking one
    for them.
    """
    if value is None:
        return None

    return f"{currency_symbol(currency_code)} {float(value):,.2f}"
