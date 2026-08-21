from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.data_access import get_anomalies, get_anomaly_filter_options, get_plants
from ui.evidence_display import render_readable


st.title("Anomalies / Engineering Findings")
st.caption(
    "Statistically-abnormal behaviour detected against each tag's own learned "
    "baseline (Phase 8/9) - a separate concept from the PLC engineering alarm "
    "system (Live Data / Event Records), which is untouched by this page. "
    "A finding here means 'unusual compared to learned history', not "
    "'exceeded a hard limit', root cause, or a recommended action."
)

plants = get_plants()
filter_options = get_anomaly_filter_options()

filter_cols = st.columns(4)

with filter_cols[0]:
    status_label = st.selectbox("Status", ["Open", "Resolved", "All"], index=0)
    status = {"Open": "OPEN", "Resolved": "RESOLVED", "All": None}[status_label]

with filter_cols[1]:
    plant_options = {p["code"].upper(): p["code"] for p in plants}
    plant_label = st.selectbox("Plant", ["All"] + list(plant_options), key="anomalies_plant")
    plant_code = None if plant_label == "All" else plant_options[plant_label]

with filter_cols[2]:
    category = st.selectbox("Category", ["All"] + filter_options["categories"])
    category = None if category == "All" else category

with filter_cols[3]:
    severity = st.selectbox("Severity", ["All"] + filter_options["severities"])
    severity = None if severity == "All" else severity

limit = st.slider("Max rows to show", min_value=50, max_value=1000, value=300, step=50)

anomalies = get_anomalies(status=status, plant_code=plant_code, category=category, severity=severity, limit=limit)

if not anomalies:
    st.info("No matching anomalies/engineering findings.")
    st.stop()

st.caption(f"{len(anomalies)} matching row(s).")

severity_colors = {
    "HIGH": "background-color: #ffb3b3; color: #1a1a1a",
    "WARNING": "background-color: #ffe6a3; color: #1a1a1a",
    "ATTENTION": "background-color: #fff3cc; color: #1a1a1a",
    "INFORMATION": "background-color: #d6e9ff; color: #1a1a1a",
}


def _fmt(value, decimals: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


rows = []
for a in anomalies:
    rows.append(
        {
            "Plant": a["plant_code"].upper(),
            "Equipment": a["instance_key"],
            "Target": a["target_key"],
            "Finding": a["title"],
            "Severity": a["severity"],
            "Confidence": a["baseline_confidence"] or "-",
            "Provisional": "Yes" if a["provisional"] else "",
            "Status": a["status"],
            "Actual": _fmt(a["actual_value"]),
            "Expected": _fmt(a["expected_value"]),
            "Deviation %": _fmt(a["deviation_percent"], 1),
            "Engineering Limit": a["engineering_limit_status"] or "-",
            "Est. Excess Energy (kWh)": _fmt(a["estimated_excess_energy_kwh"]) if a["estimated_excess_energy_kwh"] is not None else "Unavailable",
            "Est. Excess Cost": _fmt(a["estimated_excess_cost"]) if a["estimated_excess_cost"] is not None else "Unavailable",
            "Occurrences": a["occurrence_count"],
            "First Detected": a["first_detected"],
            "Last Seen": a["last_seen"],
            "Resolved At": a["resolved_at"] or "-",
        }
    )

df = pd.DataFrame(rows)


def _highlight_severity(row: pd.Series) -> list[str]:
    color = severity_colors.get(row["Severity"], "")
    return [color] * len(row)


st.dataframe(
    df.style.apply(_highlight_severity, axis=1),
    width="stretch",
    height=500,
    column_config={"Finding": st.column_config.TextColumn("Finding", width="large")},
)

st.divider()
st.subheader("Finding detail")

label_by_id = {
    a["id"]: f"[{a['status']}] {a['plant_code'].upper()} · {a['instance_key']} · {a['title']} (last seen {a['last_seen']})"
    for a in anomalies
}
selected_id = st.selectbox(
    "Select a row above to inspect its full evidence, assumptions, and data limitations",
    list(label_by_id),
    format_func=lambda i: label_by_id[i],
)
selected = next(a for a in anomalies if a["id"] == selected_id)

detail_cols = st.columns(3)
with detail_cols[0]:
    st.metric("Severity", selected["severity"])
    st.metric("Baseline confidence", selected["baseline_confidence"] or "-")
with detail_cols[1]:
    st.metric("Status", selected["status"])
    st.metric("Occurrences (this condition)", selected["occurrence_count"])
with detail_cols[2]:
    # Markdown, not st.metric() - these two values can be long enum
    # strings (e.g. "warning_exceeded", "MANUFACTURER_REFERENCE") that
    # read awkwardly at st.metric()'s large KPI-number font size.
    st.markdown(f"**Engineering limit status**  \n{selected['engineering_limit_status'] or '-'}")
    st.markdown(f"**Threshold provenance**  \n{selected['threshold_provenance']}")

st.caption(
    f"Baseline type: {selected['baseline_type'] or '-'} · level: {selected['baseline_level'] or '-'} · "
    f"normalized deviation: {_fmt(selected['normalized_deviation'])} · "
    f"expected range: {_fmt(selected['expected_low'])} - {_fmt(selected['expected_high'])}"
)

for label, key in (
    ("Evidence", "evidence_json"),
    ("Assumptions", "assumptions_json"),
    ("Data limitations", "data_limitations_json"),
    ("Source tags", "source_tags"),
):
    raw = selected.get(key)
    with st.expander(label, expanded=(label == "Evidence")):
        if not raw:
            st.write("None recorded.")
            continue
        try:
            render_readable(json.loads(raw))
        except (TypeError, ValueError):
            st.write(raw)

st.caption(
    "Financial figures are ESTIMATED excess energy/cost only (interval-integrated "
    "against actual tariff history), attached only to energy-relevant findings - "
    "never a claimed 'saving', root cause, or maintenance recommendation."
)
