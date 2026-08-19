import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from ui import data_health_fleet_data as dhfd
from ui import data_health_history_data as dhhd

"""
Phase 16.5 - Data Health history UI-adapter and page-smoke tests.
Read-only against the real live simulation database (already migrated
and seeded by this phase's own commissioning pass) - matches every
prior phase's own 'test against real data' discipline.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_HEALTH_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "21_Data_Health.py")
EQUIPMENT_HEALTH_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "19_Equipment_Health.py")


class TestHistoryAdapter(unittest.TestCase):
    def setUp(self):
        dhhd.force_refresh()

    def test_first_recorded_at_is_set_after_commissioning(self):
        self.assertIsNotNone(dhhd.first_recorded_at())

    def test_default_window_returns_start_before_end(self):
        start, end = dhhd.default_window(days=7)
        self.assertLess(start, end)

    def test_get_equipment_history_shape(self):
        start, end = dhhd.default_window(days=7)
        result = dhhd.get_equipment_history("p01", "P01.WATER.WSP01", start, end)
        for key in ("history", "duration", "transitions", "recurrence", "component_history"):
            self.assertIn(key, result)

    def test_get_equipment_history_unknown_plant_code_is_safe(self):
        start, end = dhhd.default_window(days=7)
        result = dhhd.get_equipment_history("p99", "P99.NOWHERE.FAKE01", start, end)
        self.assertTrue(result["duration"]["insufficient_history"])

    def test_get_fleet_history_cache_reused(self):
        start, end = dhhd.default_window(days=7)
        dhhd.get_fleet_history(start, end)
        with patch("ui.data_health_history_data.dhh.fleet_status_summary") as mocked:
            dhhd.get_fleet_history(start, end)
            mocked.assert_not_called()

    def test_force_refresh_bypasses_cache(self):
        start, end = dhhd.default_window(days=7)
        dhhd.get_fleet_history(start, end)
        dhhd.force_refresh()
        with patch("ui.data_health_history_data.dhh.fleet_status_summary", return_value={"equipment": [], "by_plant": {}, "by_area": {}, "by_system": {}, "by_equipment_type": {}}) as mocked:
            dhhd.get_fleet_history(start, end)
            mocked.assert_called_once()


class TestDataHealthPageWithHistoryTabsSmoke(unittest.TestCase):
    def setUp(self):
        dhfd.get_fleet_data_health.clear()
        dhhd.force_refresh()

    def test_page_with_new_tabs_loads_without_exception(self):
        at = AppTest.from_file(DATA_HEALTH_PAGE, default_timeout=120)
        at.run()
        self.assertFalse(bool(at.exception))

    def test_page_has_five_tabs(self):
        at = AppTest.from_file(DATA_HEALTH_PAGE, default_timeout=120)
        at.run()
        tab_labels = [t.proto.label for t in at.tabs]
        self.assertEqual(
            tab_labels,
            ["Equipment Overview", "Plant / Area / System Grouping", "Issue View", "History & Trends", "Recurring Issues"],
        )

    def test_equipment_health_page_still_loads_without_exception(self):
        """Untouched by Phase 16.5 - confirms no accidental interference
        from the new history modules."""
        at = AppTest.from_file(EQUIPMENT_HEALTH_PAGE, default_timeout=60)
        at.run()
        self.assertFalse(bool(at.exception))


if __name__ == "__main__":
    unittest.main()
