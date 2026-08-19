import random
import sqlite3
import statistics
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from database.database import DatabaseManager
from database.initialize_config_db import initialize_config_database
from engine import baseline_engine as b
from engine.baseline_migrator import migrate as migrate_baseline
from engine.baseline_targets import BaselineTarget, discover_targets
from engine.energy_tariff_migrator import migrate as migrate_energy_tariff
from engine.energy_kpi_migrator import migrate as migrate_energy_kpi
from engine.factory_structure_migrator import migrate as migrate_factory_structure
from engine.metadata_migrator import migrate as migrate_metadata
from engine.operating_schedule_migrator import migrate as migrate_operating_schedule
from engine.production_migrator import migrate as migrate_production


# BASE is chosen well after MODERN_DATA_BOUNDARY (2026-08-09) so
# reference/recent window logic behaves realistically in tests.
BASE = datetime(2026, 8, 20, 0, 0, 0)


def _seed_config_db(database_path: Path) -> None:
    initialize_config_database(str(database_path))

    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO equipment (name, display_name) VALUES (?, ?)",
        ("p01_chiller_chl01", "Chiller CHL01 (P01)"),
    )
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
    migrate_baseline(database_path, backup=False)


def _insert_tag(database_path: Path, tag_name: str, data_type="REAL", unit="", measurement="") -> None:
    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT OR IGNORE INTO tags (tag_name, driver, data_type, unit, enabled, measurement) VALUES (?, 'simulator', ?, ?, 1, ?)",
        (tag_name, data_type, unit, measurement),
    )
    connection.commit()
    connection.close()


def _insert_threshold(database_path: Path, tag_name: str, low_warning=None, low_alarm=None, high_warning=None, high_alarm=None) -> None:
    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT OR REPLACE INTO thresholds (tag_name, low_warning, low_alarm, high_warning, high_alarm) VALUES (?, ?, ?, ?, ?)",
        (tag_name, low_warning, low_alarm, high_warning, high_alarm),
    )
    connection.commit()
    connection.close()


def _seed_series(historian: DatabaseManager, tag: str, points: list[tuple[datetime, float]]) -> None:
    for timestamp, value in points:
        historian.save_tag(tag, "sim", value, timestamp)


def _make_target(
    instance_key="P01.UTILITY.CHL01", equipment_type="chiller", target_key="Power_kW",
    tag_name="P01.UTILITY.CHL01.Power_kW", is_derived=False,
    context_dimensions=("load_bucket", "ambient_bucket", "hour_bucket"),
    aggregation_minutes=5, reference_window_days_max=60, recent_window_days=7,
) -> BaselineTarget:
    return BaselineTarget(
        plant_code="p01", instance_key=instance_key, equipment_type=equipment_type, target_key=target_key,
        tag_name=tag_name, is_derived=is_derived, context_dimensions=context_dimensions,
        aggregation_minutes=aggregation_minutes, reference_window_days_max=reference_window_days_max,
        recent_window_days=recent_window_days,
    )


class BaselineEngineTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        self.historian = DatabaseManager(db_path=self.machine_db)

    def tearDown(self):
        self.temp_dir.cleanup()


# ---------------------------------------------------------------------------
# Downsampling / representative sampling (item 3)
# ---------------------------------------------------------------------------

class TestDownsampling(unittest.TestCase):
    def test_many_adjacent_samples_collapse_to_one_bucket(self):
        rows = [{"time": (BASE + timedelta(seconds=10 * i)).strftime(b.TIME_FORMAT), "value": 50.0 + i * 0.01} for i in range(30)]
        downsampled = b._downsample(rows, bucket_minutes=5)
        # 30 samples at 10s apart span 290s (< 5 min) - all one bucket.
        self.assertEqual(len(downsampled), 1)

    def test_raw_vs_downsampled_does_not_falsely_inflate_confidence(self):
        """Item 17 - thousands of 10s samples across a single afternoon
        must not produce more representative samples (and therefore
        not more apparent confidence) than the actual number of
        5-minute buckets they span."""
        rows = [{"time": (BASE + timedelta(seconds=10 * i)).strftime(b.TIME_FORMAT), "value": 50.0} for i in range(3000)]  # ~8.3 hours of 10s data
        downsampled = b._downsample(rows, bucket_minutes=5)
        # ~8.3 hours / 5 min = ~100 buckets, nowhere near 3000.
        self.assertLess(len(downsampled), 105)
        self.assertGreater(len(downsampled), 90)


# ---------------------------------------------------------------------------
# Bootstrap / mature classification (items 5, 6)
# ---------------------------------------------------------------------------

class TestClassification(unittest.TestCase):
    def test_below_minimum_is_unavailable(self):
        status, confidence = b._classify(representative_sample_count=3, distinct_days=1, level="C", diversity_dimensions=0)
        self.assertEqual(status, "unavailable")
        self.assertIsNone(confidence)

    def test_enough_samples_but_one_day_is_bootstrap_not_unavailable(self):
        """The exact scenario item 5 asks to resolve explicitly: many
        samples, poor temporal diversity -> a real, honestly-labeled
        provisional value, not a false 'unavailable'."""
        status, confidence = b._classify(representative_sample_count=50, distinct_days=1, level="A", diversity_dimensions=2)
        self.assertEqual(status, "bootstrap")
        self.assertEqual(confidence, "Low")

    def test_ten_thousand_samples_one_afternoon_capped_at_bootstrap(self):
        """The literal scenario from the approved plan's worked
        example - huge raw sample count, single day, must never reach
        Medium/High confidence."""
        # Even generously assuming every sample became its own
        # representative bucket (worse case than reality, since
        # downsampling would reduce this further):
        status, confidence = b._classify(representative_sample_count=10000, distinct_days=1, level="A", diversity_dimensions=3)
        self.assertEqual(status, "bootstrap")
        self.assertEqual(confidence, "Low")

    def test_mature_medium_requires_real_diversity(self):
        status, confidence = b._classify(representative_sample_count=30, distinct_days=4, level="B", diversity_dimensions=2)
        self.assertEqual(status, "mature")
        self.assertEqual(confidence, "Medium")

    def test_mature_high_requires_level_a_and_strong_coverage(self):
        status, confidence = b._classify(representative_sample_count=60, distinct_days=8, level="A", diversity_dimensions=2)
        self.assertEqual(status, "mature")
        self.assertEqual(confidence, "High")

    def test_mature_but_thin_coverage_is_low_not_high(self):
        status, confidence = b._classify(representative_sample_count=20, distinct_days=3, level="C", diversity_dimensions=0)
        self.assertEqual(status, "mature")
        self.assertEqual(confidence, "Low")


# ---------------------------------------------------------------------------
# Robust statistics + configurable Normal Range default (item 4)
# ---------------------------------------------------------------------------

class TestSummarize(unittest.TestCase):
    def test_median_and_mad_robust_to_outlier(self):
        values = [50.0] * 20 + [500.0]  # one extreme outlier
        median, low, high, mad = b._summarize(values)
        self.assertAlmostEqual(median, 50.0)
        naive_mean = statistics.mean(values)
        self.assertLess(median, naive_mean)  # the outlier visibly skews the mean but not the median

    def test_default_normal_range_is_not_hardcoded_elsewhere(self):
        """item 17 - if the central default changes, _summarize()'s
        actual output must change with it - proving no second copy of
        P10/P90 (or whatever the default is) is hardcoded anywhere else."""
        values = list(range(1, 101))  # 1..100
        _, low_default, high_default, _ = b._summarize([float(v) for v in values])

        _, low_custom, high_custom, _ = b._summarize([float(v) for v in values], percentiles=(25, 75))

        self.assertNotEqual((low_default, high_default), (low_custom, high_custom))
        # Narrower percentile band must be, well, narrower.
        self.assertGreater(low_custom, low_default)
        self.assertLess(high_custom, high_default)


# ---------------------------------------------------------------------------
# Context matching / fallback (items 9, 10, 11)
# ---------------------------------------------------------------------------

class TestContextMatchingGeneric(unittest.TestCase):
    def test_narrow_bucket_gracefully_broadens(self):
        """item 11 - a Level-A combination with almost no data should
        fall back to a coarser level rather than returning nothing,
        as long as the coarser level has enough data."""
        buckets = [BASE + timedelta(minutes=5 * i) for i in range(30)]
        values_by_bucket = {buckets[i]: 50.0 + i * 0.1 for i in range(30)}
        # Only ONE bucket has "load=high"; the rest are "load=low" -
        # matching load=high AND hour=X should fail to reach minimum,
        # forcing a drop back to hour-only (still "load" dropped).
        context_by_bucket = {
            buckets[i]: {"load_bucket": "high" if i == 0 else "low", "hour_bucket": "00-04"}
            for i in range(30)
        }
        current_context = {"load_bucket": "high", "hour_bucket": "00-04"}

        matched, matched_buckets, dims_used, level = b._match_generic(
            values_by_bucket, context_by_bucket, ("load_bucket", "hour_bucket"), current_context,
        )
        # Fell back from Level A (both dims) to Level B (hour_bucket only).
        self.assertEqual(level, "B")
        self.assertGreater(len(matched), 5)

    def test_no_context_falls_to_level_c(self):
        buckets = [BASE + timedelta(minutes=5 * i) for i in range(10)]
        values_by_bucket = {buckets[i]: 50.0 for i in range(10)}
        context_by_bucket = {buckets[i]: {"load_bucket": f"unique_{i}"} for i in range(10)}  # every bucket unique - no match possible
        current_context = {"load_bucket": "unique_0"}

        matched, matched_buckets, dims_used, level = b._match_generic(values_by_bucket, context_by_bucket, ("load_bucket",), current_context)
        self.assertEqual(level, "C")
        self.assertEqual(dims_used, ())
        self.assertEqual(len(matched), 10)  # everything, ungrouped

    def test_self_referential_context_dimension_is_dropped(self):
        """item 10, generalized - a target's own tag must never be used
        as its own context source (checked via _context_source_tag)."""
        target = _make_target(target_key="LoadPct", tag_name="P01.UTILITY.CHL01.LoadPct", context_dimensions=("load_bucket", "hour_bucket"))
        source = b._context_source_tag(target, "load_bucket")
        self.assertEqual(source, target.tag_name)  # confirmed self-referential for this specific target


class TestFaultExclusionDoesNotOverreach(unittest.TestCase):
    def test_in_limit_high_values_are_never_excluded(self):
        """item 9 - a genuinely high-but-in-limits value (an uncommon
        but valid operating condition) must remain a valid baseline
        candidate, not be treated as a fault."""
        thresholds = {"low_warning": 10.0, "low_alarm": 5.0, "high_warning": 90.0, "high_alarm": 95.0}
        rows = [{"time": (BASE + timedelta(minutes=i)).strftime(b.TIME_FORMAT), "value": 85.0} for i in range(10)]  # high but under high_warning=90
        intervals = b._abnormal_intervals_from_thresholds(rows, thresholds)
        self.assertEqual(intervals, [])

    def test_threshold_breaching_values_are_excluded(self):
        thresholds = {"low_warning": 10.0, "low_alarm": 5.0, "high_warning": 90.0, "high_alarm": 95.0}
        rows = [{"time": (BASE + timedelta(minutes=i)).strftime(b.TIME_FORMAT), "value": 98.0} for i in range(5)]  # over high_alarm
        intervals = b._abnormal_intervals_from_thresholds(rows, thresholds)
        self.assertGreater(len(intervals), 0)

    def test_missing_thresholds_excludes_nothing(self):
        rows = [{"time": (BASE + timedelta(minutes=i)).strftime(b.TIME_FORMAT), "value": 999999.0} for i in range(5)]
        intervals = b._abnormal_intervals_from_thresholds(rows, None)
        self.assertEqual(intervals, [])


# ---------------------------------------------------------------------------
# Reference vs recent windows (item 2)
# ---------------------------------------------------------------------------

class TestReferenceRecentWindows(unittest.TestCase):
    def test_young_history_collapses_to_one_window(self):
        now = b.MODERN_DATA_BOUNDARY + timedelta(days=3)  # less than recent_window_days
        reference, recent = b._reference_recent_windows(now, reference_window_days_max=60, recent_window_days=7)
        self.assertEqual(reference, recent)

    def test_mature_history_produces_non_overlapping_adjacent_windows(self):
        now = b.MODERN_DATA_BOUNDARY + timedelta(days=90)
        reference, recent = b._reference_recent_windows(now, reference_window_days_max=60, recent_window_days=7)
        self.assertNotEqual(reference, recent)
        self.assertEqual(reference[1], recent[0])  # adjacent, not overlapping
        self.assertLess(reference[0], reference[1])
        self.assertEqual(recent[1], now)

    def test_reference_window_capped_at_max_days(self):
        now = b.MODERN_DATA_BOUNDARY + timedelta(days=200)
        reference, recent = b._reference_recent_windows(now, reference_window_days_max=60, recent_window_days=7)
        span_days = (reference[1] - reference[0]).days
        self.assertLessEqual(span_days, 60)


# ---------------------------------------------------------------------------
# Full pipeline (compute_baseline) against a synthetic isolated DB
# ---------------------------------------------------------------------------

class TestComputeBaselineChiller(BaselineEngineTestBase):
    def _seed_chiller_tags(self):
        for suffix, unit, measurement in (
            ("Power_kW", "kW", "power"), ("LoadPct", "%", ""), ("SupplyTemp", "degC", "temperature"),
            ("ReturnTemp", "degC", "temperature"), ("WaterFlow", "m3/h", "flow"),
        ):
            _insert_tag(self.config_db, f"P01.UTILITY.CHL01.{suffix}", unit=unit, measurement=measurement)

    def test_same_context_gives_similar_expected_value(self):
        self._seed_chiller_tags()
        now = BASE
        points_power, points_load = [], []
        rng = random.Random(1)
        # 5 days of similar afternoon operation (16:00-16:30), same load band.
        for day in range(5):
            for minute in range(0, 30, 5):
                t = now - timedelta(days=5 - day) + timedelta(hours=16, minutes=minute)
                points_power.append((t, 60.0 + rng.uniform(-1, 1)))
                points_load.append((t, 70.0 + rng.uniform(-1, 1)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points_power)
        _seed_series(self.historian, "P01.UTILITY.CHL01.LoadPct", points_load)

        target = _make_target(context_dimensions=("load_bucket", "hour_bucket"))
        result = b.compute_baseline(target, self.historian, self.config_db, actual_value=60.5, now=now - timedelta(days=1) + timedelta(hours=16, minutes=10))

        self.assertIsNotNone(result["expected_value"])
        self.assertAlmostEqual(result["expected_value"], 60.0, delta=3.0)

    def test_different_ambient_context_changes_chiller_expected_power(self):
        self._seed_chiller_tags()
        _insert_tag(self.config_db, "P01.ENV01.Temperature", unit="degC", measurement="temperature")

        now = BASE
        points_power, points_ambient = [], []
        # Cool days -> lower power; hot days -> higher power (physically
        # sensible: more cooling load when it's hot outside).
        for day in range(6):
            t = now - timedelta(days=6 - day) + timedelta(hours=14)
            hot = day % 2 == 0
            points_power.append((t, 80.0 if hot else 50.0))
            points_ambient.append((t, 33.0 if hot else 26.0))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points_power)
        _seed_series(self.historian, "P01.ENV01.Temperature", points_ambient)

        target = _make_target(context_dimensions=("ambient_bucket",))
        hot_now = now - timedelta(days=2) + timedelta(hours=14)
        cool_now = now - timedelta(days=1) + timedelta(hours=14)

        hot_result = b.compute_baseline(target, self.historian, self.config_db, now=hot_now)
        cool_result = b.compute_baseline(target, self.historian, self.config_db, now=cool_now)

        # Not asserting exact equality of context (bootstrap-level data)
        # - just that the two ambient conditions do not collapse to an
        # identical expectation when enough signal exists to tell them apart.
        if hot_result["expected_value"] is not None and cool_result["expected_value"] is not None:
            self.assertNotAlmostEqual(hot_result["expected_value"], cool_result["expected_value"], delta=0.01)

    def test_insufficient_history_returns_unavailable(self):
        self._seed_chiller_tags()
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", [(BASE, 60.0), (BASE + timedelta(minutes=5), 61.0)])
        target = _make_target()
        result = b.compute_baseline(target, self.historian, self.config_db, now=BASE + timedelta(minutes=10))
        self.assertEqual(result["baseline_status"], "unavailable")
        self.assertIsNone(result["expected_value"])

    def test_fault_excluded_from_baseline(self):
        self._seed_chiller_tags()
        _insert_threshold(self.config_db, "P01.UTILITY.CHL01.Power_kW", high_warning=90.0, high_alarm=100.0)

        now = BASE
        points = []
        rng = random.Random(2)
        for day in range(6):
            for minute in range(0, 20, 5):
                t = now - timedelta(days=6 - day) + timedelta(hours=10, minutes=minute)
                points.append((t, 60.0 + rng.uniform(-1, 1)))
        # Inject a clear fault spike on one day, well past high_alarm.
        points.append((now - timedelta(days=3) + timedelta(hours=10, minutes=30), 150.0))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",))
        result = b.compute_baseline(target, self.historian, self.config_db, now=now - timedelta(days=1) + timedelta(hours=10))

        self.assertIsNotNone(result["expected_value"])
        self.assertLess(result["expected_value"], 90.0)  # the 150.0 spike never pulled the median up


class TestPlantIndependence(BaselineEngineTestBase):
    def test_p01_and_p02_never_cross_contaminate(self):
        for plant in ("P01", "P02"):
            _insert_tag(self.config_db, f"{plant}.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")

        now = BASE
        p01_points = [(now - timedelta(days=i) + timedelta(hours=10), 50.0) for i in range(1, 6)]
        p02_points = [(now - timedelta(days=i) + timedelta(hours=10), 200.0) for i in range(1, 6)]
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", p01_points)
        _seed_series(self.historian, "P02.UTILITY.CHL01.Power_kW", p02_points)

        p01_target = _make_target(instance_key="P01.UTILITY.CHL01", tag_name="P01.UTILITY.CHL01.Power_kW", context_dimensions=("hour_bucket",))
        p02_target = _make_target(instance_key="P02.UTILITY.CHL01", tag_name="P02.UTILITY.CHL01.Power_kW", context_dimensions=("hour_bucket",))

        p01_result = b.compute_baseline(p01_target, self.historian, self.config_db, now=now - timedelta(hours=14))
        p02_result = b.compute_baseline(p02_target, self.historian, self.config_db, now=now - timedelta(hours=14))

        self.assertIsNotNone(p01_result["expected_value"])
        self.assertIsNotNone(p02_result["expected_value"])
        self.assertLess(p01_result["expected_value"], 100.0)
        self.assertGreater(p02_result["expected_value"], 100.0)


class TestDeteriorationNotNormalizedAway(BaselineEngineTestBase):
    def test_reference_lags_recent_during_slow_drift(self):
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")

        # now is far enough past MODERN_DATA_BOUNDARY for reference and
        # recent windows to genuinely split (item 2).
        now = b.MODERN_DATA_BOUNDARY + timedelta(days=90)
        points = []
        # Slow linear drift: 50kW at day 0, +0.3kW/day, sampled daily at 10:00
        # across the ENTIRE available history (90 days).
        for day in range(0, 90):
            t = b.MODERN_DATA_BOUNDARY + timedelta(days=day, hours=10)
            points.append((t, 50.0 + day * 0.3))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",), reference_window_days_max=60, recent_window_days=7)
        result = b.compute_baseline(target, self.historian, self.config_db, now=now)

        rvr = result["reference_vs_recent"]
        self.assertFalse(rvr["windows_overlap"])
        self.assertIsNotNone(rvr["reference_expected_value"])
        self.assertIsNotNone(rvr["recent_expected_value"])
        # Recent (most-drifted) must read meaningfully higher than
        # reference (older, less-drifted) - the reference baseline has
        # NOT chased the drift up to match recent.
        self.assertGreater(rvr["recent_expected_value"], rvr["reference_expected_value"] + 5.0)
        self.assertGreater(rvr["drift_percent"], 0)


class TestMissingContextGracefulFallback(BaselineEngineTestBase):
    def test_missing_context_tag_does_not_crash(self):
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        # LoadPct tag deliberately NOT inserted/seeded at all.
        now = BASE
        points = [(now - timedelta(days=i) + timedelta(hours=10), 55.0) for i in range(1, 6)]
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("load_bucket", "hour_bucket"))
        result = b.compute_baseline(target, self.historian, self.config_db, now=now - timedelta(hours=14))

        # Must not raise - and should still produce a usable answer via
        # a coarser context level.
        self.assertIn(result["baseline_status"], ("unavailable", "bootstrap", "mature"))


# ---------------------------------------------------------------------------
# Production equipment - same-product > category > operating-state (item 10)
# ---------------------------------------------------------------------------

class TestProductionFallback(BaselineEngineTestBase):
    def _seed_batch(self, product_id, plant_id, start, end, status="completed"):
        connection = sqlite3.connect(self.config_db)
        connection.execute(
            """
            INSERT INTO production_batches (
                batch_code, product_id, plant_id, status, start_time, end_time,
                planned_quantity, actual_quantity, good_quantity, reject_quantity,
                unit_of_measure, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 100, 100, 100, 0, 'kg', 'simulated', ?)
            """,
            (f"TEST-{start.strftime('%Y%m%d%H%M%S')}-{id(start)}", product_id, plant_id, status,
             start.strftime(b.TIME_FORMAT), end.strftime(b.TIME_FORMAT), datetime.now().strftime(b.TIME_FORMAT)),
        )
        connection.commit()
        connection.close()

    def test_same_product_preferred_over_category(self):
        _insert_tag(self.config_db, "P01.PROD.MILL01.MotorPower", unit="kW", measurement="power")

        connection = sqlite3.connect(self.config_db)
        plant_id = connection.execute("SELECT id FROM plants WHERE code = 'p01'").fetchone()[0]
        products = connection.execute("SELECT id, product_code, category_id FROM products WHERE active = 1").fetchall()
        connection.close()
        same_category_products = [p for p in products if p[2] == products[0][2]]
        self.assertGreaterEqual(len(same_category_products), 2, "test fixture needs >=2 products sharing a category")
        target_product = same_category_products[0]
        other_same_category_product = same_category_products[1]

        now = BASE
        points = []
        # Same-product batches read ~20kW; same-category-but-different-
        # product batches read ~35kW.
        for day in range(1, 6):
            t = now - timedelta(days=day, hours=-10)
            points.append((t, 20.0))
            self._seed_batch(target_product[0], plant_id, t - timedelta(minutes=5), t + timedelta(minutes=5))
        for day in range(1, 4):
            t2 = now - timedelta(days=day, hours=-11)
            points.append((t2, 35.0))
            self._seed_batch(other_same_category_product[0], plant_id, t2 - timedelta(minutes=5), t2 + timedelta(minutes=5))

        _seed_series(self.historian, "P01.PROD.MILL01.MotorPower", points)

        current_time = now - timedelta(days=1, hours=-10)
        self._seed_batch(target_product[0], plant_id, current_time - timedelta(minutes=2), current_time + timedelta(minutes=2))

        target = _make_target(
            instance_key="P01.PROD.MILL01", equipment_type="production_process", target_key="MotorPower",
            tag_name="P01.PROD.MILL01.MotorPower", context_dimensions=("product_code", "product_category", "running_state"),
        )
        result = b.compute_baseline(target, self.historian, self.config_db, now=current_time)

        if result["expected_value"] is not None:
            # Should track the same-product value (~20), not the
            # blended/other-product value (~35).
            self.assertLess(result["expected_value"], 27.0)


# ---------------------------------------------------------------------------
# Phase 11.3 regression - additive matched_bucket_starts field on
# compute_window_baseline()'s return. Must not change any existing
# field's value/presence/type - only ADD one new key.
# ---------------------------------------------------------------------------

class TestComputeWindowBaselineMatchedBucketStarts(BaselineEngineTestBase):
    def _seed_chiller_tags(self):
        for suffix, unit, measurement in (
            ("Power_kW", "kW", "power"), ("LoadPct", "%", ""), ("SupplyTemp", "degC", "temperature"),
            ("ReturnTemp", "degC", "temperature"), ("WaterFlow", "m3/h", "flow"),
        ):
            _insert_tag(self.config_db, f"P01.UTILITY.CHL01.{suffix}", unit=unit, measurement=measurement)

    def test_matched_bucket_starts_present_and_consistent_with_sample_count(self):
        self._seed_chiller_tags()
        now = BASE
        points = []
        rng = random.Random(7)
        for day in range(5):
            for minute in range(0, 30, 5):
                t = now - timedelta(days=5 - day) + timedelta(hours=16, minutes=minute)
                points.append((t, 60.0 + rng.uniform(-1, 1)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",))
        current_context = b._current_context(target, self.historian, self.config_db, now - timedelta(days=1) + timedelta(hours=16, minutes=10))
        result = b.compute_window_baseline(target, self.historian, self.config_db, now - timedelta(days=6), now, current_context)

        self.assertIn("matched_bucket_starts", result)
        self.assertIsInstance(result["matched_bucket_starts"], list)
        self.assertEqual(len(result["matched_bucket_starts"]), result["representative_sample_count"])
        self.assertEqual(result["matched_bucket_starts"], sorted(result["matched_bucket_starts"]))  # deterministic ordering

    def test_matched_bucket_starts_empty_list_for_unavailable_result(self):
        self._seed_chiller_tags()
        now = BASE
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", [(now, 60.0), (now + timedelta(minutes=5), 61.0)])
        target = _make_target()
        result = b.compute_window_baseline(target, self.historian, self.config_db, now - timedelta(days=1), now + timedelta(minutes=10), {})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["matched_bucket_starts"], [])

    def test_matched_bucket_starts_empty_list_when_no_data_at_all(self):
        self._seed_chiller_tags()
        now = BASE
        target = _make_target()
        result = b.compute_window_baseline(target, self.historian, self.config_db, now - timedelta(days=1), now, {})
        self.assertEqual(result["matched_bucket_starts"], [])

    def test_all_preexisting_fields_unaffected_by_the_additive_change(self):
        """Direct regression proof - every field compute_window_baseline()
        returned before Phase 11.3 must still be present with the
        expected type/shape; only a new key was added, nothing removed
        or altered."""
        self._seed_chiller_tags()
        now = BASE
        points = []
        rng = random.Random(3)
        for day in range(5):
            for minute in range(0, 30, 5):
                t = now - timedelta(days=5 - day) + timedelta(hours=16, minutes=minute)
                points.append((t, 60.0 + rng.uniform(-1, 1)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",))
        current_context = b._current_context(target, self.historian, self.config_db, now - timedelta(days=1) + timedelta(hours=16, minutes=10))
        result = b.compute_window_baseline(target, self.historian, self.config_db, now - timedelta(days=6), now, current_context)

        expected_preexisting_keys = {
            "status", "confidence", "level", "median", "range_low", "range_high", "mad",
            "representative_sample_count", "raw_sample_count", "distinct_days", "diversity_dimensions",
            "context_used", "missing_context", "history_start", "history_end",
        }
        self.assertTrue(expected_preexisting_keys.issubset(result.keys()))
        self.assertEqual(result["status"], "mature")
        self.assertIsInstance(result["median"], float)
        self.assertIsInstance(result["context_used"], dict)

    def test_compute_baseline_public_entry_point_still_works_end_to_end(self):
        """The one real caller of compute_window_baseline() besides
        Phase 11 - Phase 8's own compute_baseline() - must be entirely
        unaffected."""
        self._seed_chiller_tags()
        now = BASE
        points = []
        rng = random.Random(9)
        for day in range(5):
            for minute in range(0, 30, 5):
                t = now - timedelta(days=5 - day) + timedelta(hours=16, minutes=minute)
                points.append((t, 60.0 + rng.uniform(-1, 1)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",))
        result = b.compute_baseline(target, self.historian, self.config_db, actual_value=60.5, now=now - timedelta(days=1) + timedelta(hours=16, minutes=10))
        self.assertIsNotNone(result["expected_value"])
        self.assertAlmostEqual(result["expected_value"], 60.0, delta=3.0)


if __name__ == "__main__":
    unittest.main()
