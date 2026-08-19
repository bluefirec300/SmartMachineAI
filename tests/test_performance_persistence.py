import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from engine import performance_domain as dom
from engine import performance_migrator as pm
from engine.performance_engine import MaintenanceComparison, PerformanceObservation
from engine.performance_targets import LOWER_IS_BETTER, STATE_STABLE
from engine.savings_verification_evidence import EVIDENCE_CONTEXT_MATCHED

from tests.test_baseline_engine import _seed_config_db

NOW = datetime(2026, 9, 20, 12, 0, 0)


def _observation(**overrides) -> PerformanceObservation:
    defaults = dict(
        equipment_id=1, plant_id=1, plant_code="p01", instance_key="P01.UTILITY.CHL01", equipment_type="chiller",
        target_key="Power_kW", direction=LOWER_IS_BETTER, unit="kW", observed_value=60.0, reference_value=58.0,
        absolute_change=2.0, percent_change=3.4, performance_state=STATE_STABLE, evidence_quality=EVIDENCE_CONTEXT_MATCHED,
        sample_count=20, reference_sample_count=25, participates_in_degradation=True,
        context_used={"hour_bucket": "14"}, missing_context=[], reason="STABLE - test fixture",
        assumptions=["a"], limitations=[], computed_at=NOW.strftime("%Y-%m-%d %H:%M:%S"),
    )
    defaults.update(overrides)
    return PerformanceObservation(**defaults)


def _comparison(**overrides) -> MaintenanceComparison:
    defaults = dict(
        equipment_id=1, plant_id=1, plant_code="p01", instance_key="P01.UTILITY.CHL01", equipment_type="chiller",
        target_key="Power_kW", direction=LOWER_IS_BETTER, unit="kW", maintenance_log_id=1,
        performed_at="2026-09-10 08:00:00", pre_window=("2026-08-11 08:00:00", "2026-09-10 07:59:59"),
        post_window=("2026-09-11 08:00:00", "2026-09-20 12:00:00"), pre_value=80.0, post_value=60.0,
        absolute_change=-20.0, percent_change=-25.0, pre_sample_count=15, post_sample_count=10,
        evidence_quality=EVIDENCE_CONTEXT_MATCHED, effectiveness_result="IMPROVED", reason="test fixture",
        assumptions=["a"], limitations=[], computed_at=NOW.strftime("%Y-%m-%d %H:%M:%S"),
    )
    defaults.update(overrides)
    return MaintenanceComparison(**defaults)


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        _seed_config_db(self.config_db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_migration_is_additive_and_idempotent(self):
        connection = sqlite3.connect(self.config_db)
        before_tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        connection.close()

        pm.migrate(self.config_db, backup=False)
        pm.migrate(self.config_db, backup=False)  # idempotent - must not raise or duplicate

        connection = sqlite3.connect(self.config_db)
        after_tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        connection.close()

        self.assertTrue(before_tables.issubset(after_tables))
        self.assertIn("asset_performance_observations", after_tables)
        self.assertIn("asset_performance_maintenance_comparisons", after_tables)

    def test_backup_created_via_sqlite_api_not_raw_copy(self):
        source = Path("engine/performance_migrator.py").read_text()
        self.assertIn(".backup(destination)", source)
        self.assertNotIn("shutil.copy2(database", source)


class PerformancePersistenceTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        _seed_config_db(self.config_db)
        pm.migrate(self.config_db, backup=False)

    def tearDown(self):
        self.temp_dir.cleanup()


class TestPersistObservation(PerformancePersistenceTestBase):
    def test_insert_and_retrieve(self):
        observation = _observation()
        row_id = dom.persist_observation(self.config_db, observation, "initial", consecutive_degrading_observations=0, sustained_degradation=False, now=NOW)
        latest = dom.get_latest_observation(self.config_db, 1, "P01.UTILITY.CHL01", "Power_kW")
        self.assertEqual(latest["id"], row_id)
        self.assertEqual(latest["performance_state"], STATE_STABLE)
        self.assertEqual(latest["context_used"], {"hour_bucket": "14"})
        self.assertIsInstance(latest["participates_in_degradation"], bool)

    def test_history_ordering_chronological(self):
        for i in range(3):
            observation = _observation(computed_at=f"2026-09-{10+i:02d} 08:00:00", percent_change=float(i))
            dom.persist_observation(self.config_db, observation, "initial", consecutive_degrading_observations=0, sustained_degradation=False, now=NOW)
        history = dom.list_observations(self.config_db, 1, "P01.UTILITY.CHL01", "Power_kW")
        self.assertEqual(len(history), 3)
        self.assertEqual([h["percent_change"] for h in history], [0.0, 1.0, 2.0])  # oldest first

    def test_list_latest_for_plant_one_row_per_instance_target(self):
        dom.persist_observation(self.config_db, _observation(target_key="Power_kW"), "initial", 0, False, now=NOW)
        dom.persist_observation(self.config_db, _observation(target_key="cop"), "initial", 0, False, now=NOW)
        dom.persist_observation(self.config_db, _observation(target_key="Power_kW", percent_change=99.0), "material_change", 0, False, now=NOW)
        rows = dom.list_latest_observations_for_plant(self.config_db, 1)
        self.assertEqual(len(rows), 2)  # 2 distinct target_keys, not 3 rows
        power_row = next(r for r in rows if r["target_key"] == "Power_kW")
        self.assertEqual(power_row["percent_change"], 99.0)  # the LATEST one

    def test_insufficient_evidence_still_persists_a_real_row(self):
        """Never let equipment 'disappear' from history - mirrors Phase 12.2's own guarantee."""
        observation = _observation(observed_value=None, reference_value=None, percent_change=None, performance_state="INSUFFICIENT_EVIDENCE", evidence_quality="INSUFFICIENT")
        row_id = dom.persist_observation(self.config_db, observation, "initial", 0, False, now=NOW)
        latest = dom.get_latest_observation(self.config_db, 1, "P01.UTILITY.CHL01", "Power_kW")
        self.assertEqual(latest["id"], row_id)
        self.assertIsNone(latest["observed_value"])

    def test_recent_observations_bounded_by_limit(self):
        for i in range(10):
            dom.persist_observation(self.config_db, _observation(computed_at=f"2026-09-{10+i:02d} 08:00:00"), "initial", 0, False, now=NOW)
        recent = dom.recent_observations_for_consecutive_count(self.config_db, 1, "P01.UTILITY.CHL01", "Power_kW", limit=3)
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[0]["computed_at"], "2026-09-19 08:00:00")  # most-recent-first


class TestPersistMaintenanceComparison(PerformancePersistenceTestBase):
    def test_insert_and_retrieve(self):
        comparison = _comparison()
        row_id = dom.persist_maintenance_comparison(self.config_db, comparison, now=NOW)
        latest = dom.get_latest_maintenance_comparison(self.config_db, 1, "P01.UTILITY.CHL01", "Power_kW", 1)
        self.assertEqual(latest["id"], row_id)
        self.assertEqual(latest["effectiveness_result"], "IMPROVED")

    def test_re_evaluation_appends_never_updates(self):
        """Append-only - re-evaluating the same maintenance event with new
        evidence adds a NEW row, never overwrites the prior one."""
        dom.persist_maintenance_comparison(self.config_db, _comparison(post_sample_count=5), now=NOW)
        dom.persist_maintenance_comparison(self.config_db, _comparison(post_sample_count=10), now=NOW)
        history = dom.list_maintenance_comparisons_for_instance(self.config_db, 1, "P01.UTILITY.CHL01")
        self.assertEqual(len(history), 2)

    def test_list_latest_for_plant_one_row_per_instance_target(self):
        dom.persist_maintenance_comparison(self.config_db, _comparison(target_key="Power_kW"), now=NOW)
        dom.persist_maintenance_comparison(self.config_db, _comparison(target_key="cop"), now=NOW)
        rows = dom.list_latest_maintenance_comparisons_for_plant(self.config_db, 1)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
