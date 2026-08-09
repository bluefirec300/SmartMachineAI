import sqlite3
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "database"
    / "machine_data.db"
)


class EventStore:
    """
    Stores and retrieves significant machine events.

    Events are kept in the historian database because they are
    operational records, not configuration data.

    Duplicate protection is based on:

        event_time
        tag
        severity
        condition
    """

    def __init__(
        self,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
    ):
        self.database_path = Path(
            database_path
        ).resolve()

        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path
        )

        connection.row_factory = sqlite3.Row

        return connection

    def _initialize_database(self) -> None:
        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS machine_events
                (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    event_time TEXT NOT NULL,
                    detected_at TEXT NOT NULL,

                    equipment TEXT NOT NULL,
                    tag TEXT NOT NULL,

                    severity TEXT NOT NULL,
                    condition TEXT NOT NULL,

                    value REAL,
                    unit TEXT,
                    trend TEXT,
                    message TEXT,
                    address TEXT,

                    samples INTEGER,
                    minimum REAL,
                    maximum REAL,
                    average REAL,
                    change REAL,

                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    UNIQUE
                    (
                        event_time,
                        tag,
                        severity,
                        condition
                    )
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_machine_events_time
                ON machine_events(event_time)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_machine_events_equipment
                ON machine_events(equipment, event_time)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_machine_events_tag
                ON machine_events(tag, event_time)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_machine_events_severity
                ON machine_events(severity, event_time)
                """
            )

    @staticmethod
    def _normalize_event(
        event: dict[str, Any],
    ) -> dict[str, Any]:
        required_fields = (
            "event_time",
            "detected_at",
            "equipment",
            "tag",
            "severity",
            "condition",
        )

        normalized = dict(event)

        for field_name in required_fields:
            value = normalized.get(
                field_name
            )

            if value is None:
                raise ValueError(
                    f"Event field is required: "
                    f"{field_name}"
                )

            text = str(value).strip()

            if not text:
                raise ValueError(
                    f"Event field cannot be empty: "
                    f"{field_name}"
                )

            normalized[field_name] = text

        normalized["severity"] = str(
            normalized["severity"]
        ).strip().lower()

        normalized["condition"] = str(
            normalized["condition"]
        ).strip().lower()

        for field_name in (
            "unit",
            "trend",
            "message",
            "address",
        ):
            value = normalized.get(
                field_name
            )

            if value is None:
                normalized[field_name] = None
            else:
                text = str(value).strip()

                normalized[field_name] = (
                    text
                    if text
                    else None
                )

        return normalized

    def insert_event(
        self,
        event: dict[str, Any],
    ) -> int | None:
        """
        Insert one event.

        Returns:
            New event ID when inserted.
            None when an identical event already exists.
        """
        normalized = self._normalize_event(
            event
        )

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO machine_events
                (
                    event_time,
                    detected_at,
                    equipment,
                    tag,
                    severity,
                    condition,
                    value,
                    unit,
                    trend,
                    message,
                    address,
                    samples,
                    minimum,
                    maximum,
                    average,
                    change
                )
                VALUES
                (
                    :event_time,
                    :detected_at,
                    :equipment,
                    :tag,
                    :severity,
                    :condition,
                    :value,
                    :unit,
                    :trend,
                    :message,
                    :address,
                    :samples,
                    :minimum,
                    :maximum,
                    :average,
                    :change
                )
                """,
                {
                    "event_time": normalized[
                        "event_time"
                    ],
                    "detected_at": normalized[
                        "detected_at"
                    ],
                    "equipment": normalized[
                        "equipment"
                    ],
                    "tag": normalized[
                        "tag"
                    ],
                    "severity": normalized[
                        "severity"
                    ],
                    "condition": normalized[
                        "condition"
                    ],
                    "value": normalized.get(
                        "value"
                    ),
                    "unit": normalized.get(
                        "unit"
                    ),
                    "trend": normalized.get(
                        "trend"
                    ),
                    "message": normalized.get(
                        "message"
                    ),
                    "address": normalized.get(
                        "address"
                    ),
                    "samples": normalized.get(
                        "samples"
                    ),
                    "minimum": normalized.get(
                        "minimum"
                    ),
                    "maximum": normalized.get(
                        "maximum"
                    ),
                    "average": normalized.get(
                        "average"
                    ),
                    "change": normalized.get(
                        "change"
                    ),
                },
            )

            if cursor.rowcount == 0:
                return None

            return int(
                cursor.lastrowid
            )

    def insert_events(
        self,
        events: list[dict[str, Any]],
    ) -> list[int]:
        """
        Insert multiple events.

        Duplicate events are skipped.
        """
        inserted_ids: list[int] = []

        for event in events:
            event_id = self.insert_event(
                event
            )

            if event_id is not None:
                inserted_ids.append(
                    event_id
                )

        return inserted_ids

    def get_event(
        self,
        event_id: int,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM machine_events
                WHERE id = ?
                """,
                (
                    event_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return dict(row)

    def get_recent_events(
        self,
        limit: int = 20,
        equipment: str | None = None,
        tag: str | None = None,
        severity: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        conditions: list[str] = []
        parameters: list[Any] = []

        if equipment:
            conditions.append(
                "equipment = ?"
            )
            parameters.append(
                equipment.strip()
            )

        if tag:
            conditions.append(
                "tag = ?"
            )
            parameters.append(
                tag.strip()
            )

        if severity:
            conditions.append(
                "severity = ?"
            )
            parameters.append(
                severity.strip().lower()
            )

        if start_time:
            conditions.append(
                "event_time >= ?"
            )
            parameters.append(
                start_time
            )

        if end_time:
            conditions.append(
                "event_time <= ?"
            )
            parameters.append(
                end_time
            )

        where_clause = ""

        if conditions:
            where_clause = (
                "WHERE "
                + " AND ".join(conditions)
            )

        parameters.append(
            int(limit)
        )

        query = f"""
            SELECT *
            FROM machine_events
            {where_clause}
            ORDER BY event_time DESC, id DESC
            LIMIT ?
        """

        with self._connect() as connection:
            rows = connection.execute(
                query,
                parameters,
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def count_events(
        self,
        equipment: str | None = None,
        tag: str | None = None,
        severity: str | None = None,
        condition: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> int:
        conditions: list[str] = []
        parameters: list[Any] = []

        filters = {
            "equipment": equipment,
            "tag": tag,
            "severity": (
                severity.lower()
                if severity
                else None
            ),
            "condition": (
                condition.lower()
                if condition
                else None
            ),
        }

        for column_name, value in filters.items():
            if value:
                conditions.append(
                    f"{column_name} = ?"
                )
                parameters.append(
                    value.strip()
                )

        if start_time:
            conditions.append(
                "event_time >= ?"
            )
            parameters.append(
                start_time
            )

        if end_time:
            conditions.append(
                "event_time <= ?"
            )
            parameters.append(
                end_time
            )

        where_clause = ""

        if conditions:
            where_clause = (
                "WHERE "
                + " AND ".join(conditions)
            )

        query = f"""
            SELECT COUNT(*) AS event_count
            FROM machine_events
            {where_clause}
        """

        with self._connect() as connection:
            row = connection.execute(
                query,
                parameters,
            ).fetchone()

        return int(
            row["event_count"]
        )

    def delete_all_events(self) -> int:
        """
        Intended mainly for tests and development resets.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM machine_events
                """
            )

            return int(
                cursor.rowcount
            )
