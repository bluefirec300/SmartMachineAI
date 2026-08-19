import sqlite3
import unittest
from datetime import datetime, timedelta

from engine import health_domain as dom
from engine import maintenance_intelligence_engine as mie
from engine.maintenance_intelligence_targets import (
    CONFIDENCE_HIGH,
    CONFIDENCE_INSUFFICIENT,
    CONFIDENCE_LOW,
    CONFIDENCE_RANK,
    PRIORITY_BANDS,
    PRIORITY_NOT_ASSESSED,
    PRIORITY_REVIEW,
    PRIORITY_ROUTINE,
    PRIORITY_SORT_RANK,
    PRIORITY_URGENT_REVIEW,
    PRIORITY_VALUES,
)

from tests.test_health_engine import NOW, _seed_anomaly, _seed_equipment, _seed_event, _seed_mature_baseline
from tests.test_health_persistence import HealthPersistenceTestBase


class MaintenanceIntelligenceTestBase(HealthPersistenceTestBase):
    """Reuses Phase 12.2's own fixtures/base exactly (same config/machine
    db seeding, same water_supply_pump equipment) - Phase 13 must operate
    on REAL persisted Phase 12 snapshots, never a parallel fixture shape."""

    def _persist(self, result=None, reason="initial", now=None):
        result = result or self._calculate(now=now)
        dom.persist_snapshot(self.config_db, result, reason, now=now or NOW)
        return result

    def _set_next_due(self, next_due_at):
        connection = sqlite3.connect(self.config_db)
        connection.execute("UPDATE equipment SET next_due_at = ? WHERE id = ?", (next_due_at, self.equipment_id))
        connection.commit()
        connection.close()

    def _seed_fresh_events_feed(self):
        """Marks the events feed system-wide 'fresh' (Phase 12.1's own
        freshness gate) via a single INFO-severity row for this
        equipment's own tag - INFO doesn't qualify as an alarm/warning
        event, so the EVENTS factor becomes usable-and-clean rather than
        'missing', without itself contributing a penalty."""
        _seed_event(self.machine_db, "P01.WATER.WSP01.Vibration", "info_only", "info", NOW)

    def _seed_decayed_low_confidence_anomaly(self):
        """A RESOLVED, Low-confidence anomaly decayed to within ~0.005
        days of Phase 12.1's own RECENCY_DECAY_DAYS=30 boundary - its
        rounded penalty contribution is exactly 0.0 (status is still
        'penalized', not 'clean' - recency is a hair above zero, not
        literally zero), but its 'Low' statistical confidence still
        counts toward the persisted snapshot's own assessment_confidence
        via engine.health_engine._coverage_status(). This produces a
        real, reproducible 'assessed, HEALTHY, health_confidence=LOW,
        condition contribution rounds to exactly 0.0' fixture - the
        precise shape Correction 3's final pass targets."""
        resolved_at = (NOW - timedelta(days=29.995)).strftime("%Y-%m-%d %H:%M:%S")
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="RESOLVED", severity="WARNING", confidence="Low", resolved_at=resolved_at,
        )

    def _priority(self, now=None):
        return mie.calculate_maintenance_priority(
            self.config_db, self.plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01", now=now or NOW,
        )


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------

class TestRegistrySanity(unittest.TestCase):
    def test_priority_bands_cover_0_100_with_no_gaps(self):
        from engine.maintenance_intelligence_targets import priority_band_for_score
        for score in [0, 0.1, 24.99, 25, 25.01, 49.99, 50, 50.01, 74.99, 75, 75.01, 99.99, 100]:
            band = priority_band_for_score(score)
            self.assertIn(band, [b[2] for b in PRIORITY_BANDS])

    def test_not_assessed_is_not_a_scored_band(self):
        self.assertNotIn(PRIORITY_NOT_ASSESSED, [b[2] for b in PRIORITY_BANDS])

    def test_five_priority_values_defined(self):
        self.assertEqual(len(PRIORITY_VALUES), 5)
        self.assertIn(PRIORITY_NOT_ASSESSED, PRIORITY_VALUES)

    def test_sort_order_urgent_priority_review_not_assessed_routine(self):
        ordering = sorted(PRIORITY_SORT_RANK, key=lambda p: PRIORITY_SORT_RANK[p], reverse=True)
        self.assertEqual(ordering, [PRIORITY_URGENT_REVIEW, "PRIORITY", PRIORITY_REVIEW, PRIORITY_NOT_ASSESSED, PRIORITY_ROUTINE])

    def test_confidence_rank_ordering(self):
        self.assertLess(CONFIDENCE_RANK[CONFIDENCE_INSUFFICIENT], CONFIDENCE_RANK[CONFIDENCE_LOW])
        self.assertLess(CONFIDENCE_RANK[CONFIDENCE_LOW], CONFIDENCE_RANK["MEDIUM"])
        self.assertLess(CONFIDENCE_RANK["MEDIUM"], CONFIDENCE_RANK[CONFIDENCE_HIGH])


# ---------------------------------------------------------------------------
# CORRECTION 2 - NOT_ASSESSED vs ROUTINE (the critical guardrail check)
# ---------------------------------------------------------------------------

class TestNotAssessedVsRoutine(MaintenanceIntelligenceTestBase):
    def test_no_snapshot_and_no_overdue_maintenance_is_not_assessed(self):
        # No Phase 12 snapshot ever persisted, no maintenance schedule at all.
        result = self._priority()
        self.assertEqual(result.maintenance_priority, PRIORITY_NOT_ASSESSED)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_INSUFFICIENT)
        self.assertEqual(result.confidence_basis, [])
        self.assertIn("Condition assessment is limited by insufficient Equipment Health evidence.", result.limitations)

    def test_insufficient_health_coverage_and_no_overdue_is_not_assessed(self):
        # A real snapshot exists but coverage is INSUFFICIENT (no evidence seeded at all).
        self._persist()
        result = self._priority()
        self.assertIsNone(result.health_score)
        self.assertEqual(result.maintenance_priority, PRIORITY_NOT_ASSESSED)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_INSUFFICIENT)

    def test_healthy_and_adequately_assessed_with_zero_penalties_is_routine_not_not_assessed(self):
        """The exact failure mode the guardrail message called out: a
        genuinely HEALTHY, cleanly-assessed equipment must never fall
        into NOT_ASSESSED merely because its penalty contributions are
        zero - it has REAL, ADEQUATE evidence that was actually assessed."""
        self._seed_all_factors_clean()
        self._persist()
        result = self._priority()
        self.assertEqual(result.health_band, "HEALTHY")
        self.assertEqual(result.maintenance_priority, PRIORITY_ROUTINE)
        self.assertNotEqual(result.maintenance_priority, PRIORITY_NOT_ASSESSED)
        self.assertIn("health_condition", result.confidence_basis)
        self.assertNotEqual(result.recommendation_confidence, CONFIDENCE_INSUFFICIENT)


# ---------------------------------------------------------------------------
# CORRECTION 1 - overdue-maintenance REVIEW floor (revised Example C)
# ---------------------------------------------------------------------------

class TestOverdueFloor(MaintenanceIntelligenceTestBase):
    def test_healthy_equipment_with_overdue_maintenance_floors_to_review(self):
        self._seed_all_factors_clean()
        self._seed_fresh_events_feed()
        self._persist()
        self._set_next_due("2026-08-01")  # 19 days before NOW (2026-08-20)

        result = self._priority()

        self.assertEqual(result.health_band, "HEALTHY")  # Health completely untouched
        self.assertEqual(result.maintenance_priority, PRIORITY_REVIEW)
        self.assertTrue(result.floor_applied)
        self.assertEqual(result.floor_reason, "Scheduled maintenance is overdue.")
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_HIGH)
        self.assertIn("maintenance_overdue", result.confidence_basis)
        self.assertEqual(result.recommended_checks, ["Review overdue scheduled maintenance."])

    def test_floor_never_lowers_health_score_or_band(self):
        self._seed_all_factors_clean()
        healthy_result = self._calculate()
        self._persist(healthy_result)
        self._set_next_due("2026-07-01")

        result = self._priority()

        self.assertEqual(result.health_score, healthy_result.health_score)
        self.assertEqual(result.health_band, healthy_result.health_band)

    def test_floor_does_not_apply_when_not_overdue(self):
        self._seed_all_factors_clean()
        self._persist()
        self._set_next_due("2026-12-01")  # future - not overdue

        result = self._priority()

        self.assertFalse(result.floor_applied)
        self.assertIsNone(result.floor_reason)
        self.assertEqual(result.maintenance_priority, PRIORITY_ROUTINE)

    def test_no_snapshot_but_confirmed_overdue_still_yields_review(self):
        """Revised Example E2 - Health has no assessment at all, but
        overdue maintenance independently provides deterministic evidence."""
        self._set_next_due("2026-08-01")

        result = self._priority()

        self.assertIsNone(result.health_score)
        self.assertEqual(result.maintenance_priority, PRIORITY_REVIEW)
        self.assertTrue(result.floor_applied)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_HIGH)
        self.assertNotEqual(result.maintenance_priority, PRIORITY_NOT_ASSESSED)

    def test_floor_is_a_no_op_when_score_already_at_or_above_review(self):
        """Correction 1's own example: ATTENTION + overdue + recurring
        condition evidence may already reach PRIORITY/URGENT_REVIEW on
        the normal score alone - the floor must not misreport itself as
        having been the deciding factor in that case."""
        self._seed_all_factors_clean()
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="HIGH", confidence="High",
        )
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "BearingTemp", "wsp_bearing_temp_increase",
            status="OPEN", severity="HIGH", confidence="High",
        )
        self._persist()
        self._set_next_due("2026-08-01")

        result = self._priority()

        self.assertIn(result.maintenance_priority, ("PRIORITY", PRIORITY_URGENT_REVIEW, PRIORITY_REVIEW))
        if result.maintenance_priority != PRIORITY_REVIEW:
            self.assertFalse(result.floor_applied)


# ---------------------------------------------------------------------------
# CORRECTION 3 - evidence-aware confidence (Examples A/B/C/D)
# ---------------------------------------------------------------------------

class TestEvidenceAwareConfidence(MaintenanceIntelligenceTestBase):
    def test_example_a_condition_driven_confidence_not_degraded_by_missing_schedule(self):
        """Health ATTENTION-ish, strong HIGH-confidence condition evidence,
        NO maintenance schedule configured at all - confidence must stay
        HIGH, never dragged down by an uninvolved dimension."""
        self._seed_all_factors_clean()
        self._seed_fresh_events_feed()
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="WARNING", confidence="High",
        )
        self._persist()
        # equipment seeded via _seed_equipment() with no service_interval_days/
        # last_serviced_at/next_due_at at all -> has_schedule_data is False.

        result = self._priority()

        self.assertFalse(result.maintenance_status["has_schedule_data"])
        self.assertFalse(result.floor_applied)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_HIGH)
        self.assertIn("health_condition", result.confidence_basis)
        self.assertNotIn("maintenance_overdue", result.confidence_basis)

    def test_example_b_maintenance_driven_confidence_reflects_schedule_not_health(self):
        """Health HEALTHY (health didn't drive the REVIEW conclusion -
        the floor did) - confidence should reflect the schedule
        evidence's own reliability (HIGH, deterministic), not be
        dragged down or determined by health confidence."""
        self._seed_all_factors_clean()
        self._seed_fresh_events_feed()
        self._set_next_due("2026-08-05")  # set BEFORE persisting, so the persisted
        # snapshot's own MAINTENANCE factor genuinely reflects the overdue evidence
        # too (as it would after the next real Phase 12 worker cycle).
        self._persist()

        result = self._priority()

        self.assertEqual(result.maintenance_priority, PRIORITY_REVIEW)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_HIGH)

    def test_example_c_combined_confidence_is_conservative_minimum(self):
        """Health ATTENTION-ish with LOW-confidence condition evidence,
        PLUS confirmed overdue maintenance - combined confidence must be
        the conservative minimum among CONTRIBUTING sources (LOW), not
        HIGH and not an unconditional min() over uninvolved dimensions."""
        self._seed_all_factors_clean(confidence="Low")
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="WARNING", confidence="Low",
        )
        self._persist()
        self._set_next_due("2026-08-10")

        result = self._priority()

        self.assertIn("maintenance_overdue", result.confidence_basis)
        self.assertIn("health_condition", result.confidence_basis)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_LOW)

    def test_example_d_insufficient_is_not_assessed_with_insufficient_confidence(self):
        result = self._priority()
        self.assertEqual(result.maintenance_priority, PRIORITY_NOT_ASSESSED)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_INSUFFICIENT)

    def test_low_confidence_never_reduces_priority_or_floor_behavior(self):
        self._seed_all_factors_clean(confidence="Low")
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="HIGH", confidence="Low",
        )
        self._persist()
        self._set_next_due("2026-08-01")

        result = self._priority()

        self.assertEqual(result.recommendation_confidence, CONFIDENCE_LOW)
        # LOW confidence must not have suppressed the floor or lowered the priority class.
        self.assertIn(result.maintenance_priority, (PRIORITY_REVIEW, "PRIORITY", PRIORITY_URGENT_REVIEW))


# ---------------------------------------------------------------------------
# Energy Opportunity separation (ELECTRICAL_LOAD exclusion)
# ---------------------------------------------------------------------------

class TestElectricalLoadExclusion(MaintenanceIntelligenceTestBase):
    def test_electrical_load_factor_never_produces_a_recommended_check(self):
        self._seed_all_factors_clean()
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Power_kW", "wsp_power_deviation",
            status="OPEN", severity="HIGH", confidence="High",
        )
        self._persist()

        result = self._priority()

        for factor in result.priority_factors:
            self.assertNotEqual(factor.dimension, "electrical_load")
        for check in result.recommended_checks:
            self.assertNotIn("power", check.lower())
            self.assertNotIn("electrical", check.lower())

    def test_recommended_check_for_factor_rejects_electrical_load_family(self):
        from engine.maintenance_intelligence_targets import recommended_check_for_factor
        with self.assertRaises(ValueError):
            recommended_check_for_factor("wsp_power_load", "ELECTRICAL_LOAD")


# ---------------------------------------------------------------------------
# Maintenance context / recommended checks / result contract shape
# ---------------------------------------------------------------------------

class TestResultContract(MaintenanceIntelligenceTestBase):
    def test_maintenance_context_is_structured_not_free_text(self):
        result = self._priority()
        self.assertIn("last_serviced_at", result.maintenance_context)
        self.assertIn("recent_log_count_by_category", result.maintenance_context)
        self.assertIsInstance(result.maintenance_context["recent_log_count_by_category"], dict)

    def test_recommended_checks_deduplicated(self):
        self._seed_all_factors_clean()
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="HIGH", confidence="High",
        )
        self._persist()
        self._set_next_due("2026-08-01")

        result = self._priority()
        self.assertEqual(len(result.recommended_checks), len(set(result.recommended_checks)))

    def test_model_version_and_generated_at_present(self):
        result = self._priority()
        self.assertEqual(result.model_version, mie.MAINTENANCE_INTELLIGENCE_MODEL_VERSION)
        self.assertTrue(result.generated_at)

    def test_priority_score_never_none_even_when_not_assessed(self):
        result = self._priority()
        self.assertIsInstance(result.priority_score, float)


# ---------------------------------------------------------------------------
# Final semantic correction - assessed_evidence vs recommendation_contributors
# (Cases 1-6 exactly as specified in the correction request). Case 2 is the
# primary regression test for the confirmed bug: a floor-driven REVIEW must
# not have its confidence contaminated by unrelated Health confidence when
# Health's own contribution to the recommendation is exactly zero.
# ---------------------------------------------------------------------------

class TestAssessedEvidenceVsRecommendationContributors(MaintenanceIntelligenceTestBase):
    def test_case_1_healthy_valid_assessment_no_overdue_no_concern_is_routine(self):
        self._seed_all_factors_clean()
        self._seed_fresh_events_feed()
        self._persist()

        result = self._priority()

        self.assertEqual(result.maintenance_priority, PRIORITY_ROUTINE)
        self.assertFalse(result.floor_applied)
        # Health assessment counts as assessed evidence AND, on the
        # ordinary (non-floor) path, as a recommendation contributor.
        self.assertIn("health_condition", result.confidence_basis)
        self.assertNotEqual(result.recommendation_confidence, CONFIDENCE_INSUFFICIENT)

    def test_case_2_healthy_overdue_zero_health_contribution_review_confidence_unpolluted(self):
        """PRIMARY REGRESSION TEST for the confirmed bug. Health is
        assessed (HEALTHY, health_score=100.0) but its OWN Low
        statistical confidence must not leak into recommendation_confidence
        when health's rounded contribution to the recommendation is
        exactly zero and the REVIEW verdict came solely from the
        overdue floor."""
        self._seed_all_factors_clean()
        self._seed_fresh_events_feed()
        self._seed_decayed_low_confidence_anomaly()
        result_to_persist = self._calculate()
        self.assertEqual(result_to_persist.health_score, 100.0)
        self.assertEqual(result_to_persist.health_band, "HEALTHY")
        self.assertEqual(result_to_persist.assessment_confidence, CONFIDENCE_LOW)
        self._persist(result_to_persist)
        self._set_next_due("2026-08-01")

        result = self._priority()

        self.assertEqual(result.health_score, 100.0)
        self.assertEqual(result.health_band, "HEALTHY")
        self.assertEqual(result.health_confidence, CONFIDENCE_LOW)  # Health's own confidence, unaffected
        self.assertEqual(result.maintenance_priority, PRIORITY_REVIEW)
        self.assertTrue(result.floor_applied)
        self.assertNotIn("health_condition", result.confidence_basis)
        self.assertEqual(result.confidence_basis, ["maintenance_overdue"])
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_HIGH)

    def test_case_3_poor_condition_no_schedule_confidence_from_condition_only(self):
        self._seed_all_factors_clean()
        self._seed_fresh_events_feed()
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="HIGH", confidence="High",
        )
        self._persist()
        # equipment has no service_interval_days/last_serviced_at/next_due_at -> has_schedule_data is False.

        result = self._priority()

        self.assertFalse(result.maintenance_status["has_schedule_data"])
        self.assertFalse(result.floor_applied)
        self.assertIn("health_condition", result.confidence_basis)
        self.assertNotIn("maintenance_overdue", result.confidence_basis)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_HIGH)  # from the genuine condition evidence

    def test_case_4_poor_condition_and_confirmed_overdue_both_contributors(self):
        self._seed_all_factors_clean(confidence="Low")
        self._seed_fresh_events_feed()
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="WARNING", confidence="Low",
        )
        self._persist()
        self._set_next_due("2026-08-10")

        result = self._priority()

        self.assertIn("maintenance_overdue", result.confidence_basis)
        self.assertIn("health_condition", result.confidence_basis)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_LOW)  # conservative minimum

    def test_case_5_no_usable_assessment_no_overdue_is_not_assessed(self):
        result = self._priority()
        self.assertEqual(result.maintenance_priority, PRIORITY_NOT_ASSESSED)
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_INSUFFICIENT)
        self.assertEqual(result.confidence_basis, [])

    def test_case_6_no_usable_assessment_confirmed_overdue_still_review(self):
        self._set_next_due("2026-08-01")

        result = self._priority()

        self.assertIsNone(result.health_score)
        self.assertEqual(result.maintenance_priority, PRIORITY_REVIEW)
        self.assertTrue(result.floor_applied)
        self.assertEqual(result.confidence_basis, ["maintenance_overdue"])
        self.assertEqual(result.recommendation_confidence, CONFIDENCE_HIGH)

    def test_health_still_counts_as_recommendation_contributor_under_floor_when_materially_nonzero(self):
        """The gate is 'not floor_applied OR a real nonzero contribution'
        - not a blanket exclusion of health whenever a floor applies. A
        real (non-negligible) condition penalty must still participate
        in confidence even when the floor also fires."""
        self._seed_all_factors_clean()
        self._seed_fresh_events_feed()
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="HIGH", confidence="High",
        )
        self._persist()
        self._set_next_due("2026-08-19")  # 1 day overdue - floor still needed since score alone stays low

        result = self._priority()

        self.assertTrue(result.floor_applied)
        self.assertIn("health_condition", result.confidence_basis)


# ---------------------------------------------------------------------------
# Source guards - no LLM, no persistence, no live health recalculation
# ---------------------------------------------------------------------------

def _source_without_module_docstring(path):
    """Strips the leading module docstring before scanning for banned
    call-site patterns - the docstring itself legitimately explains
    what this module does NOT do (e.g. 'never calls
    engine.health_engine.calculate_health()'), which would otherwise
    self-trip a bare-substring check. A well-established recurring
    false-positive class in this project's own test suite (Phase 12.1/
    12.3/12.3A/12.4 all hit this)."""
    with open(path) as handle:
        text = handle.read()
    marker = '"""'
    first = text.find(marker)
    second = text.find(marker, first + 3)
    if first == -1 or second == -1:
        return text
    return text[:first] + text[second + 3:]


class TestSourceGuards(unittest.TestCase):
    def test_engine_never_calls_calculate_health(self):
        source = _source_without_module_docstring("engine/maintenance_intelligence_engine.py")
        self.assertNotIn("calculate_health(", source)

    def test_engine_has_no_llm_or_ai_provider_imports(self):
        source = _source_without_module_docstring("engine/maintenance_intelligence_engine.py")
        for banned in ("ai_provider", "ai.prompt_builder", "ai.providers", "ai.root_cause_engine"):
            self.assertNotIn(banned, source)

    def test_engine_performs_no_write_queries(self):
        source = _source_without_module_docstring("engine/maintenance_intelligence_engine.py")
        for banned in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE"):
            self.assertNotIn(banned, source)

    def test_targets_module_has_no_electrical_load_recommended_checks(self):
        from engine.health_targets import HEALTH_FACTOR_REGISTRY, FAMILY_ELECTRICAL_LOAD
        from engine.maintenance_intelligence_targets import RECOMMENDED_CHECK_BY_FACTOR_ID
        electrical_factor_ids = {
            f.factor_id for factors in HEALTH_FACTOR_REGISTRY.values() for f in factors
            if f.family == FAMILY_ELECTRICAL_LOAD
        }
        self.assertTrue(electrical_factor_ids.isdisjoint(RECOMMENDED_CHECK_BY_FACTOR_ID))


if __name__ == "__main__":
    unittest.main()
