from __future__ import annotations

import fcntl
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.configuration_manager import ConfigurationManager
from ui.data_access import CONFIG_DATABASE_PATH

"""
Read/write helpers for the Production Context page (Phase 4) - product
master data and a read-only view of the production_batches history the
standalone production_simulator.service generates. Products are the
only thing this page writes directly; batches are system-generated
(see app/production_simulator.py) and only ever read here.
"""

LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "production_simulator.lock"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _audit(username: str, action: str, entity_type: str, entity_name: str, details: str) -> None:
    ConfigurationManager(database_path=CONFIG_DATABASE_PATH).write_audit_log(
        username=username, action=action, entity_type=entity_type,
        entity_name=entity_name, details=details,
    )


def is_production_simulator_running() -> bool:
    """
    Best-effort liveness check: try to acquire the same exclusive lock
    app/production_simulator.py holds for its entire lifetime. If the
    lock is currently held by another process, this fails immediately
    (meaning the simulator IS running); if it succeeds, nothing is
    holding it (not running) - release it again right away either way,
    since this is just a probe, not a real claim on the lock.
    """
    if not LOCK_FILE_PATH.exists():
        return False

    try:
        with open(LOCK_FILE_PATH, "w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True  # someone else holds it - simulator is running
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def get_product_categories() -> list[dict[str, Any]]:
    connection = _connect()
    try:
        return [dict(r) for r in connection.execute("SELECT * FROM product_categories ORDER BY name")]
    finally:
        connection.close()


def get_products(active_only: bool = False) -> list[dict[str, Any]]:
    connection = _connect()
    try:
        query = """
            SELECT p.*, c.name AS category_name
            FROM products p
            LEFT JOIN product_categories c ON c.id = p.category_id
        """
        if active_only:
            query += " WHERE p.active = 1"
        query += " ORDER BY c.name, p.product_name"
        return [dict(r) for r in connection.execute(query)]
    finally:
        connection.close()


def create_product(
    product_code: str, product_name: str, category_id: int, variant: str | None,
    standard_batch_size: float | None, unit_of_measure: str, recipe_reference: str | None,
    username: str,
) -> int:
    connection = _connect()
    try:
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = connection.execute(
            """
            INSERT INTO products (
                product_code, product_name, category_id, variant, standard_batch_size,
                unit_of_measure, recipe_reference, active, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'user_entered', ?)
            """,
            (product_code, product_name, category_id, variant, standard_batch_size, unit_of_measure, recipe_reference, now),
        )
        connection.commit()
        product_id = cursor.lastrowid
    finally:
        connection.close()

    _audit(username, "create_product", "product", product_code, product_name)
    return product_id


def update_product(product_id: int, username: str, **fields: Any) -> None:
    editable = {"product_name", "category_id", "variant", "standard_batch_size", "unit_of_measure", "recipe_reference", "active"}
    updates = {k: v for k, v in fields.items() if k in editable}
    if not updates:
        return

    connection = _connect()
    try:
        set_clause = ", ".join(f"{field} = ?" for field in updates)
        connection.execute(f"UPDATE products SET {set_clause} WHERE id = ?", (*updates.values(), product_id))
        connection.commit()
    finally:
        connection.close()

    _audit(username, "update_product", "product", str(product_id), str(updates))


def get_batches(
    plant_id: int | None = None, product_id: int | None = None,
    status: str | None = None, limit: int = 200,
) -> list[dict[str, Any]]:
    connection = _connect()
    try:
        query = """
            SELECT b.*, p.product_code, p.product_name, pl.code AS plant_code, e.display_name AS equipment_display_name
            FROM production_batches b
            JOIN products p ON p.id = b.product_id
            JOIN plants pl ON pl.id = b.plant_id
            LEFT JOIN equipment e ON e.id = b.equipment_id
            WHERE 1=1
        """
        params: list[Any] = []
        if plant_id is not None:
            query += " AND b.plant_id = ?"
            params.append(plant_id)
        if product_id is not None:
            query += " AND b.product_id = ?"
            params.append(product_id)
        if status is not None:
            query += " AND b.status = ?"
            params.append(status)
        query += " ORDER BY b.id DESC LIMIT ?"
        params.append(limit)

        return [dict(r) for r in connection.execute(query, params)]
    finally:
        connection.close()


def get_batch_status_counts() -> dict[str, int]:
    connection = _connect()
    try:
        rows = connection.execute("SELECT status, COUNT(*) AS c FROM production_batches GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}
    finally:
        connection.close()
