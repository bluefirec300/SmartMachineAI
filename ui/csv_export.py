"""
Shared CSV-export helper for pages that offer a "Download CSV" button
(Event Records, Energy Dashboard - Phase V1.5).

Deliberately tiny: CSV serialization and filename formatting only. No
report layout, aggregation, or calculation logic lives here - each
page builds its own plain DataFrame from data it already has (existing
data-access functions, existing filter/date-range state), then hands
it to dataframe_to_csv_bytes() unchanged.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def export_filename(prefix: str, extension: str = "csv") -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}.{extension}"
