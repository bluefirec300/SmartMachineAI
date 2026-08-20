from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_active_environment
from ui import auth
from ui import data_health_fleet_data as dhfd
from ui import health_data as hd


st.set_page_config(
    page_title="SmartFactoryAI",
    page_icon="🏭",
    layout="wide",
)

# Blocks (renders a login form + st.stop()) until authenticated. Every
# page below only ever runs after this succeeds, so none of them need
# their own login check - they can assume auth.current_user() is set.
user = auth.require_login()


def _overview_page() -> None:
    st.title("🏭 SmartFactoryAI")

    st.markdown(
        """
Use the pages in the sidebar:

- **Ask AI** - ask questions in plain English ("why is the compressor
  pressure dropping") without needing the terminal.
- **Live Data** - current value of every enabled tag, grouped by
  equipment, with engineering-limit status.
- **Energy Dashboard** - demand, energy, cost, and utility-performance
  KPIs per plant, plus a P01 vs P02 comparison - all computed
  deterministically, never by the LLM.
- **Event Records** - browse the full alarm/warning history, filterable
  by severity, equipment, tag, and time range.
- **Anomalies** - statistically-abnormal behaviour vs. each tag's learned
  baseline (a separate concept from engineering alarms), filterable by
  status/plant/category/severity.
- **Energy Opportunities** - defensible Engineering Investigation
  Priorities derived from Phase 9 anomaly findings, with Potential
  Saving shown only when a specific calculation basis exists.
- **Savings Verification** - record an engineering intervention against
  an opportunity, then track its evidence-driven verification outcome
  (Verified / Not Verified / More Evidence Required) - never manually
  forced, always backend-controlled.
- **Equipment Health** - deterministic 0-100 engineering prioritization
  score per equipment, with a factor-by-factor breakdown, historical
  trend, and evidence-grounded change explanations - a separate
  Assessment Confidence always shown alongside, never fabricated.
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

    # Phase 12.4 (item 8) - a lightweight factory-wide count of the
    # CURRENT persisted Equipment Health state, never a computed
    # factory/plant "health score" - no averaging across equipment into
    # a single number, no plant-vs-plant ranking (item 16). The
    # Equipment Health page remains the authoritative detailed view.
    summary = hd.get_health_summary_counts()
    if summary["total"] > 0:
        st.divider()
        st.markdown("**Equipment Health** (see the Equipment Health page for full detail)")
        health_cols = st.columns(6)
        health_cols[0].metric("Monitored", summary["total"])
        health_cols[1].metric("Healthy", summary["healthy"])
        health_cols[2].metric("Monitor", summary["monitor"])
        health_cols[3].metric("Attention", summary["attention"])
        health_cols[4].metric("Investigate", summary["investigate"])
        health_cols[5].metric("Insufficient Data", summary["insufficient"])

    # Phase 16.4 (item 22) - a SMALL read-only summary of the same
    # cached fleet Data Health result the Data Health page itself uses
    # (ui.data_health_fleet_data.get_fleet_data_health() is
    # st.cache_data-wrapped - calling it here never triggers a second,
    # independent fleet evaluation). Deliberately distribution/count-based,
    # never averaged into one "Factory Data Confidence" number - Data
    # Health describes telemetry trustworthiness, never machine condition,
    # so it is never blended with the Equipment Health summary above.
    dh_summary = dhfd.get_fleet_data_health()
    if dh_summary["equipment_count"] > 0:
        st.divider()
        st.markdown("**Data Health** (telemetry trustworthiness - see the Data Health page for full detail)")
        dh_cols = st.columns(4)
        dh_cols[0].metric("GOOD", dh_summary["good_count"])
        dh_cols[1].metric("DEGRADED", dh_summary["degraded_count"])
        dh_cols[2].metric("POOR", dh_summary["poor_count"])
        dh_cols[3].metric("UNAVAILABLE", dh_summary["unavailable_count"])


general_pages = [
    st.Page(_overview_page, title="Home", icon="🏭", default=True),
    st.Page("pages/1_Ask_AI.py", title="Ask AI", icon="💬"),
    st.Page("pages/12_SCADA_Floor_Plan.py", title="SCADA Floor Plan", icon="🗺️"),
    # Post-Phase-14 cleanup - Live Data hidden from the sidebar (SCADA
    # Floor Plan is now the main live-data interface) per explicit
    # direction. The page file, its data access, and all supporting
    # code are DELIBERATELY UNTOUCHED - only removed from this
    # navigation registration, so it may be reinstated later by adding
    # this one line back. Confirmed nothing else in the app links to it
    # via st.switch_page() (grepped repo-wide), so removing it here
    # introduces no dangling internal navigation reference.
    # st.Page("pages/2_Live_Data.py", title="Live Data", icon="📊"),
    st.Page("pages/15_Energy_Dashboard.py", title="Energy Dashboard", icon="⚡"),
    st.Page("pages/5_Event_Records.py", title="Event Records", icon="📋"),
    st.Page("pages/16_Anomalies.py", title="Anomalies", icon="🔎"),
    st.Page("pages/17_Energy_Opportunities.py", title="Energy Opportunities", icon="💡"),
    st.Page("pages/18_Savings_Verification.py", title="Savings Verification", icon="✅"),
    st.Page("pages/19_Equipment_Health.py", title="Equipment Health", icon="🩺"),
    st.Page("pages/20_Asset_Performance.py", title="Asset Performance", icon="📈"),
    st.Page("pages/21_Data_Health.py", title="Data Health", icon="📡"),
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
    st.Page("pages/13_Factory_Configuration.py", title="Factory Configuration", icon="🏭"),
    st.Page("pages/14_Production_Context.py", title="Production Context", icon="🧴"),
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
