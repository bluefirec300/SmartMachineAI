import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

from config.configuration_manager import ConfigurationManager

"""
Phase V2.9 - audit-log search/export. get_audit_log()'s new filter
params are tested against an isolated temp database (same schema/
fixture pattern as tests/test_setup_readiness_data.py), never the live
database. A full authenticated AppTest render is used for the page
itself, same as Phase V2.7's New-Factory Setup page - this page
performs zero writes (filters + a CSV download button only), so the
established "avoid live authenticated sessions on write-capable admin
pages" caution doesn't apply here.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_LOG_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "25_Audit_Log.py")


class GetAuditLogFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db_path = self.tmpdir / "config.db"

        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, username TEXT, action TEXT,
                entity_type TEXT, entity_name TEXT, old_value TEXT, new_value TEXT, details TEXT
            )"""
        )
        connection.commit()
        connection.close()

        self.manager = ConfigurationManager(database_path=self.db_path)

    def _seed(self, rows: list[dict]):
        connection = sqlite3.connect(self.db_path)
        for row in rows:
            connection.execute(
                """
                INSERT INTO audit_log
                (timestamp, username, action, entity_type, entity_name, old_value, new_value, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["timestamp"],
                    row["username"],
                    row["action"],
                    row.get("entity_type"),
                    row.get("entity_name"),
                    row.get("old_value"),
                    row.get("new_value"),
                    row.get("details"),
                ),
            )
        connection.commit()
        connection.close()

    def test_no_filters_returns_everything_up_to_limit(self):
        self._seed(
            [
                {
                    "timestamp": "2026-08-20T09:00:00",
                    "username": "alice",
                    "action": "update_setpoint",
                    "entity_type": "setpoint",
                    "entity_name": "Pressure",
                },
                {
                    "timestamp": "2026-08-21T09:00:00",
                    "username": "bob",
                    "action": "create_user",
                    "entity_type": "user",
                    "entity_name": "carol",
                },
            ]
        )

        results = self.manager.get_audit_log(limit=20)

        self.assertEqual(len(results), 2)
        # Most recent first.
        self.assertEqual(results[0]["username"], "bob")

    def test_filters_by_entity_type(self):
        self._seed(
            [
                {
                    "timestamp": "2026-08-20T09:00:00",
                    "username": "alice",
                    "action": "update_setpoint",
                    "entity_type": "setpoint",
                    "entity_name": "Pressure",
                },
                {
                    "timestamp": "2026-08-21T09:00:00",
                    "username": "bob",
                    "action": "create_user",
                    "entity_type": "user",
                    "entity_name": "carol",
                },
            ]
        )

        results = self.manager.get_audit_log(limit=20, entity_type="user")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["entity_name"], "carol")

    def test_filters_by_username(self):
        self._seed(
            [
                {"timestamp": "2026-08-20T09:00:00", "username": "alice", "action": "a"},
                {"timestamp": "2026-08-21T09:00:00", "username": "bob", "action": "b"},
            ]
        )

        results = self.manager.get_audit_log(limit=20, username="alice")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["username"], "alice")

    def test_search_matches_entity_name(self):
        self._seed(
            [
                {
                    "timestamp": "2026-08-20T09:00:00",
                    "username": "alice",
                    "action": "update_setpoint",
                    "entity_name": "Compressor Pressure",
                },
                {
                    "timestamp": "2026-08-21T09:00:00",
                    "username": "bob",
                    "action": "update_setpoint",
                    "entity_name": "Chiller Temperature",
                },
            ]
        )

        results = self.manager.get_audit_log(limit=20, search="Compressor")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["entity_name"], "Compressor Pressure")

    def test_search_matches_action_and_details(self):
        self._seed(
            [
                {
                    "timestamp": "2026-08-20T09:00:00",
                    "username": "alice",
                    "action": "switch_environment",
                    "details": "simulation -> actual",
                },
            ]
        )

        self.assertEqual(len(self.manager.get_audit_log(search="switch_environment")), 1)
        self.assertEqual(len(self.manager.get_audit_log(search="actual")), 1)
        self.assertEqual(len(self.manager.get_audit_log(search="nonexistent")), 0)

    def test_date_range_filter(self):
        self._seed(
            [
                {"timestamp": "2026-08-18T09:00:00", "username": "alice", "action": "a"},
                {"timestamp": "2026-08-20T09:00:00", "username": "alice", "action": "b"},
                {"timestamp": "2026-08-22T09:00:00", "username": "alice", "action": "c"},
            ]
        )

        results = self.manager.get_audit_log(
            limit=20, start_date="2026-08-19", end_date="2026-08-21"
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action"], "b")

    def test_limit_is_respected(self):
        self._seed(
            [
                {"timestamp": f"2026-08-{day:02d}T09:00:00", "username": "alice", "action": "a"}
                for day in range(1, 11)
            ]
        )

        self.assertEqual(len(self.manager.get_audit_log(limit=3)), 3)

    def test_filters_combine_with_and(self):
        self._seed(
            [
                {
                    "timestamp": "2026-08-20T09:00:00",
                    "username": "alice",
                    "action": "update_setpoint",
                    "entity_type": "setpoint",
                },
                {
                    "timestamp": "2026-08-20T09:00:00",
                    "username": "bob",
                    "action": "update_setpoint",
                    "entity_type": "setpoint",
                },
            ]
        )

        results = self.manager.get_audit_log(
            limit=20, entity_type="setpoint", username="bob"
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["username"], "bob")

    def test_get_audit_log_filter_options_returns_distinct_values(self):
        self._seed(
            [
                {
                    "timestamp": "2026-08-20T09:00:00",
                    "username": "alice",
                    "action": "a",
                    "entity_type": "setpoint",
                },
                {
                    "timestamp": "2026-08-21T09:00:00",
                    "username": "alice",
                    "action": "b",
                    "entity_type": "setpoint",
                },
                {
                    "timestamp": "2026-08-22T09:00:00",
                    "username": "bob",
                    "action": "c",
                    "entity_type": "user",
                },
            ]
        )

        options = self.manager.get_audit_log_filter_options()

        self.assertEqual(options["entity_types"], ["setpoint", "user"])
        self.assertEqual(options["usernames"], ["alice", "bob"])

    def test_empty_table_returns_no_rows_and_no_filter_options(self):
        self.assertEqual(self.manager.get_audit_log(), [])
        options = self.manager.get_audit_log_filter_options()
        self.assertEqual(options["entity_types"], [])
        self.assertEqual(options["usernames"], [])


class AuditLogPageSmokeTests(unittest.TestCase):
    def test_page_blocks_unauthenticated_access_without_raising(self):
        at = AppTest.from_file(AUDIT_LOG_PAGE, default_timeout=30)
        at.run()

        self.assertFalse(bool(at.exception))
        self.assertTrue(any("permission" in e.value for e in at.error))

    def test_authenticated_admin_renders_without_error(self):
        # Zero-write page (filters + CSV download only) - safe to run a
        # full authenticated session, same reasoning as the New-Factory
        # Setup page in Phase V2.7.
        at = AppTest.from_file(AUDIT_LOG_PAGE, default_timeout=30)
        at.session_state["auth_user"] = {
            "username": "test_admin",
            "display_name": "Test Admin",
            "role": "admin",
        }
        at.run()

        self.assertFalse(bool(at.exception))
        titles = [t.value for t in at.title]
        self.assertTrue(any("Audit Log" in t for t in titles))


if __name__ == "__main__":
    unittest.main()
