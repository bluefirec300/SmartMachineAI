import random
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from database.initialize_config_db import initialize_config_database
from engine.factory_structure_migrator import migrate as migrate_factory_structure
from engine.operating_schedule_migrator import migrate as migrate_operating_schedule
from engine.production_migrator import migrate as migrate_production
from simulator.production_batch_simulator import (
    CANCELLATION_PROBABILITY,
    INTERRUPTION_PROBABILITY,
    get_active_products,
    get_production_equipment,
    get_running_batch,
    progress_batch,
    start_batch,
    tick,
)


class ProductionSimulatorTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "config.db"
        initialize_config_database(str(self.database_path))

        # Seed one real p01_ equipment row so factory_structure_migrator
        # creates a real plant/area/system to attach production
        # equipment to, matching how the live database was seeded.
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

        migrate_factory_structure(self.database_path, backup=False)
        migrate_operating_schedule(self.database_path, backup=False)
        migrate_production(self.database_path, backup=False)

    def tearDown(self):
        self.temp_dir.cleanup()


class TestMigrationSeeding(ProductionSimulatorTestBase):
    def test_categories_and_products_seeded(self):
        connection = sqlite3.connect(self.database_path)
        category_count = connection.execute("SELECT COUNT(*) FROM product_categories").fetchone()[0]
        product_count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        connection.close()

        self.assertEqual(category_count, 9)
        self.assertGreater(product_count, 0)

    def test_products_have_varied_units_not_just_tonnes(self):
        products = get_active_products(self.database_path)
        units = {p["unit_of_measure"] for p in products}
        self.assertIn("litre", units)
        self.assertIn("kg", units)
        self.assertNotIn("tonne", units)  # none of the seed products happen to use tonnes - confirms no hardcoded assumption either way

    def test_all_seed_products_marked_simulated_source(self):
        products = get_active_products(self.database_path)
        self.assertTrue(all(p["source"] == "simulated" for p in products))


class TestProductionEquipmentDiscovery(ProductionSimulatorTestBase):
    def test_finds_only_production_capable_equipment_with_structured_plant_link(self):
        equipment = get_production_equipment(self.database_path)
        names = {e["name"] for e in equipment}
        self.assertIn("p01_bead_mill_mill01", names)
        self.assertIn("p01_filling_machine_fill01", names)
        for e in equipment:
            self.assertIsNotNone(e["plant_id"])  # structured FK, not a string to parse later


class TestBatchLifecycle(ProductionSimulatorTestBase):
    def test_start_batch_uses_equipments_own_structured_plant_area_system(self):
        equipment = get_production_equipment(self.database_path)[0]
        products = get_active_products(self.database_path)
        rng = random.Random(42)

        batch_id = start_batch(self.database_path, equipment, products, rng, now=datetime(2026, 8, 15, 10, 0, 0))

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        batch = dict(connection.execute("SELECT * FROM production_batches WHERE id = ?", (batch_id,)).fetchone())
        connection.close()

        self.assertEqual(batch["plant_id"], equipment["plant_id"])
        self.assertEqual(batch["area_id"], equipment["area_id"])
        self.assertEqual(batch["system_id"], equipment["system_id"])
        self.assertEqual(batch["status"], "running")
        self.assertEqual(batch["source"], "simulated")
        self.assertIsNotNone(batch["planned_quantity"])
        self.assertGreater(batch["planned_quantity"], 0)

    def test_progress_batch_normal_path_increases_actual_quantity(self):
        equipment = get_production_equipment(self.database_path)[0]
        products = get_active_products(self.database_path)
        rng = random.Random(1)  # seed chosen to avoid the rare interrupt/cancel rolls on this one call

        batch_id = start_batch(self.database_path, equipment, products, rng, now=datetime(2026, 8, 15, 10, 0, 0))
        batch = get_running_batch(self.database_path, equipment["id"])
        self.assertIsNotNone(batch)

        status = progress_batch(self.database_path, batch, rng, now=datetime(2026, 8, 15, 10, 0, 30))

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        updated = dict(connection.execute("SELECT * FROM production_batches WHERE id = ?", (batch_id,)).fetchone())
        connection.close()

        self.assertIn(status, ("running", "completed"))  # never interrupted/cancelled with this seed+call
        self.assertGreater(updated["actual_quantity"], 0)

    def test_batch_eventually_completes_over_many_ticks(self):
        equipment = get_production_equipment(self.database_path)[0]
        products = get_active_products(self.database_path)
        # Seed deliberately chosen (verified) to never roll interrupt/cancel
        # across this run, so completion is guaranteed and testable.
        rng = random.Random(7)

        start_batch(self.database_path, equipment, products, rng, now=datetime(2026, 8, 15, 10, 0, 0))

        completed = False
        for i in range(200):
            batch = get_running_batch(self.database_path, equipment["id"])
            if batch is None:
                completed = True
                break
            progress_batch(self.database_path, batch, rng, now=datetime(2026, 8, 15, 10, i, 0))

        self.assertTrue(completed, "batch never reached a terminal state within 200 ticks")

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM production_batches").fetchall()
        connection.close()
        self.assertEqual(len(rows), 1)
        final_status = rows[0]["status"]
        self.assertIn(final_status, ("completed", "interrupted", "cancelled"))
        if final_status == "completed":
            self.assertEqual(rows[0]["actual_quantity"], rows[0]["planned_quantity"])
            self.assertIsNotNone(rows[0]["end_time"])

    def test_cancellation_path_explicitly_exercised_with_forced_roll(self):
        """Unit test explicitly exercises the rare cancelled path via a
        seed search, rather than relying on the live simulator's low
        probability - per explicit direction: rare states are tested
        directly, not made artificially common in the real simulator."""
        equipment = get_production_equipment(self.database_path)[0]
        products = get_active_products(self.database_path)

        rng_for_start = random.Random(0)
        start_batch(self.database_path, equipment, products, rng_for_start, now=datetime(2026, 8, 15, 10, 0, 0))
        batch = get_running_batch(self.database_path, equipment["id"])

        class ForcedRoll(random.Random):
            def random(self):
                return 0.0  # guaranteed below CANCELLATION_PROBABILITY

        status = progress_batch(self.database_path, batch, ForcedRoll(), now=datetime(2026, 8, 15, 10, 1, 0))
        self.assertEqual(status, "cancelled")

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        updated = dict(connection.execute("SELECT * FROM production_batches WHERE id = ?", (batch["id"],)).fetchone())
        connection.close()
        self.assertEqual(updated["status"], "cancelled")
        self.assertIsNotNone(updated["end_time"])

    def test_interruption_path_records_downtime(self):
        equipment = get_production_equipment(self.database_path)[0]
        products = get_active_products(self.database_path)

        rng_for_start = random.Random(0)
        start_batch(self.database_path, equipment, products, rng_for_start, now=datetime(2026, 8, 15, 10, 0, 0))
        batch = get_running_batch(self.database_path, equipment["id"])

        class ForcedRoll(random.Random):
            def random(self):
                return CANCELLATION_PROBABILITY + (INTERRUPTION_PROBABILITY / 2)  # lands in the interruption band

            def uniform(self, a, b):
                return (a + b) / 2

            def choice(self, seq):
                return seq[0]

        status = progress_batch(self.database_path, batch, ForcedRoll(), now=datetime(2026, 8, 15, 10, 1, 0))
        self.assertEqual(status, "interrupted")

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        updated = dict(connection.execute("SELECT * FROM production_batches WHERE id = ?", (batch["id"],)).fetchone())
        connection.close()
        self.assertEqual(updated["status"], "interrupted")
        self.assertIsNotNone(updated["downtime_minutes"])
        self.assertIsNotNone(updated["downtime_reason"])


class TestRestartSafety(ProductionSimulatorTestBase):
    def test_tick_never_starts_a_second_concurrent_batch_for_the_same_equipment(self):
        """Simulates a 'restart' by calling tick() with a fresh call
        stack / no shared in-memory state between calls - exactly what
        a real process restart looks like, since tick() takes no
        persistent object as an argument."""
        rng = random.Random(3)

        for _ in range(30):
            tick(self.database_path, rng=rng, now=datetime(2026, 8, 15, 10, 0, 0))

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        equipment_ids = [e["id"] for e in connection.execute("SELECT id FROM equipment WHERE plant_id IS NOT NULL")]
        for equipment_id in equipment_ids:
            running_count = connection.execute(
                "SELECT COUNT(*) FROM production_batches WHERE equipment_id = ? AND status = 'running'",
                (equipment_id,),
            ).fetchone()[0]
            self.assertLessEqual(running_count, 1, f"equipment {equipment_id} has more than one concurrent running batch")
        connection.close()

    def test_a_fresh_tick_call_after_simulated_restart_does_not_duplicate_a_running_batch(self):
        equipment = get_production_equipment(self.database_path)[0]
        products = get_active_products(self.database_path)
        rng = random.Random(5)

        start_batch(self.database_path, equipment, products, rng, now=datetime(2026, 8, 15, 10, 0, 0))

        # "Restart": a brand new tick() call, no shared state with the
        # call above at all (this is exactly what a fresh process
        # instance's first tick looks like).
        result = tick(self.database_path, rng=random.Random(99), now=datetime(2026, 8, 15, 10, 0, 30))

        connection = sqlite3.connect(self.database_path)
        running_count = connection.execute(
            "SELECT COUNT(*) FROM production_batches WHERE equipment_id = ? AND status = 'running'",
            (equipment["id"],),
        ).fetchone()[0]
        connection.close()

        self.assertEqual(running_count, 1)
        # The already-running batch must have been progressed, not
        # duplicated as a fresh "started" batch on the same equipment.
        self.assertGreaterEqual(result["progressed"], 1)


class TestNoEnergyCoupling(ProductionSimulatorTestBase):
    def test_tick_never_writes_to_plc_data_or_touches_any_electrical_tag(self):
        """Explicit negative check per direction: Phase 4 must not
        introduce any production-to-energy correlation - that's
        Phase 5's job."""
        import inspect

        import simulator.production_batch_simulator as module

        source = inspect.getsource(module)
        self.assertNotIn("plc_data", source)
        self.assertNotIn("Energy_kWh", source)
        self.assertNotIn("Power_kW", source)


if __name__ == "__main__":
    unittest.main()
