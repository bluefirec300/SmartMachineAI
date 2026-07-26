import json
from pathlib import Path
from typing import Any

from config.configuration_manager import ConfigurationManager
from config.configuration_service import get_configuration


class TagRegistry:
    """
    Provides logical PLC tag definitions to the application.

    Primary source:
        database/config.db

    Temporary fallback:
        config/tag_registry.json

    The public methods remain compatible with the original JSON-based
    TagRegistry so the logger, router, observation engine, and rule engine
    do not need to change.
    """

    def __init__(
        self,
        registry_path: str | Path | None = None,
        database_path: str | Path | None = None,
        allow_json_fallback: bool = True,
    ):
        project_root = Path(__file__).resolve().parent.parent

        self.registry_path = (
            Path(registry_path)
            if registry_path
            else project_root / "config" / "tag_registry.json"
        )

        self.database_path = (
            Path(database_path)
            if database_path
            else project_root / "database" / "config.db"
        )

        self.allow_json_fallback = allow_json_fallback
        self.source = ""

        self._tags = self._load()

    def _load(self) -> list[dict[str, Any]]:
        """
        Load tags from config.db.

        If the database is unavailable or contains no tags, optionally
        fall back to tag_registry.json.
        """
        database_error: Exception | None = None

        try:
            tags = self._load_from_database()

            if tags:
                self.source = "database"
                self._validate(tags)
                return tags

        except Exception as error:
            database_error = error

        if self.allow_json_fallback:
            tags = self._load_from_json()
            self.source = "json"
            self._validate(tags)
            return tags

        if database_error is not None:
            raise RuntimeError(
                "Unable to load tag registry from configuration database."
            ) from database_error

        raise RuntimeError(
            "Configuration database contains no tags and JSON fallback "
            "is disabled."
        )

    def _load_from_database(self) -> list[dict[str, Any]]:
        if not self.database_path.exists():
            raise FileNotFoundError(
                f"Configuration database not found: {self.database_path}"
            )

        default_database_path = (
            Path(__file__).resolve().parent.parent
            / "database"
            / "config.db"
        ).resolve()

        if self.database_path.resolve() == default_database_path:
            config = get_configuration()
        else:
            config = ConfigurationManager(
                database_path=self.database_path
            )

        database_tags = config.get_tags(
            enabled_only=False
        )

        database_addresses = config.get_tag_addresses(
            enabled_only=True
        )

        addresses_by_tag: dict[str, dict[str, str]] = {}

        for address_record in database_addresses:
            tag_name = address_record["tag_name"]
            driver = address_record["driver"]
            address = address_record["address"]

            addresses_by_tag.setdefault(
                tag_name,
                {},
            )[driver] = address

        tags: list[dict[str, Any]] = []

        for database_tag in database_tags:
            tag_name = database_tag["tag_name"]

            tag = {
                "name": tag_name,
                "description": (
                    database_tag.get("description") or ""
                ),
                "unit": database_tag.get("unit") or "",
                "data_type": (
                    database_tag.get("data_type") or "REAL"
                ),
                "enabled": bool(
                    database_tag.get("enabled", 0)
                ),
                "addresses": addresses_by_tag.get(
                    tag_name,
                    {},
                ),
            }

            equipment_name = database_tag.get(
                "equipment_name"
            )

            if equipment_name:
                tag["equipment"] = equipment_name

            tags.append(tag)

        return tags

    def _load_from_json(self) -> list[dict[str, Any]]:
        if not self.registry_path.exists():
            raise FileNotFoundError(
                f"Tag registry not found: {self.registry_path}"
            )

        with self.registry_path.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if not isinstance(data, list):
            raise ValueError(
                "Tag registry must contain a JSON list."
            )

        return data

    def _validate(
        self,
        tags: list[dict[str, Any]],
    ) -> None:
        required_fields = {
            "name",
            "data_type",
            "enabled",
            "addresses",
        }

        names: set[str] = set()

        for index, tag in enumerate(tags):
            missing = required_fields - tag.keys()

            if missing:
                raise ValueError(
                    f"Tag at index {index} is missing fields: "
                    f"{sorted(missing)}"
                )

            name = tag["name"]

            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"Tag at index {index} has an invalid name."
                )

            if name in names:
                raise ValueError(
                    f"Duplicate tag name: {name}"
                )

            names.add(name)

            if not isinstance(tag["addresses"], dict):
                raise ValueError(
                    f"Addresses for tag '{name}' "
                    "must be an object."
                )

    def reload(self) -> None:
        """
        Reload tags from the configuration source.

        This can be called after configuration changes without restarting
        the entire application.
        """
        self._tags = self._load()

    def get_source(self) -> str:
        """
        Return 'database' or 'json' depending on the active source.
        """
        return self.source

    def get_all(
        self,
        enabled_only: bool = True,
    ) -> list[dict[str, Any]]:
        if enabled_only:
            return [
                tag.copy()
                for tag in self._tags
                if tag.get("enabled", False)
            ]

        return [
            tag.copy()
            for tag in self._tags
        ]

    def get(
        self,
        name: str,
    ) -> dict[str, Any]:
        for tag in self._tags:
            if tag["name"] == name:
                return tag.copy()

        raise KeyError(
            f"Unknown tag: {name}"
        )

    def get_address(
        self,
        name: str,
        driver: str,
    ) -> str:
        tag = self.get(name)
        addresses = tag["addresses"]

        if driver not in addresses:
            raise KeyError(
                f"Tag '{name}' has no address "
                f"for driver '{driver}'."
            )

        return addresses[driver]

    def get_enabled_for_driver(
        self,
        driver: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        for tag in self.get_all(
            enabled_only=True
        ):
            address = tag["addresses"].get(driver)

            if address:
                item = tag.copy()
                item["address"] = address
                result.append(item)

        return result
