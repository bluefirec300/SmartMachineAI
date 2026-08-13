from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_active_environment
from ui import auth


st.set_page_config(
    page_title="SmartMachineAI",
    page_icon="🏭",
    layout="wide",
)

# Blocks (renders a login form + st.stop()) until authenticated. Every
# page below only ever runs after this succeeds, so none of them need
# their own login check - they can assume auth.current_user() is set.
user = auth.require_login()


def _overview_page() -> None:
    st.title("🏭 SmartMachineAI")

    st.markdown(
        """
Use the pages in the sidebar:

- **Ask AI** - ask questions in plain English ("why is the compressor
  pressure dropping") without needing the terminal.
- **Live Data** - current value of every enabled tag, grouped by
  equipment, with engineering-limit status.
- **Event Records** - browse the full alarm/warning history, filterable
  by severity, equipment, tag, and time range.
"""
    )

    if get_active_environment() == "simulation":
        st.markdown(
            """
- **Simulator (Testing Only)** - manually trigger a realistic fault on
  any simulated equipment instance, for testing the AI's root-cause
  diagnosis on demand.
"""
        )

    st.markdown(
        """
- **Engineer** section - Setpoints (low/high warning and alarm limits),
  Service & Maintenance, and Documentation (equipment manuals/photos).
  Visible to everyone, but only Engineer/Administrator accounts can
  change anything on these pages.
"""
    )

    if auth.has_role("admin"):
        st.markdown(
            "- **Admin** section - User Management, PLC Connectivity, and "
            "Equipment & Tag Configuration, "
            "visible to Administrator accounts only."
        )

    st.markdown(
        """
This shows whatever is currently **enabled** in `database/config.db`
- as more of the P01/P02/Phase 2/3 tag dataset gets enabled
(`engine/tag_dataset_importer.py`), it appears here automatically,
with no changes needed to this app.
"""
    )


general_pages = [
    st.Page(_overview_page, title="Home", icon="🏭", default=True),
    st.Page("pages/1_Ask_AI.py", title="Ask AI", icon="💬"),
    st.Page("pages/12_SCADA_Floor_Plan.py", title="SCADA Floor Plan", icon="🗺️"),
    st.Page("pages/2_Live_Data.py", title="Live Data", icon="📊"),
    st.Page("pages/5_Event_Records.py", title="Event Records", icon="📋"),
]

# Only meaningful when the simulator is actually driving data - hidden
# entirely in the Actual environment rather than shown-but-useless.
if get_active_environment() == "simulation":
    general_pages.append(
        st.Page("pages/6_Simulator_(testing_only).py", title="Simulator (Testing Only)", icon="🧪")
    )

# Visible to every role (operators can view/download, only
# Engineer/Administrator accounts see enabled write controls - each
# page enforces that itself via ui.auth.can_edit()).
engineer_pages = [
    st.Page("pages/3_Setpoints.py", title="Setpoints", icon="⚙️"),
    st.Page("pages/4_Service_and_Maintenance.py", title="Service & Maintenance", icon="🛠️"),
    st.Page("pages/8_Documentation.py", title="Documentation", icon="📚"),
]

pages = {
    "": general_pages,
    "Engineer": engineer_pages,
}

if auth.has_role("admin"):
    pages["Admin"] = [
        st.Page("pages/9_User_Management.py", title="User Management", icon="👤"),
        st.Page("pages/10_PLC_Connectivity.py", title="PLC Connectivity", icon="🔌"),
        st.Page(
            "pages/11_Equipment_and_Tag_Configuration.py",
            title="Equipment & Tag Configuration",
            icon="🧩",
        ),
    ]

auth.render_sidebar_identity()

navigation = st.navigation(pages)
navigation.run()
