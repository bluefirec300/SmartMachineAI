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

Five semantic tiers, background/text/accent triples chosen for a
muted, higher-contrast "dashboard" look (subtle tint + a left accent
stripe) rather than the brighter flat-pastel fill used elsewhere in
this app. Callers map their OWN existing deterministic state/status
values onto these five tiers - this module never invents a new
classification, it only supplies presentation for classifications the
respective engine already produces.
"""

POSITIVE = "positive"  # e.g. improving / the best available status
STABLE = "stable"      # e.g. steady/normal, no concerning change
CAUTION = "caution"    # e.g. degrading / attention-worthy
WARNING = "warning"    # e.g. significantly degrading / poor
NEUTRAL = "neutral"    # e.g. insufficient evidence / unavailable / not classified

_PALETTE: dict[str, tuple[str, str, str]] = {
    # (background, text, accent)
    POSITIVE: ("#e6f4ea", "#1e6b3f", "#2f7d52"),
    STABLE: ("#eaf0f4", "#2c4a63", "#3f7290"),
    CAUTION: ("#faf0da", "#7a5510", "#b3811f"),
    WARNING: ("#f8e2df", "#8a2f22", "#a63f34"),
    NEUTRAL: ("#eef0f2", "#495057", "#7a8390"),
}


def row_css(tier: str | None) -> str:
    """CSS for one dataframe cell/row for a given semantic tier. An
    unrecognized/None tier renders with no styling (never guesses)."""
    colors = _PALETTE.get(tier)

    if colors is None:
        return ""

    background, text, accent = colors
    return f"background-color: {background}; color: {text}; border-left: 4px solid {accent}; font-weight: 500;"


def badge_html(label: str, tier: str | None) -> str:
    """A single-value colored badge (for a detail-view header, not a
    table) in the same muted palette as row_css()."""
    colors = _PALETTE.get(tier)
    background, text, accent = colors if colors is not None else ("#eef0f2", "#495057", "#7a8390")

    return (
        f"<span style='background-color:{background}; color:{text}; "
        f"border-left: 3px solid {accent}; padding:2px 10px 2px 8px; "
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
