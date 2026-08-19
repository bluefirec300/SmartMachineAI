import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from engine import savings_verification_domain as dom
from engine.opportunity_migrator import migrate as migrate_opportunity
from engine.savings_verification_migrator import migrate as migrate_savings_verification
from engine.savings_verification_targets import ACTION_CATEGORY_SETPOINT_CHANGE
from ui import savings_verification_data as svd

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _plant_id


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OPPORTUNITIES_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "17_Energy_Opportunities.py")
VERIFICATION_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "18_Savings_Verification.py")

IMPLEMENTED_AT = datetime(2026, 8, 25, 0, 0, 0)


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


class UITestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        migrate_opportunity(self.config_db, backup=False)
        migrate_savings_verification(self.config_db, backup=False)
        self.plant_id = _plant_id(self.config_db, "p01")
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        self.opportunity_id = _seed_opportunity(self.config_db, self.plant_id)

        self._config_patches = [
            patch("ui.savings_verification_data.CONFIG_DATABASE_PATH", str(self.config_db)),
            patch("ui.data_access.CONFIG_DATABASE_PATH", str(self.config_db)),
        ]
        for p in self._config_patches:
            p.start()

    def tearDown(self):
        for p in self._config_patches:
            p.stop()
        self.temp_dir.cleanup()

    def _record_verification_result(self, intervention_id, result, **kwargs):
        defaults = dict(
            evidence_json=json.dumps({"before_basis": 60.0, "after_basis": 50.0, "basis_unit": "kW",
                                       "usable_reference_sample_count": 15, "usable_after_sample_count": 15,
                                       "energy_saving_percentage": 16.67}),
            assumptions_json="[]", limitations_json="[]",
            reference_period_start="2026-06-25 23:59:59", reference_period_end="2026-08-24 23:59:59",
            verification_period_start="2026-08-27 00:00:00", verification_period_end="2026-09-14 00:00:00",
            verification_method="context_matched_power_median_interval_integration",
            reason="VERIFIED - 12.500 kWh observed saving.", confidence="CONTEXT_MATCHED",
        )
        defaults.update(kwargs)
        return dom.record_verification_result(self.config_db, intervention_id, result, **defaults)


# ---------------------------------------------------------------------------
# Pure status-label / stabilization function tests (no AppTest needed)
# ---------------------------------------------------------------------------

class TestStatusLabels(unittest.TestCase):
    def test_planned_label(self):
        self.assertEqual(svd.engineer_status_label("PLANNED", None), "Planned")

    def test_implemented_label(self):
        self.assertEqual(svd.engineer_status_label("IMPLEMENTED", None), "Implemented")

    def test_pending_label(self):
        self.assertEqual(svd.engineer_status_label("VERIFICATION_PENDING", None), "Stabilizing / Awaiting Verification")

    def test_in_progress_no_result_yet(self):
        self.assertEqual(svd.engineer_status_label("VERIFICATION_IN_PROGRESS", None), "Verification In Progress")

    def test_in_progress_inconclusive_shows_more_evidence_required(self):
        self.assertEqual(svd.engineer_status_label("VERIFICATION_IN_PROGRESS", "INCONCLUSIVE"), "More Evidence Required")

    def test_completed_verified_label(self):
        self.assertEqual(svd.engineer_status_label("COMPLETED", "VERIFIED"), "Verified")

    def test_completed_rejected_label(self):
        self.assertEqual(svd.engineer_status_label("COMPLETED", "REJECTED"), "Not Verified")

    def test_backend_vocabulary_never_altered(self):
        """The function must accept and correctly branch on the EXACT
        backend constant strings - never a renamed/invented vocabulary."""
        from engine.savings_verification_targets import (
            INTERVENTION_STATUS_COMPLETED, INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS,
            VERIFICATION_RESULT_VERIFIED, VERIFICATION_RESULT_REJECTED,
        )
        self.assertEqual(svd.engineer_status_label(INTERVENTION_STATUS_COMPLETED, VERIFICATION_RESULT_VERIFIED), "Verified")
        self.assertEqual(svd.engineer_status_label(INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS, VERIFICATION_RESULT_REJECTED), "Verification In Progress")


class TestStabilizationInfo(UITestBase):
    def test_no_implemented_at_returns_not_eligible(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "desc", "eng",
        )
        intervention = dom.get_intervention(self.config_db, intervention_id)
        info = svd.get_stabilization_info(intervention)
        self.assertFalse(info["eligible"])
        self.assertIsNone(info["implemented_at"])

    def test_stabilizing_shows_days_remaining(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "desc", "eng", stabilization_days=5,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        intervention = dom.get_intervention(self.config_db, intervention_id)
        info = svd.get_stabilization_info(intervention, now=IMPLEMENTED_AT + timedelta(days=2))
        self.assertFalse(info["eligible"])
        self.assertGreater(info["days_remaining"], 0)

    def test_eligible_after_stabilization_elapses(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "desc", "eng", stabilization_days=2,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        intervention = dom.get_intervention(self.config_db, intervention_id)
        info = svd.get_stabilization_info(intervention, now=IMPLEMENTED_AT + timedelta(days=5))
        self.assertTrue(info["eligible"])
        self.assertIsNone(info["days_remaining"])

    def test_missing_stabilization_days_uses_category_default(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "desc", "eng",
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        intervention = dom.get_intervention(self.config_db, intervention_id)
        info = svd.get_stabilization_info(intervention, now=IMPLEMENTED_AT)
        from engine.savings_verification_targets import DEFAULT_STABILIZATION_DAYS
        self.assertEqual(info["stabilization_days"], DEFAULT_STABILIZATION_DAYS[ACTION_CATEGORY_SETPOINT_CHANGE])


# ---------------------------------------------------------------------------
# Savings Verification page - empty state, KPIs, filters, detail (AppTest)
# ---------------------------------------------------------------------------

class TestSavingsVerificationPage(UITestBase):
    def _run(self):
        at = AppTest.from_file(VERIFICATION_PAGE, default_timeout=30)
        at.run()
        return at

    def test_page_loads_with_zero_interventions_professional_empty_state(self):
        at = self._run()
        self.assertIsNone(at.exception if isinstance(at.exception, Exception) else None)
        self.assertFalse(bool(at.exception))
        info_texts = " ".join(i.value for i in at.info)
        self.assertIn("No interventions have been recorded yet", info_texts)
        self.assertIn("Record Intervention", info_texts)

    def test_kpi_cards_present_with_zero_interventions(self):
        at = self._run()
        metric_labels = [m.label for m in at.metric]
        for expected in ("Active Interventions", "Awaiting Verification", "Verified", "More Evidence Required", "Rejected"):
            self.assertIn(expected, metric_labels)

    def test_lifecycle_status_rendering_stabilizing(self):
        """The UI must render whatever the backend actually persisted -
        it never computes its own lifecycle status. IMPLEMENTED->
        VERIFICATION_PENDING is exclusively Phase 11.4's worker
        transition (never mark_implemented(), never this UI), so this
        test simulates that worker transition explicitly before
        asserting on the resulting label - it does not expect the UI
        to derive "Stabilizing" on its own from stabilization math."""
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "eng", stabilization_days=5,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=datetime.now() - timedelta(hours=1))
        dom.update_intervention_status(self.config_db, intervention_id, "VERIFICATION_PENDING")
        at = self._run()
        self.assertFalse(bool(at.exception))
        table_values = at.dataframe[0].value.to_string() if at.dataframe else ""
        self.assertIn("Stabilizing", table_values)

    def test_inconclusive_rendering_and_explanation(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "eng", stabilization_days=0,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        self._record_verification_result(
            intervention_id, "INCONCLUSIVE", reason="INCONCLUSIVE - evidence quality is LIMITED.",
            verified_energy_kwh=None, verified_cost=None, verified_cost_currency=None, confidence="LIMITED",
        )
        at = self._run()
        self.assertFalse(bool(at.exception))
        warning_texts = " ".join(w.value for w in at.warning)
        self.assertIn("does not mean the intervention failed", warning_texts)
        self.assertIn("automatically re-evaluate", warning_texts)

    def test_verified_rendering_shows_estimated_vs_verified_separately(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "eng", stabilization_days=0,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        self._record_verification_result(
            intervention_id, "VERIFIED", verified_energy_kwh=12.5, verified_cost=6.25, verified_cost_currency="MYR",
        )
        dom.update_intervention_status(self.config_db, intervention_id, "COMPLETED")
        at = self._run()
        self.assertFalse(bool(at.exception))
        markdown_texts = " ".join(m.value for m in at.markdown)
        self.assertIn("Estimated Savings (opportunity-level)", markdown_texts)
        self.assertIn("Verified Saving (measured, this period only)", markdown_texts)
        self.assertIn("MYR 6.25", markdown_texts)

    def test_rejected_rendering_not_presented_as_failure(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "eng", stabilization_days=0,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        self._record_verification_result(
            intervention_id, "REJECTED", reason="REJECTED - observed change was -1.5 kWh (not a positive saving).",
            verified_energy_kwh=-1.5, verified_cost=-0.75, verified_cost_currency="MYR",
        )
        dom.update_intervention_status(self.config_db, intervention_id, "COMPLETED")
        at = self._run()
        self.assertFalse(bool(at.exception))
        error_texts = " ".join(e.value for e in at.error)
        self.assertIn("not a software failure", error_texts)

    def test_multiple_inconclusive_history_rows_displayed_chronologically(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "eng", stabilization_days=0,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        self._record_verification_result(intervention_id, "INCONCLUSIVE", reason="first", confidence="INSUFFICIENT",
                                          verified_energy_kwh=None, verified_cost=None, verified_cost_currency=None,
                                          evaluated_at=IMPLEMENTED_AT + timedelta(days=1))
        self._record_verification_result(intervention_id, "INCONCLUSIVE", reason="second", confidence="LIMITED",
                                          verified_energy_kwh=None, verified_cost=None, verified_cost_currency=None,
                                          evaluated_at=IMPLEMENTED_AT + timedelta(days=5))
        self._record_verification_result(intervention_id, "VERIFIED", reason="third", confidence="STRONG",
                                          verified_energy_kwh=12.5, verified_cost=6.25, verified_cost_currency="MYR",
                                          evaluated_at=IMPLEMENTED_AT + timedelta(days=10))
        dom.update_intervention_status(self.config_db, intervention_id, "COMPLETED")

        history = svd.get_verification_history(intervention_id)
        self.assertEqual(len(history), 3)
        self.assertEqual([h["evaluated_at"] for h in history], sorted(h["evaluated_at"] for h in history))  # chronological

        at = self._run()
        self.assertFalse(bool(at.exception))
        expander_labels = [exp.label for exp in at.expander]
        self.assertTrue(any("Evaluation 1" in label for label in expander_labels))
        self.assertTrue(any("Evaluation 2" in label for label in expander_labels))
        self.assertTrue(any("Evaluation 3" in label for label in expander_labels))

    def test_evidence_quality_rendering(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "eng", stabilization_days=0,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        self._record_verification_result(intervention_id, "VERIFIED", confidence="STRONG",
                                          verified_energy_kwh=12.5, verified_cost=6.25, verified_cost_currency="MYR")
        dom.update_intervention_status(self.config_db, intervention_id, "COMPLETED")
        at = self._run()
        self.assertFalse(bool(at.exception))
        captions = " ".join(c.value for c in at.caption)
        self.assertIn(svd.EVIDENCE_QUALITY_EXPLANATIONS["STRONG"], captions)

    def test_filters_narrow_the_table(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "eng", stabilization_days=0,
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=IMPLEMENTED_AT)
        at = self._run()
        status_select = [sb for sb in at.selectbox if sb.label == "Lifecycle status"][0]
        status_select.set_value("Planned")  # no interventions match this lifecycle status yet
        at.run()
        self.assertFalse(bool(at.exception))
        self.assertTrue(any(i.value == "No interventions match the current filters." for i in at.info))


# ---------------------------------------------------------------------------
# Energy Opportunities page - Record Intervention integration (item 3, 6, 23)
# ---------------------------------------------------------------------------

class TestOpportunityInterventionIntegration(UITestBase):
    def _run(self, role="engineer"):
        at = AppTest.from_file(OPPORTUNITIES_PAGE, default_timeout=30)
        at.session_state["auth_user"] = {"username": "testeng", "role": role, "display_name": "Test Engineer"}
        at.run()
        return at

    def test_existing_opportunity_page_still_functional(self):
        """item 23 - dataframe, dismiss form, and every prior element
        remain present alongside the new Record Intervention section."""
        at = self._run()
        self.assertFalse(bool(at.exception))
        self.assertEqual(len(at.dataframe), 1)
        subheaders = [s.value for s in at.subheader]
        self.assertIn("Dismiss this opportunity", subheaders)

    def test_record_intervention_form_present_for_engineer(self):
        at = self._run(role="engineer")
        self.assertFalse(bool(at.exception))
        subheaders = [s.value for s in at.subheader]
        self.assertIn("Record Intervention", subheaders)

    def test_record_intervention_hidden_for_operator(self):
        at = self._run(role="operator")
        self.assertFalse(bool(at.exception))
        subheaders = [s.value for s in at.subheader]
        self.assertNotIn("Record Intervention", subheaders)

    def test_valid_submission_creates_real_intervention(self):
        at = self._run()
        desc_box = [ta for ta in at.text_area if ta.key and "desc_" in ta.key][0]
        desc_box.set_value("Lowered chiller setpoint by 2C")
        by_box = [ti for ti in at.text_input if ti.key and "by_" in ti.key][0]
        by_box.set_value("Test Engineer")
        submit_btn = [b for b in at.button if b.label == "Record Intervention"][0]
        submit_btn.click()
        at.run()

        self.assertFalse(bool(at.exception))
        interventions = svd.get_interventions()
        self.assertEqual(len(interventions), 1)
        self.assertEqual(interventions[0]["action_description"], "Lowered chiller setpoint by 2C")
        self.assertEqual(interventions[0]["status"], "IMPLEMENTED")  # create + mark_implemented, one step

    def test_empty_description_rejected_with_friendly_error_not_traceback(self):
        at = self._run()
        by_box = [ti for ti in at.text_input if ti.key and "by_" in ti.key][0]
        by_box.set_value("Test Engineer")
        submit_btn = [b for b in at.button if b.label == "Record Intervention"][0]
        submit_btn.click()
        at.run()

        self.assertFalse(bool(at.exception))  # no raw traceback
        error_texts = " ".join(e.value for e in at.error)
        self.assertIn("description is required", error_texts.lower())
        self.assertEqual(len(svd.get_interventions()), 0)  # nothing created

    def test_stabilization_widget_cannot_go_negative(self):
        at = self._run()
        stab_box = [ni for ni in at.number_input if ni.key and "stab_" in ni.key][0]
        self.assertEqual(stab_box.min, 0)

    def test_duplicate_submission_protection_shows_existing_status_not_a_second_form(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "First action", "eng",
        )
        at = self._run()
        self.assertFalse(bool(at.exception))
        subheaders = [s.value for s in at.subheader]
        self.assertNotIn("Record Intervention", subheaders)
        self.assertIn("Intervention already recorded", subheaders)

    def test_no_manual_force_verify_action_exists_anywhere(self):
        """item 20 - source-inspection guard. Only bans WRITE-side forcing
        (calling update_intervention_status to set a worker-owned state, or
        a forcing UI action) - legitimate READ/DISPLAY comparisons like
        `if selected["latest_outcome"] == "VERIFIED"` are not violations."""
        for path in ("ui/pages/17_Energy_Opportunities.py", "ui/pages/18_Savings_Verification.py", "ui/savings_verification_data.py"):
            source = Path(path).read_text()
            for banned in ("update_intervention_status(", "Force Verification", "Mark Savings Verified", "Accept Savings"):
                self.assertNotIn(banned, source, msg=f"found {banned!r} in {path}")

    def test_no_systemctl_call_in_ui(self):
        for path in ("ui/pages/17_Energy_Opportunities.py", "ui/pages/18_Savings_Verification.py", "ui/savings_verification_data.py"):
            source = Path(path).read_text()
            self.assertNotIn("systemctl", source)

    def test_no_verification_mathematics_duplicated_in_ui(self):
        for path in ("ui/pages/17_Energy_Opportunities.py", "ui/pages/18_Savings_Verification.py", "ui/savings_verification_data.py"):
            source = Path(path).read_text()
            for banned in ("effective_tariff", "compute_window_baseline", "calculate_verified_savings", "_summarize(", "_classify("):
                self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
