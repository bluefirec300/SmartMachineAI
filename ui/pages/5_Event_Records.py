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
from ui.data_access import MACHINE_DATABASE_PATH, get_event_filter_options


st.set_page_config(page_title="Event Records - SmartMachineAI", page_icon="📋", layout="wide")
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

df = pd.DataFrame(rows)

severity_colors = {
    "ALARM": "background-color: #ffb3b3",
    "WARNING": "background-color: #ffe6a3",
}


def _highlight_severity(row: pd.Series) -> list[str]:
    color = severity_colors.get(row["Severity"], "")
    return [color] * len(row)


st.dataframe(df.style.apply(_highlight_severity, axis=1), width="stretch", height=700)
