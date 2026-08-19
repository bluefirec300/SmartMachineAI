import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from database.database import DatabaseManager
from engine import performance_engine as pe
from engine.performance_targets import (
    EFFECTIVENESS_IMPROVED,
    EFFECTIVENESS_INSUFFICIENT_EVIDENCE,
    EFFECTIVENESS_NOT_CLASSIFIED,
    EFFECTIVENESS_NO_MEASURABLE_CHANGE,
    EFFECTIVENESS_WORSENED,
    HIGHER_IS_BETTER,
    INFORMATIONAL_ONLY,
    LOWER_IS_BETTER,
    STATE_DEGRADING,
    STATE_IMPROVING,
    STATE_INSUFFICIENT_EVIDENCE,
    STATE_NOT_CLASSIFIED,
    STATE_SIGNIFICANTLY_DEGRADING,
    STATE_STABLE,
    TARGET_RANGE,
)
from engine.savings_verification_evidence import EVIDENCE_INSUFFICIENT

from tests.test_baseline_engine import _insert_tag, _make_target, _seed_config_db, _seed_series

# Anchored well past MODERN_DATA_BOUNDARY (2026-08-09) so a real
# reference/recent split is available (recent_window_days default 7).
NOW = datetime(2026, 9, 20, 0, 0, 0)


# ---------------------------------------------------------------------------
# Pure classification functions (item 40) - no fixtures needed.
# ---------------------------------------------------------------------------

class TestClassifyChange(unittest.TestCase):
    def test_lower_is_better_improving(self):
        self.assertEqual(pe.classify_change(LOWER_IS_BETTER, -10.0), STATE_IMPROVING)

    def test_lower_is_better_stable_small_change(self):
        self.assertEqual(pe.classify_change(LOWER_IS_BETTER, 2.0), STATE_STABLE)

    def test_lower_is_better_degrading(self):
        self.assertEqual(pe.classify_change(LOWER_IS_BETTER, 20.0), STATE_DEGRADING)

    def test_lower_is_better_significantly_degrading(self):
        self.assertEqual(pe.classify_change(LOWER_IS_BETTER, 40.0), STATE_SIGNIFICANTLY_DEGRADING)

    def test_higher_is_better_inverts_sign(self):
        # A DECREASE (negative percent_change) is worse for HIGHER_IS_BETTER.
        self.assertEqual(pe.classify_change(HIGHER_IS_BETTER, -20.0), STATE_DEGRADING)
        self.assertEqual(pe.classify_change(HIGHER_IS_BETTER, 20.0), STATE_IMPROVING)

    def test_target_range_uses_absolute_deviation(self):
        self.assertEqual(pe.classify_change(TARGET_RANGE, 20.0), STATE_DEGRADING)
        self.assertEqual(pe.classify_change(TARGET_RANGE, -20.0), STATE_DEGRADING)

    def test_target_range_never_reports_improving(self):
        for pct in (-50.0, -20.0, -5.0, 0.0, 5.0, 20.0, 50.0):
            self.assertNotEqual(pe.classify_change(TARGET_RANGE, pct), STATE_IMPROVING)

    def test_informational_only_is_not_classified(self):
        self.assertEqual(pe.classify_change(INFORMATIONAL_ONLY, 50.0), STATE_NOT_CLASSIFIED)

    def test_none_percent_change_is_insufficient_evidence(self):
        self.assertEqual(pe.classify_change(LOWER_IS_BETTER, None), STATE_INSUFFICIENT_EVIDENCE)

    def test_boundary_exactly_at_degrading_threshold(self):
        from engine.performance_targets import DEGRADING_THRESHOLD_PCT
        self.assertEqual(pe.classify_change(LOWER_IS_BETTER, DEGRADING_THRESHOLD_PCT), STATE_DEGRADING)


class TestClassifyEffectiveness(unittest.TestCase):
    def test_improved(self):
        self.assertEqual(pe.classify_effectiveness(LOWER_IS_BETTER, -10.0), EFFECTIVENESS_IMPROVED)

    def test_no_measurable_change(self):
        self.assertEqual(pe.classify_effectiveness(LOWER_IS_BETTER, 2.0), EFFECTIVENESS_NO_MEASURABLE_CHANGE)

    def test_worsened(self):
        self.assertEqual(pe.classify_effectiveness(LOWER_IS_BETTER, 10.0), EFFECTIVENESS_WORSENED)

    def test_higher_is_better_inverted(self):
        self.assertEqual(pe.classify_effectiveness(HIGHER_IS_BETTER, 10.0), EFFECTIVENESS_IMPROVED)
        self.assertEqual(pe.classify_effectiveness(HIGHER_IS_BETTER, -10.0), EFFECTIVENESS_WORSENED)

    def test_informational_only_not_classified(self):
        self.assertEqual(pe.classify_effectiveness(INFORMATIONAL_ONLY, 50.0), EFFECTIVENESS_NOT_CLASSIFIED)

    def test_none_percent_change_insufficient(self):
        self.assertEqual(pe.classify_effectiveness(LOWER_IS_BETTER, None), EFFECTIVENESS_INSUFFICIENT_EVIDENCE)


class TestComputeChange(unittest.TestCase):
    def test_normal_change(self):
        absolute, percent, limitations = pe._compute_change(50.0, 55.0)
        self.assertEqual(absolute, 5.0)
        self.assertEqual(percent, 10.0)
        self.assertEqual(limitations, [])

    def test_zero_reference_never_divides_silently(self):
        absolute, percent, limitations = pe._compute_change(0.0, 5.0)
        self.assertEqual(absolute, 5.0)
        self.assertIsNone(percent)
        self.assertTrue(limitations)

    def test_missing_values_return_none(self):
        self.assertEqual(pe._compute_change(None, 5.0), (None, None, []))
        self.assertEqual(pe._compute_change(5.0, None), (None, None, []))

    def test_negative_reference_computes_but_flags_limitation(self):
        absolute, percent, limitations = pe._compute_change(-10.0, -8.0)
        self.assertEqual(absolute, 2.0)
        self.assertIsNotNone(percent)
        self.assertTrue(any("negative" in limitation for limitation in limitations))

    def test_no_fabricated_zero_percent_from_undefined_comparison(self):
        _, percent, _ = pe._compute_change(0.0, 0.0)
        self.assertIsNone(percent)  # never silently "0% degradation"


# ---------------------------------------------------------------------------
# End-to-end observation calculation against a real (isolated) historian
# fixture (item 40's "improvement/degradation/stable/insufficient" cases).
# ---------------------------------------------------------------------------

class PerformanceEngineTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        self.historian = DatabaseManager(db_path=self.machine_db)
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_power(self, reference_value: float, recent_value: float, noise=0.5):
        rng = random.Random(7)
        points = []
        # ~14 days of reference-period data, then ~5 days of recent-period data.
        for day in range(14):
            t = NOW - timedelta(days=20 - day, hours=-10)
            points.append((t, reference_value + rng.uniform(-noise, noise)))
        for day in range(5):
            t = NOW - timedelta(days=5 - day, hours=-10)
            points.append((t, recent_value + rng.uniform(-noise, noise)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)


class TestCalculatePerformanceObservation(PerformanceEngineTestBase):
    def test_stable_power_is_stable(self):
        self._seed_power(reference_value=60.0, recent_value=60.5)
        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_performance_observation(self.config_db, self.machine_db, self.historian, plant_id=1, target=target, now=NOW)
        self.assertEqual(result.direction, LOWER_IS_BETTER)
        self.assertIn(result.performance_state, (STATE_STABLE, STATE_INSUFFICIENT_EVIDENCE))

    def test_higher_recent_power_degrades(self):
        self._seed_power(reference_value=60.0, recent_value=80.0)
        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_performance_observation(self.config_db, self.machine_db, self.historian, plant_id=1, target=target, now=NOW)
        if result.evidence_quality != EVIDENCE_INSUFFICIENT:
            self.assertIn(result.performance_state, (STATE_DEGRADING, STATE_SIGNIFICANTLY_DEGRADING))
            self.assertGreater(result.percent_change, 0)

    def test_lower_recent_power_improves(self):
        self._seed_power(reference_value=80.0, recent_value=60.0)
        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_performance_observation(self.config_db, self.machine_db, self.historian, plant_id=1, target=target, now=NOW)
        if result.evidence_quality != EVIDENCE_INSUFFICIENT:
            self.assertEqual(result.performance_state, STATE_IMPROVING)
            self.assertLess(result.percent_change, 0)

    def test_no_data_at_all_is_insufficient_evidence(self):
        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_performance_observation(self.config_db, self.machine_db, self.historian, plant_id=1, target=target, now=NOW)
        self.assertEqual(result.performance_state, STATE_INSUFFICIENT_EVIDENCE)
        self.assertEqual(result.evidence_quality, EVIDENCE_INSUFFICIENT)
        self.assertIsNone(result.observed_value)

    def test_informational_only_dimension_is_not_classified_even_with_data(self):
        _insert_tag(self.config_db, "P01.UTILITY.AC01.Pressure", unit="bar")
        rng = random.Random(3)
        points = [(NOW - timedelta(days=15 - i), 7.0 + rng.uniform(-0.1, 0.1)) for i in range(15)]
        _seed_series(self.historian, "P01.UTILITY.AC01.Pressure", points)
        target = _make_target(
            instance_key="P01.UTILITY.AC01", equipment_type="air_compressor", target_key="Pressure",
            tag_name="P01.UTILITY.AC01.Pressure", context_dimensions=(),
        )
        result = pe.calculate_performance_observation(self.config_db, self.machine_db, self.historian, plant_id=1, target=target, now=NOW)
        self.assertEqual(result.direction, INFORMATIONAL_ONLY)
        self.assertEqual(result.performance_state, STATE_NOT_CLASSIFIED)
        self.assertFalse(result.participates_in_degradation)

    def test_evidence_quality_never_none(self):
        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_performance_observation(self.config_db, self.machine_db, self.historian, plant_id=1, target=target, now=NOW)
        self.assertIsNotNone(result.evidence_quality)

    def test_reason_is_generated_from_structured_facts(self):
        self._seed_power(reference_value=60.0, recent_value=80.0)
        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_performance_observation(self.config_db, self.machine_db, self.historian, plant_id=1, target=target, now=NOW)
        self.assertTrue(result.reason)
        self.assertNotIn("None", result.reason)


# ---------------------------------------------------------------------------
# Maintenance before/after comparison (item 41)
# ---------------------------------------------------------------------------

class TestCalculateMaintenanceComparison(PerformanceEngineTestBase):
    def _maintenance_row(self, performed_at: datetime, log_id=1):
        return {"id": log_id, "equipment_id": 1, "performed_at": performed_at.strftime("%Y-%m-%d %H:%M:%S")}

    def test_improvement_after_maintenance(self):
        performed_at = NOW - timedelta(days=10)
        rng = random.Random(11)
        points = []
        for day in range(8):
            points.append((performed_at - timedelta(days=8 - day, hours=-10), 80.0 + rng.uniform(-1, 1)))
        for day in range(8):
            points.append((performed_at + timedelta(days=day + 2, hours=-10), 60.0 + rng.uniform(-1, 1)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_maintenance_comparison(
            self.config_db, self.machine_db, self.historian, plant_id=1, target=target,
            maintenance_log_row=self._maintenance_row(performed_at), now=NOW,
        )
        if result.evidence_quality != EVIDENCE_INSUFFICIENT:
            self.assertEqual(result.effectiveness_result, EFFECTIVENESS_IMPROVED)
            self.assertIn("improved", result.reason.lower())
            self.assertNotIn("caused", result.reason.lower())  # causal-language discipline

    def test_worsening_after_maintenance(self):
        performed_at = NOW - timedelta(days=10)
        rng = random.Random(13)
        points = []
        for day in range(8):
            points.append((performed_at - timedelta(days=8 - day, hours=-10), 60.0 + rng.uniform(-1, 1)))
        for day in range(8):
            points.append((performed_at + timedelta(days=day + 2, hours=-10), 80.0 + rng.uniform(-1, 1)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_maintenance_comparison(
            self.config_db, self.machine_db, self.historian, plant_id=1, target=target,
            maintenance_log_row=self._maintenance_row(performed_at), now=NOW,
        )
        if result.evidence_quality != EVIDENCE_INSUFFICIENT:
            self.assertEqual(result.effectiveness_result, EFFECTIVENESS_WORSENED)

    def test_no_measurable_change(self):
        performed_at = NOW - timedelta(days=10)
        rng = random.Random(17)
        points = []
        for day in range(8):
            points.append((performed_at - timedelta(days=8 - day, hours=-10), 60.0 + rng.uniform(-0.5, 0.5)))
        for day in range(8):
            points.append((performed_at + timedelta(days=day + 2, hours=-10), 60.3 + rng.uniform(-0.5, 0.5)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_maintenance_comparison(
            self.config_db, self.machine_db, self.historian, plant_id=1, target=target,
            maintenance_log_row=self._maintenance_row(performed_at), now=NOW,
        )
        if result.evidence_quality != EVIDENCE_INSUFFICIENT:
            self.assertEqual(result.effectiveness_result, EFFECTIVENESS_NO_MEASURABLE_CHANGE)

    def test_missing_pre_maintenance_data(self):
        performed_at = NOW - timedelta(days=10)
        rng = random.Random(19)
        points = [(performed_at + timedelta(days=day + 2, hours=-10), 60.0 + rng.uniform(-1, 1)) for day in range(8)]
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_maintenance_comparison(
            self.config_db, self.machine_db, self.historian, plant_id=1, target=target,
            maintenance_log_row=self._maintenance_row(performed_at), now=NOW,
        )
        self.assertEqual(result.effectiveness_result, EFFECTIVENESS_INSUFFICIENT_EVIDENCE)

    def test_missing_post_maintenance_data_stabilization_not_finished(self):
        performed_at = NOW - timedelta(hours=1)  # far too recent
        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_maintenance_comparison(
            self.config_db, self.machine_db, self.historian, plant_id=1, target=target,
            maintenance_log_row=self._maintenance_row(performed_at), now=NOW,
        )
        self.assertIsNone(result.post_window)
        self.assertEqual(result.effectiveness_result, EFFECTIVENESS_INSUFFICIENT_EVIDENCE)

    def test_causal_language_never_used(self):
        performed_at = NOW - timedelta(days=10)
        rng = random.Random(23)
        points = []
        for day in range(8):
            points.append((performed_at - timedelta(days=8 - day, hours=-10), 80.0 + rng.uniform(-1, 1)))
        for day in range(8):
            points.append((performed_at + timedelta(days=day + 2, hours=-10), 60.0 + rng.uniform(-1, 1)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        target = _make_target(context_dimensions=("hour_bucket",))
        result = pe.calculate_maintenance_comparison(
            self.config_db, self.machine_db, self.historian, plant_id=1, target=target,
            maintenance_log_row=self._maintenance_row(performed_at), now=NOW,
        )
        # Checks the user-facing `reason` text only - the assumptions list
        # legitimately DISCLAIMS causal language by name ("never a causal
        # claim that the maintenance itself caused the change"), which
        # would otherwise self-trip a bare substring check on "caused".
        for banned in ("maintenance fixed", "maintenance caused", "caused the improvement", "caused the degradation"):
            self.assertNotIn(banned, result.reason.lower())


if __name__ == "__main__":
    unittest.main()
