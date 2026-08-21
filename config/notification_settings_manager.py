from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from config.environment import get_config_db_path

DEFAULT_DATABASE_PATH = get_config_db_path()

MIN_SEVERITY_OPTIONS = ("alarm", "warning")

# Deliberately simple, practical validation (not full RFC 5322) -
# rejects the obviously malformed (missing @, no domain, whitespace)
# without trying to be a complete email grammar. Consistent with how
# most admin UIs validate this field.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(address: str) -> bool:
    return bool(_EMAIL_PATTERN.match((address or "").strip()))


def _utc_timestamp() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


class NotificationSettingsManager:
    """
    Phase V2.3 - admin-editable alarm notification settings
    (enabled/min_severity/cooldown_minutes) and recipient list, stored
    in config.db so the new Alarm Notification Settings page can
    change them live with an audit trail - exactly the same reasoning
    PLCConnectionManager already established for PLC connections
    ("so the admin page can change it live... without editing a text
    file on the server").

    Deliberately does NOT store SMTP host/port/TLS/from-address or any
    credential - those remain server configuration only
    (config/settings.ini's [NOTIFICATIONS] section for the non-secret
    ones, SMTP_USERNAME/SMTP_PASSWORD environment variables for the
    secret ones), read directly by app/notification_worker.py via
    config.config_manager.ConfigManager, untouched by this class.
    """

    def __init__(self, database_path: Path | str = DEFAULT_DATABASE_PATH):
        self.database_path = Path(database_path).resolve()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        connection = self._connect()

        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_settings
                (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    min_severity TEXT NOT NULL DEFAULT 'alarm',
                    cooldown_minutes REAL NOT NULL DEFAULT 60.0,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT
                )
                """
            )

            # Single settings row, seeded once with the same
            # safe/disabled defaults V2.2 shipped in settings.ini -
            # never a silent opt-in just because this table now exists.
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_settings
                (id, enabled, min_severity, cooldown_minutes, updated_at, updated_by)
                VALUES (1, 0, 'alarm', 60.0, ?, NULL)
                """,
                (_utc_timestamp(),),
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_recipients
                (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    created_by TEXT
                )
                """
            )

            connection.commit()
        finally:
            connection.close()

    def _write_audit_log(
        self, connection: sqlite3.Connection, username: str, action: str,
        entity_type: str, entity_name: str, details: str = "",
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log
            (timestamp, username, action, entity_type, entity_name, old_value, new_value, details)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (_utc_timestamp(), username, action, entity_type, entity_name, details or None),
        )

    # ------------------------------------------------------------------ #
    # Settings (enabled / min_severity / cooldown_minutes)
    # ------------------------------------------------------------------ #

    def get_settings(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT enabled, min_severity, cooldown_minutes, updated_at, updated_by "
                "FROM notification_settings WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()

        return {
            "enabled": bool(row["enabled"]),
            "min_severity": row["min_severity"],
            "cooldown_minutes": row["cooldown_minutes"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    def update_settings(
        self, enabled: bool, min_severity: str, cooldown_minutes: float, changed_by: str,
    ) -> dict[str, Any]:
        if min_severity not in MIN_SEVERITY_OPTIONS:
            raise ValueError(f"min_severity must be one of {MIN_SEVERITY_OPTIONS}, got {min_severity!r}")

        if cooldown_minutes <= 0:
            raise ValueError("cooldown_minutes must be a positive number")

        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE notification_settings
                SET enabled = ?, min_severity = ?, cooldown_minutes = ?, updated_at = ?, updated_by = ?
                WHERE id = 1
                """,
                (int(bool(enabled)), min_severity, float(cooldown_minutes), _utc_timestamp(), changed_by),
            )
            self._write_audit_log(
                connection, changed_by, "update_notification_settings", "notification_settings", "settings",
                details=f"enabled={enabled}, min_severity={min_severity}, cooldown_minutes={cooldown_minutes}",
            )
            connection.commit()
        finally:
            connection.close()

        return self.get_settings()

    # ------------------------------------------------------------------ #
    # Recipients
    # ------------------------------------------------------------------ #

    def list_recipients(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, email, created_at, created_by FROM notification_recipients ORDER BY email"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def add_recipient(self, email: str, created_by: str) -> dict[str, Any]:
        email = (email or "").strip()

        if not is_valid_email(email):
            raise ValueError(f"{email!r} is not a valid email address")

        connection = self._connect()
        try:
            existing = connection.execute(
                "SELECT id FROM notification_recipients WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"{email} is already a recipient")

            cursor = connection.execute(
                "INSERT INTO notification_recipients (email, created_at, created_by) VALUES (?, ?, ?)",
                (email, _utc_timestamp(), created_by),
            )
            self._write_audit_log(
                connection, created_by, "add_notification_recipient", "notification_recipient", email,
            )
            connection.commit()

            return dict(
                connection.execute(
                    "SELECT id, email, created_at, created_by FROM notification_recipients WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            )
        finally:
            connection.close()

    def remove_recipient(self, recipient_id: int, changed_by: str) -> None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT email FROM notification_recipients WHERE id = ?", (recipient_id,)
            ).fetchone()

            if row is None:
                return

            connection.execute("DELETE FROM notification_recipients WHERE id = ?", (recipient_id,))
            self._write_audit_log(
                connection, changed_by, "remove_notification_recipient", "notification_recipient", row["email"],
            )
            connection.commit()
        finally:
            connection.close()
