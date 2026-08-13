from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.user_manager import ROLES, UserManager
from ui import auth
from ui.data_access import CONFIG_DATABASE_PATH


auth.require_role("admin")

current_user = auth.current_user()

st.title("👤 User Management")
st.caption(
    "Create accounts, change roles, and reset passwords. Every change here is "
    "recorded in the audit log below. Admin-only page."
)


@st.cache_resource
def _get_user_manager() -> UserManager:
    return UserManager(database_path=CONFIG_DATABASE_PATH)


user_manager = _get_user_manager()

ROLE_LABELS = {"admin": "Administrator", "engineer": "Engineer", "operator": "Operator"}


@st.dialog("Create User")
def _create_user_dialog() -> None:
    username = st.text_input("Username (login id)", key="new_user_username")
    display_name = st.text_input("Display name", key="new_user_display_name")
    role = st.selectbox("Role", ROLES, format_func=lambda r: ROLE_LABELS[r], key="new_user_role")
    password = st.text_input(
        "Initial password", type="password", value="123456", key="new_user_password"
    )

    if st.button("Create", type="primary", key="new_user_save"):
        if not username.strip() or not display_name.strip():
            st.error("Username and display name are required.")
        elif user_manager.get_user(username) is not None:
            st.error(f"Username '{username.strip().lower()}' already exists.")
        else:
            user_manager.create_user(
                username=username,
                display_name=display_name,
                password=password,
                role=role,
                created_by=current_user["username"],
            )
            st.success(f"Created {display_name} ({ROLE_LABELS[role]}).")
            st.rerun()


@st.dialog("Reset Password")
def _reset_password_dialog(target_username: str, target_display_name: str) -> None:
    st.write(f"Reset password for **{target_display_name}**")
    new_password = st.text_input("New password", type="password", key="reset_pw_value")

    if st.button("Reset", type="primary", key="reset_pw_save"):
        if not new_password:
            st.error("Enter a new password.")
        else:
            user_manager.reset_password(
                username=target_username,
                new_password=new_password,
                changed_by=current_user["username"],
            )
            st.success(f"Password reset for {target_display_name}.")
            st.rerun()


if st.button("+ Create User", type="primary"):
    _create_user_dialog()

st.divider()

users = user_manager.get_users()
users_df = pd.DataFrame(
    [
        {
            "Username": user["username"],
            "Display Name": user["display_name"],
            "Role": ROLE_LABELS.get(user["role"], user["role"]),
            "Active": "Yes" if user["active"] else "No",
            "Last Login": user["last_login_at"] or "never",
            "Created": user["created_at"],
        }
        for user in users
    ]
)

selection = st.dataframe(
    users_df,
    hide_index=True,
    width="stretch",
    height=400,
    on_select="rerun",
    selection_mode="single-row",
    key="users_table",
)

selected_positions = selection.selection.rows if selection else []

if not selected_positions:
    st.caption("Select a row above to change its role, reset its password, or deactivate it.")
else:
    selected_user = users[selected_positions[0]]

    if selected_user["username"] == current_user["username"]:
        st.info("This is your own account - role/active-state changes to your own account are disabled here to avoid accidentally locking yourself out.")
    else:
        st.write(f"Selected: **{selected_user['display_name']}** ({selected_user['username']})")

        role_col, password_col, active_col = st.columns(3)

        with role_col:
            new_role = st.selectbox(
                "Change role to",
                ROLES,
                index=ROLES.index(selected_user["role"]),
                format_func=lambda r: ROLE_LABELS[r],
                key=f"role_select_{selected_user['username']}",
            )
            if new_role != selected_user["role"] and st.button(
                "Apply role change", key=f"apply_role_{selected_user['username']}"
            ):
                user_manager.update_role(
                    username=selected_user["username"],
                    new_role=new_role,
                    changed_by=current_user["username"],
                )
                st.success(f"{selected_user['display_name']} is now {ROLE_LABELS[new_role]}.")
                st.rerun()

        with password_col:
            if st.button("Reset password", key=f"reset_{selected_user['username']}"):
                _reset_password_dialog(selected_user["username"], selected_user["display_name"])

        with active_col:
            if selected_user["active"]:
                if st.button("Deactivate", key=f"deactivate_{selected_user['username']}"):
                    user_manager.set_active(
                        username=selected_user["username"],
                        active=False,
                        changed_by=current_user["username"],
                    )
                    st.success(f"{selected_user['display_name']} deactivated.")
                    st.rerun()
            else:
                if st.button("Reactivate", key=f"reactivate_{selected_user['username']}"):
                    user_manager.set_active(
                        username=selected_user["username"],
                        active=True,
                        changed_by=current_user["username"],
                    )
                    st.success(f"{selected_user['display_name']} reactivated.")
                    st.rerun()

st.divider()

with st.expander("Recent changes (audit log)"):
    from config.configuration_manager import ConfigurationManager

    audit_rows = ConfigurationManager(database_path=CONFIG_DATABASE_PATH).get_audit_log(limit=50)
    user_audit_rows = [row for row in audit_rows if row["entity_type"] == "user"]

    if not user_audit_rows:
        st.caption("No user-management changes recorded yet.")
    else:
        for row in user_audit_rows:
            detail_suffix = f" ({row['details']})" if row["details"] else ""
            st.write(
                f"- {row['timestamp']} — **{row['action']}** on `{row['entity_name']}`"
                f"{detail_suffix} (by {row['username']})"
            )
