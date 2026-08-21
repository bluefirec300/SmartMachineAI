from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.user_manager import DEFAULT_DATABASE_PATH, UserManager

SESSION_KEY = "auth_user"

ROLE_LABELS = {
    "admin": "Administrator",
    "engineer": "Engineer",
    "operator": "Operator",
}


def current_user() -> dict[str, Any] | None:
    return st.session_state.get(SESSION_KEY)


def is_logged_in() -> bool:
    return current_user() is not None


def has_role(*roles: str) -> bool:
    user = current_user()
    return bool(user) and user["role"] in roles


def can_edit(*roles_allowed_to_edit: str) -> bool:
    """
    True if the current user's role is one of roles_allowed_to_edit.

    Used throughout the config-carrying pages (Setpoints, Service &
    Maintenance, Documentation, User Management) to gate the actual
    write controls - viewing a page and being able to change something
    on it are different permissions, and "operator" always gets the
    former without the latter.
    """
    return has_role(*roles_allowed_to_edit)


def logout() -> None:
    st.session_state.pop(SESSION_KEY, None)


def _login_form() -> None:
    """
    Renders the login form. Streamlit draws widgets top-to-bottom as
    the script executes, so the title/fields below are already on the
    page by the time we know whether submission succeeded - meaning a
    successful login can NOT just fall through into rendering the rest
    of the app in this same pass without both showing up stacked on
    one page at once. st.rerun() is required here: it halts this run
    immediately, so the login form is never drawn on the run that
    renders the real app.
    """
    st.title("🏭 SmartFactoryAI")
    st.subheader("Sign in")

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")

    if not submitted:
        return

    if not username or not password:
        st.error("Enter both a username and password.")
        return

    manager = UserManager(database_path=DEFAULT_DATABASE_PATH)

    # Phase V1.3 - checked BEFORE the password attempt, so a locked
    # account gets a distinct, honest message instead of the generic
    # "incorrect" one (which would otherwise be technically true but
    # misleading - the password may well be correct, the account is
    # just temporarily locked out from repeated failed attempts).
    lockout = manager.get_lockout_status(username)

    if lockout["locked"]:
        st.error(
            "This account is temporarily locked after repeated failed sign-in attempts. "
            f"Try again after {lockout['locked_until']} UTC, or ask an administrator to reset the password."
        )
        return

    user = manager.authenticate(username, password)

    if user is None:
        st.error("Incorrect username or password.")
        return

    st.session_state[SESSION_KEY] = user
    st.rerun()


def require_login() -> dict[str, Any]:
    """
    Call once, at the very top of the app entrypoint (before the real
    st.navigation(...).run()). Blocks (via st.stop()) and renders a
    login form until a valid session exists; returns the logged-in
    user dict once one does.

    Streamlit's own docs are explicit: "As soon as any session of your
    app executes the st.navigation command, your app will ignore the
    pages/ directory." Until that first call happens in any session -
    i.e. on this exact run, before login - Streamlit falls back to
    auto-listing every file in ui/pages/ in the sidebar, regardless of
    role. So the pre-login screen calls st.navigation() too (with
    position="hidden", a single page holding the login form), instead
    of skipping straight to st.stop() - that's what keeps the sidebar
    empty instead of leaking every page name pre-auth.
    """
    user = current_user()

    if user is not None:
        return user

    st.navigation([st.Page(_login_form, title="Sign In")], position="hidden").run()
    st.stop()
    raise RuntimeError("unreachable")  # st.stop() never returns


def render_sidebar_identity() -> None:
    """
    "Logged in as X (Role)" + a Logout button - shown once per page
    load in the sidebar, so it's visible no matter which page is open.
    """
    user = current_user()

    if user is None:
        return

    with st.sidebar:
        st.divider()
        st.caption(f"Signed in as **{user['display_name']}**")
        st.caption(ROLE_LABELS.get(user["role"], user["role"]))

        if st.button("Log out", key="sidebar_logout_button"):
            logout()
            st.rerun()


def require_role(*roles: str) -> None:
    """
    Page-level hard gate - call at the top of a page that should not
    even be viewable by roles outside `roles` (e.g. User Management,
    admin-only). Unlike can_edit(), this stops rendering entirely
    rather than just disabling controls.
    """
    if not has_role(*roles):
        st.error("You don't have permission to view this page.")
        st.stop()
