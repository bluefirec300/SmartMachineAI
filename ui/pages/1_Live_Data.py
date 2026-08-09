from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.rule_engine import RuleEngine
from database.database import DatabaseManager
from ui.data_access import CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH, get_enabled_tags


st.set_page_config(page_title="Live Data - SmartMachineAI", page_icon="📊", layout="wide")
st.title("📊 Live Data")

auto_refresh = st.checkbox("Auto-refresh every 5 seconds", value=True)

database = DatabaseManager(db_path=MACHINE_DATABASE_PATH)
rule_engine = RuleEngine(database_path=CONFIG_DATABASE_PATH)

tags = get_enabled_tags()
latest = database.get_latest_all()

rows = []

for tag in tags:
    reading = latest.get(tag["tag_name"])

    if tag["data_type"] == "STRING":
        # Text tags (e.g. AlarmCode) are deliberately not written to
        # plc_data (a REAL-only column) - see app/plc_logger.py.
        status = "TEXT TAG"
        value_display = "(not tracked here)"
        updated = "-"
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

df = pd.DataFrame(rows)

status_colors = {
    "ALARM": "background-color: #ffb3b3",
    "WARNING": "background-color: #ffe6a3",
    "NO DATA": "background-color: #e0e0e0",
    "TEXT TAG": "background-color: #eeeeee",
}


def _highlight_status(row: pd.Series) -> list[str]:
    color = status_colors.get(row["Status"], "")
    return [color] * len(row)


st.caption(f"{len(rows)} tags enabled")
st.dataframe(
    df.style.apply(_highlight_status, axis=1),
    use_container_width=True,
    height=700,
)

if auto_refresh:
    time.sleep(5)
    st.rerun()
