from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.configuration_manager import ConfigurationManager
from ui.data_access import CONFIG_DATABASE_PATH, get_enabled_tags


st.set_page_config(page_title="Setpoints - SmartMachineAI", page_icon="⚙️", layout="wide")
st.title("⚙️ Setpoints")
st.caption(
    "View and edit the low/high warning and alarm limits used to evaluate "
    "each tag. Changes take effect immediately and are recorded in the "
    "audit log."
)

PARAMETERS = ["low_alarm", "low_warning", "high_warning", "high_alarm"]
PARAMETER_LABELS = {
    "low_alarm": "Low Alarm",
    "low_warning": "Low Warning",
    "high_warning": "High Warning",
    "high_alarm": "High Alarm",
}


@st.cache_resource
def _get_config_manager() -> ConfigurationManager:
    return ConfigurationManager(database_path=CONFIG_DATABASE_PATH)


config_manager = _get_config_manager()

all_tags = [tag for tag in get_enabled_tags() if tag["data_type"] != "STRING"]

if not all_tags:
    st.info("No numeric tags are currently enabled.")
    st.stop()

equipment_names = sorted({tag["equipment_display_name"] for tag in all_tags})
selected_equipment = st.selectbox("Equipment", equipment_names)

group_tags = [tag for tag in all_tags if tag["equipment_display_name"] == selected_equipment]
thresholds_by_tag = {row["tag_name"]: row for row in config_manager.get_thresholds()}

st.divider()

for tag in group_tags:
    existing = thresholds_by_tag.get(tag["tag_name"], {})
    unit_suffix = f" ({tag['unit']})" if tag["unit"] else ""
    st.subheader(f"{tag['tag_name']}{unit_suffix}")
    if tag["description"]:
        st.caption(tag["description"])

    columns = st.columns(4)
    for column, parameter in zip(columns, PARAMETERS):
        with column:
            st.number_input(
                PARAMETER_LABELS[parameter],
                value=existing.get(parameter),
                step=0.1,
                format="%.2f",
                key=f"thr_{tag['tag_name']}_{parameter}",
            )

st.divider()

if "setpoints_pending_changes" not in st.session_state:
    st.session_state["setpoints_pending_changes"] = None

if st.button("Review Changes", type="primary"):
    changes = []
    for tag in group_tags:
        existing = thresholds_by_tag.get(tag["tag_name"], {})
        for parameter in PARAMETERS:
            new_value = st.session_state.get(f"thr_{tag['tag_name']}_{parameter}")
            old_value = existing.get(parameter)
            if new_value != old_value:
                changes.append(
                    {
                        "tag_name": tag["tag_name"],
                        "parameter": parameter,
                        "old_value": old_value,
                        "new_value": new_value,
                    }
                )
    st.session_state["setpoints_pending_changes"] = changes

pending_changes = st.session_state["setpoints_pending_changes"]

if pending_changes is not None:
    if not pending_changes:
        st.info("No changes to save.")
    else:
        st.warning(f"You are about to apply {len(pending_changes)} change(s):")
        for change in pending_changes:
            st.write(
                f"- **{change['tag_name']}** / {PARAMETER_LABELS[change['parameter']]}: "
                f"{change['old_value']} → {change['new_value']}"
            )

        confirm_col, cancel_col = st.columns(2)

        if confirm_col.button("Confirm & Apply", type="primary"):
            for change in pending_changes:
                config_manager.set_threshold(
                    tag_name=change["tag_name"],
                    parameter=change["parameter"],
                    value=change["new_value"],
                    username="ui_user",
                )
            st.session_state["setpoints_pending_changes"] = None
            st.success("Changes applied.")
            st.rerun()

        if cancel_col.button("Cancel"):
            st.session_state["setpoints_pending_changes"] = None
            st.rerun()

with st.expander("Recent changes (audit log)"):
    audit_rows = config_manager.get_audit_log(limit=20)
    threshold_rows = [row for row in audit_rows if row["entity_type"] == "threshold"]
    if not threshold_rows:
        st.caption("No threshold changes recorded yet.")
    else:
        for row in threshold_rows:
            tag_name, _, parameter = row["entity_name"].rpartition(".")
            try:
                old_snapshot = json.loads(row["old_value"]) if row["old_value"] else {}
                new_snapshot = json.loads(row["new_value"]) if row["new_value"] else {}
                old_display = old_snapshot.get(parameter)
                new_display = new_snapshot.get(parameter)
            except (TypeError, ValueError):
                old_display = row["old_value"]
                new_display = row["new_value"]
            st.write(
                f"- {row['timestamp']} — **{row['entity_name']}** "
                f"{old_display} → {new_display} (by {row['username']})"
            )
