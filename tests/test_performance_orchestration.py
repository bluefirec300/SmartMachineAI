import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from database.database import DatabaseManager
from engine import performance_domain as dom
from engine import performance_migrator as pm
from engine import performance_orchestration as orch
from engine.maintenance_migrator import migrate as migrate_maintenance
from engine.performance_targets import (
    STATE_DEGRADING,
    STATE_STABLE,
    SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS,
)
from engine.savings_verification_evidence import EVIDENCE_CONTEXT_MATCHED

from tests.test_baseline_engine import _insert_tag, _make_target, _seed_config_db, _seed_series
from tests.test_performance_persistence import _comparison, _observation

NOW = datetime(2026, 9, 20, 0, 0, 0)


class TestHasMaterialChange(unittest.TestCase):
    def _obs(self, **kw):
        return _observation(**kw)

    def test_no_prior_result_is_always_material(self):
        self.assertTrue(orch.has_material_change(self._obs(), None))

    def test_same_state_and_confidence_and_close_percent_is_not_material(self):
        prior = {"performance_state": STATE_STABLE, "evidence_quality": EVIDENCE_CONTEXT_MATCHED, "percent_change": 3.0, "computed_at": "2026-09-19 08:00:00"}
        observation = self._obs(performance_state=STATE_STABLE, evidence_quality=EVIDENCE_CONTEXT_MATCHED, percent_change=3.5)
        self.assertFalse(orch.has_material_change(observation, prior))

    def test_state_change_is_material(self):
        prior = {"performance_state": STATE_STABLE, "evidence_quality": EVIDENCE_CONTEXT_MATCHED, "percent_change": 3.0, "computed_at": "2026-09-19 08:00:00"}
        observation = self._obs(performance_state=STATE_DEGRADING, evidence_quality=EVIDENCE_CONTEXT_MATCHED, percent_change=20.0)
        self.assertTrue(orch.has_material_change(observation, prior))

    def test_large_percent_shift_is_material_even_with_same_state(self):
        prior = {"performance_state": STATE_STABLE, "evidence_quality": EVIDENCE_CONTEXT_MATCHED, "percent_change": 1.0, "computed_at": "2026-09-19 08:00:00"}
        observation = self._obs(performance_state=STATE_STABLE, evidence_quality=EVIDENCE_CONTEXT_MATCHED, percent_change=8.0)
        self.assertTrue(orch.has_material_change(observation, prior))

    def test_percent_availability_change_is_material(self):
        prior = {"performance_state": STATE_STABLE, "evidence_quality": EVIDENCE_CONTEXT_MATCHED, "percent_change": 3.0, "computed_at": "2026-09-19 08:00:00"}
        observation = self._obs(performance_state=STATE_STABLE, evidence_quality=EVIDENCE_CONTEXT_MATCHED, percent_change=None)
        self.assertTrue(orch.has_material_change(observation, prior))


class TestShouldHeartbeat(unittest.TestCase):
    def test_no_prior_never_heartbeats(self):
        self.assertFalse(orch._should_heartbeat(None, NOW))

    def test_recent_prior_does_not_heartbeat(self):
        prior = {"computed_at": (NOW - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")}
        self.assertFalse(orch._should_heartbeat(prior, NOW))

    def test_stale_prior_heartbeats(self):
        prior = {"computed_at": (NOW - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")}
        self.assertTrue(orch._should_heartbeat(prior, NOW))


class TestHasMateriallyNewMaintenanceEvidence(unittest.TestCase):
    def test_no_prior_is_always_new(self):
        self.assertTrue(orch.has_materially_new_maintenance_evidence(_comparison(), None))

    def test_more_post_samples_is_new(self):
        prior = {"post_sample_count": 5, "evidence_quality": EVIDENCE_CONTEXT_MATCHED, "effectiveness_result": "IMPROVED"}
        comparison = _comparison(post_sample_count=10, evidence_quality=EVIDENCE_CONTEXT_MATCHED, effectiveness_result="IMPROVED")
        self.assertTrue(orch.has_materially_new_maintenance_evidence(comparison, prior))

    def test_unchanged_is_not_new(self):
        prior = {"post_sample_count": 10, "evidence_quality": EVIDENCE_CONTEXT_MATCHED, "effectiveness_result": "IMPROVED"}
        comparison = _comparison(post_sample_count=10, evidence_quality=EVIDENCE_CONTEXT_MATCHED, effectiveness_result="IMPROVED")
        self.assertFalse(orch.has_materially_new_maintenance_evidence(comparison, prior))


class PerformanceOrchestrationTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        pm.migrate(self.config_db, backup=False)
        migrate_maintenance(self.config_db, backup=False)
        self.historian = DatabaseManager(db_path=self.machine_db)
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_power(self, reference_value: float, recent_value: float):
        rng = random.Random(5)
        points = []
        for day in range(14):
            points.append((NOW - timedelta(days=20 - day, hours=-10), reference_value + rng.uniform(-0.5, 0.5)))
        for day in range(5):
            points.append((NOW - timedelta(days=5 - day, hours=-10), recent_value + rng.uniform(-0.5, 0.5)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)


class TestComputeConsecutiveDegrading(PerformanceOrchestrationTestBase):
    def test_zero_when_not_degrading(self):
        count, sustained = orch.compute_consecutive_degrading(self.config_db, 1, "P01.UTILITY.CHL01", "Power_kW", STATE_STABLE)
        self.assertEqual(count, 0)
        self.assertFalse(sustained)

    def test_counts_consecutive_prior_degrading_observations(self):
        for i in range(SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS - 1):
            dom.persist_observation(
                self.config_db, _observation(computed_at=f"2026-09-{10+i:02d} 08:00:00", performance_state=STATE_DEGRADING),
                "initial", 0, False, now=NOW,
            )
        count, sustained = orch.compute_consecutive_degrading(self.config_db, 1, "P01.UTILITY.CHL01", "Power_kW", STATE_DEGRADING)
        self.assertEqual(count, SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS)  # prior N-1 + this new one
        self.assertTrue(sustained)

    def test_stops_at_first_non_degrading_observation(self):
        dom.persist_observation(self.config_db, _observation(computed_at="2026-09-08 08:00:00", performance_state=STATE_STABLE), "initial", 0, False, now=NOW)
        dom.persist_observation(self.config_db, _observation(computed_at="2026-09-09 08:00:00", performance_state=STATE_DEGRADING), "initial", 0, False, now=NOW)
        count, sustained = orch.compute_consecutive_degrading(self.config_db, 1, "P01.UTILITY.CHL01", "Power_kW", STATE_DEGRADING)
        self.assertEqual(count, 2)  # the prior DEGRADING one + this new one - STABLE breaks the streak
        self.assertFalse(sustained)


class TestEvaluateAndMaybePersistObservation(PerformanceOrchestrationTestBase):
    def test_first_evaluation_is_initial_and_persists(self):
        self._seed_power(60.0, 60.5)
        target = _make_target(context_dimensions=("hour_bucket",))
        outcome = orch.evaluate_and_maybe_persist_observation(self.config_db, self.machine_db, self.historian, 1, target, now=NOW)
        self.assertEqual(outcome["action"], "persisted")
        self.assertEqual(outcome["change_reason"], "initial")

    def test_idempotent_repeat_call_with_no_change_is_skipped(self):
        """item 32 - repeated processing of the same equipment/time window
        must not create uncontrolled duplicate rows."""
        self._seed_power(60.0, 60.5)
        target = _make_target(context_dimensions=("hour_bucket",))
        orch.evaluate_and_maybe_persist_observation(self.config_db, self.machine_db, self.historian, 1, target, now=NOW)
        outcome2 = orch.evaluate_and_maybe_persist_observation(self.config_db, self.machine_db, self.historian, 1, target, now=NOW)
        self.assertEqual(outcome2["action"], "skipped")
        history = dom.list_observations(self.config_db, 1, "P01.UTILITY.CHL01", "Power_kW")
        self.assertEqual(len(history), 1)  # not duplicated

    def test_heartbeat_persists_after_interval_even_without_material_change(self):
        self._seed_power(60.0, 60.5)
        target = _make_target(context_dimensions=("hour_bucket",))
        orch.evaluate_and_maybe_persist_observation(self.config_db, self.machine_db, self.historian, 1, target, now=NOW)
        later = NOW + timedelta(hours=25)
        outcome = orch.evaluate_and_maybe_persist_observation(self.config_db, self.machine_db, self.historian, 1, target, now=later)
        self.assertEqual(outcome["action"], "persisted")
        self.assertEqual(outcome["change_reason"], "heartbeat")


class TestRunObservationCycle(PerformanceOrchestrationTestBase):
    def test_unsupported_equipment_type_does_not_crash_cycle(self):
        # No tags seeded for any OTHER equipment type - discover_targets()
        # naturally returns only P01.UTILITY.CHL01.Power_kW; the cycle
        # must still complete cleanly with just that one target.
        self._seed_power(60.0, 60.5)
        summary = orch.run_observation_cycle(self.config_db, self.machine_db, self.historian, "p01", now=NOW)
        self.assertEqual(summary["failed"], 0)
        self.assertGreaterEqual(summary["evaluated"], 1)

    def test_one_target_failure_does_not_abort_the_rest_of_the_cycle(self):
        self._seed_power(60.0, 60.5)
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.WaterFlow", unit="m3/h", measurement="flow")

        original = orch.perf.calculate_performance_observation
        call_count = {"n": 0}

        def _flaky(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated failure for the first target only")
            return original(*args, **kwargs)

        orch.perf.calculate_performance_observation = _flaky
        try:
            summary = orch.run_observation_cycle(self.config_db, self.machine_db, self.historian, "p01", now=NOW)
        finally:
            orch.perf.calculate_performance_observation = original

        self.assertEqual(summary["failed"], 1)
        self.assertGreaterEqual(summary["evaluated"], 2)


class TestRunMaintenanceComparisonCycle(PerformanceOrchestrationTestBase):
    def _seed_maintenance_log(self, equipment_id: int, performed_at: str):
        import sqlite3
        connection = sqlite3.connect(self.config_db)
        connection.execute(
            "INSERT INTO maintenance_log (equipment_id, category, description, performed_at, created_at) VALUES (?, 'Preventive Maintenance', 'test', ?, ?)",
            (equipment_id, performed_at, performed_at),
        )
        connection.commit()
        connection.close()

    def test_no_maintenance_log_rows_completes_cleanly(self):
        self._seed_power(60.0, 60.5)
        summary = orch.run_maintenance_comparison_cycle(self.config_db, self.machine_db, self.historian, "p01", now=NOW)
        self.assertEqual(summary["evaluated"], 0)
        self.assertEqual(summary["failed"], 0)

    def test_equipment_with_maintenance_log_is_processed(self):
        import sqlite3
        connection = sqlite3.connect(self.config_db)
        equipment_id = connection.execute("SELECT id FROM equipment WHERE name = 'p01_chiller_chl01'").fetchone()[0]
        connection.close()

        performed_at = (NOW - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        self._seed_maintenance_log(equipment_id, performed_at)

        rng = random.Random(29)
        points = []
        for day in range(8):
            points.append((NOW - timedelta(days=18 - day, hours=-10), 80.0 + rng.uniform(-1, 1)))
        for day in range(8):
            points.append((NOW - timedelta(days=8 - day, hours=-10), 60.0 + rng.uniform(-1, 1)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        summary = orch.run_maintenance_comparison_cycle(self.config_db, self.machine_db, self.historian, "p01", now=NOW)
        self.assertGreaterEqual(summary["evaluated"], 1)
        self.assertEqual(summary["failed"], 0)

    def test_re_running_cycle_does_not_duplicate_when_no_new_evidence(self):
        import sqlite3
        connection = sqlite3.connect(self.config_db)
        equipment_id = connection.execute("SELECT id FROM equipment WHERE name = 'p01_chiller_chl01'").fetchone()[0]
        connection.close()

        performed_at = (NOW - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        self._seed_maintenance_log(equipment_id, performed_at)

        rng = random.Random(31)
        points = []
        for day in range(8):
            points.append((NOW - timedelta(days=18 - day, hours=-10), 80.0 + rng.uniform(-1, 1)))
        for day in range(8):
            points.append((NOW - timedelta(days=8 - day, hours=-10), 60.0 + rng.uniform(-1, 1)))
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        orch.run_maintenance_comparison_cycle(self.config_db, self.machine_db, self.historian, "p01", now=NOW)
        summary2 = orch.run_maintenance_comparison_cycle(self.config_db, self.machine_db, self.historian, "p01", now=NOW)
        self.assertEqual(summary2["persisted"], 0)  # identical evidence the second time - skipped, not re-persisted


class TestRunCycle(PerformanceOrchestrationTestBase):
    def test_run_cycle_aggregates_both_plants(self):
        self._seed_power(60.0, 60.5)
        summary = orch.run_cycle(self.config_db, self.machine_db, now=NOW)
        self.assertIn("observations_evaluated", summary)
        self.assertIn("maintenance_evaluated", summary)
        self.assertEqual(summary["observations_failed"], 0)


class TestRotationWorkerSupport(PerformanceOrchestrationTestBase):
    """item 43 - the actual worker processes ONE item per tick
    (mirrors app/baseline_worker.py's own round-robin, not a full sweep)."""

    def test_build_rotation_includes_observation_items(self):
        self._seed_power(60.0, 60.5)
        rotation = orch.build_rotation(self.config_db)
        self.assertTrue(any(item.kind == "observation" for item in rotation))

    def test_process_one_work_item_persists_a_single_observation(self):
        self._seed_power(60.0, 60.5)
        rotation = orch.build_rotation(self.config_db)
        observation_item = next(item for item in rotation if item.kind == "observation")
        outcome = orch.process_one_work_item(self.config_db, self.machine_db, self.historian, observation_item, now=NOW)
        self.assertEqual(outcome["action"], "persisted")
        history = dom.list_observations(self.config_db, observation_item.plant_id, observation_item.target.instance_key, observation_item.target.target_key)
        self.assertEqual(len(history), 1)

    def test_rotation_is_deterministic_and_reproducible(self):
        self._seed_power(60.0, 60.5)
        rotation1 = [(i.kind, i.target.instance_key, i.target.target_key) for i in orch.build_rotation(self.config_db)]
        rotation2 = [(i.kind, i.target.instance_key, i.target.target_key) for i in orch.build_rotation(self.config_db)]
        self.assertEqual(rotation1, rotation2)


if __name__ == "__main__":
    unittest.main()
