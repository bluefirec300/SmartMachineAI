import json
import sqlite3
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSON_PATH = PROJECT_ROOT / "config" / "equipment_knowledge.json"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"


class EquipmentKnowledgeImporter:
    def __init__(
        self,
        json_path: str | Path = DEFAULT_JSON_PATH,
        database_path: str | Path = DEFAULT_DATABASE_PATH,
    ):
        self.json_path = Path(json_path).resolve()
        self.database_path = Path(database_path).resolve()

    def load_source(self) -> dict[str, dict[str, Any]]:
        if not self.json_path.exists():
            raise FileNotFoundError(
                f"Equipment knowledge file not found: {self.json_path}"
            )

        data = json.loads(self.json_path.read_text())

        if not isinstance(data, dict):
            raise ValueError(
                "Equipment knowledge must contain a JSON object."
            )

        return data

    def import_all(self) -> dict[str, int]:
        knowledge = self.load_source()

        counts = {
            "equipment_inserted": 0,
            "equipment_updated": 0,
            "aliases_inserted": 0,
            "relationships_inserted": 0,
            "relationships_updated": 0,
        }

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.row_factory = sqlite3.Row

            for equipment_name, equipment_data in knowledge.items():
                equipment_id, inserted = self._upsert_equipment(
                    connection,
                    equipment_name,
                    equipment_data,
                )

                if inserted:
                    counts["equipment_inserted"] += 1
                else:
                    counts["equipment_updated"] += 1

                counts["aliases_inserted"] += self._insert_aliases(
                    connection,
                    equipment_id,
                    equipment_data.get("aliases", []),
                )

                counts["relationships_inserted"] += (
                    self._upsert_relationships(
                        connection,
                        equipment_id,
                        equipment_data.get("main_tags", []),
                        relationship_type="main",
                    )
                )

                counts["relationships_inserted"] += (
                    self._upsert_relationships(
                        connection,
                        equipment_id,
                        equipment_data.get("related_tags", []),
                        relationship_type="related",
                    )
                )

            connection.commit()

        return counts

    @staticmethod
    def _upsert_equipment(
        connection: sqlite3.Connection,
        equipment_name: str,
        equipment_data: dict[str, Any],
    ) -> tuple[int, bool]:
        existing = connection.execute(
            """
            SELECT id
            FROM equipment
            WHERE name = ?
            """,
            (equipment_name,),
        ).fetchone()

        display_name = equipment_data.get(
            "display_name",
            equipment_name.replace("_", " ").title(),
        )
        description = equipment_data.get("description")

        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO equipment (
                    name,
                    display_name,
                    description
                )
                VALUES (?, ?, ?)
                """,
                (
                    equipment_name,
                    display_name,
                    description,
                ),
            )
            return int(cursor.lastrowid), True

        equipment_id = int(existing["id"])

        connection.execute(
            """
            UPDATE equipment
            SET display_name = ?,
                description = ?
            WHERE id = ?
            """,
            (
                display_name,
                description,
                equipment_id,
            ),
        )

        return equipment_id, False

    @staticmethod
    def _insert_aliases(
        connection: sqlite3.Connection,
        equipment_id: int,
        aliases: list[str],
    ) -> int:
        inserted = 0

        for alias in aliases:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO equipment_aliases (
                    equipment_id,
                    alias
                )
                VALUES (?, ?)
                """,
                (
                    equipment_id,
                    alias.strip().lower(),
                ),
            )

            if cursor.rowcount == 1:
                inserted += 1

        return inserted

    @staticmethod
    def _upsert_relationships(
        connection: sqlite3.Connection,
        equipment_id: int,
        tag_names: list[str],
        relationship_type: str,
    ) -> int:
        inserted = 0

        for display_order, tag_name in enumerate(tag_names):
            tag = connection.execute(
                """
                SELECT id
                FROM tags
                WHERE tag_name = ?
                """,
                (tag_name,),
            ).fetchone()

            if tag is None:
                raise ValueError(
                    f"Tag not found in configuration database: {tag_name}"
                )

            cursor = connection.execute(
                """
                INSERT INTO equipment_tags (
                    equipment_id,
                    tag_id,
                    relationship_type,
                    display_order
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(
                    equipment_id,
                    tag_id,
                    relationship_type
                )
                DO UPDATE SET
                    display_order = excluded.display_order
                """,
                (
                    equipment_id,
                    int(tag["id"]),
                    relationship_type,
                    display_order,
                ),
            )

            if cursor.rowcount == 1:
                inserted += 1

        return inserted


def main() -> None:
    importer = EquipmentKnowledgeImporter()
    result = importer.import_all()

    print("Equipment knowledge import complete.")
    print()

    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
