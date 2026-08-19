import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from database.database import DatabaseManager
from engine import savings_verification_domain as dom
from engine import savings_verification_orchestration as orch
from engine.baseline_targets import BaselineTarget
from engine.opportunity_migrator import migrate as migrate_opportunity
from engine.savings_verification_migrator import migrate as migrate_savings_verification
from engine.savings_verification_targets import ACTION_CATEGORY_SETPOINT_CHANGE

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _insert_tariff, _plant_id


IMPLEMENTED_AT = datetime(2026, 8, 25, 0, 0, 0)

SIMPLE_TARGET = BaselineTarget(
    plant_code="p01", instance_key="P01.UTILITY.CHL01", equipment_type="chiller", target_key="Power_kW",
    tag_name="P01.UTILITY.CHL01.Power_kW", is_derived=False, context_dimensions=(), aggregation_minutes=5,
    reference_window_days_max=60, recent_window_days=7,
)


def _seed_opportunity(config_db: Path, plant_id: int, instance_key: str = "P01.UTILITY.CHL01") -> int:
    now_text = IMPLEMENTED_AT.strftime("%Y-%m-%d %H:%M:%S")
    row = dict(
        plant_id=plant_id, equipment_id=None, instance_key=instance_key, rule_key="chiller_power_opportunity",
        category="CHILLER", title="t", status="NEW", priority="LOW", priority_score=4.0,
        priority_breakdown_json="{}", confidence="High", source_anomaly_ids="[1]",
        source_anomaly_rule_key="chiller_power_deviation", first_identified=now_text, last_updated=now_text,
        dismissed_at=None, dismissal_reason=None, dismissal_comment=None, occurrence_count=1,
        observed_period_start=now_text, observed_period_end=now_text, observed_excess_energy_kwh=6.5,
        observed_excess_cost=3.38, observed_cost_currency="MYR", saving_basis="unavailable",
        saving_unavailable_reason="no defensible method", estimated_potential_saving_period=None,
        estimated_monthly_saving=None, estimated_annual_saving=None, annualization_method="unavailable_conservative",
        saving_currency=None, tariff_provenance="SIMULATION", implementation_difficulty="UNKNOWN",
        equipment_criticality=None, recommendation="r", rule_provenance="SIMULATION_TUNING",
        evidence_json="{}", assumptions_json="[]", limitations_json="[]", created_at=now_text, updated_at=now_text,
    )
    connection = sqlite3.connect(config_db)
    cols = list(row.keys())
    cursor = connection.execute(f"INSERT INTO energy_opportunities ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
    connection.commit()
    new_id = cursor.lastrowid
    connection.close()
    return new_id


def _seed_daily(historian: DatabaseManager, tag: str, start_day_offset: int, end_day_offset: int, value: float, hour: int = 14) -> None:
    for day in range(start_day_offset, end_day_offset + 1):
        t = IMPLEMENTED_AT + timedelta(days=day, hours=hour)
        historian.save_tag(tag, "sim", value, t)


class WorkerTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        migrate_opportunity(self.config_db, backup=False)
        migrate_savings_verification(self.config_db, backup=False)
        self.historian = DatabaseManager(db_path=self.machine_db)
        self.plant_id = _plant_id(self.config_db, "p01")
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        self.opportunity_id = _seed_opportunity(self.config_db, self.plant_id)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _new_intervention(self, implemented_at=None, stabilization_days=None):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "test_engineer", stabilization_days=stabilization_days,
        )
        if implemented_at is not None:
            dom.mark_implemented(self.config_db, intervention_id, implemented_at=implemented_at)
        return intervention_id

    def _evaluate(self, intervention_id, now):
        with patch("engine.savings_verification_evidence.resolve_target_for_intervention", return_value=SIMPLE_TARGET):
            return orch.evaluate_intervention(self.config_db, self.machine_db, self.historian, intervention_id, now=now)

    def _run_cycle(self, now):
        with patch("engine.savings_verification_evidence.resolve_target_for_intervention", return_value=SIMPLE_TARGET):
            return orch.run_cycle(self.config_db, self.machine_db, self.historian, now=now)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

class TestEligibility(WorkerTestBase):
    def test_zero_interventions_gives_zero_everything(self):
        summary = self._run_cycle(IMPLEMENTED_AT)
        self.assertEqual(summary["eligible_interventions"], 0)
        self.assertEqual(summary["evaluated"], 0)
        self.assertEqual(summary["persisted"], 0)

    def test_planned_intervention_not_eligible(self):
        intervention_id = self._new_intervention(implemented_at=None)  # stays PLANNED
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT)
        self.assertEqual(outcome["action"], "skipped")
        self.assertEqual(outcome["skip_category"], "not_eligible")
        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "PLANNED")  # never touched

    def test_future_implemented_at_not_eligible(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT + timedelta(days=100), stabilization_days=2)
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT)
        self.assertEqual(outcome["action"], "skipped")
        self.assertEqual(outcome["skip_category"], "not_eligible")

    def test_stabilization_unfinished_not_eligible(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=5)
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=1))
        self.assertEqual(outcome["action"], "skipped")
        self.assertEqual(outcome["skip_category"], "not_eligible")

    def test_terminal_completed_intervention_is_skipped(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=0)
        dom.update_intervention_status(self.config_db, intervention_id, "COMPLETED")
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome["action"], "skipped")
        self.assertEqual(outcome["skip_category"], "not_eligible")
        self.assertIn("terminal", outcome["reason"])


# ---------------------------------------------------------------------------
# First evaluation / persistence / lifecycle transitions
# ---------------------------------------------------------------------------

class TestFirstEvaluationAndLifecycle(WorkerTestBase):
    def test_implemented_transitions_to_pending_on_first_sight(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=5)
        self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(hours=1))  # stabilization not finished
        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "VERIFICATION_PENDING")

    def test_first_legitimate_eligible_evaluation_runs(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome["action"], "evaluated")
        self.assertEqual(outcome["result"], "VERIFIED")
        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 1)

    def test_verification_pending_transitions_to_in_progress_then_completed_on_verified(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "COMPLETED")

    def test_rejected_result_also_completes_intervention(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 50.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 60.0)  # worse than before
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome["result"], "REJECTED")
        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "COMPLETED")

    def test_planned_never_jumps_directly_to_completed(self):
        intervention_id = self._new_intervention(implemented_at=None)
        self._evaluate(intervention_id, IMPLEMENTED_AT)
        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "PLANNED")


# ---------------------------------------------------------------------------
# INCONCLUSIVE is not terminal (item 4)
# ---------------------------------------------------------------------------

class TestInconclusiveNotTerminal(WorkerTestBase):
    def test_inconclusive_persists_but_does_not_complete(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        # No historian data seeded at all - guarantees INSUFFICIENT -> INCONCLUSIVE.
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=3))
        self.assertEqual(outcome["action"], "evaluated")
        self.assertEqual(outcome["result"], "INCONCLUSIVE")
        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "VERIFICATION_IN_PROGRESS")  # NOT COMPLETED

    def test_inconclusive_intervention_remains_eligible_for_future_evaluation(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=3))  # INCONCLUSIVE, no data

        # Now real evidence appears - a later cycle must be able to evaluate again.
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        outcome2 = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome2["action"], "evaluated")
        self.assertEqual(outcome2["result"], "VERIFIED")
        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 2)  # both evaluations preserved, append-only


# ---------------------------------------------------------------------------
# Materially new evidence / duplicate suppression (items 5, 7)
# ---------------------------------------------------------------------------

class TestMateriallyNewEvidence(WorkerTestBase):
    def test_immediate_rerun_with_same_evidence_produces_no_duplicate(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        first = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=3))
        self.assertEqual(first["action"], "evaluated")

        second = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=3, minutes=1))
        self.assertEqual(second["action"], "skipped")
        self.assertEqual(second["skip_category"], "no_new_evidence")

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 1)

    def test_materially_new_after_bucket_allows_retry(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 0, 4, 50.0)  # 5 days - meets the minimum, likely still bootstrap/limited
        first = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=5))

        # More real AFTER data accumulates.
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 5, 19, 50.0)
        second = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(second["action"], "evaluated")

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 2)

    def test_early_gated_inconclusive_does_not_repeat_forever_with_unchanged_evidence(self):
        """Regression guard for a real bug found during Phase 11.4
        performance measurement: when calculate_verified_savings() is
        gated at its FIRST evidence-quality check (before maintenance-
        exclusion recomputation runs), usable_reference/after_sample_count
        stay None in the persisted row. Comparing None as if it were 0
        made every later cycle look like materially new evidence even
        with byte-identical data, causing a repeated-duplicate-
        INCONCLUSIVE-row flood - exactly what item 5 explicitly forbids."""
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=0)
        # A real target with real context dimensions (not SIMPLE_TARGET) - guarantees
        # gating at the FIRST evidence-quality check (LIMITED, no load/ambient seeded).
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 0, 14, 50.0)
        now = IMPLEMENTED_AT + timedelta(days=15)

        first = orch.evaluate_intervention(self.config_db, self.machine_db, self.historian, intervention_id, now=now)
        self.assertEqual(first["action"], "evaluated")
        self.assertEqual(first["result"], "INCONCLUSIVE")

        row = dom.get_latest_verification_result(self.config_db, intervention_id)
        self.assertIsNotNone(row)  # sanity - a row was actually persisted

        second = orch.evaluate_intervention(self.config_db, self.machine_db, self.historian, intervention_id, now=now + timedelta(seconds=5))
        self.assertEqual(second["action"], "skipped")
        self.assertEqual(second["skip_category"], "no_new_evidence")

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 1)  # NOT 2 - no duplicate

    def test_increased_matched_coverage_allows_retry(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 0, 2, 50.0)  # 3 days - INSUFFICIENT-ish
        self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=3))

        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)  # much more coverage now
        second = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(second["action"], "evaluated")


# ---------------------------------------------------------------------------
# Failure isolation (item 9)
# ---------------------------------------------------------------------------

class TestFailureIsolation(WorkerTestBase):
    def test_one_intervention_failure_does_not_stop_another(self):
        broken_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        # A second, independent opportunity/intervention (a second instance) -
        # one opportunity can have at most one ACTIVE intervention (Phase 11.1's
        # own proven partial-unique-index constraint), so a second FK target is required.
        _insert_tag(self.config_db, "P01.UTILITY.CHL02.Power_kW", unit="kW", measurement="power")
        second_opportunity_id = _seed_opportunity(self.config_db, self.plant_id, instance_key="P01.UTILITY.CHL02")
        good_id = dom.create_intervention(
            self.config_db, second_opportunity_id, self.plant_id, "P01.UTILITY.CHL02",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "test_engineer", stabilization_days=2,
        )
        dom.mark_implemented(self.config_db, good_id, implemented_at=IMPLEMENTED_AT)

        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL02.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL02.Power_kW", 3, 17, 50.0)

        now = IMPLEMENTED_AT + timedelta(days=20)
        call_count = {"n": 0}
        real_calculate = __import__("engine.savings_verification_engine", fromlist=["calculate_verified_savings"]).calculate_verified_savings

        def _flaky(config_db, machine_db, historian, intervention_id, now=None):
            call_count["n"] += 1
            if intervention_id == broken_id:
                raise RuntimeError("simulated calculation failure")
            return real_calculate(config_db, machine_db, historian, intervention_id, now=now)

        with patch("engine.savings_verification_evidence.resolve_target_for_intervention", return_value=SIMPLE_TARGET), \
             patch("engine.savings_verification_orchestration.calc.calculate_verified_savings", side_effect=_flaky):
            summary = orch.run_cycle(self.config_db, self.machine_db, self.historian, now=now)

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["evaluated"], 1)
        good_row = dom.get_intervention(self.config_db, good_id)
        self.assertEqual(good_row["status"], "COMPLETED")
        broken_row = dom.get_intervention(self.config_db, broken_id)
        self.assertNotEqual(broken_row["status"], "COMPLETED")  # never falsely completed

    def test_failed_calculation_produces_no_fake_result_row(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        now = IMPLEMENTED_AT + timedelta(days=20)

        with patch("engine.savings_verification_evidence.resolve_target_for_intervention", return_value=SIMPLE_TARGET), \
             patch("engine.savings_verification_orchestration.calc.calculate_verified_savings", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                orch.evaluate_intervention(self.config_db, self.machine_db, self.historian, intervention_id, now=now)

        self.assertEqual(len(dom.list_verification_history(self.config_db, intervention_id)), 0)

    def test_failed_persistence_does_not_mark_completed(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        now = IMPLEMENTED_AT + timedelta(days=20)

        with patch("engine.savings_verification_evidence.resolve_target_for_intervention", return_value=SIMPLE_TARGET), \
             patch("engine.savings_verification_orchestration.calc.persist_verification_result", side_effect=RuntimeError("db write failed")):
            with self.assertRaises(RuntimeError):
                orch.evaluate_intervention(self.config_db, self.machine_db, self.historian, intervention_id, now=now)

        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertNotEqual(row["status"], "COMPLETED")


# ---------------------------------------------------------------------------
# Append-only history / determinism / no-mutation
# ---------------------------------------------------------------------------

class TestHistoryAndDeterminism(WorkerTestBase):
    def test_append_only_result_history_never_overwritten(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=0)
        self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=1))  # INCONCLUSIVE - no data
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 0, 19, 50.0)
        self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertGreaterEqual(len(history), 2)
        ids = [h["id"] for h in history]
        self.assertEqual(len(ids), len(set(ids)))  # all distinct rows

    def test_deterministic_evaluation_ordering(self):
        id1 = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        first_list = [i["id"] for i in orch._candidate_interventions(self.config_db)]
        second_list = [i["id"] for i in orch._candidate_interventions(self.config_db)]
        self.assertEqual(first_list, second_list)

    def test_no_mutation_of_energy_opportunities_or_anomalies(self):
        intervention_id = self._new_intervention(implemented_at=IMPLEMENTED_AT, stabilization_days=2)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)

        def _snapshot():
            connection = sqlite3.connect(self.config_db)
            snap = {
                "opportunities": connection.execute("SELECT * FROM energy_opportunities ORDER BY id").fetchall(),
                "anomalies": connection.execute("SELECT * FROM anomalies ORDER BY id").fetchall(),
            }
            connection.close()
            return snap

        before = _snapshot()
        self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        after = _snapshot()
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# Single-instance / concurrency protection (item 8)
# ---------------------------------------------------------------------------

class TestSingleInstanceLock(unittest.TestCase):
    def test_duplicate_start_refused(self):
        import subprocess
        import time as time_module

        lock_path = Path("logs/savings_verification_worker.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        proc = subprocess.Popen(
            ["python3", "-u", "-m", "app.savings_verification_worker"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            time_module.sleep(1.5)
            result = subprocess.run(
                ["timeout", "3", "python3", "-u", "-m", "app.savings_verification_worker"],
                cwd=str(Path(__file__).resolve().parent.parent),
                capture_output=True, text=True,
            )
            self.assertIn("refusing to start a second one", result.stdout)
        finally:
            proc.terminate()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Source-inspection guards
# ---------------------------------------------------------------------------

class TestSourceInspection(unittest.TestCase):
    def test_no_llm_or_plc_imports(self):
        for path in ("engine/savings_verification_orchestration.py", "app/savings_verification_worker.py"):
            source = Path(path).read_text()
            for banned in ("import ai.", "from ai.", "ai_provider", "plc.driver_factory", "opcua", "modbus"):
                self.assertNotIn(banned, source)

    def test_orchestration_never_writes_energy_opportunities_or_anomalies(self):
        source = Path("engine/savings_verification_orchestration.py").read_text()
        for banned in ("UPDATE energy_opportunities", "INSERT INTO energy_opportunities", "UPDATE anomalies", "INSERT INTO anomalies", "UPDATE baseline_context_summary"):
            self.assertNotIn(banned, source)

    def test_app_layer_contains_no_calculation_logic(self):
        """Business rules must live in engine/, not app/."""
        source = Path("app/savings_verification_worker.py").read_text()
        for banned in ("effective_tariff", "compute_window_baseline", "_summarize(", "_classify("):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
