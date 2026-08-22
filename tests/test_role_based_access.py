import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from ui import auth

"""
Phase V2.9 - dedicated RBAC tests. Three layers:

1. Unit tests of ui/auth.py's role/session primitives (has_role,
   can_edit, require_role, the new idle-timeout logic in
   require_login()) - `st` mocked directly, same pattern as
   tests/test_sidebar_environment_badge.py, since these functions only
   ever read/write st.session_state and call a couple of st.* widgets.
2. Unauthenticated AppTest smoke checks confirming every admin-only
   page's require_role("admin") gate actually blocks (renders the
   permission error, no exception) - not just that the function exists
   in isolation.
3. A source-level "every page file is reachable from Home.py's
   navigation" check. This exists because Phase V2.9's own
   investigation found exactly this bug already in production: System
   Health, Alarm Notification Settings, and New-Factory Setup were all
   fully built, tested, and internally role-gated in earlier phases,
   but were never added to ui/Home.py's navigation dict - making them
   silently unreachable from the sidebar for every role, including
   admin. Fixed in this same phase; this test exists so a future page
   can't regress the same way undetected.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_PAGES_DIR = PROJECT_ROOT / "ui" / "pages"
HOME_PAGE_SOURCE = (PROJECT_ROOT / "ui" / "Home.py").read_text(encoding="utf-8")

ADMIN_ONLY_PAGES = [
    "9_User_Management.py",
    "10_PLC_Connectivity.py",
    "11_Equipment_and_Tag_Configuration.py",
    "23_Alarm_Notification_Settings.py",
    "24_New_Factory_Setup.py",
    "25_Audit_Log.py",
]


class RoleHelperTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(auth, "st")
        self.mock_st = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_st.session_state = {}

    def _log_in_as(self, role: str):
        self.mock_st.session_state[auth.SESSION_KEY] = {
            "username": "test_user",
            "display_name": "Test User",
            "role": role,
        }

    def test_has_role_true_for_matching_role(self):
        self._log_in_as("admin")
        self.assertTrue(auth.has_role("admin"))

    def test_has_role_false_for_non_matching_role(self):
        self._log_in_as("operator")
        self.assertFalse(auth.has_role("admin"))

    def test_has_role_accepts_multiple_candidates(self):
        self._log_in_as("engineer")
        self.assertTrue(auth.has_role("admin", "engineer"))

    def test_has_role_false_when_logged_out(self):
        self.assertFalse(auth.has_role("admin"))

    def test_can_edit_matches_has_role(self):
        self._log_in_as("operator")
        self.assertFalse(auth.can_edit("admin", "engineer"))
        self._log_in_as("engineer")
        self.assertTrue(auth.can_edit("admin", "engineer"))

    def test_require_role_allows_matching_role(self):
        self._log_in_as("admin")
        auth.require_role("admin")
        self.mock_st.error.assert_not_called()
        self.mock_st.stop.assert_not_called()

    def test_require_role_blocks_non_matching_role(self):
        self._log_in_as("operator")
        auth.require_role("admin")
        self.mock_st.error.assert_called_once()
        self.assertIn("permission", self.mock_st.error.call_args.args[0])
        self.mock_st.stop.assert_called_once()

    def test_require_role_blocks_logged_out_session(self):
        auth.require_role("admin")
        self.mock_st.error.assert_called_once()
        self.mock_st.stop.assert_called_once()


class RequireLoginIdleTimeoutTests(unittest.TestCase):
    """
    require_login() is called on every rerun (Home.py calls it at
    module scope, which re-executes on every interaction), so it
    doubles as the idle-timeout check - no separate background timer
    needed.
    """

    def setUp(self):
        patcher = mock.patch.object(auth, "st")
        self.mock_st = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_st.session_state = {
            auth.SESSION_KEY: {
                "username": "test_user",
                "display_name": "Test User",
                "role": "engineer",
            }
        }

        time_patcher = mock.patch.object(auth, "time")
        self.mock_time = time_patcher.start()
        self.addCleanup(time_patcher.stop)

    def test_first_access_after_login_initializes_the_clock_without_logging_out(self):
        # No LAST_ACTIVITY_KEY yet - simulates the run right after
        # _login_form() set SESSION_KEY.
        self.mock_time.monotonic.return_value = 1000.0

        user = auth.require_login()

        self.assertEqual(user["username"], "test_user")
        self.assertEqual(self.mock_st.session_state[auth.LAST_ACTIVITY_KEY], 1000.0)
        self.mock_st.stop.assert_not_called()

    def test_activity_within_the_timeout_window_keeps_the_session(self):
        self.mock_st.session_state[auth.LAST_ACTIVITY_KEY] = 1000.0
        self.mock_time.monotonic.return_value = 1000.0 + (auth.IDLE_TIMEOUT_MINUTES * 60) - 1

        user = auth.require_login()

        self.assertEqual(user["username"], "test_user")
        self.assertIn(auth.SESSION_KEY, self.mock_st.session_state)
        self.mock_st.stop.assert_not_called()

    def test_idle_beyond_the_timeout_logs_out(self):
        self.mock_st.session_state[auth.LAST_ACTIVITY_KEY] = 1000.0
        self.mock_time.monotonic.return_value = 1000.0 + (auth.IDLE_TIMEOUT_MINUTES * 60) + 1

        # st.stop() is mocked (doesn't actually halt), so execution
        # falls through to the function's own "unreachable" guard -
        # that RuntimeError is this test's proof the timeout path was
        # taken (a real Streamlit session would have actually stopped
        # here instead).
        with self.assertRaises(RuntimeError):
            auth.require_login()

        self.assertNotIn(auth.SESSION_KEY, self.mock_st.session_state)
        self.assertNotIn(auth.LAST_ACTIVITY_KEY, self.mock_st.session_state)
        self.assertTrue(self.mock_st.session_state.get(auth._IDLE_LOGOUT_FLASH_KEY))

    def test_no_session_at_all_does_not_touch_the_activity_clock(self):
        self.mock_st.session_state = {}

        with self.assertRaises(RuntimeError):
            auth.require_login()

        self.assertNotIn(auth.LAST_ACTIVITY_KEY, self.mock_st.session_state)


class AdminPageGateSmokeTests(unittest.TestCase):
    """Every admin-only page actually blocks an unauthenticated
    session, via a real AppTest run (not just the require_role() unit
    tests above, which don't exercise the page's own top-of-file call)."""

    def test_every_admin_only_page_blocks_unauthenticated_access(self):
        for page_name in ADMIN_ONLY_PAGES:
            with self.subTest(page=page_name):
                at = AppTest.from_file(str(UI_PAGES_DIR / page_name), default_timeout=30)
                at.run()

                self.assertFalse(bool(at.exception), f"{page_name} raised: {at.exception}")
                self.assertTrue(
                    any("permission" in e.value for e in at.error),
                    f"{page_name} did not show a permission error when unauthenticated",
                )


class NavigationReachabilityTests(unittest.TestCase):
    """
    Guards against the exact gap this phase found: a page file that
    exists, is fully built, and gates itself correctly, but was never
    added to Home.py's navigation - making it unreachable regardless
    of role.
    """

    def test_every_page_file_is_referenced_in_home_navigation(self):
        page_files = sorted(p.name for p in UI_PAGES_DIR.glob("*.py"))

        self.assertTrue(page_files, "No page files found - check UI_PAGES_DIR")

        unreferenced = [
            name for name in page_files if f"pages/{name}" not in HOME_PAGE_SOURCE
        ]

        self.assertEqual(
            unreferenced,
            [],
            f"These page files exist but are never referenced in ui/Home.py's "
            f"navigation, making them unreachable from the sidebar: {unreferenced}",
        )

    def test_every_admin_gated_page_is_registered_under_the_admin_role_check(self):
        # A page requiring admin role must be listed inside the
        # `if auth.has_role("admin"):` block, not the always-visible
        # general/engineer lists - otherwise it would appear in the
        # sidebar for non-admins even though it blocks them once clicked
        # (a confusing, unnecessary exposure of the page's existence).
        admin_block_start = HOME_PAGE_SOURCE.index('if auth.has_role("admin"):')
        admin_block = HOME_PAGE_SOURCE[admin_block_start:]

        for page_name in ADMIN_ONLY_PAGES:
            with self.subTest(page=page_name):
                self.assertIn(f"pages/{page_name}", admin_block)


if __name__ == "__main__":
    unittest.main()
