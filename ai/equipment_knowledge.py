import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KNOWLEDGE_PATH = (
    PROJECT_ROOT / "config" / "equipment_knowledge.json"
)


class EquipmentKnowledge:
    def __init__(
        self,
        knowledge_path: Path | str = DEFAULT_KNOWLEDGE_PATH,
    ):
        self.knowledge_path = Path(knowledge_path).resolve()
        self._equipment = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.knowledge_path.exists():
            raise FileNotFoundError(
                f"Equipment knowledge file not found: "
                f"{self.knowledge_path}"
            )

        with self.knowledge_path.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError(
                "Equipment knowledge must contain a JSON object."
            )

        return data

    def get_all(self) -> dict[str, dict[str, Any]]:
        return self._equipment.copy()

    def get(
        self,
        equipment_name: str,
    ) -> dict[str, Any] | None:
        equipment = self._equipment.get(equipment_name)

        if equipment is None:
            return None

        return equipment.copy()

    def get_tags(
        self,
        equipment_name: str,
        include_related: bool = True,
    ) -> list[str]:
        equipment = self.get(equipment_name)

        if equipment is None:
            return []

        tags = list(equipment.get("main_tags", []))

        if include_related:
            tags.extend(equipment.get("related_tags", []))

        return list(dict.fromkeys(tags))

    def find_equipment(
        self,
        question: str,
    ) -> str | None:
        text = question.lower()

        matches: list[tuple[int, str]] = []

        for equipment_name, equipment in self._equipment.items():
            aliases = equipment.get("aliases", [])

            for alias in aliases:
                normalized_alias = str(alias).lower().strip()

                if normalized_alias and normalized_alias in text:
                    matches.append(
                        (
                            len(normalized_alias),
                            equipment_name,
                        )
                    )

        if not matches:
            return None

        # Prefer the longest matching phrase.
        matches.sort(reverse=True)

        return matches[0][1]


_default_knowledge = EquipmentKnowledge()


def get_equipment_tags(
    equipment_name: str,
    include_related: bool = True,
) -> list[str]:
    return _default_knowledge.get_tags(
        equipment_name=equipment_name,
        include_related=include_related,
    )
