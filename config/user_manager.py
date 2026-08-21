from __future__ import annotations
from config.environment import get_config_db_path

import hashlib
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

ROLES = ("admin", "engineer", "operator")

# PBKDF2-HMAC-SHA256, stdlib only (no bcrypt/passlib dependency for
# what is currently a small internal-tool user base). 200k iterations
# matches OWASP's current minimum recommendation for this algorithm.
PBKDF2_ITERATIONS = 200_000

# Phase V1.3 - login-attempt protection. Per-username lockout (not
# per-IP/global - this is a small internal-tool user base per an
# equipment-monitoring department, not a public-facing service; a
# global rate limiter is deliberately out of scope for V1, matching
# "sensible" protection rather than enterprise-grade infrastructure).
# Does NOT touch ROLES/RBAC in any way - purely an authentication-
# attempt counter alongside the existing password check.
MAX_FAILED_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PBKDF2_ITERATIONS,
    ).hex()


def _utc_timestamp() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


class UserManager:
    """
    Login accounts and role-based access (admin/engineer/operator).

    Passwords are stored as PBKDF2-HMAC-SHA256 hashes with a random
    per-user salt - never in plain text, even for the default "123456"
    accounts created at rollout. Every create/role-change/password-
    reset is written to the existing `audit_log` table (never the
    password itself), the same table ConfigurationManager already uses
    for threshold/maintenance changes.
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
            existing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }

            # A `users` table already existed (0 rows, nothing in the
            # codebase referenced it - dead scaffolding from earlier
            # work) with an older schema/role vocabulary
            # (operator/maintenance/engineer/administrator, single
            # password_hash column with no separate salt). Since it's
            # empty, replace it outright rather than trying to migrate
            # data that doesn't exist - but back up first regardless,
            # matching this project's other migrators.
            if existing_columns and "display_name" not in existing_columns:
                backup_path = self.database_path.with_name(
                    f"{self.database_path.stem}_before_users_table_rebuild_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    f"{self.database_path.suffix}"
                )
                shutil.copy2(self.database_path, backup_path)
                connection.execute("DROP TABLE users")

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users
                (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_login_at TEXT
                )
                """
            )

            # Phase V1.3 - additive-only, idempotent (same convention as
            # every other *_migrator.py in this project). RBAC columns
            # above (role, active) are untouched - these two are purely
            # about failed-attempt tracking.
            users_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "failed_login_attempts" not in users_columns:
                connection.execute("ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0")
            if "locked_until" not in users_columns:
                connection.execute("ALTER TABLE users ADD COLUMN locked_until TEXT")

            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None

        data = dict(row)
        data.pop("password_hash", None)
        data.pop("password_salt", None)
        return data

    def _write_audit_log(
        self,
        connection: sqlite3.Connection,
        username: str,
        action: str,
        entity_name: str,
        details: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log
            (timestamp, username, action, entity_type, entity_name, old_value, new_value, details)
            VALUES (?, ?, ?, 'user', ?, NULL, NULL, ?)
            """,
            (_utc_timestamp(), username, action, entity_name, details),
        )

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        role: str,
        created_by: str = "system",
    ) -> dict[str, Any]:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {role!r}")

        if not username.strip():
            raise ValueError("username is required.")

        if not password:
            raise ValueError("password is required.")

        salt = secrets.token_hex(16)
        password_hash = _hash_password(password, salt)

        connection = self._connect()

        try:
            cursor = connection.execute(
                """
                INSERT INTO users
                (username, display_name, password_hash, password_salt, role, active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    username.strip().lower(),
                    display_name.strip(),
                    password_hash,
                    salt,
                    role,
                    _utc_timestamp(),
                ),
            )

            self._write_audit_log(
                connection,
                username=created_by,
                action="create_user",
                entity_name=username.strip().lower(),
                details=f"role={role}",
            )

            connection.commit()

            return self._row_to_dict(
                connection.execute(
                    "SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
            )
        finally:
            connection.close()

    @staticmethod
    def _is_locked(row: sqlite3.Row) -> bool:
        locked_until = row["locked_until"]
        if not locked_until:
            return False
        try:
            return datetime.utcnow() < datetime.fromisoformat(locked_until)
        except ValueError:
            return False

    def get_lockout_status(self, username: str) -> dict[str, Any]:
        """
        Read-only - never modifies state and never counts as an
        attempt. `ui/auth.py` calls this BEFORE attempting a password
        check, so a locked account can be told apart from a wrong
        password without authenticate() needing a second return shape
        (it stays a plain dict | None, unchanged for any other caller).
        """
        connection = self._connect()

        try:
            row = connection.execute(
                "SELECT locked_until, failed_login_attempts FROM users WHERE username = ?",
                (username.strip().lower(),),
            ).fetchone()

            if row is None or not self._is_locked(row):
                return {"locked": False, "locked_until": None}

            return {"locked": True, "locked_until": row["locked_until"]}
        finally:
            connection.close()

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """
        Unchanged return contract (dict | None) for every existing
        caller. A locked account returns None here too (same as a
        wrong password) - callers that want to DISTINGUISH "locked" from
        "wrong password" for messaging should check get_lockout_status()
        first, as ui/auth.py's login form now does.
        """
        connection = self._connect()

        try:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? AND active = 1",
                (username.strip().lower(),),
            ).fetchone()

            if row is None:
                return None

            if self._is_locked(row):
                return None

            expected_hash = _hash_password(password, row["password_salt"])

            if not secrets.compare_digest(expected_hash, row["password_hash"]):
                self._record_failed_attempt(connection, row)
                connection.commit()
                return None

            connection.execute(
                "UPDATE users SET last_login_at = ?, failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
                (_utc_timestamp(), row["id"]),
            )
            connection.commit()

            return self._row_to_dict(row)
        finally:
            connection.close()

    def _record_failed_attempt(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        attempts = (row["failed_login_attempts"] or 0) + 1
        locked_until = None

        if attempts >= MAX_FAILED_LOGIN_ATTEMPTS:
            locked_until = (datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")

        connection.execute(
            "UPDATE users SET failed_login_attempts = ?, locked_until = ? WHERE id = ?",
            (attempts, locked_until, row["id"]),
        )

        if locked_until:
            self._write_audit_log(
                connection,
                username="system",
                action="account_locked",
                entity_name=row["username"],
                details=f"locked after {attempts} failed login attempts, until {locked_until} UTC",
            )

    def get_users(self) -> list[dict[str, Any]]:
        connection = self._connect()

        try:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY role, display_name"
            ).fetchall()

            return [self._row_to_dict(row) for row in rows]
        finally:
            connection.close()

    def get_user(self, username: str) -> dict[str, Any] | None:
        connection = self._connect()

        try:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?",
                (username.strip().lower(),),
            ).fetchone()

            return self._row_to_dict(row)
        finally:
            connection.close()

    def update_role(self, username: str, new_role: str, changed_by: str) -> None:
        if new_role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {new_role!r}")

        connection = self._connect()

        try:
            connection.execute(
                "UPDATE users SET role = ? WHERE username = ?",
                (new_role, username.strip().lower()),
            )

            self._write_audit_log(
                connection,
                username=changed_by,
                action="update_role",
                entity_name=username.strip().lower(),
                details=f"new_role={new_role}",
            )

            connection.commit()
        finally:
            connection.close()

    def set_active(self, username: str, active: bool, changed_by: str) -> None:
        connection = self._connect()

        try:
            connection.execute(
                "UPDATE users SET active = ? WHERE username = ?",
                (1 if active else 0, username.strip().lower()),
            )

            self._write_audit_log(
                connection,
                username=changed_by,
                action="set_active" if active else "deactivate_user",
                entity_name=username.strip().lower(),
                details=f"active={active}",
            )

            connection.commit()
        finally:
            connection.close()

    def reset_password(self, username: str, new_password: str, changed_by: str) -> None:
        if not new_password:
            raise ValueError("new_password is required.")

        salt = secrets.token_hex(16)
        password_hash = _hash_password(new_password, salt)

        connection = self._connect()

        try:
            # Also clears any lockout - an admin resetting the password
            # is a deliberate, authenticated intervention that should
            # un-stick a locked account, not leave it locked against the
            # very password they just set.
            connection.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, failed_login_attempts = 0, locked_until = NULL WHERE username = ?",
                (password_hash, salt, username.strip().lower()),
            )

            self._write_audit_log(
                connection,
                username=changed_by,
                action="reset_password",
                entity_name=username.strip().lower(),
            )

            connection.commit()
        finally:
            connection.close()
