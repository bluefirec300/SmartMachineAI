from __future__ import annotations

from collections.abc import Callable

import pandas as pd

"""
UI polish phase - a small, shared table/badge styling helper for
Asset Performance and Data Health specifically (the two pages this
phase touches). Deliberately NOT a repo-wide restyle: every other page
that already highlights rows (Event Records, Anomalies, Live Data,
Savings Verification, Energy Opportunities) keeps its own existing
pastel palette untouched - rewiring those was out of scope and is not
done here.

Revision (user feedback): the first version of this palette used
muted, low-saturation tints - reported as too hard to tell apart
(green vs. grey in particular). Replaced with the SAME bright, fully-
saturated pastel-fill convention already used by Event Records/
Anomalies (`#ffb3b3`/`#ffe6a3`/`#d6e9ff`/etc., dark text, no accent
border) for consistency and clearer at-a-glance differentiation,
rather than inventing a second, competing visual language. Callers map
their OWN existing deterministic state/status values onto these five
tiers - this module never invents a new classification, it only
supplies presentation for classifications the respective engine
already produces.
"""

POSITIVE = "positive"  # e.g. improving / the best available status
STABLE = "stable"      # e.g. steady/normal, no concerning change
CAUTION = "caution"    # e.g. degrading / attention-worthy
WARNING = "warning"    # e.g. significantly degrading / poor
NEUTRAL = "neutral"    # e.g. insufficient evidence / unavailable / not classified

_PALETTE: dict[str, tuple[str, str]] = {
    # (background, text) - same values as Event Records'/Anomalies' own
    # severity_colors dicts, so a color means the same thing everywhere
    # in the app.
    POSITIVE: ("#b3ffb3", "#1a1a1a"),
    STABLE: ("#d6e9ff", "#1a1a1a"),
    CAUTION: ("#ffe6a3", "#1a1a1a"),
    WARNING: ("#ffb3b3", "#1a1a1a"),
    NEUTRAL: ("#e6e6e6", "#1a1a1a"),
}


def row_css(tier: str | None) -> str:
    """CSS for one dataframe cell/row for a given semantic tier. An
    unrecognized/None tier renders with no styling (never guesses)."""
    colors = _PALETTE.get(tier)

    if colors is None:
        return ""

    background, text = colors
    return f"background-color: {background}; color: {text}"


def badge_html(label: str, tier: str | None) -> str:
    """A single-value colored badge (for a detail-view header, not a
    table), same palette as row_css()."""
    background, text = _PALETTE.get(tier, ("#e6e6e6", "#1a1a1a"))

    return (
        f"<span style='background-color:{background}; color:{text}; padding:2px 10px; "
        f"border-radius:4px; font-weight:600;'>{label}</span>"
    )


def make_row_highlighter(column: str, value_to_tier: dict[str, str]) -> Callable[[pd.Series], list[str]]:
    """
    Returns a function suitable for `DataFrame.style.apply(fn, axis=1)`
    that colors an entire row by the semantic tier of one column's
    value. `value_to_tier` maps the table's own display value (e.g. a
    state label) to one of this module's five tier constants - callers
    own that mapping, this function only applies it.
    """

    def _highlight(row: pd.Series) -> list[str]:
        return [row_css(value_to_tier.get(row[column]))] * len(row)

    return _highlight
