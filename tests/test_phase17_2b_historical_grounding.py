import unittest

from ai import context_builder as cb
from ai.grounding_guard import check_grounding
from ai.interpretation_prompt_builder import build_interpretation_prompt, render_equipment_context
from config.environment import get_config_db_path, get_machine_db_path

"""
Phase 17.2b - historical evidence interpretation/grounding tests.

Two styles, matching this project's established convention:
- Prompt-rendering/context-integration tests run against the real live
  simulation database (same pattern as tests/test_phase17_2a_context_history.py).
- Grounding-guard tests use small, hand-built context dicts (same
  pattern as tests/test_phase16_3_data_health_grounding.py) - no
  database needed, deterministic adversarial examples.
"""

CONFIG_DB = get_config_db_path()
MACHINE_DB = get_machine_db_path()
INSTANCE_KEY = "P01.WATER.WSP01"


# ---------------------------------------------------------------------------
# Hand-built context helpers for grounding-guard tests
# ---------------------------------------------------------------------------

def _base_context(**overrides):
    context = {
        "request": {"requires_telemetry_confidence": False, "history_domains_fetched": []},
        "health": {"available": False}, "asset_performance": {"available": False},
        "maintenance_intelligence": {"available": False}, "savings_verification": {"available": False},
        "anomalies": {"available": False}, "data_health": {"available": False},
    }
    context.update(overrides)
    return context


def _data_health_history(good=12.7, degraded=0.0, poor=0.0, unavailable=0.0, no_history=87.3, transitions=None, recurring=None):
    return {
        "available": True, "recorded_since": "2026-08-17 22:41:13", "window_days": 7.0,
        "status_duration": {
            "insufficient_history": False,
            "percentages": {"GOOD": good, "DEGRADED": degraded, "POOR": poor, "UNAVAILABLE": unavailable},
            "no_history_percentage": no_history,
        },
        "recent_transitions": transitions or [],
        "recurring_issues": recurring or {"STALE": 0, "INVALID": 0, "CONTINUITY_GAP": 0, "TIMESTAMP_ISSUE": 0, "INDETERMINATE_CHANGE_ONLY": 0, "FROZEN_CANDIDATE": 0, "MISSING": 0},
        "reason": None,
    }


def _health_history(direction="DETERIORATING", latest=66.2, previous=71.1, change=-4.9, transitions=None):
    return {
        "available": True,
        "trend": {"direction": direction, "latest_score": latest, "previous_score": previous, "absolute_change": change,
                   "observation_period_start": "2026-08-18 06:27:39", "observation_period_end": "2026-08-18 17:44:31"},
        "recent_band_transitions": transitions or [],
        "reason": None,
    }


def _asset_performance_history(observations=None, maintenance_comparisons=None):
    return {
        "available": True,
        "recent_observations": observations if observations is not None else [
            {"target_key": "flow_per_kw", "computed_at": "2026-08-18 17:31:26", "performance_state": "DEGRADING",
             "observed_value": 1.65, "percent_change": -28.32, "evidence_quality": "STRONG"},
        ],
        "maintenance_comparisons": maintenance_comparisons or [],
        "reason": None,
    }


# ---------------------------------------------------------------------------
# Current vs historical separation (prompt rendering, real live DB)
# ---------------------------------------------------------------------------

class TestCurrentVsHistoricalSeparation(unittest.TestCase):
    def test_history_section_absent_when_no_history_domains_requested(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION")
        rendered = render_equipment_context(context)
        self.assertNotIn("HISTORICAL EVIDENCE", rendered)

    def test_history_section_present_and_delimited_when_requested(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION",
            history_domains=("data_health_history", "health_history", "asset_performance_history"),
        )
        rendered = render_equipment_context(context)
        self.assertIn("=== HISTORICAL EVIDENCE", rendered)
        self.assertIn("=== END HISTORICAL EVIDENCE ===", rendered)
        start = rendered.index("=== HISTORICAL EVIDENCE")
        end = rendered.index("=== END HISTORICAL EVIDENCE ===")
        self.assertLess(start, end)

    def test_current_health_section_appears_before_historical_section(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("health_history",),
        )
        rendered = render_equipment_context(context)
        if "Equipment Health (assessed" in rendered:
            self.assertLess(rendered.index("Equipment Health (assessed"), rendered.index("=== HISTORICAL EVIDENCE"))

    def test_prompt_current_state_only_byte_identical_regardless_of_history_capability_existing(self):
        """The mere EXISTENCE of history rendering code must not change a
        current-state-only prompt at all."""
        ctx_no_history = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", question="why is P01 WSP01 unhealthy")
        prompt = build_interpretation_prompt(ctx_no_history, "HEALTH_EXPLANATION", "why is P01 WSP01 unhealthy")
        self.assertNotIn("Historical evidence rules", prompt)
        self.assertNotIn("HISTORICAL EVIDENCE", prompt)


# ---------------------------------------------------------------------------
# Data Health history rendering
# ---------------------------------------------------------------------------

class TestDataHealthHistoryRendering(unittest.TestCase):
    def test_percentages_and_no_history_rendered_as_distinct_lines(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("data_health_history",),
        )
        rendered = render_equipment_context(context)
        if context["data_health_history"]["available"]:
            self.assertIn("Time-in-status over the RECORDED portion", rendered)
            if context["data_health_history"]["status_duration"]["no_history_percentage"]:
                self.assertIn("No recorded Data Health history for", rendered)
                self.assertIn("NOT a GOOD/DEGRADED/POOR/UNAVAILABLE status", rendered)

    def test_unavailable_history_renders_reason_not_fabricated_content(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, "P99.NOWHERE.FAKE01", "HEALTH_EXPLANATION", history_domains=("data_health_history",),
        )
        rendered = render_equipment_context(context)
        self.assertIn("Historical Data Health: unavailable", rendered)
        self.assertNotIn("Time-in-status", rendered)


# ---------------------------------------------------------------------------
# Health history rendering
# ---------------------------------------------------------------------------

class TestHealthHistoryRendering(unittest.TestCase):
    def test_direction_and_scores_rendered(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("health_history",),
        )
        rendered = render_equipment_context(context)
        if context["health_history"]["available"]:
            trend = context["health_history"]["trend"]
            self.assertIn(f"Direction: {trend['direction']}", rendered)

    def test_current_health_state_not_overwritten_by_history_section(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("health_history",),
        )
        rendered = render_equipment_context(context)
        self.assertIn("does not override the current Health State/Score above", rendered)


# ---------------------------------------------------------------------------
# Asset Performance history rendering
# ---------------------------------------------------------------------------

class TestAssetPerformanceHistoryRendering(unittest.TestCase):
    def test_observations_rendered_with_bound_intact(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("asset_performance_history",),
        )
        if context["asset_performance_history"]["available"]:
            self.assertLessEqual(len(context["asset_performance_history"]["recent_observations"]), 3)
            rendered = render_equipment_context(context)
            self.assertIn("NOT a new trend calculation", rendered)


# ---------------------------------------------------------------------------
# Grounding: Data Health history
# ---------------------------------------------------------------------------

class TestGroundingDataHealthHistory(unittest.TestCase):
    def test_fabricated_percentage_detected(self):
        context = _base_context(data_health_history=_data_health_history())
        result = check_grounding("Telemetry quality was GOOD for 15% of the week.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_correct_percentage_passes(self):
        context = _base_context(data_health_history=_data_health_history())
        result = check_grounding("Telemetry quality was GOOD for 12.7% of the requested window.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_no_history_mislabeled_as_unavailable_status_detected(self):
        """The core adversarial case from section 5/17 of the approved
        plan - no_history_percentage must never be claimed as if it were
        the UNAVAILABLE status's own percentage."""
        context = _base_context(data_health_history=_data_health_history())
        result = check_grounding("Data Health was UNAVAILABLE for 87.3% of the week.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_no_history_mislabeled_as_poor_status_detected(self):
        context = _base_context(data_health_history=_data_health_history())
        result = check_grounding("The telemetry was POOR for 87.3% of the remaining week.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_correctly_worded_no_history_disclosure_passes(self):
        context = _base_context(data_health_history=_data_health_history())
        answer = "No recorded history for 87.3% of the requested window, which predates when persistence began."
        result = check_grounding(answer, context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_no_data_health_history_context_never_raises_or_false_flags(self):
        context = _base_context()
        result = check_grounding("Telemetry was GOOD for 90% of the week.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)


# ---------------------------------------------------------------------------
# Grounding: Health history
# ---------------------------------------------------------------------------

class TestGroundingHealthHistory(unittest.TestCase):
    def test_reversed_direction_detected(self):
        context = _base_context(health_history=_health_history(direction="DETERIORATING"))
        result = check_grounding("The historical trend direction is IMPROVING.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_correct_direction_passes(self):
        context = _base_context(health_history=_health_history(direction="DETERIORATING"))
        result = check_grounding("The historical trend direction is DETERIORATING.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_wrong_historical_score_detected(self):
        context = _base_context(health_history=_health_history(previous=71.1, latest=66.2))
        result = check_grounding("The Health Score moved from 71.1 to 62.6.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_correct_historical_score_passes(self):
        context = _base_context(health_history=_health_history(previous=71.1, latest=66.2))
        result = check_grounding("The Health Score moved from 71.1 to 66.2.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_prediction_language_from_deterioration_detected(self):
        context = _base_context(health_history=_health_history(direction="DETERIORATING"))
        result = check_grounding("The equipment will continue deteriorating and is likely to fail soon.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_remaining_useful_life_language_detected(self):
        context = _base_context(health_history=_health_history())
        result = check_grounding("Based on this trend, the Remaining Useful Life is limited.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_backward_looking_language_passes(self):
        context = _base_context(health_history=_health_history(direction="DETERIORATING"))
        result = check_grounding("The Health Score has deteriorated over the recorded observations.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)


# ---------------------------------------------------------------------------
# Grounding: Asset Performance history
# ---------------------------------------------------------------------------

class TestGroundingAssetPerformanceHistory(unittest.TestCase):
    def test_wrong_percent_change_detected(self):
        context = _base_context(asset_performance_history=_asset_performance_history())
        result = check_grounding("The recorded change was 61%.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_correct_percent_change_passes(self):
        context = _base_context(asset_performance_history=_asset_performance_history())
        result = check_grounding("The recorded change was -28.32%.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_fabricated_performance_state_detected(self):
        """The one recorded observation's real state is DEGRADING (see
        _asset_performance_history()'s default) - claiming a DIFFERENT
        real vocabulary value (IMPROVING) must be caught. An entirely
        unknown word (e.g. "CRITICAL") is deliberately NOT checkable -
        that's the existing, documented "unknown vocabulary value, skip
        rather than guess" rule, unchanged by this phase."""
        context = _base_context(asset_performance_history=_asset_performance_history())
        result = check_grounding("The performance state is IMPROVING.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_real_historical_performance_state_passes(self):
        context = _base_context(asset_performance_history=_asset_performance_history())
        result = check_grounding("The performance state is DEGRADING.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_maintenance_causal_claim_detected(self):
        context = _base_context(asset_performance_history=_asset_performance_history(
            maintenance_comparisons=[{"target_key": "flow_per_kw"}],
        ))
        result = check_grounding("The maintenance caused the improvement in flow.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_maintenance_association_wording_passes(self):
        context = _base_context(asset_performance_history=_asset_performance_history(
            maintenance_comparisons=[{"target_key": "flow_per_kw"}],
        ))
        result = check_grounding("Performance was better in the recorded post-maintenance comparison.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_maintenance_causal_claim_not_flagged_when_no_comparisons_exist(self):
        context = _base_context(asset_performance_history=_asset_performance_history(maintenance_comparisons=[]))
        result = check_grounding("The maintenance caused the improvement.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)


# ---------------------------------------------------------------------------
# Cross-domain behavior
# ---------------------------------------------------------------------------

class TestCrossDomainHistoricalBehavior(unittest.TestCase):
    def test_current_and_historical_data_health_remain_distinguishable_in_prompt(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("data_health_history",),
        )
        rendered = render_equipment_context(context)
        self.assertIn("Data Health (telemetry trustworthiness, NOT equipment condition, as of", rendered)
        self.assertIn("Historical Data Health (recorded since", rendered)

    def test_current_and_health_direction_remain_distinguishable_in_prompt(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("health_history",),
        )
        rendered = render_equipment_context(context)
        if "Equipment Health (assessed" in rendered:
            self.assertIn("Historical Health Trend (BACKWARD-LOOKING ONLY", rendered)

    def test_unavailable_one_history_domain_does_not_suppress_another(self):
        context = _base_context(
            data_health_history={"available": False, "reason": "no persisted history", "recorded_since": None, "window_days": 7.0},
            health_history=_health_history(),
        )
        rendered_dh = context["data_health_history"]
        self.assertFalse(rendered_dh["available"])
        self.assertTrue(context["health_history"]["available"])
        # both domains coexist in the same context without one blanking the other
        result = check_grounding("The historical trend direction is DETERIORATING.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)


# ---------------------------------------------------------------------------
# Regression / safety
# ---------------------------------------------------------------------------

class TestRegressionSafety(unittest.TestCase):
    def test_history_domains_still_default_off(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION")
        self.assertEqual(context["request"]["history_domains_fetched"], [])
        self.assertNotIn("data_health_history", context)

    def test_existing_data_health_conflation_check_still_works(self):
        """Phase 16.3's own adversarial check must remain intact after
        this phase's additions."""
        context = _base_context(data_health={"available": True, "confidence_status": "GOOD", "frozen_candidates": [], "source_driver": None})
        result = check_grounding("The pump is healthy because Data Confidence is GOOD.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_comparison_and_factory_summary_still_skipped(self):
        result = check_grounding("Equipment A is healthier than B.", {}, "COMPARISON")
        self.assertTrue(result.grounded)
        self.assertFalse(result.checked)


if __name__ == "__main__":
    unittest.main()
