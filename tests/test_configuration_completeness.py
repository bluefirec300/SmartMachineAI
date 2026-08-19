import tempfile
import unittest
from pathlib import Path

from database.initialize_config_db import initialize_config_database
from engine.configuration_completeness import (
    calculate_equipment_completeness,
    calculate_factory_completeness,
    calculate_overall_completeness,
    calculate_plant_completeness,
    equipment_field_weights,
)
from engine.factory_structure_migrator import migrate as migrate_factory_structure
from engine.operating_schedule_migrator import migrate as migrate_operating_schedule


class TestFactoryAndPlantCompleteness(unittest.TestCase):
    def test_fully_filled_factory_scores_100(self):
        row = {
            "name": "MMG Smart Factory", "company": "MMG", "country": "Malaysia",
            "currency": "MYR", "timezone": "Asia/Kuala_Lumpur", "factory_type": "Automotive Paint",
            "floor_area_m2": 12000,
        }
        result = calculate_factory_completeness(row)
        self.assertEqual(result["percent"], 100)
        self.assertEqual(result["missing"], [])

    def test_empty_factory_scores_0_and_lists_every_field(self):
        row = {"name": None, "company": None, "country": None, "currency": None,
               "timezone": None, "factory_type": None, "floor_area_m2": None}
        result = calculate_factory_completeness(row)
        self.assertEqual(result["percent"], 0)
        self.assertEqual(len(result["missing"]), 7)

    def test_missing_fields_sorted_by_weight_descending(self):
        row = {"name": None, "company": None, "country": None, "currency": None,
               "timezone": None, "factory_type": None, "floor_area_m2": None}
        result = calculate_factory_completeness(row)
        weights = [w for _, w in result["missing"]]
        self.assertEqual(weights, sorted(weights, reverse=True))
        # currency/timezone (weight 2) must appear before the weight-1 fields
        self.assertIn("currency", [f for f, w in result["missing"][:2]])
        self.assertIn("timezone", [f for f, w in result["missing"][:2]])

    def test_empty_string_counts_as_missing_not_just_none(self):
        row = {"name": "", "company": None, "country": None, "currency": None,
               "timezone": None, "factory_type": None, "floor_area_m2": None}
        result = calculate_factory_completeness(row)
        self.assertIn("name", [f for f, _ in result["missing"]])

    def test_plant_completeness_ignores_code_and_active(self):
        # code/active are never "missing" (code always set, active
        # defaults to 1) - a plant with only those two populated must
        # still score 0, not partially complete.
        row = {"code": "p01", "active": 1, "name": None, "description": None,
               "floor_area_m2": None, "production_capacity": None}
        result = calculate_plant_completeness(row)
        self.assertEqual(result["percent"], 0)


class TestEquipmentCompleteness(unittest.TestCase):
    def _blank_equipment(self, equipment_type=None):
        return {
            "equipment_type": equipment_type, "brand": None, "model": None,
            "serial_number": None, "installation_date": None, "commission_date": None,
            "criticality": None, "rated_power": None, "rated_voltage": None,
            "rated_current": None, "rated_flow": None, "rated_pressure": None,
            "rated_capacity": None, "replacement_cost": None,
            "expected_life_years": None, "normal_operating_hours": None,
        }

    def test_air_compressor_prioritizes_power_pressure_flow(self):
        weights = equipment_field_weights("Air Compressor")
        self.assertEqual(weights["rated_power"], 4)
        self.assertEqual(weights["rated_pressure"], 4)
        self.assertEqual(weights["rated_flow"], 4)
        # a field this type doesn't override still uses the default
        self.assertEqual(weights["rated_voltage"], 1)

    def test_chiller_prioritizes_power_and_capacity(self):
        weights = equipment_field_weights("Chiller")
        self.assertEqual(weights["rated_power"], 4)
        self.assertEqual(weights["rated_capacity"], 4)

    def test_weather_node_zeroes_out_irrelevant_ratings(self):
        weights = equipment_field_weights("Weather Node")
        for field in ("rated_power", "rated_voltage", "rated_current", "rated_flow", "rated_pressure", "rated_capacity"):
            self.assertEqual(weights[field], 0)

    def test_zero_weight_fields_never_appear_in_missing_list(self):
        row = self._blank_equipment("Weather Node")
        result = calculate_equipment_completeness(row)
        missing_fields = {f for f, _ in result["missing"]}
        self.assertNotIn("rated_power", missing_fields)
        self.assertNotIn("rated_capacity", missing_fields)

    def test_unknown_equipment_type_falls_back_to_defaults(self):
        weights_known = equipment_field_weights("Totally New Equipment Type Not Yet Mapped")
        weights_default = dict(equipment_field_weights(None))
        self.assertEqual(weights_known, weights_default)

    def test_same_missing_field_scores_higher_weight_for_the_type_that_prioritizes_it(self):
        row_compressor = self._blank_equipment("Air Compressor")
        row_compressor["brand"] = "Atlas Copco"
        row_compressor["model"] = "GA30+"

        result = calculate_equipment_completeness(row_compressor)
        pressure_weight = dict(result["missing"])["rated_pressure"]
        self.assertEqual(pressure_weight, 4)

    def test_fully_filled_equipment_scores_100(self):
        row = self._blank_equipment("Chiller")
        for field in row:
            if row[field] is None:
                row[field] = "x" if field in ("equipment_type", "brand", "model", "serial_number",
                                               "installation_date", "commission_date", "criticality") else 1
        result = calculate_equipment_completeness(row)
        self.assertEqual(result["percent"], 100)
        self.assertEqual(result["missing"], [])


class TestOverallCompleteness(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "config.db"
        initialize_config_database(str(self.database_path))
        migrate_factory_structure(self.database_path, backup=False)
        migrate_operating_schedule(self.database_path, backup=False)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_overall_completeness_runs_against_a_freshly_migrated_empty_database(self):
        # A fresh config.db (no equipment, no factory row seeded since
        # factory_structure_migrator only seeds a factory row when at
        # least one p0N_-prefixed equipment row exists) must not crash -
        # zero equipment/plants is a legitimate starting state.
        result = calculate_overall_completeness(self.database_path)
        self.assertIn("overall_percent", result)
        self.assertIsInstance(result["top_missing"], list)

    def test_overall_completeness_against_real_seeded_structure(self):
        import sqlite3

        connection = sqlite3.connect(self.database_path)
        now = "2026-08-15 00:00:00"
        connection.execute(
            "INSERT INTO equipment (name, display_name) VALUES (?, ?)",
            ("p01_air_compressor_ac01", "Air Compressor AC01 (P01)"),
        )
        connection.commit()
        connection.close()

        from engine.factory_structure_migrator import migrate as remigrate
        remigrate(self.database_path, backup=False)

        result = calculate_overall_completeness(self.database_path)
        self.assertIsNotNone(result["factory"])
        self.assertEqual(len(result["plants"]), 1)
        self.assertEqual(len(result["equipment"]), 1)
        # equipment_type is backfilled by the migrator itself, so the
        # single equipment row isn't at 0% - but every rating/serial/
        # date/criticality field is still genuinely unfilled.
        self.assertLess(result["equipment"][0]["percent"], 15)
        missing_fields = {f for f, _ in result["equipment"][0]["missing"]}
        self.assertIn("rated_power", missing_fields)
        self.assertIn("rated_pressure", missing_fields)
        self.assertTrue(len(result["top_missing"]) > 0)
        # Every top_missing entry must carry a positive weight (zero-
        # weight fields must never leak into the ranked list).
        for item in result["top_missing"]:
            self.assertGreater(item["weight"], 0)


if __name__ == "__main__":
    unittest.main()
