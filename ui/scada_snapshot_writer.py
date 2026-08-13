from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.rule_engine import RuleEngine
from database.database import DatabaseManager
from ui.data_access import CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH
from ui.scada_floor_plan_data import build_board_snapshot, build_equipment_snapshot

SNAPSHOT_PATH = PROJECT_ROOT / "manuals" / "_scada_live.json"
WRITE_INTERVAL_SECONDS = 2
PLANTS = ("p01", "p02")


def build_snapshot() -> dict:
    database = DatabaseManager(db_path=MACHINE_DATABASE_PATH)
    rule_engine = RuleEngine(database_path=CONFIG_DATABASE_PATH)

    plants = {}
    for plant in PLANTS:
        plants[plant] = {
            "board": build_board_snapshot(plant, database, rule_engine),
            "equipment": build_equipment_snapshot(plant, database, rule_engine),
        }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "plants": plants,
    }


def _write_snapshot_atomic(snapshot: dict, target_path: Path) -> None:
    """
    Writes to a temp file in the same directory, then renames it over
    the real path. os.replace() is atomic on POSIX, so the browser's
    fetch() can never observe a half-written file - it either sees the
    old snapshot or the fully-written new one, never a partial one.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(".json.tmp")
    with open(temp_path, "w") as handle:
        json.dump(snapshot, handle)
    os.replace(temp_path, target_path)


def _writer_loop() -> None:
    while True:
        try:
            snapshot = build_snapshot()
            _write_snapshot_atomic(snapshot, SNAPSHOT_PATH)
        except Exception as error:
            # One bad tick must never kill the thread permanently - same
            # discipline app/plc_logger.py's per-tag try/except follows.
            print(f"[scada_snapshot_writer] tick failed: {error}")
        time.sleep(WRITE_INTERVAL_SECONDS)


@st.cache_resource
def start_snapshot_writer() -> bool:
    """
    Starts the background writer thread exactly once per Streamlit
    server process. st.cache_resource caches the return value across
    every session sharing this process, so calling this at the top of
    every page load only actually runs the function body (and starts
    the thread) the first time - every later call is a free cache hit,
    regardless of how many browser tabs/users are connected.
    """
    thread = threading.Thread(target=_writer_loop, daemon=True, name="scada-snapshot-writer")
    thread.start()
    return True
