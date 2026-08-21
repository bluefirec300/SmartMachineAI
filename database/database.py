import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config.environment import get_machine_db_path
from database.backup import backup_sqlite_database
from database.historian_archive import archive_dir_for, query_archived_history


DB_PATH = get_machine_db_path()


class DatabaseManager:
    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir = archive_dir_for(self.db_path)
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

        return self.get_history_range(
            tag,
            start_time.strftime("%Y-%m-%d %H:%M:%S"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            limit=limit,
        )

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

        Phase V1.2 - transparently merges in archived (Historian
        Archive) rows when the requested range reaches further back
        than what's still in the live table, so every existing caller
        (deterministic engines, Ask AI, UI pages) keeps working exactly
        as before with no code change of their own and no need to know
        whether a given row came from the live table or an archive
        file. A no-op (one cheap directory-existence check) for the
        overwhelming majority of queries, which never reach archived
        data at all - see database.historian_archive's module docstring.
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

        live_rows = [dict(row) for row in rows]

        if len(live_rows) < limit and self.archive_dir.exists():
            archived_rows = query_archived_history(self.archive_dir, tag, start, end, limit)

            if archived_rows:
                # Archived months are always strictly older than whatever
                # remains in the live table (archive_and_purge_eligible_
                # months() only ever archives a FULLY elapsed month, and
                # the live table only ever holds the retention window) -
                # both lists are already individually time-ordered, so a
                # plain concatenation is already fully time-ordered too;
                # no re-sort needed.
                live_rows = (archived_rows + live_rows)[:limit]

        return live_rows

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
