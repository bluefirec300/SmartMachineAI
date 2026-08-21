import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

from config.notification_settings_manager import (
    NotificationSettingsManager,
    is_valid_email,
)

"""
Phase V2.3 - Alarm Notification Settings admin UI. Tests the
DB-backed settings/recipients manager in isolation (temp config.db,
never the live one) plus a page-level smoke check confirming the
admin-only gate blocks an unauthenticated session cleanly. A full
authenticated click-through isn't attempted here - same reasoning as
Phase V1.3's login lockout tests: this page performs real writes, and
its @st.cache_resource-wrapped manager is process-wide, not
per-session, so a live AppTest walkthrough risks touching real data or
cross-contaminating between test methods. The manager's actual
behavior is fully covered directly below instead.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "23_Alarm_Notification_Settings.py")


class IsValidEmailTests(unittest.TestCase):
    def test_accepts_a_realistic_address(self):
        self.assertTrue(is_valid_email("engineer@example.com"))

    def test_accepts_a_subdomain_address(self):
        self.assertTrue(is_valid_email("alerts@mail.example.co.uk"))

    def test_rejects_missing_at_sign(self):
        self.assertFalse(is_valid_email("engineer.example.com"))

    def test_rejects_missing_domain_dot(self):
        self.assertFalse(is_valid_email("engineer@example"))

    def test_rejects_whitespace(self):
        self.assertFalse(is_valid_email("engineer @example.com"))

    def test_rejects_empty_string(self):
        self.assertFalse(is_valid_email(""))

    def test_rejects_none(self):
        self.assertFalse(is_valid_email(None))

    def test_strips_surrounding_whitespace_before_checking(self):
        self.assertTrue(is_valid_email("  engineer@example.com  "))


class NotificationSettingsManagerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db_path = self.tmpdir / "config.db"

        # audit_log is created by a different migrator in the real app -
        # seed the minimal table this manager writes to, same precedent
        # as tests/test_user_manager_lockout.py's LockoutTestBase.
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, username TEXT, action TEXT,
                entity_type TEXT, entity_name TEXT, old_value TEXT, new_value TEXT, details TEXT
            )"""
        )
        connection.commit()
        connection.close()

        self.manager = NotificationSettingsManager(database_path=self.db_path)


class DefaultSettingsTests(NotificationSettingsManagerTestBase):
    def test_fresh_database_defaults_to_disabled(self):
        self.assertFalse(self.manager.get_settings()["enabled"])

    def test_fresh_database_defaults_to_alarm_severity(self):
        self.assertEqual(self.manager.get_settings()["min_severity"], "alarm")

    def test_fresh_database_defaults_to_60_minute_cooldown(self):
        self.assertEqual(self.manager.get_settings()["cooldown_minutes"], 60.0)

    def test_fresh_database_has_no_recipients(self):
        self.assertEqual(self.manager.list_recipients(), [])

    def test_constructing_the_manager_twice_does_not_reset_settings(self):
        self.manager.update_settings(True, "warning", 15.0, changed_by="admin")
        second_manager = NotificationSettingsManager(database_path=self.db_path)
        self.assertTrue(second_manager.get_settings()["enabled"])


class UpdateSettingsTests(NotificationSettingsManagerTestBase):
    def test_update_persists_and_is_read_back(self):
        self.manager.update_settings(True, "warning", 30.0, changed_by="admin")
        settings = self.manager.get_settings()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["min_severity"], "warning")
        self.assertEqual(settings["cooldown_minutes"], 30.0)

    def test_update_records_who_and_when(self):
        self.manager.update_settings(True, "alarm", 60.0, changed_by="admin_bob")
        settings = self.manager.get_settings()
        self.assertEqual(settings["updated_by"], "admin_bob")
        self.assertIsNotNone(settings["updated_at"])

    def test_invalid_min_severity_is_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.update_settings(True, "critical", 60.0, changed_by="admin")

    def test_zero_cooldown_is_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.update_settings(True, "alarm", 0, changed_by="admin")

    def test_negative_cooldown_is_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.update_settings(True, "alarm", -5.0, changed_by="admin")

    def test_rejected_update_does_not_change_stored_settings(self):
        self.manager.update_settings(True, "alarm", 60.0, changed_by="admin")
        with self.assertRaises(ValueError):
            self.manager.update_settings(True, "bogus", 60.0, changed_by="admin")
        self.assertEqual(self.manager.get_settings()["min_severity"], "alarm")

    def test_update_writes_to_the_audit_log(self):
        self.manager.update_settings(True, "warning", 30.0, changed_by="admin_bob")
        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT username, entity_type FROM audit_log WHERE action = 'update_notification_settings'"
        ).fetchone()
        connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "admin_bob")
        self.assertEqual(row[1], "notification_settings")


class RecipientManagementTests(NotificationSettingsManagerTestBase):
    def test_add_recipient_appears_in_list(self):
        self.manager.add_recipient("engineer@example.com", created_by="admin")
        emails = [r["email"] for r in self.manager.list_recipients()]
        self.assertIn("engineer@example.com", emails)

    def test_add_recipient_records_who_added_it(self):
        self.manager.add_recipient("engineer@example.com", created_by="admin_bob")
        recipient = self.manager.list_recipients()[0]
        self.assertEqual(recipient["created_by"], "admin_bob")

    def test_add_invalid_email_is_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.add_recipient("not-an-email", created_by="admin")

    def test_invalid_email_is_never_added(self):
        with self.assertRaises(ValueError):
            self.manager.add_recipient("not-an-email", created_by="admin")
        self.assertEqual(self.manager.list_recipients(), [])

    def test_duplicate_email_is_rejected(self):
        self.manager.add_recipient("engineer@example.com", created_by="admin")
        with self.assertRaises(ValueError):
            self.manager.add_recipient("engineer@example.com", created_by="admin")

    def test_duplicate_email_is_rejected_case_insensitively(self):
        self.manager.add_recipient("Engineer@Example.com", created_by="admin")
        with self.assertRaises(ValueError):
            self.manager.add_recipient("engineer@example.com", created_by="admin")

    def test_duplicate_rejection_does_not_add_a_second_row(self):
        self.manager.add_recipient("engineer@example.com", created_by="admin")
        try:
            self.manager.add_recipient("engineer@example.com", created_by="admin")
        except ValueError:
            pass
        self.assertEqual(len(self.manager.list_recipients()), 1)

    def test_remove_recipient_deletes_it(self):
        added = self.manager.add_recipient("engineer@example.com", created_by="admin")
        self.manager.remove_recipient(added["id"], changed_by="admin")
        self.assertEqual(self.manager.list_recipients(), [])

    def test_removing_an_unknown_id_does_not_raise(self):
        self.manager.remove_recipient(9999, changed_by="admin")

    def test_add_recipient_writes_to_the_audit_log(self):
        self.manager.add_recipient("engineer@example.com", created_by="admin_bob")
        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT username, entity_name FROM audit_log WHERE action = 'add_notification_recipient'"
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "admin_bob")
        self.assertEqual(row[1], "engineer@example.com")

    def test_remove_recipient_writes_to_the_audit_log(self):
        added = self.manager.add_recipient("engineer@example.com", created_by="admin")
        self.manager.remove_recipient(added["id"], changed_by="admin_bob")
        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT username, entity_name FROM audit_log WHERE action = 'remove_notification_recipient'"
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "admin_bob")
        self.assertEqual(row[1], "engineer@example.com")

    def test_multiple_recipients_are_listed_alphabetically(self):
        self.manager.add_recipient("zeta@example.com", created_by="admin")
        self.manager.add_recipient("alpha@example.com", created_by="admin")
        emails = [r["email"] for r in self.manager.list_recipients()]
        self.assertEqual(emails, ["alpha@example.com", "zeta@example.com"])


class AlarmNotificationSettingsPageTests(unittest.TestCase):
    def test_unauthenticated_session_is_blocked_not_a_crash(self):
        at = AppTest.from_file(SETTINGS_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))
        errors = [e.value for e in at.error]
        self.assertTrue(any("permission" in e.lower() for e in errors))

    def test_page_source_gates_on_admin_role(self):
        source = Path(SETTINGS_PAGE).read_text(encoding="utf-8")
        self.assertIn('auth.require_role("admin")', source)

    def test_page_never_reads_smtp_credential_env_vars_or_config(self):
        # The docstring/caption DO mention SMTP_USERNAME/SMTP_PASSWORD/
        # smtp_host by name, explaining that they're intentionally
        # excluded - that's honest documentation, not a violation. What
        # actually matters is that the page never READS them (os.getenv,
        # ConfigManager.smtp_*) or renders a credential input widget.
        source = Path(SETTINGS_PAGE).read_text(encoding="utf-8")
        self.assertNotIn("os.getenv", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn(".smtp_host", source)
        self.assertNotIn(".smtp_port", source)
        self.assertNotIn(".smtp_use_tls", source)
        self.assertNotIn(".smtp_from_address", source)
        self.assertNotIn('type="password"', source)


if __name__ == "__main__":
    unittest.main()
