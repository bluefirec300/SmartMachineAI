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
            "today_water_m3", "water_flow",
            "alarm_count", "warning_count", "normal_count",
            "not_evaluated_count", "no_data_count", "total",
        }
        self.assertEqual(set(board.keys()), expected_keys)
        self.assertEqual(board["total"], 0)


if __name__ == "__main__":
    unittest.main()
