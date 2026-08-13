# SCADA Floor Plan Flicker-Free Live View + Main-App Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the SCADA Floor Plan page's flashing `st.fragment(run_every=30)` live section with a flicker-free, 2-second-polling JS-driven view, and move the page from standalone port 8503 into the main app's sidebar (below "Ask AI").

**Architecture:** A background daemon thread (one per Streamlit server process, started via `st.cache_resource`) reads live tag data every 2 seconds using the same functions the page already uses, and writes one JSON snapshot covering both plants to `manuals/_scada_live.json` (served at `/app/static/_scada_live.json` via Streamlit's existing static file server). The page itself renders its equipment grid, detail panel, and info boards **once** per page load (no `st.fragment`, no `run_every`) via `st.components.v1.html()`; an embedded `<script>` polls that JSON file every 2 seconds and updates colors/values directly in the DOM. Plant-toggle and equipment-click are handled entirely client-side in that same script - zero server round-trips for either.

**Tech Stack:** Python (Streamlit `st.cache_resource`, `st.components.v1.html`), SQLite (existing `config.db`/`machine_data.db`), vanilla JavaScript (no framework, no build step), existing `RuleEngine`/`DatabaseManager`.

## Global Constraints

- Refresh cadence: 2 seconds (per spec, confirmed with user).
- No new dependencies - no React/npm build toolchain (spec's explicit "out of scope").
- Snapshot file: `manuals/_scada_live.json` (leading underscore, obviously-not-a-manual), reachable at `/app/static/_scada_live.json`. `manuals/*` is already gitignored (`.gitignore` line: `manuals/*` with `!manuals/.gitkeep`), so this generated file is never at risk of being committed.
- One writer thread per server process, regardless of how many browser tabs/users are connected (`st.cache_resource`, not `st.cache_data`).
- Page is read-only (no writes), same as today.
- Staleness threshold: ~15 seconds without `generated_at` advancing shows a "data may be stale" notice.
- `ui/Home.py`'s `general_pages` list gets exactly one new entry, directly after the `pages/1_Ask_AI.py` entry.
- The page's own `st.set_page_config(...)` call must be removed (Home.py already calls it once; a second call raises an error).
- Test suite command: `python -m unittest discover tests` (existing convention - all new tests use `unittest.TestCase`, not pytest, to match).
- `streamlit.service` needs a restart to pick up the change (systemd, code loaded once at process start) - the user runs this themselves (no `sudo` access in this environment); confirm via `journalctl` afterward once they've done it.

---

## File Structure

| File | Responsibility |
|---|---|
| `ui/scada_floor_plan_data.py` (new) | Pure data/logic: constants (`SEVERITY_*`, `CATEGORY_ICON`, `ZONES`), equipment loading (`_load_equipment`), severity evaluation (`_tag_severity`, `_equipment_status`), zone grouping (`group_into_zones`), per-plant board figures (`build_board_snapshot`), full per-plant equipment snapshot (`build_equipment_snapshot`). No `st.markdown`/HTML rendering, no page config - safe to import from both the page and the background writer thread. |
| `ui/scada_snapshot_writer.py` (new) | `build_snapshot()` (assembles the full two-plant JSON document), `start_snapshot_writer()` (`st.cache_resource`-guarded daemon thread that calls `build_snapshot()` and writes it atomically every 2s). |
| `ui/pages/12_SCADA_Floor_Plan.py` (rewritten) | Renders the static page shell once (title, caption, legend, zone/equipment DOM skeleton, info-board skeleton) and embeds the polling `<script>` via `st.components.v1.html()`. Starts the snapshot writer. No more `st.fragment`, no more `st.set_page_config`. |
| `ui/Home.py` (modified) | One new `st.Page(...)` entry in `general_pages`, after Ask AI. |
| `tests/test_scada_floor_plan_data.py` (new) | Unit tests for the pure functions in `ui/scada_floor_plan_data.py`. |
| `tests/test_scada_snapshot_writer.py` (new) | Unit tests for `build_snapshot()`'s shape and the atomic-write helper. |

---

### Task 1: Extract pure data/logic into `ui/scada_floor_plan_data.py`

**Files:**
- Create: `ui/scada_floor_plan_data.py`
- Test: `tests/test_scada_floor_plan_data.py`

**Interfaces:**
- Consumes: `ai.rule_engine.RuleEngine.evaluate_summary(dict) -> dict` (existing, returns `{"condition": str, "severity": str, ...}`), `database.database.DatabaseManager.get_latest_all() -> dict[str, dict]`, `.get_latest_text_all() -> dict[str, dict]`, `.get_history(tag: str, hours: float, limit: int) -> list[dict]`, `ui.data_access.CONFIG_DATABASE_PATH: str`.
- Produces (used by Task 2 and Task 3):
  - `SEVERITY_COLOR: dict[str, str]`, `SEVERITY_RANK: dict[str, int]`, `SEVERITY_LABEL: dict[str, str]`, `SEVERITY_DOT: dict[str, str]`, `SEVERITY_TEXT: dict[str, str]`
  - `CATEGORY_ICON: dict[str, str]`, `DEFAULT_ICON: str`
  - `ZONES: list[dict]` (each `{"name": str, "categories": set[str]}` - `rect` key dropped, no longer needed now that the decorative SVG is gone and zones are plain grouped containers)
  - `_category_key(equipment_name: str) -> str`
  - `_short_code(equipment_name: str) -> str`
  - `_load_equipment(plant: str) -> list[dict]` (each dict: `name, display_name, category, icon, code, tags` where `tags` is `list[dict]` of `{tag_name, unit, data_type}`)
  - `_equipment_status(equipment: dict, latest: dict, latest_text: dict, rule_engine: RuleEngine) -> dict` (adds `overall_severity: str` and replaces `tags` with per-tag `{**tag, severity, value, updated}`)
  - `group_into_zones(equipment_list: list[dict]) -> list[tuple[str, list[dict]]]` (zone name -> equipment dicts, includes an `"Other"` bucket for anything unmatched, same grouping logic currently inlined in `_live_section`)
  - `build_equipment_snapshot(plant: str, database: DatabaseManager, rule_engine: RuleEngine) -> dict[str, dict]` (keyed by equipment `name`)
  - `build_board_snapshot(plant: str, database: DatabaseManager, rule_engine: RuleEngine) -> dict` (raw numeric figures, no HTML - see step 3 below for exact keys)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scada_floor_plan_data.py`:

```python
import tempfile
import unittest
from pathlib import Path

from ai.rule_engine import RuleEngine
from database.database import DatabaseManager
from database.initialize_config_db import initialize_config_database
from ui.scada_floor_plan_data import (
    CATEGORY_ICON,
    DEFAULT_ICON,
    SEVERITY_RANK,
    ZONES,
    _category_key,
    _equipment_status,
    _short_code,
    build_board_snapshot,
    build_equipment_snapshot,
    group_into_zones,
)


class TestCategoryAndCodeHelpers(unittest.TestCase):
    def test_category_key_strips_plant_and_instance(self):
        self.assertEqual(_category_key("p01_cold_room_cr01"), "cold_room")
        self.assertEqual(_category_key("p02_air_compressor_ac03"), "air_compressor")

    def test_category_key_falls_back_to_full_name_when_no_match(self):
        self.assertEqual(_category_key("not_the_convention"), "not_the_convention")

    def test_short_code_takes_last_underscore_segment_uppercased(self):
        self.assertEqual(_short_code("p01_air_compressor_ac01"), "AC01")
        self.assertEqual(_short_code("p01_main_incomer_main"), "MAIN")


class TestEquipmentStatus(unittest.TestCase):
    def test_worst_severity_wins_ties_correctly(self):
        """
        Regression test for the SEVERITY_RANK tie-break bug fixed
        2026-08-13: a "normal" REAL tag must outrank a "not_evaluated"
        STRING AlarmCode tag on the same equipment, regardless of
        which tag sorts first alphabetically.
        """
        equipment = {
            "name": "p01_air_compressor_ac02",
            "tags": [
                {"tag_name": "P01.UTILITY.AC02.AlarmCode", "unit": "", "data_type": "STRING"},
                {"tag_name": "P01.UTILITY.AC02.Pressure", "unit": "bar", "data_type": "REAL"},
            ],
        }
        latest = {"P01.UTILITY.AC02.Pressure": {"value": 7.2, "time": "2026-08-13 10:00:00"}}
        latest_text = {}

        class StubRuleEngine:
            def evaluate_summary(self, summary):
                return {"condition": "normal", "severity": "normal"}

        result = _equipment_status(equipment, latest, latest_text, StubRuleEngine())
        self.assertEqual(result["overall_severity"], "normal")

    def test_severity_rank_orders_alarm_above_everything(self):
        self.assertGreater(SEVERITY_RANK["alarm"], SEVERITY_RANK["warning"])
        self.assertGreater(SEVERITY_RANK["warning"], SEVERITY_RANK["normal"])
        self.assertGreater(SEVERITY_RANK["normal"], SEVERITY_RANK["not_evaluated"])
        self.assertGreater(SEVERITY_RANK["not_evaluated"], SEVERITY_RANK["no_data"])


class TestGroupIntoZones(unittest.TestCase):
    def test_known_category_placed_in_its_zone(self):
        equipment_list = [
            {"name": "p01_cold_room_cr01", "category": "cold_room", "display_name": "Cold Room CR01 (P01)"},
        ]
        zones = group_into_zones(equipment_list)
        zone_names = [name for name, _ in zones]
        self.assertIn("Cold Storage", zone_names)

    def test_unknown_category_falls_into_other(self):
        equipment_list = [
            {"name": "p01_mystery_thing_x01", "category": "mystery_thing", "display_name": "Mystery X01"},
        ]
        zones = group_into_zones(equipment_list)
        zone_names = [name for name, _ in zones]
        self.assertEqual(zone_names, ["Other"])

    def test_every_default_category_icon_has_a_zone_or_falls_back_cleanly(self):
        # Every category CATEGORY_ICON knows about should either match a
        # ZONES entry or safely land in "Other" - this test just proves
        # group_into_zones never raises/loses equipment either way.
        equipment_list = [
            {"name": f"p01_{category}_x01", "category": category, "display_name": category}
            for category in CATEGORY_ICON
        ]
        zones = group_into_zones(equipment_list)
        placed = sum(len(items) for _, items in zones)
        self.assertEqual(placed, len(equipment_list))


class TestSnapshotBuilders(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db_path = str(Path(self.temp_dir.name) / "config.db")
        self.machine_db_path = str(Path(self.temp_dir.name) / "machine_data.db")
        initialize_config_database(self.config_db_path)
        self.database = DatabaseManager(db_path=self.machine_db_path)
        self.rule_engine = RuleEngine(database_path=self.config_db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_build_equipment_snapshot_returns_dict_keyed_by_name_even_when_empty(self):
        # An empty (freshly-initialized) config.db has no equipment rows
        # for "p01" - build_equipment_snapshot must return {}, not raise.
        import ui.scada_floor_plan_data as data_module

        original_path = data_module.CONFIG_DATABASE_PATH
        data_module.CONFIG_DATABASE_PATH = self.config_db_path
        try:
            snapshot = build_equipment_snapshot("p01", self.database, self.rule_engine)
        finally:
            data_module.CONFIG_DATABASE_PATH = original_path

        self.assertEqual(snapshot, {})

    def test_build_board_snapshot_returns_expected_keys_with_no_data(self):
        import ui.scada_floor_plan_data as data_module

        original_path = data_module.CONFIG_DATABASE_PATH
        data_module.CONFIG_DATABASE_PATH = self.config_db_path
        try:
            board = build_board_snapshot("p01", self.database, self.rule_engine)
        finally:
            data_module.CONFIG_DATABASE_PATH = original_path

        expected_keys = {
            "today_kwh", "power_kw", "pf", "frequency", "currents", "voltages",
            "air_pressure", "air_flow", "water_pressure", "water_level",
            "alarm_count", "warning_count", "normal_count",
            "not_evaluated_count", "no_data_count", "total",
        }
        self.assertEqual(set(board.keys()), expected_keys)
        self.assertEqual(board["total"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_scada_floor_plan_data -v`
Expected: FAIL/ERROR - `ModuleNotFoundError: No module named 'ui.scada_floor_plan_data'` (module doesn't exist yet).

- [ ] **Step 3: Create `ui/scada_floor_plan_data.py`**

```python
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

import streamlit as st

from ai.rule_engine import RuleEngine
from database.database import DatabaseManager
from ui.data_access import CONFIG_DATABASE_PATH


SEVERITY_COLOR = {
    "alarm": "#e5484d",
    "warning": "#f5a524",
    "normal": "#3dd68c",
    "not_evaluated": "#8a8f98",
    "no_data": "#4a4f58",
}
# "normal" must outrank "not_evaluated": _equipment_status()'s max()
# picks the first tag on a tie, and an equipment's alphabetically-first
# tag is often a STRING AlarmCode (always "not_evaluated" absent a
# fault) - if it tied with a real, in-range measurement tag, the
# AlarmCode would win the tie and the equipment would wrongly show
# "no threshold configured" despite having real configured limits.
SEVERITY_RANK = {"alarm": 4, "warning": 3, "normal": 2, "not_evaluated": 1, "no_data": 0}
SEVERITY_LABEL = {
    "alarm": "Alarm",
    "warning": "Warning",
    "normal": "Normal",
    "not_evaluated": "No threshold configured",
    "no_data": "No data yet",
}
SEVERITY_DOT = {"alarm": "\U0001F534", "warning": "\U0001F7E0", "normal": "\U0001F7E2", "not_evaluated": "⚪", "no_data": "⚫"}
SEVERITY_TEXT = {
    "alarm": "#ffffff",
    "warning": "#1a1206",
    "normal": "#062017",
    "not_evaluated": "#14181f",
    "no_data": "#e6edf3",
}

CATEGORY_ICON = {
    "air_compressor": "\U0001F4A8",
    "compressed_air_header": "\U0001F32C️",
    "chiller": "❄️",
    "chilled_water_pump": "\U0001F4A7",
    "water_supply_pump": "\U0001F6B0",
    "water_supply": "\U0001F6B0",
    "cold_room": "\U0001F9CA",
    "bead_mill": "⚙️",
    "high_speed_disperser": "\U0001F300",
    "mixer": "\U0001F504",
    "filling_machine": "\U0001F9F4",
    "dust_collector": "\U0001F32A️",
    "tank": "\U0001F6E2️",
    "solvent_transfer": "\U0001F9EA",
    "water_treatment_system": "\U0001F6BF",
    "ro_di_system": "\U0001F4A7",
    "effluent_treatment": "♻️",
    "main_incomer": "⚡",
    "building_incomer": "⚡",
    "transformer": "\U0001F50C",
    "ups": "\U0001F50B",
    "generator": "⛽",
    "ahu": "\U0001F32C️",
    "mcc_room": "\U0001F5A5️",
    "area_monitoring": "\U0001F321️",
    "weather_node": "☁️",
    "building_water_meter": "\U0001F4A6",
    "fire_water_system": "\U0001F9EF",
}
DEFAULT_ICON = "\U0001F3ED"

# Which equipment "category" (the p01_<category>_<instance> naming
# app/ask.py's _equipment_category_key() also relies on) belongs in
# which imagined room. Anything unmatched falls into an auto-added
# "Other" zone via group_into_zones(), so a newly-enabled equipment
# type never disappears from the map, it just starts out ungrouped
# until this dict is taught about it.
ZONES = [
    {
        "name": "Utilities Yard",
        "categories": {
            "air_compressor", "compressed_air_header", "chiller",
            "chilled_water_pump", "water_supply_pump", "water_supply",
        },
    },
    {
        "name": "Electrical Room",
        "categories": {
            "main_incomer", "building_incomer", "transformer", "ups", "generator",
        },
    },
    {"name": "Cold Storage", "categories": {"cold_room"}},
    {
        "name": "Production Floor",
        "categories": {
            "bead_mill", "high_speed_disperser", "mixer",
            "filling_machine", "dust_collector",
        },
    },
    {"name": "Tank Farm", "categories": {"tank", "solvent_transfer"}},
    {
        "name": "Water Treatment Plant",
        "categories": {"water_treatment_system", "ro_di_system", "effluent_treatment"},
    },
    {
        "name": "HVAC & Monitoring",
        "categories": {
            "ahu", "mcc_room", "area_monitoring", "weather_node", "building_water_meter",
        },
    },
    {"name": "Fire Safety", "categories": {"fire_water_system"}},
]


def _category_key(equipment_name: str) -> str:
    """p01_cold_room_cr01 -> cold_room (same convention app/ask.py's
    _equipment_category_key() uses)."""
    match = re.match(r"^p\d+_(.+)_[a-z0-9]+$", equipment_name)
    return match.group(1) if match else equipment_name


def _short_code(equipment_name: str) -> str:
    """p01_air_compressor_ac01 -> AC01, p01_main_incomer_main -> MAIN."""
    return equipment_name.rsplit("_", 1)[-1].upper()


@st.cache_data(ttl=5)
def _load_equipment(plant: str) -> list[dict]:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        equipment_rows = connection.execute(
            "SELECT id, name, display_name FROM equipment WHERE name LIKE ? ORDER BY display_name",
            (f"{plant}_%",),
        ).fetchall()

        equipment = []

        for row in equipment_rows:
            tag_rows = connection.execute(
                "SELECT tag_name, unit, data_type FROM tags "
                "WHERE equipment_id = ? AND enabled = 1 ORDER BY tag_name",
                (row["id"],),
            ).fetchall()

            if not tag_rows:
                continue

            category = _category_key(row["name"])
            equipment.append(
                {
                    "name": row["name"],
                    "display_name": row["display_name"],
                    "category": category,
                    "icon": CATEGORY_ICON.get(category, DEFAULT_ICON),
                    "code": _short_code(row["name"]),
                    "tags": [dict(t) for t in tag_rows],
                }
            )
    finally:
        connection.close()

    return equipment


def _tag_severity(tag_name: str, data_type: str, latest: dict, latest_text: dict, rule_engine: RuleEngine) -> str:
    if data_type == "STRING":
        text_reading = latest_text.get(tag_name)
        if text_reading is None:
            return "no_data"
        if tag_name.endswith(".AlarmCode") and text_reading["value"] not in (None, "None"):
            return "alarm"
        return "not_evaluated"

    reading = latest.get(tag_name)
    if reading is None:
        return "no_data"

    result = rule_engine.evaluate_summary({"tag": tag_name, "current": reading["value"]})

    if result["condition"] == "not_evaluated":
        return "not_evaluated"

    return result["severity"]


def _equipment_status(equipment: dict, latest: dict, latest_text: dict, rule_engine: RuleEngine) -> dict:
    per_tag = [
        {
            **tag,
            "severity": _tag_severity(tag["tag_name"], tag["data_type"], latest, latest_text, rule_engine),
            "value": (latest_text.get(tag["tag_name"], {}).get("value") if tag["data_type"] == "STRING" else (latest.get(tag["tag_name"], {}) or {}).get("value")),
            "updated": (latest_text.get(tag["tag_name"], {}).get("time") if tag["data_type"] == "STRING" else (latest.get(tag["tag_name"], {}) or {}).get("time")),
        }
        for tag in equipment["tags"]
    ]

    worst = max(per_tag, key=lambda t: SEVERITY_RANK[t["severity"]])["severity"] if per_tag else "no_data"

    return {**equipment, "tags": per_tag, "overall_severity": worst}


def group_into_zones(equipment_list: list[dict]) -> list[tuple[str, list[dict]]]:
    """
    Groups equipment (by their 'category' field) into the imagined
    room layout, sorted by display_name within each zone. Equipment
    whose category doesn't match any ZONES entry lands in one "Other"
    bucket at the end rather than disappearing. Works on either plain
    _load_equipment() output or _equipment_status()-evaluated output -
    only 'category'/'name'/'display_name' are used.
    """
    by_category: dict[str, list[dict]] = {}
    for eq in equipment_list:
        by_category.setdefault(eq["category"], []).append(eq)

    placed_names = set()
    zone_groups = []
    for zone in ZONES:
        zone_equipment = []
        for category in sorted(zone["categories"]):
            for eq in by_category.get(category, []):
                zone_equipment.append(eq)
                placed_names.add(eq["name"])
        if zone_equipment:
            zone_groups.append((zone["name"], sorted(zone_equipment, key=lambda e: e["display_name"])))

    leftover = [eq for eq in equipment_list if eq["name"] not in placed_names]
    if leftover:
        zone_groups.append(("Other", sorted(leftover, key=lambda e: e["display_name"])))

    return zone_groups


def _todays_energy_kwh(database: DatabaseManager, tag_name: str) -> float | None:
    """
    Energy_kWh is a monotonic running total (never resets), so "today's
    consumption" is the delta between its value at midnight and now.
    None if nothing's logged yet today, rather than a misleading 0.
    """
    now = datetime.now()
    hours_since_midnight = max(
        0.05, (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600
    )

    try:
        rows = database.get_history(tag=tag_name, hours=hours_since_midnight, limit=3000)
    except Exception:
        return None

    if len(rows) < 2:
        return None

    return rows[-1]["value"] - rows[0]["value"]


def build_equipment_snapshot(plant: str, database: DatabaseManager, rule_engine: RuleEngine) -> dict[str, dict]:
    equipment_list = _load_equipment(plant)
    latest = database.get_latest_all()
    latest_text = database.get_latest_text_all()

    snapshot: dict[str, dict] = {}
    for equipment in equipment_list:
        evaluated = _equipment_status(equipment, latest, latest_text, rule_engine)
        snapshot[evaluated["name"]] = {
            "display_name": evaluated["display_name"],
            "code": evaluated["code"],
            "icon": evaluated["icon"],
            "category": evaluated["category"],
            "overall_severity": evaluated["overall_severity"],
            "tags": [
                {
                    "tag_name": tag["tag_name"],
                    "unit": tag.get("unit") or "",
                    "value": tag["value"],
                    "severity": tag["severity"],
                    "updated": tag["updated"],
                }
                for tag in evaluated["tags"]
            ],
        }
    return snapshot


def build_board_snapshot(plant: str, database: DatabaseManager, rule_engine: RuleEngine) -> dict:
    equipment_list = _load_equipment(plant)
    latest = database.get_latest_all()
    latest_text = database.get_latest_text_all()
    evaluated = [_equipment_status(eq, latest, latest_text, rule_engine) for eq in equipment_list]

    incomer_prefix = f"{plant.upper()}.ELEC.MAIN"
    today_kwh = _todays_energy_kwh(database, f"{incomer_prefix}.Energy_kWh")
    power = latest.get(f"{incomer_prefix}.Power_kW")
    pf = latest.get(f"{incomer_prefix}.PF")
    freq = latest.get(f"{incomer_prefix}.Frequency")
    currents = [latest.get(f"{incomer_prefix}.Current_L{p}") for p in (1, 2, 3)]
    voltages = [
        latest.get(f"{incomer_prefix}.Voltage_L1L2"),
        latest.get(f"{incomer_prefix}.Voltage_L2L3"),
        latest.get(f"{incomer_prefix}.Voltage_L3L1"),
    ]

    air_pressure = latest.get(f"{plant.upper()}.UTILITY.AIRHDR01.Pressure")
    air_flow = latest.get(f"{plant.upper()}.UTILITY.AIRHDR01.Flow")
    water_pressure = latest.get(f"{plant.upper()}.WATER.SYS01.HeaderPressure")
    water_level = latest.get(f"{plant.upper()}.WATER.SYS01.TankLevel")

    alarm_count = sum(1 for e in evaluated if e["overall_severity"] == "alarm")
    warning_count = sum(1 for e in evaluated if e["overall_severity"] == "warning")
    normal_count = sum(1 for e in evaluated if e["overall_severity"] == "normal")
    not_evaluated_count = sum(1 for e in evaluated if e["overall_severity"] == "not_evaluated")
    no_data_count = sum(1 for e in evaluated if e["overall_severity"] == "no_data")

    return {
        "today_kwh": today_kwh,
        "power_kw": power["value"] if power else None,
        "pf": pf["value"] if pf else None,
        "frequency": freq["value"] if freq else None,
        "currents": [c["value"] if c else None for c in currents],
        "voltages": [v["value"] if v else None for v in voltages],
        "air_pressure": air_pressure["value"] if air_pressure else None,
        "air_flow": air_flow["value"] if air_flow else None,
        "water_pressure": water_pressure["value"] if water_pressure else None,
        "water_level": water_level["value"] if water_level else None,
        "alarm_count": alarm_count,
        "warning_count": warning_count,
        "normal_count": normal_count,
        "not_evaluated_count": not_evaluated_count,
        "no_data_count": no_data_count,
        "total": len(evaluated),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_scada_floor_plan_data -v`
Expected: PASS (all tests green).

- [ ] **Step 5: Commit**

```bash
git add ui/scada_floor_plan_data.py tests/test_scada_floor_plan_data.py
git commit -m "Extract SCADA floor plan data/logic into a reusable module"
```

---

### Task 2: Background snapshot writer

**Files:**
- Create: `ui/scada_snapshot_writer.py`
- Test: `tests/test_scada_snapshot_writer.py`

**Interfaces:**
- Consumes: `ui.scada_floor_plan_data.build_equipment_snapshot`, `.build_board_snapshot` (Task 1), `database.database.DatabaseManager`, `ai.rule_engine.RuleEngine`, `ui.data_access.CONFIG_DATABASE_PATH`, `ui.data_access.MACHINE_DATABASE_PATH`.
- Produces (used by Task 3):
  - `SNAPSHOT_PATH: Path` - absolute path to `manuals/_scada_live.json`.
  - `WRITE_INTERVAL_SECONDS: int` (2).
  - `build_snapshot() -> dict` - `{"generated_at": "YYYY-MM-DD HH:MM:SS", "plants": {"p01": {"board": {...}, "equipment": {...}}, "p02": {...}}}`.
  - `start_snapshot_writer() -> None` - call at page load; idempotent (only the first call in the process actually starts the thread).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scada_snapshot_writer.py`:

```python
import json
import tempfile
import time
import unittest
from pathlib import Path

from ui.scada_snapshot_writer import _write_snapshot_atomic, build_snapshot


class TestBuildSnapshot(unittest.TestCase):
    def test_build_snapshot_has_both_plants_and_generated_at(self):
        snapshot = build_snapshot()
        self.assertIn("generated_at", snapshot)
        self.assertIn("plants", snapshot)
        self.assertEqual(set(snapshot["plants"].keys()), {"p01", "p02"})
        for plant_data in snapshot["plants"].values():
            self.assertIn("board", plant_data)
            self.assertIn("equipment", plant_data)

    def test_build_snapshot_is_json_serializable(self):
        snapshot = build_snapshot()
        # Raises if anything non-serializable (e.g. a raw sqlite3.Row) slipped through.
        json.dumps(snapshot)


class TestWriteSnapshotAtomic(unittest.TestCase):
    def test_writes_valid_json_readable_at_target_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "_scada_live.json"
            _write_snapshot_atomic({"generated_at": "2026-08-13 10:00:00", "plants": {}}, target)

            self.assertTrue(target.exists())
            with open(target) as handle:
                loaded = json.load(handle)
            self.assertEqual(loaded["generated_at"], "2026-08-13 10:00:00")

    def test_no_temp_file_left_behind_after_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "_scada_live.json"
            _write_snapshot_atomic({"generated_at": "x", "plants": {}}, target)

            remaining = list(Path(temp_dir).iterdir())
            self.assertEqual(remaining, [target])

    def test_overwrites_existing_file_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "_scada_live.json"
            _write_snapshot_atomic({"generated_at": "first", "plants": {}}, target)
            _write_snapshot_atomic({"generated_at": "second", "plants": {}}, target)

            with open(target) as handle:
                loaded = json.load(handle)
            self.assertEqual(loaded["generated_at"], "second")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_scada_snapshot_writer -v`
Expected: FAIL/ERROR - `ModuleNotFoundError: No module named 'ui.scada_snapshot_writer'`.

- [ ] **Step 3: Create `ui/scada_snapshot_writer.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_scada_snapshot_writer -v`
Expected: PASS (all tests green).

- [ ] **Step 5: Commit**

```bash
git add ui/scada_snapshot_writer.py tests/test_scada_snapshot_writer.py
git commit -m "Add background snapshot writer for the SCADA floor plan live view"
```

---

### Task 3: Rewrite the page as a static shell + polling JS, drop the fragment

**Files:**
- Modify: `ui/pages/12_SCADA_Floor_Plan.py` (full rewrite)

**Interfaces:**
- Consumes: everything from Task 1 (`ui.scada_floor_plan_data`) and Task 2 (`ui.scada_snapshot_writer.start_snapshot_writer`).
- Produces: nothing consumed by later tasks (leaf of the dependency graph on the Python side); the embedded JS's `fetch()` target (`/app/static/_scada_live.json`) is a contract with Task 2's `SNAPSHOT_PATH` (`manuals/_scada_live.json`, served via the existing `ui/static -> ../manuals` symlink + `.streamlit/config.toml`'s `enableStaticServing = true` - both already in place, no changes needed here).

- [ ] **Step 1: Replace the file**

```python
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.scada_floor_plan_data import (
    SEVERITY_COLOR,
    SEVERITY_DOT,
    SEVERITY_LABEL,
    SEVERITY_TEXT,
    _load_equipment,
)

# ---------------------------------------------------------------------------
# SCADA-style mimic diagram - part of the main app since 2026-08-13. No real
# factory floor plan exists to copy - the room layout is an imagined,
# plausible arrangement built from the tag-naming convention's own area
# codes (P01.UTILITY.*, P01.COLDROOM.*, P01.PROD.*, ...). Every equipment
# instance, tag, and live value shown is real data from the running system -
# only the physical room positions are invented.
#
# Architecture (see docs/superpowers/specs/2026-08-13-scada-floor-plan-live-
# view-design.md for the full design rationale): a background thread
# (ui/scada_snapshot_writer.py, started below) writes a JSON snapshot of
# both plants to manuals/_scada_live.json every 2 seconds. This page renders
# its shell ONCE (no st.fragment, no run_every - three earlier rounds of
# fixes confirmed, against Streamlit's own fragment.py source, that
# run_every fragments redraw everything inside them on every tick,
# native widgets included, which is what caused the visible flashing this
# rewrite exists to fix). The embedded <script> below polls that JSON file
# directly and updates the DOM in place - no Streamlit rerun of any kind is
# involved in the live updates, so nothing can flash.
# ---------------------------------------------------------------------------

from ui.scada_snapshot_writer import start_snapshot_writer

start_snapshot_writer()

FONT_STACK = "'Source Sans Pro', -apple-system, 'Segoe UI', sans-serif"
MONO_STACK = "'Source Code Pro', 'SF Mono', Consolas, monospace"

st.title("\U0001F3ED SCADA Floor Plan")
st.caption(
    "Room layout is imagined (no real factory drawing exists); every status/value shown "
    "is real, live data from the system, refreshed every 2 seconds. Click any equipment "
    "below its room to see its live tag values."
)

legend_cols = st.columns(5)
for col, key in zip(legend_cols, ["alarm", "warning", "normal", "not_evaluated", "no_data"]):
    with col:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:8px;font-family:{FONT_STACK};">'
            f'<div style="width:16px;height:16px;border-radius:4px;'
            f'background:{SEVERITY_COLOR[key]};flex-shrink:0;"></div>'
            f'<span style="font-size:13px;">{SEVERITY_LABEL[key]}</span></div>',
            unsafe_allow_html=True,
        )


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_plant_shell_html(plant: str) -> str:
    """
    Static (never-changing) DOM structure for one plant: zone containers
    with one equipment "card" div per equipment instance, each tagged
    with data-name so the polling script can find and update it. Colors
    start at SEVERITY_COLOR["no_data"] since no live snapshot has been
    fetched yet on first paint.
    """
    from ui.scada_floor_plan_data import group_into_zones

    equipment_list = _load_equipment(plant)
    zone_groups = group_into_zones(equipment_list)

    zones_html = []
    for zone_name, zone_equipment in zone_groups:
        cards = []
        for eq in zone_equipment:
            cards.append(
                f'<div class="scada-card" id="scada-eq-{_escape(eq["name"])}" '
                f'data-name="{_escape(eq["name"])}" data-plant="{plant}" '
                f'title="{_escape(eq["display_name"])} - waiting for live data..." '
                f'style="background:{SEVERITY_COLOR["no_data"]};color:{SEVERITY_TEXT["no_data"]};">'
                f'{eq["icon"]} {_escape(eq["code"])}</div>'
            )
        zones_html.append(
            f'<div class="scada-zone"><div class="scada-zone-title">{_escape(zone_name.upper())}</div>'
            f'<div class="scada-zone-cards">{"".join(cards)}</div></div>'
        )

    display_style = "" if plant == "p01" else "display:none;"
    return f'<div class="scada-plant" id="scada-plant-{plant}" style="{display_style}">{"".join(zones_html)}</div>'


shell_html = "".join(_build_plant_shell_html(plant) for plant in ("p01", "p02"))
severity_colors_json = json.dumps(SEVERITY_COLOR)
severity_text_json = json.dumps(SEVERITY_TEXT)
severity_label_json = json.dumps(SEVERITY_LABEL)
severity_dot_json = json.dumps(SEVERITY_DOT)

page_html = f"""
<style>
  body {{ margin:0; background:#0d1117; color:#e6edf3; font-family:{FONT_STACK}; }}
  .scada-toolbar {{ display:flex; gap:8px; align-items:center; margin-bottom:12px; }}
  .scada-toolbar button {{
    background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px;
    padding:6px 16px; font-size:13px; cursor:pointer; font-family:{FONT_STACK};
  }}
  .scada-toolbar button.active {{ background:#3d5afe; border-color:#3d5afe; }}
  #scada-stale-banner {{
    display:none; background:#3a2a06; color:#f5a524; border:1px solid #f5a524;
    border-radius:6px; padding:8px 12px; margin-bottom:12px; font-size:13px;
  }}
  .scada-layout {{ display:flex; gap:24px; align-items:flex-start; }}
  .scada-floor {{ flex:3; }}
  .scada-boards {{ flex:1; min-width:260px; }}
  .scada-zone {{ border:1px solid #30363d; border-radius:8px; padding:10px 12px; margin-bottom:12px; }}
  .scada-zone-title {{ font-size:12px; color:#8a8f98; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:8px; }}
  .scada-zone-cards {{ display:flex; flex-wrap:wrap; gap:8px; }}
  .scada-card {{
    padding:8px 14px; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer;
    border:1px solid rgba(255,255,255,0.15); user-select:none;
  }}
  .scada-card:hover {{ filter:brightness(1.1); }}
  #scada-detail {{ border-top:1px solid #30363d; padding-top:12px; margin-top:8px; }}
  .scada-detail-row {{
    display:grid; grid-template-columns:14px 1fr 110px 150px; gap:12px;
    align-items:center; padding:6px 0; border-bottom:1px solid #21262d; font-size:13px;
  }}
  .scada-detail-header {{ color:#8a8f98; font-size:11px; text-transform:uppercase; letter-spacing:0.05em; }}
  .scada-dot {{ width:10px; height:10px; border-radius:50%; }}
  .scada-board-heading {{ font-size:12px; font-weight:700; color:#c9d1d9; margin:8px 0 2px; }}
  .scada-board-row {{
    display:flex; justify-content:space-between; align-items:baseline; padding:2px 0;
    border-bottom:1px solid #1c2128; font-size:12px;
  }}
  .scada-board-row .label {{ font-size:11.5px; color:#8a8f98; }}
  .scada-board-row .value {{ font-size:12.5px; font-weight:600; white-space:nowrap; }}
</style>

<div id="scada-stale-banner">⚠️ Live data may be stale - the background updater hasn't reported in recently.</div>

<div class="scada-toolbar">
  <button id="scada-tab-p01" class="active" onclick="scadaSetPlant('p01')">P01</button>
  <button id="scada-tab-p02" onclick="scadaSetPlant('p02')">P02</button>
  <span id="scada-asof" style="font-size:12px;color:#8a8f98;margin-left:8px;">waiting for live data...</span>
</div>

<div class="scada-layout">
  <div class="scada-floor">
    {shell_html}
    <div id="scada-detail">
      <div id="scada-detail-body" style="font-size:13px;color:#8a8f98;">
        Click any equipment above to see its live tag values.
      </div>
    </div>
  </div>
  <div class="scada-boards">
    <div style="font-weight:700;font-size:13px;">⚡ P01 Main Incomer</div>
    <div id="scada-board-p01"></div>
    <hr style="border-color:#30363d;margin:12px 0;">
    <div style="font-weight:700;font-size:13px;">⚡ P02 Main Incomer</div>
    <div id="scada-board-p02"></div>
  </div>
</div>

<script>
const SEVERITY_COLOR = {severity_colors_json};
const SEVERITY_TEXT = {severity_text_json};
const SEVERITY_LABEL = {severity_label_json};
const SEVERITY_DOT = {severity_dot_json};

let scadaCurrentPlant = "p01";
let scadaSelectedEquipment = null;
let scadaLastGeneratedAt = null;
let scadaLastAdvanceTime = Date.now();
let scadaLatestSnapshot = null;

function scadaSetPlant(plant) {{
  scadaCurrentPlant = plant;
  document.getElementById("scada-plant-p01").style.display = plant === "p01" ? "" : "none";
  document.getElementById("scada-plant-p02").style.display = plant === "p02" ? "" : "none";
  document.getElementById("scada-tab-p01").classList.toggle("active", plant === "p01");
  document.getElementById("scada-tab-p02").classList.toggle("active", plant === "p02");
  if (scadaSelectedEquipment && !scadaSelectedEquipment.startsWith(plant + "_")) {{
    scadaSelectedEquipment = null;
    document.getElementById("scada-detail-body").innerHTML =
      "Click any equipment above to see its live tag values.";
  }}
}}

function scadaFormatNumber(value, decimals) {{
  if (value === null || value === undefined) return "-";
  return Number(value).toFixed(decimals);
}}

function scadaBoardHeading(text) {{
  return '<div class="scada-board-heading">' + text + '</div>';
}}

function scadaBoardRow(label, value, dot) {{
  const dotHtml = dot ? '<span style="margin-right:4px;">' + dot + '</span>' : '';
  return '<div class="scada-board-row"><span class="label">' + label +
    '</span><span class="value">' + dotHtml + value + '</span></div>';
}}

function scadaRenderBoard(plant, board) {{
  let html = scadaBoardHeading('Electrical');
  html += scadaBoardRow('Today\\'s consumption',
    board.today_kwh !== null ? scadaFormatNumber(board.today_kwh, 1) + ' kWh' : '-');
  html += scadaBoardRow('Power draw',
    board.power_kw !== null ? scadaFormatNumber(board.power_kw, 1) + ' kW' : '-');
  if (board.currents.some(c => c !== null)) {{
    const currentText = board.currents.map(c => c !== null ? scadaFormatNumber(c, 1) : '-').join(' / ');
    html += scadaBoardRow('Current L1/L2/L3', currentText + ' A');
  }}
  if (board.voltages.some(v => v !== null)) {{
    const voltageText = board.voltages.map(v => v !== null ? scadaFormatNumber(v, 0) : '-').join(' / ');
    html += scadaBoardRow('Voltage L1-2/L2-3/L3-1', voltageText + ' V');
  }}
  html += scadaBoardRow('Power factor / freq',
    (board.pf !== null && board.frequency !== null)
      ? scadaFormatNumber(board.pf, 2) + ' / ' + scadaFormatNumber(board.frequency, 1) + ' Hz' : '-');

  if (board.air_pressure !== null || board.water_pressure !== null) {{
    html += scadaBoardHeading('Utilities');
    if (board.air_pressure !== null) {{
      let text = scadaFormatNumber(board.air_pressure, 1) + ' bar';
      if (board.air_flow !== null) text += ' / ' + scadaFormatNumber(board.air_flow, 0) + ' Nm³/h';
      html += scadaBoardRow('Compressed air header', text);
    }}
    if (board.water_pressure !== null) {{
      let text = scadaFormatNumber(board.water_pressure, 1) + ' bar';
      if (board.water_level !== null) text += ' / ' + scadaFormatNumber(board.water_level, 0) + '% tank';
      html += scadaBoardRow('Water supply header', text);
    }}
  }}

  const total = board.total || 1;
  html += scadaBoardHeading('Plant Health');
  html += scadaBoardRow('Equipment normal',
    board.normal_count + '/' + board.total + ' (' + Math.round(100 * board.normal_count / total) + '%)');
  html += scadaBoardRow('In alarm', String(board.alarm_count), board.alarm_count ? SEVERITY_DOT.alarm : '');
  html += scadaBoardRow('In warning', String(board.warning_count), board.warning_count ? SEVERITY_DOT.warning : '');
  if (board.not_evaluated_count || board.no_data_count) {{
    html += scadaBoardRow('No threshold / no data', board.not_evaluated_count + ' / ' + board.no_data_count);
  }}

  document.getElementById('scada-board-' + plant).innerHTML = html;
}}

function scadaRenderDetail(equipmentName) {{
  if (!scadaLatestSnapshot) return;
  const plant = equipmentName.startsWith('p01_') ? 'p01' : 'p02';
  const eq = scadaLatestSnapshot.plants[plant].equipment[equipmentName];
  if (!eq) return;

  const alarmCount = eq.tags.filter(t => t.severity === 'alarm').length;
  const warningCount = eq.tags.filter(t => t.severity === 'warning').length;
  let summary;
  if (alarmCount || warningCount) {{
    const bits = [];
    if (alarmCount) bits.push(alarmCount + ' alarm');
    if (warningCount) bits.push(warningCount + ' warning');
    summary = '<div style="color:#f5a524;">' + bits.join(' and ') + ' on this equipment.</div>';
  }} else {{
    summary = '<div style="color:#3dd68c;">Everything within configured limits.</div>';
  }}

  let rows = '<div class="scada-detail-row scada-detail-header"><div></div><div>Tag</div>' +
    '<div style="text-align:right;">Value</div><div style="text-align:right;">Updated</div></div>';
  const sortedTags = [...eq.tags].sort((a, b) => a.tag_name.localeCompare(b.tag_name));
  for (const tag of sortedTags) {{
    const value = tag.value !== null && tag.value !== undefined ? tag.value : '-';
    const unit = tag.unit ? ' ' + tag.unit : '';
    const updated = tag.updated || 'never logged';
    rows += '<div class="scada-detail-row">' +
      '<div class="scada-dot" style="background:' + SEVERITY_COLOR[tag.severity] + ';"></div>' +
      '<div style="font-family:monospace;font-size:13px;">' + tag.tag_name + '</div>' +
      '<div style="font-weight:600;font-size:13px;text-align:right;">' + value + unit + '</div>' +
      '<div style="color:#8a8f98;font-size:12px;text-align:right;">' + updated + '</div></div>';
  }}

  document.getElementById('scada-detail-body').innerHTML =
    '<h4 style="margin:4px 0 8px;">' + eq.display_name + '</h4>' + summary + rows;
}}

function scadaApplySnapshot(snapshot) {{
  scadaLatestSnapshot = snapshot;

  if (snapshot.generated_at !== scadaLastGeneratedAt) {{
    scadaLastGeneratedAt = snapshot.generated_at;
    scadaLastAdvanceTime = Date.now();
    document.getElementById('scada-stale-banner').style.display = 'none';
  }}
  document.getElementById('scada-asof').textContent = 'As of ' + snapshot.generated_at;

  for (const plant of ['p01', 'p02']) {{
    for (const [name, eq] of Object.entries(snapshot.plants[plant].equipment)) {{
      const card = document.getElementById('scada-eq-' + name);
      if (!card) continue;
      card.style.background = SEVERITY_COLOR[eq.overall_severity];
      card.style.color = SEVERITY_TEXT[eq.overall_severity];
      card.title = eq.display_name + ' - ' + SEVERITY_LABEL[eq.overall_severity];
    }}
    scadaRenderBoard(plant, snapshot.plants[plant].board);
  }}

  if (scadaSelectedEquipment) {{
    scadaRenderDetail(scadaSelectedEquipment);
  }}
}}

function scadaPoll() {{
  fetch('/app/static/_scada_live.json?_=' + Date.now())
    .then(response => {{
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    }})
    .then(scadaApplySnapshot)
    .catch(error => {{
      console.error('SCADA snapshot fetch failed:', error);
    }});

  if (Date.now() - scadaLastAdvanceTime > 15000) {{
    document.getElementById('scada-stale-banner').style.display = 'block';
  }}
}}

document.addEventListener('click', function (event) {{
  const card = event.target.closest('.scada-card');
  if (!card) return;
  scadaSelectedEquipment = card.dataset.name;
  scadaRenderDetail(scadaSelectedEquipment);
}});

scadaPoll();
setInterval(scadaPoll, 2000);
</script>
"""

components.html(page_html, height=1400, scrolling=True)
```

- [ ] **Step 2: Compile-check the file**

Run: `python3 -m py_compile ui/pages/12_SCADA_Floor_Plan.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Verify with AppTest**

Run this as a one-off script (not part of the permanent test suite, since `AppTest`
cannot execute the embedded JS or verify the live-updating behavior - it only proves
the page renders without a server-side exception and that the background thread
singleton starts):

```python
from streamlit.testing.v1 import AppTest

at = AppTest.from_file("ui/pages/12_SCADA_Floor_Plan.py")
at.run()
assert not at.exception, at.exception
print("OK: page rendered with no exception")
```

Run: `python3 -c "$(cat above script)"` (or save to a temp `.py` and run it).
Expected: `OK: page rendered with no exception`.

- [ ] **Step 4: Commit**

```bash
git add ui/pages/12_SCADA_Floor_Plan.py
git commit -m "Rewrite SCADA Floor Plan as a flicker-free JS-polling live view"
```

---

### Task 4: Wire the page into `ui/Home.py`'s navigation

**Files:**
- Modify: `ui/Home.py`

**Interfaces:**
- Consumes: `ui/pages/12_SCADA_Floor_Plan.py` (Task 3) as a `st.Page(...)` target - no Python-level import, Streamlit loads it by file path.

- [ ] **Step 1: Add the page entry**

In `ui/Home.py`, change:

```python
general_pages = [
    st.Page(_overview_page, title="Home", icon="🏭", default=True),
    st.Page("pages/1_Ask_AI.py", title="Ask AI", icon="💬"),
    st.Page("pages/2_Live_Data.py", title="Live Data", icon="📊"),
    st.Page("pages/5_Event_Records.py", title="Event Records", icon="📋"),
]
```

to:

```python
general_pages = [
    st.Page(_overview_page, title="Home", icon="🏭", default=True),
    st.Page("pages/1_Ask_AI.py", title="Ask AI", icon="💬"),
    st.Page("pages/12_SCADA_Floor_Plan.py", title="SCADA Floor Plan", icon="🗺️"),
    st.Page("pages/2_Live_Data.py", title="Live Data", icon="📊"),
    st.Page("pages/5_Event_Records.py", title="Event Records", icon="📋"),
]
```

- [ ] **Step 2: Compile-check**

Run: `python3 -m py_compile ui/Home.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Verify with AppTest**

```python
from streamlit.testing.v1 import AppTest

at = AppTest.from_file("ui/Home.py")
at.run()
assert not at.exception, at.exception
print("OK: Home.py rendered with no exception")
```

Note: this only verifies `Home.py` itself (the login gate + navigation shell)
renders without error - `AppTest.from_file` does not execute the selected sub-page
by default, so it does not exercise `12_SCADA_Floor_Plan.py` a second time here
(that's already covered by Task 3 Step 4).

- [ ] **Step 4: Commit**

```bash
git add ui/Home.py
git commit -m "Add SCADA Floor Plan to the main app's sidebar navigation"
```

---

### Task 5: Real end-to-end verification + full regression suite + retire port 8503

**Files:** none (verification-only task).

- [ ] **Step 1: Stop the standalone port-8503 process, if still running**

```bash
pkill -f "streamlit run ui/pages/12_SCADA_Floor_Plan.py" || true
ss -tln | grep 8503 || echo "port 8503 is free"
```

- [ ] **Step 2: Run the full regression suite**

```bash
python -m unittest discover tests 2>&1 | tail -30
```

Expected: the pre-existing baseline failures only (per CLAUDE.md's "Regression
baseline note" - confirm the exact current count/files by running
`python -m unittest discover tests 2>&1 | grep -E "^(FAIL|ERROR)"` and comparing
against `test_equipment_knowledge.py`/`test_hybrid_router.py`/`test_pipeline.py` -
all pre-existing, unrelated dead-code failures). Zero new failures from
`tests/test_scada_floor_plan_data.py` or `tests/test_scada_snapshot_writer.py`
(both should be fully green) or from anything else touched in this plan.

- [ ] **Step 3: Ask the user to restart `streamlit.service`**

This environment has no `sudo`/TTY access. Ask the user to run, in their own
terminal (or via a Claude Code `! <command>` prefix):

```bash
sudo systemctl restart streamlit.service
```

- [ ] **Step 4: Confirm the restart picked up the change**

```bash
sudo systemctl status streamlit.service --no-pager | head -10
journalctl -u streamlit.service -n 30 --no-pager
```

Expected: recent start timestamp, no startup traceback.

- [ ] **Step 5: Confirm the snapshot file is being written**

```bash
ls -la /home/test/SmartMachineAI/manuals/_scada_live.json
sleep 5
stat -c '%Y' /home/test/SmartMachineAI/manuals/_scada_live.json
sleep 5
stat -c '%Y' /home/test/SmartMachineAI/manuals/_scada_live.json
```

Expected: the file exists and its modification timestamp advances between the two
`stat` calls (proves the writer thread is genuinely ticking every 2s in the live
service, not just in a test).

- [ ] **Step 6: Confirm the static route serves it**

```bash
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://localhost:8501/app/static/_scada_live.json
```

Expected: `200 application/json`.

- [ ] **Step 7: Manual browser check (ask the user, since no browser tool is available here)**

Ask the user to open `http://localhost:8501` (or the VM's LAN IP), log in, and
confirm: "SCADA Floor Plan" appears in the sidebar directly below "Ask AI"; the
page loads with equipment colored correctly; clicking equipment shows its tag
detail with no visible page flash; toggling P01/P02 switches instantly with no
flash; leaving the tab open for 20+ seconds shows the "As of ..." timestamp
advancing without any visible redraw/flicker.

- [ ] **Step 8: Report status back to the user**

Summarize: regression suite result, snapshot-file-advancing confirmation, static
route confirmation, and the specific browser checks still pending their own
look (matching this project's standing "worth the user's own look before
trusting the visual polish fully" discipline for anything not directly
browser-verifiable from this environment).

---

## Self-Review Notes

- **Spec coverage:** background writer (Task 2) ✓, static JS-driven page with
  2s polling + client-side plant-toggle/equipment-click (Task 3) ✓, snapshot
  file location reusing the existing `ui/static` symlink (Task 2/3, no new
  static-serving mechanism) ✓, staleness detection at ~15s (Task 3's
  `scadaPoll()`) ✓, `st.Page` integration below Ask AI + removal of
  `st.set_page_config` and standalone framing (Task 3 Step 1, Task 4) ✓, error
  handling for missing/stale snapshot (Task 3's `.catch()` + stale banner) and
  per-tick writer try/except (Task 2) ✓, testing plan items (py_compile,
  AppTest, live end-to-end check, full regression suite, service restart) all
  covered across Tasks 3-5 ✓.
- **Deliberate simplification vs. the spec's JSON schema sketch:** the spec's
  sketch included a `"zones"` key per plant; this plan omits it from the JSON
  and instead builds the zone/equipment DOM structure once, server-side, at
  shell-render time (Task 3's `_build_plant_shell_html`) - zone membership
  never changes between polls, so there's nothing for the JSON to usefully
  carry there. The live snapshot only ever updates values inside
  already-placed DOM elements, never restructures the page. This preserves
  every actual requirement (2s live values, flicker-free, client-side
  interactions, staleness detection) with one less moving part.
- **Placeholder scan:** no TBD/TODO markers or unfilled code remain anywhere
  in the plan - every step's code block is complete and directly usable.
- **Type/name consistency:** `build_equipment_snapshot`/`build_board_snapshot`
  (Task 1) are the exact names imported in `ui/scada_snapshot_writer.py`
  (Task 2); `SNAPSHOT_PATH`/`start_snapshot_writer` (Task 2) are the exact
  names imported in the page (Task 3); the JSON keys produced by
  `build_board_snapshot` (Task 1) exactly match the keys read by
  `scadaRenderBoard()` in the embedded JS (Task 3) - cross-checked field by
  field (`today_kwh`, `power_kw`, `pf`, `frequency`, `currents`, `voltages`,
  `air_pressure`, `air_flow`, `water_pressure`, `water_level`,
  `alarm_count`, `warning_count`, `normal_count`, `not_evaluated_count`,
  `no_data_count`, `total`).
