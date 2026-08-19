import json
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
from ui import savings_verification_data as svd

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _insert_tariff, _plant_id

"""
Phase 11.6 - end-to-end acceptance tests for the COMPLETE Phase 11
chain, run against an isolated throwaway test database only (never the
live production DB - item 26):

    Energy Opportunity -> Engineer Intervention -> Stabilization ->
    Comparable Evidence -> Verification Calculation -> Verification
    Worker -> Verification History -> Engineer-Facing Result (UI)

Every test drives the REAL domain API (engine.savings_verification_domain),
the REAL orchestration/worker path (engine.savings_verification_orchestration),
and then reads back through the REAL UI data-access layer
(ui.savings_verification_data) to confirm the engineer-facing result
matches what was actually persisted - no shortcuts, no fabricated rows,
no recalculation of anything the backend already computed.
"""

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
        category="CHILLER", title="Chiller Power Above Expected", status="NEW", priority="LOW", priority_score=4.0,
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


def _get_opportunity(config_db: Path, opportunity_id: int) -> dict:
    connection = sqlite3.connect(config_db)
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT * FROM energy_opportunities WHERE id = ?", (opportunity_id,)).fetchone()
    connection.close()
    return dict(row)


def _seed_daily(historian: DatabaseManager, tag: str, start_day_offset: int, end_day_offset: int, value: float, hour: int = 14) -> None:
    for day in range(start_day_offset, end_day_offset + 1):
        t = IMPLEMENTED_AT + timedelta(days=day, hours=hour)
        historian.save_tag(tag, "sim", value, t)


class E2ETestBase(unittest.TestCase):
    """Full-chain fixture: real config/machine DBs, real migrations, real
    domain/orchestration calls, and the real UI data-access layer pointed
    at the SAME throwaway DB (never the live one - item 26)."""

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
        self.opportunity_id = _seed_opportunity(self.config_db, self.plant_id)

        self._config_patches = [
            patch("ui.savings_verification_data.CONFIG_DATABASE_PATH", str(self.config_db)),
        ]
        for p in self._config_patches:
            p.start()

    def tearDown(self):
        for p in self._config_patches:
            p.stop()
        self.temp_dir.cleanup()

    def _new_intervention(self, stabilization_days=2):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered chiller setpoint by 2C", "test_engineer",
            stabilization_days=stabilization_days,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        return intervention_id

    def _evaluate(self, intervention_id, now):
        with patch("engine.savings_verification_evidence.resolve_target_for_intervention", return_value=SIMPLE_TARGET):
            return orch.evaluate_intervention(self.config_db, self.machine_db, self.historian, intervention_id, now=now)

    def _insert_maintenance(self, performed_at: datetime):
        connection = sqlite3.connect(self.config_db)
        equipment_id = connection.execute("SELECT id FROM equipment WHERE name = 'p01_chiller_chl01'").fetchone()[0]
        connection.execute(
            "INSERT INTO maintenance_log (equipment_id, category, description, performed_at, next_due_at, created_at) "
            "VALUES (?, 'preventive', 'test', ?, NULL, ?)",
            (equipment_id, performed_at.strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.commit()
        connection.close()


# ---------------------------------------------------------------------------
# Item 13 - VERIFIED end-to-end
# ---------------------------------------------------------------------------

class TestVerifiedEndToEnd(E2ETestBase):
    def test_full_chain_verified(self):
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        intervention_id = self._new_intervention()
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)  # BEFORE
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)   # AFTER - genuinely lower

        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome["action"], "evaluated")
        self.assertEqual(outcome["result"], "VERIFIED")

        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "COMPLETED")

        latest = dom.get_latest_verification_result(self.config_db, intervention_id)
        self.assertEqual(latest["result"], "VERIFIED")
        self.assertGreater(latest["verified_energy_kwh"], 0)
        self.assertIsNotNone(latest["verified_cost"])
        self.assertGreater(latest["verified_cost"], 0)

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 1)

        # Opportunity's own estimate must remain byte-for-byte unchanged.
        opportunity_after = _get_opportunity(self.config_db, self.opportunity_id)
        self.assertIsNone(opportunity_after["estimated_potential_saving_period"])

        # UI data helpers must surface the correct final state (item 12.11).
        ui_intervention = svd.get_intervention(intervention_id)
        self.assertEqual(ui_intervention["engineer_status"], "Verified")
        self.assertEqual(ui_intervention["latest_outcome"], "VERIFIED")
        saving = svd.verified_saving_summary(ui_intervention)
        self.assertIn("MYR", saving["display"])
        self.assertNotEqual(saving["display"], "Waiting for verification")

        ui_list = svd.get_interventions()
        self.assertEqual(len(ui_list), 1)
        self.assertEqual(ui_list[0]["engineer_status"], "Verified")

        summary = svd.get_summary(ui_list)
        self.assertEqual(summary["verified"], 1)
        self.assertAlmostEqual(summary["verified_savings_total"], latest["verified_cost"], places=3)


# ---------------------------------------------------------------------------
# Item 14 - REJECTED end-to-end
# ---------------------------------------------------------------------------

class TestRejectedEndToEnd(E2ETestBase):
    def test_full_chain_rejected_negative_saving_not_clamped(self):
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        intervention_id = self._new_intervention()
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 50.0)  # BEFORE
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 60.0)   # AFTER - worse

        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome["result"], "REJECTED")

        latest = dom.get_latest_verification_result(self.config_db, intervention_id)
        self.assertEqual(latest["result"], "REJECTED")
        self.assertIsNotNone(latest["verified_energy_kwh"])
        self.assertLess(latest["verified_energy_kwh"], 0)  # negative saving survives, never clamped to 0

        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "COMPLETED")

        ui_intervention = svd.get_intervention(intervention_id)
        self.assertEqual(ui_intervention["engineer_status"], "Not Verified")
        saving = svd.verified_saving_summary(ui_intervention)
        self.assertEqual(saving["display"], "Not applicable")
        self.assertNotIn("failure", saving["explanation"].lower())

        # Excluded from the Verified Savings KPI entirely - not zero, not counted.
        summary = svd.get_summary(svd.get_interventions())
        self.assertEqual(summary["verified"], 0)
        self.assertEqual(summary["verified_savings_total"], 0.0)


# ---------------------------------------------------------------------------
# Item 15 - INCONCLUSIVE end-to-end + re-evaluation
# ---------------------------------------------------------------------------

class TestInconclusiveEndToEndAndRetry(E2ETestBase):
    def test_inconclusive_then_reevaluates_to_verified_on_new_evidence(self):
        intervention_id = self._new_intervention(stabilization_days=0)

        # No AFTER evidence at all yet.
        outcome1 = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=1))
        self.assertEqual(outcome1["result"], "INCONCLUSIVE")

        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "VERIFICATION_IN_PROGRESS")  # never falsely COMPLETED

        ui_intervention = svd.get_intervention(intervention_id)
        self.assertEqual(ui_intervention["engineer_status"], "More Evidence Required")
        saving = svd.verified_saving_summary(ui_intervention)
        self.assertEqual(saving["display"], "Waiting for verification")

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0]["verified_energy_kwh"])  # no verified-saving amount claimed

        # Materially new evidence appears.
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 0, 19, 50.0)
        outcome2 = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome2["action"], "evaluated")
        self.assertEqual(outcome2["result"], "VERIFIED")

        row2 = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row2["status"], "COMPLETED")

        ui_intervention2 = svd.get_intervention(intervention_id)
        self.assertEqual(ui_intervention2["engineer_status"], "Verified")

        history2 = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history2), 2)  # both evaluations preserved


# ---------------------------------------------------------------------------
# Item 16 - append-only history acceptance
# ---------------------------------------------------------------------------

class TestAppendOnlyHistoryAcceptance(E2ETestBase):
    def test_inconclusive_inconclusive_verified_all_three_rows_preserved_chronological(self):
        intervention_id = self._new_intervention(stabilization_days=0)

        first = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=1))  # no data
        self.assertEqual(first["result"], "INCONCLUSIVE")

        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 0, 1, 50.0)  # a little AFTER data - still thin
        second = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=2))
        self.assertEqual(second["action"], "evaluated")
        self.assertEqual(second["result"], "INCONCLUSIVE")

        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 2, 19, 50.0)  # enough now
        third = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(third["action"], "evaluated")
        self.assertEqual(third["result"], "VERIFIED")

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 3)
        self.assertEqual([h["id"] for h in history], sorted(h["id"] for h in history))  # never overwritten
        self.assertEqual([h["evaluated_at"] for h in history], sorted(h["evaluated_at"] for h in history))  # chronological
        self.assertEqual(history[0]["result"], "INCONCLUSIVE")
        self.assertEqual(history[1]["result"], "INCONCLUSIVE")
        self.assertEqual(history[2]["result"], "VERIFIED")

        # UI history read is chronological and complete.
        ui_history = svd.get_verification_history(intervention_id)
        self.assertEqual([h["id"] for h in ui_history], [h["id"] for h in history])

        # Aggregate Verified Savings must use ONLY the terminal VERIFIED
        # result, never sum across the two earlier INCONCLUSIVE rows.
        latest_verified_cost = history[2]["verified_cost"]
        summary = svd.get_summary(svd.get_interventions())
        self.assertEqual(summary["verified"], 1)
        self.assertAlmostEqual(summary["verified_savings_total"], latest_verified_cost or 0.0, places=3)


# ---------------------------------------------------------------------------
# Item 17 - duplicate suppression acceptance
# ---------------------------------------------------------------------------

class TestDuplicateSuppressionAcceptance(E2ETestBase):
    def test_repeated_cycles_with_unchanged_evidence_produce_no_duplicates(self):
        intervention_id = self._new_intervention(stabilization_days=0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)

        with patch("engine.savings_verification_evidence.resolve_target_for_intervention", return_value=SIMPLE_TARGET):
            first_cycle = orch.run_cycle(self.config_db, self.machine_db, self.historian, now=IMPLEMENTED_AT + timedelta(days=20))
            second_cycle = orch.run_cycle(self.config_db, self.machine_db, self.historian, now=IMPLEMENTED_AT + timedelta(days=20, minutes=5))
            third_cycle = orch.run_cycle(self.config_db, self.machine_db, self.historian, now=IMPLEMENTED_AT + timedelta(days=20, minutes=10))

        self.assertEqual(first_cycle["evaluated"], 1)
        self.assertEqual(second_cycle["evaluated"], 0)  # COMPLETED after first VERIFIED - filtered before evaluation
        self.assertEqual(third_cycle["evaluated"], 0)

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 1)  # no duplicate evaluation rows

        interventions = dom.list_interventions(self.config_db)
        self.assertEqual(len(interventions), 1)  # no duplicate interventions

        summary = svd.get_summary(svd.get_interventions())
        self.assertEqual(summary["verified"], 1)  # no duplicate verified saving

    def test_new_evidence_after_completion_does_not_reopen_but_a_second_intervention_can_retry(self):
        """A COMPLETED intervention is permanently filtered from future
        evaluation (Phase 11.4's own lifecycle - re-verification of a
        terminal result is out of scope for Phase 11), but a legitimate
        materially-new-evidence retry on a still-open (INCONCLUSIVE)
        intervention is real and must still work end-to-end."""
        intervention_id = self._new_intervention(stabilization_days=0)
        first = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=1))  # no data -> INCONCLUSIVE
        self.assertEqual(first["result"], "INCONCLUSIVE")

        immediate_rerun = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=1, minutes=1))
        self.assertEqual(immediate_rerun["action"], "skipped")
        self.assertEqual(immediate_rerun["skip_category"], "no_new_evidence")

        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 0, 19, 50.0)
        retried = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(retried["action"], "evaluated")
        self.assertEqual(retried["result"], "VERIFIED")

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 2)


# ---------------------------------------------------------------------------
# Item 18 - tariff acceptance (A, B, C)
# ---------------------------------------------------------------------------

class TestTariffAcceptance(E2ETestBase):
    def test_a_tariff_available_energy_and_cost_both_verified(self):
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        intervention_id = self._new_intervention()
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome["result"], "VERIFIED")

        latest = dom.get_latest_verification_result(self.config_db, intervention_id)
        self.assertIsNotNone(latest["verified_cost"])

        ui_intervention = svd.get_intervention(intervention_id)
        saving = svd.verified_saving_summary(ui_intervention)
        self.assertIn("MYR", saving["display"])

    def test_b_tariff_unavailable_energy_verified_cost_stays_unavailable_not_zero(self):
        # No tariff inserted at all.
        intervention_id = self._new_intervention()
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome["result"], "VERIFIED")  # energy verification not rejected for lack of tariff

        latest = dom.get_latest_verification_result(self.config_db, intervention_id)
        self.assertGreater(latest["verified_energy_kwh"], 0)
        self.assertIsNone(latest["verified_cost"])  # never defaulted to 0

        ui_intervention = svd.get_intervention(intervention_id)
        saving = svd.verified_saving_summary(ui_intervention)
        self.assertEqual(saving["display"], "Tariff unavailable")  # distinguished, not generic "Unavailable"

        # This intervention's energy-only VERIFIED result is correctly
        # excluded from the cost KPI (nothing to sum), and disclosed.
        summary = svd.get_summary(svd.get_interventions())
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["verified_savings_total"], 0.0)
        self.assertEqual(summary["verified_without_cost_count"], 1)

    def test_c_partial_tariff_coverage_disclosed_not_hidden(self):
        # Tariff effective only partway through the AFTER window - some
        # contributing buckets are priced, some are not.
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR",
                        effective_date=(IMPLEMENTED_AT + timedelta(days=10)).strftime("%Y-%m-%d"))
        intervention_id = self._new_intervention()
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(outcome["result"], "VERIFIED")

        latest = dom.get_latest_verification_result(self.config_db, intervention_id)
        self.assertIsNotNone(latest["verified_cost"])  # partial coverage still gives a real (partial) figure
        limitations = json.loads(latest["limitations_json"] or "[]")
        self.assertTrue(any("partial" in item.lower() for item in limitations))

        # The UI must display this disclosure verbatim, never recompute it.
        ui_history = svd.get_verification_history(intervention_id)
        self.assertEqual(json.loads(ui_history[0]["limitations_json"]), limitations)


# ---------------------------------------------------------------------------
# Item 19 - maintenance exclusion acceptance through the full workflow
# ---------------------------------------------------------------------------

class TestMaintenanceExclusionAcceptance(E2ETestBase):
    def test_some_evidence_removed_quality_still_sufficient(self):
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        intervention_id = self._new_intervention()
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 27, 50.0)  # 25 AFTER days
        self._insert_maintenance(IMPLEMENTED_AT + timedelta(days=5, hours=14))  # 1 day contaminated

        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=30))
        self.assertIn(outcome["result"], ("VERIFIED", "REJECTED"))  # still reaches a real result

        latest = dom.get_latest_verification_result(self.config_db, intervention_id)
        evidence = json.loads(latest["evidence_json"])
        self.assertGreater(evidence["exclusion_summary"].get("maintenance_contaminated_after_buckets_excluded", 0), 0)

        # Disclosed to the UI without recalculation.
        ui_history = svd.get_verification_history(intervention_id)
        self.assertEqual(ui_history[0]["result"], outcome["result"])

    def test_heavy_removal_downgrades_to_inconclusive(self):
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        intervention_id = self._new_intervention()
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 22, 50.0)  # 20 AFTER days
        for day in range(3, 23):
            self._insert_maintenance(IMPLEMENTED_AT + timedelta(days=day, hours=14))  # every AFTER day contaminated

        outcome = self._evaluate(intervention_id, IMPLEMENTED_AT + timedelta(days=25))
        self.assertEqual(outcome["result"], "INCONCLUSIVE")

        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], "VERIFICATION_IN_PROGRESS")  # not terminal

        ui_intervention = svd.get_intervention(intervention_id)
        self.assertEqual(ui_intervention["engineer_status"], "More Evidence Required")
        latest = dom.get_latest_verification_result(self.config_db, intervention_id)
        self.assertIn("maintenance", latest["reason"].lower())


# ---------------------------------------------------------------------------
# Item 24 - no double-counting of verified savings
# ---------------------------------------------------------------------------

class TestNoDoubleCounting(E2ETestBase):
    def test_summary_uses_latest_result_only_not_sum_of_all_history(self):
        """Direct regression guard for item 24's exact scenario: even if
        a future feature ever produced more than one terminal-looking
        VERIFIED row in append-only history for one intervention, the UI
        aggregation must use only the LATEST evaluation, never SUM every
        VERIFIED row it has ever seen."""
        intervention_id = self._new_intervention(stabilization_days=0)
        base = IMPLEMENTED_AT
        # Simulate 3 historical evaluations directly via the append-only
        # domain API (adversarial construction - not achievable through
        # the real worker, which stops evaluating after one VERIFIED,
        # but the aggregation function must be safe regardless).
        for i, cost in enumerate((1000.0, 1000.0, 1000.0)):
            dom.record_verification_result(
                self.config_db, intervention_id, "VERIFIED",
                evidence_json="{}", assumptions_json="[]", limitations_json="[]",
                verified_energy_kwh=2000.0, verified_cost=cost, verified_cost_currency="MYR",
                confidence="STRONG", evaluated_at=base + timedelta(days=i + 1),
                reason=f"VERIFIED - evaluation {i + 1}",
                verification_period_start=(base + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S"),
                verification_period_end=(base + timedelta(days=i + 1)).strftime("%Y-%m-%d %H:%M:%S"),
            )
        dom.update_intervention_status(self.config_db, intervention_id, "COMPLETED")

        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 3)  # all preserved, append-only

        ui_interventions = svd.get_interventions()
        self.assertEqual(len(ui_interventions), 1)
        summary = svd.get_summary(ui_interventions)
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["verified_savings_total"], 1000.0)  # NOT 3000.0

        by_period = svd.get_verified_savings_by_period(ui_interventions, now=base + timedelta(days=5))
        self.assertEqual(by_period["this_year_total"], 1000.0)  # also not 3000.0


# ---------------------------------------------------------------------------
# Item 25 - Observed Excess vs Potential vs Verified never summed together
# ---------------------------------------------------------------------------

class TestOpportunityVsVerifiedSeparation(unittest.TestCase):
    def test_no_total_savings_label_or_summed_expression_anywhere(self):
        for path in (
            "ui/pages/17_Energy_Opportunities.py", "ui/pages/18_Savings_Verification.py",
            "ui/savings_verification_data.py", "engine/savings_verification_engine.py",
            "engine/savings_verification_domain.py",
        ):
            source = Path(path).read_text()
            self.assertNotIn("Total Savings", source, msg=f"found a 'Total Savings' label in {path}")
            for line in source.splitlines():
                has_observed = "observed_excess_cost" in line
                has_potential = "estimated_potential_saving" in line
                has_verified = "verified_cost" in line or "verified_savings" in line
                combined = sum([has_observed, has_potential, has_verified]) >= 2 and "+" in line
                self.assertFalse(combined, msg=f"possible cross-concept summation in {path}: {line!r}")


# ---------------------------------------------------------------------------
# Item 11 - no control actions anywhere in the Phase 11 UI
# ---------------------------------------------------------------------------

class TestNoControlActions(unittest.TestCase):
    def test_no_plc_write_setpoint_or_alarm_actions_in_ui(self):
        for path in ("ui/pages/17_Energy_Opportunities.py", "ui/pages/18_Savings_Verification.py", "ui/savings_verification_data.py"):
            source = Path(path).read_text()
            for banned in (
                "driver_factory", "opcua", "modbus", "s7_driver", "write_tag", "set_setpoint",
                "reset_alarm", "start_equipment", "stop_equipment", "issue_maintenance_command",
                "plc.driver", "PLC_WRITE",
            ):
                self.assertNotIn(banned, source, msg=f"found banned control-action reference {banned!r} in {path}")


if __name__ == "__main__":
    unittest.main()
