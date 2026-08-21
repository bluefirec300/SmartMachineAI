from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui import table_style as ts
from ui.system_health_data import ServiceHealth, format_time_ago, get_all_service_health

"""
Phase V2.1 - System & Worker Health. Answers "is SmartFactoryAI's own
SOFTWARE running correctly" - a completely different question from
Equipment Health (mechanical condition of factory machines) or Data
Health (telemetry trustworthiness of factory sensors). This page knows
nothing about compressors, chillers, or tags - only about the 13
systemd processes that make up this application itself.

Every value shown here is either a real systemd property (queried via
unprivileged `systemctl show`/`journalctl -u`, no sudo required - see
ui/system_health_data.py's own docstring for why that's safe), a
timestamp already written by that service/worker to a table it owns as
part of its real job, or an on-disk marker file
app/historian_maintenance_worker.py already writes. Nothing here is a
new heartbeat mechanism, and nothing is guessed - an unavailable
signal shows as "Unavailable", never a fabricated value.
"""

st.title("🩺 System Health")
st.caption(
    "Is the SmartFactoryAI **software itself** running correctly - not the factory. "
    "For factory equipment condition, see **Equipment Health**; for sensor/telemetry "
    "trustworthiness, see **Data Health**. This page only ever reports what systemd and "
    "each service's own existing output already record - it never invents a status."
)

RUNNING = "running"
STOPPED = "stopped"
NOT_INSTALLED = "not_installed"
UNKNOWN = "unknown"

STATE_TIERS = {
    RUNNING: ts.POSITIVE,
    STOPPED: ts.WARNING,
    NOT_INSTALLED: ts.NEUTRAL,
    UNKNOWN: ts.NEUTRAL,
}

STATE_LABELS = {
    RUNNING: "✅ Running",
    STOPPED: "🛑 Stopped",
    NOT_INSTALLED: "⚪ Not installed on this host",
    UNKNOWN: "❔ Unknown (systemctl unavailable)",
}


def _state_key(service: ServiceHealth) -> str:
    if not service.systemctl_available:
        return UNKNOWN
    if service.unit_found is False:
        return NOT_INSTALLED
    if service.running is True:
        return RUNNING
    if service.running is False:
        return STOPPED
    return UNKNOWN


def _error_badge(service: ServiceHealth) -> str:
    if not service.journal_available:
        return ts.badge_html("Unavailable", ts.NEUTRAL)
    if service.recent_error_count is None:
        return ts.badge_html("Unavailable", ts.NEUTRAL)
    if service.recent_error_count == 0:
        return ts.badge_html("None found", ts.STABLE)
    return ts.badge_html(f"{service.recent_error_count} found", ts.CAUTION)


if st.button("🔄 Refresh now", help="Re-queries systemd and each service's own status data live - this page never caches."):
    st.rerun()

services = get_all_service_health()
now = datetime.now()

unavailable_count = sum(1 for s in services if not s.systemctl_available)
if unavailable_count:
    st.warning(
        f"`systemctl` could not be reached for {unavailable_count} service(s) - their state shows as "
        "Unknown below rather than a guessed value. This page requires the app to run on the same host "
        "as these systemd services."
    )

st.caption(f"As of {now.strftime('%Y-%m-%d %H:%M:%S')} - queried live on every page load, never cached.")


def _build_dataframe(entries: list[ServiceHealth]) -> tuple[pd.DataFrame, list[str]]:
    rows = []
    state_keys = []
    for service in entries:
        state_key = _state_key(service)
        state_keys.append(state_key)

        uptime = "-"
        if service.running and service.since:
            uptime = format_time_ago(service.since, now).replace(" ago", "")

        last_activity = format_time_ago(service.last_activity, now) if service.last_activity else "Unavailable"

        rows.append(
            {
                "Service": service.label,
                "State": STATE_LABELS[state_key],
                "Uptime": uptime,
                # str, not int - this column mixes real counts with "Unavailable"
                # for not-installed/unreachable services (e.g. a worker whose
                # systemd unit hasn't been deployed yet), and pandas/Arrow
                # cannot serialize a column mixing int and str.
                "Restarts": str(service.restart_count) if service.restart_count is not None else "Unavailable",
                "Last activity": last_activity,
                "What that means": service.activity_label,
                "Recent errors (log scan)": (
                    "Unavailable" if not service.journal_available or service.recent_error_count is None
                    else ("None found" if service.recent_error_count == 0 else f"{service.recent_error_count} found")
                ),
            }
        )
    return pd.DataFrame(rows), state_keys


def _styled(df: pd.DataFrame, state_keys: list[str]):
    def highlight(row: pd.Series) -> list[str]:
        # row.name is the positional (default RangeIndex) row number,
        # which lines up 1:1 with state_keys since both were built
        # from the same entries list in the same order.
        tier = STATE_TIERS.get(state_keys[row.name], ts.NEUTRAL)
        return [ts.row_css(tier)] * len(row)

    return df.style.apply(highlight, axis=1)


st.subheader("Core services")
st.caption("The three processes every other part of this app depends on directly.")
core_df, core_state_keys = _build_dataframe([s for s in services if s.category == "Core service"])
st.dataframe(_styled(core_df, core_state_keys), hide_index=True, width="stretch")

st.subheader("Background workers")
st.caption(
    "Periodic engines that compute Equipment Health, Data Health, Anomalies, Energy KPIs, and the rest - "
    "each writes to its own table on its own schedule, so 'Last activity' means different things per worker "
    "(see 'What that means' for each). A long gap is not automatically a failure - some tables only gain a "
    "new row when something worth recording actually happens."
)
worker_df, worker_state_keys = _build_dataframe([s for s in services if s.category == "Background worker"])
st.dataframe(_styled(worker_df, worker_state_keys), hide_index=True, width="stretch", height=450)

with st.expander("Error scan details (recent log lines flagged per service)"):
    st.caption(
        "Scans each service's most recent 200 journald log lines for anything that looks like an error. "
        "This is a soft signal, not a confirmed diagnosis - a flagged line may be old within that window, "
        "and a routine 'cycle: {...}' summary line reporting its own zero error count is correctly never "
        "flagged (only a genuinely non-zero reported count, or an actual exception/traceback, counts)."
    )
    for service in services:
        if service.recent_error_count:
            st.markdown(f"**{service.label}** - {service.recent_error_count} flagged line(s)")
            if service.recent_error_sample:
                st.code(service.recent_error_sample, language=None)
    if not any(s.recent_error_count for s in services):
        st.write("No error-looking lines found in any service's recent log window.")

    notes = [s for s in services if s.last_activity_note]
    if notes:
        st.markdown("**Additional notes:**")
        for service in notes:
            st.caption(f"- {service.label}: {service.last_activity_note}")
