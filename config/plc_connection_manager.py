from __future__ import annotations
from config.environment import get_config_db_path

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

PROTOCOLS = ("simulator", "fins", "modbus_tcp", "opcua", "s7")

# Which of the generic columns each protocol actually uses - drives
# both the admin UI's conditional form fields and driver_factory's
# construction of the right driver kwargs.
PROTOCOL_FIELDS = {
    "simulator": (),
    "fins": ("host", "port", "plc_node", "pc_node"),
    "modbus_tcp": ("host", "port", "unit_id"),
    "opcua": ("endpoint_url", "username", "password"),
    "s7": ("host", "port", "rack", "slot"),
}


def _utc_timestamp() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


class PLCConnectionManager:
    """
    Named PLC connection profiles (protocol + connection details), one
    of which is "active" at a time - that's the one
    plc.driver_factory.create_driver() uses. Kept in the database
    (not config/settings.ini) specifically so the new PLC Connectivity
    admin page can change it live, with an audit trail, without
    editing a text file on the server.
    """

    def __init__(self, database_path: Path | str = DEFAULT_DATABASE_PATH):
        self.database_path = Path(database_path).resolve()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        connection = self._connect()

        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plc_connections
                (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    protocol TEXT NOT NULL,
                    host TEXT,
                    port INTEGER,
                    plc_node INTEGER,
                    pc_node INTEGER,
                    unit_id INTEGER,
                    endpoint_url TEXT,
                    username TEXT,
                    password TEXT,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            # Siemens S7 addressing needs rack/slot, which don't
            # overload cleanly onto any existing column (plc_node/
            # pc_node are FINS-specific concepts, not "which S7 rack").
            existing_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(plc_connections)").fetchall()
            }

            for column in ("rack", "slot"):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE plc_connections ADD COLUMN {column} INTEGER")

            connection.commit()
        finally:
            connection.close()

    def _write_audit_log(
        self, connection: sqlite3.Connection, username: str, action: str, entity_name: str, details: str = ""
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log
            (timestamp, username, action, entity_type, entity_name, old_value, new_value, details)
            VALUES (?, ?, ?, 'plc_connection', ?, NULL, NULL, ?)
            """,
            (_utc_timestamp(), username, action, entity_name, details or None),
        )

    def list_connections(self) -> list[dict[str, Any]]:
        connection = self._connect()

        try:
            rows = connection.execute(
                "SELECT * FROM plc_connections ORDER BY name"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def get_active(self) -> dict[str, Any] | None:
        connection = self._connect()

        try:
            row = connection.execute(
                "SELECT * FROM plc_connections WHERE is_active = 1 LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def create_connection(
        self,
        name: str,
        protocol: str,
        created_by: str,
        host: str | None = None,
        port: int | None = None,
        plc_node: int | None = None,
        pc_node: int | None = None,
        unit_id: int | None = None,
        endpoint_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        rack: int | None = None,
        slot: int | None = None,
    ) -> dict[str, Any]:
        if protocol not in PROTOCOLS:
            raise ValueError(f"protocol must be one of {PROTOCOLS}, got {protocol!r}")

        if not name.strip():
            raise ValueError("name is required.")

        now = _utc_timestamp()
        connection = self._connect()

        try:
            cursor = connection.execute(
                """
                INSERT INTO plc_connections
                (name, protocol, host, port, plc_node, pc_node, unit_id,
                 endpoint_url, username, password, rack, slot, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    name.strip(), protocol, host, port, plc_node, pc_node, unit_id,
                    endpoint_url, username, password, rack, slot, now, now,
                ),
            )

            self._write_audit_log(
                connection, created_by, "create_connection", name.strip(), f"protocol={protocol}"
            )
            connection.commit()

            return dict(
                connection.execute(
                    "SELECT * FROM plc_connections WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
            )
        finally:
            connection.close()

    def set_active(self, connection_id: int, changed_by: str) -> None:
        connection = self._connect()

        try:
            row = connection.execute(
                "SELECT name FROM plc_connections WHERE id = ?", (connection_id,)
            ).fetchone()

            if row is None:
                raise ValueError(f"No connection with id {connection_id}")

            connection.execute("UPDATE plc_connections SET is_active = 0")
            connection.execute(
                "UPDATE plc_connections SET is_active = 1, updated_at = ? WHERE id = ?",
                (_utc_timestamp(), connection_id),
            )

            self._write_audit_log(connection, changed_by, "activate_connection", row["name"])
            connection.commit()
        finally:
            connection.close()

    def deactivate(self, connection_id: int, changed_by: str) -> None:
        """
        Clear a connection's active flag without activating anything
        else in its place - leaves the system with no active
        connection at all (falling back to config/settings.ini, same
        as before any connection was ever activated). Mirrors
        set_active() above but without the "clear everyone else's flag
        first" step, since there's nothing new to make active.
        """
        connection = self._connect()

        try:
            row = connection.execute(
                "SELECT name FROM plc_connections WHERE id = ?", (connection_id,)
            ).fetchone()

            if row is None:
                raise ValueError(f"No connection with id {connection_id}")

            connection.execute(
                "UPDATE plc_connections SET is_active = 0, updated_at = ? WHERE id = ?",
                (_utc_timestamp(), connection_id),
            )

            self._write_audit_log(connection, changed_by, "deactivate_connection", row["name"])
            connection.commit()
        finally:
            connection.close()

    def delete_connection(self, connection_id: int, changed_by: str) -> None:
        connection = self._connect()

        try:
            row = connection.execute(
                "SELECT name FROM plc_connections WHERE id = ?", (connection_id,)
            ).fetchone()

            if row is None:
                return

            connection.execute("DELETE FROM plc_connections WHERE id = ?", (connection_id,))
            self._write_audit_log(connection, changed_by, "delete_connection", row["name"])
            connection.commit()
        finally:
            connection.close()
