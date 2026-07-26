import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "database" / "config.db"
)


class ConfigurationManager:
    def __init__(
        self,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
    ):
        self.database_path = Path(database_path).resolve()

        if not self.database_path.exists():
            raise FileNotFoundError(
                "Configuration database not found: "
                f"{self.database_path}\n"
                "Run: python -m database.initialize_config_db"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        return connection

    @staticmethod
    def _row_to_dict(
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None

        return dict(row)

    @staticmethod
    def _utc_timestamp() -> str:
        return datetime.utcnow().isoformat(
            timespec="seconds"
        )

    def write_audit_log(
        self,
        username: str,
        action: str,
        entity_type: str | None = None,
        entity_name: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
        details: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        owns_connection = connection is None

        if connection is None:
            connection = self._connect()

        try:
            connection.execute(
                """
                INSERT INTO audit_log (
                    timestamp,
                    username,
                    action,
                    entity_type,
                    entity_name,
                    old_value,
                    new_value,
                    details
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._utc_timestamp(),
                    username,
                    action,
                    entity_type,
                    entity_name,
                    json.dumps(old_value)
                    if old_value is not None
                    else None,
                    json.dumps(new_value)
                    if new_value is not None
                    else None,
                    details,
                ),
            )

            if owns_connection:
                connection.commit()

        except Exception:
            if owns_connection:
                connection.rollback()

            raise

        finally:
            if owns_connection:
                connection.close()

    def get_equipment(
        self,
        name: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        connection = self._connect()

        try:
            if name is not None:
                row = connection.execute(
                    """
                    SELECT
                        id,
                        name,
                        display_name,
                        description
                    FROM equipment
                    WHERE name = ?
                    """,
                    (name,),
                ).fetchone()

                return self._row_to_dict(row)

            rows = connection.execute(
                """
                SELECT
                    id,
                    name,
                    display_name,
                    description
                FROM equipment
                ORDER BY name
                """
            ).fetchall()

            return [dict(row) for row in rows]

        finally:
            connection.close()

    def add_equipment(
        self,
        name: str,
        display_name: str,
        description: str = "",
        username: str = "system",
    ) -> int:
        connection = self._connect()

        try:
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
                    name,
                    display_name,
                    description,
                ),
            )

            equipment_id = int(cursor.lastrowid)

            self.write_audit_log(
                username=username,
                action="create",
                entity_type="equipment",
                entity_name=name,
                new_value={
                    "display_name": display_name,
                    "description": description,
                },
                connection=connection,
            )

            connection.commit()

            return equipment_id

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def get_tags(
        self,
        equipment_name: str | None = None,
        driver: str | None = None,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return logical tag definitions.

        When a driver is supplied, its address is loaded from
        tag_addresses. The legacy tags.driver and tags.address columns
        are intentionally ignored.
        """
        conditions: list[str] = []
        parameters: list[Any] = []

        if equipment_name is not None:
            conditions.append("equipment.name = ?")
            parameters.append(equipment_name)

        if enabled_only:
            conditions.append("tags.enabled = 1")

        where_clause = ""

        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        connection = self._connect()

        try:
            if driver is None:
                rows = connection.execute(
                    f"""
                    SELECT
                        tags.id,
                        tags.tag_name,
                        tags.description,
                        NULL AS driver,
                        NULL AS address,
                        tags.data_type,
                        tags.unit,
                        tags.enabled,
                        equipment.name AS equipment_name
                    FROM tags
                    LEFT JOIN equipment
                        ON equipment.id = tags.equipment_id
                    {where_clause}
                    ORDER BY tags.tag_name
                    """,
                    parameters,
                ).fetchall()

            else:
                driver_parameters = [driver, *parameters]

                rows = connection.execute(
                    f"""
                    SELECT
                        tags.id,
                        tags.tag_name,
                        tags.description,
                        tag_addresses.driver,
                        tag_addresses.address,
                        tags.data_type,
                        tags.unit,
                        tags.enabled,
                        equipment.name AS equipment_name
                    FROM tags
                    INNER JOIN tag_addresses
                        ON tag_addresses.tag_id = tags.id
                       AND tag_addresses.driver = ?
                    LEFT JOIN equipment
                        ON equipment.id = tags.equipment_id
                    {where_clause}
                    ORDER BY tags.tag_name
                    """,
                    driver_parameters,
                ).fetchall()

            return [dict(row) for row in rows]

        finally:
            connection.close()

    def get_tag(
        self,
        tag_name: str,
        driver: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Return one logical tag.

        When a driver is supplied, its protocol address is loaded from
        tag_addresses rather than the deprecated columns in tags.
        """
        connection = self._connect()

        try:
            if driver is None:
                row = connection.execute(
                    """
                    SELECT
                        tags.id,
                        tags.tag_name,
                        tags.description,
                        NULL AS driver,
                        NULL AS address,
                        tags.data_type,
                        tags.unit,
                        tags.enabled,
                        equipment.name AS equipment_name
                    FROM tags
                    LEFT JOIN equipment
                        ON equipment.id = tags.equipment_id
                    WHERE tags.tag_name = ?
                    """,
                    (tag_name,),
                ).fetchone()

            else:
                row = connection.execute(
                    """
                    SELECT
                        tags.id,
                        tags.tag_name,
                        tags.description,
                        tag_addresses.driver,
                        tag_addresses.address,
                        tags.data_type,
                        tags.unit,
                        tags.enabled,
                        equipment.name AS equipment_name
                    FROM tags
                    INNER JOIN tag_addresses
                        ON tag_addresses.tag_id = tags.id
                       AND tag_addresses.driver = ?
                    LEFT JOIN equipment
                        ON equipment.id = tags.equipment_id
                    WHERE tags.tag_name = ?
                    """,
                    (
                        driver,
                        tag_name,
                    ),
                ).fetchone()

            return self._row_to_dict(row)

        finally:
            connection.close()

    def add_tag(
        self,
        tag_name: str,
        equipment_name: str | None = None,
        description: str = "",
        data_type: str = "float",
        unit: str = "",
        enabled: bool = True,
        username: str = "system",
    ) -> int:
        connection = self._connect()

        try:
            equipment_id = None

            if equipment_name is not None:
                equipment_row = connection.execute(
                    """
                    SELECT id
                    FROM equipment
                    WHERE name = ?
                    """,
                    (equipment_name,),
                ).fetchone()

                if equipment_row is None:
                    raise ValueError(
                        f"Unknown equipment: {equipment_name}"
                    )

                equipment_id = equipment_row["id"]

            cursor = connection.execute(
                """
                INSERT INTO tags (
                    equipment_id,
                    tag_name,
                    description,
                    data_type,
                    unit,
                    enabled
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    equipment_id,
                    tag_name,
                    description,
                    data_type,
                    unit,
                    int(enabled),
                ),
            )

            tag_id = int(cursor.lastrowid)

            self.write_audit_log(
                username=username,
                action="create",
                entity_type="tag",
                entity_name=tag_name,
                new_value={
                    "equipment_name": equipment_name,
                    "description": description,
                    "data_type": data_type,
                    "unit": unit,
                    "enabled": enabled,
                },
                connection=connection,
            )

            connection.commit()
            return tag_id

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def add_tag_address(
        self,
        tag_name: str,
        driver: str,
        address: str,
        enabled: bool = True,
        username: str = "system",
    ) -> int:
        connection = self._connect()

        try:
            tag_row = connection.execute(
                """
                SELECT id
                FROM tags
                WHERE tag_name = ?
                """,
                (tag_name,),
            ).fetchone()

            if tag_row is None:
                raise ValueError(
                    f"Unknown tag: {tag_name}"
                )

            cursor = connection.execute(
                """
                INSERT INTO tag_addresses (
                    tag_id,
                    driver,
                    address,
                    enabled
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    tag_row["id"],
                    driver,
                    address,
                    int(enabled),
                ),
            )

            address_id = int(cursor.lastrowid)

            self.write_audit_log(
                username=username,
                action="create",
                entity_type="tag_address",
                entity_name=f"{tag_name}.{driver}",
                new_value={
                    "address": address,
                    "enabled": enabled,
                },
                connection=connection,
            )

            connection.commit()
            return address_id

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def set_tag_address(
        self,
        tag_name: str,
        driver: str,
        address: str,
        enabled: bool = True,
        username: str = "system",
    ) -> dict[str, Any]:
        connection = self._connect()

        try:
            tag_row = connection.execute(
                """
                SELECT id
                FROM tags
                WHERE tag_name = ?
                """,
                (tag_name,),
            ).fetchone()

            if tag_row is None:
                raise ValueError(
                    f"Unknown tag: {tag_name}"
                )

            existing_row = connection.execute(
                """
                SELECT
                    id,
                    address,
                    enabled
                FROM tag_addresses
                WHERE tag_id = ?
                  AND driver = ?
                """,
                (
                    tag_row["id"],
                    driver,
                ),
            ).fetchone()

            old_value = (
                dict(existing_row)
                if existing_row is not None
                else None
            )

            connection.execute(
                """
                INSERT INTO tag_addresses (
                    tag_id,
                    driver,
                    address,
                    enabled
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tag_id, driver)
                DO UPDATE SET
                    address = excluded.address,
                    enabled = excluded.enabled
                """,
                (
                    tag_row["id"],
                    driver,
                    address,
                    int(enabled),
                ),
            )

            updated_row = connection.execute(
                """
                SELECT
                    driver,
                    address,
                    enabled
                FROM tag_addresses
                WHERE tag_id = ?
                  AND driver = ?
                """,
                (
                    tag_row["id"],
                    driver,
                ),
            ).fetchone()

            result = dict(updated_row)

            self.write_audit_log(
                username=username,
                action="set_tag_address",
                entity_type="tag_address",
                entity_name=f"{tag_name}.{driver}",
                old_value=old_value,
                new_value=result,
                connection=connection,
            )

            connection.commit()
            return result

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def get_tag_addresses(
        self,
        tag_name: str | None = None,
        driver: str | None = None,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        conditions = []
        parameters: list[Any] = []

        if tag_name is not None:
            conditions.append("tags.tag_name = ?")
            parameters.append(tag_name)

        if driver is not None:
            conditions.append("tag_addresses.driver = ?")
            parameters.append(driver)

        if enabled_only:
            conditions.append("tag_addresses.enabled = 1")

        where_clause = ""

        if conditions:
            where_clause = (
                "WHERE " + " AND ".join(conditions)
            )

        connection = self._connect()

        try:
            rows = connection.execute(
                f"""
                SELECT
                    tags.tag_name,
                    tag_addresses.driver,
                    tag_addresses.address,
                    tag_addresses.enabled
                FROM tag_addresses
                INNER JOIN tags
                    ON tags.id = tag_addresses.tag_id
                {where_clause}
                ORDER BY
                    tags.tag_name,
                    tag_addresses.driver
                """,
                parameters,
            ).fetchall()

            return [dict(row) for row in rows]

        finally:
            connection.close()



    def get_threshold(
        self,
        tag_name: str,
    ) -> dict[str, Any] | None:
        connection = self._connect()

        try:
            row = connection.execute(
                """
                SELECT
                    tag_name,
                    low_warning,
                    low_alarm,
                    high_warning,
                    high_alarm
                FROM thresholds
                WHERE tag_name = ?
                """,
                (tag_name,),
            ).fetchone()

            return self._row_to_dict(row)

        finally:
            connection.close()

    def get_thresholds(
        self,
    ) -> list[dict[str, Any]]:
        connection = self._connect()

        try:
            rows = connection.execute(
                """
                SELECT
                    tag_name,
                    low_warning,
                    low_alarm,
                    high_warning,
                    high_alarm
                FROM thresholds
                ORDER BY tag_name
                """
            ).fetchall()

            return [dict(row) for row in rows]

        finally:
            connection.close()

    def set_threshold(
        self,
        tag_name: str,
        parameter: str,
        value: float | None,
        username: str = "system",
    ) -> dict[str, Any]:
        allowed_parameters = {
            "low_warning",
            "low_alarm",
            "high_warning",
            "high_alarm",
        }

        if parameter not in allowed_parameters:
            raise ValueError(
                f"Unsupported threshold parameter: {parameter}"
            )

        connection = self._connect()

        try:
            tag_exists = connection.execute(
                """
                SELECT 1
                FROM tags
                WHERE tag_name = ?
                """,
                (tag_name,),
            ).fetchone()

            if tag_exists is None:
                raise ValueError(
                    f"Unknown tag: {tag_name}"
                )

            existing_row = connection.execute(
                """
                SELECT
                    tag_name,
                    low_warning,
                    low_alarm,
                    high_warning,
                    high_alarm
                FROM thresholds
                WHERE tag_name = ?
                """,
                (tag_name,),
            ).fetchone()

            old_threshold = (
                dict(existing_row)
                if existing_row is not None
                else None
            )

            if existing_row is None:
                connection.execute(
                    """
                    INSERT INTO thresholds (
                        tag_name
                    )
                    VALUES (?)
                    """,
                    (tag_name,),
                )

            connection.execute(
                f"""
                UPDATE thresholds
                SET {parameter} = ?
                WHERE tag_name = ?
                """,
                (
                    value,
                    tag_name,
                ),
            )

            updated_row = connection.execute(
                """
                SELECT
                    tag_name,
                    low_warning,
                    low_alarm,
                    high_warning,
                    high_alarm
                FROM thresholds
                WHERE tag_name = ?
                """,
                (tag_name,),
            ).fetchone()

            updated_threshold = dict(updated_row)

            self.write_audit_log(
                username=username,
                action="update_threshold",
                entity_type="threshold",
                entity_name=f"{tag_name}.{parameter}",
                old_value=old_threshold,
                new_value=updated_threshold,
                connection=connection,
            )

            connection.commit()

            return updated_threshold

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def get_audit_log(
        self,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        connection = self._connect()

        try:
            rows = connection.execute(
                """
                SELECT
                    timestamp,
                    username,
                    action,
                    entity_type,
                    entity_name,
                    old_value,
                    new_value,
                    details
                FROM audit_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            return [dict(row) for row in rows]

        finally:
            connection.close()
