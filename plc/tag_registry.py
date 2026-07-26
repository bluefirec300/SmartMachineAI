import json
from pathlib import Path
from typing import Any


class TagRegistry:
    def __init__(self, registry_path: str | Path | None = None):
        project_root = Path(__file__).resolve().parent.parent

        self.registry_path = (
            Path(registry_path)
            if registry_path
            else project_root / "config" / "tag_registry.json"
        )

        self._tags = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self.registry_path.exists():
            raise FileNotFoundError(
                f"Tag registry not found: {self.registry_path}"
            )

        with self.registry_path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            raise ValueError("Tag registry must contain a JSON list.")

        self._validate(data)
        return data

    def _validate(self, tags: list[dict[str, Any]]) -> None:
        required_fields = {
            "name",
            "data_type",
            "enabled",
            "addresses",
        }

        names = set()

        for index, tag in enumerate(tags):
            missing = required_fields - tag.keys()

            if missing:
                raise ValueError(
                    f"Tag at index {index} is missing fields: "
                    f"{sorted(missing)}"
                )

            name = tag["name"]

            if name in names:
                raise ValueError(f"Duplicate tag name: {name}")

            names.add(name)

            if not isinstance(tag["addresses"], dict):
                raise ValueError(
                    f"Addresses for tag '{name}' must be an object."
                )

    def get_all(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        if enabled_only:
            return [
                tag.copy()
                for tag in self._tags
                if tag.get("enabled", False)
            ]

        return [tag.copy() for tag in self._tags]

    def get(self, name: str) -> dict[str, Any]:
        for tag in self._tags:
            if tag["name"] == name:
                return tag.copy()

        raise KeyError(f"Unknown tag: {name}")

    def get_address(self, name: str, driver: str) -> str:
        tag = self.get(name)
        addresses = tag["addresses"]

        if driver not in addresses:
            raise KeyError(
                f"Tag '{name}' has no address for driver '{driver}'."
            )

        return addresses[driver]

    def get_enabled_for_driver(
        self,
        driver: str,
    ) -> list[dict[str, Any]]:
        result = []

        for tag in self.get_all(enabled_only=True):
            address = tag["addresses"].get(driver)

            if address:
                item = tag.copy()
                item["address"] = address
                result.append(item)

        return result
