import shutil
import sqlite3
import tempfile
import unittest
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path
from unittest import mock

from ai.event_store import EventStore
from ai.notification_log import NotificationLog
from app import notification_worker
from config.config_manager import ConfigManager
from config.notification_settings_manager import NotificationSettingsManager
from engine.alarm_notification_engine import (
    format_notification_email,
    select_notifiable_events,
    send_email,
    should_notify,
)

"""
Phase V2.2 - Alarm Notifications. Covers the pure decision/formatting
logic, the notification log's cooldown-tracking persistence, the new
EventStore.get_events_since_id() cursor query, the worker's run_cycle()
integration (cooldown skip, cursor advance/rollback on failure,
disabled-by-default), and config parsing - all against isolated temp
databases/files, never the live system, and with smtplib always
mocked (no real email is ever sent by these tests).
"""


SAMPLE_EVENT = {
    "id": 1,
    "event_time": "2026-08-21 13:00:00",
    "equipment": "Air Compressor AC03 (P01)",
    "tag": "P01.UTILITY.AC03.Power_kW",
    "severity": "alarm",
    "condition": "high_alarm",
    "value": 40.18,
    "unit": "kW",
    "message": "P01.UTILITY.AC03.Power_kW is critically high at 40.18 kW (high alarm limit: 35 kW).",
}


class SelectNotifiableEventsTests(unittest.TestCase):
    def test_alarm_qualifies_when_min_severity_is_alarm(self):
        result = select_notifiable_events([SAMPLE_EVENT], "alarm")
        self.assertEqual(result, [SAMPLE_EVENT])

    def test_warning_does_not_qualify_when_min_severity_is_alarm(self):
        warning_event = {**SAMPLE_EVENT, "severity": "warning"}
        result = select_notifiable_events([warning_event], "alarm")
        self.assertEqual(result, [])

    def test_warning_qualifies_when_min_severity_is_warning(self):
        warning_event = {**SAMPLE_EVENT, "severity": "warning"}
        result = select_notifiable_events([warning_event], "warning")
        self.assertEqual(result, [warning_event])

    def test_alarm_still_qualifies_when_min_severity_is_warning(self):
        result = select_notifiable_events([SAMPLE_EVENT], "warning")
        self.assertEqual(result, [SAMPLE_EVENT])

    def test_unrecognized_severity_is_never_silently_dropped(self):
        odd_event = {**SAMPLE_EVENT, "severity": "critical"}
        result = select_notifiable_events([odd_event], "alarm")
        self.assertEqual(result, [odd_event])


class ShouldNotifyTests(unittest.TestCase):
    def test_never_notified_before_always_notifies(self):
        self.assertTrue(should_notify(datetime(2026, 8, 21, 13, 0), None, 60))

    def test_within_cooldown_window_is_suppressed(self):
        now = datetime(2026, 8, 21, 13, 30)
        last = datetime(2026, 8, 21, 13, 0)
        self.assertFalse(should_notify(now, last, 60))

    def test_exactly_at_cooldown_boundary_notifies(self):
        now = datetime(2026, 8, 21, 14, 0)
        last = datetime(2026, 8, 21, 13, 0)
        self.assertTrue(should_notify(now, last, 60))

    def test_past_cooldown_window_notifies_again(self):
        now = datetime(2026, 8, 21, 15, 0)
        last = datetime(2026, 8, 21, 13, 0)
        self.assertTrue(should_notify(now, last, 60))


class FormatNotificationEmailTests(unittest.TestCase):
    def test_subject_identifies_severity_equipment_and_condition(self):
        subject, _ = format_notification_email(SAMPLE_EVENT)
        self.assertIn("ALARM", subject)
        self.assertIn("Air Compressor AC03 (P01)", subject)
        self.assertIn("high_alarm", subject)

    def test_body_contains_every_required_field(self):
        _, body = format_notification_email(SAMPLE_EVENT)
        self.assertIn("Equipment: Air Compressor AC03 (P01)", body)
        self.assertIn("Tag: P01.UTILITY.AC03.Power_kW", body)
        self.assertIn("Severity: ALARM", body)
        self.assertIn("Condition: high_alarm", body)
        self.assertIn("Value: 40.18 kW", body)
        self.assertIn("Timestamp: 2026-08-21 13:00:00", body)
        self.assertIn("critically high", body)  # the reused message text

    def test_missing_value_is_labeled_unavailable_not_blank_or_zero(self):
        event = {**SAMPLE_EVENT, "value": None}
        _, body = format_notification_email(event)
        self.assertIn("Value: Unavailable", body)


class SendEmailTests(unittest.TestCase):
    def _kwargs(self, **overrides):
        base = dict(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_use_tls=True,
            smtp_username="user",
            smtp_password="pass",
            from_address="alerts@example.com",
            recipients=["engineer@example.com"],
            subject="subject",
            body="body",
        )
        base.update(overrides)
        return base

    def test_missing_host_fails_without_raising(self):
        ok, error = send_email(**self._kwargs(smtp_host=""))
        self.assertFalse(ok)
        self.assertIn("smtp_host", error)

    def test_missing_recipients_fails_without_raising(self):
        ok, error = send_email(**self._kwargs(recipients=[]))
        self.assertFalse(ok)
        self.assertIn("recipients", error)

    def test_missing_from_address_fails_without_raising(self):
        ok, error = send_email(**self._kwargs(from_address=""))
        self.assertFalse(ok)
        self.assertIn("smtp_from_address", error)

    def test_successful_send_uses_starttls_and_login(self):
        with mock.patch("smtplib.SMTP") as smtp_cls:
            server = smtp_cls.return_value.__enter__.return_value
            ok, error = send_email(**self._kwargs())

        self.assertTrue(ok)
        self.assertIsNone(error)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user", "pass")
        server.send_message.assert_called_once()

    def test_no_tls_skips_starttls(self):
        with mock.patch("smtplib.SMTP") as smtp_cls:
            server = smtp_cls.return_value.__enter__.return_value
            send_email(**self._kwargs(smtp_use_tls=False))

        server.starttls.assert_not_called()

    def test_smtp_exception_is_caught_and_reported_never_raised(self):
        with mock.patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
            ok, error = send_email(**self._kwargs())

        self.assertFalse(ok)
        self.assertIn("ConnectionRefusedError", error)


class NotificationLogTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.log = NotificationLog(database_path=self.tmpdir / "machine_data.db")

    def test_unknown_condition_has_no_last_notified_at(self):
        self.assertIsNone(self.log.get_last_notified_at("Eq", "Tag", "high_alarm"))

    def test_record_then_read_round_trips(self):
        when = datetime(2026, 8, 21, 13, 0, 0)
        self.log.record_notified("Eq", "Tag", "high_alarm", "alarm", 1, when)
        self.assertEqual(self.log.get_last_notified_at("Eq", "Tag", "high_alarm"), when)

    def test_condition_is_normalized_case_insensitively(self):
        when = datetime(2026, 8, 21, 13, 0, 0)
        self.log.record_notified("Eq", "Tag", "HIGH_ALARM", "alarm", 1, when)
        self.assertEqual(self.log.get_last_notified_at("Eq", "Tag", "high_alarm"), when)

    def test_re_notifying_updates_timestamp_and_increments_count(self):
        first = datetime(2026, 8, 21, 13, 0, 0)
        second = datetime(2026, 8, 21, 15, 0, 0)
        self.log.record_notified("Eq", "Tag", "high_alarm", "alarm", 1, first)
        self.log.record_notified("Eq", "Tag", "high_alarm", "alarm", 2, second)

        rows = self.log.get_recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["notification_count"], 2)
        self.assertEqual(rows[0]["last_notified_at"], "2026-08-21 15:00:00")

    def test_different_conditions_are_tracked_independently(self):
        when = datetime(2026, 8, 21, 13, 0, 0)
        self.log.record_notified("Eq", "Tag", "high_alarm", "alarm", 1, when)
        self.log.record_notified("Eq", "Tag", "low_alarm", "alarm", 2, when)
        self.assertEqual(len(self.log.get_recent()), 2)


class GetEventsSinceIdTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = EventStore(database_path=self.tmpdir / "machine_data.db")

        for i in range(3):
            self.store.insert_event(
                {
                    "event_time": f"2026-08-21 13:0{i}:00",
                    "detected_at": f"2026-08-21 13:0{i}:00",
                    "equipment": "Eq",
                    "tag": f"Tag{i}",
                    "severity": "alarm",
                    "condition": "high_alarm",
                }
            )

    def test_since_zero_returns_all_events_oldest_first(self):
        events = self.store.get_events_since_id(0)
        self.assertEqual([e["tag"] for e in events], ["Tag0", "Tag1", "Tag2"])

    def test_since_a_later_id_returns_only_newer_events(self):
        first_batch = self.store.get_events_since_id(0)
        cursor = first_batch[0]["id"]
        events = self.store.get_events_since_id(cursor)
        self.assertEqual([e["tag"] for e in events], ["Tag1", "Tag2"])

    def test_since_the_latest_id_returns_nothing(self):
        all_events = self.store.get_events_since_id(0)
        latest_id = all_events[-1]["id"]
        self.assertEqual(self.store.get_events_since_id(latest_id), [])


def _config_with(ini_text: str) -> ConfigManager:
    config = ConfigManager.__new__(ConfigManager)
    config.project_root = Path(tempfile.mkdtemp())
    config.config_path = config.project_root / "settings.ini"
    config.config = ConfigParser()
    config.config.read_string(ini_text)
    return config


class NotificationConfigTests(unittest.TestCase):
    def test_missing_section_defaults_to_disabled(self):
        config = _config_with("[DATABASE]\npath = x\n")
        self.assertFalse(config.notifications_enabled)

    def test_missing_section_defaults_to_no_recipients(self):
        config = _config_with("[DATABASE]\npath = x\n")
        self.assertEqual(config.notification_recipients, [])

    def test_recipients_are_split_and_trimmed(self):
        config = _config_with("[NOTIFICATIONS]\nrecipients = a@example.com, b@example.com ,c@example.com\n")
        self.assertEqual(config.notification_recipients, ["a@example.com", "b@example.com", "c@example.com"])

    def test_min_severity_defaults_to_alarm(self):
        config = _config_with("[DATABASE]\npath = x\n")
        self.assertEqual(config.notification_min_severity, "alarm")

    def test_explicit_enabled_true_is_honored(self):
        config = _config_with("[NOTIFICATIONS]\nenabled = true\n")
        self.assertTrue(config.notifications_enabled)

    def test_real_project_settings_ini_defaults_to_disabled(self):
        # Integration check against the actual shipped settings.ini -
        # notifications must ship OFF, same principle as historian backup.
        config = ConfigManager()
        self.assertFalse(config.notifications_enabled)


class RunCycleTests(unittest.TestCase):
    """
    Phase V2.3 - enabled/min_severity/cooldown_minutes/recipients now
    come from a real NotificationSettingsManager against a temp
    config.db (not a mock) - these tests exercise the real
    validation/persistence path the admin Settings page also uses,
    not just a stand-in. Only smtp_host/port/tls/from_address remain
    on a mocked ConfigManager, since V2.3 explicitly never touches
    those.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.store = EventStore(database_path=self.tmpdir / "machine_data.db")
        self.log = NotificationLog(database_path=self.tmpdir / "machine_data.db")

        # audit_log is created by a different migrator in the real app
        # (database/initialize_config_db.py) - NotificationSettingsManager
        # itself only owns its own two tables, so seed the minimal table
        # it writes audit entries to, same precedent as
        # tests/test_user_manager_lockout.py's LockoutTestBase.
        config_db_path = self.tmpdir / "config.db"
        connection = sqlite3.connect(config_db_path)
        connection.execute(
            """CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, username TEXT, action TEXT,
                entity_type TEXT, entity_name TEXT, old_value TEXT, new_value TEXT, details TEXT
            )"""
        )
        connection.commit()
        connection.close()

        self.settings_manager = NotificationSettingsManager(database_path=config_db_path)
        self.marker_path = self.tmpdir / "cursor.txt"

    def _config(self):
        config = mock.Mock()
        config.smtp_host = "smtp.example.com"
        config.smtp_port = 587
        config.smtp_use_tls = True
        config.smtp_from_address = "alerts@example.com"
        return config

    def _settings(self, enabled=True, min_severity="alarm", cooldown_minutes=60.0, recipients=("engineer@example.com",)):
        self.settings_manager.update_settings(enabled, min_severity, cooldown_minutes, changed_by="tester")
        for existing in self.settings_manager.list_recipients():
            self.settings_manager.remove_recipient(existing["id"], changed_by="tester")
        for email in recipients:
            self.settings_manager.add_recipient(email, created_by="tester")
        return self.settings_manager

    def _insert_alarm(self, tag="Tag1", condition="high_alarm", severity="alarm", event_time=None):
        # A distinct event_time per call by default - machine_events'
        # own UNIQUE(event_time, tag, severity, condition) constraint
        # would otherwise silently no-op a second call with identical
        # values (INSERT OR IGNORE), which is exactly correct for the
        # real app but would hide bugs in these tests, whose whole
        # point is simulating the SAME alarm being re-detected/
        # re-inserted at a LATER event_time (e.g. after a restart).
        self._event_counter = getattr(self, "_event_counter", 0) + 1
        event_time = event_time or f"2026-08-21 13:{self._event_counter:02d}:00"
        return self.store.insert_event(
            {
                "event_time": event_time,
                "detected_at": event_time,
                "equipment": "Eq",
                "tag": tag,
                "severity": severity,
                "condition": condition,
                "value": 1.0,
            }
        )

    def test_disabled_config_does_nothing(self):
        self._insert_alarm()
        settings = self._settings(enabled=False)
        with mock.patch("app.notification_worker.send_email") as send:
            summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path)
        send.assert_not_called()
        self.assertEqual(summary, {"skipped": "disabled"})

    def test_no_new_events_reports_zero_checked(self):
        settings = self._settings()
        summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path)
        self.assertEqual(summary["checked"], 0)

    def test_qualifying_alarm_sends_and_advances_cursor(self):
        self._insert_alarm()
        settings = self._settings()
        with mock.patch("app.notification_worker.send_email", return_value=(True, None)) as send:
            summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 13, 5))

        send.assert_called_once()
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(notification_worker._read_cursor(self.marker_path), 1)

    def test_second_cycle_within_cooldown_does_not_resend(self):
        self._insert_alarm(tag="Tag1")
        settings = self._settings()
        with mock.patch("app.notification_worker.send_email", return_value=(True, None)):
            notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 13, 5))

        # Same (equipment, tag, condition) alarm re-inserted, as a
        # restart of event_monitor.py re-evaluating an already-active
        # alarm would do.
        self._insert_alarm(tag="Tag1")
        with mock.patch("app.notification_worker.send_email", return_value=(True, None)) as send:
            summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 13, 10))

        send.assert_not_called()
        self.assertEqual(summary["skipped_cooldown"], 1)

    def test_after_cooldown_expires_it_notifies_again(self):
        self._insert_alarm(tag="Tag1")
        settings = self._settings(cooldown_minutes=60)
        with mock.patch("app.notification_worker.send_email", return_value=(True, None)):
            notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 13, 0))

        self._insert_alarm(tag="Tag1")
        with mock.patch("app.notification_worker.send_email", return_value=(True, None)) as send:
            summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 14, 5))

        send.assert_called_once()
        self.assertEqual(summary["sent"], 1)

    def test_warning_is_skipped_when_min_severity_is_alarm(self):
        self._insert_alarm(severity="warning", condition="high_warning")
        settings = self._settings(min_severity="alarm")
        with mock.patch("app.notification_worker.send_email") as send:
            summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path)

        send.assert_not_called()
        self.assertEqual(summary["notifiable"], 0)
        # Non-notifiable events still advance the cursor - they're not retried forever.
        self.assertEqual(notification_worker._read_cursor(self.marker_path), 1)

    def test_send_failure_rolls_cursor_back_for_retry(self):
        self._insert_alarm(tag="Tag1")
        settings = self._settings()
        with mock.patch("app.notification_worker.send_email", return_value=(False, "connection refused")):
            summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 13, 0))

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["sent"], 0)
        # Cursor stays before the failed event, so it's retried next cycle.
        self.assertEqual(notification_worker._read_cursor(self.marker_path), 0)

    def test_failed_send_is_retried_and_succeeds_next_cycle(self):
        self._insert_alarm(tag="Tag1")
        settings = self._settings()
        with mock.patch("app.notification_worker.send_email", return_value=(False, "connection refused")):
            notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 13, 0))

        with mock.patch("app.notification_worker.send_email", return_value=(True, None)) as send:
            summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 13, 1))

        send.assert_called_once()
        self.assertEqual(summary["sent"], 1)

    def test_send_email_raising_is_treated_as_a_failure_not_a_crash(self):
        self._insert_alarm(tag="Tag1")
        settings = self._settings()
        with mock.patch("app.notification_worker.send_email", side_effect=RuntimeError("boom")):
            summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 13, 0))

        self.assertEqual(summary["failed"], 1)

    def test_no_recipients_configured_is_not_notifiable_as_a_send_failure(self):
        # An admin enabling notifications before adding any recipient
        # should not crash or silently report success - send_email()
        # itself already returns a clear (False, "no recipients
        # configured") for this, exercised for real here (not mocked).
        self._insert_alarm(tag="Tag1")
        settings = self._settings(recipients=())
        summary = notification_worker.run_cycle(self.store, self.log, self._config(), settings, self.marker_path, now=datetime(2026, 8, 21, 13, 0))

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["sent"], 0)


class CursorMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.marker_path = self.tmpdir / "cursor.txt"

    def test_missing_marker_defaults_to_zero(self):
        self.assertEqual(notification_worker._read_cursor(self.marker_path), 0)

    def test_write_then_read_round_trips(self):
        notification_worker._write_cursor(42, self.marker_path)
        self.assertEqual(notification_worker._read_cursor(self.marker_path), 42)

    def test_malformed_marker_defaults_to_zero_not_a_crash(self):
        self.marker_path.write_text("not a number", encoding="utf-8")
        self.assertEqual(notification_worker._read_cursor(self.marker_path), 0)


if __name__ == "__main__":
    unittest.main()
