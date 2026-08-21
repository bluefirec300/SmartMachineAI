from configparser import ConfigParser
from pathlib import Path


class ConfigManager:

    def __init__(self):

        self.project_root = Path(__file__).resolve().parent.parent

        self.config_path = (
            self.project_root
            / "config"
            / "settings.ini"
        )

        self.config = ConfigParser()

        self.config.read(self.config_path)

    @property
    def machine_name(self):
        return self.config["MACHINE"]["name"]

    @property
    def driver(self):
        return self.config["DATA_SOURCE"]["driver"].lower()

    @property
    def scan_interval(self):
        return self.config.getint(
            "DATA_SOURCE",
            "scan_interval"
        )

    @property
    def database_path(self):

        return (
            self.project_root
            / self.config["DATABASE"]["path"]
        )

    @property
    def historian_retention_days(self) -> int:
        """
        How many days of raw plc_data history to keep - the value
        Phase 18's audit found sitting in settings.ini but never
        actually read by any code. Now the single source of truth
        DatabaseManager.cleanup() is called with (see
        app/historian_maintenance_worker.py) - never a hardcoded
        constant elsewhere.
        """
        return self.config.getint("DATABASE", "retention_days", fallback=90)

    @property
    def historian_retention_check_interval_hours(self) -> float:
        return self.config.getfloat(
            "HISTORIAN_MAINTENANCE", "retention_check_interval_hours", fallback=24.0
        )

    @property
    def historian_backup_enabled(self) -> bool:
        """
        Backup readiness follow-up - fallback is deliberately False, not
        True. If the [HISTORIAN_MAINTENANCE] section or this key is ever
        missing (a fresh settings.ini, a partial upgrade), automatic
        backup must stay OFF by default - it must never be silently
        switched on by an application startup or a software upgrade.
        Enabling it is always an explicit, intentional edit to
        settings.ini.
        """
        return self.config.getboolean(
            "HISTORIAN_MAINTENANCE", "backup_enabled", fallback=False
        )

    @property
    def historian_backup_interval_hours(self) -> float:
        return self.config.getfloat(
            "HISTORIAN_MAINTENANCE", "backup_interval_hours", fallback=24.0
        )

    @property
    def historian_backup_destination_dir(self) -> Path:
        return (
            self.project_root
            / self.config.get("HISTORIAN_MAINTENANCE", "backup_destination_dir", fallback="backups")
        )

    @property
    def historian_backup_retention_count(self) -> int:
        return self.config.getint(
            "HISTORIAN_MAINTENANCE", "backup_retention_count", fallback=7
        )

    @property
    def historian_backup_minimum_free_reserve_bytes(self) -> int:
        gigabytes = self.config.getfloat(
            "HISTORIAN_MAINTENANCE", "backup_minimum_free_reserve_gb", fallback=5.0
        )
        return int(gigabytes * (1000 ** 3))

    @property
    def notifications_enabled(self) -> bool:
        """
        Same "must never be silently switched on" reasoning as
        historian_backup_enabled above - fallback is deliberately
        False.

        Phase V2.3: app/notification_worker.py no longer reads this -
        it reads config.notification_settings_manager.NotificationSettingsManager
        (config.db) instead, so an admin can change it live from the
        Alarm Notification Settings page. Kept here as the schema's
        seed default and for anything that still wants the
        settings.ini-configured value directly.
        """
        return self.config.getboolean(
            "NOTIFICATIONS", "enabled", fallback=False
        )

    @property
    def notification_min_severity(self) -> str:
        """Superseded by NotificationSettingsManager as of Phase V2.3 - see notifications_enabled's docstring."""
        return self.config.get(
            "NOTIFICATIONS", "min_severity", fallback="alarm"
        ).strip().lower()

    @property
    def notification_cooldown_minutes(self) -> float:
        """Superseded by NotificationSettingsManager as of Phase V2.3 - see notifications_enabled's docstring."""
        return self.config.getfloat(
            "NOTIFICATIONS", "cooldown_minutes", fallback=60.0
        )

    @property
    def notification_recipients(self) -> list[str]:
        """Superseded by NotificationSettingsManager as of Phase V2.3 - see notifications_enabled's docstring."""
        raw = self.config.get("NOTIFICATIONS", "recipients", fallback="")
        return [address.strip() for address in raw.split(",") if address.strip()]

    @property
    def notification_poll_interval_seconds(self) -> float:
        return self.config.getfloat(
            "NOTIFICATIONS", "poll_interval_seconds", fallback=30.0
        )

    @property
    def smtp_host(self) -> str:
        return self.config.get("NOTIFICATIONS", "smtp_host", fallback="").strip()

    @property
    def smtp_port(self) -> int:
        return self.config.getint("NOTIFICATIONS", "smtp_port", fallback=587)

    @property
    def smtp_use_tls(self) -> bool:
        return self.config.getboolean("NOTIFICATIONS", "smtp_use_tls", fallback=True)

    @property
    def smtp_from_address(self) -> str:
        return self.config.get("NOTIFICATIONS", "smtp_from_address", fallback="").strip()

    @property
    def fins_ip(self):
        return self.config["FINS"]["ip"]

    @property
    def fins_port(self):
        return self.config.getint(
            "FINS",
            "port"
        )

    @property
    def plc_node(self):
        return self.config.getint(
            "FINS",
            "plc_node"
        )

    @property
    def pc_node(self):
        return self.config.getint(
            "FINS",
            "pc_node"
        )

    @property
    def ai_provider(self):
        return self.config["AI"]["provider"]

    @property
    def ai_model(self):
        return self.config["AI"]["model"]

    @property
    def ollama_url(self):
        return self.config["OLLAMA"]["url"]

    @property
    def ollama_model(self):
        return self.config["OLLAMA"]["model"]
