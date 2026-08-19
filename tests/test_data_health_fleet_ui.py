import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from engine.data_health_fleet import FleetDataHealthResult
from ui import data_health_fleet_data as dhfd

"""
Phase 16.4 - fleet Data Health UI-adapter and page-smoke tests.

Adapter-conversion tests use a small hand-built FleetDataHealthResult
patched directly into ui.data_health_fleet_data's own
engine.data_health_fleet.calculate_fleet_data_health call (isolating the
adapter's dict-conversion/caching logic from the separately-tested
engine aggregation). Page-smoke tests run AppTest against the REAL live
simulation database (same discipline as tests/test_phase15_ask_integration.py)
since every page/adapter here is strictly read-only.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_HEALTH_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "21_Data_Health.py")
HOME_PAGE = str(PROJECT_ROOT / "ui" / "Home.py")
ASK_AI_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "1_Ask_AI.py")
SCADA_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "12_SCADA_Floor_Plan.py")


def _fake_result() -> FleetDataHealthResult:
    return FleetDataHealthResult(
        as_of="2026-08-20 12:00:00",
        equipment_count=2, assessed_count=2, good_count=1, degraded_count=0, poor_count=1, unavailable_count=0,
        by_plant={"p01": {"equipment": 2, "GOOD": 1, "DEGRADED": 0, "POOR": 1, "UNAVAILABLE": 0, "missing_tags": 0, "stale_tags": 1, "invalid_tags": 0, "gaps": 0}},
        by_area={}, by_system={}, by_equipment_type={"water_supply_pump": {"equipment": 2, "GOOD": 1, "DEGRADED": 0, "POOR": 1, "UNAVAILABLE": 0, "missing_tags": 0, "stale_tags": 1, "invalid_tags": 0, "gaps": 0}},
        missing_tag_count=0, stale_tag_count=1, invalid_tag_count=0, gap_count=0,
        timestamp_issue_count=0, indeterminate_freshness_count=0, frozen_candidate_count=0,
        equipment=[
            {
                "instance_key": "P01.WATER.WSP01", "equipment_id": 1, "equipment_type": "water_supply_pump",
                "confidence_score": 95.0, "confidence_status": "GOOD",
                "component_scores": {"freshness": 95.0, "availability": 95.0, "validity": 95.0, "continuity": 95.0},
                "component_applicability": {"freshness": True, "availability": True, "validity": True, "continuity": True},
                "required_tag_count": 2, "available_tag_count": 2, "fresh_tag_count": 2,
                "missing_tags": [], "stale_tags": [], "invalid_tags": [], "indeterminate_freshness_tags": [],
                "frozen_candidates": [], "gaps": [], "timestamp_issues": [],
                "source_driver": "simulator", "reasons": [], "limitations": [], "as_of": "2026-08-20 12:00:00",
                "evaluation_error": None, "display_name": "WSP01", "plant_code": "p01", "area_name": "Water", "system_name": "Water Supply",
            },
            {
                "instance_key": "P01.WATER.WSP02", "equipment_id": 2, "equipment_type": "water_supply_pump",
                "confidence_score": 40.0, "confidence_status": "POOR",
                "component_scores": {"freshness": 40.0, "availability": 40.0, "validity": 40.0, "continuity": 40.0},
                "component_applicability": {"freshness": True, "availability": True, "validity": True, "continuity": True},
                "required_tag_count": 2, "available_tag_count": 2, "fresh_tag_count": 1,
                "missing_tags": [], "stale_tags": ["P01.WATER.WSP02.Pressure"], "invalid_tags": [], "indeterminate_freshness_tags": [],
                "frozen_candidates": [], "gaps": [], "timestamp_issues": [],
                "source_driver": "simulator", "reasons": [], "limitations": [], "as_of": "2026-08-20 12:00:00",
                "evaluation_error": None, "display_name": "WSP02", "plant_code": "p01", "area_name": "Water", "system_name": "Water Supply",
            },
        ],
        issues=[{
            "tag_name": "P01.WATER.WSP02.Pressure", "instance_key": "P01.WATER.WSP02", "display_name": "WSP02",
            "plant_code": "p01", "last_value": 1.0, "last_time": "2026-08-20 11:00:00",
            "logging_mode": "Fixed interval", "source_driver": "simulator", "issue": "STALE",
        }],
        failures=[],
    )


class TestFleetAdapterConversion(unittest.TestCase):
    def setUp(self):
        dhfd.get_fleet_data_health.clear()
        self._patch = patch("ui.data_health_fleet_data.calculate_fleet_data_health", return_value=_fake_result())
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        dhfd.get_fleet_data_health.clear()

    def test_dict_conversion_preserves_counts(self):
        data = dhfd.get_fleet_data_health()
        self.assertEqual(data["equipment_count"], 2)
        self.assertEqual(data["good_count"], 1)
        self.assertEqual(data["poor_count"], 1)

    def test_raw_tag_dataclasses_never_leak_into_ui_dict(self):
        data = dhfd.get_fleet_data_health()
        for row in data["equipment"]:
            self.assertNotIn("tags", row)

    def test_equipment_rows_include_presentation_helpers(self):
        data = dhfd.get_fleet_data_health()
        row = data["equipment"][0]
        self.assertIn("equipment_type_label", row)
        self.assertIn("confidence_score_text", row)
        self.assertIn("component_display", row)
        self.assertEqual(row["equipment_type_label"], "Water Supply Pump")

    def test_filter_options_derived_from_current_rows(self):
        data = dhfd.get_fleet_data_health()
        options = dhfd.filter_options(data["equipment"])
        self.assertEqual(options["plants"], ["p01"])
        self.assertEqual(options["equipment_types"], ["water_supply_pump"])

    def test_cache_reused_across_repeated_calls(self):
        dhfd.get_fleet_data_health()
        with patch("ui.data_health_fleet_data.calculate_fleet_data_health") as mocked:
            dhfd.get_fleet_data_health()
            mocked.assert_not_called()

    def test_force_refresh_bypasses_cache(self):
        dhfd.get_fleet_data_health()
        dhfd.force_refresh()
        with patch("ui.data_health_fleet_data.calculate_fleet_data_health", return_value=_fake_result()) as mocked:
            dhfd.get_fleet_data_health()
            mocked.assert_called_once()


class TestPageSmokeAgainstRealDatabase(unittest.TestCase):
    """Read-only against the real live simulation database - no fixture
    setup needed, matching tests/test_phase15_context_builder.py's own
    'Read-only against the real live simulation database' convention."""

    def setUp(self):
        dhfd.get_fleet_data_health.clear()

    def test_data_health_fleet_page_loads_without_exception(self):
        at = AppTest.from_file(DATA_HEALTH_PAGE, default_timeout=120)
        at.run()
        self.assertFalse(bool(at.exception))

    def test_data_health_fleet_page_shows_distribution_metrics(self):
        at = AppTest.from_file(DATA_HEALTH_PAGE, default_timeout=120)
        at.run()
        metric_labels = {m.label for m in at.metric}
        self.assertIn("GOOD", metric_labels)
        self.assertIn("DEGRADED", metric_labels)
        self.assertIn("POOR", metric_labels)
        self.assertIn("UNAVAILABLE", metric_labels)

    def test_home_page_with_new_data_health_summary_loads_without_exception(self):
        at = AppTest.from_file(HOME_PAGE, default_timeout=120)
        at.session_state["auth_user"] = {"username": "test_admin", "role": "admin", "display_name": "Test Admin"}
        at.run()
        self.assertFalse(bool(at.exception))

    def test_ask_ai_page_still_loads_without_exception(self):
        """Untouched by Phase 16.4 - confirms no accidental import-time
        interference from the new modules."""
        at = AppTest.from_file(ASK_AI_PAGE, default_timeout=60)
        at.run()
        self.assertFalse(bool(at.exception))

    def test_scada_floor_plan_page_still_loads_without_exception(self):
        """Untouched by Phase 16.4 - confirms the SCADA page was not
        accidentally wired to Data Health (item 4/23's hard boundary).

        Two cross-test-pollution sources are neutralized here, both
        traced directly (not guessed) via a reproduction: (1)
        start_snapshot_writer() spawns a persistent daemon thread
        (@st.cache_resource, survives for the rest of THIS TEST PROCESS
        once started) - patched to a no-op, since this test only needs
        the page SCRIPT to run without exception, not the background
        writer to actually start (already exercised by the page running
        continuously in production). (2) the page's own body ALSO calls
        ui.scada_floor_plan_data._load_equipment() synchronously on
        render (line 91 of the page) - that function is
        @st.cache_data(ttl=5)-memoized keyed only on `plant`, not on
        CONFIG_DATABASE_PATH, so a single render leaves the shared,
        process-wide cache warm with REAL data for up to 5s, silently
        defeating a LATER test's CONFIG_DATABASE_PATH monkey-patch
        (confirmed: this is exactly what broke
        tests/test_scada_floor_plan_data.py's TestSnapshotBuilders when
        run after this one, in the same process, before this fix) -
        explicitly cleared afterward so this smoke test can never leak
        state into any other test file regardless of run order."""
        with patch("ui.scada_snapshot_writer.start_snapshot_writer", return_value=True):
            at = AppTest.from_file(SCADA_PAGE, default_timeout=60)
            at.run()
        import ui.scada_floor_plan_data as sfd
        sfd._load_equipment.clear()
        self.assertFalse(bool(at.exception))


if __name__ == "__main__":
    unittest.main()
