import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from database.database import DatabaseManager
from engine import health_domain as dom
from engine import health_engine as he
from engine import health_history as hist
from engine import health_orchestration as orch
from engine.health_migrator import migrate as migrate_health
from engine.health_persistence_targets import HEARTBEAT_MAX_INTERVAL_HOURS, SCORE_CHANGE_THRESHOLD

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _plant_id
from tests.test_health_engine import (
    NOW, _seed_anomaly, _seed_equipment, _seed_event, _seed_mature_baseline, _create_machine_events_table,
)
from engine.equipment_metadata_migrator import migrate as migrate_equipment_metadata


class HealthPersistenceTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        migrate_equipment_metadata(self.config_db, backup=False)
        migrate_health(self.config_db, backup=False)
        _create_machine_events_table(self.machine_db)
        DatabaseManager(db_path=self.machine_db)
        self.plant_id = _plant_id(self.config_db, "p01")
        self.equipment_id = _seed_equipment(self.config_db, "p01_pump_wsp01")
        _insert_tag(self.config_db, "P01.WATER.WSP01.Vibration", unit="mm/s", measurement="vibration")
        _insert_tag(self.config_db, "P01.WATER.WSP01.BearingTemp", unit="degC", measurement="temperature")
        _insert_tag(self.config_db, "P01.WATER.WSP01.Power_kW", unit="kW", measurement="power")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_all_factors_clean(self, confidence="High"):
        for target in ("Vibration", "BearingTemp", "Power_kW", "Flow", "delta_p_bar", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence=confidence)

    def _calculate(self, now=None):
        return he.calculate_health(self.config_db, self.machine_db, self.plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01", now=now or NOW)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

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
        before_anomalies = connection.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
        connection.close()

        migrate_health(self.config_db, backup=False)
        migrate_health(self.config_db, backup=False)  # idempotent - must not raise or duplicate

        connection = sqlite3.connect(self.config_db)
        after_tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        after_anomalies = connection.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
        connection.close()

        self.assertTrue(before_tables.issubset(after_tables))  # nothing removed
        self.assertIn("equipment_health_snapshots", after_tables)
        self.assertIn("equipment_health_factor_snapshots", after_tables)
        self.assertEqual(before_anomalies, after_anomalies)  # Phase 9 data untouched

    def test_backup_created_via_sqlite_api_not_raw_copy(self):
        source = Path("engine/health_migrator.py").read_text()
        self.assertIn(".backup(destination)", source)
        self.assertNotIn("shutil.copy2(database", source)  # no actual shutil.copy2 call site


# ---------------------------------------------------------------------------
# Persistence atomicity / basic correctness
# ---------------------------------------------------------------------------

class TestPersistSnapshot(HealthPersistenceTestBase):
    def test_first_assessment_persists_with_linked_factors(self):
        self._seed_all_factors_clean()
        result = self._calculate()
        snapshot_id = dom.persist_snapshot(self.config_db, result, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")
        self.assertEqual(latest["id"], snapshot_id)
        self.assertEqual(latest["health_score"], result.health_score)
        factors = dom.get_factor_snapshots(self.config_db, snapshot_id)
        self.assertEqual(len(factors), len(result.factor_results))
        self.assertTrue(all(f["health_snapshot_id"] == snapshot_id for f in factors))

    def test_insufficient_assessment_persists_with_null_score(self):
        result = self._calculate()  # no evidence seeded at all -> INSUFFICIENT
        self.assertIsNone(result.health_score)
        snapshot_id = dom.persist_snapshot(self.config_db, result, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")
        self.assertIsNone(latest["health_score"])
        self.assertIsNone(latest["health_band"])
        self.assertEqual(latest["coverage_status"], "INSUFFICIENT")
        self.assertEqual(latest["id"], snapshot_id)  # a real row exists, not "equipment disappears"

    def test_provisional_flag_persists(self):
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "water_supply_pump", confidence="Low")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "BearingTemp", "water_supply_pump", confidence="Low")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "flow_per_kw", "water_supply_pump", confidence="Low")
        result = self._calculate()
        self.assertTrue(result.provisional)
        dom.persist_snapshot(self.config_db, result, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")
        self.assertEqual(latest["provisional"], 1)

    def test_model_version_and_provenance_persisted(self):
        self._seed_all_factors_clean()
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")
        result = self._calculate()
        snapshot_id = dom.persist_snapshot(self.config_db, result, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")
        self.assertEqual(latest["health_model_version"], result.health_model_version)
        factors = dom.get_factor_snapshots(self.config_db, snapshot_id)
        penalized = next(f for f in factors if f["status"] == "penalized")
        self.assertEqual(penalized["provenance"], "SIMULATION_TUNING")

    def test_transaction_rollback_prevents_partial_snapshot(self):
        self._seed_all_factors_clean()
        result = self._calculate()
        self.assertGreaterEqual(len(result.factor_results), 2)
        # A genuine NOT NULL constraint violation on the SECOND factor row
        # (the first factor row - and the parent snapshot row - are
        # already inserted in this same transaction by that point) -
        # no mocking needed, a real sqlite3.IntegrityError mid-transaction.
        result.factor_results[1].family = None

        with self.assertRaises(sqlite3.IntegrityError):
            dom.persist_snapshot(self.config_db, result, "initial")

        # No partial state: neither the parent snapshot nor any factor row survives.
        self.assertIsNone(dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01"))
        connection = sqlite3.connect(self.config_db)
        count = connection.execute("SELECT COUNT(*) FROM equipment_health_factor_snapshots").fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_historical_row_does_not_depend_on_current_registry(self):
        """item 10/11 - a persisted factor's max_penalty/reason must be
        readable without consulting today's health_targets registry."""
        self._seed_all_factors_clean()
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")
        result = self._calculate()
        snapshot_id = dom.persist_snapshot(self.config_db, result, "initial")
        original_max_penalty = next(f.max_penalty for f in result.factor_results if f.factor_id == "wsp_vibration_condition")

        # Simulate a FUTURE registry change (Phase 12.1's own methodology
        # evolving) - the stored historical row must be unaffected.
        with patch("engine.health_targets.FAMILY_MAX_PENALTY", {**he.FAMILY_MAX_PENALTY, "CONDITION": 5.0}):
            factors = dom.get_factor_snapshots(self.config_db, snapshot_id)
            stored = next(f for f in factors if f["factor_id"] == "wsp_vibration_condition")
            self.assertEqual(stored["maximum_penalty"], original_max_penalty)  # unaffected by the patched-in future registry


# ---------------------------------------------------------------------------
# Change detection (item 7)
# ---------------------------------------------------------------------------

class TestChangeDetection(HealthPersistenceTestBase):
    def test_identical_calculation_does_not_spam_duplicates(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial")
        result2 = self._calculate(now=NOW + timedelta(minutes=5))
        changed, reason = orch.has_material_change(self.config_db, result2, dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01"))
        self.assertFalse(changed)
        self.assertEqual(reason, "unchanged")

    def test_score_change_above_threshold_persists(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")

        result2 = self._calculate()
        result2.health_score = latest["health_score"] - (SCORE_CHANGE_THRESHOLD + 1)
        changed, reason = orch.has_material_change(self.config_db, result2, latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "score_changed")

    def test_score_change_below_threshold_does_not_persist(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")

        result2 = self._calculate()
        result2.health_score = latest["health_score"] - (SCORE_CHANGE_THRESHOLD - 0.5)
        result2.health_band = latest["health_band"]
        changed, reason = orch.has_material_change(self.config_db, result2, latest)
        self.assertFalse(changed)

    def test_band_change_persists(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")

        result2 = self._calculate()
        result2.health_band = "ATTENTION" if latest["health_band"] != "ATTENTION" else "MONITOR"
        changed, reason = orch.has_material_change(self.config_db, result2, latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "band_changed")

    def test_confidence_change_persists(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")

        result2 = self._calculate()
        result2.assessment_confidence = "LOW" if latest["assessment_confidence"] != "LOW" else "MEDIUM"
        changed, reason = orch.has_material_change(self.config_db, result2, latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "confidence_changed")

    def test_coverage_change_persists(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")

        result2 = self._calculate()
        result2.coverage_status = "LOW" if latest["coverage_status"] != "LOW" else "MEDIUM"
        changed, reason = orch.has_material_change(self.config_db, result2, latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "coverage_changed")

    def test_provisional_change_persists(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")

        result2 = self._calculate()
        result2.provisional = not bool(latest["provisional"])
        changed, reason = orch.has_material_change(self.config_db, result2, latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "provisional_changed")

    def test_factor_composition_change_persists(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")

        # A genuinely new anomaly appears - real evidence-driven change, not a manual field edit.
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="WARNING", confidence="High")
        result2 = self._calculate()
        changed, reason = orch.has_material_change(self.config_db, result2, latest)
        self.assertTrue(changed)
        self.assertIn(reason, ("factor_composition_changed", "score_changed", "band_changed"))  # whichever fires first, deterministically

    def test_became_insufficient_persists(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")

        result2 = self._calculate()
        result2.health_score = None
        changed, reason = orch.has_material_change(self.config_db, result2, latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "became_insufficient")

    def test_recovered_from_insufficient_persists(self):
        result1 = self._calculate()  # no evidence -> INSUFFICIENT
        dom.persist_snapshot(self.config_db, result1, "initial")
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")
        self.assertIsNone(latest["health_score"])

        self._seed_all_factors_clean()
        result2 = self._calculate()
        self.assertIsNotNone(result2.health_score)
        changed, reason = orch.has_material_change(self.config_db, result2, latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "recovered_from_insufficient")


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat(HealthPersistenceTestBase):
    def test_unchanged_state_does_not_persist_before_heartbeat_interval(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial", now=NOW)
        outcome = orch.evaluate_and_maybe_persist(
            self.config_db, self.machine_db, self.plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01",
            now=NOW + timedelta(hours=HEARTBEAT_MAX_INTERVAL_HOURS - 1),
        )
        self.assertEqual(outcome["action"], "unchanged")

    def test_unchanged_state_persists_at_heartbeat_interval(self):
        self._seed_all_factors_clean()
        result1 = self._calculate()
        dom.persist_snapshot(self.config_db, result1, "initial", now=NOW)
        outcome = orch.evaluate_and_maybe_persist(
            self.config_db, self.machine_db, self.plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01",
            now=NOW + timedelta(hours=HEARTBEAT_MAX_INTERVAL_HOURS + 1),
        )
        self.assertEqual(outcome["action"], "persisted")
        self.assertEqual(outcome["change_reason"], "heartbeat")


# ---------------------------------------------------------------------------
# Restart safety / duplicate-explosion / failure isolation
# ---------------------------------------------------------------------------

class TestWorkerCycle(HealthPersistenceTestBase):
    def test_restart_does_not_duplicate_snapshots(self):
        self._seed_all_factors_clean()
        first = orch.run_cycle(self.config_db, self.machine_db, now=NOW)
        second = orch.run_cycle(self.config_db, self.machine_db, now=NOW + timedelta(minutes=1))
        self.assertGreater(first["persisted"], 0)
        self.assertEqual(second["persisted"], 0)
        self.assertEqual(second["unchanged"], second["evaluated"])

    def test_one_equipment_failure_does_not_stop_the_cycle(self):
        self._seed_all_factors_clean()
        second_equipment_id = _seed_equipment(self.config_db, "p01_pump_wsp02")
        _insert_tag(self.config_db, "P01.WATER.WSP02.Vibration", unit="mm/s", measurement="vibration")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP02", "Vibration", "water_supply_pump", confidence="High")

        real_calculate = he.calculate_health

        def _flaky(config_db, machine_db, plant_id, plant_code, equipment_type, instance_key, now=None):
            if instance_key == "P01.WATER.WSP01":
                raise RuntimeError("simulated calculation failure")
            return real_calculate(config_db, machine_db, plant_id, plant_code, equipment_type, instance_key, now=now)

        with patch("engine.health_orchestration.he.calculate_health", side_effect=_flaky):
            summary = orch.run_cycle(self.config_db, self.machine_db, now=NOW)

        self.assertGreaterEqual(summary["errors"], 1)
        self.assertGreater(summary["evaluated"], 0)  # WSP02 still processed
        self.assertIsNone(dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01"))
        self.assertIsNotNone(dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP02"))

    def test_plant_isolation_p01_p02(self):
        self._seed_all_factors_clean()
        p2_id = _plant_id(self.config_db, "p02")
        _seed_equipment(self.config_db, "p02_pump_wsp01")
        _insert_tag(self.config_db, "P02.WATER.WSP01.Vibration", unit="mm/s", measurement="vibration")
        _seed_mature_baseline(self.config_db, p2_id, "P02.WATER.WSP01", "Vibration", "water_supply_pump", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")

        orch.run_cycle(self.config_db, self.machine_db, now=NOW)

        p1_latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")
        p2_latest = dom.get_latest_snapshot(self.config_db, p2_id, "P02.WATER.WSP01")
        self.assertNotEqual(p1_latest["health_score"], p2_latest["health_score"])


# ---------------------------------------------------------------------------
# Historical query / trend API
# ---------------------------------------------------------------------------

class TestHistoricalQuery(HealthPersistenceTestBase):
    def test_history_returns_chronological_data_with_null_scores_preserved(self):
        insufficient = self._calculate(now=NOW)
        dom.persist_snapshot(self.config_db, insufficient, "initial", now=NOW)

        self._seed_all_factors_clean()
        scored = self._calculate(now=NOW + timedelta(days=1))
        dom.persist_snapshot(self.config_db, scored, "recovered_from_insufficient", now=NOW + timedelta(days=1))

        history = hist.get_history(self.config_db, self.plant_id, "P01.WATER.WSP01", days=30, now=NOW + timedelta(days=1))
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[0]["health_score"])   # NULL preserved, never coerced
        self.assertIsNotNone(history[1]["health_score"])
        self.assertEqual([h["computed_at"] for h in history], sorted(h["computed_at"] for h in history))

    def test_factor_detail_retrieval(self):
        self._seed_all_factors_clean()
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")
        result = self._calculate()
        snapshot_id = dom.persist_snapshot(self.config_db, result, "initial")
        factors = hist.get_factor_detail(self.config_db, snapshot_id)
        self.assertEqual(len(factors), len(result.factor_results))
        penalized = next(f for f in factors if f["status"] == "penalized")
        self.assertGreater(penalized["penalty"], 0)


class TestTrendAndTransitions(HealthPersistenceTestBase):
    def test_deteriorating_trend(self):
        self._seed_all_factors_clean()
        healthy = self._calculate(now=NOW)
        dom.persist_snapshot(self.config_db, healthy, "initial", now=NOW)

        worse = self._calculate(now=NOW + timedelta(days=3))
        worse.health_score = (healthy.health_score or 100) - 20
        dom.persist_snapshot(self.config_db, worse, "score_changed", now=NOW + timedelta(days=3))

        trend = hist.compute_trend(self.config_db, self.plant_id, "P01.WATER.WSP01", now=NOW + timedelta(days=3))
        self.assertEqual(trend["direction"], "DETERIORATING")
        self.assertLess(trend["absolute_change"], 0)

    def test_insufficient_history_never_fabricates_a_trend(self):
        result = self._calculate(now=NOW)
        dom.persist_snapshot(self.config_db, result, "initial", now=NOW)
        trend = hist.compute_trend(self.config_db, self.plant_id, "P01.WATER.WSP01", now=NOW)
        self.assertEqual(trend["direction"], "INSUFFICIENT_HISTORY")

    def test_band_transition_recorded(self):
        self._seed_all_factors_clean()
        first = self._calculate(now=NOW)
        dom.persist_snapshot(self.config_db, first, "initial", now=NOW)
        second = self._calculate(now=NOW + timedelta(days=1))
        second.health_band = "ATTENTION" if first.health_band != "ATTENTION" else "MONITOR"
        dom.persist_snapshot(self.config_db, second, "band_changed", now=NOW + timedelta(days=1))

        transitions = hist.band_transitions(self.config_db, self.plant_id, "P01.WATER.WSP01", now=NOW + timedelta(days=1))
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["to_band"], second.health_band)

    def test_confidence_transition_independent_of_score(self):
        self._seed_all_factors_clean()
        first = self._calculate(now=NOW)
        dom.persist_snapshot(self.config_db, first, "initial", now=NOW)
        second = self._calculate(now=NOW + timedelta(days=1))
        second.health_score = first.health_score  # score UNCHANGED
        second.assessment_confidence = "LOW" if first.assessment_confidence != "LOW" else "MEDIUM"
        dom.persist_snapshot(self.config_db, second, "confidence_changed", now=NOW + timedelta(days=1))

        conf_transitions = hist.confidence_transitions(self.config_db, self.plant_id, "P01.WATER.WSP01", now=NOW + timedelta(days=1))
        score_trend = hist.compute_trend(self.config_db, self.plant_id, "P01.WATER.WSP01", now=NOW + timedelta(days=1))
        self.assertEqual(len(conf_transitions), 1)
        self.assertEqual(score_trend["direction"], "STABLE")  # score trend unaffected by the confidence-only change


# ---------------------------------------------------------------------------
# Engine unaffected by persistence layer / no rescans / no LLM / no PLC
# ---------------------------------------------------------------------------

class TestEngineIndependence(HealthPersistenceTestBase):
    def test_calculate_health_result_unchanged_by_persisting(self):
        self._seed_all_factors_clean()
        before = self._calculate()
        dom.persist_snapshot(self.config_db, before, "initial")
        after = self._calculate()
        self.assertEqual(before.health_score, after.health_score)
        self.assertEqual(before.factor_results[0].contribution, after.factor_results[0].contribution)


class TestSourceInspection(unittest.TestCase):
    def test_no_rescans_no_llm_no_plc_no_ai_prose(self):
        for path in (
            "engine/health_migrator.py", "engine/health_domain.py", "engine/health_orchestration.py",
            "engine/health_history.py", "engine/health_persistence_targets.py", "app/equipment_health_worker.py",
        ):
            source = Path(path).read_text()
            for banned in (
                "import ai.", "from ai.", "ai_provider", "plc.driver_factory", "opcua", "modbus",
                "write_tag", "set_setpoint", "reset_alarm", "SELECT * FROM plc_data", "FROM plc_data",
                "work_order", "maintenance_recommendation",
            ):
                self.assertNotIn(banned, source, msg=f"found banned reference {banned!r} in {path}")

    def test_phase_12_1_engine_untouched_source_wise(self):
        # Confirm no accidental edits crept into the accepted Phase 12.1
        # files while building 12.2 (a lightweight sentinel, not a full diff).
        for path in ("engine/health_targets.py", "engine/health_evidence.py", "engine/health_engine.py"):
            source = Path(path).read_text()
            self.assertIn("Phase 12.1", source)

    def test_no_automatic_backfill_language_or_calls(self):
        source = Path("app/equipment_health_worker.py").read_text() + Path("engine/health_orchestration.py").read_text()
        for banned in ("backfill",):
            self.assertNotIn(banned, source.lower())


if __name__ == "__main__":
    unittest.main()
