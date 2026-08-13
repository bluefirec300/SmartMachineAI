"""
Builds a fresh, empty "actual" environment - same full schema as the
simulation database, zero data rows, one admin account so it's usable
on first login. Meant to be run once, before the "actual" environment
is ever selected in the PLC Connectivity page.

Deliberately does NOT run engine/seed_equipment_metadata.py or
engine/seed_engineering_thresholds.py - those fill in illustrative
*fake* demo data (fabricated brand/model info, hand-picked simulator
ranges) that only makes sense for the simulation environment. A real
deployment starts genuinely empty; equipment/tags/thresholds get
created through the Equipment & Tag Configuration page instead.

Safe to re-run - every step it calls is itself idempotent/additive.
"""

from __future__ import annotations

import sqlite3

from config.environment import ENVIRONMENTS
from config.plc_connection_manager import PLCConnectionManager
from config.user_manager import UserManager
from database.database import DatabaseManager
from database.initialize_config_db import initialize_config_database
from engine.equipment_metadata_migrator import migrate as migrate_equipment_metadata
from engine.maintenance_migrator import migrate as migrate_maintenance
from engine.metadata_migrator import migrate as migrate_tag_concepts
from engine.service_migrator import migrate as migrate_service
from engine.tag_dataset_importer import _ensure_columns as ensure_cadence_columns
from rag.document_store import ensure_schema as ensure_document_schema


def initialize_actual_environment() -> None:
    config_db_path = ENVIRONMENTS["actual"]["config_db"]
    machine_db_path = ENVIRONMENTS["actual"]["machine_db"]

    print(f"Config database: {config_db_path}")
    initialize_config_database(config_db_path)
    migrate_equipment_metadata(config_db_path, backup=False)
    migrate_maintenance(config_db_path, backup=False)
    migrate_service(config_db_path, backup=False)
    # Adds tags.measurement/location/signal_type/event_type/threshold_type -
    # required by both the simulator (TagDatasetSimulator._load_tags()
    # selects them directly) and the NLP query engine, not just an
    # optional enrichment.
    migrate_tag_concepts(config_db_path, backup=False)

    connection = sqlite3.connect(config_db_path)
    connection.row_factory = sqlite3.Row
    try:
        ensure_document_schema(connection)
        # Adds tags.logging_interval_seconds/log_on_change - the
        # Equipment & Tag Configuration page's cadence field writes to
        # these unconditionally, so a tags table missing them isn't a
        # degraded state, it's a hard crash the moment someone creates
        # their first tag.
        ensure_cadence_columns(connection)
        connection.commit()
    finally:
        connection.close()

    # Self-provisions plc_connections (and, via UserManager, rebuilds
    # the base schema's placeholder `users` table into the real one -
    # same self-healing migration used for the simulation database).
    PLCConnectionManager(database_path=config_db_path)
    user_manager = UserManager(database_path=config_db_path)

    if user_manager.get_user("admin") is None:
        user_manager.create_user(
            username="admin",
            display_name="Administrator",
            password="123456",
            role="admin",
            created_by="system",
        )
        print("Created default admin account (username: admin, password: 123456)")
    else:
        print("Admin account already exists, left unchanged.")

    print(f"Machine database: {machine_db_path}")
    # Self-provisions plc_data/plc_text_data on first connect.
    DatabaseManager(db_path=machine_db_path)

    print("Actual environment initialized - schema complete, zero data rows.")


if __name__ == "__main__":
    initialize_actual_environment()
