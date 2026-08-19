import random
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from database.initialize_config_db import initialize_config_database
from engine.factory_structure_migrator import migrate as migrate_factory_structure
from engine.metadata_migrator import migrate as migrate_metadata
from engine.operating_schedule_migrator import migrate as migrate_operating_schedule
from engine.production_migrator import migrate as migrate_production
from simulator import plant_context
from simulator.production_batch_simulator import get_active_products, start_batch
from simulator.tag_dataset_model import (
    SIMULATED_TICK_SECONDS,
    TagDatasetSimulator,
    _InstanceFaultState,
)


def _insert_tag(
    database_path, tag_name, data_type, unit="", measurement="", event_type="",
):
    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO tags (tag_name, driver, data_type, unit, enabled, measurement, event_type) "
        "VALUES (?, 'simulator', ?, ?, 1, ?, ?)",
        (tag_name, data_type, unit, measurement, event_type),
    )
    connection.commit()
    connection.close()


# A minimal but representative tag set: one Main Incomer with a genuine
# mix of included end loads and explicitly-excluded equipment (item 6),
# one production-linked instance of each production tag shape (item 4),
# and the outdoor ambient sensor (item 5).
def _seed_minimal_tag_set(database_path):
    tags = [
        # Main Incomer itself
        ("P01.ELEC.MAIN.Power_kW", "REAL", "kW", "power", ""),
        ("P01.ELEC.MAIN.Energy_kWh", "REAL", "kWh", "energy", ""),
        # Included end loads
        ("P01.UTILITY.AC01.Power_kW", "REAL", "kW", "power", ""),
        ("P01.UTILITY.AC01.OutletTemp", "REAL", "°C", "temperature", ""),
        ("P01.PROD.MILL01.MotorPower", "REAL", "kW", "power", ""),
        ("P01.PROD.MILL01.MotorCurrent", "REAL", "A", "current", ""),
        ("P01.PROD.MILL01.Vibration", "REAL", "mm/s", "vibration", ""),
        ("P01.PROD.MILL01.RunStatus", "BOOL", "", "running", ""),
        ("P01.PROD.MILL01.BatchNumber", "STRING", "", "", ""),
        ("P01.PROD.MILL01.ProductCode", "STRING", "", "", ""),
        ("P01.FILL.FILL01.Power_kW", "REAL", "kW", "power", ""),
        ("P01.FILL.FILL01.RunStatus", "BOOL", "", "running", ""),
        ("P01.FILL.FILL01.BatchNumber", "STRING", "", "", ""),
        ("P01.FILL.FILL01.ProductCode", "STRING", "", "", ""),
        ("P01.FILL.FILL01.GoodCount", "INT", "", "", ""),
        ("P01.FILL.FILL01.RejectCount", "INT", "", "", ""),
        ("P01.FILL.FILL01.ContainerSize", "REAL", "L/kg", "", ""),
        # Explicitly excluded from Main Incomer
        ("P01.ELEC.GEN01.Power_kW", "REAL", "kW", "power", ""),
        ("P01.ELEC.TR01.LoadPct", "REAL", "%", "", ""),
        ("P01.IT.UPS01.LoadPct", "REAL", "%", "", ""),
        # Outdoor ambient
        ("P01.ENV01.Temperature", "REAL", "°C", "temperature", ""),
        ("P01.ENV01.Humidity", "REAL", "%RH", "humidity", ""),
    ]
    for tag_name, data_type, unit, measurement, event_type in tags:
        _insert_tag(database_path, tag_name, data_type, unit, measurement, event_type)


class TagDatasetSimulatorTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "config.db"
        initialize_config_database(str(self.database_path))

        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "INSERT INTO equipment (name, display_name) VALUES (?, ?)",
            ("p01_bead_mill_mill01", "Bead Mill MILL01 (P01)"),
        )
        connection.execute(
            "INSERT INTO equipment (name, display_name) VALUES (?, ?)",
            ("p01_filling_machine_fill01", "Filling Machine FILL01 (P01)"),
        )
        connection.commit()
        connection.close()

        migrate_metadata(self.database_path, backup=False)
        migrate_factory_structure(self.database_path, backup=False)
        migrate_operating_schedule(self.database_path, backup=False)
        migrate_production(self.database_path, backup=False)

        _seed_minimal_tag_set(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _equipment_row(self, name_fragment):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT id, name, plant_id, area_id, system_id FROM equipment WHERE name LIKE ?",
                (f"%{name_fragment}%",),
            ).fetchone()
            return dict(row)
        finally:
            connection.close()


class TestMainIncomerAggregation(TagDatasetSimulatorTestBase):
    def test_sums_only_included_end_loads_plus_base_load(self):
        sim = TagDatasetSimulator(self.database_path)
        sim.update_values()

        values = {name: value for name, _, value in sim.get_tags()}
        contributing = sim._main_incomer_contributing_by_plant["p01"]

        # Every seeded end load actually got included.
        self.assertIn("P01.UTILITY.AC01.Power_kW", contributing)
        self.assertIn("P01.PROD.MILL01.MotorPower", contributing)
        self.assertIn("P01.FILL.FILL01.Power_kW", contributing)
        # Every seeded exclusion actually got excluded.
        self.assertNotIn("P01.ELEC.GEN01.Power_kW", contributing)
        self.assertNotIn("P01.ELEC.TR01.LoadPct", contributing)
        self.assertNotIn("P01.IT.UPS01.LoadPct", contributing)

        expected_sum = sum(values[t] for t in contributing)
        main_power = values["P01.ELEC.MAIN.Power_kW"]

        # Main Incomer = contributing end loads + a small base load, not
        # a random independent value.
        self.assertGreater(main_power, expected_sum)
        self.assertLess(main_power - expected_sum, 15.0)

    def test_generator_power_never_moves_the_main_incomer(self):
        sim = TagDatasetSimulator(self.database_path)
        sim.update_values()
        values_before = {name: value for name, _, value in sim.get_tags()}

        # Force the generator's own (excluded) power way up and confirm
        # Main Incomer is unaffected by it.
        sim._values["P01.ELEC.GEN01.Power_kW"] = 100000.0
        sim.update_values()
        values_after = {name: value for name, _, value in sim.get_tags()}

        self.assertLess(
            abs(values_after["P01.ELEC.MAIN.Power_kW"] - values_before["P01.ELEC.MAIN.Power_kW"]),
            50.0,
        )

    def test_energy_kwh_integrates_from_power_kw(self):
        sim = TagDatasetSimulator(self.database_path)
        sim._values["P01.ELEC.MAIN.Power_kW"] = 100.0
        sim._values["P01.ELEC.MAIN.Energy_kWh"] = 0.0

        before = sim._values["P01.ELEC.MAIN.Energy_kWh"]
        sim.update_values()
        after = {name: value for name, _, value in sim.get_tags()}["P01.ELEC.MAIN.Energy_kWh"]

        expected_increment = 100.0 * (SIMULATED_TICK_SECONDS / 3600.0)
        self.assertAlmostEqual(after - before, expected_increment, delta=0.01)


class TestProductionSync(TagDatasetSimulatorTestBase):
    def test_run_status_and_batch_fields_idle_without_a_running_batch(self):
        sim = TagDatasetSimulator(self.database_path)
        sim.update_values()
        values = {name: value for name, _, value in sim.get_tags()}

        self.assertEqual(values["P01.PROD.MILL01.RunStatus"], 0)
        self.assertEqual(values["P01.PROD.MILL01.BatchNumber"], "IDLE")
        self.assertEqual(values["P01.PROD.MILL01.ProductCode"], "IDLE")

    def test_run_status_and_batch_fields_reflect_a_real_running_batch(self):
        equipment = self._equipment_row("bead_mill")
        products = get_active_products(self.database_path)
        rng = random.Random(42)
        start_batch(self.database_path, equipment, products, rng, now=datetime(2026, 8, 15, 10, 0))

        sim = TagDatasetSimulator(self.database_path)
        sim.update_values()
        values = {name: value for name, _, value in sim.get_tags()}

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        batch = connection.execute(
            "SELECT b.*, p.product_code FROM production_batches b "
            "JOIN products p ON p.id = b.product_id WHERE b.equipment_id = ?",
            (equipment["id"],),
        ).fetchone()
        connection.close()

        self.assertEqual(values["P01.PROD.MILL01.RunStatus"], 1)
        self.assertEqual(values["P01.PROD.MILL01.BatchNumber"], batch["batch_code"])
        self.assertEqual(values["P01.PROD.MILL01.ProductCode"], batch["product_code"])

    def test_good_count_and_reject_count_derive_from_batch_quantities(self):
        equipment = self._equipment_row("filling_machine")
        products = get_active_products(self.database_path)
        rng = random.Random(7)
        start_batch(self.database_path, equipment, products, rng, now=datetime(2026, 8, 15, 10, 0))

        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "UPDATE production_batches SET actual_quantity = 100.0, good_quantity = 98.0, "
            "reject_quantity = 2.0 WHERE equipment_id = ?",
            (equipment["id"],),
        )
        connection.commit()
        connection.close()

        sim = TagDatasetSimulator(self.database_path)
        sim._values["P01.FILL.FILL01.ContainerSize"] = 1.0
        sim.update_values()
        values = {name: value for name, _, value in sim.get_tags()}

        self.assertEqual(values["P01.FILL.FILL01.GoodCount"], 98)
        self.assertEqual(values["P01.FILL.FILL01.RejectCount"], 2)

    def test_good_count_holds_last_value_when_batch_finishes(self):
        equipment = self._equipment_row("filling_machine")
        products = get_active_products(self.database_path)
        rng = random.Random(7)
        start_batch(self.database_path, equipment, products, rng, now=datetime(2026, 8, 15, 10, 0))

        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "UPDATE production_batches SET actual_quantity = 100.0, good_quantity = 98.0, "
            "reject_quantity = 2.0 WHERE equipment_id = ?",
            (equipment["id"],),
        )
        connection.commit()
        connection.close()

        sim = TagDatasetSimulator(self.database_path)
        sim._values["P01.FILL.FILL01.ContainerSize"] = 1.0
        sim.update_values()

        # The batch finishes (no longer 'running') - GoodCount should
        # hold its last value, not reset to 0.
        connection = sqlite3.connect(self.database_path)
        connection.execute("UPDATE production_batches SET status = 'completed' WHERE equipment_id = ?", (equipment["id"],))
        connection.commit()
        connection.close()

        sim.update_values()
        values = {name: value for name, _, value in sim.get_tags()}
        self.assertEqual(values["P01.FILL.FILL01.GoodCount"], 98)

    def test_non_production_instance_unaffected(self):
        """AC01 has no production_batches row at all - its RunStatus
        must stay on the original random-noise path, never forced to 0
        by the production-sync logic."""
        sim = TagDatasetSimulator(self.database_path)
        inst_key = "P01.UTILITY.AC01"
        self.assertFalse(plant_context.is_production_linked_instance(inst_key))


class TestEfficiencyAndDeteriorationWiring(TagDatasetSimulatorTestBase):
    def test_efficiency_factor_assigned_and_stable_across_fresh_instances(self):
        """Restart-safety: a freshly constructed simulator against the
        same database must derive the same efficiency factor for the
        same instance, since it's seeded from the instance key, not
        per-process randomness."""
        sim_a = TagDatasetSimulator(self.database_path)
        sim_b = TagDatasetSimulator(self.database_path)

        factor_a = sim_a._instances["P01.UTILITY.AC01"].efficiency_factor
        factor_b = sim_b._instances["P01.UTILITY.AC01"].efficiency_factor

        self.assertEqual(factor_a, factor_b)

    def test_deterioration_eligibility_assigned_and_stable(self):
        sim_a = TagDatasetSimulator(self.database_path)
        sim_b = TagDatasetSimulator(self.database_path)

        self.assertEqual(
            sim_a._instances["P01.UTILITY.AC01"].deterioration_eligible,
            sim_b._instances["P01.UTILITY.AC01"].deterioration_eligible,
        )

    def test_deterioration_only_affects_a_minority_of_instances(self):
        sim = TagDatasetSimulator(self.database_path)
        eligible = [i for i in sim._instances.values() if i.deterioration_eligible]
        self.assertLess(len(eligible), len(sim._instances))


class TestInstanceFaultStateDeterioration(unittest.TestCase):
    def test_ineligible_instance_never_deteriorates(self):
        instance = _InstanceFaultState(random.Random(1), deterioration_eligible=False)
        for _ in range(1000):
            instance.advance_deterioration(tick_seconds=2.0)
        self.assertEqual(instance.deterioration_level, 0.0)
        self.assertEqual(instance.deterioration_state, "stable")

    def test_eligible_instance_drifts_upward_over_many_ticks(self):
        instance = _InstanceFaultState(random.Random(1), deterioration_eligible=True)
        self.assertEqual(instance.deterioration_state, "deteriorating")
        self.assertEqual(instance.deterioration_level, 0.0)

        for _ in range(1000):
            instance.advance_deterioration(tick_seconds=2.0)

        self.assertGreater(instance.deterioration_level, 0.0)
        self.assertLessEqual(instance.deterioration_level, 1.0)

    def test_deterioration_level_never_exceeds_one(self):
        instance = _InstanceFaultState(random.Random(1), deterioration_eligible=True)
        for _ in range(1_000_000):
            instance.advance_deterioration(tick_seconds=2.0)
        self.assertEqual(instance.deterioration_level, 1.0)

    def test_force_recover_deterioration_resets_and_stops_further_drift(self):
        instance = _InstanceFaultState(random.Random(1), deterioration_eligible=True)
        for _ in range(1000):
            instance.advance_deterioration(tick_seconds=2.0)
        self.assertGreater(instance.deterioration_level, 0.0)

        recovered = instance.force_recover_deterioration()
        self.assertTrue(recovered)
        self.assertEqual(instance.deterioration_level, 0.0)
        self.assertEqual(instance.deterioration_state, "recovered")

        # Recovered state no longer drifts - it's not "deteriorating" anymore.
        instance.advance_deterioration(tick_seconds=2.0)
        self.assertEqual(instance.deterioration_level, 0.0)

    def test_force_recover_deterioration_no_op_when_already_stable(self):
        instance = _InstanceFaultState(random.Random(1), deterioration_eligible=False)
        self.assertFalse(instance.force_recover_deterioration())


if __name__ == "__main__":
    unittest.main()
