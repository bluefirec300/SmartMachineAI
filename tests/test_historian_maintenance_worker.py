import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.historian_maintenance_worker import (
    _is_due,
    _prune_old_backups,
    _read_last_run,
    _write_last_run,
    read_last_backup_summary,
    run_backup_if_due,
    run_historian_archive_if_due,
    run_retention_if_due,
)
from database.backup import BACKUP_SKIPPED_INSUFFICIENT_DISK_SPACE, backup_sqlite_database, check_backup_preflight
from database.database import DatabaseManager
from database.historian_archive import archive_dir_for, archive_path_for_month

"""
Phase 18.1a - historian retention + backup worker tests. Covers the
pure, standalone functions the worker's run_forever() loop calls -
never the infinite loop itself, matching this project's established
convention for testing worker logic (see app/energy_kpi_worker.py's
update_maximum_demand()/finalize_daily_summaries(), tested the same
way).
"""


class TestMarkerFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.marker = self.tmpdir / "marker.txt"

    def test_read_last_run_missing_file_returns_none(self):
        self.assertIsNone(_read_last_run(self.marker))

    def test_write_then_read_round_trips(self):
        when = datetime(2026, 8, 19, 10, 30, 0)
        _write_last_run(self.marker, when)
        self.assertEqual(_read_last_run(self.marker), when)

    def test_read_malformed_marker_returns_none(self):
        self.marker.write_text("not a timestamp", encoding="utf-8")
        self.assertIsNone(_read_last_run(self.marker))

    def test_is_due_true_when_never_run(self):
        self.assertTrue(_is_due(None, 24, datetime.now()))

    def test_is_due_false_when_interval_not_elapsed(self):
        now = datetime(2026, 8, 19, 12, 0, 0)
        last_run = now - timedelta(hours=1)
        self.assertFalse(_is_due(last_run, 24, now))

    def test_is_due_true_when_interval_elapsed(self):
        now = datetime(2026, 8, 19, 12, 0, 0)
        last_run = now - timedelta(hours=25)
        self.assertTrue(_is_due(last_run, 24, now))


class TestRunRetentionIfDue(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.historian = DatabaseManager(db_path=self.tmpdir / "machine.db")
        self.marker = self.tmpdir / "retention_marker.txt"
        self.now = datetime(2026, 8, 19, 12, 0, 0)

    def test_deletes_only_rows_older_than_retention_window(self):
        self.historian.save_tag("T1", "addr", 1.0, timestamp=self.now - timedelta(days=100))
        self.historian.save_tag("T1", "addr", 2.0, timestamp=self.now - timedelta(days=5))

        summary = run_retention_if_due(self.historian, retention_days=90, interval_hours=24, marker_path=self.marker, now=self.now)

        self.assertIsNotNone(summary)
        self.assertEqual(summary["deleted_rows"], 1)

        with self.historian.get_connection() as connection:
            remaining = connection.execute("SELECT value FROM plc_data").fetchall()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["value"], 2.0)

    def test_never_touches_events_or_other_tables(self):
        # plc_text_data is the STRING-tag snapshot table - must never be
        # affected by historian retention (it isn't a growing time
        # series, and cleanup() never references it).
        self.historian.save_text_tag("AlarmCode", "addr", "E01", timestamp=self.now - timedelta(days=200))
        run_retention_if_due(self.historian, retention_days=90, interval_hours=24, marker_path=self.marker, now=self.now)

        with self.historian.get_connection() as connection:
            text_rows = connection.execute("SELECT * FROM plc_text_data").fetchall()
        self.assertEqual(len(text_rows), 1)

    def test_not_due_yet_returns_none_and_does_not_delete(self):
        self.historian.save_tag("T1", "addr", 1.0, timestamp=self.now - timedelta(days=100))
        _write_last_run(self.marker, self.now - timedelta(hours=1))

        summary = run_retention_if_due(self.historian, retention_days=90, interval_hours=24, marker_path=self.marker, now=self.now)

        self.assertIsNone(summary)
        with self.historian.get_connection() as connection:
            remaining = connection.execute("SELECT COUNT(*) AS c FROM plc_data").fetchone()
        self.assertEqual(remaining["c"], 1)

    def test_second_call_immediately_after_is_a_no_op(self):
        self.historian.save_tag("T1", "addr", 1.0, timestamp=self.now - timedelta(days=100))
        first = run_retention_if_due(self.historian, retention_days=90, interval_hours=24, marker_path=self.marker, now=self.now)
        second = run_retention_if_due(self.historian, retention_days=90, interval_hours=24, marker_path=self.marker, now=self.now)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_marker_updated_after_run(self):
        run_retention_if_due(self.historian, retention_days=90, interval_hours=24, marker_path=self.marker, now=self.now)
        self.assertEqual(_read_last_run(self.marker), self.now)


class TestRunHistorianArchiveIfDue(unittest.TestCase):
    """Phase V1.2 - the archive-then-delete function run_forever()
    actually calls now (see the module's own docstring for why
    TestRunRetentionIfDue's subject above is kept but no longer used
    in production)."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.historian = DatabaseManager(db_path=self.tmpdir / "machine.db")
        self.archive_dir = archive_dir_for(self.historian.db_path)
        self.marker = self.tmpdir / "archive_marker.txt"
        self.now = datetime(2026, 8, 21, 12, 0, 0)

    def test_archives_and_removes_a_fully_elapsed_month_from_live(self):
        self.historian.save_tag("T1", "addr", 1.0, timestamp=datetime(2026, 3, 10))
        self.historian.save_tag("T1", "addr", 2.0, timestamp=datetime(2026, 8, 15))  # recent, stays live

        summary = run_historian_archive_if_due(
            self.historian, retention_days=90, interval_hours=24, minimum_free_reserve_bytes=1024,
            marker_path=self.marker, now=self.now,
        )

        self.assertIsNotNone(summary)
        self.assertEqual(summary["months_processed"], 1)
        self.assertTrue(summary["months"][0]["verified"])
        self.assertTrue(archive_path_for_month(self.archive_dir, 2026, 3).exists())

        with self.historian.get_connection() as connection:
            remaining = connection.execute("SELECT COUNT(*) AS c FROM plc_data").fetchone()["c"]
        self.assertEqual(remaining, 1)  # only the recent row survives in the live table

    def test_reported_as_ran_even_when_nothing_is_eligible_yet(self):
        self.historian.save_tag("T1", "addr", 1.0, timestamp=self.now)  # entirely recent
        summary = run_historian_archive_if_due(
            self.historian, retention_days=90, interval_hours=24, minimum_free_reserve_bytes=1024,
            marker_path=self.marker, now=self.now,
        )
        self.assertIsNotNone(summary)
        self.assertEqual(summary["months_processed"], 0)

    def test_not_due_yet_returns_none_and_touches_nothing(self):
        self.historian.save_tag("T1", "addr", 1.0, timestamp=datetime(2026, 3, 10))
        _write_last_run(self.marker, self.now - timedelta(hours=1))

        summary = run_historian_archive_if_due(
            self.historian, retention_days=90, interval_hours=24, minimum_free_reserve_bytes=1024,
            marker_path=self.marker, now=self.now,
        )
        self.assertIsNone(summary)
        self.assertFalse(archive_path_for_month(self.archive_dir, 2026, 3).exists())

    def test_marker_updated_after_run(self):
        run_historian_archive_if_due(
            self.historian, retention_days=90, interval_hours=24, minimum_free_reserve_bytes=1024,
            marker_path=self.marker, now=self.now,
        )
        self.assertEqual(_read_last_run(self.marker), self.now)

    def test_insufficient_disk_space_never_deletes_anything(self):
        self.historian.save_tag("T1", "addr", 1.0, timestamp=datetime(2026, 3, 10))
        run_historian_archive_if_due(
            self.historian, retention_days=90, interval_hours=24, minimum_free_reserve_bytes=10**18,
            marker_path=self.marker, now=self.now,
        )
        with self.historian.get_connection() as connection:
            remaining = connection.execute("SELECT COUNT(*) AS c FROM plc_data").fetchone()["c"]
        self.assertEqual(remaining, 1)  # nothing deleted - disk-space skip
        self.assertFalse(archive_path_for_month(self.archive_dir, 2026, 3).exists())


class TestBackupSqliteDatabase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_backup_produces_a_queryable_copy_with_same_data(self):
        source_path = self.tmpdir / "source.db"
        connection = sqlite3.connect(source_path)
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO t (value) VALUES ('hello')")
        connection.commit()
        connection.close()

        destination_path = self.tmpdir / "nested" / "backup.db"
        result_path = backup_sqlite_database(source_path, destination_path)

        self.assertEqual(result_path, destination_path.resolve())
        self.assertTrue(destination_path.exists())

        verify = sqlite3.connect(destination_path)
        try:
            row = verify.execute("SELECT value FROM t").fetchone()
        finally:
            verify.close()
        self.assertEqual(row[0], "hello")

    def test_database_manager_backup_delegates_correctly(self):
        historian = DatabaseManager(db_path=self.tmpdir / "machine.db")
        historian.save_tag("T1", "addr", 42.0)

        destination_path = self.tmpdir / "machine_backup.db"
        historian.backup(destination_path)

        verify = sqlite3.connect(destination_path)
        try:
            row = verify.execute("SELECT value FROM plc_data").fetchone()
        finally:
            verify.close()
        self.assertEqual(row[0], 42.0)

    def test_config_db_backup_does_not_gain_historian_tables(self):
        # backup_sqlite_database() must be usable directly against
        # config.db (a different schema) WITHOUT going through
        # DatabaseManager, which would wrongly create plc_data/
        # plc_text_data in it.
        source_path = self.tmpdir / "config.db"
        connection = sqlite3.connect(source_path)
        connection.execute("CREATE TABLE equipment (id INTEGER PRIMARY KEY, name TEXT)")
        connection.commit()
        connection.close()

        destination_path = self.tmpdir / "config_backup.db"
        backup_sqlite_database(source_path, destination_path)

        verify = sqlite3.connect(destination_path)
        try:
            tables = {row[0] for row in verify.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            verify.close()
        self.assertIn("equipment", tables)
        self.assertNotIn("plc_data", tables)
        self.assertNotIn("plc_text_data", tables)


class TestCheckBackupPreflight(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.source_db = self.tmpdir / "source.db"
        self.source_db.write_bytes(b"x" * 1000)  # a small, known-size fixture - never the real historian
        self.destination_dir = self.tmpdir / "backups"

    def test_passes_with_a_trivially_small_reserve(self):
        result = check_backup_preflight(self.source_db, self.destination_dir, minimum_free_reserve_bytes=1024)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])

    def test_fails_with_an_impossible_reserve(self):
        result = check_backup_preflight(self.source_db, self.destination_dir, minimum_free_reserve_bytes=10 ** 18)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], BACKUP_SKIPPED_INSUFFICIENT_DISK_SPACE)

    def test_required_bytes_is_conservatively_2x_database_size_plus_reserve(self):
        result = check_backup_preflight(self.source_db, self.destination_dir, minimum_free_reserve_bytes=500)
        self.assertEqual(result["database_size_bytes"], 1000)
        self.assertEqual(result["required_free_bytes"], 1000 * 2 + 500)

    def test_naive_free_space_greater_than_database_size_alone_is_not_sufficient(self):
        # A destination with free space strictly between 1x and 2x the
        # database size (plus reserve) must still fail - proving this
        # is NOT the naive `free_space > database_size` check the
        # instructions explicitly warned against.
        result = check_backup_preflight(self.source_db, self.destination_dir, minimum_free_reserve_bytes=0)
        naive_pass_only = result["free_bytes"] > result["database_size_bytes"]
        conservative_pass = result["free_bytes"] >= result["required_free_bytes"]
        # On this real filesystem both will typically be true (plenty
        # of free space) - the meaningful assertion is that the
        # function's own decision uses the conservative 2x formula,
        # not the naive one, regardless of which happens to pass here.
        self.assertEqual(result["ok"], conservative_pass)
        self.assertEqual(result["required_free_bytes"], result["database_size_bytes"] * 2)

    def test_missing_source_database_raises_rather_than_silently_passing(self):
        with self.assertRaises(FileNotFoundError):
            check_backup_preflight(self.tmpdir / "does_not_exist.db", self.destination_dir, minimum_free_reserve_bytes=0)

    def test_never_creates_the_destination_directory(self):
        check_backup_preflight(self.source_db, self.destination_dir, minimum_free_reserve_bytes=1024)
        self.assertFalse(self.destination_dir.exists())

    def test_works_before_destination_directory_exists(self):
        # Preflight must be safely callable even the very first time,
        # before any backup (and therefore the destination directory)
        # has ever been created.
        self.assertFalse(self.destination_dir.exists())
        result = check_backup_preflight(self.source_db, self.destination_dir, minimum_free_reserve_bytes=1024)
        self.assertIn("free_bytes", result)


class TestPruneOldBackups(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_keeps_only_the_most_recent_n(self):
        for i in range(5):
            (self.tmpdir / f"config_backup_2026010{i}_000000.db").write_text("x")

        deleted = _prune_old_backups(self.tmpdir, "config_backup_", keep_count=2)

        self.assertEqual(len(deleted), 3)
        remaining = sorted(p.name for p in self.tmpdir.glob("config_backup_*.db"))
        self.assertEqual(remaining, ["config_backup_20260103_000000.db", "config_backup_20260104_000000.db"])

    def test_fewer_files_than_keep_count_deletes_nothing(self):
        (self.tmpdir / "config_backup_20260101_000000.db").write_text("x")
        deleted = _prune_old_backups(self.tmpdir, "config_backup_", keep_count=5)
        self.assertEqual(deleted, [])

    def test_does_not_touch_files_with_a_different_prefix(self):
        (self.tmpdir / "config_backup_20260101_000000.db").write_text("x")
        (self.tmpdir / "machine_data_backup_20260101_000000.db").write_text("x")

        _prune_old_backups(self.tmpdir, "config_backup_", keep_count=1)
        self.assertTrue((self.tmpdir / "machine_data_backup_20260101_000000.db").exists())

    def test_zero_keep_count_raises(self):
        with self.assertRaises(ValueError):
            _prune_old_backups(self.tmpdir, "config_backup_", keep_count=0)


class TestRunBackupIfDue(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.config_db = self.tmpdir / "config.db"
        sqlite3.connect(self.config_db).close()
        self.machine_db = self.tmpdir / "machine.db"
        DatabaseManager(db_path=self.machine_db)
        self.destination_dir = self.tmpdir / "backups"
        self.marker = self.tmpdir / "backup_marker.txt"
        self.summary_path = self.tmpdir / "backup_summary.json"
        self.now = datetime(2026, 8, 19, 12, 0, 0)

    def test_backs_up_both_databases(self):
        summary = run_backup_if_due(
            self.config_db, self.machine_db, self.destination_dir,
            interval_hours=24, retention_count=7, minimum_free_reserve_bytes=1024,
            marker_path=self.marker, summary_path=self.summary_path, now=self.now,
        )

        self.assertIsNotNone(summary)
        self.assertEqual(summary["databases"]["config"]["status"], "ok")
        self.assertEqual(summary["databases"]["machine_data"]["status"], "ok")
        self.assertEqual(len(list(self.destination_dir.glob("*.db"))), 2)

    def test_not_due_yet_returns_none(self):
        _write_last_run(self.marker, self.now - timedelta(hours=1))
        summary = run_backup_if_due(
            self.config_db, self.machine_db, self.destination_dir,
            interval_hours=24, retention_count=7, minimum_free_reserve_bytes=1024,
            marker_path=self.marker, summary_path=self.summary_path, now=self.now,
        )
        self.assertIsNone(summary)

    def test_a_failure_backing_up_one_database_does_not_prevent_the_other(self):
        # A directory (not a file) at the source path makes sqlite3.connect()
        # genuinely fail ("unable to open database file") - unlike a
        # merely-nonexistent path, which SQLite silently creates.
        unopenable_source = self.tmpdir / "not_a_database_dir"
        unopenable_source.mkdir()

        summary = run_backup_if_due(
            unopenable_source, self.machine_db, self.destination_dir,
            interval_hours=24, retention_count=7, minimum_free_reserve_bytes=1024,
            marker_path=self.marker, summary_path=self.summary_path, now=self.now,
        )

        self.assertIsNotNone(summary)
        self.assertEqual(summary["databases"]["config"]["status"], "failed")
        self.assertEqual(summary["databases"]["machine_data"]["status"], "ok")
        # marker still updated - a failed backup attempt should not be retried every tick
        self.assertEqual(_read_last_run(self.marker), self.now)

    def test_insufficient_disk_space_skips_without_deleting_or_attempting_backup(self):
        summary = run_backup_if_due(
            self.config_db, self.machine_db, self.destination_dir,
            interval_hours=24, retention_count=7, minimum_free_reserve_bytes=10 ** 18,  # impossible to satisfy
            marker_path=self.marker, summary_path=self.summary_path, now=self.now,
        )

        self.assertIsNotNone(summary)
        self.assertEqual(summary["databases"]["config"]["status"], "skipped")
        self.assertEqual(summary["databases"]["config"]["reason"], "BACKUP_SKIPPED_INSUFFICIENT_DISK_SPACE")
        self.assertEqual(summary["databases"]["machine_data"]["status"], "skipped")
        # no backup file was created for either database
        self.assertEqual(list(self.destination_dir.glob("*.db")), [])
        # source databases are completely untouched
        self.assertTrue(self.config_db.exists())
        self.assertTrue(self.machine_db.exists())

    def test_insufficient_disk_space_does_not_prune_an_existing_valid_backup(self):
        # Seed one real, already-successful backup first.
        existing_backup = self.destination_dir / "config_backup_20200101_000000.db"
        self.destination_dir.mkdir(parents=True, exist_ok=True)
        backup_sqlite_database(self.config_db, existing_backup)
        self.assertTrue(existing_backup.exists())

        run_backup_if_due(
            self.config_db, self.machine_db, self.destination_dir,
            interval_hours=24, retention_count=1, minimum_free_reserve_bytes=10 ** 18,
            marker_path=self.marker, summary_path=self.summary_path, now=self.now,
        )

        # the pre-existing good backup must still be there - a skipped
        # (insufficient-space) attempt must never prune anything.
        self.assertTrue(existing_backup.exists())

    def test_successful_backup_writes_a_readable_json_summary(self):
        run_backup_if_due(
            self.config_db, self.machine_db, self.destination_dir,
            interval_hours=24, retention_count=7, minimum_free_reserve_bytes=1024,
            marker_path=self.marker, summary_path=self.summary_path, now=self.now,
        )

        summary = read_last_backup_summary(self.summary_path)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["databases"]["config"]["status"], "ok")
        self.assertEqual(summary["databases"]["machine_data"]["status"], "ok")

    def test_missing_summary_file_returns_none_not_a_fabricated_summary(self):
        self.assertIsNone(read_last_backup_summary(self.tmpdir / "never_written.json"))


if __name__ == "__main__":
    unittest.main()
