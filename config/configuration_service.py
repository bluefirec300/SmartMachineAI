from pathlib import Path
from threading import RLock

from config.configuration_manager import (
    DEFAULT_DATABASE_PATH,
    ConfigurationManager,
)


class ConfigurationService:
    """
    Provides one shared ConfigurationManager instance.

    The service can recreate the manager when the configuration database
    location changes or when an application requests a reload.
    """

    def __init__(
        self,
        database_path: str | Path = DEFAULT_DATABASE_PATH,
    ):
        self._database_path = Path(database_path).resolve()
        self._lock = RLock()
        self._manager: ConfigurationManager | None = None

    @property
    def database_path(self) -> Path:
        return self._database_path

    def get_manager(self) -> ConfigurationManager:
        with self._lock:
            if self._manager is None:
                self._manager = ConfigurationManager(
                    database_path=self._database_path
                )

            return self._manager

    def reload(self) -> ConfigurationManager:
        """
        Recreate the shared manager.

        ConfigurationManager currently reads directly from SQLite for each
        operation, so reload is mainly preparation for future caching.
        """
        with self._lock:
            self._manager = ConfigurationManager(
                database_path=self._database_path
            )

            return self._manager

    def set_database_path(
        self,
        database_path: str | Path,
    ) -> ConfigurationManager:
        """
        Change the configuration database used by the shared service.
        """
        with self._lock:
            self._database_path = Path(database_path).resolve()
            self._manager = ConfigurationManager(
                database_path=self._database_path
            )

            return self._manager

    def is_available(self) -> bool:
        return self._database_path.exists()


configuration_service = ConfigurationService()


def get_configuration() -> ConfigurationManager:
    """
    Return the application's shared ConfigurationManager.
    """
    return configuration_service.get_manager()
