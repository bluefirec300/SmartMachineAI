import tempfile
import unittest
from configparser import ConfigParser
from pathlib import Path

from config.config_manager import ConfigManager

"""
Backup readiness follow-up - focused tests for the historian
retention/backup configuration properties added in this and the prior
Phase 18.1a work. Confirms disabled-by-default is real, both against
the project's actual settings.ini and against a missing-key fallback.
"""


class TestHistorianBackupConfigDefaultsAgainstRealSettings(unittest.TestCase):
    """Integration check against the actual shipped config/settings.ini
    - not a fixture - proving the real, current configuration reads back
    correctly, not merely in an isolated test."""

    def test_backup_is_enabled_in_the_real_project_settings_ini(self):
        # Phase V1.2 - re-confirmed real disk headroom on this VM and
        # deliberately enabled backup for real (was disabled since the
        # original backup-readiness follow-up). This assertion tracks
        # that intentional, current state - not a regression guard for
        # the OLD disabled default (see TestHistorianBackupConfigFallbacks
        # below for the fallback-when-unconfigured behavior, which is
        # still correctly False/disabled-by-default).
        config = ConfigManager()
        self.assertTrue(config.historian_backup_enabled)

    def test_retention_remains_configured_and_unaffected(self):
        # Disabling backup must not disable retention - they are
        # independent settings.
        config = ConfigManager()
        self.assertEqual(config.historian_retention_days, 90)

    def test_minimum_free_reserve_is_a_positive_number_of_bytes(self):
        config = ConfigManager()
        self.assertGreater(config.historian_backup_minimum_free_reserve_bytes, 0)


class TestHistorianBackupConfigFallbacks(unittest.TestCase):
    """A settings.ini missing the [HISTORIAN_MAINTENANCE] section (or
    the backup_enabled key specifically) entirely - e.g. a partial
    upgrade, or a fresh file predating this feature - must still
    default to DISABLED, never silently enabled."""

    def _config_with(self, ini_text: str) -> ConfigManager:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        ini_path = Path(temp_dir.name) / "settings.ini"
        ini_path.write_text(ini_text, encoding="utf-8")

        config = ConfigManager.__new__(ConfigManager)
        config.project_root = Path(temp_dir.name)
        config.config_path = ini_path
        config.config = ConfigParser()
        config.config.read(ini_path)
        return config

    def test_missing_section_entirely_defaults_to_disabled(self):
        config = self._config_with("[DATABASE]\npath = database/machine_data.db\n")
        self.assertFalse(config.historian_backup_enabled)

    def test_section_present_but_key_missing_defaults_to_disabled(self):
        config = self._config_with("[HISTORIAN_MAINTENANCE]\nbackup_interval_hours = 24\n")
        self.assertFalse(config.historian_backup_enabled)

    def test_missing_reserve_key_defaults_to_a_positive_conservative_value(self):
        config = self._config_with("[HISTORIAN_MAINTENANCE]\nbackup_enabled = false\n")
        self.assertGreater(config.historian_backup_minimum_free_reserve_bytes, 0)

    def test_explicit_true_is_honored(self):
        # Confirms the flag is genuinely readable/settable - disabled-
        # by-default is a DEFAULT, not a hardcoded permanent False.
        config = self._config_with("[HISTORIAN_MAINTENANCE]\nbackup_enabled = true\n")
        self.assertTrue(config.historian_backup_enabled)


if __name__ == "__main__":
    unittest.main()
