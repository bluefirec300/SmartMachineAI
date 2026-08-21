import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from config.user_manager import MAX_FAILED_LOGIN_ATTEMPTS, UserManager

"""
Phase V1.3 - New-Factory Configuration & Security Readiness. Login-
attempt lockout tests. Per-username, not per-IP/global - deliberately
sensible-scoped for a small internal-tool user base, not enterprise
rate-limiting infrastructure. Never touches ROLES/RBAC.
"""


class LockoutTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db_path = self.tmpdir / "config.db"

        # audit_log is created by a different migrator in the real app
        # (config/initialize_config_db.py) - UserManager itself only
        # owns the users table, so seed the minimal table it writes to.
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, username TEXT, action TEXT,
                entity_type TEXT, entity_name TEXT, old_value TEXT, new_value TEXT, details TEXT
            )"""
        )
        connection.commit()
        connection.close()

        self.manager = UserManager(database_path=self.db_path)
        self.manager.create_user("bob", "Bob", "correct-pw", "engineer")


class TestLockoutDoesNotTriggerEarly(LockoutTestBase):
    def test_fewer_than_max_failed_attempts_does_not_lock(self):
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS - 1):
            self.assertIsNone(self.manager.authenticate("bob", "wrong"))
        self.assertFalse(self.manager.get_lockout_status("bob")["locked"])

    def test_correct_password_before_lockout_still_succeeds(self):
        self.manager.authenticate("bob", "wrong")
        self.manager.authenticate("bob", "wrong")
        result = self.manager.authenticate("bob", "correct-pw")
        self.assertIsNotNone(result)

    def test_successful_login_resets_the_failed_counter(self):
        self.manager.authenticate("bob", "wrong")
        self.manager.authenticate("bob", "wrong")
        self.manager.authenticate("bob", "correct-pw")  # resets counter
        # Now needs a FULL fresh MAX_FAILED_LOGIN_ATTEMPTS to lock, not just 1 more.
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS - 1):
            self.manager.authenticate("bob", "wrong")
        self.assertFalse(self.manager.get_lockout_status("bob")["locked"])


class TestLockoutTriggersAtThreshold(LockoutTestBase):
    def test_reaching_max_attempts_locks_the_account(self):
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
            self.manager.authenticate("bob", "wrong")
        self.assertTrue(self.manager.get_lockout_status("bob")["locked"])

    def test_locked_account_rejects_even_the_correct_password(self):
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
            self.manager.authenticate("bob", "wrong")
        result = self.manager.authenticate("bob", "correct-pw")
        self.assertIsNone(result)

    def test_lockout_status_reports_a_locked_until_timestamp(self):
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
            self.manager.authenticate("bob", "wrong")
        status = self.manager.get_lockout_status("bob")
        self.assertIsNotNone(status["locked_until"])

    def test_lockout_event_is_written_to_the_audit_log(self):
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
            self.manager.authenticate("bob", "wrong")
        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT action, entity_name FROM audit_log WHERE action = 'account_locked'"
        ).fetchone()
        connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "bob")


class TestLockoutRecovery(LockoutTestBase):
    def test_admin_password_reset_clears_lockout(self):
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
            self.manager.authenticate("bob", "wrong")
        self.assertTrue(self.manager.get_lockout_status("bob")["locked"])

        self.manager.reset_password("bob", "new-pw", changed_by="admin")

        self.assertFalse(self.manager.get_lockout_status("bob")["locked"])
        self.assertIsNotNone(self.manager.authenticate("bob", "new-pw"))


class TestLockoutDoesNotAffectRoles(LockoutTestBase):
    def test_lockout_never_changes_role_or_active_flag(self):
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
            self.manager.authenticate("bob", "wrong")
        user = self.manager.get_user("bob")
        self.assertEqual(user["role"], "engineer")
        self.assertEqual(user["active"], 1)

    def test_get_lockout_status_is_read_only_and_never_counts_as_an_attempt(self):
        for _ in range(10):
            self.manager.get_lockout_status("bob")
        self.assertFalse(self.manager.get_lockout_status("bob")["locked"])

    def test_unknown_username_reports_not_locked_never_raises(self):
        status = self.manager.get_lockout_status("nonexistent")
        self.assertFalse(status["locked"])


class TestLoginFormWiring(unittest.TestCase):
    """
    A live AppTest of the login form would need to run 5 failed logins
    against a REAL account in the real config.db to prove the lockout
    message appears - which would leave that real account's lockout
    counters mutated afterward. Deliberately avoided; a static source
    check instead confirms the wiring is present without touching any
    real user's data. The actual lockout BEHAVIOR is fully covered
    above against isolated temp databases.
    """

    def test_login_form_checks_lockout_status_before_authenticating(self):
        source = (Path(__file__).resolve().parent.parent / "ui" / "auth.py").read_text(encoding="utf-8")
        lockout_check_pos = source.index("get_lockout_status")
        authenticate_pos = source.index("manager.authenticate(username, password)")
        self.assertLess(lockout_check_pos, authenticate_pos, "lockout must be checked BEFORE attempting authentication")
        self.assertIn("temporarily locked", source)


if __name__ == "__main__":
    unittest.main()
