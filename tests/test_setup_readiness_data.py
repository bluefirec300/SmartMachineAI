import shutil
import sqlite3
import tempfile
import unittest
from configparser import ConfigParser
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from config.config_manager import ConfigManager
from ui.setup_readiness_data import (
    SIMULATION_TUNING_REGISTRIES,
    get_all_readiness_status,
    get_area_system_status,
    get_backup_status,
    get_documentation_status,
    get_plc_connection_status,
    get_tariff_status,
    get_user_account_status,
)

"""
Phase V2.7 - New-Factory Setup Readiness. Every get_*_status() function
is tested against an isolated temp database (never the live one, so
these tests can never accidentally report on or affect a real
deployment's actual readiness state) with a schema matching just the
columns each function reads - plus one integration test against the
real live simulation database, confirming the whole aggregator runs
cleanly end-to-end with genuine, current data.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETUP_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "24_New_Factory_Setup.py")


class AreaSystemStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db_path = self.tmpdir / "config.db"
        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE areas (id INTEGER PRIMARY KEY, source TEXT)")
        connection.execute("CREATE TABLE systems (id INTEGER PRIMARY KEY, source TEXT)")
        connection.commit()
        connection.close()

    def _insert(self, table: str, sources: list[str]):
        connection = sqlite3.connect(self.db_path)
        connection.executemany(f"INSERT INTO {table} (source) VALUES (?)", [(s,) for s in sources])
        connection.commit()
        connection.close()

    def test_all_inferred_reports_zero_percent_confirmed(self):
        self._insert("areas", ["inferred_from_simulation"] * 3)
        self._insert("systems", ["inferred_from_simulation"] * 2)
        result = get_area_system_status(self.db_path)
        self.assertEqual(result["percent_confirmed"], 0)

    def test_all_confirmed_reports_100_percent(self):
        self._insert("areas", ["confirmed", "confirmed"])
        self._insert("systems", ["confirmed"])
        result = get_area_system_status(self.db_path)
        self.assertEqual(result["percent_confirmed"], 100)

    def test_mixed_confirmation_is_counted_correctly(self):
        self._insert("areas", ["confirmed", "inferred_from_simulation"])
        self._insert("systems", ["confirmed", "confirmed", "inferred_from_simulation"])
        result = get_area_system_status(self.db_path)
        self.assertEqual(result["areas"], {"total": 2, "confirmed": 1, "unconfirmed": 1})
        self.assertEqual(result["systems"], {"total": 3, "confirmed": 2, "unconfirmed": 1})

    def test_empty_tables_do_not_crash_and_report_100_percent(self):
        result = get_area_system_status(self.db_path)
        self.assertEqual(result["percent_confirmed"], 100)


class TariffStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db_path = self.tmpdir / "config.db"
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "CREATE TABLE energy_tariffs (plant_id INTEGER, is_simulated INTEGER, currency TEXT, "
            "energy_rate REAL, expiry_date TEXT)"
        )
        connection.commit()
        connection.close()

    def _insert(self, plant_id, is_simulated, expiry_date=None):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "INSERT INTO energy_tariffs (plant_id, is_simulated, currency, energy_rate, expiry_date) "
            "VALUES (?, ?, 'MYR', 0.5, ?)",
            (plant_id, int(is_simulated), expiry_date),
        )
        connection.commit()
        connection.close()

    def test_simulated_tariff_is_not_counted_as_real(self):
        self._insert(None, is_simulated=True)
        result = get_tariff_status(self.db_path)
        self.assertEqual(result["real_count"], 0)
        self.assertEqual(result["total"], 1)

    def test_real_tariff_is_counted(self):
        self._insert(None, is_simulated=False)
        result = get_tariff_status(self.db_path)
        self.assertEqual(result["real_count"], 1)

    def test_closed_out_historical_tariff_is_excluded(self):
        self._insert(None, is_simulated=True, expiry_date="2026-01-01")
        result = get_tariff_status(self.db_path)
        self.assertEqual(result["total"], 0)

    def test_no_tariff_rows_does_not_crash(self):
        result = get_tariff_status(self.db_path)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["scopes"], [])


class DocumentationStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db_path = self.tmpdir / "config.db"
        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE equipment_documents (equipment_id INTEGER, file_path TEXT)")
        connection.commit()
        connection.close()

    def _insert(self, equipment_id, file_path):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "INSERT INTO equipment_documents (equipment_id, file_path) VALUES (?, ?)", (equipment_id, file_path)
        )
        connection.commit()
        connection.close()

    def test_real_manual_is_counted_as_real(self):
        self._insert(None, "manuals/Some_Brand_manual_REAL.pdf")
        result = get_documentation_status(self.db_path)
        self.assertEqual(result["real_count"], 1)
        self.assertEqual(result["synthetic_count"], 0)

    def test_synthetic_manual_is_counted_as_synthetic(self):
        self._insert(None, "manuals/Some_Brand_manual.pdf")
        result = get_documentation_status(self.db_path)
        self.assertEqual(result["synthetic_count"], 1)

    def test_site_uploaded_document_is_excluded_from_reference_manual_count(self):
        self._insert(7, "manuals/uploaded/a_photo.png")
        result = get_documentation_status(self.db_path)
        self.assertEqual(result["total_reference_manuals"], 0)


class PlcConnectionStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.actual_db = self.tmpdir / "actual_config.db"

        # audit_log is created by a different migrator in the real app -
        # PLCConnectionManager itself only owns plc_connections, so seed
        # the minimal table it writes audit entries to (same precedent
        # as tests/test_user_manager_lockout.py's LockoutTestBase). Also
        # creates the db FILE itself, which get_plc_connection_status()
        # checks for before doing anything else.
        connection = sqlite3.connect(self.actual_db)
        connection.execute(
            """CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, username TEXT, action TEXT,
                entity_type TEXT, entity_name TEXT, old_value TEXT, new_value TEXT, details TEXT
            )"""
        )
        connection.commit()
        connection.close()

    def _patch_environments(self, path):
        return mock.patch.dict(
            "ui.setup_readiness_data.ENVIRONMENTS", {"actual": {"config_db": path}}
        )

    def test_missing_actual_database_reports_unavailable(self):
        with self._patch_environments(self.tmpdir / "does_not_exist.db"):
            result = get_plc_connection_status()
        self.assertFalse(result["available"])

    def test_no_active_connection_is_reported_honestly(self):
        with self._patch_environments(self.actual_db):
            result = get_plc_connection_status()
        self.assertTrue(result["available"])
        self.assertIsNone(result["active_connection"])

    def test_local_test_endpoint_is_flagged(self):
        with self._patch_environments(self.actual_db):
            from config.plc_connection_manager import PLCConnectionManager

            manager = PLCConnectionManager(database_path=self.actual_db)
            created = manager.create_connection(
                name="test opc", protocol="opcua", created_by="tester",
                endpoint_url="opc.tcp://0.0.0.0:4840/opcua/simulator",
            )
            manager.set_active(created["id"], changed_by="tester")

            result = get_plc_connection_status()

        self.assertTrue(result["active_connection"]["looks_local"])

    def test_genuine_looking_address_is_not_flagged(self):
        with self._patch_environments(self.actual_db):
            from config.plc_connection_manager import PLCConnectionManager

            manager = PLCConnectionManager(database_path=self.actual_db)
            created = manager.create_connection(
                name="Plant PLC", protocol="modbus_tcp", created_by="tester", host="10.20.30.40", port=502,
            )
            manager.set_active(created["id"], changed_by="tester")

            result = get_plc_connection_status()

        self.assertFalse(result["active_connection"]["looks_local"])


class UserAccountStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db_path = self.tmpdir / "config.db"
        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE users (username TEXT, role TEXT, active INTEGER)")
        connection.executemany(
            "INSERT INTO users (username, role, active) VALUES (?, ?, ?)",
            [("admin", "admin", 1), ("bob", "engineer", 1), ("old_demo", "engineer", 0)],
        )
        connection.commit()
        connection.close()

    def test_counts_are_accurate(self):
        result = get_user_account_status(self.db_path)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["active_count"], 2)
        self.assertEqual(result["admin_count"], 1)

    def test_never_guesses_which_accounts_are_demo_accounts(self):
        # No field in the result claims to identify "demo" accounts -
        # that judgment is explicitly left to a human, per this phase's
        # "do not invent site values" constraint.
        result = get_user_account_status(self.db_path)
        for account in result["accounts"]:
            self.assertNotIn("is_demo", account)
            self.assertNotIn("suspicious", account)


class BackupStatusTests(unittest.TestCase):
    def _config_with(self, ini_text: str) -> ConfigManager:
        config = ConfigManager.__new__(ConfigManager)
        config.project_root = Path(tempfile.mkdtemp())
        config.config_path = config.project_root / "settings.ini"
        config.config = ConfigParser()
        config.config.read_string(ini_text)
        return config

    def test_default_local_backups_dir_is_flagged(self):
        with mock.patch("ui.setup_readiness_data.ConfigManager") as mock_cls:
            mock_cls.return_value.historian_backup_enabled = True
            mock_cls.return_value.historian_backup_destination_dir = (
                Path(__file__).resolve().parent.parent / "backups"
            )
            result = get_backup_status()
        self.assertTrue(result["looks_like_default_local_path"])

    def test_a_different_destination_is_not_flagged(self):
        with mock.patch("ui.setup_readiness_data.ConfigManager") as mock_cls:
            mock_cls.return_value.historian_backup_enabled = True
            mock_cls.return_value.historian_backup_destination_dir = Path("/mnt/offsite-backups")
            result = get_backup_status()
        self.assertFalse(result["looks_like_default_local_path"])

    def test_disabled_backup_is_reported(self):
        with mock.patch("ui.setup_readiness_data.ConfigManager") as mock_cls:
            mock_cls.return_value.historian_backup_enabled = False
            mock_cls.return_value.historian_backup_destination_dir = Path("/mnt/offsite-backups")
            result = get_backup_status()
        self.assertFalse(result["enabled"])


class SimulationTuningRegistryListTests(unittest.TestCase):
    def test_lists_all_five_known_registries(self):
        self.assertEqual(len(SIMULATION_TUNING_REGISTRIES), 5)

    def test_every_entry_has_domain_file_and_defines(self):
        for entry in SIMULATION_TUNING_REGISTRIES:
            self.assertIn("domain", entry)
            self.assertIn("file", entry)
            self.assertIn("defines", entry)

    def test_every_referenced_file_actually_exists(self):
        for entry in SIMULATION_TUNING_REGISTRIES:
            self.assertTrue((PROJECT_ROOT / entry["file"]).exists(), entry["file"])


class GetAllReadinessStatusIntegrationTests(unittest.TestCase):
    """Integration check against the real live simulation database -
    proves the whole aggregator runs cleanly end-to-end with genuine,
    current data, not just isolated fixtures."""

    def test_runs_cleanly_against_the_real_project_database_and_has_every_section(self):
        status = get_all_readiness_status()
        self.assertEqual(
            set(status.keys()),
            {"area_system", "tariff", "documentation", "plc_connection", "user_accounts", "backup", "configuration_completeness"},
        )

    def test_real_documentation_counts_match_the_known_25_of_28(self):
        status = get_all_readiness_status()
        self.assertEqual(status["documentation"]["total_reference_manuals"], 28)
        self.assertEqual(status["documentation"]["real_count"], 25)
        self.assertEqual(status["documentation"]["synthetic_count"], 3)


class SetupReadinessPageTests(unittest.TestCase):
    def test_unauthenticated_session_is_blocked_not_a_crash(self):
        at = AppTest.from_file(SETUP_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))
        errors = [e.value for e in at.error]
        self.assertTrue(any("permission" in e.lower() for e in errors))

    def test_admin_session_renders_all_eight_sections(self):
        # Safe to run a real authenticated session here - this page is
        # read-only and performs no writes at all, unlike the Alarm
        # Notification Settings admin page.
        at = AppTest.from_file(SETUP_PAGE, default_timeout=30)
        at.session_state["auth_user"] = {"username": "test_admin", "display_name": "Test Admin", "role": "admin"}
        at.run()
        self.assertFalse(bool(at.exception))
        subheaders = [s.value for s in at.subheader]
        self.assertEqual(len(subheaders), 8)

    def test_page_source_never_edits_or_writes_any_database(self):
        source = Path(SETUP_PAGE).read_text(encoding="utf-8")
        for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", ".execute(", "st.form", "st.button"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
