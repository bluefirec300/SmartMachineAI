import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

from ai.plc_tag_read_status import PlcTagReadStatus
from app.plc_logger import detect_tag_read_changes

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLC_CONNECTIVITY_PAGE = str(
    PROJECT_ROOT / "ui" / "pages" / "10_PLC_Connectivity.py"
)


class PlcTagReadStatusTests(unittest.TestCase):
    """
    Phase V2.8 - per-tag PLC read health tracking, isolated from Data
    Health. Uses a temp DB, same pattern as test_event_store.py.
    """

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temp_directory.name) / "test_machine_data.db"
        )
        self.status = PlcTagReadStatus(database_path=self.database_path)

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_no_rows_before_any_failure(self):
        self.assertEqual(self.status.get_all(), [])

    def test_a_new_failure_creates_a_row(self):
        self.status.record_cycle(
            {"Pressure"}, set(), datetime(2026, 8, 22, 9, 0, 0)
        )

        row = self.status.get("Pressure")

        self.assertIsNotNone(row)
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertIsNone(row["last_success_at"])
        self.assertEqual(row["last_failure_at"], "2026-08-22 09:00:00")

    def test_repeated_failures_increment_the_streak(self):
        self.status.record_cycle(
            {"Pressure"}, set(), datetime(2026, 8, 22, 9, 0, 0)
        )
        self.status.record_cycle(
            {"Pressure"}, set(), datetime(2026, 8, 22, 9, 0, 2)
        )
        self.status.record_cycle(
            {"Pressure"}, set(), datetime(2026, 8, 22, 9, 0, 4)
        )

        row = self.status.get("Pressure")

        self.assertEqual(row["consecutive_failures"], 3)
        self.assertEqual(row["last_failure_at"], "2026-08-22 09:00:04")

    def test_recovery_resets_the_streak_but_keeps_the_row(self):
        self.status.record_cycle(
            {"Pressure"}, set(), datetime(2026, 8, 22, 9, 0, 0)
        )
        self.status.record_cycle(
            set(), {"Pressure"}, datetime(2026, 8, 22, 9, 0, 2)
        )

        row = self.status.get("Pressure")

        self.assertIsNotNone(row)
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertEqual(row["last_success_at"], "2026-08-22 09:00:02")
        # The prior failure timestamp is not erased on recovery - the
        # row is history, not just current state.
        self.assertEqual(row["last_failure_at"], "2026-08-22 09:00:00")

    def test_a_tag_that_only_ever_succeeds_gets_no_row(self):
        self.status.record_cycle(set(), set(), datetime(2026, 8, 22, 9, 0, 0))

        self.assertEqual(self.status.get_all(), [])
        self.assertIsNone(self.status.get("Pressure"))

    def test_get_all_orders_worst_streak_first(self):
        self.status.record_cycle(
            {"TagA"}, set(), datetime(2026, 8, 22, 9, 0, 0)
        )
        self.status.record_cycle(
            {"TagB"}, set(), datetime(2026, 8, 22, 9, 0, 2)
        )
        self.status.record_cycle(
            {"TagB"}, set(), datetime(2026, 8, 22, 9, 0, 4)
        )

        rows = self.status.get_all()

        self.assertEqual([row["tag_name"] for row in rows], ["TagB", "TagA"])

    def test_multiple_tags_in_one_cycle_are_independent(self):
        self.status.record_cycle(
            {"TagA", "TagB"}, set(), datetime(2026, 8, 22, 9, 0, 0)
        )

        self.assertEqual(self.status.get("TagA")["consecutive_failures"], 1)
        self.assertEqual(self.status.get("TagB")["consecutive_failures"], 1)

    def test_a_healthy_second_instance_reads_the_same_rows_back(self):
        self.status.record_cycle(
            {"Pressure"}, set(), datetime(2026, 8, 22, 9, 0, 0)
        )

        second_instance = PlcTagReadStatus(database_path=self.database_path)

        self.assertEqual(len(second_instance.get_all()), 1)


class DetectTagReadChangesTests(unittest.TestCase):
    """
    app/plc_logger.py's pure, database-free failure-detection helper -
    Phase V2.8's extraction so this logic is testable without a real
    driver or database.
    """

    def test_all_tags_succeeding_reports_nothing(self):
        tags = [{"name": "Pressure"}, {"name": "Temperature"}]
        values = [{"name": "Pressure"}, {"name": "Temperature"}]

        failed, recovered = detect_tag_read_changes(tags, values, {})

        self.assertEqual(failed, set())
        self.assertEqual(recovered, set())

    def test_a_missing_tag_is_reported_as_failed(self):
        tags = [{"name": "Pressure"}, {"name": "Temperature"}]
        values = [{"name": "Pressure"}]

        streaks: dict[str, int] = {}
        failed, recovered = detect_tag_read_changes(tags, values, streaks)

        self.assertEqual(failed, {"Temperature"})
        self.assertEqual(recovered, set())
        self.assertEqual(streaks["Temperature"], 1)

    def test_a_tag_recovering_from_a_prior_failure_is_reported(self):
        tags = [{"name": "Pressure"}]
        values = [{"name": "Pressure"}]

        streaks = {"Pressure": 3}
        failed, recovered = detect_tag_read_changes(tags, values, streaks)

        self.assertEqual(failed, set())
        self.assertEqual(recovered, {"Pressure"})
        self.assertEqual(streaks["Pressure"], 0)

    def test_a_tag_that_has_always_succeeded_is_not_reported_as_recovered(self):
        tags = [{"name": "Pressure"}]
        values = [{"name": "Pressure"}]

        streaks: dict[str, int] = {}
        failed, recovered = detect_tag_read_changes(tags, values, streaks)

        self.assertEqual(recovered, set())

    def test_streak_keeps_incrementing_across_repeated_calls(self):
        tags = [{"name": "Pressure"}]
        values: list[dict] = []

        streaks: dict[str, int] = {}
        detect_tag_read_changes(tags, values, streaks)
        detect_tag_read_changes(tags, values, streaks)
        failed, _ = detect_tag_read_changes(tags, values, streaks)

        self.assertEqual(failed, {"Pressure"})
        self.assertEqual(streaks["Pressure"], 3)


class PlcConnectivityPageSmokeTests(unittest.TestCase):
    """
    Unauthenticated smoke check only - this is a write-capable admin
    page (Connections/Tag Mapping forms), so per this project's
    established precedent a full authenticated session is avoided
    here. This just confirms the new Per-Tag Read Health section's
    import (ai.plc_tag_read_status) doesn't break the page's own
    require_role("admin") gate, which blocks before any DB access.
    """

    def test_page_blocks_unauthenticated_access_without_raising(self):
        at = AppTest.from_file(PLC_CONNECTIVITY_PAGE, default_timeout=30)
        at.run()

        self.assertFalse(bool(at.exception))
        self.assertTrue(
            any("permission" in e.value for e in at.error)
        )


if __name__ == "__main__":
    unittest.main()
