import inspect
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import streamlit as st

from database.database import DatabaseManager
from engine import energy_kpi_engine as k
from engine.energy_tariff import create_tariff
from tests.test_energy_kpi_engine import BASE, _plant_id, _seed_config_db, _seed_series
from ui import energy_dashboard_data as d

PAGE_PATH = Path(__file__).resolve().parent.parent / "ui" / "pages" / "15_Energy_Dashboard.py"


class EnergyDashboardDataTestBase(unittest.TestCase):
    """Points ui.energy_dashboard_data at an isolated pair of test
    databases (not the real config/environment paths) and clears
    Streamlit's cache so no result leaks between tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"

        _seed_config_db(self.config_db)
        self.historian = DatabaseManager(db_path=self.machine_db)
        self.plant_id = _plant_id(self.config_db)

        self._patches = [
            mock.patch("ui.energy_dashboard_data.CONFIG_DATABASE_PATH", str(self.config_db)),
            mock.patch("ui.energy_dashboard_data.MACHINE_DATABASE_PATH", str(self.machine_db)),
        ]
        for p in self._patches:
            p.start()
        st.cache_data.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        st.cache_data.clear()
        self.temp_dir.cleanup()


# ---------------------------------------------------------------------------
# Value parity - the data/caching layer must return exactly what calling
# the engine directly returns, never a UI-side recomputation.
# ---------------------------------------------------------------------------

class TestValueParity(EnergyDashboardDataTestBase):
    def test_current_demand_matches_engine_directly(self):
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", [(datetime.now(), 123.4)])

        via_module = d.get_current_demand("p01")
        via_engine = k.current_demand_kw(self.historian, "p01")

        self.assertEqual(via_module["value"], via_engine["value"])
        self.assertEqual(via_module["classification"], via_engine["classification"])
        self.assertEqual(via_module["calculation_type"], via_engine["calculation_type"])

    def test_period_energy_matches_engine_directly(self):
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 50.0)])
        start, end = BASE, BASE + timedelta(hours=1)

        bundle = d.get_period_kpis("p01", start, end, now=end)
        direct = k.energy_for_period(self.historian, "p01", start, end)

        self.assertEqual(bundle["energy"]["value"], direct["value"])

    def test_chiller_cop_matches_engine_directly(self):
        now = datetime.now()
        _seed_series(self.historian, "P01.UTILITY.CHL01.WaterFlow", [(now, 50.0)])
        _seed_series(self.historian, "P01.UTILITY.CHL01.SupplyTemp", [(now, 6.0)])
        _seed_series(self.historian, "P01.UTILITY.CHL01.ReturnTemp", [(now, 12.0)])
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", [(now, 70.0)])

        via_module = d.get_chilled_water_kpis("p01")["CHL01"]
        via_engine = k.chiller_cop(self.historian, "p01", "CHL01")

        self.assertEqual(via_module["cop"]["value"], via_engine["cop"]["value"])
        self.assertEqual(via_module["cooling_output_kw"]["value"], via_engine["cooling_output_kw"]["value"])

    def test_a_formula_change_in_the_engine_is_visible_through_the_data_layer(self):
        """Proves the data layer isn't caching an independent copy of
        the logic - if the engine's math changes, the wrapped result
        changes too."""
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", [(datetime.now(), 100.0)])
        original = d.get_current_demand("p01")

        with mock.patch.object(k, "current_demand_kw", return_value={
            "value": 999.0, "unit": "kW", "period_start": None, "period_end": None,
            "source_tags": [], "classification": "DIRECT", "calculation_type": "MEASURED",
            "assumptions": [], "missing_inputs": [],
        }):
            st.cache_data.clear()
            patched = d.get_current_demand("p01")

        self.assertNotEqual(original["value"], patched["value"])
        self.assertEqual(patched["value"], 999.0)


# ---------------------------------------------------------------------------
# No independent KPI arithmetic in the UI layer
# ---------------------------------------------------------------------------

class TestNoIndependentKpiArithmetic(unittest.TestCase):
    # Fingerprints of the engine's own formulas - if any of these show
    # up outside engine/energy_kpi_engine.py, something duplicated a
    # calculation instead of calling it.
    FORMULA_FINGERPRINTS = ("0.027778", "4.186", "WATER_DENSITY_KG_PER_M3", "PUMP_EFFICIENCY_IMPLAUSIBLE_THRESHOLD_PCT")

    def test_data_module_never_duplicates_engine_formula_constants(self):
        source = inspect.getsource(d)
        for fingerprint in self.FORMULA_FINGERPRINTS:
            self.assertNotIn(fingerprint, source, f"'{fingerprint}' should only appear in engine/energy_kpi_engine.py")

    def test_page_never_duplicates_engine_formula_constants(self):
        source = PAGE_PATH.read_text()
        for fingerprint in self.FORMULA_FINGERPRINTS:
            self.assertNotIn(fingerprint, source, f"'{fingerprint}' should only appear in engine/energy_kpi_engine.py")

    def test_data_module_calls_the_engine_for_every_major_kpi(self):
        source = inspect.getsource(d)
        for engine_call in (
            "k.energy_for_period", "k.cost_for_period", "k.estimated_maximum_demand",
            "k.chiller_cop", "k.pump_hydraulic_efficiency", "k.pump_specific_energy",
            "k.header_specific_energy", "k.water_treatment_specific_energy",
            "k.production_energy_intensity", "k.production_non_production_split",
        ):
            self.assertIn(engine_call, source, f"expected {engine_call} to be called from the data layer")


# ---------------------------------------------------------------------------
# No ranking/verdict language anywhere in the comparison view
# ---------------------------------------------------------------------------

class TestNoRankingLanguage(unittest.TestCase):
    BANNED_PHRASES = ("more efficient", "less efficient", " better ", " worse ", "winner", "outperform", "superior to")

    def test_page_never_declares_a_plant_better_or_worse(self):
        source = PAGE_PATH.read_text().lower()
        for phrase in self.BANNED_PHRASES:
            self.assertNotIn(phrase, source, f"found ranking language: '{phrase}'")


# ---------------------------------------------------------------------------
# Time-period selection - live vs. historical dispatch
# ---------------------------------------------------------------------------

class TestPeriodDispatch(unittest.TestCase):
    def test_period_ending_now_is_live(self):
        now = datetime(2026, 8, 15, 12, 0, 0)
        self.assertTrue(d.is_live_period(now, now=now))
        self.assertTrue(d.is_live_period(now - timedelta(seconds=30), now=now))

    def test_fully_elapsed_period_is_historical(self):
        now = datetime(2026, 8, 15, 12, 0, 0)
        self.assertFalse(d.is_live_period(now - timedelta(days=1), now=now))
        self.assertFalse(d.is_live_period(now.replace(hour=0, minute=0, second=0), now=now.replace(day=16)))


class TestPeriodSelectionReachesEngine(EnergyDashboardDataTestBase):
    def test_different_periods_produce_different_results(self):
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", [
            (BASE, 0.0), (BASE + timedelta(hours=1), 10.0),
            (BASE + timedelta(days=1), 10.0), (BASE + timedelta(days=1, hours=1), 60.0),
        ])

        day1 = d.get_period_kpis("p01", BASE, BASE + timedelta(hours=1), now=BASE + timedelta(hours=1))
        day2 = d.get_period_kpis(
            "p01", BASE + timedelta(days=1), BASE + timedelta(days=1, hours=1), now=BASE + timedelta(days=1, hours=1),
        )

        self.assertNotEqual(day1["energy"]["value"], day2["energy"]["value"])
        self.assertAlmostEqual(day1["energy"]["value"], 10.0)
        self.assertAlmostEqual(day2["energy"]["value"], 50.0)


# ---------------------------------------------------------------------------
# Unavailable KPI handling
# ---------------------------------------------------------------------------

class TestUnavailableHandling(EnergyDashboardDataTestBase):
    def test_water_sys01_unavailable_through_data_layer(self):
        water = d.get_water_treatment_kpis("p01", BASE, BASE + timedelta(hours=1), now=BASE + timedelta(hours=1))
        self.assertEqual(water["SYS01"]["classification"], "UNAVAILABLE")
        self.assertTrue(water["SYS01"]["missing_inputs"])

    def test_billing_month_peak_none_when_worker_has_not_run(self):
        self.assertIsNone(d.get_billing_month_peak("p01"))

    def test_current_demand_unavailable_never_renders_as_zero(self):
        result = d.get_current_demand("p01")  # no data seeded at all
        self.assertEqual(result["classification"], "UNAVAILABLE")
        self.assertNotEqual(result["value"], 0)
        self.assertIsNone(result["value"])


# ---------------------------------------------------------------------------
# Simulated tariff indication
# ---------------------------------------------------------------------------

class TestTariffProvenance(EnergyDashboardDataTestBase):
    def test_simulated_tariff_flagged(self):
        create_tariff(self.config_db, None, BASE.strftime("%Y-%m-%d"), "tester", is_simulated=True, energy_rate=0.5, currency="MYR")
        provenance = d.get_active_tariff_provenance("p01")
        self.assertTrue(provenance["is_simulated"])

    def test_real_tariff_not_flagged(self):
        create_tariff(self.config_db, self.plant_id, BASE.strftime("%Y-%m-%d"), "tester", is_simulated=False, energy_rate=0.6, currency="MYR")
        provenance = d.get_active_tariff_provenance("p01")
        self.assertFalse(provenance["is_simulated"])


# ---------------------------------------------------------------------------
# Mixed production-unit handling
# ---------------------------------------------------------------------------

class TestMixedUnitHandling(EnergyDashboardDataTestBase):
    def test_kg_and_litre_never_combined_through_data_layer(self):
        import sqlite3
        config = sqlite3.connect(self.config_db)
        kg_product = config.execute("SELECT id FROM products WHERE unit_of_measure = 'kg' LIMIT 1").fetchone()[0]
        litre_product = config.execute("SELECT id FROM products WHERE unit_of_measure = 'litre' LIMIT 1").fetchone()[0]
        now = datetime.now().strftime(k.TIME_FORMAT)
        for product_id, qty, start_h, end_h in ((kg_product, 100.0, 1, 2), (litre_product, 200.0, 3, 4)):
            config.execute(
                """
                INSERT INTO production_batches (
                    batch_code, product_id, plant_id, status, start_time, end_time,
                    planned_quantity, actual_quantity, good_quantity, reject_quantity,
                    unit_of_measure, source, created_at
                ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, 0, 'kg', 'simulated', ?)
                """,
                (
                    f"TEST-{product_id}-{start_h}", product_id, self.plant_id,
                    (BASE + timedelta(hours=start_h)).strftime(k.TIME_FORMAT), (BASE + timedelta(hours=end_h)).strftime(k.TIME_FORMAT),
                    qty, qty, qty, now,
                ),
            )
        config.commit()
        config.close()

        points = [(BASE + timedelta(minutes=i), 1000.0 + i) for i in range(0, 24 * 60, 10)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", points)

        bundle = d.get_period_kpis("p01", BASE, BASE + timedelta(days=1), now=BASE + timedelta(days=1))
        intensity = bundle["production_intensity"]

        self.assertIn("kwh_per_kg", intensity)
        self.assertIn("kwh_per_litre", intensity)
        self.assertNotEqual(intensity["kwh_per_kg"]["value"], intensity["kwh_per_litre"]["value"])


# ---------------------------------------------------------------------------
# Query-volume / caching
# ---------------------------------------------------------------------------

class TestQueryCaching(EnergyDashboardDataTestBase):
    def test_repeated_current_demand_calls_hit_cache_not_historian(self):
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", [(datetime.now(), 100.0)])

        call_count = {"n": 0}
        original = DatabaseManager.get_latest

        def counting_get_latest(self, tag):
            call_count["n"] += 1
            return original(self, tag)

        with mock.patch.object(DatabaseManager, "get_latest", counting_get_latest):
            for _ in range(5):
                d.get_current_demand("p01")

        self.assertEqual(call_count["n"], 1, "5 repeated calls within the TTL window should hit the historian exactly once")

    def test_different_plants_each_trigger_their_own_query(self):
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", [(datetime.now(), 100.0)])
        _seed_series(self.historian, "P02.ELEC.MAIN.Power_kW", [(datetime.now(), 200.0)])

        call_count = {"n": 0}
        original = DatabaseManager.get_latest

        def counting_get_latest(self, tag):
            call_count["n"] += 1
            return original(self, tag)

        with mock.patch.object(DatabaseManager, "get_latest", counting_get_latest):
            d.get_current_demand("p01")
            d.get_current_demand("p02")
            d.get_current_demand("p01")  # cache hit
            d.get_current_demand("p02")  # cache hit

        self.assertEqual(call_count["n"], 2)

    def test_historical_period_kpis_cached_across_repeated_calls(self):
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 10.0)])
        past_start, past_end = BASE, BASE + timedelta(hours=1)
        now = past_end + timedelta(days=5)  # firmly in the past relative to "now"

        call_count = {"n": 0}
        original = DatabaseManager.get_history_range

        def counting_get_history_range(self, **kwargs):
            call_count["n"] += 1
            return original(self, **kwargs)

        with mock.patch.object(DatabaseManager, "get_history_range", counting_get_history_range):
            for _ in range(4):
                d.get_period_kpis("p01", past_start, past_end, now=now)

        # One call for the first (real) computation; the other 3 must
        # be cache hits with zero additional historian queries.
        self.assertGreater(call_count["n"], 0)
        first_call_count = call_count["n"]

        with mock.patch.object(DatabaseManager, "get_history_range", counting_get_history_range):
            d.get_period_kpis("p01", past_start, past_end, now=now)

        self.assertEqual(call_count["n"], first_call_count, "a repeated historical-period call must not re-query the historian")


if __name__ == "__main__":
    unittest.main()
