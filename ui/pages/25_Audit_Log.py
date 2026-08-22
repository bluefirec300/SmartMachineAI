from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.configuration_manager import ConfigurationManager
from ui import auth
from ui.csv_export import dataframe_to_csv_bytes, export_filename
from ui.data_access import CONFIG_DATABASE_PATH

auth.require_role("admin")

st.title("📜 Audit Log")
st.caption(
    "Search and export every tracked configuration change (setpoints, tag "
    "addresses, user accounts, PLC connections, notification settings, and "
    "more) across this environment. Each individual admin page still shows "
    "its own recent-changes list - this page searches the full history at "
    "once. Admin-only."
)


@st.cache_resource
def _get_config_manager() -> ConfigurationManager:
    return ConfigurationManager(database_path=CONFIG_DATABASE_PATH)


config_manager = _get_config_manager()
filter_options = config_manager.get_audit_log_filter_options()

st.divider()
st.subheader("Filters")

filter_row1 = st.columns(3)

with filter_row1[0]:
    selected_entity_type = st.selectbox(
        "Entity type",
        ["All"] + filter_options["entity_types"],
    )

with filter_row1[1]:
    selected_username = st.selectbox(
        "User",
        ["All"] + filter_options["usernames"],
    )

with filter_row1[2]:
    search_text = st.text_input(
        "Search",
        placeholder="Matches action, entity name, or details",
    )

filter_row2 = st.columns(3)

with filter_row2[0]:
    start_date = st.date_input("From date", value=None)

with filter_row2[1]:
    end_date = st.date_input("To date", value=None)

with filter_row2[2]:
    result_limit = st.number_input(
        "Max rows", min_value=20, max_value=5000, value=200, step=20
    )

rows = config_manager.get_audit_log(
    limit=int(result_limit),
    entity_type=None if selected_entity_type == "All" else selected_entity_type,
    username=None if selected_username == "All" else selected_username,
    search=search_text.strip() or None,
    start_date=start_date.isoformat() if start_date else None,
    end_date=end_date.isoformat() if end_date else None,
)

st.divider()

if not rows:
    st.caption("No audit log entries match these filters.")
else:
    st.caption(
        f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'} shown "
        f"(most recent first, capped at {int(result_limit)})."
    )

    df = pd.DataFrame(
        [
            {
                "Timestamp (UTC)": row["timestamp"],
                "User": row["username"],
                "Action": row["action"],
                "Entity type": row["entity_type"] or "-",
                "Entity name": row["entity_name"] or "-",
                "Old value": row["old_value"] or "-",
                "New value": row["new_value"] or "-",
                "Details": row["details"] or "-",
            }
            for row in rows
        ]
    )

    st.dataframe(df, hide_index=True, width="stretch")

    st.download_button(
        "⬇️ Download CSV",
        data=dataframe_to_csv_bytes(df),
        file_name=export_filename("audit_log"),
        mime="text/csv",
        help="Exports exactly the rows shown above, with the current filters applied.",
    )
