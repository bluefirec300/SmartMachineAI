import unittest
from unittest import mock

from ui import auth

"""
Phase V1.4 - render_sidebar_identity() is the one function already
proven (per its own docstring/usage from Home.py) to render on every
page. Before this, no page outside PLC Connectivity/Equipment & Tag
Configuration/Energy Dashboard/Production Context showed which
environment (simulation vs. actual/real PLC) was active - a real gap
once real PLC data starts flowing, since an engineer on any other page
had no cue whether they were looking at demo or real data. These tests
mock st.sidebar/st.error/st.caption directly (consistent with the
precedent in tests/test_energy_dashboard_data.py) rather than driving a
full authenticated AppTest session, since ui.auth.current_user() reads
st.session_state, which is easy to fake without a real script run.
"""


class RenderSidebarEnvironmentBadgeTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(auth, "st")
        self.mock_st = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_st.session_state = {auth.SESSION_KEY: {"display_name": "Bob", "role": "engineer"}}
        self.mock_st.sidebar.__enter__ = mock.Mock(return_value=None)
        self.mock_st.sidebar.__exit__ = mock.Mock(return_value=False)

    def test_simulation_environment_shows_a_quiet_caption(self):
        with mock.patch.object(auth, "get_active_environment", return_value="simulation"):
            auth.render_sidebar_identity()

        self.mock_st.error.assert_not_called()
        caption_texts = [call.args[0] for call in self.mock_st.caption.call_args_list]
        self.assertTrue(any("Simulation" in text for text in caption_texts))

    def test_actual_environment_shows_a_prominent_warning(self):
        with mock.patch.object(auth, "get_active_environment", return_value="actual"):
            auth.render_sidebar_identity()

        self.mock_st.error.assert_called_once()
        error_text = self.mock_st.error.call_args.args[0]
        self.assertIn("ACTUAL", error_text)

    def test_no_badge_rendered_when_logged_out(self):
        self.mock_st.session_state = {}

        with mock.patch.object(auth, "get_active_environment", return_value="actual"):
            auth.render_sidebar_identity()

        self.mock_st.error.assert_not_called()
        self.mock_st.caption.assert_not_called()


if __name__ == "__main__":
    unittest.main()
