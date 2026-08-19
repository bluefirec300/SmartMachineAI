import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from database.database import DatabaseManager
from engine import savings_verification_domain as dom
from engine import savings_verification_evidence as ev
from engine.baseline_targets import BaselineTarget
from engine.opportunity_migrator import migrate as migrate_opportunity
from engine.savings_verification_migrator import migrate as migrate_savings_verification
from engine.savings_verification_targets import ACTION_CATEGORY_SETPOINT_CHANGE

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _plant_id


# Well after MODERN_DATA_BOUNDARY (2026-08-09), matching every prior
# phase's test convention.
IMPLEMENTED_AT = datetime(2026, 8, 25, 0, 0, 0)


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


def _seed_flat_series(historian: DatabaseManager, tag: str, start: datetime, end: datetime, value: float, hour_of_day: int = 10) -> None:
    day = start.date()
    while datetime.combine(day, datetime.min.time()) <= end:
        t = datetime.combine(day, datetime.min.time()) + timedelta(hours=hour_of_day)
        if start <= t <= end:
            historian.save_tag(tag, "sim", value, t)
        day += timedelta(days=1)


class EvidenceTestBase(unittest.TestCase):
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
        self.intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "test_engineer",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _implement(self, stabilization_days: int | None = None):
        if stabilization_days is not None:
            connection = sqlite3.connect(self.config_db)
            connection.execute("UPDATE savings_interventions SET stabilization_days = ? WHERE id = ?", (stabilization_days, self.intervention_id))
            connection.commit()
            connection.close()
        dom.mark_implemented(self.config_db, self.intervention_id, implemented_at=IMPLEMENTED_AT)


# ---------------------------------------------------------------------------
# Pure boundary functions (items C/D/E, no DB needed)
# ---------------------------------------------------------------------------

class TestReferenceWindowBoundary(unittest.TestCase):
    def test_reference_end_is_strictly_before_implemented_at(self):
        start, end = ev.compute_reference_window(IMPLEMENTED_AT, reference_window_days_max=60)
        self.assertLess(end, IMPLEMENTED_AT)
        self.assertEqual(IMPLEMENTED_AT - end, timedelta(seconds=1))

    def test_reference_window_frozen_regardless_of_when_computed(self):
        """Item 2's core requirement - the reference window must never
        drift forward as time passes."""
        window_a = ev.compute_reference_window(IMPLEMENTED_AT, reference_window_days_max=60)
        window_b = ev.compute_reference_window(IMPLEMENTED_AT, reference_window_days_max=60)
        self.assertEqual(window_a, window_b)  # deterministic, no hidden "now" dependency at all

    def test_reference_window_span_matches_reference_window_days_max(self):
        start, end = ev.compute_reference_window(IMPLEMENTED_AT, reference_window_days_max=30)
        self.assertEqual((end - start).days, 30)


class TestAfterWindowBoundary(unittest.TestCase):
    def test_after_start_is_implemented_at_plus_stabilization_inclusive(self):
        window = ev.compute_after_window(IMPLEMENTED_AT, stabilization_days=3, now=IMPLEMENTED_AT + timedelta(days=10))
        self.assertIsNotNone(window)
        self.assertEqual(window[0], IMPLEMENTED_AT + timedelta(days=3))

    def test_after_window_none_when_stabilization_not_finished(self):
        window = ev.compute_after_window(IMPLEMENTED_AT, stabilization_days=3, now=IMPLEMENTED_AT + timedelta(days=1))
        self.assertIsNone(window)

    def test_after_window_none_exactly_one_second_before_eligible(self):
        eligible = IMPLEMENTED_AT + timedelta(days=3)
        window = ev.compute_after_window(IMPLEMENTED_AT, stabilization_days=3, now=eligible - timedelta(seconds=1))
        self.assertIsNone(window)

    def test_after_window_valid_exactly_at_eligible_instant(self):
        eligible = IMPLEMENTED_AT + timedelta(days=3)
        window = ev.compute_after_window(IMPLEMENTED_AT, stabilization_days=3, now=eligible)
        self.assertIsNotNone(window)
        self.assertEqual(window, (eligible, eligible))

    def test_stabilization_zero_makes_after_start_equal_implemented_at(self):
        window = ev.compute_after_window(IMPLEMENTED_AT, stabilization_days=0, now=IMPLEMENTED_AT + timedelta(hours=1))
        self.assertIsNotNone(window)
        self.assertEqual(window[0], IMPLEMENTED_AT)

    def test_unusually_long_stabilization_correctly_returns_none_if_not_yet_elapsed(self):
        window = ev.compute_after_window(IMPLEMENTED_AT, stabilization_days=365, now=IMPLEMENTED_AT + timedelta(days=30))
        self.assertIsNone(window)

    def test_future_implementation_date_is_handled_like_any_unfinished_stabilization(self):
        future_implemented_at = IMPLEMENTED_AT + timedelta(days=100)
        window = ev.compute_after_window(future_implemented_at, stabilization_days=3, now=IMPLEMENTED_AT)
        self.assertIsNone(window)

    def test_zero_gap_stabilization_never_double_counts_boundary_sample(self):
        """A sample recorded at EXACTLY implemented_at must land in
        AFTER (>= eligible_start) and never in BEFORE (> reference_end,
        which is implemented_at - 1s) - no overlap, no double counting."""
        ref_start, ref_end = ev.compute_reference_window(IMPLEMENTED_AT, reference_window_days_max=60)
        after_window = ev.compute_after_window(IMPLEMENTED_AT, stabilization_days=0, now=IMPLEMENTED_AT)
        self.assertLess(ref_end, IMPLEMENTED_AT)
        self.assertGreaterEqual(after_window[0], IMPLEMENTED_AT)
        self.assertLess(ref_end, after_window[0])


# ---------------------------------------------------------------------------
# Evidence-quality tiering (item G) - tested directly against synthetic
# window results, isolated from real context-matching noise.
# ---------------------------------------------------------------------------

class TestEvidenceQualityTiering(unittest.TestCase):
    def _target(self, context_dimensions=("load_bucket",)):
        return BaselineTarget(
            plant_code="p01", instance_key="P01.UTILITY.CHL01", equipment_type="chiller", target_key="Power_kW",
            tag_name="P01.UTILITY.CHL01.Power_kW", is_derived=False, context_dimensions=context_dimensions,
            aggregation_minutes=5, reference_window_days_max=60, recent_window_days=7,
        )

    def _window(self, status, level, context_used=None):
        return {"status": status, "level": level, "context_used": context_used or {}, "missing_context": []}

    def test_insufficient_when_either_window_unavailable(self):
        ref = self._window("unavailable", "D")
        after = self._window("mature", "A", {"load_bucket": "low"})
        self.assertEqual(ev.classify_evidence_quality(ref, after, set(), self._target()), ev.EVIDENCE_INSUFFICIENT)

    def test_limited_when_either_window_only_bootstrap(self):
        ref = self._window("bootstrap", "C")
        after = self._window("mature", "A", {"load_bucket": "low"})
        self.assertEqual(ev.classify_evidence_quality(ref, after, set(), self._target()), ev.EVIDENCE_LIMITED)

    def test_limited_when_both_mature_but_no_shared_dimension(self):
        ref = self._window("mature", "B", {"hour_bucket": "08-12"})
        after = self._window("mature", "B", {"load_bucket": "low"})
        self.assertEqual(ev.classify_evidence_quality(ref, after, set(), self._target()), ev.EVIDENCE_LIMITED)

    def test_strong_when_both_mature_level_ab_and_shared_dimension(self):
        ref = self._window("mature", "A", {"load_bucket": "low"})
        after = self._window("mature", "B", {"load_bucket": "low"})
        self.assertEqual(ev.classify_evidence_quality(ref, after, {"load_bucket"}, self._target()), ev.EVIDENCE_STRONG)

    def test_context_matched_when_shared_but_level_c(self):
        ref = self._window("mature", "C", {"load_bucket": "low"})
        after = self._window("mature", "C", {"load_bucket": "low"})
        self.assertEqual(ev.classify_evidence_quality(ref, after, {"load_bucket"}, self._target()), ev.EVIDENCE_CONTEXT_MATCHED)

    def test_target_with_no_context_dimensions_never_forced_to_limited_for_missing_shared_dims(self):
        """A target with context_dimensions=() has nothing to share by
        definition - that must not be conflated with 'context available
        but not matched', which would wrongly cap it at LIMITED. Both
        windows are mature at level B here, so the correct outcome is
        STRONG - the missing-shared-dims penalty must never fire for a
        target that never had any context dimension to begin with."""
        ref = self._window("mature", "B", {})
        after = self._window("mature", "B", {})
        target = self._target(context_dimensions=())
        self.assertEqual(ev.classify_evidence_quality(ref, after, set(), target), ev.EVIDENCE_STRONG)


# ---------------------------------------------------------------------------
# Full orchestration - real seeded historian/config data
# ---------------------------------------------------------------------------

class TestComputeVerificationEvidence(EvidenceTestBase):
    def test_no_intervention_returns_insufficient(self):
        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, 99999, now=IMPLEMENTED_AT)
        self.assertEqual(result["evidence_quality"], "INSUFFICIENT")

    def test_intervention_not_implemented_returns_insufficient(self):
        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT)
        self.assertEqual(result["evidence_quality"], "INSUFFICIENT")
        self.assertIn("not yet implemented", result["reason"])

    def test_zero_before_data_is_insufficient(self):
        self._implement(stabilization_days=2)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT + timedelta(days=3), IMPLEMENTED_AT + timedelta(days=13), 50.0)
        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=15))
        self.assertEqual(result["reference_sample_count"], 0)
        self.assertEqual(result["evidence_quality"], "INSUFFICIENT")

    def test_zero_after_data_is_insufficient(self):
        self._implement(stabilization_days=2)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=3))
        self.assertEqual(result["after_sample_count"], 0)
        self.assertEqual(result["evidence_quality"], "INSUFFICIENT")

    def test_only_one_side_has_data_is_insufficient(self):
        self._implement(stabilization_days=0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        # No AFTER data seeded at all.
        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=5))
        self.assertGreater(result["reference_sample_count"], 0)
        self.assertEqual(result["after_sample_count"], 0)
        self.assertEqual(result["evidence_quality"], "INSUFFICIENT")

    def test_stabilization_period_data_never_counted_in_after(self):
        """A sample recorded during the stabilization gap (day 1, before
        the 3-day stabilization elapses) must not appear in AFTER."""
        self._implement(stabilization_days=3)
        self.historian.save_tag("P01.UTILITY.CHL01.Power_kW", "sim", 999.0, IMPLEMENTED_AT + timedelta(days=1))  # inside stabilization gap
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT + timedelta(days=4), IMPLEMENTED_AT + timedelta(days=14), 50.0)
        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=15))
        # eligible_after_start must be day+3, never day+1 (the stabilization-gap sample's day).
        self.assertEqual(result["eligible_after_start"], (IMPLEMENTED_AT + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"))

    def test_no_post_intervention_leakage_into_reference(self):
        self._implement(stabilization_days=0)
        # A sample recorded exactly AT implemented_at must never count toward reference.
        self.historian.save_tag("P01.UTILITY.CHL01.Power_kW", "sim", 999.0, IMPLEMENTED_AT)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=5))
        self.assertLess(result["reference_end"], IMPLEMENTED_AT.strftime("%Y-%m-%d %H:%M:%S"))

    def test_result_is_deterministic_across_repeated_calls(self):
        self._implement(stabilization_days=2)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT + timedelta(days=3), IMPLEMENTED_AT + timedelta(days=13), 50.0)
        now = IMPLEMENTED_AT + timedelta(days=15)
        first = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=now)
        second = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=now)
        self.assertEqual(first, second)

    def test_no_database_mutation_from_evidence_selection(self):
        """Evidence selection is read-only - row counts across every
        table must be identical before/after a call."""
        self._implement(stabilization_days=2)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT + timedelta(days=3), IMPLEMENTED_AT + timedelta(days=13), 50.0)

        def _counts():
            connection = sqlite3.connect(self.config_db)
            counts = {
                t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("savings_interventions", "savings_verification_results", "energy_opportunities", "anomalies", "baseline_context_summary")
            }
            connection.close()
            return counts

        before = _counts()
        ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=15))
        after = _counts()
        self.assertEqual(before, after)

    def test_no_verification_results_row_is_ever_inserted(self):
        self._implement(stabilization_days=2)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT + timedelta(days=3), IMPLEMENTED_AT + timedelta(days=13), 50.0)
        ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=15))
        self.assertEqual(len(dom.list_verification_history(self.config_db, self.intervention_id)), 0)

    def test_intervention_status_never_advanced_by_evidence_selection(self):
        self._implement(stabilization_days=2)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT + timedelta(days=3), IMPLEMENTED_AT + timedelta(days=13), 50.0)
        ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=15))
        row = dom.get_intervention(self.config_db, self.intervention_id)
        self.assertEqual(row["status"], "IMPLEMENTED")  # never auto-advanced to VERIFICATION_* or COMPLETED

    def test_no_financial_fields_present_in_result(self):
        """Structural guard - the domain result must contain no
        savings/cost/energy key at all."""
        self._implement(stabilization_days=2)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT + timedelta(days=3), IMPLEMENTED_AT + timedelta(days=13), 50.0)
        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=15))
        serialized = json.dumps(result).lower()
        for banned in ("verified_energy", "verified_cost", "saving", "roi", "payback", "avoided_cost"):
            self.assertNotIn(banned, serialized)

    def test_missing_stabilization_days_falls_back_to_category_default(self):
        # stabilization_days left NULL on this intervention (never set via _implement's optional override).
        dom.mark_implemented(self.config_db, self.intervention_id, implemented_at=IMPLEMENTED_AT)
        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(hours=1))
        # SETPOINT_CHANGE's default is 2 days - "now" is only 1 hour past implemented_at, so still insufficient/pending.
        self.assertEqual(result["evidence_quality"], "INSUFFICIENT")
        self.assertIn("stabilization period not finished", result["reason"])


class TestExclusionAccounting(EvidenceTestBase):
    def test_maintenance_window_overlap_is_reported_not_silently_dropped(self):
        self._implement(stabilization_days=0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT, IMPLEMENTED_AT + timedelta(days=10), 50.0)

        connection = sqlite3.connect(self.config_db)
        equipment_id = connection.execute("SELECT id FROM equipment WHERE name = 'p01_chiller_chl01'").fetchone()[0]
        connection.execute(
            "INSERT INTO maintenance_log (equipment_id, category, description, performed_at, next_due_at, created_at) VALUES (?, 'preventive', 'test', ?, NULL, ?)",
            (equipment_id, (IMPLEMENTED_AT - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.commit()
        connection.close()

        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=12))
        self.assertGreater(result["exclusion_summary"]["maintenance_windows_overlapping_reference"], 0)
        # Reported, not silently dropped from the sample count entirely.
        self.assertGreater(result["reference_sample_count"], 0)

    def test_anomaly_overlap_is_reported_never_used_to_exclude(self):
        """Item 5's explicit direction - an anomaly's existence must
        never automatically exclude data."""
        self._implement(stabilization_days=0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT - timedelta(days=10), IMPLEMENTED_AT - timedelta(seconds=1), 60.0)
        _seed_flat_series(self.historian, "P01.UTILITY.CHL01.Power_kW", IMPLEMENTED_AT, IMPLEMENTED_AT + timedelta(days=10), 50.0)

        connection = sqlite3.connect(self.config_db)
        now_text = (IMPLEMENTED_AT + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        row = dict(
            plant_id=self.plant_id, equipment_id=None, instance_key="P01.UTILITY.CHL01", target_key="Power_kW",
            rule_key="chiller_power_deviation", anomaly_type="deviation", category="UTILITY", title="t",
            severity="ATTENTION", confidence="High", provisional=0, status="RESOLVED",
            first_detected=now_text, last_seen=now_text, resolved_at=now_text, occurrence_count=1,
            open_persistence_periods=3, resolve_persistence_periods=0, last_bucket_start=now_text,
            actual_value=90.0, expected_value=50.0, expected_low=40.0, expected_high=60.0, deviation_absolute=40.0,
            deviation_percent=80.0, normalized_deviation=13.3, baseline_type="recent", baseline_level="B",
            baseline_confidence="High", engineering_limit_status="not_exceeded", engineering_limit_event_id=None,
            estimated_excess_energy_kwh=1.0, estimated_excess_cost=1.0, estimated_excess_cost_currency="MYR",
            threshold_provenance="SIMULATION_TUNING", source_tags="[]", evidence_json="{}", assumptions_json="[]",
            data_limitations_json="[]", created_at=now_text, updated_at=now_text,
        )
        cols = list(row.keys())
        connection.execute(f"INSERT INTO anomalies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
        connection.commit()
        connection.close()

        result = ev.compute_verification_evidence(self.config_db, self.machine_db, self.historian, self.intervention_id, now=IMPLEMENTED_AT + timedelta(days=12))
        self.assertEqual(result["exclusion_summary"]["anomaly_occurrences_overlapping_after"], 1)
        # The anomaly-overlapping samples remain counted, not excluded.
        self.assertGreater(result["after_sample_count"], 0)


class TestResolveTargetForIntervention(EvidenceTestBase):
    def test_resolves_the_correct_target(self):
        intervention = dom.get_intervention(self.config_db, self.intervention_id)
        target = ev.resolve_target_for_intervention(self.config_db, intervention)
        self.assertIsNotNone(target)
        self.assertEqual(target.instance_key, "P01.UTILITY.CHL01")
        self.assertEqual(target.target_key, "Power_kW")
        self.assertEqual(target.equipment_type, "chiller")

    def test_returns_none_for_unresolvable_opportunity(self):
        connection = sqlite3.connect(self.config_db)
        connection.execute("UPDATE energy_opportunities SET rule_key = 'not_a_real_rule' WHERE id = ?", (self.opportunity_id,))
        connection.commit()
        connection.close()
        intervention = dom.get_intervention(self.config_db, self.intervention_id)
        target = ev.resolve_target_for_intervention(self.config_db, intervention)
        self.assertIsNone(target)


# ---------------------------------------------------------------------------
# Source-inspection guards - no financial calculation, no result writes.
# ---------------------------------------------------------------------------

class TestSourceInspection(unittest.TestCase):
    def test_no_financial_calculation_in_evidence_module(self):
        source = Path("engine/savings_verification_evidence.py").read_text()
        for banned in ("verified_energy_kwh =", "verified_cost =", "effective_tariff", "bucket_excess_energy_cost"):
            self.assertNotIn(banned, source)

    def test_evidence_module_never_calls_record_verification_result(self):
        """Checks for an actual CALL (dom.record_verification_result(...))
        - the module's own docstring legitimately mentions the function
        by name to explain why it's never invoked, so a bare substring
        search would false-positive on that explanatory text."""
        source = Path("engine/savings_verification_evidence.py").read_text()
        self.assertNotIn("dom.record_verification_result(", source)

    def test_evidence_module_never_calls_update_intervention_status(self):
        source = Path("engine/savings_verification_evidence.py").read_text()
        self.assertNotIn("update_intervention_status(", source)


if __name__ == "__main__":
    unittest.main()
