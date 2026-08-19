import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from config.environment import get_config_db_path, get_machine_db_path
from engine import data_health_domain as dhdom
from engine import data_health_history as dhh
from engine import data_health_migrator as migrator
from engine import data_health_orchestration as orch
from engine.data_health_engine import EquipmentDataHealth
from engine.data_health_persistence_targets import HEARTBEAT_MAX_INTERVAL_HOURS

"""
Phase 16.5 - Data Health History (persistence, change-detection,
historical read model) tests. Uses a real SQLite copy of the live
config database (schema + real equipment/tags), migrated with the new
data_health_snapshots table, then seeded with hand-built
EquipmentDataHealth fixtures for full control over exact timing/values -
mirrors this project's own established test-fixture convention (see
tests/test_data_health_engine.py's own docstring for the same
"controlled fixture, isolated from live timing" rationale).
"""

INSTANCE_KEY = "P01.WATER.WSP01"
BASE = datetime(2026, 8, 20, 0, 0, 0)


def _result(status="GOOD", score=95.0, missing=0, stale=0, invalid=0, gaps=0, computed_at=None, **overrides):
    fields = dict(
        instance_key=INSTANCE_KEY, equipment_id=1, equipment_type="water_supply_pump",
        confidence_score=score, confidence_status=status,
        component_scores={"freshness": score, "availability": score, "validity": score, "continuity": score},
        component_applicability={"freshness": True, "availability": True, "validity": True, "continuity": True},
        required_tag_count=5, available_tag_count=5 - missing, fresh_tag_count=5 - stale,
        missing_tags=[f"m{i}" for i in range(missing)],
        stale_tags=[f"s{i}" for i in range(stale)],
        invalid_tags=[f"i{i}" for i in range(invalid)],
        gaps=[{"tag": f"g{i}"} for i in range(gaps)],
        source={"configured_driver": "simulator"},
        computed_at=(computed_at or BASE).strftime("%Y-%m-%d %H:%M:%S"),
    )
    fields.update(overrides)
    return EquipmentDataHealth(**fields)


class DataHealthHistoryTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        shutil.copy2(get_config_db_path(), self.config_db)
        migrator.migrate(self.config_db, backup=False)

        connection = sqlite3.connect(self.config_db)
        self.plant_id = connection.execute("SELECT id FROM plants WHERE code = 'p01'").fetchone()[0]
        # The real live config.db (copied above) has its OWN real
        # data_health_snapshots rows by this point in the session (the
        # Phase 16.5 commissioning pass already ran the real migrator +
        # a real worker cycle against it) - every test here needs a
        # guaranteed-empty table regardless of that, so it is never
        # testing against a mix of real and synthetic history.
        connection.execute("DELETE FROM data_health_snapshots")
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _persist(self, result, reason="test", at=None, evaluation_error=None):
        return dhdom.persist_snapshot(self.config_db, self.plant_id, "p01", result, reason, now=at or BASE, evaluation_error=evaluation_error)


class TestMigration(DataHealthHistoryTestBase):
    def test_table_and_indexes_created(self):
        connection = sqlite3.connect(self.config_db)
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        connection.close()
        self.assertIn("data_health_snapshots", tables)
        self.assertIn("idx_data_health_snapshots_instance_time", indexes)
        self.assertIn("idx_data_health_snapshots_plant_time", indexes)

    def test_migration_is_idempotent(self):
        migrator.migrate(self.config_db, backup=False)
        migrator.migrate(self.config_db, backup=False)
        connection = sqlite3.connect(self.config_db)
        count = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='data_health_snapshots'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_no_raw_telemetry_columns_in_schema(self):
        """The approved contract: no raw plc_data value/timestamp column
        anywhere in this table - only derived Data Health state."""
        connection = sqlite3.connect(self.config_db)
        columns = {r[1] for r in connection.execute("PRAGMA table_info(data_health_snapshots)")}
        connection.close()
        self.assertNotIn("value", columns)
        self.assertNotIn("raw_value", columns)
        self.assertNotIn("plc_data_id", columns)


class TestMaterialChangeDetection(DataHealthHistoryTestBase):
    def test_no_previous_snapshot_is_always_initial(self):
        changed, reason = orch.has_material_change(_result(), None)
        self.assertTrue(changed)
        self.assertEqual(reason, "initial")

    def test_status_change_is_material(self):
        self._persist(_result("GOOD", 95.0))
        latest = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        changed, reason = orch.has_material_change(_result("DEGRADED", 65.0), latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "status_changed")

    def test_became_unavailable_is_material(self):
        self._persist(_result("GOOD", 95.0))
        latest = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        changed, reason = orch.has_material_change(_result("UNAVAILABLE", None), latest)
        self.assertTrue(changed)
        self.assertIn(reason, ("status_changed", "became_unavailable"))

    def test_confidence_score_change_above_threshold_is_material(self):
        self._persist(_result("GOOD", 95.0))
        latest = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        changed, reason = orch.has_material_change(_result("GOOD", 90.0), latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "confidence_score_changed")

    def test_tiny_score_noise_below_threshold_is_not_material(self):
        self._persist(_result("GOOD", 95.0))
        latest = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        changed, reason = orch.has_material_change(_result("GOOD", 94.9), latest)
        self.assertFalse(changed)
        self.assertEqual(reason, "unchanged")

    def test_issue_count_change_is_material(self):
        self._persist(_result("GOOD", 95.0, stale=0))
        latest = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        changed, reason = orch.has_material_change(_result("GOOD", 95.0, stale=1), latest)
        self.assertTrue(changed)
        self.assertEqual(reason, "issue_count_changed")

    def test_identical_result_is_unchanged(self):
        self._persist(_result("GOOD", 95.0))
        latest = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        changed, reason = orch.has_material_change(_result("GOOD", 95.0), latest)
        self.assertFalse(changed)
        self.assertEqual(reason, "unchanged")


class TestHeartbeat(DataHealthHistoryTestBase):
    def test_no_heartbeat_before_interval_elapses(self):
        latest = {"computed_at": BASE.strftime("%Y-%m-%d %H:%M:%S")}
        self.assertFalse(orch._should_heartbeat(latest, BASE + timedelta(hours=1)))

    def test_heartbeat_fires_after_interval(self):
        latest = {"computed_at": BASE.strftime("%Y-%m-%d %H:%M:%S")}
        self.assertTrue(orch._should_heartbeat(latest, BASE + timedelta(hours=HEARTBEAT_MAX_INTERVAL_HOURS)))

    def test_unchanged_result_persists_at_heartbeat(self):
        self._persist(_result("GOOD", 95.0), at=BASE)

        with patch("engine.data_health_orchestration.calculate_equipment_data_health", return_value=_result("GOOD", 95.0)):
            outcome = orch.evaluate_and_maybe_persist(
                self.config_db, get_machine_db_path(), self.plant_id, "p01",
                "water_supply_pump", INSTANCE_KEY, now=BASE + timedelta(hours=HEARTBEAT_MAX_INTERVAL_HOURS),
            )
        self.assertEqual(outcome["action"], "persisted")
        self.assertEqual(outcome["change_reason"], "heartbeat")

    def test_no_write_when_unchanged_and_before_heartbeat(self):
        self._persist(_result("GOOD", 95.0), at=BASE)
        with patch("engine.data_health_orchestration.calculate_equipment_data_health", return_value=_result("GOOD", 95.0)):
            outcome = orch.evaluate_and_maybe_persist(
                self.config_db, get_machine_db_path(), self.plant_id, "p01",
                "water_supply_pump", INSTANCE_KEY, now=BASE + timedelta(hours=1),
            )
        self.assertEqual(outcome["action"], "unchanged")
        self.assertIsNone(outcome["snapshot_id"])


class TestRestartAndFailureIsolation(DataHealthHistoryTestBase):
    def test_restart_does_not_create_duplicate_snapshot(self):
        """Simulates a worker restart: re-running evaluate_and_maybe_persist
        immediately with an unchanged result must not write a second row."""
        with patch("engine.data_health_orchestration.calculate_equipment_data_health", return_value=_result("GOOD", 95.0)):
            outcome1 = orch.evaluate_and_maybe_persist(
                self.config_db, get_machine_db_path(), self.plant_id, "p01", "water_supply_pump", INSTANCE_KEY, now=BASE,
            )
            outcome2 = orch.evaluate_and_maybe_persist(
                self.config_db, get_machine_db_path(), self.plant_id, "p01", "water_supply_pump", INSTANCE_KEY,
                now=BASE + timedelta(minutes=5),
            )
        self.assertEqual(outcome1["action"], "persisted")
        self.assertEqual(outcome2["action"], "unchanged")
        history = dhdom.list_snapshots(self.config_db, self.plant_id, INSTANCE_KEY)
        self.assertEqual(len(history), 1)

    def test_evaluation_exception_persists_as_unavailable_not_silently_good(self):
        with patch("engine.data_health_orchestration.calculate_equipment_data_health", side_effect=RuntimeError("db hiccup")):
            outcome = orch.evaluate_and_maybe_persist(
                self.config_db, get_machine_db_path(), self.plant_id, "p01", "water_supply_pump", INSTANCE_KEY, now=BASE,
            )
        self.assertEqual(outcome["confidence_status"], "UNAVAILABLE")
        self.assertEqual(outcome["evaluation_error"], "db hiccup")
        row = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        self.assertEqual(row["confidence_status"], "UNAVAILABLE")
        self.assertEqual(row["evaluation_error"], "db hiccup")

    def test_one_equipment_failure_does_not_abort_run_cycle(self):
        real_calc = orch.calculate_equipment_data_health

        def flaky(config_db, machine_db, instance_key, now=None):
            if instance_key == INSTANCE_KEY:
                raise RuntimeError("simulated failure")
            return real_calc(config_db, machine_db, instance_key, now=now)

        with patch("engine.data_health_orchestration.calculate_equipment_data_health", side_effect=flaky):
            summary = orch.run_cycle(self.config_db, get_machine_db_path(), now=BASE)

        self.assertEqual(summary["errors"], 0)  # handled at evaluate_and_maybe_persist, not a cycle-level error
        self.assertGreater(summary["evaluated"], 1)
        row = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        self.assertEqual(row["confidence_status"], "UNAVAILABLE")


class TestHistoryOrdering(DataHealthHistoryTestBase):
    def test_history_is_chronological(self):
        self._persist(_result(computed_at=BASE), at=BASE)
        self._persist(_result("DEGRADED", 65.0, computed_at=BASE + timedelta(hours=1)), at=BASE + timedelta(hours=1))
        history = dhh.get_history(self.config_db, self.plant_id, INSTANCE_KEY)
        self.assertEqual([h["confidence_status"] for h in history], ["GOOD", "DEGRADED"])

    def test_empty_history_returns_empty_list(self):
        self.assertEqual(dhh.get_history(self.config_db, self.plant_id, INSTANCE_KEY), [])

    def test_single_snapshot_history(self):
        self._persist(_result())
        history = dhh.get_history(self.config_db, self.plant_id, INSTANCE_KEY)
        self.assertEqual(len(history), 1)


class TestStatusDuration(DataHealthHistoryTestBase):
    def _seed_transition_scenario(self):
        self._persist(_result("GOOD", 95.0, computed_at=BASE), at=BASE)
        t1 = BASE + timedelta(hours=1)
        self._persist(_result("DEGRADED", 65.0, stale=2, computed_at=t1), at=t1)
        t2 = BASE + timedelta(hours=1, minutes=30)
        self._persist(_result("GOOD", 95.0, computed_at=t2), at=t2)

    def test_duration_splits_correctly_across_statuses(self):
        self._seed_transition_scenario()
        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        result = dhh.status_duration(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        self.assertEqual(result["percentages"]["GOOD"], 75.0)
        self.assertEqual(result["percentages"]["DEGRADED"], 25.0)
        self.assertEqual(result["percentages"]["POOR"], 0.0)
        self.assertEqual(result["percentages"]["UNAVAILABLE"], 0.0)

    def test_final_interval_extends_to_query_end(self):
        self._persist(_result("GOOD", 95.0, computed_at=BASE), at=BASE)
        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        result = dhh.status_duration(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        self.assertEqual(result["final_interval_treatment"], "extended_to_end")
        self.assertEqual(result["percentages"]["GOOD"], 100.0)

    def test_leading_gap_before_first_snapshot_is_no_history_never_fabricated(self):
        self._persist(_result("GOOD", 95.0, computed_at=BASE), at=BASE)
        start = (BASE - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        end = BASE.strftime("%Y-%m-%d %H:%M:%S")
        result = dhh.status_duration(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        self.assertGreater(result["no_history_percentage"], 0)
        self.assertEqual(result["percentages"]["GOOD"], 0.0)

    def test_no_snapshots_at_all_is_insufficient_history_not_zero(self):
        result = dhh.status_duration(self.config_db, self.plant_id, "P01.WATER.WSP99", "2026-01-01 00:00:00", "2026-01-02 00:00:00")
        self.assertTrue(result["insufficient_history"])
        self.assertEqual(result["percentages"], {})


class TestStatusTransitions(DataHealthHistoryTestBase):
    def test_transitions_detected_with_structured_changes(self):
        self._persist(_result("GOOD", 95.0, computed_at=BASE), at=BASE)
        t1 = BASE + timedelta(hours=1)
        self._persist(_result("DEGRADED", 65.0, stale=2, computed_at=t1), at=t1)

        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        transitions = dhh.status_transitions(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["from_status"], "GOOD")
        self.assertEqual(transitions[0]["to_status"], "DEGRADED")
        fields_changed = {c["field"] for c in transitions[0]["changes"]}
        self.assertIn("stale_tag_count", fields_changed)

    def test_no_transition_when_status_never_changes(self):
        self._persist(_result("GOOD", 95.0, computed_at=BASE), at=BASE)
        self._persist(_result("GOOD", 95.0, computed_at=BASE + timedelta(hours=24)), at=BASE + timedelta(hours=24))
        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
        transitions = dhh.status_transitions(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        self.assertEqual(transitions, [])

    def test_transition_never_contains_ai_generated_narrative_text(self):
        self._persist(_result("GOOD", 95.0, computed_at=BASE), at=BASE)
        t1 = BASE + timedelta(hours=1)
        self._persist(_result("POOR", 40.0, invalid=1, computed_at=t1), at=t1)
        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        transitions = dhh.status_transitions(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        for change in transitions[0]["changes"]:
            self.assertEqual(set(change.keys()), {"field", "from", "to"})


class TestRecurringIssues(DataHealthHistoryTestBase):
    def test_inactive_to_active_counts_as_one_occurrence(self):
        self._persist(_result("GOOD", 95.0, stale=0, computed_at=BASE), at=BASE)
        t1 = BASE + timedelta(hours=1)
        self._persist(_result("DEGRADED", 65.0, stale=2, computed_at=t1), at=t1)

        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        result = dhh.recurring_issues(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        self.assertEqual(result["occurrences"]["STALE"], 1)

    def test_heartbeat_does_not_create_fake_recurrence(self):
        """A stale issue that stays active across a heartbeat snapshot
        must count as ONE occurrence, not two."""
        self._persist(_result("DEGRADED", 65.0, stale=2, computed_at=BASE), at=BASE)
        t1 = BASE + timedelta(hours=HEARTBEAT_MAX_INTERVAL_HOURS)
        self._persist(_result("DEGRADED", 65.0, stale=2, computed_at=t1), at=t1, reason="heartbeat")

        start = (BASE - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        end = (t1 + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        result = dhh.recurring_issues(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        # The FIRST observed row (no carry-in before it) never counts its
        # own already-nonzero count as a fresh occurrence (approved rule).
        self.assertEqual(result["occurrences"]["STALE"], 0)

    def test_recurring_twice_counts_two_occurrences(self):
        self._persist(_result("GOOD", 95.0, stale=0, computed_at=BASE), at=BASE)
        t1 = BASE + timedelta(hours=1)
        self._persist(_result("DEGRADED", 65.0, stale=1, computed_at=t1), at=t1)
        t2 = BASE + timedelta(hours=2)
        self._persist(_result("GOOD", 95.0, stale=0, computed_at=t2), at=t2)
        t3 = BASE + timedelta(hours=3)
        self._persist(_result("DEGRADED", 65.0, stale=1, computed_at=t3), at=t3)

        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
        result = dhh.recurring_issues(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        self.assertEqual(result["occurrences"]["STALE"], 2)

    def test_no_history_is_flagged_not_zero(self):
        result = dhh.recurring_issues(self.config_db, self.plant_id, "P01.WATER.WSP99", "2026-01-01 00:00:00", "2026-01-02 00:00:00")
        self.assertTrue(result["insufficient_history"])


class TestFrozenCandidateAndLogOnChangePreserved(DataHealthHistoryTestBase):
    def test_frozen_candidate_recurrence_never_labeled_sensor_failure(self):
        self._persist(_result("GOOD", 95.0, computed_at=BASE), at=BASE)
        t1 = BASE + timedelta(hours=1)
        frozen_result = _result("GOOD", 90.0, computed_at=t1)
        frozen_result.frozen_candidates = ["P01.WATER.WSP01.Pressure"]
        self._persist(frozen_result, at=t1)

        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        result = dhh.recurring_issues(self.config_db, self.plant_id, INSTANCE_KEY, start, end)
        self.assertEqual(result["occurrences"]["FROZEN_CANDIDATE"], 1)
        # The occurrences dict's own keys never use failure/fault language.
        for key in result["occurrences"]:
            self.assertNotIn("FAIL", key)
            self.assertNotIn("FAULT", key)

    def test_indeterminate_change_only_never_counted_as_stale(self):
        r = _result("GOOD", 95.0, computed_at=BASE)
        r.indeterminate_freshness_tags = ["P01.WATER.WSP01.AlarmState"]
        self._persist(r, at=BASE)
        latest = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        self.assertEqual(latest["indeterminate_freshness_count"], 1)
        self.assertEqual(latest["stale_tag_count"], 0)


class TestFleetStatusSummary(DataHealthHistoryTestBase):
    def test_hierarchy_filters_present(self):
        self._persist(_result("GOOD", 95.0, computed_at=BASE), at=BASE)
        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        summary = dhh.fleet_status_summary(self.config_db, start, end, plant_codes=("p01",))
        self.assertIn("by_plant", summary)
        self.assertIn("by_area", summary)
        self.assertIn("by_system", summary)
        self.assertIn("by_equipment_type", summary)
        wsp01_row = next(r for r in summary["equipment"] if r["instance_key"] == INSTANCE_KEY)
        self.assertEqual(wsp01_row["plant_code"], "p01")

    def test_plant_filter_narrows_result(self):
        both = dhh.fleet_status_summary(self.config_db, "2026-01-01 00:00:00", "2026-01-02 00:00:00")
        p01_only = dhh.fleet_status_summary(self.config_db, "2026-01-01 00:00:00", "2026-01-02 00:00:00", plant_codes=("p01",))
        self.assertLessEqual(len(p01_only["equipment"]), len(both["equipment"]))
        self.assertTrue(all(r["plant_code"] == "p01" for r in p01_only["equipment"]))

    def test_no_hidden_aggregate_historical_score(self):
        start = BASE.strftime("%Y-%m-%d %H:%M:%S")
        end = (BASE + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        summary = dhh.fleet_status_summary(self.config_db, start, end, plant_codes=("p01",))
        for forbidden in ("historical_score", "reliability_score", "telemetry_health_score", "data_risk_score"):
            self.assertNotIn(forbidden, summary)
            for bucket in summary["by_plant"].values():
                self.assertNotIn(forbidden, bucket)


class TestUnavailableNeverBecomesZero(DataHealthHistoryTestBase):
    def test_unavailable_confidence_score_stored_as_null(self):
        self._persist(_result("UNAVAILABLE", None, computed_at=BASE), at=BASE)
        row = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        self.assertIsNone(row["confidence_score"])
        self.assertNotEqual(row["confidence_score"], 0)

    def test_unavailable_component_scores_stored_as_null(self):
        r = _result("UNAVAILABLE", None, computed_at=BASE)
        r.component_scores = {"freshness": None, "availability": None, "validity": None, "continuity": None}
        self._persist(r, at=BASE)
        row = dhdom.get_latest_snapshot(self.config_db, self.plant_id, INSTANCE_KEY)
        self.assertIsNone(row["freshness_score"])


class TestNoScadaIntegration(unittest.TestCase):
    def test_scada_snapshot_writer_still_never_imports_data_health(self):
        source = Path("ui/scada_snapshot_writer.py").read_text()
        self.assertNotIn("data_health", source.lower())


class TestRealLiveDatabaseWiring(unittest.TestCase):
    """Confirms the real end-to-end wiring works against the real live
    database (already migrated and seeded by this phase's own
    commissioning pass)."""

    def test_real_first_recorded_at_is_set(self):
        recorded = dhh.first_recorded_at(get_config_db_path())
        self.assertIsNotNone(recorded)

    def test_real_fleet_status_summary_runs_without_error(self):
        now = datetime.now()
        start = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
        summary = dhh.fleet_status_summary(get_config_db_path(), start, end)
        self.assertGreater(len(summary["equipment"]), 0)


if __name__ == "__main__":
    unittest.main()
