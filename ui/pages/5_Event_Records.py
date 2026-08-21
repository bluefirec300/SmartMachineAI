from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.event_store import EventStore
from ai.trend_analyzer import format_number
from ui.csv_export import dataframe_to_csv_bytes
from ui.csv_export import export_filename as export_csv_filename
from ui.data_access import MACHINE_DATABASE_PATH, get_event_filter_options
from ui.pdf_export import build_pdf_report
from ui.pdf_export import export_filename as export_pdf_filename


st.title("📋 Event Records")
st.caption("Alarm and warning history recorded by the background event monitor.")

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

TIME_RANGES = {
    "Last 24 hours": timedelta(hours=24),
    "Last 7 days": timedelta(days=7),
    "Last 30 days": timedelta(days=30),
    "All time": None,
}


@st.cache_resource
def _get_event_store() -> EventStore:
    return EventStore(database_path=MACHINE_DATABASE_PATH)


event_store = _get_event_store()
filter_options = get_event_filter_options()

filter_cols = st.columns(4)

with filter_cols[0]:
    severity = st.selectbox("Severity", ["All", "alarm", "warning"])

with filter_cols[1]:
    equipment = st.selectbox("Equipment", ["All"] + filter_options["equipment"])

with filter_cols[2]:
    tag = st.selectbox("Tag", ["All"] + filter_options["tags"])

with filter_cols[3]:
    time_range_label = st.selectbox("Time range", list(TIME_RANGES.keys()))

limit = st.slider("Max rows to show", min_value=50, max_value=2000, value=200, step=50)

start_time = None
window = TIME_RANGES[time_range_label]
if window is not None:
    start_time = (datetime.now() - window).strftime(TIMESTAMP_FORMAT)

filter_kwargs = {
    "severity": None if severity == "All" else severity,
    "equipment": None if equipment == "All" else equipment,
    "tag": None if tag == "All" else tag,
    "start_time": start_time,
}

total_matching = event_store.count_events(**filter_kwargs)
events = event_store.get_recent_events(limit=limit, **filter_kwargs)

if total_matching > len(events):
    st.caption(f"Showing the most recent {len(events)} of {total_matching} matching events.")
else:
    st.caption(f"{total_matching} matching event(s).")

if not events:
    st.info("No events match the current filters.")
    st.stop()


def events_to_dataframe(events: list[dict]) -> pd.DataFrame:
    """
    Shared by the on-screen table and the CSV export (Phase V1.5) - both
    show exactly the same rows for whatever filters are currently
    applied, since they're built from this one function.
    """
    rows = []
    for event in events:
        value_text = ""
        if event.get("value") is not None:
            value_text = format_number(event["value"])
            if event.get("unit"):
                value_text += f" {event['unit']}"

        rows.append(
            {
                "Time": event["event_time"],
                "Equipment": event["equipment"],
                "Tag": event["tag"],
                "Severity": event["severity"].upper(),
                "Condition": event["condition"],
                "Value": value_text,
                "Message": event.get("message") or "",
            }
        )

    return pd.DataFrame(rows)


df = events_to_dataframe(events)

# Explicit text color alongside background - Streamlit's dark theme
# defaults to white table text, which is unreadable against these
# light backgrounds if only background-color is set.
severity_colors = {
    "ALARM": "background-color: #ffb3b3; color: #1a1a1a",
    "WARNING": "background-color: #ffe6a3; color: #1a1a1a",
}


def _highlight_severity(row: pd.Series) -> list[str]:
    color = severity_colors.get(row["Severity"], "")
    return [color] * len(row)


download_cols = st.columns(2)

with download_cols[0]:
    st.download_button(
        "⬇️ Download CSV",
        data=dataframe_to_csv_bytes(df),
        file_name=export_csv_filename("event_records"),
        mime="text/csv",
        help="Exports exactly the rows shown below, with the current Severity/Equipment/Tag/Time range filters applied.",
    )

with download_cols[1]:
    # PDF rendering costs ~5ms/row (reportlab Paragraph flowables per
    # cell) and st.download_button evaluates `data=` eagerly on every
    # page rerun, not just on click - at the slider's full 2000-row
    # max that's ~11s added to EVERY interaction on this page, not just
    # downloads. Capped independently of "Max rows to show" (which CSV
    # still honors in full) so the PDF stays fast regardless of that
    # slider; a report reader also has less use for 2000 printed rows
    # than a CSV importer does.
    PDF_ROW_CAP = 200
    pdf_df = df.head(PDF_ROW_CAP)

    report_parameters = {
        "Severity": severity,
        "Equipment": equipment,
        "Tag": tag,
        "Time range": time_range_label,
        "Matching events": f"{total_matching} total, {len(events)} shown on screen"
        + (f", first {PDF_ROW_CAP} in this PDF" if len(pdf_df) < len(df) else ""),
    }
    st.download_button(
        "⬇️ Download PDF",
        data=build_pdf_report("Event Records Report", report_parameters, pdf_df, highlight_column="Severity"),
        file_name=export_pdf_filename("event_records"),
        mime="application/pdf",
        help=f"Printable report with the active filters shown at the top. Capped at {PDF_ROW_CAP} rows for "
        "readability/generation time - use CSV for the full export.",
    )

st.dataframe(df.style.apply(_highlight_severity, axis=1), width="stretch", height=700)
