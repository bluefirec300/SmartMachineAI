import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from database.database import DatabaseManager
from database.initialize_config_db import initialize_config_database
from engine import energy_kpi_engine as k
from engine.energy_kpi_migrator import migrate as migrate_energy_kpi
from engine.energy_tariff import create_tariff
from engine.energy_tariff_migrator import migrate as migrate_energy_tariff
from engine.factory_structure_migrator import migrate as migrate_factory_structure
from engine.metadata_migrator import migrate as migrate_metadata
from engine.operating_schedule_migrator import migrate as migrate_operating_schedule
from engine.production_migrator import migrate as migrate_production


BASE = datetime(2026, 8, 15, 0, 0, 0)


def _seed_config_db(database_path: Path) -> None:
    initialize_config_database(str(database_path))

    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO equipment (name, display_name) VALUES (?, ?)",
        ("p01_bead_mill_mill01", "Bead Mill MILL01 (P01)"),
    )
    connection.commit()
    connection.close()

    migrate_metadata(database_path, backup=False)
    migrate_factory_structure(database_path, backup=False)
    migrate_operating_schedule(database_path, backup=False)
    migrate_production(database_path, backup=False)
    migrate_energy_tariff(database_path, backup=False)
    migrate_energy_kpi(database_path, backup=False)


def _plant_id(database_path: Path, code: str = "p01") -> int:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute("SELECT id FROM plants WHERE code = ?", (code,)).fetchone()[0]
    finally:
        connection.close()


def _seed_series(historian: DatabaseManager, tag: str, points: list[tuple[datetime, float]]) -> None:
    for timestamp, value in points:
        historian.save_tag(tag, "sim", value, timestamp)


def _insert_batch(
    database_path: Path, plant_id: int, product_id: int, start: datetime, end: datetime | None,
    status: str = "completed", actual_quantity: float = 100.0,
) -> None:
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        INSERT INTO production_batches (
            batch_code, product_id, plant_id, area_id, system_id, equipment_id, shift_id,
            status, start_time, end_time, planned_quantity, actual_quantity, good_quantity,
            reject_quantity, unit_of_measure, source, created_at
        ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, 0, 'kg', 'simulated', ?)
        """,
        (
            f"TEST-{start.strftime('%Y%m%d%H%M%S')}-{id(start)}-{id(end)}", product_id, plant_id, status,
            start.strftime(k.TIME_FORMAT), end.strftime(k.TIME_FORMAT) if end else None,
            actual_quantity, actual_quantity, actual_quantity,
            datetime.now().strftime(k.TIME_FORMAT),
        ),
    )
    connection.commit()
    connection.close()


def _get_product_id(database_path: Path, unit_of_measure: str) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(
            "SELECT id FROM products WHERE unit_of_measure = ? LIMIT 1", (unit_of_measure,)
        ).fetchone()[0]
    finally:
        connection.close()


class EnergyKpiTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"

        _seed_config_db(self.config_db)
        self.historian = DatabaseManager(db_path=self.machine_db)
        self.plant_id = _plant_id(self.config_db)

    def tearDown(self):
        self.temp_dir.cleanup()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

class TestAccumulatedPositiveDelta(EnergyKpiTestBase):
    def test_sums_positive_deltas(self):
        _seed_series(self.historian, "TAG.Energy_kWh", [
            (BASE, 100.0), (BASE + timedelta(minutes=10), 105.0), (BASE + timedelta(minutes=20), 112.0),
        ])
        total = k.accumulated_positive_delta(self.historian, "TAG.Energy_kWh", BASE, BASE + timedelta(hours=1))
        self.assertAlmostEqual(total, 12.0)

    def test_skips_a_counter_reset(self):
        # Mirrors the real incident that motivated this logic: a
        # service restart re-seeds the counter from a lower baseline.
        _seed_series(self.historian, "TAG.Energy_kWh", [
            (BASE, 100.0), (BASE + timedelta(minutes=10), 110.0),
            (BASE + timedelta(minutes=20), 5.0),   # reset
            (BASE + timedelta(minutes=30), 15.0),
        ])
        total = k.accumulated_positive_delta(self.historian, "TAG.Energy_kWh", BASE, BASE + timedelta(hours=1))
        self.assertAlmostEqual(total, 10.0 + 10.0)  # the reset delta itself is skipped

    def test_none_with_fewer_than_two_samples(self):
        _seed_series(self.historian, "TAG.Energy_kWh", [(BASE, 100.0)])
        total = k.accumulated_positive_delta(self.historian, "TAG.Energy_kWh", BASE, BASE + timedelta(hours=1))
        self.assertIsNone(total)


class TestTrapezoidalIntegral(EnergyKpiTestBase):
    def test_integrates_constant_power(self):
        _seed_series(self.historian, "TAG.Power_kW", [
            (BASE, 10.0), (BASE + timedelta(hours=1), 10.0), (BASE + timedelta(hours=2), 10.0),
        ])
        total = k.trapezoidal_integral(self.historian, "TAG.Power_kW", BASE, BASE + timedelta(hours=2))
        self.assertAlmostEqual(total, 20.0)  # 10kW for 2h = 20kWh


class TestRollingWindowMaxAverage(unittest.TestCase):
    def test_finds_the_highest_window(self):
        samples = [
            (BASE + timedelta(seconds=10 * i), value)
            for i, value in enumerate([10] * 30 + [100] * 90 + [10] * 30)
        ]
        max_avg, occurred_at = k.rolling_window_max_average(samples, window_minutes=15, assumed_interval_seconds=10)
        self.assertGreater(max_avg, 50)  # the 100-value plateau should dominate

    def test_undersized_window_never_registers_as_maximum(self):
        """min_fill_fraction requires a window to hold at least half the
        expected sample count before it's eligible at all - confirmed
        here by supplying far fewer total samples than that threshold,
        so nothing should ever qualify, regardless of value."""
        samples = [(BASE + timedelta(seconds=10 * i), 99999.0 if i == 0 else 10.0) for i in range(5)]
        max_avg, occurred_at = k.rolling_window_max_average(samples, window_minutes=15, assumed_interval_seconds=10)
        self.assertIsNone(max_avg)
        self.assertIsNone(occurred_at)


class TestMergeIntervals(unittest.TestCase):
    def test_merges_overlapping_intervals(self):
        merged = k._merge_intervals([
            (BASE, BASE + timedelta(minutes=30)),
            (BASE + timedelta(minutes=15), BASE + timedelta(minutes=45)),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0], (BASE, BASE + timedelta(minutes=45)))

    def test_keeps_disjoint_intervals_separate(self):
        merged = k._merge_intervals([
            (BASE, BASE + timedelta(minutes=10)),
            (BASE + timedelta(hours=1), BASE + timedelta(hours=1, minutes=10)),
        ])
        self.assertEqual(len(merged), 2)


# ---------------------------------------------------------------------------
# Current demand / staleness
# ---------------------------------------------------------------------------

class TestCurrentDemand(EnergyKpiTestBase):
    def test_fresh_reading_is_direct_measured(self):
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", [(datetime.now(), 123.4)])
        result = k.current_demand_kw(self.historian, "p01")
        self.assertEqual(result["classification"], "DIRECT")
        self.assertEqual(result["calculation_type"], "MEASURED")
        self.assertAlmostEqual(result["value"], 123.4)

    def test_stale_reading_is_unavailable(self):
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", [(datetime.now() - timedelta(hours=2), 123.4)])
        result = k.current_demand_kw(self.historian, "p01")
        self.assertEqual(result["classification"], "UNAVAILABLE")
        self.assertTrue(result["missing_inputs"])

    def test_missing_tag_is_unavailable(self):
        result = k.current_demand_kw(self.historian, "p01")
        self.assertEqual(result["classification"], "UNAVAILABLE")


# ---------------------------------------------------------------------------
# Energy / cost / tariff
# ---------------------------------------------------------------------------

class TestEnergyAndCost(EnergyKpiTestBase):
    def test_energy_for_period(self):
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", [
            (BASE, 1000.0), (BASE + timedelta(hours=12), 1200.0),
        ])
        result = k.energy_for_period(self.historian, "p01", BASE, BASE + timedelta(days=1))
        self.assertEqual(result["classification"], "DIRECT")
        self.assertAlmostEqual(result["value"], 200.0)

    def test_cost_uses_plant_specific_tariff_when_present(self):
        create_tariff(self.config_db, self.plant_id, BASE.strftime("%Y-%m-%d"), "tester", energy_rate=1.0, currency="MYR")
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 50.0)])

        result = k.cost_for_period(self.config_db, self.historian, self.plant_id, "p01", BASE, BASE + timedelta(days=1))
        self.assertEqual(result["classification"], "DIRECT")
        self.assertAlmostEqual(result["value"], 50.0)

    def test_cost_falls_back_to_factory_wide_tariff(self):
        create_tariff(self.config_db, None, BASE.strftime("%Y-%m-%d"), "tester", energy_rate=2.0, currency="MYR")
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 10.0)])

        result = k.cost_for_period(self.config_db, self.historian, self.plant_id, "p01", BASE, BASE + timedelta(days=1))
        self.assertEqual(result["classification"], "DIRECT")
        self.assertAlmostEqual(result["value"], 20.0)

    def test_cost_splits_across_a_tariff_change_mid_period(self):
        day1 = BASE
        day2 = BASE + timedelta(days=1)
        create_tariff(self.config_db, self.plant_id, day1.strftime("%Y-%m-%d"), "tester", energy_rate=1.0, currency="MYR")
        create_tariff(self.config_db, self.plant_id, day2.strftime("%Y-%m-%d"), "tester", energy_rate=2.0, currency="MYR")

        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", [
            (day1, 0.0), (day1 + timedelta(hours=12), 10.0),
            (day2, 10.0), (day2 + timedelta(hours=12), 20.0),
        ])

        result = k.cost_for_period(self.config_db, self.historian, self.plant_id, "p01", day1, day2 + timedelta(days=1))
        # day1: 10 kWh @ 1.0 = 10; day2: 10 kWh @ 2.0 = 20 -> total 30
        self.assertAlmostEqual(result["value"], 30.0)

    def test_cost_flags_partial_total_when_some_days_have_no_tariff(self):
        """A real bug caught during Phase 7 live verification: a
        tariff configured only partway through a month made
        cost_for_period silently return a much-too-low figure with no
        indication it was incomplete. Confirms the fix - a day with
        real energy but no tariff in effect is excluded from the cost
        total (not fabricated) AND explicitly flagged as partial."""
        month_start = BASE.replace(day=1)
        tariff_start = BASE  # tariff only takes effect on day 15, mid-month
        create_tariff(self.config_db, self.plant_id, tariff_start.strftime("%Y-%m-%d"), "tester", energy_rate=1.0, currency="MYR")

        points = [(month_start + timedelta(days=d, hours=h), 100.0 * d + h * 10.0) for d in range(16) for h in (0, 12)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", points)

        now = BASE + timedelta(hours=12)
        result = k.cost_for_period(self.config_db, self.historian, self.plant_id, "p01", month_start, now)

        self.assertEqual(result["classification"], "DIRECT")
        self.assertTrue(any("PARTIAL TOTAL" in a for a in result["assumptions"]))

    def test_projected_month_cost_is_estimated(self):
        month_start = BASE.replace(day=1)
        create_tariff(self.config_db, self.plant_id, month_start.strftime("%Y-%m-%d"), "tester", energy_rate=1.0, currency="MYR")
        # At least two points per day so cost_for_period's day-by-day
        # accumulation has something to difference within each day.
        points = [(month_start + timedelta(days=d, hours=h), 100.0 * d + h * 10.0) for d in range(6) for h in (0, 12)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", points)
        now = month_start + timedelta(days=5)
        result = k.projected_month_cost(self.config_db, self.historian, self.plant_id, "p01", now)
        self.assertEqual(result["classification"], "ESTIMATED")
        self.assertEqual(result["calculation_type"], "ESTIMATED")
        self.assertIsNotNone(result["value"])


class TestEstimatedMaximumDemand(EnergyKpiTestBase):
    def test_labeled_estimated_never_official(self):
        points = [(BASE + timedelta(seconds=10 * i), 100.0 + i) for i in range(200)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", points)
        result = k.estimated_maximum_demand(self.historian, "p01", BASE, BASE + timedelta(hours=1))
        self.assertEqual(result["classification"], "DIRECT")
        self.assertTrue(any("Estimated Maximum Demand" in a and "NOT confirmed" in a for a in result["assumptions"]))

    def test_demand_charge_unavailable_without_configured_rate(self):
        create_tariff(self.config_db, self.plant_id, BASE.strftime("%Y-%m-%d"), "tester", energy_rate=1.0, currency="MYR")
        points = [(BASE + timedelta(seconds=10 * i), 100.0) for i in range(200)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", points)
        result = k.estimated_demand_charge(self.config_db, self.historian, self.plant_id, "p01", BASE, BASE + timedelta(hours=1))
        self.assertEqual(result["classification"], "UNAVAILABLE")

    def test_demand_charge_direct_when_rate_configured(self):
        create_tariff(
            self.config_db, self.plant_id, BASE.strftime("%Y-%m-%d"), "tester",
            mode="advanced", energy_rate=1.0, currency="MYR", maximum_demand_charge=50.0,
        )
        points = [(BASE + timedelta(seconds=10 * i), 100.0) for i in range(200)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", points)
        result = k.estimated_demand_charge(self.config_db, self.historian, self.plant_id, "p01", BASE, BASE + timedelta(hours=1))
        self.assertEqual(result["classification"], "DIRECT")
        self.assertAlmostEqual(result["value"], 100.0 * 50.0)


# ---------------------------------------------------------------------------
# Production / non-production split - overlapping batches
# ---------------------------------------------------------------------------

class TestProductionSplit(EnergyKpiTestBase):
    def test_overlapping_batches_do_not_double_count_energy(self):
        product_id = _get_product_id(self.config_db, "kg")
        # Two overlapping batches: 10:00-10:30 and 10:15-10:45.
        _insert_batch(self.config_db, self.plant_id, product_id, BASE + timedelta(hours=10), BASE + timedelta(hours=10, minutes=30))
        _insert_batch(self.config_db, self.plant_id, product_id, BASE + timedelta(hours=10, minutes=15), BASE + timedelta(hours=10, minutes=45))

        # Steady 100kW for the whole day -> production energy should
        # equal exactly the UNION window (10:00-10:45 = 45 min), not
        # the sum of both batches' own 30-minute spans (60 min worth).
        points = []
        t = BASE
        while t <= BASE + timedelta(days=1):
            points.append((t, 1000.0 + (t - BASE).total_seconds() / 60.0))  # +1 kWh per minute
            t += timedelta(minutes=1)
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", points)

        split = k.production_non_production_split(
            self.historian, self.config_db, "p01", self.plant_id, BASE, BASE + timedelta(days=1), now=BASE + timedelta(days=1),
        )
        production_kwh = split["production_energy_kwh"]["value"]
        # 45 minutes of the 1kWh/min series = ~45kWh, generously bounded
        # to allow for sample-boundary rounding, but must be well under
        # 60 (which is what double-counting the overlap would produce).
        self.assertGreater(production_kwh, 35)
        self.assertLess(production_kwh, 55)

    def test_non_production_plus_production_equals_total(self):
        product_id = _get_product_id(self.config_db, "kg")
        _insert_batch(self.config_db, self.plant_id, product_id, BASE + timedelta(hours=10), BASE + timedelta(hours=11))

        points = [(BASE + timedelta(minutes=i), 1000.0 + i) for i in range(0, 24 * 60, 10)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", points)

        split = k.production_non_production_split(
            self.historian, self.config_db, "p01", self.plant_id, BASE, BASE + timedelta(days=1), now=BASE + timedelta(days=1),
        )
        total = k.energy_for_period(self.historian, "p01", BASE, BASE + timedelta(days=1))["value"]
        combined = split["production_energy_kwh"]["value"] + split["non_production_energy_kwh"]["value"]
        self.assertAlmostEqual(combined, total, delta=0.01)


class TestEstimatedBaseLoad(EnergyKpiTestBase):
    def test_returns_a_low_percentile_not_the_average(self):
        points = [(BASE + timedelta(seconds=10 * i), 20.0) for i in range(200)]
        points += [(BASE + timedelta(seconds=10 * (i + 200)), 200.0) for i in range(10)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", points)

        result = k.estimated_base_load(self.historian, self.config_db, "p01", self.plant_id, BASE, BASE + timedelta(hours=1))
        self.assertEqual(result["classification"], "ESTIMATED")
        self.assertLess(result["value"], 50)  # the low plateau, not skewed up by the spike


# ---------------------------------------------------------------------------
# Production normalization - never combine kg and litre
# ---------------------------------------------------------------------------

class TestProductionEnergyIntensity(EnergyKpiTestBase):
    def test_kg_and_litre_never_combined(self):
        kg_product = _get_product_id(self.config_db, "kg")
        litre_product = _get_product_id(self.config_db, "litre")
        _insert_batch(self.config_db, self.plant_id, kg_product, BASE + timedelta(hours=1), BASE + timedelta(hours=2), actual_quantity=100.0)
        _insert_batch(self.config_db, self.plant_id, litre_product, BASE + timedelta(hours=3), BASE + timedelta(hours=4), actual_quantity=200.0)

        points = [(BASE + timedelta(minutes=i), 1000.0 + i) for i in range(0, 24 * 60, 10)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", points)

        results = k.production_energy_intensity(self.config_db, self.historian, "p01", self.plant_id, BASE, BASE + timedelta(days=1), now=BASE + timedelta(days=1))
        self.assertIn("kwh_per_kg", results)
        self.assertIn("kwh_per_litre", results)
        self.assertNotEqual(results["kwh_per_kg"]["value"], results["kwh_per_litre"]["value"])
        self.assertEqual(results["kwh_per_kg"]["classification"], "DIRECT")
        self.assertEqual(results["kwh_per_litre"]["classification"], "DIRECT")

    def test_tonne_unavailable_for_litre_only_production(self):
        litre_product = _get_product_id(self.config_db, "litre")
        _insert_batch(self.config_db, self.plant_id, litre_product, BASE + timedelta(hours=1), BASE + timedelta(hours=2), actual_quantity=200.0)

        points = [(BASE + timedelta(minutes=i), 1000.0 + i) for i in range(0, 24 * 60, 10)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", points)

        results = k.production_energy_intensity(self.config_db, self.historian, "p01", self.plant_id, BASE, BASE + timedelta(days=1), now=BASE + timedelta(days=1))
        self.assertEqual(results["kwh_per_tonne"]["classification"], "UNAVAILABLE")

    def test_tonne_direct_for_kg_production(self):
        kg_product = _get_product_id(self.config_db, "kg")
        _insert_batch(self.config_db, self.plant_id, kg_product, BASE + timedelta(hours=1), BASE + timedelta(hours=2), actual_quantity=1000.0)

        points = [(BASE + timedelta(minutes=i), 1000.0 + i) for i in range(0, 24 * 60, 10)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Energy_kWh", points)

        results = k.production_energy_intensity(self.config_db, self.historian, "p01", self.plant_id, BASE, BASE + timedelta(days=1), now=BASE + timedelta(days=1))
        self.assertEqual(results["kwh_per_tonne"]["classification"], "DIRECT")
        self.assertAlmostEqual(results["kwh_per_tonne"]["value"], results["kwh_per_kg"]["value"] * 1000, delta=0.01)


# ---------------------------------------------------------------------------
# Compressor
# ---------------------------------------------------------------------------

class TestCompressorKPIs(EnergyKpiTestBase):
    def test_loaded_unloaded_percentages(self):
        _seed_series(self.historian, "P01.UTILITY.AC01.LoadedHours", [(BASE, 0.0), (BASE + timedelta(hours=1), 0.6)])
        _seed_series(self.historian, "P01.UTILITY.AC01.UnloadedHours", [(BASE, 0.0), (BASE + timedelta(hours=1), 0.4)])
        result = k.compressor_load_stats(self.historian, "p01", "AC01", BASE, BASE + timedelta(hours=1))
        self.assertAlmostEqual(result["loaded_pct"]["value"], 60.0)
        self.assertAlmostEqual(result["unloaded_pct"]["value"], 40.0)

    def test_header_specific_energy_and_inferred_allocation(self):
        for instance in ("AC01", "AC02", "AC03"):
            _seed_series(self.historian, f"P01.UTILITY.{instance}.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 10.0)])
        _seed_series(self.historian, "P01.UTILITY.AIRHDR01.FlowTotal", [(BASE, 0.0), (BASE + timedelta(hours=1), 30.0)])

        header = k.header_specific_energy(self.historian, "p01", ["AC01", "AC02", "AC03"], BASE, BASE + timedelta(hours=1))
        self.assertEqual(header["classification"], "DIRECT")
        self.assertAlmostEqual(header["value"], 1.0)  # 30kWh / 30 Nm3

        allocation = k.per_compressor_inferred_allocation(self.historian, "p01", "AC01", ["AC01", "AC02", "AC03"], BASE, BASE + timedelta(hours=1))
        self.assertEqual(allocation["classification"], "ESTIMATED")

    def test_header_specific_energy_zero_flow_is_unavailable(self):
        for instance in ("AC01", "AC02", "AC03"):
            _seed_series(self.historian, f"P01.UTILITY.{instance}.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 10.0)])
        _seed_series(self.historian, "P01.UTILITY.AIRHDR01.FlowTotal", [(BASE, 0.0), (BASE + timedelta(hours=1), 0.0)])

        header = k.header_specific_energy(self.historian, "p01", ["AC01", "AC02", "AC03"], BASE, BASE + timedelta(hours=1))
        self.assertEqual(header["classification"], "UNAVAILABLE")

    def test_header_specific_energy_falls_back_to_flow_rate_when_no_flow_total(self):
        """P02's real header has no FlowTotal tag at all (found live
        during Phase 6 verification) - only the instantaneous Flow
        rate. Confirms the numerical-integration fallback still
        produces a DIRECT result rather than going UNAVAILABLE."""
        for instance in ("AC01", "AC02", "AC03"):
            _seed_series(self.historian, f"P02.UTILITY.{instance}.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 10.0)])
        # No P02.UTILITY.AIRHDR01.FlowTotal seeded at all - only Flow.
        _seed_series(self.historian, "P02.UTILITY.AIRHDR01.Flow", [
            (BASE, 30.0), (BASE + timedelta(hours=1), 30.0),
        ])

        header = k.header_specific_energy(self.historian, "p02", ["AC01", "AC02", "AC03"], BASE, BASE + timedelta(hours=1))
        self.assertEqual(header["classification"], "DIRECT")
        self.assertAlmostEqual(header["value"], 1.0, delta=0.01)  # 30kWh / 30 Nm3
        self.assertTrue(any("numerically integrated" in a for a in header["assumptions"]))

    def test_total_compressor_energy_and_header_air_volume_standalone(self):
        for instance in ("AC01", "AC02", "AC03"):
            _seed_series(self.historian, f"P01.UTILITY.{instance}.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 10.0)])
        _seed_series(self.historian, "P01.UTILITY.AIRHDR01.FlowTotal", [(BASE, 0.0), (BASE + timedelta(hours=1), 30.0)])

        energy = k.total_compressor_energy(self.historian, "p01", ["AC01", "AC02", "AC03"], BASE, BASE + timedelta(hours=1))
        self.assertEqual(energy["classification"], "DIRECT")
        self.assertAlmostEqual(energy["value"], 30.0)

        volume = k.header_air_volume(self.historian, "p01", BASE, BASE + timedelta(hours=1))
        self.assertEqual(volume["classification"], "DIRECT")
        self.assertEqual(volume["calculation_type"], "MEASURED")
        self.assertAlmostEqual(volume["value"], 30.0)

    def test_header_air_volume_calculated_when_falling_back_to_flow_rate(self):
        _seed_series(self.historian, "P02.UTILITY.AIRHDR01.Flow", [(BASE, 30.0), (BASE + timedelta(hours=1), 30.0)])
        volume = k.header_air_volume(self.historian, "p02", BASE, BASE + timedelta(hours=1))
        self.assertEqual(volume["classification"], "DIRECT")
        self.assertEqual(volume["calculation_type"], "CALCULATED")  # numerically integrated, not a direct meter reading

    def test_compressed_air_cost_per_nm3(self):
        create_tariff(self.config_db, self.plant_id, BASE.strftime("%Y-%m-%d"), "tester", energy_rate=2.0, currency="MYR")
        for instance in ("AC01", "AC02", "AC03"):
            _seed_series(self.historian, f"P01.UTILITY.{instance}.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 10.0)])
        _seed_series(self.historian, "P01.UTILITY.AIRHDR01.FlowTotal", [(BASE, 0.0), (BASE + timedelta(hours=1), 30.0)])

        result = k.compressed_air_cost_per_nm3(self.config_db, self.historian, self.plant_id, "p01", ["AC01", "AC02", "AC03"], BASE, BASE + timedelta(hours=1))
        self.assertEqual(result["classification"], "DIRECT")
        self.assertAlmostEqual(result["value"], 2.0)  # 1.0 kWh/Nm3 * 2.0 MYR/kWh

    def test_compressed_air_cost_per_nm3_unavailable_without_tariff_rate(self):
        for instance in ("AC01", "AC02", "AC03"):
            _seed_series(self.historian, f"P01.UTILITY.{instance}.Energy_kWh", [(BASE, 0.0), (BASE + timedelta(hours=1), 10.0)])
        _seed_series(self.historian, "P01.UTILITY.AIRHDR01.FlowTotal", [(BASE, 0.0), (BASE + timedelta(hours=1), 30.0)])

        result = k.compressed_air_cost_per_nm3(self.config_db, self.historian, self.plant_id, "p01", ["AC01", "AC02", "AC03"], BASE, BASE + timedelta(hours=1))
        self.assertEqual(result["classification"], "UNAVAILABLE")


# ---------------------------------------------------------------------------
# Chiller - divide-by-zero and negative-deltaT guards
# ---------------------------------------------------------------------------

class TestChillerCOP(EnergyKpiTestBase):
    def _seed_chiller(self, flow, supply, return_temp, power):
        now = datetime.now()
        _seed_series(self.historian, "P01.UTILITY.CHL01.WaterFlow", [(now, flow)])
        _seed_series(self.historian, "P01.UTILITY.CHL01.SupplyTemp", [(now, supply)])
        _seed_series(self.historian, "P01.UTILITY.CHL01.ReturnTemp", [(now, return_temp)])
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", [(now, power)])

    def test_normal_case_is_direct_calculated(self):
        self._seed_chiller(flow=50.0, supply=6.0, return_temp=12.0, power=70.0)
        result = k.chiller_cop(self.historian, "p01", "CHL01")
        self.assertEqual(result["cooling_output_kw"]["classification"], "DIRECT")
        self.assertEqual(result["cop"]["classification"], "DIRECT")
        self.assertEqual(result["cop"]["calculation_type"], "CALCULATED")
        self.assertGreater(result["cop"]["value"], 0)

    def test_zero_flow_is_unavailable_not_divide_by_zero(self):
        self._seed_chiller(flow=0.0, supply=6.0, return_temp=12.0, power=70.0)
        result = k.chiller_cop(self.historian, "p01", "CHL01")
        self.assertEqual(result["cooling_output_kw"]["classification"], "UNAVAILABLE")
        self.assertEqual(result["cop"]["classification"], "UNAVAILABLE")

    def test_negative_delta_t_is_unavailable_not_misleading(self):
        self._seed_chiller(flow=50.0, supply=12.0, return_temp=6.0, power=70.0)  # reversed
        result = k.chiller_cop(self.historian, "p01", "CHL01")
        self.assertEqual(result["cooling_output_kw"]["classification"], "UNAVAILABLE")
        self.assertEqual(result["cop"]["classification"], "UNAVAILABLE")

    def test_zero_power_is_unavailable(self):
        self._seed_chiller(flow=50.0, supply=6.0, return_temp=12.0, power=0.0)
        result = k.chiller_cop(self.historian, "p01", "CHL01")
        self.assertEqual(result["cop"]["classification"], "UNAVAILABLE")

    def test_raw_measured_components_exposed(self):
        self._seed_chiller(flow=50.0, supply=6.0, return_temp=12.0, power=70.0)
        result = k.chiller_cop(self.historian, "p01", "CHL01")
        self.assertEqual(result["power_kw"]["value"], 70.0)
        self.assertEqual(result["power_kw"]["calculation_type"], "MEASURED")
        self.assertEqual(result["water_flow_m3h"]["value"], 50.0)
        self.assertEqual(result["supply_temp_c"]["value"], 6.0)
        self.assertEqual(result["return_temp_c"]["value"], 12.0)
        self.assertEqual(result["delta_t_c"]["value"], 6.0)
        self.assertEqual(result["delta_t_c"]["calculation_type"], "CALCULATED")

    def test_raw_components_still_available_when_cop_is_unavailable(self):
        """Even when cooling output/COP go UNAVAILABLE (e.g. negative
        deltaT), the raw measured readings themselves should still be
        shown - they're real sensor readings, independent of whether
        the derived formula's guard conditions are satisfied."""
        self._seed_chiller(flow=50.0, supply=12.0, return_temp=6.0, power=70.0)  # reversed deltaT
        result = k.chiller_cop(self.historian, "p01", "CHL01")
        self.assertEqual(result["power_kw"]["classification"], "DIRECT")
        self.assertEqual(result["supply_temp_c"]["classification"], "DIRECT")


# ---------------------------------------------------------------------------
# Pump - implausible efficiency flagged, not silently accepted
# ---------------------------------------------------------------------------

class TestPumpHydraulicEfficiency(EnergyKpiTestBase):
    def _seed_pump(self, flow, suction, discharge, power):
        now = datetime.now()
        _seed_series(self.historian, "P01.UTILITY.CHWP01.Flow", [(now, flow)])
        _seed_series(self.historian, "P01.UTILITY.CHWP01.SuctionPressure", [(now, suction)])
        _seed_series(self.historian, "P01.UTILITY.CHWP01.DischargePressure", [(now, discharge)])
        _seed_series(self.historian, "P01.UTILITY.CHWP01.Power_kW", [(now, power)])

    def test_normal_case(self):
        self._seed_pump(flow=40.0, suction=3.5, discharge=4.5, power=15.0)
        result = k.pump_hydraulic_efficiency(self.historian, "p01", "UTILITY", "CHWP01")
        self.assertEqual(result["efficiency_pct"]["classification"], "DIRECT")

    def test_zero_flow_is_unavailable(self):
        self._seed_pump(flow=0.0, suction=3.5, discharge=4.5, power=15.0)
        result = k.pump_hydraulic_efficiency(self.historian, "p01", "UTILITY", "CHWP01")
        self.assertEqual(result["hydraulic_power_kw"]["classification"], "UNAVAILABLE")

    def test_negative_delta_p_is_unavailable(self):
        self._seed_pump(flow=40.0, suction=4.5, discharge=3.5, power=15.0)  # reversed
        result = k.pump_hydraulic_efficiency(self.historian, "p01", "UTILITY", "CHWP01")
        self.assertEqual(result["hydraulic_power_kw"]["classification"], "UNAVAILABLE")

    def test_implausible_efficiency_is_flagged_not_silently_accepted(self):
        # Deliberately tiny electrical power relative to a large
        # hydraulic power -> efficiency far over 100%.
        self._seed_pump(flow=1000.0, suction=1.0, discharge=10.0, power=0.5)
        result = k.pump_hydraulic_efficiency(self.historian, "p01", "UTILITY", "CHWP01")
        self.assertEqual(result["efficiency_pct"]["classification"], "ESTIMATED")
        self.assertTrue(any("exceeds physical plausibility" in a for a in result["efficiency_pct"]["assumptions"]))
        # The raw (implausible) figure is still visible, not hidden.
        self.assertGreater(result["efficiency_pct"]["value"], 100)

    def test_raw_measured_components_exposed(self):
        self._seed_pump(flow=40.0, suction=3.5, discharge=4.5, power=15.0)
        result = k.pump_hydraulic_efficiency(self.historian, "p01", "UTILITY", "CHWP01")
        self.assertEqual(result["power_kw"]["value"], 15.0)
        self.assertEqual(result["flow_m3h"]["value"], 40.0)
        self.assertAlmostEqual(result["delta_p_bar"]["value"], 1.0)


class TestPumpSpecificEnergy(EnergyKpiTestBase):
    def test_integrates_power_and_flow_over_period(self):
        _seed_series(self.historian, "P01.UTILITY.CHWP01.Power_kW", [
            (BASE + timedelta(minutes=i), 10.0) for i in range(0, 120, 10)
        ])
        _seed_series(self.historian, "P01.UTILITY.CHWP01.Flow", [
            (BASE + timedelta(minutes=i), 20.0) for i in range(0, 120, 10)
        ])
        result = k.pump_specific_energy(self.historian, "p01", "UTILITY", "CHWP01", BASE, BASE + timedelta(hours=2))
        self.assertEqual(result["classification"], "DIRECT")
        # 10kW for 2h = 20kWh; 20 m3/h for 2h = 40 m3 -> 0.5 kWh/m3
        self.assertAlmostEqual(result["value"], 0.5, delta=0.01)

    def test_unavailable_with_insufficient_history(self):
        result = k.pump_specific_energy(self.historian, "p01", "UTILITY", "CHWP01", BASE, BASE + timedelta(hours=2))
        self.assertEqual(result["classification"], "UNAVAILABLE")


# ---------------------------------------------------------------------------
# Water treatment
# ---------------------------------------------------------------------------

class TestWaterTreatment(EnergyKpiTestBase):
    def test_ro_available_when_power_and_flow_exist(self):
        _seed_series(self.historian, "P01.WT.RO01.Power_kW", [(BASE + timedelta(minutes=i), 20.0) for i in range(0, 120, 10)])
        _seed_series(self.historian, "P01.WT.RO01.PermeateFlow", [(BASE + timedelta(minutes=i), 8.0) for i in range(0, 120, 10)])
        result = k.water_treatment_specific_energy(self.historian, "p01", "WT", "RO01", "Power_kW", "PermeateFlow", BASE, BASE + timedelta(hours=2))
        self.assertEqual(result["classification"], "DIRECT")

    def test_sys_unavailable_no_power_tag(self):
        _seed_series(self.historian, "P01.WT.SYS01.TreatedWaterFlow", [(BASE + timedelta(minutes=i), 15.0) for i in range(0, 120, 10)])
        result = k.water_treatment_specific_energy(self.historian, "p01", "WT", "SYS01", None, "TreatedWaterFlow", BASE, BASE + timedelta(hours=2))
        self.assertEqual(result["classification"], "UNAVAILABLE")
        self.assertTrue(any("no electrical power tag" in m for m in result["missing_inputs"]))


# ---------------------------------------------------------------------------
# After-hours consumption - unavailable without configured shifts
# ---------------------------------------------------------------------------

class TestAfterHours(EnergyKpiTestBase):
    def test_unavailable_when_no_shifts_configured(self):
        result = k.after_hours_consumption(self.config_db, self.historian, "p01", self.plant_id, BASE, BASE + timedelta(days=1))
        self.assertEqual(result["classification"], "UNAVAILABLE")


# ---------------------------------------------------------------------------
# Worker restart-safety (demand interval correct across a fresh process)
# ---------------------------------------------------------------------------

class TestWorkerRestartSafety(EnergyKpiTestBase):
    def test_repeated_ticks_do_not_regress_or_duplicate(self):
        from app.energy_kpi_worker import update_maximum_demand, _billing_period

        now = BASE + timedelta(hours=5)
        period_start, _ = _billing_period(now)
        points = [(period_start + timedelta(seconds=10 * i), 100.0 + i) for i in range(400)]
        _seed_series(self.historian, "P01.ELEC.MAIN.Power_kW", points)

        plant = {"id": self.plant_id, "code": "p01"}
        update_maximum_demand(self.config_db, self.historian, plant, now)

        connection = sqlite3.connect(self.config_db)
        first = connection.execute("SELECT max_demand_kw FROM energy_kpi_maximum_demand").fetchall()
        connection.close()
        self.assertEqual(len(first), 1)

        # A second, independent "tick" (simulating a fresh process
        # after a restart) against the same data must not duplicate
        # the row or regress the stored value.
        update_maximum_demand(self.config_db, self.historian, plant, now)
        connection = sqlite3.connect(self.config_db)
        second = connection.execute("SELECT max_demand_kw FROM energy_kpi_maximum_demand").fetchall()
        connection.close()
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0][0], second[0][0])


if __name__ == "__main__":
    unittest.main()
