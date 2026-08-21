import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "database"
    / "machine_data.db"
)


class NotificationLog:
    """
    Phase V2.2 - tracks the last time an email notification was sent
    for a given (equipment, tag, condition) combination, so the
    notification worker can apply a cooldown and never spam the same
    ongoing alarm every time app/event_monitor.py re-inserts a
    transition event for it (e.g. after a service restart re-evaluates
    an already-active alarm as "new").

    Lives in the historian database, alongside machine_events - this
    is operational state about actual alarms, not configuration.
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
                CREATE TABLE IF NOT EXISTS notification_log
                (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    equipment TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    severity TEXT NOT NULL,

                    last_event_id INTEGER,
                    last_notified_at TEXT NOT NULL,
                    notification_count INTEGER NOT NULL DEFAULT 1,

                    UNIQUE (equipment, tag, condition)
                )
                """
            )

    def get_last_notified_at(
        self,
        equipment: str,
        tag: str,
        condition: str,
    ) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT last_notified_at
                FROM notification_log
                WHERE equipment = ? AND tag = ? AND condition = ?
                """,
                (equipment, tag, condition.strip().lower()),
            ).fetchone()

        if row is None:
            return None

        try:
            return datetime.strptime(row["last_notified_at"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

    def record_notified(
        self,
        equipment: str,
        tag: str,
        condition: str,
        severity: str,
        event_id: int | None,
        when: datetime,
    ) -> None:
        timestamp = when.strftime("%Y-%m-%d %H:%M:%S")

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO notification_log
                (equipment, tag, condition, severity, last_event_id, last_notified_at, notification_count)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (equipment, tag, condition) DO UPDATE SET
                    severity = excluded.severity,
                    last_event_id = excluded.last_event_id,
                    last_notified_at = excluded.last_notified_at,
                    notification_count = notification_count + 1
                """,
                (equipment, tag, condition.strip().lower(), severity.strip().lower(), event_id, timestamp),
            )

    def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM notification_log
                ORDER BY last_notified_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()

        return [dict(row) for row in rows]
