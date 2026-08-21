from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.notification_settings_manager import MIN_SEVERITY_OPTIONS, NotificationSettingsManager
from ui import auth
from ui.data_access import CONFIG_DATABASE_PATH

"""
Phase V2.3 - admin UI for who receives alarm emails and the core
enabled/severity/cooldown behavior added in Phase V2.2. Deliberately
does NOT touch SMTP host/port/TLS/from-address (config/settings.ini)
or SMTP_USERNAME/SMTP_PASSWORD (environment variables only) - those
stay server-configuration-only, per this phase's explicit requirement
to keep credentials out of the UI. See docs/ALARM_NOTIFICATIONS_SETUP.md
for the full picture of what's admin-editable here vs. server-only.

app/notification_worker.py reads NotificationSettingsManager fresh
every cycle (default 30s) - changes made here take effect without a
service restart, unlike most other admin-configurable settings in
this app.
"""

auth.require_role("admin")

current_user = auth.current_user()

st.title("🔔 Alarm Notification Settings")
st.caption(
    "Who receives alarm emails, and when. This is the SAME notification system built in Phase V2.2 - "
    "this page only changes what's stored in the database, not how notifications are sent. "
    "SMTP server address/port/TLS and credentials are server configuration only "
    "(`config/settings.ini` / environment variables) and are never shown or editable here."
)


@st.cache_resource
def _get_settings_manager() -> NotificationSettingsManager:
    return NotificationSettingsManager(database_path=CONFIG_DATABASE_PATH)


settings_manager = _get_settings_manager()

st.divider()
st.subheader("Behavior")

settings = settings_manager.get_settings()

with st.form("notification_settings_form"):
    enabled = st.toggle(
        "Enable alarm email notifications",
        value=settings["enabled"],
        help="Off by default. When off, the notification worker does nothing every cycle - no SMTP connection is ever attempted.",
    )

    behavior_cols = st.columns(2)
    with behavior_cols[0]:
        min_severity = st.selectbox(
            "Minimum severity to notify",
            MIN_SEVERITY_OPTIONS,
            index=MIN_SEVERITY_OPTIONS.index(settings["min_severity"]),
            help="\"warning\" also includes \"alarm\" - severities are ranked, not exclusive tiers.",
        )
    with behavior_cols[1]:
        cooldown_minutes = st.number_input(
            "Cooldown between repeat notifications (minutes)",
            min_value=1.0,
            value=float(settings["cooldown_minutes"]),
            step=5.0,
            help="How long before the SAME equipment/tag/alarm condition is allowed to email again - "
            "prevents a service restart re-evaluating an already-active alarm from spamming a fresh email.",
        )

    submitted = st.form_submit_button("Save behavior settings", type="primary")

if submitted:
    settings_manager.update_settings(
        enabled=enabled,
        min_severity=min_severity,
        cooldown_minutes=cooldown_minutes,
        changed_by=current_user["username"],
    )
    st.success("Saved. The notification worker picks this up on its next cycle (within ~30 seconds) - no restart needed.")
    st.rerun()

if settings["updated_at"]:
    st.caption(f"Last changed {settings['updated_at']} UTC by {settings['updated_by'] or 'unknown'}.")

st.divider()
st.subheader("Recipients")

recipients = settings_manager.list_recipients()

with st.form("add_recipient_form", clear_on_submit=True):
    add_cols = st.columns([4, 1])
    with add_cols[0]:
        new_email = st.text_input("Add recipient", placeholder="engineer@example.com", label_visibility="collapsed")
    with add_cols[1]:
        add_submitted = st.form_submit_button("+ Add", width="stretch")

if add_submitted:
    if not new_email.strip():
        st.error("Enter an email address.")
    else:
        try:
            settings_manager.add_recipient(new_email.strip(), created_by=current_user["username"])
            st.success(f"Added {new_email.strip()}.")
            st.rerun()
        except ValueError as error:
            st.error(str(error))

if not recipients:
    st.caption("No recipients configured yet - notifications have nowhere to send even if enabled.")
else:
    recipients_df = pd.DataFrame(
        [{"Email": r["email"], "Added": r["created_at"], "Added by": r["created_by"] or "-"} for r in recipients]
    )

    selection = st.dataframe(
        recipients_df,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="recipients_table",
    )

    selected_positions = selection.selection.rows if selection else []

    if selected_positions:
        selected_recipient = recipients[selected_positions[0]]
        st.write(f"Selected: **{selected_recipient['email']}**")

        if st.button("🗑 Remove recipient"):
            settings_manager.remove_recipient(selected_recipient["id"], changed_by=current_user["username"])
            st.success(f"Removed {selected_recipient['email']}.")
            st.rerun()
    else:
        st.caption("Select a row above to remove a recipient.")

st.divider()

with st.expander("Recent changes (audit log)"):
    from config.configuration_manager import ConfigurationManager

    audit_rows = ConfigurationManager(database_path=CONFIG_DATABASE_PATH).get_audit_log(limit=50)
    notification_audit_rows = [
        row for row in audit_rows if row["entity_type"] in ("notification_settings", "notification_recipient")
    ]

    if not notification_audit_rows:
        st.caption("No notification setting changes recorded yet.")
    else:
        for row in notification_audit_rows:
            detail_suffix = f" ({row['details']})" if row["details"] else ""
            st.write(f"- {row['timestamp']} — **{row['action']}** `{row['entity_name']}`{detail_suffix} (by {row['username']})")
