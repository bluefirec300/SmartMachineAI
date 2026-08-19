import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from database.database import DatabaseManager
from database.initialize_config_db import initialize_config_database
from engine import anomaly_engine as a
from engine import baseline_engine as b
from engine.anomaly_migrator import migrate as migrate_anomaly
from engine.anomaly_targets import get_rule
from engine.baseline_migrator import migrate as migrate_baseline
from engine.baseline_targets import BaselineTarget
from engine.energy_kpi_migrator import migrate as migrate_energy_kpi
from engine.energy_tariff_migrator import migrate as migrate_energy_tariff
from engine.factory_structure_migrator import migrate as migrate_factory_structure
from engine.maintenance_migrator import migrate as migrate_maintenance
from engine.metadata_migrator import migrate as migrate_metadata
from engine.operating_schedule_migrator import migrate as migrate_operating_schedule
from engine.production_migrator import migrate as migrate_production


# Well after MODERN_DATA_BOUNDARY so nothing in these tests is affected
# by the legacy-format exclusion window.
BASE = datetime(2026, 8, 20, 12, 0, 0)


def _seed_config_db(database_path: Path) -> None:
    initialize_config_database(str(database_path))

    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO equipment (name, display_name) VALUES (?, ?)",
        ("p01_chiller_chl01", "Chiller CHL01 (P01)"),
    )
    connection.execute(
        "INSERT INTO equipment (name, display_name) VALUES (?, ?)",
        ("p02_chiller_chl01", "Chiller CHL01 (P02)"),
    )
    connection.commit()
    connection.close()

    migrate_metadata(database_path, backup=False)
    migrate_factory_structure(database_path, backup=False)
    migrate_maintenance(database_path, backup=False)
    migrate_operating_schedule(database_path, backup=False)
    migrate_production(database_path, backup=False)
    migrate_energy_tariff(database_path, backup=False)
    migrate_energy_kpi(database_path, backup=False)
    migrate_baseline(database_path, backup=False)
    migrate_anomaly(database_path, backup=False)


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


def _insert_tariff(database_path: Path, plant_id: int | None, energy_rate: float, currency: str, effective_date: str, expiry_date: str | None = None) -> None:
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        INSERT INTO energy_tariffs (plant_id, mode, currency, energy_rate, is_simulated, effective_date, expiry_date, created_at)
        VALUES (?, 'simple', ?, ?, 1, ?, ?, ?)
        """,
        (plant_id, currency, energy_rate, effective_date, expiry_date, datetime.now().strftime(b.TIME_FORMAT)),
    )
    connection.commit()
    connection.close()


def _plant_id(database_path: Path, code: str) -> int:
    connection = sqlite3.connect(database_path)
    row = connection.execute("SELECT id FROM plants WHERE code = ?", (code,)).fetchone()
    connection.close()
    return row[0]


def _seed_series(historian: DatabaseManager, tag: str, points: list[tuple[datetime, float]]) -> None:
    for timestamp, value in points:
        historian.save_tag(tag, "sim", value, timestamp)


def _make_target(
    instance_key="P01.UTILITY.CHL01", equipment_type="chiller", target_key="Power_kW",
    tag_name="P01.UTILITY.CHL01.Power_kW", is_derived=False, context_dimensions=(),
    aggregation_minutes=5, reference_window_days_max=60, recent_window_days=7,
) -> BaselineTarget:
    return BaselineTarget(
        plant_code=instance_key.split(".")[0].lower(), instance_key=instance_key, equipment_type=equipment_type,
        target_key=target_key, tag_name=tag_name, is_derived=is_derived, context_dimensions=context_dimensions,
        aggregation_minutes=aggregation_minutes, reference_window_days_max=reference_window_days_max,
        recent_window_days=recent_window_days,
    )


def _insert_baseline_row(
    database_path: Path, plant_id: int, instance_key: str, target_key: str, equipment_type: str = "chiller",
    baseline_type: str = "recent", context_bucket_key: str = "__all__", baseline_level: str = "C",
    baseline_status: str = "mature", confidence: str = "High", median_value: float = 50.0,
    range_low: float = 40.0, range_high: float = 60.0, mad: float = 3.0,
    history_start: str = "2026-08-13 00:00:00", history_end: str = "2026-08-20 00:00:00",
    computed_at: str | None = None,
) -> None:
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        INSERT INTO baseline_context_summary (
            plant_id, instance_key, target_key, equipment_type, baseline_type, context_bucket_key,
            baseline_level, baseline_status, confidence, median_value, range_low, range_high, mad,
            representative_sample_count, raw_sample_count, distinct_days, diversity_dimensions,
            history_start, history_end, aggregation_minutes, context_json, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plant_id, instance_key, target_key, baseline_type, context_bucket_key) DO UPDATE SET
            baseline_status = excluded.baseline_status, confidence = excluded.confidence,
            median_value = excluded.median_value, range_low = excluded.range_low, range_high = excluded.range_high,
            mad = excluded.mad, history_start = excluded.history_start, history_end = excluded.history_end,
            computed_at = excluded.computed_at
        """,
        (
            plant_id, instance_key, target_key, equipment_type, baseline_type, context_bucket_key,
            baseline_level, baseline_status, confidence, median_value, range_low, range_high, mad,
            30, 30, 6, 1, history_start, history_end, 5,
            json.dumps({"context_used": {}, "missing_context": [], "assumptions": []}),
            computed_at or datetime.now().strftime(b.TIME_FORMAT),
        ),
    )
    connection.commit()
    connection.close()


def _seed_flat_series(historian: DatabaseManager, tag: str, now: datetime, value: float, n_buckets: int, aggregation_minutes: int) -> None:
    """One representative sample per completed bucket, for exactly the
    buckets evaluate_deviation_rule will look at."""
    buckets = a._recent_completed_buckets(now, aggregation_minutes, n_buckets)
    for bucket_start in buckets:
        historian.save_tag(tag, "sim", value, bucket_start + timedelta(minutes=1))


class AnomalyEngineTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        self.historian = DatabaseManager(db_path=self.machine_db)
        self.plant_id = _plant_id(self.config_db, "p01")
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.RunStatus", data_type="BOOL")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_running(self, now: datetime) -> None:
        self.historian.save_tag("P01.UTILITY.CHL01.RunStatus", "sim", 1, now - timedelta(minutes=1))


# ---------------------------------------------------------------------------
# Bucket helpers (item 4 / 22 - bucket-not-tick, timezone/day-boundary)
# ---------------------------------------------------------------------------

class TestBucketHelpers(unittest.TestCase):
    def test_last_completed_bucket_excludes_in_progress_bucket(self):
        now = datetime(2026, 8, 20, 14, 6, 0)  # inside the 14:05-14:10 bucket
        last = a.last_completed_bucket_start(now, 5)
        self.assertEqual(last, datetime(2026, 8, 20, 14, 0, 0))

    def test_re_evaluating_within_same_bucket_returns_identical_result(self):
        """item 4/'a' - a worker ticking every ~60s within one 5-minute
        bucket must see the SAME last-completed-bucket every tick, not
        advance persistence per tick."""
        first = a.last_completed_bucket_start(datetime(2026, 8, 20, 14, 6, 0), 5)
        second = a.last_completed_bucket_start(datetime(2026, 8, 20, 14, 8, 30), 5)
        third = a.last_completed_bucket_start(datetime(2026, 8, 20, 14, 9, 59), 5)
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_day_boundary_bucket_is_correct(self):
        now = datetime(2026, 8, 21, 0, 2, 0)
        last = a.last_completed_bucket_start(now, 5)
        self.assertEqual(last, datetime(2026, 8, 20, 23, 55, 0))

    def test_recent_completed_buckets_oldest_first_contiguous(self):
        now = datetime(2026, 8, 20, 14, 23, 0)
        buckets = a._recent_completed_buckets(now, 5, 4)
        self.assertEqual(len(buckets), 4)
        self.assertEqual(buckets, sorted(buckets))
        for i in range(1, len(buckets)):
            self.assertEqual(buckets[i] - buckets[i - 1], timedelta(minutes=5))
        self.assertEqual(buckets[-1], datetime(2026, 8, 20, 14, 15, 0))


# ---------------------------------------------------------------------------
# Deviation math / severity / classification (items 7, 10, 16)
# ---------------------------------------------------------------------------

class TestComputeDeviation(unittest.TestCase):
    def test_basic_deviation(self):
        absolute, percent, normalized = a.compute_deviation(actual=60.0, expected=50.0, mad=5.0)
        self.assertAlmostEqual(absolute, 10.0)
        self.assertAlmostEqual(percent, 20.0)
        self.assertAlmostEqual(normalized, 2.0)

    def test_zero_expected_does_not_crash(self):
        absolute, percent, normalized = a.compute_deviation(actual=5.0, expected=0.0, mad=1.0)
        self.assertAlmostEqual(absolute, 5.0)
        self.assertIsNone(percent)

    def test_zero_or_missing_mad_gives_no_normalized_deviation(self):
        _, _, normalized_zero = a.compute_deviation(actual=60.0, expected=50.0, mad=0.0)
        _, _, normalized_none = a.compute_deviation(actual=60.0, expected=50.0, mad=None)
        self.assertIsNone(normalized_zero)
        self.assertIsNone(normalized_none)


class TestClassifySeverity(unittest.TestCase):
    def test_never_returns_critical(self):
        """item 10 - pure statistics never escalate to CRITICAL, no
        matter how extreme."""
        rule = get_rule("chiller_power_deviation")
        severity = a.classify_severity(normalized_deviation=50.0, persistence_periods=999, rule=rule, engineering_status="alarm_exceeded")
        self.assertIn(severity, a.SEVERITY_LEVELS)
        self.assertNotEqual(severity, "CRITICAL")
        self.assertEqual(severity, "HIGH")  # HIGH is the actual ceiling

    def test_small_deviation_is_information_or_attention(self):
        rule = get_rule("chiller_power_deviation")
        severity = a.classify_severity(normalized_deviation=rule.deviation_threshold_mad, persistence_periods=1, rule=rule, engineering_status="not_exceeded")
        self.assertIn(severity, ("INFORMATION", "ATTENTION"))

    def test_engineering_limit_floor_bumps_to_warning_minimum(self):
        """item 7 - co-occurrence with an engineering-limit breach never
        drops severity below WARNING, even for an otherwise-mild finding."""
        rule = get_rule("chiller_power_deviation")
        mild_severity = a.classify_severity(normalized_deviation=rule.deviation_threshold_mad, persistence_periods=1, rule=rule, engineering_status="not_exceeded")
        bumped_severity = a.classify_severity(normalized_deviation=rule.deviation_threshold_mad, persistence_periods=1, rule=rule, engineering_status="alarm_exceeded")
        self.assertIn(mild_severity, ("INFORMATION", "ATTENTION"))
        self.assertEqual(a.SEVERITY_LEVELS.index(bumped_severity), max(a.SEVERITY_LEVELS.index(mild_severity), a.SEVERITY_LEVELS.index("WARNING")))


class TestEffectiveOpenThresholds(unittest.TestCase):
    def test_bootstrap_requires_stronger_evidence_than_mature(self):
        """item 5/16 - a bootstrap/Low-confidence baseline must demand a
        larger deviation AND more persistence than a mature one, never
        the same or weaker."""
        rule = get_rule("chiller_power_deviation")
        mature_threshold, mature_persistence = a._effective_open_thresholds(rule, "mature")
        bootstrap_threshold, bootstrap_persistence = a._effective_open_thresholds(rule, "bootstrap")
        self.assertEqual(mature_threshold, rule.deviation_threshold_mad)
        self.assertEqual(mature_persistence, rule.persistence_periods_open)
        self.assertGreater(bootstrap_threshold, mature_threshold)
        self.assertGreaterEqual(bootstrap_persistence, mature_persistence)


class TestClassifyBucket(unittest.TestCase):
    def test_within_normal_range_not_abnormal(self):
        rule = get_rule("chiller_power_deviation")
        result = a._classify_bucket(rule, actual=51.0, expected=50.0, mad=3.0, threshold_mad=rule.deviation_threshold_mad, range_low=40.0, range_high=60.0)
        self.assertFalse(result["is_abnormal"])
        self.assertTrue(result["is_within_resolve_band"])

    def test_large_high_deviation_is_abnormal(self):
        rule = get_rule("chiller_power_deviation")  # direction="high"
        result = a._classify_bucket(rule, actual=90.0, expected=50.0, mad=3.0, threshold_mad=rule.deviation_threshold_mad, range_low=40.0, range_high=60.0)
        self.assertTrue(result["is_abnormal"])
        self.assertFalse(result["is_within_resolve_band"])

    def test_direction_high_ignores_low_deviation(self):
        """A 'high'-direction rule (e.g. compressor power) must not flag
        an abnormally LOW reading - that's not the failure mode it watches."""
        rule = get_rule("chiller_power_deviation")
        result = a._classify_bucket(rule, actual=10.0, expected=50.0, mad=3.0, threshold_mad=rule.deviation_threshold_mad, range_low=40.0, range_high=60.0)
        self.assertFalse(result["is_abnormal"])

    def test_resolve_band_margin_widens_recovery_band(self):
        rule = get_rule("chiller_power_deviation")  # resolve_band_margin_pct=5.0
        # actual sits just past range_high (60) but within the widened margin.
        result = a._classify_bucket(rule, actual=60.5, expected=50.0, mad=3.0, threshold_mad=rule.deviation_threshold_mad, range_low=40.0, range_high=60.0)
        self.assertTrue(result["is_within_resolve_band"])

    def test_minimum_absolute_deviation_floor_prevents_percent_only_false_positive(self):
        """A tiny reading with a huge relative % swing must not trip the
        rule if the absolute change is below minimum_absolute_deviation."""
        rule = get_rule("chiller_power_deviation")  # minimum_absolute_deviation=5.0 kW
        result = a._classify_bucket(rule, actual=1.0, expected=0.5, mad=0.1, threshold_mad=rule.deviation_threshold_mad, range_low=0.3, range_high=0.7)
        self.assertFalse(result["is_abnormal"])  # 0.5kW absolute change < 5.0kW floor


# ---------------------------------------------------------------------------
# Engineering-limit cross-reference (item 7/19 - independent of statistics)
# ---------------------------------------------------------------------------

class TestEngineeringLimitStatus(AnomalyEngineTestBase):
    def test_not_configured_when_no_thresholds(self):
        status, event_id = a.engineering_limit_status(self.config_db, self.machine_db, "P01.UTILITY.CHL01.Power_kW", 55.0)
        self.assertEqual(status, "not_configured")
        self.assertIsNone(event_id)

    def test_not_exceeded_within_limits(self):
        _insert_threshold(self.config_db, "P01.UTILITY.CHL01.Power_kW", high_warning=90.0, high_alarm=100.0)
        status, _ = a.engineering_limit_status(self.config_db, self.machine_db, "P01.UTILITY.CHL01.Power_kW", 55.0)
        self.assertEqual(status, "not_exceeded")

    def test_alarm_exceeded_is_independent_of_baseline(self):
        """item 7/19 - engineering-limit status is computed purely from
        thresholds vs. the latest reading, with no reference to any
        baseline/statistical state whatsoever."""
        _insert_threshold(self.config_db, "P01.UTILITY.CHL01.Power_kW", high_warning=90.0, high_alarm=100.0)
        status, _ = a.engineering_limit_status(self.config_db, self.machine_db, "P01.UTILITY.CHL01.Power_kW", 150.0)
        self.assertEqual(status, "alarm_exceeded")


# ---------------------------------------------------------------------------
# Interval-integrated excess energy/cost (items 9, 10, 16, 18)
# ---------------------------------------------------------------------------

class TestBucketExcessEnergyCost(AnomalyEngineTestBase):
    def test_no_excess_when_actual_at_or_below_expected(self):
        kwh, cost, currency = a.bucket_excess_energy_cost(self.config_db, self.plant_id, actual_kw=40.0, expected_kw=50.0, bucket_start=BASE, aggregation_minutes=5)
        self.assertEqual(kwh, 0.0)
        self.assertIsNone(cost)

    def test_excess_energy_is_interval_integrated_not_extrapolated(self):
        """item 9 - excess_kw * (aggregation_minutes/60), NOT
        excess_kw * some arbitrary total duration."""
        kwh, _, _ = a.bucket_excess_energy_cost(self.config_db, self.plant_id, actual_kw=70.0, expected_kw=50.0, bucket_start=BASE, aggregation_minutes=5)
        self.assertAlmostEqual(kwh, 20.0 * (5 / 60.0))  # 1.667 kWh for this one 5-min bucket only

    def test_cost_unavailable_without_a_configured_tariff(self):
        """item 16 - never fabricate a rate; Unavailable stays Unavailable."""
        kwh, cost, currency = a.bucket_excess_energy_cost(self.config_db, self.plant_id, actual_kw=70.0, expected_kw=50.0, bucket_start=BASE, aggregation_minutes=5)
        self.assertGreater(kwh, 0.0)
        self.assertIsNone(cost)
        self.assertIsNone(currency)

    def test_cost_computed_from_the_tariff_effective_on_that_bucket_date(self):
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        kwh, cost, currency = a.bucket_excess_energy_cost(self.config_db, self.plant_id, actual_kw=70.0, expected_kw=50.0, bucket_start=BASE, aggregation_minutes=5)
        self.assertAlmostEqual(cost, round(kwh * 0.5, 4))
        self.assertEqual(currency, "MYR")

    def test_tariff_transition_uses_each_bucket_own_rate(self):
        """item 10 (approval clarification) - a tariff change mid-anomaly
        must use each interval's OWN applicable rate, not one rate for
        the whole span."""
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.40, currency="MYR", effective_date="2026-08-01", expiry_date="2026-08-20")
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.60, currency="MYR", effective_date="2026-08-20")
        before = BASE - timedelta(days=1)  # 2026-08-19 - old rate
        after = BASE  # 2026-08-20 - new rate
        _, cost_before, _ = a.bucket_excess_energy_cost(self.config_db, self.plant_id, actual_kw=70.0, expected_kw=50.0, bucket_start=before, aggregation_minutes=5)
        _, cost_after, _ = a.bucket_excess_energy_cost(self.config_db, self.plant_id, actual_kw=70.0, expected_kw=50.0, bucket_start=after, aggregation_minutes=5)
        self.assertLess(cost_before, cost_after)


# ---------------------------------------------------------------------------
# Operating-state gating (item 15)
# ---------------------------------------------------------------------------

class TestOperatingStateGating(AnomalyEngineTestBase):
    def test_stopped_equipment_is_skipped_not_flagged(self):
        rule = get_rule("chiller_power_deviation")
        target = _make_target()
        now = BASE
        self.historian.save_tag("P01.UTILITY.CHL01.RunStatus", "sim", 0, now - timedelta(minutes=1))
        result = a.evaluate_deviation_rule(rule, target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(result["action"], "skipped")

    def test_rule_with_no_operating_state_requirement_is_never_gated(self):
        rule = get_rule("plant_high_demand")  # applicable_operating_states=()
        self.assertEqual(rule.applicable_operating_states, ())
        ok, reason = a._operating_state_ok(rule, self.historian, "P01.ELEC.MAIN")
        self.assertTrue(ok)
        self.assertIsNone(reason)


# ---------------------------------------------------------------------------
# Full deviation-rule lifecycle (items 1-6, 8, 11, 20; clarifications a/b/f/i/j)
# ---------------------------------------------------------------------------

class TestDeviationRuleLifecycle(AnomalyEngineTestBase):
    def setUp(self):
        super().setUp()
        self.rule = get_rule("chiller_power_deviation")  # persistence_periods_open=3, persistence_periods_resolve=3
        self.target = _make_target()

    def _seed_baseline(self, **overrides):
        fields = dict(median_value=50.0, range_low=40.0, range_high=60.0, mad=3.0, baseline_status="mature", confidence="High")
        fields.update(overrides)
        _insert_baseline_row(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, **fields)

    def test_single_outlier_does_not_open(self):
        """item 1 - a single abnormal sample must never open an anomaly
        (persistence_periods_open=3 requires 3 consecutive)."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        buckets = a._recent_completed_buckets(now, 5, 4)
        # 3 normal buckets, then ONE abnormal (the most recent).
        for bucket_start in buckets[:-1]:
            self.historian.save_tag(self.target.tag_name, "sim", 51.0, bucket_start + timedelta(minutes=1))
        self.historian.save_tag(self.target.tag_name, "sim", 90.0, buckets[-1] + timedelta(minutes=1))

        result = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(result["action"], "candidate")
        self.assertEqual(a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key), None)

    def test_persistent_deviation_opens(self):
        """item 2 - 3 consecutive abnormal buckets opens a real row."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)

        result = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(result["action"], "opened")
        row = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "OPEN")
        self.assertEqual(row["open_persistence_periods"], 3)
        self.assertEqual(row["occurrence_count"], 1)

    def test_bucket_not_tick_reevaluating_same_bucket_is_idempotent(self):
        """clarification a/i - calling evaluate_deviation_rule multiple
        times within the same completed bucket (simulating a ~60s-tick
        worker re-checking a 5-min bucket) must not double-advance
        persistence or repeatedly rewrite the row."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)

        first = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(first["action"], "opened")
        row_after_open = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)

        # Re-evaluate a few minutes later, still inside the same completed bucket.
        second = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now + timedelta(minutes=2))
        self.assertEqual(second["action"], "unchanged")
        row_after_second = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)
        self.assertEqual(row_after_open["updated_at"], row_after_second["updated_at"])
        self.assertEqual(row_after_open["id"], row_after_second["id"])

    def test_restart_safety_candidate_persistence_reconstructed_identically(self):
        """clarification b - candidate persistence is recomputed fresh
        from real historian buckets every call (no in-memory counter),
        so a 'restart' (a brand-new call with no prior state) sees the
        exact same candidate progress as before."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        buckets = a._recent_completed_buckets(now, 5, 4)
        self.historian.save_tag(self.target.tag_name, "sim", 51.0, buckets[0] + timedelta(minutes=1))  # normal
        self.historian.save_tag(self.target.tag_name, "sim", 51.0, buckets[1] + timedelta(minutes=1))  # normal
        for bucket_start in buckets[2:]:
            self.historian.save_tag(self.target.tag_name, "sim", 90.0, bucket_start + timedelta(minutes=1))  # 2 abnormal, below the 3 required

        first = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        second = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(first["action"], "candidate")
        self.assertEqual(first["open_persistence"], second["open_persistence"])

    def test_sustained_recovery_required_to_resolve(self):
        """item 3 - resolving requires persistence_periods_resolve (3)
        CONSECUTIVE within-band buckets, not just one good reading."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)
        a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)

        # One good bucket only - not enough to resolve.
        now2 = now + timedelta(minutes=5)
        self._seed_running(now2)
        bucket2 = a.last_completed_bucket_start(now2, 5)
        self.historian.save_tag(self.target.tag_name, "sim", 51.0, bucket2 + timedelta(minutes=1))
        result2 = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now2)
        self.assertEqual(result2["action"], "updated")
        row_still_open = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)
        self.assertIsNotNone(row_still_open)

        # Two more good buckets -> 3 consecutive -> resolves.
        for i in range(2, 4):
            now_i = now + timedelta(minutes=5 * i)
            self._seed_running(now_i)
            bucket_i = a.last_completed_bucket_start(now_i, 5)
            self.historian.save_tag(self.target.tag_name, "sim", 51.0, bucket_i + timedelta(minutes=1))
            result_i = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now_i)

        self.assertEqual(result_i["action"], "resolved")
        self.assertIsNone(a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key))

    def test_no_flapping_isolated_recovery_sample_does_not_resolve(self):
        """item 4 - one recovered bucket surrounded by abnormal ones
        never resolves (walking backward from the latest bucket stops
        at the first non-qualifying one)."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        buckets = a._recent_completed_buckets(now, 5, 4)
        self.historian.save_tag(self.target.tag_name, "sim", 90.0, buckets[0] + timedelta(minutes=1))
        self.historian.save_tag(self.target.tag_name, "sim", 90.0, buckets[1] + timedelta(minutes=1))
        self.historian.save_tag(self.target.tag_name, "sim", 90.0, buckets[2] + timedelta(minutes=1))
        a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)

        # Next bucket recovers, but the very next isn't yet checked -
        # single-recovery must show "updated", never "resolved" (needs 3 in a row).
        now2 = now + timedelta(minutes=5)
        self._seed_running(now2)
        bucket2 = a.last_completed_bucket_start(now2, 5)
        self.historian.save_tag(self.target.tag_name, "sim", 51.0, bucket2 + timedelta(minutes=1))
        result2 = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now2)
        self.assertNotEqual(result2["action"], "resolved")

    def test_bootstrap_baseline_requires_stronger_evidence_than_mature(self):
        """item 5 - the exact same 3 abnormal buckets that OPEN against a
        mature baseline must NOT open (only reach 'candidate') against a
        bootstrap baseline, since persistence_periods_open is scaled by
        bootstrap_multiplier (1.75 -> ceil(3*1.75)=6)."""
        self._seed_baseline(baseline_status="bootstrap", confidence="Low")
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)

        result = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(result["action"], "candidate")

    def test_energy_relevant_utility_category_rule_still_gets_excess_cost(self):
        """Regression guard - energy_relevant is the field the plan
        (item 18) specifically designed to gate excess-cost, independent
        of category (chiller/compressor/pump power-deviation rules are
        category=UTILITY, not ENERGY, but are still energy_relevant=True
        and must still get a real excess-energy figure)."""
        self.assertEqual(self.rule.category, "UTILITY")
        self.assertTrue(self.rule.energy_relevant)
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)
        a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        row = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)
        self.assertIsNotNone(row["estimated_excess_energy_kwh"])
        self.assertGreater(row["estimated_excess_energy_kwh"], 0.0)
        self.assertIsNotNone(row["estimated_excess_cost"])

    def test_bootstrap_open_is_marked_provisional(self):
        self._seed_baseline(baseline_status="bootstrap", confidence="Low")
        now = BASE
        self._seed_running(now)
        threshold, persistence = a._effective_open_thresholds(self.rule, "bootstrap")
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, persistence, self.target.aggregation_minutes)

        result = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(result["action"], "opened")
        row = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)
        self.assertEqual(row["provisional"], 1)
        limitations = json.loads(row["data_limitations_json"])
        self.assertTrue(any("bootstrap" in item.lower() for item in limitations))

    def test_baseline_unavailable_skips_statistical_evaluation(self):
        """item 6 - no baseline row at all -> skipped, never fabricated."""
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)
        result = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(result["action"], "skipped")
        self.assertIn("baseline unavailable", result["reason"])

    def test_engineering_alarm_already_active_suppresses_new_finding(self):
        """item 11 policy B - a first-ever evaluation, no existing
        candidate, engineering alarm already active on this exact tag ->
        redundant statistical finding is suppressed, not opened."""
        self._seed_baseline()
        _insert_threshold(self.config_db, self.target.tag_name, high_warning=90.0, high_alarm=100.0)
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 150.0, 3, self.target.aggregation_minutes)  # over high_alarm

        result = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(result["action"], "suppressed")
        self.assertIsNone(a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key))

    def test_pre_existing_anomaly_remains_linked_when_limit_later_crossed(self):
        """item 11 policy A - an anomaly already OPEN before an
        engineering-limit breach stays the SAME row (annotated), never
        duplicated, once the limit is later crossed too."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)
        opened = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(opened["action"], "opened")
        original_id = opened["id"]

        # NOW configure a threshold that the already-elevated reading breaches.
        _insert_threshold(self.config_db, self.target.tag_name, high_warning=80.0, high_alarm=85.0)
        now2 = now + timedelta(minutes=5)
        self._seed_running(now2)
        bucket2 = a.last_completed_bucket_start(now2, 5)
        self.historian.save_tag(self.target.tag_name, "sim", 90.0, bucket2 + timedelta(minutes=1))
        result2 = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now2)

        self.assertEqual(result2["action"], "updated")
        self.assertEqual(result2["id"], original_id)  # same row, not duplicated
        row = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)
        self.assertEqual(row["engineering_limit_status"], "alarm_exceeded")

    def test_same_condition_updates_not_duplicates(self):
        """item 8 - repeated new-bucket evaluations of an already-OPEN
        anomaly update the same row (partial unique index also enforces
        this at the DB level)."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)
        opened = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)

        now2 = now + timedelta(minutes=5)
        self._seed_running(now2)
        bucket2 = a.last_completed_bucket_start(now2, 5)
        self.historian.save_tag(self.target.tag_name, "sim", 92.0, bucket2 + timedelta(minutes=1))
        updated = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now2)

        self.assertEqual(updated["action"], "updated")
        self.assertEqual(updated["id"], opened["id"])
        connection = sqlite3.connect(self.config_db)
        count = connection.execute("SELECT COUNT(*) FROM anomalies WHERE status = 'OPEN'").fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_resolved_can_recur_as_a_new_row(self):
        """item 9/14 - a resolved condition recurring later creates a
        brand-new row (real recurrence history), with occurrence_count
        reflecting real prior RESOLVED rows."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)
        opened = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        first_id = opened["id"]

        t = now
        result = None
        for i in range(1, self.rule.persistence_periods_resolve + 1):
            t = now + timedelta(minutes=5 * i)
            self._seed_running(t)
            bucket_i = a.last_completed_bucket_start(t, 5)
            self.historian.save_tag(self.target.tag_name, "sim", 51.0, bucket_i + timedelta(minutes=1))
            result = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=t)
        self.assertEqual(result["action"], "resolved")
        self.assertEqual(a.count_prior_occurrences(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key), 1)

        # Recur: 3 more fresh abnormal buckets after the resolve.
        t2 = t + timedelta(minutes=5)
        recur_buckets = a._recent_completed_buckets(t2, 5, 3)
        for bucket_start in recur_buckets:
            self._seed_running(bucket_start)
            self.historian.save_tag(self.target.tag_name, "sim", 90.0, bucket_start + timedelta(minutes=1))
        reopened = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=t2)

        self.assertEqual(reopened["action"], "opened")
        self.assertNotEqual(reopened["id"], first_id)  # a NEW row, not a resurrection of the old one
        new_row = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)
        self.assertEqual(new_row["occurrence_count"], 2)  # 1 prior RESOLVED + this one

    def test_threshold_provenance_present_in_stored_row(self):
        """item 8 (approval) - provenance must be visible on every finding."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)
        a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        row = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)
        self.assertEqual(row["threshold_provenance"], self.rule.threshold_provenance)
        assumptions_text = " ".join(json.loads(row["assumptions_json"]))
        self.assertIn(self.rule.threshold_provenance, assumptions_text)

    def test_no_root_cause_or_savings_language(self):
        """items 17/18 - assumptions/evidence/title must never claim a
        cause, a fix, or a 'saving'."""
        self._seed_baseline()
        now = BASE
        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)
        a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        row = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)

        text = " ".join([
            row["title"],
            " ".join(json.loads(row["assumptions_json"])),
            json.dumps(json.loads(row["evidence_json"])),
        ]).lower()
        for banned in ("root cause", "saving", "recommend", "caused by", "fix ", "leak"):
            self.assertNotIn(banned, text)

    def test_maintenance_window_suppresses_new_open(self):
        """item 11 policy C, no-existing branch."""
        self._seed_baseline()
        now = BASE
        connection = sqlite3.connect(self.config_db)
        equipment_id = connection.execute("SELECT id FROM equipment WHERE name = 'p01_chiller_chl01'").fetchone()[0]
        connection.execute(
            "INSERT INTO maintenance_log (equipment_id, category, description, performed_at, next_due_at, created_at) VALUES (?, 'preventive', 'test', ?, NULL, ?)",
            (equipment_id, now.strftime(b.TIME_FORMAT), datetime.now().strftime(b.TIME_FORMAT)),
        )
        connection.commit()
        connection.close()

        self._seed_running(now)
        _seed_flat_series(self.historian, self.target.tag_name, now, 90.0, 3, self.target.aggregation_minutes)
        result = a.evaluate_deviation_rule(self.rule, self.target, self.config_db, self.machine_db, self.historian, self.plant_id, now=now)
        self.assertEqual(result["action"], "suppressed")
        self.assertEqual(result["reason"], "maintenance window")


# ---------------------------------------------------------------------------
# P01/P02 independence (item 10 of the original spec list)
# ---------------------------------------------------------------------------

class TestPlantIndependence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        self.historian = DatabaseManager(db_path=self.machine_db)
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        _insert_tag(self.config_db, "P02.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_p01_anomaly_never_appears_under_p02(self):
        rule = get_rule("chiller_power_deviation")
        p01_id, p02_id = _plant_id(self.config_db, "p01"), _plant_id(self.config_db, "p02")
        p01_target = _make_target(instance_key="P01.UTILITY.CHL01", tag_name="P01.UTILITY.CHL01.Power_kW")
        p02_target = _make_target(instance_key="P02.UTILITY.CHL01", tag_name="P02.UTILITY.CHL01.Power_kW")

        _insert_baseline_row(self.config_db, p01_id, "P01.UTILITY.CHL01", "Power_kW", median_value=50.0, range_low=40.0, range_high=60.0)
        _insert_baseline_row(self.config_db, p02_id, "P02.UTILITY.CHL01", "Power_kW", median_value=50.0, range_low=40.0, range_high=60.0)

        now = BASE
        for tag in ("P01.UTILITY.CHL01.RunStatus", "P02.UTILITY.CHL01.RunStatus"):
            self.historian.save_tag(tag, "sim", 1, now - timedelta(minutes=1))
        # Only P01 actually goes abnormal.
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", now, 90.0, 3, 5)
        _seed_flat_series(self.historian, "P02.UTILITY.CHL01.Power_kW", now, 51.0, 3, 5)

        p01_result = a.evaluate_deviation_rule(rule, p01_target, self.config_db, self.machine_db, self.historian, p01_id, now=now)
        p02_result = a.evaluate_deviation_rule(rule, p02_target, self.config_db, self.machine_db, self.historian, p02_id, now=now)

        self.assertEqual(p01_result["action"], "opened")
        self.assertNotEqual(p02_result["action"], "opened")
        self.assertIsNotNone(a.find_open_anomaly(self.config_db, p01_id, "P01.UTILITY.CHL01", "Power_kW", rule.rule_key))
        self.assertIsNone(a.find_open_anomaly(self.config_db, p02_id, "P02.UTILITY.CHL01", "Power_kW", rule.rule_key))


# ---------------------------------------------------------------------------
# Drift-rule lifecycle (items 12, 13, 15; clarification h)
# ---------------------------------------------------------------------------

class TestDriftRuleLifecycle(AnomalyEngineTestBase):
    def setUp(self):
        super().setUp()
        self.rule = get_rule("pump_flow_per_kw_drift")
        self.target = _make_target(instance_key="P01.UTILITY.CHWP01", equipment_type="chilled_water_pump", target_key="flow_per_kw", tag_name="P01.UTILITY.CHWP01.FlowPerKw")

    def _seed_pair(self, reference_median, recent_median, computed_at="2026-08-20 10:00:00", reference_status="mature", recent_status="mature"):
        _insert_baseline_row(
            self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, baseline_type="reference",
            median_value=reference_median, range_low=reference_median - 1, range_high=reference_median + 1,
            history_start="2026-07-01 00:00:00", history_end="2026-08-01 00:00:00", baseline_status=reference_status, computed_at=computed_at,
        )
        _insert_baseline_row(
            self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, baseline_type="recent",
            median_value=recent_median, range_low=recent_median - 1, range_high=recent_median + 1,
            history_start="2026-08-13 00:00:00", history_end="2026-08-20 00:00:00", baseline_status=recent_status, computed_at=computed_at,
        )

    def test_skipped_when_baselines_not_yet_persisted(self):
        result = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE)
        self.assertEqual(result["action"], "skipped")

    def test_skipped_when_either_baseline_not_mature(self):
        self._seed_pair(reference_median=10.0, recent_median=8.0, recent_status="bootstrap")
        result = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE)
        self.assertEqual(result["action"], "skipped")

    def test_skipped_when_windows_have_not_separated(self):
        _insert_baseline_row(
            self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, baseline_type="reference",
            median_value=10.0, history_start="2026-08-13 00:00:00", history_end="2026-08-20 00:00:00",
        )
        _insert_baseline_row(
            self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, baseline_type="recent",
            median_value=8.0, history_start="2026-08-13 00:00:00", history_end="2026-08-20 00:00:00",
        )
        result = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE)
        self.assertEqual(result["action"], "skipped")

    def test_requires_two_distinct_qualifying_snapshots_before_opening(self):
        """item 13 - a single qualifying snapshot only reaches
        'drift_candidate'; a second, DISTINCT (different computed_at)
        qualifying snapshot is required to actually open."""
        # direction="low": recent below reference by >= 10% qualifies.
        self._seed_pair(reference_median=10.0, recent_median=8.5, computed_at="2026-08-20 10:00:00")
        first = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE)
        self.assertEqual(first["action"], "drift_candidate")
        self.assertIsNone(a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key))

        self._seed_pair(reference_median=10.0, recent_median=8.5, computed_at="2026-08-20 10:05:00")
        second = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE + timedelta(minutes=5))
        self.assertEqual(second["action"], "opened")

    def test_unchanged_snapshot_does_not_double_count(self):
        """clarification h - re-evaluating the SAME persisted recent
        snapshot (computed_at unchanged) must never advance drift state
        twice - both before and after opening."""
        self._seed_pair(reference_median=10.0, recent_median=8.5, computed_at="2026-08-20 10:00:00")
        first = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE)
        self.assertEqual(first["action"], "drift_candidate")

        repeat = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE + timedelta(seconds=30))
        self.assertEqual(repeat["action"], "unchanged_candidate")

    def test_open_drift_row_unchanged_snapshot_is_a_no_op(self):
        self._seed_pair(reference_median=10.0, recent_median=8.5, computed_at="2026-08-20 10:00:00")
        a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE)
        self._seed_pair(reference_median=10.0, recent_median=8.5, computed_at="2026-08-20 10:05:00")
        opened = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE + timedelta(minutes=5))
        self.assertEqual(opened["action"], "opened")

        # Same recent snapshot again - must be a no-op, not "updated".
        again = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE + timedelta(minutes=6))
        self.assertEqual(again["action"], "unchanged")

    def test_drift_resolves_when_no_longer_qualifying(self):
        self._seed_pair(reference_median=10.0, recent_median=8.5, computed_at="2026-08-20 10:00:00")
        a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE)
        self._seed_pair(reference_median=10.0, recent_median=8.5, computed_at="2026-08-20 10:05:00")
        a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE + timedelta(minutes=5))

        # Recovers back to matching reference.
        self._seed_pair(reference_median=10.0, recent_median=9.9, computed_at="2026-08-20 10:10:00")
        result = a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE + timedelta(minutes=10))
        self.assertEqual(result["action"], "resolved")

    def test_drift_evidence_uses_neutral_no_cause_language(self):
        """item 15/13 - drift reports movement only, never a cause."""
        self._seed_pair(reference_median=10.0, recent_median=8.5, computed_at="2026-08-20 10:00:00")
        a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE)
        self._seed_pair(reference_median=10.0, recent_median=8.5, computed_at="2026-08-20 10:05:00")
        a.evaluate_drift_rule(self.rule, self.target, self.config_db, self.plant_id, now=BASE + timedelta(minutes=5))

        row = a.find_open_anomaly(self.config_db, self.plant_id, self.target.instance_key, self.target.target_key, self.rule.rule_key)
        assumptions_text = " ".join(json.loads(row["assumptions_json"])).lower()
        self.assertIn("no cause is inferred", assumptions_text)
        for banned in ("root cause", "caused by", "leak", "fouling"):
            self.assertNotIn(banned, assumptions_text)


# ---------------------------------------------------------------------------
# anomalies table constraint (partial unique index) - regression guard
# ---------------------------------------------------------------------------

class TestOneOpenPerConditionConstraint(AnomalyEngineTestBase):
    def test_duplicate_open_row_rejected_at_db_level(self):
        rule = get_rule("chiller_power_deviation")
        row = dict(
            plant_id=self.plant_id, equipment_id=None, instance_key="P01.UTILITY.CHL01", target_key="Power_kW",
            rule_key=rule.rule_key, anomaly_type="deviation", category="UTILITY", title="t", severity="ATTENTION",
            confidence="High", provisional=0, status="OPEN", first_detected="2026-08-20 10:00:00", last_seen="2026-08-20 10:00:00",
            resolved_at=None, occurrence_count=1, open_persistence_periods=3, resolve_persistence_periods=0,
            last_bucket_start="2026-08-20 09:55:00", actual_value=90.0, expected_value=50.0, expected_low=40.0, expected_high=60.0,
            deviation_absolute=40.0, deviation_percent=80.0, normalized_deviation=13.3, baseline_type="recent", baseline_level="C",
            baseline_confidence="High", engineering_limit_status="not_exceeded", engineering_limit_event_id=None,
            estimated_excess_energy_kwh=None, estimated_excess_cost=None, estimated_excess_cost_currency=None,
            threshold_provenance="SIMULATION_TUNING", source_tags="[]", evidence_json="{}", assumptions_json="[]",
            data_limitations_json="[]", created_at="2026-08-20 10:00:00", updated_at="2026-08-20 10:00:00",
        )
        first_id = a._write_anomaly_row(self.config_db, None, row)
        self.assertIsNotNone(first_id)
        with self.assertRaises(sqlite3.IntegrityError):
            a._write_anomaly_row(self.config_db, None, row)


if __name__ == "__main__":
    unittest.main()
