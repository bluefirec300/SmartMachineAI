from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.rule_engine import RuleEngine
from database.database import DatabaseManager
from ui.data_access import CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH, get_enabled_tags


st.title("📊 Live Data")

auto_refresh = st.checkbox("Auto-refresh every 5 seconds", value=True)


def _highlight_status(row: pd.Series, status_colors: dict[str, str]) -> list[str]:
    color = status_colors.get(row["Status"], "")
    return [color] * len(row)


# Everything data-dependent lives in this fragment so the periodic
# refresh (and the equipment/status filter widgets) only re-render this
# table, not the whole page. The previous version used a plain
# time.sleep()+st.rerun() loop at the bottom of the script, which reruns
# the *entire* page every cycle - visibly reloading it (the same "whole
# screen flashes every few seconds" issue found and fixed on the SCADA
# Floor Plan prototype; same root cause, same fix, applied here too).
@st.fragment(run_every=5 if auto_refresh else None)
def _live_table() -> None:
    database = DatabaseManager(db_path=MACHINE_DATABASE_PATH)
    rule_engine = RuleEngine(database_path=CONFIG_DATABASE_PATH)

    tags = get_enabled_tags()
    latest = database.get_latest_all()
    latest_text = database.get_latest_text_all()

    rows = []

    for tag in tags:
        reading = latest.get(tag["tag_name"])

        if tag["data_type"] == "STRING":
            # Text tags can't go in plc_data (a REAL-only column) - see
            # app/plc_logger.py - so they're read from the separate
            # current-value-only text table instead. Only *.AlarmCode is
            # actually fault-related (see simulator/tag_dataset_model.py)
            # - other text tags (BatchNumber, ProductCode, MaterialID,
            # ...) are informational, so they get a plain "NO STATUS"
            # rather than misleadingly showing up styled like an alarm.
            text_reading = latest_text.get(tag["tag_name"])
            is_alarm_code = tag["tag_name"].endswith(".AlarmCode")

            if text_reading is None:
                status = "NO DATA"
                value_display = "-"
                updated = "never logged"
            elif is_alarm_code and text_reading["value"] == "None":
                status = "TEXT TAG"
                value_display = "(no active fault)"
                updated = text_reading["time"]
            elif is_alarm_code:
                status = "ALARM"
                value_display = text_reading["value"]
                updated = text_reading["time"]
            else:
                status = "NO STATUS"
                value_display = text_reading["value"] or "-"
                updated = text_reading["time"]
        elif reading is None:
            status = "NO DATA"
            value_display = "-"
            updated = "never logged"
        else:
            rule_result = rule_engine.evaluate_summary(
                {"tag": tag["tag_name"], "current": reading["value"]}
            )
            status = rule_result["severity"].upper()
            value_display = str(reading["value"])
            updated = reading["time"]

        rows.append(
            {
                "Equipment": tag["equipment_display_name"],
                "Tag": tag["tag_name"],
                "Description": tag["description"],
                "Value": value_display,
                "Unit": tag["unit"],
                "Status": status,
                "Updated": updated,
            }
        )

    equipment_options = sorted({row["Equipment"] for row in rows})
    status_options = sorted({row["Status"] for row in rows})

    filter_cols = st.columns(2)

    with filter_cols[0]:
        equipment_filter = st.selectbox("Equipment", ["All"] + equipment_options)

    with filter_cols[1]:
        status_filter = st.selectbox("Status", ["All"] + status_options)

    filtered_rows = [
        row
        for row in rows
        if (equipment_filter == "All" or row["Equipment"] == equipment_filter)
        and (status_filter == "All" or row["Status"] == status_filter)
    ]

    df = pd.DataFrame(filtered_rows)

    # Explicit text color alongside background - Streamlit's dark theme
    # defaults to white table text, which is unreadable against these
    # light backgrounds if only background-color is set.
    status_colors = {
        "ALARM": "background-color: #ffb3b3; color: #1a1a1a",
        "WARNING": "background-color: #ffe6a3; color: #1a1a1a",
        "NO DATA": "background-color: #e0e0e0; color: #1a1a1a",
        "TEXT TAG": "background-color: #eeeeee; color: #1a1a1a",
        "NO STATUS": "background-color: #eeeeee; color: #1a1a1a",
    }

    st.caption(f"{len(filtered_rows)} of {len(rows)} tags shown")

    if not filtered_rows:
        st.info("No tags match the selected filters.")
    else:
        st.dataframe(
            df.style.apply(_highlight_status, axis=1, status_colors=status_colors),
            use_container_width=True,
            height=700,
        )


_live_table()
