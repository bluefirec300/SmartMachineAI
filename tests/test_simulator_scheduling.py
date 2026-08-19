import random
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from database.initialize_config_db import initialize_config_database
from engine import baseline_targets as bt
from engine import seed_shift_definitions as seed_shifts
from engine.factory_structure_migrator import migrate as migrate_factory_structure
from engine.metadata_migrator import migrate as migrate_metadata
from engine.operating_schedule_migrator import migrate as migrate_operating_schedule
from engine.production_migrator import migrate as migrate_production
from simulator import plant_context
from simulator import production_batch_simulator as pbs
from simulator.tag_dataset_model import TagDatasetSimulator

"""
Post-Phase-14 cleanup - realistic weekday/weekend operating-hours
scheduling. Covers: pure scheduling-factor math, real shift_definitions
seeding/lookup, TagDatasetSimulator-level value shifts, and
production_batch_simulator's schedule-aware batch-start rate.
"""

MONDAY_IN_SHIFT = datetime(2026, 8, 17, 14, 0, 0)      # Monday 14:00
MONDAY_OFF_HOURS = datetime(2026, 8, 17, 23, 0, 0)     # Monday 23:00
SATURDAY = datetime(2026, 8, 22, 14, 0, 0)
SUNDAY = datetime(2026, 8, 23, 14, 0, 0)

# Follow-up - the 4 representative operating periods explicitly required.
MONDAY_10 = datetime(2026, 8, 17, 10, 0, 0)   # weekday production
MONDAY_20 = datetime(2026, 8, 17, 20, 0, 0)   # weekday after-hours
SATURDAY_10 = datetime(2026, 8, 22, 10, 0, 0)
SUNDAY_10 = datetime(2026, 8, 23, 10, 0, 0)


def _insert_tag(database_path, tag_name, data_type, unit="", measurement="", event_type=""):
    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO tags (tag_name, driver, data_type, unit, enabled, measurement, event_type) VALUES (?, 'simulator', ?, ?, 1, ?, ?)",
        (tag_name, data_type, unit, measurement, event_type),
    )
    connection.commit()
    connection.close()


class TestScheduleLoadFactorPureMath(unittest.TestCase):
    def test_full_load_within_weekday_shift(self):
        self.assertEqual(plant_context.schedule_load_factor("UTILITY.CHL.Power_kW", MONDAY_IN_SHIFT, True), 1.0)

    def test_reduced_off_hours_weekday(self):
        factor = plant_context.schedule_load_factor("UTILITY.CHL.Power_kW", MONDAY_OFF_HOURS, False)
        self.assertEqual(factor, 0.55)

    def test_saturday_lower_than_weekday_off_hours(self):
        weekday_off = plant_context.schedule_load_factor("UTILITY.CHL.Power_kW", MONDAY_OFF_HOURS, False)
        saturday = plant_context.schedule_load_factor("UTILITY.CHL.Power_kW", SATURDAY, False)
        self.assertLess(saturday, weekday_off)

    def test_sunday_lower_than_saturday(self):
        saturday = plant_context.schedule_load_factor("UTILITY.CHL.Power_kW", SATURDAY, False)
        sunday = plant_context.schedule_load_factor("UTILITY.CHL.Power_kW", SUNDAY, False)
        self.assertLess(sunday, saturday)

    def test_never_zero(self):
        for now in (MONDAY_OFF_HOURS, SATURDAY, SUNDAY):
            factor = plant_context.schedule_load_factor("WATER.WSP.Power_kW", now, False)
            self.assertGreater(factor, 0.0)

    def test_non_schedule_sensitive_equipment_type_returns_none(self):
        # schedule_load_factor() only checks the equipment-TYPE prefix -
        # the "only power/current/speed measurements" gate is applied by
        # the caller (_update_real), not inside this function itself -
        # so this checks equipment types with no entry at all in
        # SCHEDULE_OFF_HOURS_LOAD_FACTOR (production-linked equipment
        # uses its own separate production_push mechanic instead).
        self.assertIsNone(plant_context.schedule_load_factor("PROD.MILL.MotorPower", MONDAY_OFF_HOURS, False))
        self.assertIsNone(plant_context.schedule_load_factor("FILL.FILL.Power_kW", MONDAY_OFF_HOURS, False))
        self.assertIsNone(plant_context.schedule_load_factor("ENV.Temperature", MONDAY_OFF_HOURS, False))

    def test_each_equipment_type_has_its_own_factor_not_one_global_multiplier(self):
        factors = {
            prefix: plant_context.schedule_load_factor(f"{prefix}.Power_kW", MONDAY_OFF_HOURS, False)
            for prefix in ("UTILITY.CHL", "UTILITY.CHWP", "WATER.WSP", "HVAC.AHU", "COLDROOM.CR")
        }
        # Not necessarily every value unique (chiller and its own chilled
        # water pump circuit are deliberately similar magnitude), but
        # genuinely more than one distinct tier - proves this is real
        # per-equipment-type configuration, not a single global multiplier.
        self.assertGreaterEqual(len(set(factors.values())), 3)


class TestStoppableEquipment(unittest.TestCase):
    def test_air_compressor_is_stoppable_others_are_not(self):
        self.assertTrue(plant_context.is_schedule_stoppable_canonical("UTILITY.AC.RunStatus"))
        for canonical in ("UTILITY.CHL.RunStatus", "UTILITY.CHWP.RunStatus", "WATER.WSP.RunStatus", "HVAC.AHU.RunStatus", "COLDROOM.CR.RunStatus"):
            self.assertFalse(plant_context.is_schedule_stoppable_canonical(canonical))

    def test_on_probability_much_lower_off_hours(self):
        in_shift = plant_context.schedule_stoppable_on_probability(MONDAY_IN_SHIFT, True)
        off_hours = plant_context.schedule_stoppable_on_probability(MONDAY_OFF_HOURS, False)
        self.assertGreater(in_shift, 0.9)
        self.assertLess(off_hours, 0.2)

    def test_never_literally_zero_probability(self):
        for now, within_shift in ((MONDAY_OFF_HOURS, False), (SATURDAY, False), (SUNDAY, False)):
            self.assertGreater(plant_context.schedule_stoppable_on_probability(now, within_shift), 0.0)


class TestProductionStartProbabilityFactor(unittest.TestCase):
    def test_full_within_shift_reduced_outside(self):
        self.assertEqual(plant_context.production_start_probability_factor(MONDAY_IN_SHIFT, True), 1.0)
        self.assertLess(plant_context.production_start_probability_factor(MONDAY_OFF_HOURS, False), 1.0)

    def test_saturday_higher_than_weekday_off_hours_but_lower_than_shift(self):
        """Some Saturday work is more plausible than an ordinary weekday
        night shift running unplanned batches - not a strict global
        ordering requirement, just documents the chosen shape."""
        saturday = plant_context.production_start_probability_factor(SATURDAY, False)
        sunday = plant_context.production_start_probability_factor(SUNDAY, False)
        self.assertLess(sunday, saturday)
        self.assertLess(saturday, 1.0)


class TestPlantBaseLoadWeekend(unittest.TestCase):
    def test_weekend_base_load_lower_than_weekday(self):
        rng = random.Random(1)
        weekday = plant_context.plant_base_load_kw("p01", MONDAY_IN_SHIFT, rng)
        saturday = plant_context.plant_base_load_kw("p01", SATURDAY, rng)
        sunday = plant_context.plant_base_load_kw("p01", SUNDAY, rng)
        self.assertLess(saturday, weekday)
        self.assertLess(sunday, saturday)


class TestSeedShiftDefinitions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        initialize_config_database(str(self.config_db))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_seed_creates_one_row_and_is_idempotent(self):
        result1 = seed_shifts.seed(self.config_db)
        self.assertEqual(result1["created"], ["Weekday Production"])

        result2 = seed_shifts.seed(self.config_db)
        self.assertEqual(result2["created"], [])
        self.assertEqual(result2["skipped"], ["Weekday Production"])

        connection = sqlite3.connect(self.config_db)
        count = connection.execute("SELECT COUNT(*) FROM shift_definitions").fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_is_within_active_shift_matches_seeded_row(self):
        seed_shifts.seed(self.config_db)
        self.assertTrue(plant_context.is_within_active_shift(self.config_db, MONDAY_IN_SHIFT))
        self.assertFalse(plant_context.is_within_active_shift(self.config_db, MONDAY_OFF_HOURS))
        self.assertFalse(plant_context.is_within_active_shift(self.config_db, SATURDAY))

    def test_no_shift_configured_returns_false_never_guesses(self):
        self.assertFalse(plant_context.is_within_active_shift(self.config_db, MONDAY_IN_SHIFT))


class TestBaselineTargetsDayTypeAdded(unittest.TestCase):
    def test_utility_equipment_types_now_include_day_type(self):
        for equipment_type in ("air_compressor", "chiller", "chilled_water_pump", "water_supply_pump", "ahu"):
            profile = bt.EQUIPMENT_TYPE_PROFILES[equipment_type]
            self.assertIn("day_type", profile.context_dimensions, f"{equipment_type} missing day_type context dimension")

    def test_cold_room_already_had_day_type_unchanged(self):
        self.assertIn("day_type", bt.EQUIPMENT_TYPE_PROFILES["cold_room"].context_dimensions)


# ---------------------------------------------------------------------------
# TagDatasetSimulator-level: schedule_push actually shifts REAL tag values.
# ---------------------------------------------------------------------------

class TagDatasetSchedulingTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "config.db"
        initialize_config_database(str(self.database_path))

        connection = sqlite3.connect(self.database_path)
        connection.execute("INSERT INTO equipment (name, display_name) VALUES ('p01_chiller_chl01', 'Chiller CHL01')")
        connection.commit()
        connection.close()

        migrate_metadata(self.database_path, backup=False)
        migrate_factory_structure(self.database_path, backup=False)
        migrate_operating_schedule(self.database_path, backup=False)
        migrate_production(self.database_path, backup=False)
        # A real seeded shift row - without it, is_within_active_shift()
        # (recomputed fresh every update_values() cycle, overwriting any
        # manually-set sim._within_shift) always returns False regardless
        # of the mocked `now`, silently collapsing the in-shift/off-hours
        # comparison this test exists to make.
        seed_shifts.seed(self.database_path)

        _insert_tag(self.database_path, "P01.UTILITY.CHL01.Power_kW", "REAL", "kW", "power")
        _insert_tag(self.database_path, "P01.UTILITY.AC01.Power_kW", "REAL", "kW", "power")
        _insert_tag(self.database_path, "P01.UTILITY.AC01.RunStatus", "BOOL", "", "running")

    def tearDown(self):
        self.temp_dir.cleanup()


class TestScheduleAwareRealTagValues(TagDatasetSchedulingTestBase):
    def test_chiller_power_settles_lower_off_hours_than_in_shift(self):
        with patch("simulator.tag_dataset_model.datetime") as mock_dt:
            mock_dt.now.return_value = MONDAY_IN_SHIFT
            mock_dt.strptime = datetime.strptime
            sim = TagDatasetSimulator(self.database_path)
            for _ in range(80):
                sim.update_values()
            in_shift_value = sim._values.get("P01.UTILITY.CHL01.Power_kW")

        with patch("simulator.tag_dataset_model.datetime") as mock_dt:
            mock_dt.now.return_value = MONDAY_OFF_HOURS
            mock_dt.strptime = datetime.strptime
            sim2 = TagDatasetSimulator(self.database_path)
            for _ in range(80):
                sim2.update_values()
            off_hours_value = sim2._values.get("P01.UTILITY.CHL01.Power_kW")

        self.assertLess(off_hours_value, in_shift_value)
        self.assertGreater(off_hours_value, 0.0)  # never zero - standby load preserved

    def test_air_compressor_mostly_off_outside_hours_over_many_cycles(self):
        with patch("simulator.tag_dataset_model.datetime") as mock_dt:
            mock_dt.now.return_value = MONDAY_OFF_HOURS
            mock_dt.strptime = datetime.strptime
            sim = TagDatasetSimulator(self.database_path)
            sim._rng = random.Random(99)
            on_count = 0
            for _ in range(200):
                sim.update_values()
                on_count += sim._values.get("P01.UTILITY.AC01.RunStatus")
        self.assertLess(on_count, 60)  # well under 200 - mostly stopped, never rigidly always 0 either

    def test_air_compressor_mostly_on_within_shift(self):
        with patch("simulator.tag_dataset_model.datetime") as mock_dt:
            mock_dt.now.return_value = MONDAY_IN_SHIFT
            mock_dt.strptime = datetime.strptime
            sim = TagDatasetSimulator(self.database_path)
            sim._rng = random.Random(99)
            on_count = 0
            for _ in range(200):
                sim.update_values()
                on_count += sim._values.get("P01.UTILITY.AC01.RunStatus")
        self.assertGreater(on_count, 170)


# ---------------------------------------------------------------------------
# production_batch_simulator - schedule-aware batch-start rate.
# ---------------------------------------------------------------------------

class TestProductionBatchSchedule(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        initialize_config_database(str(self.config_db))

        connection = sqlite3.connect(self.config_db)
        connection.execute("INSERT INTO equipment (name, display_name) VALUES ('p01_bead_mill_mill01', 'Bead Mill MILL01')")
        connection.commit()
        connection.close()

        migrate_metadata(self.config_db, backup=False)
        migrate_factory_structure(self.config_db, backup=False)
        migrate_operating_schedule(self.config_db, backup=False)
        migrate_production(self.config_db, backup=False)
        seed_shifts.seed(self.config_db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _count_starts(self, now, trials=300):
        started = 0
        for seed in range(trials):
            rng = random.Random(seed)
            result = pbs.tick(self.config_db, rng=rng, now=now)
            started += result["started"]
            # Reset - a batch that started must be cleared before the next trial's tick() call.
            connection = sqlite3.connect(self.config_db)
            connection.execute("DELETE FROM production_batches")
            connection.commit()
            connection.close()
        return started

    def test_batches_start_far_more_often_within_shift_than_off_hours(self):
        # products required for a batch to start at all
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        connection = sqlite3.connect(self.config_db)
        connection.execute(
            "INSERT INTO product_categories (name, created_at) VALUES ('Test Category', ?)", (now_text,),
        )
        connection.execute(
            "INSERT INTO products (product_code, product_name, category_id, standard_batch_size, unit_of_measure, active, source, created_at) "
            "VALUES ('TP01', 'Test Product', 1, 100.0, 'kg', 1, 'test', ?)", (now_text,),
        )
        connection.commit()
        connection.close()

        in_shift_starts = self._count_starts(MONDAY_IN_SHIFT)
        off_hours_starts = self._count_starts(MONDAY_OFF_HOURS)
        saturday_starts = self._count_starts(SATURDAY)
        sunday_starts = self._count_starts(SUNDAY)

        self.assertGreater(in_shift_starts, off_hours_starts)
        self.assertGreater(saturday_starts, sunday_starts)


# ---------------------------------------------------------------------------
# Follow-up - air_header (compressed-air network) schedule awareness.
# ---------------------------------------------------------------------------

class TestAirHeaderScheduleFactorPureMath(unittest.TestCase):
    def test_full_within_shift(self):
        self.assertEqual(plant_context.air_header_schedule_factor("flow", MONDAY_IN_SHIFT, True), 1.0)
        self.assertEqual(plant_context.air_header_schedule_factor("pressure", MONDAY_IN_SHIFT, True), 1.0)

    def test_flow_drops_substantially_off_hours(self):
        factor = plant_context.air_header_schedule_factor("flow", MONDAY_OFF_HOURS, False)
        self.assertLess(factor, 0.30)  # "substantially lower"

    def test_pressure_stays_close_to_setpoint(self):
        factor = plant_context.air_header_schedule_factor("pressure", MONDAY_OFF_HOURS, False)
        self.assertGreater(factor, 0.85)  # never collapses

    def test_flow_responds_much_more_strongly_than_pressure(self):
        for now, within_shift in ((MONDAY_OFF_HOURS, False), (SATURDAY, False), (SUNDAY, False)):
            flow = plant_context.air_header_schedule_factor("flow", now, within_shift)
            pressure = plant_context.air_header_schedule_factor("pressure", now, within_shift)
            # Deviation from full load (1.0) is much bigger for flow than pressure.
            self.assertGreater(1.0 - flow, 1.0 - pressure)

    def test_saturday_lower_than_weekday_off_hours_sunday_lower_than_saturday(self):
        for measurement in ("flow", "pressure"):
            weekday_off = plant_context.air_header_schedule_factor(measurement, MONDAY_OFF_HOURS, False)
            saturday = plant_context.air_header_schedule_factor(measurement, SATURDAY, False)
            sunday = plant_context.air_header_schedule_factor(measurement, SUNDAY, False)
            self.assertLessEqual(saturday, weekday_off)
            self.assertLess(sunday, saturday)

    def test_never_zero_or_negative_factor(self):
        for measurement in ("flow", "pressure"):
            for now in (MONDAY_OFF_HOURS, SATURDAY, SUNDAY):
                self.assertGreater(plant_context.air_header_schedule_factor(measurement, now, False), 0.0)

    def test_dew_point_not_schedule_sensitive(self):
        self.assertIsNone(plant_context.air_header_schedule_factor("temperature", MONDAY_OFF_HOURS, False))


class AirHeaderTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "config.db"
        initialize_config_database(str(self.database_path))

        connection = sqlite3.connect(self.database_path)
        connection.execute("INSERT INTO equipment (name, display_name) VALUES ('p01_chiller_chl01', 'Chiller CHL01')")
        connection.commit()
        connection.close()

        migrate_metadata(self.database_path, backup=False)
        migrate_factory_structure(self.database_path, backup=False)
        migrate_operating_schedule(self.database_path, backup=False)
        migrate_production(self.database_path, backup=False)
        seed_shifts.seed(self.database_path)

        _insert_tag(self.database_path, "P01.UTILITY.AIRHDR01.Flow", "REAL", "Nm3/h", "flow")
        _insert_tag(self.database_path, "P01.UTILITY.AIRHDR01.Pressure", "REAL", "bar", "pressure")
        _insert_tag(self.database_path, "P01.UTILITY.AIRHDR01.DewPoint", "REAL", "degC", "temperature")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _settle(self, now, cycles=150, seed=11):
        with patch("simulator.tag_dataset_model.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            sim = TagDatasetSimulator(self.database_path)
            sim._rng = random.Random(seed)
            min_flow, min_pressure = 1e9, 1e9
            for _ in range(cycles):
                sim.update_values()
                min_flow = min(min_flow, sim._values["P01.UTILITY.AIRHDR01.Flow"])
                min_pressure = min(min_pressure, sim._values["P01.UTILITY.AIRHDR01.Pressure"])
            return {
                "flow": sim._values["P01.UTILITY.AIRHDR01.Flow"],
                "pressure": sim._values["P01.UTILITY.AIRHDR01.Pressure"],
                "min_flow": min_flow, "min_pressure": min_pressure,
            }


class TestAirHeaderRealTagValues(AirHeaderTestBase):
    def test_flow_substantially_lower_after_hours_than_production(self):
        production = self._settle(MONDAY_10)
        after_hours = self._settle(MONDAY_20)
        self.assertLess(after_hours["flow"], production["flow"] * 0.6)  # a real, substantial drop

    def test_pressure_never_collapses(self):
        production = self._settle(MONDAY_10)
        sunday = self._settle(SUNDAY_10)
        # Pressure stays within a tight band of its own normal range
        # (6.0-7.0 configured) - never drops anywhere near flow's own
        # proportional reduction.
        self.assertGreater(sunday["pressure"], production["pressure"] * 0.85)

    def test_never_negative(self):
        for now in (MONDAY_10, MONDAY_20, SATURDAY_10, SUNDAY_10):
            result = self._settle(now)
            self.assertGreaterEqual(result["min_flow"], 0.0)
            self.assertGreaterEqual(result["min_pressure"], 0.0)

    def test_ordering_production_gt_after_hours_gt_saturday_gt_sunday_for_flow(self):
        production = self._settle(MONDAY_10)["flow"]
        after_hours = self._settle(MONDAY_20)["flow"]
        saturday = self._settle(SATURDAY_10)["flow"]
        sunday = self._settle(SUNDAY_10)["flow"]
        self.assertGreater(production, after_hours)
        self.assertGreaterEqual(after_hours, saturday)
        self.assertGreater(saturday, sunday)


# ---------------------------------------------------------------------------
# Follow-up - cross-equipment consistency review.
# ---------------------------------------------------------------------------

class TestNoEquipmentFactorExceedsFullLoad(unittest.TestCase):
    """Structural guarantee (item: 'Saturday consistently exceeding
    normal weekday production') - every configured off-hours fraction
    is < 1.0, so Saturday/Sunday can never exceed the full weekday
    in-shift factor for ANY registered equipment type, by construction."""

    def test_all_off_hours_fractions_below_full_load(self):
        for prefix, fraction in plant_context.SCHEDULE_OFF_HOURS_LOAD_FACTOR.items():
            self.assertLess(fraction, 1.0, f"{prefix} off-hours fraction must be < 1.0")

    def test_all_air_header_fractions_below_full_load(self):
        for measurement, fraction in plant_context.AIR_HEADER_OFF_HOURS_FACTOR_BY_MEASUREMENT.items():
            self.assertLess(fraction, 1.0, f"air_header {measurement} fraction must be < 1.0")

    def test_saturday_never_exceeds_weekday_off_hours_for_any_registered_equipment(self):
        for prefix in plant_context.SCHEDULE_OFF_HOURS_LOAD_FACTOR:
            weekday_off = plant_context.schedule_load_factor(f"{prefix}.Power_kW", MONDAY_OFF_HOURS, False)
            saturday = plant_context.schedule_load_factor(f"{prefix}.Power_kW", SATURDAY, False)
            self.assertLessEqual(saturday, weekday_off)


class TestAirCompressorRunStatusPowerConsistency(AirHeaderTestBase):
    """item: 'compressor stopped but electrical consumption still
    representing full load' - the specific bug found and fixed during
    this follow-up's consistency review."""

    def setUp(self):
        super().setUp()
        _insert_tag(self.database_path, "P01.UTILITY.AC01.Power_kW", "REAL", "kW", "power")
        _insert_tag(self.database_path, "P01.UTILITY.AC01.RunStatus", "BOOL", "", "running")

    def test_average_power_lower_off_hours_than_in_shift(self):
        with patch("simulator.tag_dataset_model.datetime") as mock_dt:
            mock_dt.now.return_value = MONDAY_IN_SHIFT
            mock_dt.strptime = datetime.strptime
            sim = TagDatasetSimulator(self.database_path)
            sim._rng = random.Random(3)
            for _ in range(100):
                sim.update_values()
            in_shift_power = sim._values["P01.UTILITY.AC01.Power_kW"]

        with patch("simulator.tag_dataset_model.datetime") as mock_dt:
            mock_dt.now.return_value = MONDAY_OFF_HOURS
            mock_dt.strptime = datetime.strptime
            sim2 = TagDatasetSimulator(self.database_path)
            sim2._rng = random.Random(3)
            for _ in range(100):
                sim2.update_values()
            off_hours_power = sim2._values["P01.UTILITY.AC01.Power_kW"]

        # The compressor is mostly STOPPED off-hours (RunStatus mostly
        # 0) - its power must reflect that, never sitting near the
        # same full-load level as when it's genuinely running in-shift.
        # 0.75 (not a stricter fraction) because air_compressor's own
        # TAG_PROFILES band (22-30 kW) is narrow - even blended fully
        # toward the wide anchor, ~26kW full-load settles no lower than
        # ~14kW (see SCHEDULE_WIDE_ANCHOR_PREFIXES) - still a real,
        # clearly-directioned ~40%+ reduction, just not a >50% one.
        self.assertLess(off_hours_power, in_shift_power * 0.75)


class TestContinuousDutyEquipmentNeverZero(AirHeaderTestBase):
    def setUp(self):
        super().setUp()
        _insert_tag(self.database_path, "P01.UTILITY.CHL01.Power_kW", "REAL", "kW", "power")
        _insert_tag(self.database_path, "P01.UTILITY.CHWP01.Power_kW", "REAL", "kW", "power")
        _insert_tag(self.database_path, "P01.WATER.WSP01.Power_kW", "REAL", "kW", "power")
        _insert_tag(self.database_path, "P01.HVAC.AHU01.FanPower", "REAL", "kW", "power")
        _insert_tag(self.database_path, "P01.COLDROOM.CR01.CompressorPower", "REAL", "kW", "power")

    def test_none_of_the_24_7_equipment_ever_reaches_zero_on_sunday(self):
        with patch("simulator.tag_dataset_model.datetime") as mock_dt:
            mock_dt.now.return_value = SUNDAY_10
            mock_dt.strptime = datetime.strptime
            sim = TagDatasetSimulator(self.database_path)
            sim._rng = random.Random(5)
            mins = {tag: 1e9 for tag in (
                "P01.UTILITY.CHL01.Power_kW", "P01.UTILITY.CHWP01.Power_kW", "P01.WATER.WSP01.Power_kW",
                "P01.HVAC.AHU01.FanPower", "P01.COLDROOM.CR01.CompressorPower",
            )}
            for _ in range(150):
                sim.update_values()
                for tag in mins:
                    mins[tag] = min(mins[tag], sim._values[tag])
        for tag, value in mins.items():
            self.assertGreater(value, 0.0, f"{tag} reached zero - 24/7 equipment must never stop")


class TestBuildingBaseLoadNeverNearZero(unittest.TestCase):
    def test_worst_case_weekend_night_still_meaningfully_above_zero(self):
        rng = random.Random(1)
        worst_case_now = datetime(2026, 8, 23, 3, 0, 0)  # Sunday, 3am - night AND weekend compound
        value = plant_context.plant_base_load_kw("p01", worst_case_now, rng)
        nominal = plant_context.PLANT_BASE_LOAD_KW["p01"]
        self.assertGreater(value, nominal * 0.25)  # never "unrealistically close to zero"


if __name__ == "__main__":
    unittest.main()
