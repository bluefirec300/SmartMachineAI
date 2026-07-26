import json
import sqlite3
from pathlib import Path
from typing import Any
from difflib import SequenceMatcher

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_KNOWLEDGE_PATH = (
    PROJECT_ROOT / "config" / "equipment_knowledge.json"
)

DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "database" / "config.db"
)


class EquipmentKnowledge:
    """
    Provides equipment definitions, aliases, and tag relationships.

    Primary source:
        database/config.db

    Temporary fallback:
        config/equipment_knowledge.json
    """

    def __init__(
        self,
        knowledge_path: Path | str = DEFAULT_KNOWLEDGE_PATH,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
        allow_json_fallback: bool = True,
    ):
        self.knowledge_path = Path(knowledge_path).resolve()
        self.database_path = Path(database_path).resolve()
        self.allow_json_fallback = allow_json_fallback

        self._source = ""
        self._equipment = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            equipment = self._load_from_database()

            if equipment:
                self._source = "database"
                return equipment

        except (
            FileNotFoundError,
            sqlite3.Error,
            ValueError,
        ):
            if not self.allow_json_fallback:
                raise

        if self.allow_json_fallback:
            equipment = self._load_from_json()
            self._source = "json"
            return equipment

        raise RuntimeError(
            "Configuration database contains no equipment knowledge "
            "and JSON fallback is disabled."
        )

    def _load_from_database(
        self,
    ) -> dict[str, dict[str, Any]]:
        if not self.database_path.exists():
            raise FileNotFoundError(
                f"Configuration database not found: "
                f"{self.database_path}"
            )

        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")

            equipment_rows = connection.execute(
                """
                SELECT
                    id,
                    name,
                    display_name,
                    description
                FROM equipment
                ORDER BY id
                """
            ).fetchall()

            if not equipment_rows:
                return {}

            alias_rows = connection.execute(
                """
                SELECT
                    equipment_id,
                    alias
                FROM equipment_aliases
                ORDER BY id
                """
            ).fetchall()

            tag_rows = connection.execute(
                """
                SELECT
                    et.equipment_id,
                    t.tag_name,
                    et.relationship_type,
                    et.display_order
                FROM equipment_tags AS et
                JOIN tags AS t
                    ON t.id = et.tag_id
                ORDER BY
                    et.equipment_id,
                    et.relationship_type,
                    et.display_order,
                    et.id
                """
            ).fetchall()

        aliases_by_equipment: dict[int, list[str]] = {}
        main_tags_by_equipment: dict[int, list[str]] = {}
        related_tags_by_equipment: dict[int, list[str]] = {}

        for row in alias_rows:
            equipment_id = int(row["equipment_id"])

            aliases_by_equipment.setdefault(
                equipment_id,
                [],
            ).append(str(row["alias"]))

        for row in tag_rows:
            equipment_id = int(row["equipment_id"])
            tag_name = str(row["tag_name"])
            relationship_type = str(row["relationship_type"])

            if relationship_type == "main":
                main_tags_by_equipment.setdefault(
                    equipment_id,
                    [],
                ).append(tag_name)

            elif relationship_type == "related":
                related_tags_by_equipment.setdefault(
                    equipment_id,
                    [],
                ).append(tag_name)

        equipment: dict[str, dict[str, Any]] = {}

        for row in equipment_rows:
            equipment_id = int(row["id"])
            equipment_name = str(row["name"])

            equipment[equipment_name] = {
                "display_name": row["display_name"],
                "description": row["description"],
                "aliases": aliases_by_equipment.get(
                    equipment_id,
                    [],
                ),
                "main_tags": main_tags_by_equipment.get(
                    equipment_id,
                    [],
                ),
                "related_tags": related_tags_by_equipment.get(
                    equipment_id,
                    [],
                ),
            }

        return equipment

    def _load_from_json(
        self,
    ) -> dict[str, dict[str, Any]]:
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

    def reload(self) -> None:
        self._equipment = self._load()

    def get_source(self) -> str:
        return self._source

    def get_all(self) -> dict[str, dict[str, Any]]:
        return {
            name: equipment.copy()
            for name, equipment in self._equipment.items()
        }

    def get(
        self,
        equipment_name: str,
    ) -> dict[str, Any] | None:
        equipment = self._equipment.get(equipment_name)

        if equipment is None:
            return None

        return equipment.copy()

    def get_aliases(
        self,
        equipment_name: str,
    ) -> list[str]:
        equipment = self.get(equipment_name)

        if equipment is None:
            return []

        aliases = equipment.get("aliases", [])

        return [
            str(alias).strip()
            for alias in aliases
            if str(alias).strip()
        ]

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
        text = question.lower().strip()

        exact_matches: list[tuple[int, str]] = []

        for equipment_name, equipment in self._equipment.items():
            aliases = equipment.get("aliases", [])

            for alias in aliases:
                normalized_alias = str(alias).lower().strip()

                if normalized_alias and normalized_alias in text:
                    exact_matches.append(
                        (
                            len(normalized_alias),
                            equipment_name,
                        )
                    )

        if exact_matches:
            # Prefer the longest exact phrase.
            exact_matches.sort(reverse=True)
            return exact_matches[0][1]

        words = [
            word.strip(".,?!:;()[]{}")
            for word in text.split()
        ]

        fuzzy_matches: list[tuple[float, int, str]] = []

        for equipment_name, equipment in self._equipment.items():
            aliases = equipment.get("aliases", [])

            for alias in aliases:
                normalized_alias = str(alias).lower().strip()

                # Avoid fuzzy matching very short aliases.
                if len(normalized_alias) < 5:
                    continue

                alias_words = normalized_alias.split()

                for index in range(len(words)):
                    candidate = " ".join(
                        words[index:index + len(alias_words)]
                    )

                    if not candidate:
                        continue

                    score = SequenceMatcher(
                        None,
                        candidate,
                        normalized_alias,
                    ).ratio()

                    if score >= 0.82:
                        fuzzy_matches.append(
                            (
                                score,
                                len(normalized_alias),
                                equipment_name,
                            )
                        )

        if not fuzzy_matches:
            return None

        # Prefer highest similarity, then longest alias.
        fuzzy_matches.sort(reverse=True)

        return fuzzy_matches[0][2]

_default_knowledge = EquipmentKnowledge()


def get_equipment_tags(
    equipment_name: str,
    include_related: bool = True,
) -> list[str]:
    return _default_knowledge.get_tags(
        equipment_name=equipment_name,
        include_related=include_related,
    )
