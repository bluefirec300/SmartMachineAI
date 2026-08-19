import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config.environment import get_machine_db_path
from database.backup import backup_sqlite_database


DB_PATH = get_machine_db_path()


class DatabaseManager:
    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def get_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=10,
        )

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")

        return connection

    def initialize(self) -> None:
        with self.get_connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plc_data
                (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    address TEXT NOT NULL,
                    value REAL NOT NULL
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_plc_data_tag_time
                ON plc_data(tag, time)
                """
            )

            # Current value only (not history) for STRING-typed tags
            # like AlarmCode, which can't fit plc_data's REAL column -
            # see app/plc_logger.py. One row per tag, overwritten each
            # cycle, so the UI can show "what's the alarm code right
            # now" without needing a full text historian.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plc_text_data
                (
                    tag TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    value TEXT NOT NULL,
                    time TEXT NOT NULL
                )
                """
            )

    def save_tag(
        self,
        tag: str,
        address: str,
        value: int | float,
        timestamp: datetime | None = None,
    ) -> None:
        if not tag:
            raise ValueError("Tag name cannot be empty.")

        if not address:
            raise ValueError("Address cannot be empty.")

        record_time = timestamp or datetime.now()

        with self.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO plc_data
                (
                    time,
                    tag,
                    address,
                    value
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    record_time.strftime("%Y-%m-%d %H:%M:%S"),
                    tag,
                    address,
                    float(value),
                ),
            )

    def get_latest(self, tag: str) -> dict[str, Any] | None:
        with self.get_connection() as connection:
            row = connection.execute(
                """
                SELECT time, tag, address, value
                FROM plc_data
                WHERE tag = ?
                ORDER BY time DESC, id DESC
                LIMIT 1
                """,
                (tag,),
            ).fetchone()

        if row is None:
            return None

        return dict(row)

    def save_text_tag(
        self,
        tag: str,
        address: str,
        value: str,
        timestamp: datetime | None = None,
    ) -> None:
        if not tag:
            raise ValueError("Tag name cannot be empty.")

        if not address:
            raise ValueError("Address cannot be empty.")

        record_time = timestamp or datetime.now()

        with self.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO plc_text_data (tag, address, value, time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tag) DO UPDATE SET
                    address = excluded.address,
                    value = excluded.value,
                    time = excluded.time
                """,
                (
                    tag,
                    address,
                    str(value),
                    record_time.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

    def get_latest_text_all(self) -> dict[str, dict[str, Any]]:
        with self.get_connection() as connection:
            rows = connection.execute(
                "SELECT tag, address, value, time FROM plc_text_data"
            ).fetchall()

        return {
            row["tag"]: {
                "address": row["address"],
                "value": row["value"],
                "time": row["time"],
            }
            for row in rows
        }

    def get_latest_all(self) -> dict[str, dict[str, Any]]:
        with self.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT p.time, p.tag, p.address, p.value
                FROM plc_data AS p
                INNER JOIN
                (
                    SELECT tag, MAX(id) AS latest_id
                    FROM plc_data
                    GROUP BY tag
                ) AS latest
                ON p.id = latest.latest_id
                ORDER BY p.tag
                """
            ).fetchall()

        return {
            row["tag"]: {
                "time": row["time"],
                "address": row["address"],
                "value": row["value"],
            }
            for row in rows
        }

    def get_history(
        self,
        tag: str,
        hours: int = 24,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if hours <= 0:
            raise ValueError("Hours must be greater than zero.")

        if limit <= 0:
            raise ValueError("Limit must be greater than zero.")

        start_time = datetime.now() - timedelta(hours=hours)

        with self.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT time, tag, address, value
                FROM plc_data
                WHERE tag = ?
                  AND time >= ?
                ORDER BY time ASC, id ASC
                LIMIT ?
                """,
                (
                    tag,
                    start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    limit,
                ),
            ).fetchall()

        return [dict(row) for row in rows]

    def get_history_range(
        self,
        tag: str,
        start: str,
        end: str,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """
        Like get_history(), but for an explicit [start, end] window
        (both "%Y-%m-%d %H:%M:%S" strings) instead of an "hours ago
        from now" lookback - needed to pull a specific past calendar
        day (e.g. "yesterday", or an arbitrary named date) rather than
        only ever a window ending at the current moment.
        """
        if limit <= 0:
            raise ValueError("Limit must be greater than zero.")

        with self.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT time, tag, address, value
                FROM plc_data
                WHERE tag = ?
                  AND time >= ?
                  AND time <= ?
                ORDER BY time ASC, id ASC
                LIMIT ?
                """,
                (
                    tag,
                    start,
                    end,
                    limit,
                ),
            ).fetchall()

        return [dict(row) for row in rows]

    def cleanup(self, days: int = 30) -> int:
        if days <= 0:
            raise ValueError("Days must be greater than zero.")

        cutoff_time = datetime.now() - timedelta(days=days)

        with self.get_connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM plc_data
                WHERE time < ?
                """,
                (cutoff_time.strftime("%Y-%m-%d %H:%M:%S"),),
            )

            deleted_rows = cursor.rowcount

        return deleted_rows

    def backup(self, destination: Path | str) -> Path:
        return backup_sqlite_database(self.db_path, destination)


_default_db = DatabaseManager()


def save_data(tag: str, address: str, value: int | float) -> None:
    """
    Backward-compatible function used by the existing PLC logger.
    """
    _default_db.save_tag(
        tag=tag,
        address=address,
        value=value,
    )


def get_latest(tag: str) -> dict[str, Any] | None:
    return _default_db.get_latest(tag)


if __name__ == "__main__":
    print(f"Database: {DB_PATH}")
    print("Database initialization successful.")
