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
from ui.data_access import (
    CONFIG_DATABASE_PATH,
    MACHINE_DATABASE_PATH,
    get_enabled_tags,
    get_simulator_command_log,
    get_simulator_instances,
    send_simulator_command,
)


st.set_page_config(page_title="Simulator Control - SmartMachineAI", page_icon="🧪", layout="wide")
st.title("🧪 Simulator Control")
st.warning(
    "**Temporary developer tool** - for testing the AI's root-cause diagnosis against a "
    "known, on-demand fault instead of waiting for the simulator's normal random timing "
    "(roughly once every 30-60 minutes per equipment instance). Not part of the "
    "operator-facing app; safe to remove later without affecting anything else."
)

st.markdown(
    """
Triggering a fault here uses the exact same fault-lifecycle engine the simulator already
runs randomly (`simulator/tag_dataset_model.py`) - it doesn't fabricate a scripted scenario.
That engine already produces a realistic, *correlated* signature per equipment type, because
each tag is pushed in a direction that matches what it physically measures:

- **pressure / flow / level** readings are pushed **down**
- everything else (**temperature, current, power, vibration, humidity, ...**) is pushed **up**

So triggering a compressor naturally produces "pressure drops while power/temperature rise"
- a pump naturally produces "flow drops while current/vibration/bearing temp rise" - a chiller
naturally produces "supply/return temp rises while flow drops" - each equipment type gets its
own believable signature just from which tags it actually has, not a hand-scripted story.

The fault develops gradually (~1-2 minutes to ramp up, matching the real random-fault timing),
holds at full severity for a while, then recovers - watch it happen in the live status below,
or come back to it after asking the AI a root-cause question.
"""
)

st.caption(
    "Requires `plc_logger` to be running and using the simulator driver - it polls for "
    "commands once per cycle. If nothing happens after ~10 seconds, check that it's up."
)

instances = get_simulator_instances()

if not instances:
    st.info("No simulator-driven equipment instances found among the currently enabled tags.")
    st.stop()

instance_options = {row["equipment_display_name"]: row["instance_key"] for row in instances}
selected_name = st.selectbox("Equipment instance", sorted(instance_options))
selected_instance_key = instance_options[selected_name]

trigger_col, recover_col = st.columns(2)

with trigger_col:
    if st.button("🔥 Trigger Fault", type="primary"):
        send_simulator_command(selected_instance_key, "trigger_fault")
        st.success(f"Fault command sent for {selected_name}. Watch the live status below.")

with recover_col:
    if st.button("✅ Force Recover"):
        send_simulator_command(selected_instance_key, "force_recover")
        st.success(f"Recover command sent for {selected_name}.")

st.divider()
st.subheader(f"Live Status - {selected_name}")

auto_refresh = st.checkbox("Auto-refresh every 5 seconds", value=True, key="sim_control_refresh")

database = DatabaseManager(db_path=MACHINE_DATABASE_PATH)
rule_engine = RuleEngine(database_path=CONFIG_DATABASE_PATH)
latest = database.get_latest_all()

instance_tags = [
    tag
    for tag in get_enabled_tags()
    if tag["equipment_display_name"] == selected_name and tag["data_type"] != "STRING"
]

status_rows = []
for tag in instance_tags:
    reading = latest.get(tag["tag_name"])
    if reading is None:
        status_rows.append({"Tag": tag["tag_name"], "Value": "-", "Status": "NO DATA", "Updated": "never logged"})
        continue

    result = rule_engine.evaluate_summary({"tag": tag["tag_name"], "current": reading["value"]})
    status_rows.append(
        {
            "Tag": tag["tag_name"],
            "Value": f"{reading['value']:.2f} {tag['unit']}".strip(),
            "Status": result["severity"].upper(),
            "Updated": reading["time"],
        }
    )

status_df = pd.DataFrame(status_rows)

status_colors = {
    "ALARM": "background-color: #ffb3b3; color: #1a1a1a",
    "WARNING": "background-color: #ffe6a3; color: #1a1a1a",
    "NO DATA": "background-color: #e0e0e0; color: #1a1a1a",
}


def _highlight(row: pd.Series) -> list[str]:
    return [status_colors.get(row["Status"], "")] * len(row)


st.dataframe(status_df.style.apply(_highlight, axis=1), hide_index=True, width="stretch")

st.divider()
st.subheader("Recent Commands")

command_rows = get_simulator_command_log(limit=15)
if not command_rows:
    st.caption("No commands sent yet.")
else:
    command_df = pd.DataFrame(
        [
            {
                "Sent": row["created_at"],
                "Instance": row["instance_key"],
                "Action": row["action"],
                "Result": row["result"] or "pending...",
            }
            for row in command_rows
        ]
    )
    st.dataframe(command_df, hide_index=True, width="stretch")

if auto_refresh:
    time.sleep(5)
    st.rerun()
