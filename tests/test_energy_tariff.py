import tempfile
import unittest
from pathlib import Path

from database.initialize_config_db import initialize_config_database
from engine.energy_tariff import (
    SIMULATED_TARIFF_NOTICE,
    TARIFF_PROVENANCE_CONFIGURED,
    TARIFF_PROVENANCE_SIMULATION,
    calculate_energy_cost,
    create_tariff,
    get_current_tariff,
    get_tariff_for_date,
    get_tariff_history,
    tariff_provenance_label,
)
from engine.energy_tariff_migrator import migrate as migrate_energy_tariff


class TestEnergyTariff(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "config.db"
        initialize_config_database(str(self.database_path))
        migrate_energy_tariff(self.database_path, backup=False)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_no_tariff_configured_returns_none_for_a_direct_lookup(self):
        result = get_tariff_for_date(self.database_path, plant_id=None, on_date="2026-08-15")
        self.assertIsNone(result)

    def test_get_current_tariff_auto_generates_a_marked_simulated_tariff(self):
        tariff = get_current_tariff(self.database_path, plant_id=None)
        self.assertEqual(tariff["is_simulated"], 1)
        self.assertEqual(tariff["energy_rate"], 0.50)
        self.assertEqual(tariff["currency"], "MYR")

    def test_auto_generated_simulated_tariff_is_persisted_not_recreated_every_call(self):
        first = get_current_tariff(self.database_path, plant_id=None)
        second = get_current_tariff(self.database_path, plant_id=None)
        self.assertEqual(first["id"], second["id"])

    def test_real_tariff_creation_marks_not_simulated(self):
        create_tariff(
            self.database_path, plant_id=None, effective_date="2026-01-01",
            username="admin", mode="simple", currency="MYR", energy_rate=0.45,
        )
        tariff = get_current_tariff(self.database_path, plant_id=None)
        self.assertEqual(tariff["is_simulated"], 0)
        self.assertEqual(tariff["energy_rate"], 0.45)

    def test_creating_a_new_tariff_closes_out_the_previous_one_not_deletes_it(self):
        first_id = create_tariff(
            self.database_path, plant_id=None, effective_date="2026-01-01",
            username="admin", energy_rate=0.45,
        )
        create_tariff(
            self.database_path, plant_id=None, effective_date="2026-06-01",
            username="admin", energy_rate=0.55,
        )

        history = get_tariff_history(self.database_path, plant_id=None)
        self.assertEqual(len(history), 2)

        first_row = next(r for r in history if r["id"] == first_id)
        self.assertEqual(first_row["expiry_date"], "2026-06-01")
        self.assertEqual(first_row["energy_rate"], 0.45)  # untouched, not overwritten

    def test_historical_lookup_uses_the_tariff_valid_at_that_time_not_the_current_one(self):
        create_tariff(
            self.database_path, plant_id=None, effective_date="2026-01-01",
            username="admin", energy_rate=0.45,
        )
        create_tariff(
            self.database_path, plant_id=None, effective_date="2026-06-01",
            username="admin", energy_rate=0.55,
        )

        march_tariff = get_tariff_for_date(self.database_path, plant_id=None, on_date="2026-03-15")
        july_tariff = get_tariff_for_date(self.database_path, plant_id=None, on_date="2026-07-15")

        self.assertEqual(march_tariff["energy_rate"], 0.45)
        self.assertEqual(july_tariff["energy_rate"], 0.55)

    def test_boundary_date_uses_the_new_tariff_not_the_old_one(self):
        create_tariff(
            self.database_path, plant_id=None, effective_date="2026-01-01",
            username="admin", energy_rate=0.45,
        )
        create_tariff(
            self.database_path, plant_id=None, effective_date="2026-06-01",
            username="admin", energy_rate=0.55,
        )
        boundary_tariff = get_tariff_for_date(self.database_path, plant_id=None, on_date="2026-06-01")
        self.assertEqual(boundary_tariff["energy_rate"], 0.55)

    def test_plant_scoped_tariffs_are_independent_of_factory_wide_and_each_other(self):
        create_tariff(self.database_path, plant_id=None, effective_date="2026-01-01", username="admin", energy_rate=0.50)
        create_tariff(self.database_path, plant_id=1, effective_date="2026-01-01", username="admin", energy_rate=0.60)

        factory_wide = get_tariff_for_date(self.database_path, plant_id=None, on_date="2026-08-15")
        plant_specific = get_tariff_for_date(self.database_path, plant_id=1, on_date="2026-08-15")

        self.assertEqual(factory_wide["energy_rate"], 0.50)
        self.assertEqual(plant_specific["energy_rate"], 0.60)

    def test_advanced_mode_fields_are_stored_even_though_cost_calc_uses_flat_rate(self):
        create_tariff(
            self.database_path, plant_id=None, effective_date="2026-01-01", username="admin",
            mode="advanced", energy_rate=0.50, peak_rate=0.70, off_peak_rate=0.35,
            peak_start="08:00", peak_end="22:00", maximum_demand_charge=35.0,
            contract_maximum_demand=500.0, fixed_monthly_charge=100.0,
            surcharge_percent=3.7, tax_percent=6.0, billing_cycle="calendar_month",
        )
        tariff = get_current_tariff(self.database_path, plant_id=None)
        self.assertEqual(tariff["mode"], "advanced")
        self.assertEqual(tariff["peak_rate"], 0.70)
        self.assertEqual(tariff["maximum_demand_charge"], 35.0)
        self.assertEqual(tariff["billing_cycle"], "calendar_month")


class TestCalculateEnergyCost(unittest.TestCase):
    def test_flat_rate_multiplication(self):
        cost = calculate_energy_cost(100.0, {"energy_rate": 0.50})
        self.assertEqual(cost, 50.0)

    def test_none_kwh_returns_none(self):
        self.assertIsNone(calculate_energy_cost(None, {"energy_rate": 0.50}))

    def test_missing_rate_returns_none_not_zero(self):
        self.assertIsNone(calculate_energy_cost(100.0, {"energy_rate": None}))

    def test_rounds_to_two_decimal_places(self):
        cost = calculate_energy_cost(33.333, {"energy_rate": 0.501})
        self.assertEqual(cost, round(33.333 * 0.501, 2))


class TestSimulatedTariffNotice(unittest.TestCase):
    def test_notice_text_matches_roadmap_exactly(self):
        self.assertIn("SIMULATION TARIFF", SIMULATED_TARIFF_NOTICE)
        self.assertIn("Configure actual utility tariff", SIMULATED_TARIFF_NOTICE)


class TestTariffProvenanceLabel(unittest.TestCase):
    """Phase 18.1a.1 - corrects the previously-hardcoded "SIMULATION"
    tariff_provenance bug. The label must come from the tariff's own
    is_simulated flag, never from currency or any other proxy."""

    def test_none_tariff_returns_none(self):
        self.assertIsNone(tariff_provenance_label(None))

    def test_simulated_tariff_returns_simulation_label(self):
        self.assertEqual(tariff_provenance_label({"is_simulated": 1}), TARIFF_PROVENANCE_SIMULATION)

    def test_real_tariff_returns_configured_label(self):
        self.assertEqual(tariff_provenance_label({"is_simulated": 0}), TARIFF_PROVENANCE_CONFIGURED)

    def test_myr_currency_does_not_imply_simulation(self):
        # A real, non-simulated MYR tariff (the current demo's actual
        # configuration) must be labeled CONFIGURED, not SIMULATION -
        # currency must never be used as a provenance proxy.
        self.assertEqual(tariff_provenance_label({"is_simulated": 0, "currency": "MYR"}), TARIFF_PROVENANCE_CONFIGURED)

    def test_non_myr_currency_does_not_imply_real(self):
        # Conversely, an auto-generated simulation tariff in a
        # different currency must still be labeled SIMULATION.
        self.assertEqual(tariff_provenance_label({"is_simulated": 1, "currency": "USD"}), TARIFF_PROVENANCE_SIMULATION)

    def test_end_to_end_against_a_real_database_configured_tariff(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        database_path = Path(temp_dir.name) / "config.db"
        initialize_config_database(str(database_path))
        migrate_energy_tariff(database_path, backup=False)

        create_tariff(
            database_path, plant_id=None, effective_date="2026-01-01",
            username="admin", currency="MYR", energy_rate=0.45,
        )
        tariff = get_current_tariff(database_path, plant_id=None)
        self.assertEqual(tariff_provenance_label(tariff), TARIFF_PROVENANCE_CONFIGURED)

    def test_end_to_end_against_a_real_database_auto_simulated_tariff(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        database_path = Path(temp_dir.name) / "config.db"
        initialize_config_database(str(database_path))
        migrate_energy_tariff(database_path, backup=False)

        tariff = get_current_tariff(database_path, plant_id=None)  # never configured - auto-generates the placeholder
        self.assertEqual(tariff_provenance_label(tariff), TARIFF_PROVENANCE_SIMULATION)


if __name__ == "__main__":
    unittest.main()
