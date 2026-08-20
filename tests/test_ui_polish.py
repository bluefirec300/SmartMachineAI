import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

from ui import table_style as ts
from ui.asset_performance_data import STATE_TIERS, state_badge_html
from ui.data_health_data import STATUS_TIERS, status_badge_html

"""
UI polish phase - visible SmartFactoryAI branding, and the shared
ui.table_style helper used by Asset Performance and Data Health.
Deliberately does not touch or re-test the 7+ OTHER pages that already
had their own row-highlighting before this phase (Event Records,
Anomalies, Live Data, Savings Verification, Energy Opportunities,
Equipment Health) - those were explicitly out of scope and untouched.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME_PAGE = str(PROJECT_ROOT / "ui" / "Home.py")


class TestVisibleBranding(unittest.TestCase):
    def test_home_page_title_is_smartfactoryai(self):
        source = (PROJECT_ROOT / "ui" / "Home.py").read_text(encoding="utf-8")
        self.assertIn('page_title="SmartFactoryAI"', source)
        self.assertIn('st.title("🏭 SmartFactoryAI")', source)
        self.assertNotIn("SmartMachineAI", source)

    def test_login_form_title_is_smartfactoryai(self):
        source = (PROJECT_ROOT / "ui" / "auth.py").read_text(encoding="utf-8")
        self.assertIn('st.title("🏭 SmartFactoryAI")', source)
        self.assertNotIn('st.title("🏭 SmartMachineAI', source)

    def test_login_form_renders_smartfactoryai_via_apptest(self):
        # Home.py gates on auth.require_login() - with no session_state
        # user set, this exercises the real login form render path.
        at = AppTest.from_file(HOME_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))
        titles = [t.value for t in at.title]
        self.assertTrue(any("SmartFactoryAI" in t for t in titles))
        self.assertFalse(any("SmartMachineAI" in t for t in titles))


class TestNoAccidentalInternalRenaming(unittest.TestCase):
    """Guards against the branding change spreading into internal,
    non-UI identifiers it was explicitly told not to touch."""

    def test_project_root_directory_name_unchanged(self):
        self.assertEqual(PROJECT_ROOT.name, "SmartMachineAI")

    def test_no_systemd_unit_file_was_renamed_to_smartfactoryai(self):
        deploy_dir = PROJECT_ROOT / "deploy" / "systemd"
        if deploy_dir.exists():
            for unit_file in deploy_dir.glob("*.service"):
                self.assertNotIn("smartfactoryai", unit_file.name.lower())

    def test_package_init_docstrings_left_alone(self):
        # Internal package identity, not user-facing UI - explicitly
        # out of scope per the phase instructions.
        init_text = (PROJECT_ROOT / "tests" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("SmartMachineAI", init_text)

    def test_plc_connectivity_code_comment_left_alone(self):
        # A code comment, not visible UI text - intentionally retained.
        source = (PROJECT_ROOT / "ui" / "pages" / "10_PLC_Connectivity.py").read_text(encoding="utf-8")
        self.assertIn("SmartMachineAI", source)


class TestTableStyleHelper(unittest.TestCase):
    def test_row_css_returns_empty_string_for_unknown_tier(self):
        self.assertEqual(ts.row_css(None), "")
        self.assertEqual(ts.row_css("not_a_real_tier"), "")

    def test_row_css_known_tiers_each_distinct(self):
        tiers = [ts.POSITIVE, ts.STABLE, ts.CAUTION, ts.WARNING, ts.NEUTRAL]
        styles = [ts.row_css(t) for t in tiers]
        self.assertEqual(len(styles), len(set(styles)))
        for style in styles:
            self.assertIn("background-color", style)
            self.assertIn("color", style)

    def test_badge_html_falls_back_gracefully_for_unknown_tier(self):
        html = ts.badge_html("Something", "not_a_real_tier")
        self.assertIn("Something", html)
        self.assertIn("<span", html)

    def test_make_row_highlighter_maps_column_value_to_style(self):
        import pandas as pd

        highlighter = ts.make_row_highlighter("State", {"Bad": ts.WARNING})
        row = pd.Series({"State": "Bad", "Other": 1})
        result = highlighter(row)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], ts.row_css(ts.WARNING))

        unmatched_row = pd.Series({"State": "Unknown", "Other": 1})
        self.assertEqual(highlighter(unmatched_row), ["", ""])


class TestAssetPerformanceStateMapping(unittest.TestCase):
    def test_every_engine_state_has_a_tier(self):
        for state in ("IMPROVING", "STABLE", "DEGRADING", "SIGNIFICANTLY_DEGRADING",
                      "INSUFFICIENT_EVIDENCE", "NOT_CLASSIFIED"):
            self.assertIn(state, STATE_TIERS)

    def test_improving_and_stable_are_distinguishable_tiers(self):
        self.assertNotEqual(STATE_TIERS["IMPROVING"], STATE_TIERS["STABLE"])

    def test_degrading_is_less_severe_tier_than_significantly_degrading(self):
        self.assertEqual(STATE_TIERS["DEGRADING"], ts.CAUTION)
        self.assertEqual(STATE_TIERS["SIGNIFICANTLY_DEGRADING"], ts.WARNING)

    def test_state_badge_html_reflects_the_mapped_tier(self):
        html = state_badge_html("SIGNIFICANTLY_DEGRADING")
        self.assertIn("Significantly Degrading", html)


class TestDataHealthStatusMapping(unittest.TestCase):
    def test_every_engine_status_has_a_tier(self):
        for status in ("GOOD", "DEGRADED", "POOR", "UNAVAILABLE"):
            self.assertIn(status, STATUS_TIERS)

    def test_good_is_positive_and_poor_is_warning(self):
        self.assertEqual(STATUS_TIERS["GOOD"], ts.POSITIVE)
        self.assertEqual(STATUS_TIERS["POOR"], ts.WARNING)

    def test_unavailable_is_neutral_never_positive_or_warning(self):
        self.assertEqual(STATUS_TIERS["UNAVAILABLE"], ts.NEUTRAL)

    def test_status_badge_html_reflects_the_mapped_tier(self):
        html = status_badge_html("GOOD")
        self.assertIn("Good", html)


if __name__ == "__main__":
    unittest.main()
