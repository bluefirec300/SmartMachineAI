from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui import auth, table_style as ts
from ui.setup_readiness_data import (
    SIMULATION_TUNING_REGISTRIES,
    get_all_readiness_status,
)

"""
Phase V2.7 - turns docs/NEW_FACTORY_SETUP_CHECKLIST.md's static
checklist into an in-app readiness overview. Read-only: every number
here comes from ui/setup_readiness_data.py reading already-existing
database/config state - this page adds no new confirmation mechanism
and cannot edit anything itself. Each section links to the existing
admin page where the actual work happens, matching the checklist's
own "Page:" references exactly - never a competing/parallel workflow.
"""

auth.require_role("admin")

st.title("🏭 New-Factory Setup Readiness")
st.caption(
    "An in-app view of docs/NEW_FACTORY_SETUP_CHECKLIST.md - what still needs site-specific confirmation "
    "before treating this deployment's numbers as production-trustworthy. Every item here is read-only: "
    "make the actual change on the linked page, then come back here to see it reflected. Nothing here "
    "invents a real site value - a 0% or \"unconfirmed\" status is honest, not a defect."
)

status = get_all_readiness_status()


def _badge(label: str, tier: str) -> str:
    return ts.badge_html(label, tier)


st.divider()

# --- 1. Area/System taxonomy ---
st.subheader("1. Factory Area / System taxonomy")
area_system = status["area_system"]
tier = ts.POSITIVE if area_system["percent_confirmed"] == 100 else (ts.CAUTION if area_system["percent_confirmed"] > 0 else ts.NEUTRAL)
st.markdown(_badge(f"{area_system['percent_confirmed']}% confirmed", tier), unsafe_allow_html=True)
st.caption(
    f"Areas: {area_system['areas']['confirmed']}/{area_system['areas']['total']} confirmed · "
    f"Systems: {area_system['systems']['confirmed']}/{area_system['systems']['total']} confirmed. "
    "Go to **Factory Configuration → Area/System tab** to review and confirm each one against the real site."
)

st.divider()

# --- 2. Energy tariff ---
st.subheader("2. Energy tariff")
tariff = status["tariff"]
if tariff["total"] == 0:
    st.markdown(_badge("No active tariff", ts.NEUTRAL), unsafe_allow_html=True)
    st.caption("No tariff scope found at all - this should not normally happen; check Factory Configuration.")
else:
    tier = ts.POSITIVE if tariff["real_count"] == tariff["total"] else ts.WARNING
    st.markdown(_badge(f"{tariff['real_count']}/{tariff['total']} scope(s) using a real tariff", tier), unsafe_allow_html=True)
    for scope in tariff["scopes"]:
        marker = "✅ Real" if not scope["is_simulated"] else "⚠️ Simulated"
        rate_text = f"{scope['currency']} {scope['energy_rate']}/kWh" if scope["energy_rate"] is not None else "no flat rate set"
        st.caption(f"- {scope['scope']}: {marker} ({rate_text})")
    st.caption("Go to **Factory Configuration → Energy Tariff tab** to enter the real rate for each scope.")

st.divider()

# --- 3. Equipment engineering thresholds (SIMULATION_TUNING) ---
st.subheader("3. Equipment engineering thresholds and scoring weights")
st.markdown(_badge("Manual engineering review required", ts.NEUTRAL), unsafe_allow_html=True)
st.caption(
    "Not tracked as a percentage here on purpose - these live in Python registry files, not the database, "
    "and this checklist deliberately does not propose real values (that needs real site engineering "
    "knowledge). Review each file below and update its `threshold_provenance`/weight only when you have a "
    "real basis (manufacturer spec, site history) to replace the `SIMULATION_TUNING` default."
)
for registry in SIMULATION_TUNING_REGISTRIES:
    st.caption(f"- **{registry['domain']}** (`{registry['file']}`) - {registry['defines']}")

st.divider()

# --- 4. Equipment manufacturer documentation ---
st.subheader("4. Equipment manufacturer documentation")
docs = status["documentation"]
tier = ts.POSITIVE if docs["synthetic_count"] == 0 else ts.CAUTION
st.markdown(_badge(f"{docs['real_count']}/{docs['total_reference_manuals']} manuals are real/sourced", tier), unsafe_allow_html=True)
st.caption(
    f"{docs['synthetic_count']} synthetic placeholder(s) remain (already visibly marked "
    "⚠️ Synthetic placeholder on the Documentation page itself). Go to **Documentation** to "
    "replace one via the Upload panel if a real manual becomes available."
)

st.divider()

# --- 5. Real PLC connection ---
st.subheader("5. Real PLC connection")
plc = status["plc_connection"]
if not plc["available"]:
    st.markdown(_badge("Actual environment not set up yet", ts.NEUTRAL), unsafe_allow_html=True)
    st.caption("No `database/actual/` config database found.")
elif plc["active_connection"] is None:
    st.markdown(_badge("No connection active in the Actual environment", ts.NEUTRAL), unsafe_allow_html=True)
    st.caption("Go to **PLC Connectivity (Admin)** to configure and activate a real connection.")
else:
    conn = plc["active_connection"]
    tier = ts.CAUTION if conn["looks_local"] else ts.POSITIVE
    st.markdown(_badge(f"Active: {conn['name']} ({conn['protocol']})", tier), unsafe_allow_html=True)
    st.caption(f"Address: `{conn['address']}`")
    if conn["looks_local"]:
        st.caption(
            "⚠️ This address (0.0.0.0/127.0.0.1/localhost) is commonly used by local test tooling - "
            "verify it actually points at real plant hardware, not a test/simulator endpoint, before "
            "treating this as a genuine site connection."
        )
    st.caption(
        "Validate end-to-end (Live Data showing real values, an Event Record firing from a real threshold "
        "crossing, Ask AI answering from real history) before treating a plant as live - see "
        "`docs/REAL_PLC_CUTOVER_PROCEDURE.md`."
    )

st.divider()

# --- 6. User accounts ---
st.subheader("6. User accounts")
users = status["user_accounts"]
st.markdown(_badge(f"{users['active_count']}/{users['total']} accounts active, {users['admin_count']} admin", ts.NEUTRAL), unsafe_allow_html=True)
st.caption(
    "This page cannot tell which accounts (if any) are leftover demo/test accounts - that judgment needs "
    "a human who knows the real site's intended user list. Review the list below and deactivate/reset "
    "anything that shouldn't exist in production from **User Management (Admin)**."
)
with st.expander("Current accounts"):
    for account in users["accounts"]:
        state = "active" if account["active"] else "inactive"
        st.caption(f"- {account['username']} — {account['role']}, {state}")

st.divider()

# --- 7. Backup destination ---
st.subheader("7. Backup destination and retention")
backup = status["backup"]
if not backup["enabled"]:
    st.markdown(_badge("Backup disabled", ts.WARNING), unsafe_allow_html=True)
else:
    tier = ts.CAUTION if backup["looks_like_default_local_path"] else ts.POSITIVE
    st.markdown(_badge("Backup enabled", tier), unsafe_allow_html=True)
st.caption(f"Destination: `{backup['destination']}`")
if backup["looks_like_default_local_path"]:
    st.caption(
        "⚠️ This is this project's default local path (same disk as the application) - confirm it points "
        "at genuinely durable, off-VM storage for the real deployment before relying on it for disaster "
        "recovery. Change it in `config/settings.ini`'s `[HISTORIAN_MAINTENANCE]` `backup_destination_dir`."
    )

st.divider()

# --- 8. Equipment/tag configuration completeness ---
st.subheader("8. Equipment/tag configuration completeness")
completeness = status["configuration_completeness"]
percent = completeness["overall_percent"]
tier = ts.POSITIVE if percent >= 80 else (ts.CAUTION if percent >= 40 else ts.WARNING)
st.markdown(_badge(f"{percent}% complete", tier), unsafe_allow_html=True)
st.caption(
    "Deterministic weighted score across the factory profile, every plant, and every classified equipment "
    "instance (engine/configuration_completeness.py - the same engine Factory Configuration's own summary "
    "uses). Go to **Factory Configuration** / **Equipment & Tag Configuration (Admin)** to fill in gaps."
)
if completeness["top_missing"]:
    with st.expander(f"Top {len(completeness['top_missing'])} missing fields (highest engineering significance first)"):
        for item in completeness["top_missing"]:
            st.caption(f"- {item['label']} ({item['scope']}): missing `{item['field']}`")
