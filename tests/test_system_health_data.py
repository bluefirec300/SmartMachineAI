import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from ui import system_health_data as shd

"""
Phase V2.1 - System & Worker Health. Tests the data-access layer in
isolation (mocked subprocess calls, temp SQLite files - never the live
system) plus a live AppTest smoke check of the real page against
whatever this host's actual systemd state is.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_HEALTH_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "22_System_Health.py")


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class ParseSystemctlTimestampTests(unittest.TestCase):
    def test_parses_real_systemctl_format(self):
        result = shd._parse_systemctl_timestamp("Fri 2026-08-21 09:16:47 +08")
        self.assertEqual(result, datetime(2026, 8, 21, 9, 16, 47))

    def test_handles_not_available_sentinel(self):
        self.assertIsNone(shd._parse_systemctl_timestamp("n/a"))

    def test_handles_zero_sentinel(self):
        self.assertIsNone(shd._parse_systemctl_timestamp("0"))

    def test_handles_empty_string(self):
        self.assertIsNone(shd._parse_systemctl_timestamp(""))

    def test_handles_malformed_input_without_raising(self):
        self.assertIsNone(shd._parse_systemctl_timestamp("garbage"))


class GetSystemdStateTests(unittest.TestCase):
    def test_running_service_reports_running_true(self):
        stdout = (
            "ActiveState=active\nSubState=running\n"
            "ActiveEnterTimestamp=Fri 2026-08-21 09:16:47 +08\nNRestarts=0\nLoadState=loaded\n"
        )
        with mock.patch.object(shd, "_run", return_value=_completed(stdout)):
            state = shd.get_systemd_state("plc_logger.service")

        self.assertTrue(state["available"])
        self.assertTrue(state["found"])
        self.assertTrue(state["running"])
        self.assertEqual(state["restart_count"], 0)
        self.assertEqual(state["since"], datetime(2026, 8, 21, 9, 16, 47))

    def test_stopped_service_reports_running_false(self):
        stdout = "ActiveState=inactive\nSubState=dead\nActiveEnterTimestamp=n/a\nNRestarts=2\nLoadState=loaded\n"
        with mock.patch.object(shd, "_run", return_value=_completed(stdout)):
            state = shd.get_systemd_state("some_worker.service")

        self.assertFalse(state["running"])
        self.assertEqual(state["restart_count"], 2)

    def test_not_found_unit_is_distinguished_from_stopped(self):
        stdout = "ActiveState=inactive\nSubState=dead\nActiveEnterTimestamp=n/a\nNRestarts=0\nLoadState=not-found\n"
        with mock.patch.object(shd, "_run", return_value=_completed(stdout)):
            state = shd.get_systemd_state("nonexistent.service")

        self.assertTrue(state["available"])
        self.assertFalse(state["found"])
        self.assertNotIn("running", state)

    def test_systemctl_unreachable_reports_available_false_never_a_guess(self):
        with mock.patch.object(shd, "_run", return_value=None):
            state = shd.get_systemd_state("plc_logger.service")

        self.assertEqual(state, {"available": False})

    def test_nonzero_returncode_treated_as_unavailable(self):
        with mock.patch.object(shd, "_run", return_value=_completed("", returncode=1)):
            state = shd.get_systemd_state("plc_logger.service")

        self.assertFalse(state["available"])


class IsErrorLineTests(unittest.TestCase):
    """The false-positive fix: a routine cycle-summary line reporting
    its own zero error count must never be flagged."""

    def test_cycle_summary_with_zero_errors_is_not_flagged(self):
        line = "cycle: {'eligible': 34, 'evaluated': 34, 'errors': 0, 'duration_seconds': 5.1}"
        self.assertFalse(shd._is_error_line(line))

    def test_cycle_summary_with_zero_failed_is_not_flagged(self):
        line = "cycle: 11.24s, {'evaluated': 1, 'persisted': 1, 'failed': 0, 'eligible_interventions': 1}"
        self.assertFalse(shd._is_error_line(line))

    def test_cycle_summary_with_nonzero_errors_is_flagged(self):
        line = "cycle: {'eligible': 34, 'evaluated': 34, 'errors': 3, 'duration_seconds': 5.1}"
        self.assertTrue(shd._is_error_line(line))

    def test_explicit_failure_message_is_flagged(self):
        line = "anomaly_worker cycle failed: connection refused"
        self.assertTrue(shd._is_error_line(line))

    def test_bare_traceback_is_flagged(self):
        line = "Traceback (most recent call last):"
        self.assertTrue(shd._is_error_line(line))

    def test_ordinary_line_is_not_flagged(self):
        line = "Historian maintenance worker started. Config DB: ..."
        self.assertFalse(shd._is_error_line(line))


class GetRecentJournalErrorsTests(unittest.TestCase):
    def test_counts_only_genuine_errors_among_healthy_cycle_lines(self):
        stdout = "\n".join(
            [
                "2026-08-21T13:03:37 cycle: {'errors': 0, 'eligible': 34}",
                "2026-08-21T13:04:11 cycle: {'errors': 0, 'eligible': 34}",
                "2026-08-21T13:05:00 equipment_health_worker cycle failed: timeout",
            ]
        )
        with mock.patch.object(shd, "_run", return_value=_completed(stdout)):
            result = shd.get_recent_journal_errors("equipment_health_worker.service")

        self.assertTrue(result["available"])
        self.assertEqual(result["count"], 1)
        self.assertIn("cycle failed", result["sample"])

    def test_all_healthy_lines_report_zero(self):
        stdout = "cycle: {'errors': 0}\ncycle: {'errors': 0}\n"
        with mock.patch.object(shd, "_run", return_value=_completed(stdout)):
            result = shd.get_recent_journal_errors("data_health_history_worker.service")

        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["sample"])

    def test_journalctl_unreachable_reports_unavailable_not_zero(self):
        with mock.patch.object(shd, "_run", return_value=None):
            result = shd.get_recent_journal_errors("plc_logger.service")

        self.assertFalse(result["available"])
        self.assertIsNone(result["count"])


class QueryMaxTimestampTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db_path = self.tmpdir / "config.db"

        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE anomalies (id INTEGER PRIMARY KEY, updated_at TEXT)")
        connection.execute("INSERT INTO anomalies (updated_at) VALUES ('2026-08-21 12:00:00')")
        connection.execute("INSERT INTO anomalies (updated_at) VALUES ('2026-08-21 13:30:00')")
        connection.commit()
        connection.close()

    def test_returns_the_max_timestamp(self):
        with mock.patch.object(shd, "CONFIG_DATABASE_PATH", self.db_path):
            result = shd._query_max_timestamp("config", "anomalies", "updated_at")

        self.assertEqual(result, datetime(2026, 8, 21, 13, 30, 0))

    def test_empty_table_returns_none_not_a_crash(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("DELETE FROM anomalies")
        connection.commit()
        connection.close()

        with mock.patch.object(shd, "CONFIG_DATABASE_PATH", self.db_path):
            result = shd._query_max_timestamp("config", "anomalies", "updated_at")

        self.assertIsNone(result)

    def test_missing_table_returns_none_not_a_crash(self):
        with mock.patch.object(shd, "CONFIG_DATABASE_PATH", self.db_path):
            result = shd._query_max_timestamp("config", "table_that_does_not_exist", "updated_at")

        self.assertIsNone(result)

    def test_unreachable_database_returns_none_not_a_crash(self):
        with mock.patch.object(shd, "CONFIG_DATABASE_PATH", self.tmpdir / "does_not_exist.db"):
            result = shd._query_max_timestamp("config", "anomalies", "updated_at")

        self.assertIsNone(result)


class MarkerFileTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_reads_a_real_marker_file(self):
        marker = self.tmpdir / "marker.txt"
        marker.write_text("2026-08-21 10:25:47", encoding="utf-8")
        self.assertEqual(shd._read_marker(marker), datetime(2026, 8, 21, 10, 25, 47))

    def test_missing_marker_file_returns_none(self):
        self.assertIsNone(shd._read_marker(self.tmpdir / "missing.txt"))

    def test_malformed_marker_file_returns_none_not_a_crash(self):
        marker = self.tmpdir / "marker.txt"
        marker.write_text("not a timestamp", encoding="utf-8")
        self.assertIsNone(shd._read_marker(marker))


class FormatTimeAgoTests(unittest.TestCase):
    def test_none_timestamp_is_unavailable(self):
        self.assertEqual(shd.format_time_ago(None), "Unavailable")

    def test_seconds_ago(self):
        now = datetime(2026, 8, 21, 13, 0, 30)
        self.assertEqual(shd.format_time_ago(datetime(2026, 8, 21, 13, 0, 0), now), "30 sec ago")

    def test_minutes_ago(self):
        now = datetime(2026, 8, 21, 13, 5, 0)
        self.assertEqual(shd.format_time_ago(datetime(2026, 8, 21, 13, 0, 0), now), "5 min ago")

    def test_hours_ago(self):
        now = datetime(2026, 8, 21, 15, 0, 0)
        self.assertEqual(shd.format_time_ago(datetime(2026, 8, 21, 13, 0, 0), now), "2 hr ago")

    def test_days_ago(self):
        now = datetime(2026, 8, 23, 13, 0, 0)
        self.assertEqual(shd.format_time_ago(datetime(2026, 8, 21, 13, 0, 0), now), "2 day(s) ago")


class GetAllServiceHealthTests(unittest.TestCase):
    def test_returns_one_entry_per_configured_service(self):
        healthy_stdout = (
            "ActiveState=active\nSubState=running\n"
            "ActiveEnterTimestamp=Fri 2026-08-21 09:16:47 +08\nNRestarts=0\nLoadState=loaded\n"
        )
        with mock.patch.object(shd, "_run", return_value=_completed(healthy_stdout)), \
             mock.patch.object(shd, "_query_max_timestamp", return_value=None), \
             mock.patch.object(shd, "_read_marker", return_value=None), \
             mock.patch.object(shd, "read_last_backup_summary", return_value=None):
            results = shd.get_all_service_health()

        self.assertEqual(len(results), len(shd.SERVICES))
        self.assertEqual({r.unit for r in results}, {s["unit"] for s in shd.SERVICES})

    def test_never_raises_when_every_backing_call_fails(self):
        with mock.patch.object(shd, "_run", return_value=None):
            results = shd.get_all_service_health()

        self.assertEqual(len(results), len(shd.SERVICES))
        for r in results:
            self.assertFalse(r.systemctl_available)
            self.assertIsNone(r.running)


class SystemHealthPageRendersTests(unittest.TestCase):
    def test_page_loads_without_exception(self):
        at = AppTest.from_file(SYSTEM_HEALTH_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))

    def test_page_title_distinguishes_from_equipment_and_data_health(self):
        at = AppTest.from_file(SYSTEM_HEALTH_PAGE, default_timeout=30)
        at.run()
        captions = " ".join(c.value for c in at.caption)
        self.assertIn("Equipment Health", captions)
        self.assertIn("Data Health", captions)

    def test_page_renders_both_service_tables(self):
        at = AppTest.from_file(SYSTEM_HEALTH_PAGE, default_timeout=30)
        at.run()
        self.assertEqual(len(at.dataframe), 2)
        core_df = at.dataframe[0].value
        worker_df = at.dataframe[1].value
        self.assertEqual(len(core_df), 3)
        self.assertEqual(len(worker_df), 11)


if __name__ == "__main__":
    unittest.main()
