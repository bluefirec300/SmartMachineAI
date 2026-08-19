import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database.backup import backup_sqlite_database
from database.backup_status import get_backup_status

"""
Backup readiness follow-up - tests use only small, isolated SQLite
fixtures created in a temp directory. Never touches the real,
multi-GB database/simulation/machine_data.db - no test in this file
creates or copies anything larger than a few KB.
"""


class TestGetBackupStatus(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.config_db = self.tmpdir / "config.db"
        sqlite3.connect(self.config_db).close()
        self.machine_db = self.tmpdir / "machine.db"
        sqlite3.connect(self.machine_db).close()
        self.destination_dir = self.tmpdir / "backups"
        self.marker_path = self.tmpdir / "marker.txt"
        self.summary_path = self.tmpdir / "summary.json"

    def _status(self, **overrides):
        kwargs = dict(
            config_db_path=self.config_db, machine_db_path=self.machine_db, destination_dir=self.destination_dir,
            backup_enabled=False, retention_count=7, minimum_free_reserve_bytes=5_000_000_000,
            marker_path=self.marker_path, summary_path=self.summary_path,
        )
        kwargs.update(overrides)
        return get_backup_status(**kwargs)

    def test_capability_is_always_ready(self):
        self.assertEqual(self._status()["capability"], "READY")

    def test_reports_configured_disabled_state(self):
        status = self._status(backup_enabled=False)
        self.assertFalse(status["automatic_backup_enabled"])

    def test_reports_configured_enabled_state(self):
        status = self._status(backup_enabled=True)
        self.assertTrue(status["automatic_backup_enabled"])

    def test_database_size_reporting(self):
        self.config_db.write_bytes(b"x" * 100)
        self.machine_db.write_bytes(b"x" * 250)
        status = self._status()
        self.assertEqual(status["config_db_size_bytes"], 100)
        self.assertEqual(status["machine_db_size_bytes"], 250)

    def test_missing_database_reports_none_size_not_zero(self):
        missing_db = self.tmpdir / "does_not_exist.db"
        status = self._status(machine_db_path=missing_db)
        self.assertIsNone(status["machine_db_size_bytes"])

    def test_no_backup_directory_yet_reports_zero_count_and_size(self):
        status = self._status()
        self.assertFalse(status["backup_directory_exists"])
        self.assertEqual(status["backup_file_count"], 0)
        self.assertEqual(status["backup_directory_size_bytes"], 0)

    def test_backup_directory_size_and_count(self):
        self.destination_dir.mkdir()
        backup_sqlite_database(self.config_db, self.destination_dir / "config_backup_1.db")
        backup_sqlite_database(self.config_db, self.destination_dir / "config_backup_2.db")
        status = self._status()
        self.assertTrue(status["backup_directory_exists"])
        self.assertEqual(status["backup_file_count"], 2)
        self.assertGreater(status["backup_directory_size_bytes"], 0)

    def test_free_disk_space_is_a_positive_real_number(self):
        status = self._status()
        self.assertGreater(status["free_disk_space_bytes"], 0)

    def test_configured_retention_passed_through(self):
        status = self._status(retention_count=3)
        self.assertEqual(status["configured_retention_count"], 3)

    def test_estimated_retention_storage_uses_current_sizes(self):
        self.config_db.write_bytes(b"x" * 1000)
        self.machine_db.write_bytes(b"x" * 4000)
        status = self._status(retention_count=5)
        self.assertEqual(status["estimated_retention_storage_bytes"], (1000 + 4000) * 5)

    def test_minimum_free_reserve_passed_through(self):
        status = self._status(minimum_free_reserve_bytes=123456)
        self.assertEqual(status["minimum_free_reserve_bytes"], 123456)

    def test_no_marker_file_reports_none_not_a_fabricated_timestamp(self):
        status = self._status()
        self.assertIsNone(status["last_backup_attempt_at"])

    def test_marker_file_present_reports_its_timestamp(self):
        self.marker_path.write_text("2026-08-19 12:00:00", encoding="utf-8")
        status = self._status()
        self.assertEqual(status["last_backup_attempt_at"], "2026-08-19 12:00:00")

    def test_no_summary_file_reports_none_not_a_fabricated_summary(self):
        status = self._status()
        self.assertIsNone(status["last_backup_summary"])

    def test_summary_file_present_is_read_through(self):
        self.summary_path.write_text(json.dumps({"ran_at": "2026-08-19 12:00:00", "databases": {"config": {"status": "ok"}}}), encoding="utf-8")
        status = self._status()
        self.assertEqual(status["last_backup_summary"]["databases"]["config"]["status"], "ok")

    def test_malformed_summary_file_reports_none_not_a_crash(self):
        self.summary_path.write_text("not valid json", encoding="utf-8")
        status = self._status()
        self.assertIsNone(status["last_backup_summary"])


if __name__ == "__main__":
    unittest.main()
