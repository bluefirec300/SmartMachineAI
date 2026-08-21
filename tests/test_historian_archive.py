import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from database.database import DatabaseManager
from database.historian_archive import (
    archive_and_purge_eligible_months,
    archive_dir_for,
    archive_path_for_month,
    create_month_archive,
    delete_month_from_live,
    months_eligible_for_archiving,
    query_archived_history,
    verify_month_archive,
)

"""
Phase V1.2 - Backup, Restore & Historian Retention. Tests run against
small, isolated, purpose-seeded temporary databases - never the real
multi-GB historian (matching this project's established convention).
The real live database currently has no month old enough to be
eligible for archiving yet (data only goes back ~4 weeks; default
retention is 90 days) - these tests seed synthetic old data instead,
which is the only way to genuinely exercise the archive-then-verify-
then-delete pipeline safely.
"""


class ArchiveTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.historian = DatabaseManager(db_path=self.temp_dir / "machine.db")
        self.archive_dir = archive_dir_for(self.historian.db_path)

    def _seed(self, tag: str, time_text: str, value: float = 1.0) -> None:
        self.historian.save_tag(tag, "addr", value, timestamp=datetime.strptime(time_text, "%Y-%m-%d %H:%M:%S"))


class TestMonthBoundsAndEligibility(unittest.TestCase):
    def test_month_bounds_december_rolls_into_next_year(self):
        from database.historian_archive import _month_bounds
        start, end = _month_bounds(2026, 12)
        self.assertEqual(start, "2026-12-01 00:00:00")
        self.assertEqual(end, "2027-01-01 00:00:00")

    def test_next_month_rolls_year(self):
        from database.historian_archive import _next_month
        self.assertEqual(_next_month(2026, 12), (2027, 1))
        self.assertEqual(_next_month(2026, 5), (2026, 6))


class TestEligibility(ArchiveTestBase):
    def test_no_data_returns_no_eligible_months(self):
        self.assertEqual(months_eligible_for_archiving(self.historian, retention_days=90), [])

    def test_only_fully_elapsed_months_are_eligible(self):
        # now=2026-08-21, retention_days=90 -> cutoff is 2026-05-23. A
        # month is only eligible if its LAST day is before the cutoff -
        # May itself straddles the cutoff (May 31 > May 23) so it is
        # correctly NOT eligible; March and April both end before it.
        now = datetime(2026, 8, 21, 12, 0, 0)
        self._seed("T1", "2026-03-10 00:00:00")  # fully before the cutoff
        self._seed("T1", "2026-04-15 00:00:00")  # also fully before
        self._seed("T1", "2026-08-15 00:00:00")  # recent - not eligible
        eligible = months_eligible_for_archiving(self.historian, retention_days=90, now=now)
        self.assertEqual(eligible, [(2026, 3), (2026, 4)])

    def test_boundary_month_straddling_the_cutoff_is_not_eligible(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        self._seed("T1", "2026-05-25 00:00:00")  # after the ~May 23 cutoff - month not fully elapsed
        eligible = months_eligible_for_archiving(self.historian, retention_days=90, now=now)
        self.assertEqual(eligible, [])

    def test_current_month_never_eligible_even_with_zero_retention(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        self._seed("T1", "2026-08-01 00:00:00")
        eligible = months_eligible_for_archiving(self.historian, retention_days=0, now=now)
        self.assertNotIn((2026, 8), eligible)

    def test_legacy_iso_t_separator_does_not_break_eligibility(self):
        # A handful of real historian rows use "T" instead of a space
        # (a known, documented, never-reverted legacy format quirk) -
        # confirm oldest-month detection is unaffected.
        with self.historian.get_connection() as connection:
            connection.execute(
                "INSERT INTO plc_data (time, tag, address, value) VALUES (?, ?, ?, ?)",
                ("2026-04-10T00:00:00", "T1", "addr", 1.0),
            )
        eligible = months_eligible_for_archiving(self.historian, retention_days=90, now=datetime(2026, 8, 21))
        self.assertIn((2026, 4), eligible)


class TestCreateAndVerify(ArchiveTestBase):
    def test_create_then_verify_matches_exactly(self):
        self._seed("T1", "2026-05-01 00:00:00", 1.0)
        self._seed("T1", "2026-05-15 12:00:00", 2.0)
        self._seed("T1", "2026-05-31 23:59:59", 3.0)
        self._seed("T1", "2026-06-01 00:00:00", 99.0)  # different month - must NOT be included

        result = create_month_archive(self.historian, self.archive_dir, 2026, 5, minimum_free_reserve_bytes=1024)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows_copied"], 3)

        archive_path = archive_path_for_month(self.archive_dir, 2026, 5)
        self.assertTrue(archive_path.exists())

        verification = verify_month_archive(self.historian, archive_path, 2026, 5)
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["live_count"], 3)
        self.assertEqual(verification["archived_count"], 3)

    def test_verify_fails_honestly_when_archive_missing(self):
        verification = verify_month_archive(
            self.historian, archive_path_for_month(self.archive_dir, 2026, 5), 2026, 5,
        )
        self.assertFalse(verification["verified"])

    def test_verify_fails_when_archive_incomplete(self):
        self._seed("T1", "2026-05-01 00:00:00")
        self._seed("T1", "2026-05-15 00:00:00")
        create_month_archive(self.historian, self.archive_dir, 2026, 5, minimum_free_reserve_bytes=1024)

        # Simulate a corrupted/partial archive by deleting one row from
        # the archive file directly, after creation.
        import sqlite3
        archive_path = archive_path_for_month(self.archive_dir, 2026, 5)
        connection = sqlite3.connect(archive_path)
        connection.execute("DELETE FROM plc_data WHERE time = '2026-05-15 00:00:00'")
        connection.commit()
        connection.close()

        verification = verify_month_archive(self.historian, archive_path, 2026, 5)
        self.assertFalse(verification["verified"])

    def test_disk_space_skip_creates_no_file(self):
        self._seed("T1", "2026-05-01 00:00:00")
        result = create_month_archive(
            self.historian, self.archive_dir, 2026, 5, minimum_free_reserve_bytes=10**18,  # impossible
        )
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(archive_path_for_month(self.archive_dir, 2026, 5).exists())


class TestDeleteFromLive(ArchiveTestBase):
    def test_deletes_only_the_target_month(self):
        self._seed("T1", "2026-05-15 00:00:00")
        self._seed("T1", "2026-06-15 00:00:00")
        deleted = delete_month_from_live(self.historian, 2026, 5)
        self.assertEqual(deleted, 1)
        with self.historian.get_connection() as connection:
            remaining = connection.execute("SELECT COUNT(*) AS c FROM plc_data").fetchone()["c"]
        self.assertEqual(remaining, 1)


class TestFullOrchestration(ArchiveTestBase):
    def test_never_deletes_before_verification_passes(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        self._seed("T1", "2026-03-10 00:00:00")
        self._seed("T1", "2026-08-15 00:00:00")  # recent, not eligible

        results = archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024, now=now,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["stage"], "complete")
        self.assertTrue(results[0]["verified"])
        self.assertEqual(results[0]["rows_deleted"], 1)

        with self.historian.get_connection() as connection:
            remaining_times = [r["time"] for r in connection.execute("SELECT time FROM plc_data").fetchall()]
        self.assertEqual(remaining_times, ["2026-08-15 00:00:00"])  # only the recent row survives

        archive_path = archive_path_for_month(self.archive_dir, 2026, 3)
        self.assertTrue(archive_path.exists())

    def test_disk_space_skip_leaves_live_data_untouched(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        self._seed("T1", "2026-03-10 00:00:00")

        results = archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=10**18, now=now,
        )

        self.assertEqual(results[0]["stage"], "create")
        self.assertEqual(results[0]["status"], "skipped")
        with self.historian.get_connection() as connection:
            remaining = connection.execute("SELECT COUNT(*) AS c FROM plc_data").fetchone()["c"]
        self.assertEqual(remaining, 1)  # nothing deleted

    def test_multiple_eligible_months_all_processed_oldest_first(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        self._seed("T1", "2026-02-10 00:00:00")
        self._seed("T1", "2026-03-10 00:00:00")
        self._seed("T1", "2026-04-10 00:00:00")

        results = archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024, now=now,
        )

        self.assertEqual([(r["year"], r["month"]) for r in results], [(2026, 2), (2026, 3), (2026, 4)])
        self.assertTrue(all(r["verified"] for r in results))

    def test_a_genuinely_empty_intermediate_month_is_silently_skipped(self):
        # Data in January and March, a genuine gap in February (e.g.
        # from a real logging outage) - must not produce a wasted empty
        # archive file or a spurious "processed" result for February.
        now = datetime(2026, 8, 21, 12, 0, 0)
        self._seed("T1", "2026-01-10 00:00:00")
        self._seed("T1", "2026-03-10 00:00:00")
        results = archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024, now=now,
        )
        self.assertEqual([(r["year"], r["month"]) for r in results], [(2026, 1), (2026, 3)])
        self.assertFalse(archive_path_for_month(self.archive_dir, 2026, 2).exists())

    def test_idempotent_second_run_does_nothing_destructive(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        self._seed("T1", "2026-03-10 00:00:00")
        first = archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024, now=now,
        )
        second = archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024, now=now,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)  # nothing left in live to be eligible anymore


class TestQueryArchivedHistory(ArchiveTestBase):
    def test_returns_empty_list_when_no_archive_directory(self):
        self.assertEqual(query_archived_history(self.archive_dir, "T1", "2026-01-01 00:00:00", "2026-12-31 23:59:59", 100), [])

    def test_merges_multiple_months_sorted(self):
        self._seed("T1", "2026-02-15 00:00:00", 1.0)
        self._seed("T1", "2026-03-15 00:00:00", 2.0)
        self._seed("T1", "2026-04-15 00:00:00", 3.0)
        archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024,
            now=datetime(2026, 8, 21),
        )
        rows = query_archived_history(self.archive_dir, "T1", "2026-01-01 00:00:00", "2026-12-31 23:59:59", 100)
        self.assertEqual([r["value"] for r in rows], [1.0, 2.0, 3.0])

    def test_respects_limit(self):
        for day in range(1, 6):
            self._seed("T1", f"2026-03-{day:02d} 00:00:00")
        archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024,
            now=datetime(2026, 8, 21),
        )
        rows = query_archived_history(self.archive_dir, "T1", "2026-01-01 00:00:00", "2026-12-31 23:59:59", 2)
        self.assertEqual(len(rows), 2)

    def test_only_returns_the_requested_tag(self):
        self._seed("T1", "2026-03-01 00:00:00")
        self._seed("T2", "2026-03-01 00:00:00")
        archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024,
            now=datetime(2026, 8, 21),
        )
        rows = query_archived_history(self.archive_dir, "T1", "2026-01-01 00:00:00", "2026-12-31 23:59:59", 100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tag"], "T1")


class TestTransparentQueryThroughDatabaseManager(ArchiveTestBase):
    """The key end-to-end proof: after a real archive-and-purge cycle,
    DatabaseManager.get_history_range() - the SAME method every engine,
    Ask AI, and UI page already calls - transparently returns the full
    history, merging archived and live rows, with no caller-side change
    required."""

    def test_get_history_range_spans_archived_and_live_seamlessly(self):
        self._seed("T1", "2026-02-10 00:00:00", 10.0)   # will be archived
        self._seed("T1", "2026-03-10 00:00:00", 20.0)   # will be archived
        self._seed("T1", "2026-08-15 00:00:00", 30.0)   # stays live

        results = archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024,
            now=datetime(2026, 8, 21),
        )
        self.assertTrue(all(r["stage"] == "complete" and r["verified"] for r in results))

        # Confirm the archived rows are genuinely gone from the live table.
        with self.historian.get_connection() as connection:
            live_count = connection.execute("SELECT COUNT(*) AS c FROM plc_data").fetchone()["c"]
        self.assertEqual(live_count, 1)

        # And that get_history_range() still returns all three, in order,
        # exactly as if nothing had ever been archived - the whole point.
        history = self.historian.get_history_range("T1", "2026-01-01 00:00:00", "2026-12-31 23:59:59", limit=100)
        self.assertEqual([row["value"] for row in history], [10.0, 20.0, 30.0])

    def test_get_history_hours_lookback_also_reaches_archive_when_needed(self):
        # get_history() delegates to get_history_range() internally -
        # confirm that delegation preserves archive-awareness. Seeds a
        # genuinely old, real (not frozen-`now`) timestamp - a month
        # comfortably older than 90 days back from whenever this test
        # actually runs - and lets archive_and_purge_eligible_months()
        # use the real current time too, matching how the maintenance
        # worker actually calls it in production.
        old_time = datetime.now() - timedelta(days=200)
        self.historian.save_tag("T1", "addr", 42.0, timestamp=old_time)

        archive_and_purge_eligible_months(
            self.historian, self.archive_dir, retention_days=90, minimum_free_reserve_bytes=1024,
        )
        with self.historian.get_connection() as connection:
            live_count = connection.execute("SELECT COUNT(*) AS c FROM plc_data").fetchone()["c"]
        self.assertEqual(live_count, 0)  # the 200-day-old row's whole month is now archived, not live

        history = self.historian.get_history("T1", hours=24 * 400, limit=10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["value"], 42.0)

    def test_query_within_retention_window_never_touches_archive_dir(self):
        # A normal, recent-only query must not even check for archived
        # data when the live rows alone already satisfy the request.
        self._seed("T1", "2026-08-20 00:00:00")
        self.assertFalse(self.archive_dir.exists())
        history = self.historian.get_history_range("T1", "2026-08-01 00:00:00", "2026-12-31 23:59:59", limit=100)
        self.assertEqual(len(history), 1)
        self.assertFalse(self.archive_dir.exists())  # still never created


if __name__ == "__main__":
    unittest.main()
