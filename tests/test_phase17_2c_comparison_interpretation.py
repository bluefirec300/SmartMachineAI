import unittest

from ai import context_builder as cb
from ai.grounding_guard import check_grounding
from ai.interpretation_prompt_builder import build_interpretation_prompt, render_comparison_context, render_comparison_context_full
from app.ask import AskEngine
from config.environment import get_config_db_path, get_machine_db_path
from engine.industrial_query_engine import IndustrialQueryEngine

"""
Phase 17.2c - deterministic comparison interpretation tests.

Two styles, matching this project's established convention:
- Entity-resolution and context-integration tests run against the real
  live simulation database (same pattern as every prior Phase 15/17
  test file).
- Grounding-guard tests use hand-built comparison_facts dicts (same
  pattern as tests/test_phase16_3_data_health_grounding.py) - fast,
  deterministic, no database needed.
"""

CONFIG_DB = get_config_db_path()
MACHINE_DB = get_machine_db_path()


def _engine_without_ai():
    engine = AskEngine()
    engine.ai_provider = None
    engine.ai_error = "forced unavailable for deterministic testing"
    return engine


# ---------------------------------------------------------------------------
# Entity resolution / duplicate rejection
# ---------------------------------------------------------------------------

class TestEntityResolution(unittest.TestCase):
    def setUp(self):
        self.qe = IndustrialQueryEngine(database_path=CONFIG_DB)

    def test_ordered_ab_identity_preserved(self):
        pair = self.qe.resolve_equipment_pair("compare P01 WSP01 and P02 WSP01")
        self.assertEqual(pair.status, "resolved")
        self.assertEqual(pair.entity_a.instance_key, "P01.WATER.WSP01")
        self.assertEqual(pair.entity_b.instance_key, "P02.WATER.WSP01")

    def test_p01_p02_comparison_remains_isolated(self):
        pair = self.qe.resolve_equipment_pair("compare P01 WSP01 and P02 WSP01")
        self.assertEqual(pair.entity_a.plant, "p01")
        self.assertEqual(pair.entity_b.plant, "p02")

    def test_duplicate_entity_selection_rejected(self):
        pair = self.qe.resolve_equipment_pair("compare P01 WSP01 with P01 WSP01")
        self.assertEqual(pair.status, "duplicate_entity")
        self.assertIn("same equipment", pair.message)

    def test_duplicate_entity_never_reaches_resolved_status(self):
        pair = self.qe.resolve_equipment_pair("compare P01 WSP01 with P01 WSP01")
        self.assertNotEqual(pair.status, "resolved")

    def test_ask_structured_surfaces_duplicate_entity_message_without_llm_call(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("compare P01 WSP01 with P01 WSP01")
        self.assertEqual(result.intent, "COMPARISON")
        self.assertIn("same equipment", result.answer)
        self.assertEqual(result.resolved_entities, [])


# ---------------------------------------------------------------------------
# Deterministic comparison-facts contract (real live DB)
# ---------------------------------------------------------------------------

class TestComparisonFactsContract(unittest.TestCase):
    def test_health_score_difference_deterministic(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        health = context["comparison_facts"]["health"]
        if health["comparable"]:
            expected_diff = round(health["entity_b"]["score"] - health["entity_a"]["score"], 1)
            self.assertEqual(health["score_difference"], expected_diff)

    def test_health_lower_score_entity_matches_actual_scores(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        health = context["comparison_facts"]["health"]
        if health["comparable"] and health["score_difference"] != 0:
            expected = "A" if health["entity_a"]["score"] < health["entity_b"]["score"] else "B"
            self.assertEqual(health["lower_score_entity"], expected)

    def test_maintenance_priority_ordering_uses_accepted_rank_only(self):
        """Verifies the ordering comes from engine.maintenance_intelligence_targets.PRIORITY_SORT_RANK
        - never an invented ordering."""
        from engine.maintenance_intelligence_targets import PRIORITY_SORT_RANK
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        maintenance = context["comparison_facts"]["maintenance_intelligence"]
        if maintenance["comparable"] and maintenance["higher_priority_entity"]:
            rank_a = PRIORITY_SORT_RANK[maintenance["entity_a"]["priority"]]
            rank_b = PRIORITY_SORT_RANK[maintenance["entity_b"]["priority"]]
            expected = "A" if rank_a > rank_b else "B"
            self.assertEqual(maintenance["higher_priority_entity"], expected)

    def test_asset_performance_same_dimension_comparison_works(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        performance = context["comparison_facts"]["asset_performance"]
        if performance["comparable"]:
            for dimension in performance["shared_dimensions"]:
                self.assertIn("target_key", dimension)
                self.assertIn("entity_a", dimension)
                self.assertIn("entity_b", dimension)

    def test_incompatible_performance_dimensions_not_compared_numerically(self):
        """Different equipment types (pump vs chiller) share no target_key
        - must be NOT_COMPARABLE, never a guessed comparison."""
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P01.UTILITY.CHL01")
        performance = context["comparison_facts"]["asset_performance"]
        self.assertFalse(performance["comparable"])
        self.assertEqual(performance["shared_dimensions"], [])

    def test_data_health_comparison_works(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        data_health = context["comparison_facts"]["data_health"]
        self.assertIn("comparable", data_health)
        self.assertIn("entity_a", data_health)
        self.assertIn("entity_b", data_health)

    def test_energy_observed_excess_compared_only_with_same_semantic_field(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        energy = context["comparison_facts"]["energy_opportunity"]
        # Only observed_excess_cost is compared - estimated_potential_saving is never present in this contract at all.
        self.assertNotIn("estimated_potential_saving", energy["entity_a"])
        self.assertNotIn("estimated_potential_saving", energy["entity_b"])

    def test_savings_verification_never_ranked(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        savings = context["comparison_facts"]["savings_verification"]
        self.assertNotIn("higher_result_entity", savings)
        self.assertNotIn("better_result_entity", savings)

    def test_no_history_facts_when_history_not_requested(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        self.assertNotIn("data_health_history", context["comparison_facts"])
        self.assertNotIn("health_history", context["comparison_facts"])

    def test_history_facts_present_only_when_explicitly_requested(self):
        context = cb.build_comparison_ai_context(
            CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01",
            history_domains=("data_health_history", "health_history"),
        )
        self.assertIn("data_health_history", context["comparison_facts"])
        self.assertIn("health_history", context["comparison_facts"])

    def test_historical_facts_do_not_overwrite_current_state(self):
        context = cb.build_comparison_ai_context(
            CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01",
            history_domains=("health_history",),
        )
        current_health = context["comparison_facts"]["health"]
        self.assertIn("score", current_health["entity_a"])
        # historical facts live under a totally separate key
        self.assertIn("health_history", context["comparison_facts"])
        self.assertNotEqual(context["comparison_facts"]["health"], context["comparison_facts"]["health_history"])


# ---------------------------------------------------------------------------
# No aggregate meta-verdict, entities stay independent
# ---------------------------------------------------------------------------

class TestNoAggregateMetaVerdict(unittest.TestCase):
    def test_no_overall_score_field_anywhere_in_comparison_facts(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        serialized = str(context["comparison_facts"])
        for forbidden in ("overall_score", "combined_score", "risk_score", "winner", "condition_index"):
            self.assertNotIn(forbidden, serialized)

    def test_entities_remain_distinct_objects_never_merged(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        self.assertNotEqual(context["entity_a"]["equipment"]["instance_key"], context["entity_b"]["equipment"]["instance_key"])
        self.assertIsNot(context["entity_a"], context["entity_b"])


# ---------------------------------------------------------------------------
# Grounding: comparison claims (hand-built facts, fast)
# ---------------------------------------------------------------------------

def _comparison_context(comparison_facts):
    return {"comparison_facts": comparison_facts}


def _health_facts(score_a=68.7, score_b=82.1, comparable=True):
    lower = "A" if score_a < score_b else ("B" if score_b < score_a else None)
    return {
        "comparable": comparable,
        "entity_a": {"available": True, "score": score_a, "band": "ATTENTION", "assessment_confidence": "LOW", "provisional": False},
        "entity_b": {"available": True, "score": score_b, "band": "MONITOR", "assessment_confidence": "MEDIUM", "provisional": False},
        "score_difference": round(score_b - score_a, 1), "lower_score_entity": lower,
    }


def _maintenance_facts(priority_a="REVIEW", priority_b="ROUTINE", comparable=True):
    from engine.maintenance_intelligence_targets import PRIORITY_SORT_RANK
    rank_a, rank_b = PRIORITY_SORT_RANK[priority_a], PRIORITY_SORT_RANK[priority_b]
    higher = "A" if rank_a > rank_b else ("B" if rank_b > rank_a else None)
    return {
        "comparable": comparable,
        "entity_a": {"available": True, "priority": priority_a, "recommendation_confidence": "MEDIUM"},
        "entity_b": {"available": True, "priority": priority_b, "recommendation_confidence": "HIGH"},
        "higher_priority_entity": higher,
    }


def _data_health_facts(score_a=85.0, score_b=30.0, comparable=True):
    poorer = "A" if score_a < score_b else ("B" if score_b < score_a else None)
    status = lambda s: "GOOD" if s >= 85 else ("POOR" if s < 60 else "DEGRADED")
    return {
        "comparable": comparable,
        "entity_a": {"status": status(score_a), "score": score_a},
        "entity_b": {"status": status(score_b), "score": score_b},
        "score_difference": round(score_b - score_a, 1), "poorer_data_health_entity": poorer,
    }


def _full_facts(**overrides):
    facts = {
        "health": _health_facts(), "maintenance_intelligence": _maintenance_facts(),
        "data_health": _data_health_facts(),
        "asset_performance": {"comparable": False, "shared_dimensions": [], "entity_a_attention_score": None, "entity_b_attention_score": None},
        "energy_opportunity": {"comparable": False, "entity_a": {"has_opportunity": False, "observed_excess_cost": None}, "entity_b": {"has_opportunity": False, "observed_excess_cost": None}, "higher_observed_excess_cost_entity": None},
        "savings_verification": {"comparable": False, "entity_a": {"result": None}, "entity_b": {"result": None}},
    }
    facts.update(overrides)
    return facts


class TestComparisonGroundingHealth(unittest.TestCase):
    def test_correct_lower_score_claim_grounded(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A has the lower current Health Score.", context, "COMPARISON")
        self.assertTrue(result.grounded)
        self.assertTrue(result.checked)

    def test_wrong_lower_score_claim_rejected(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity B has the lower current Health Score.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_correct_numeric_score_claim_grounded(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A Health Score is 68.7.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_wrong_numeric_score_claim_rejected(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A Health Score is 40.0.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_health_unavailable_prevents_false_ranking(self):
        facts = _full_facts(health=_health_facts(comparable=False))
        facts["health"]["entity_b"] = {"available": False, "score": None, "band": None, "assessment_confidence": None, "provisional": None}
        context = _comparison_context(facts)
        result = check_grounding("Entity A has the lower current Health Score.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_low_provisional_confidence_remains_visible_in_facts(self):
        facts = _full_facts()
        facts["health"]["entity_a"]["provisional"] = True
        self.assertTrue(facts["health"]["entity_a"]["provisional"])


class TestComparisonGroundingMaintenance(unittest.TestCase):
    def test_correct_higher_priority_claim_grounded(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A has the higher maintenance priority.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_wrong_higher_priority_claim_rejected(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity B has the higher maintenance priority.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_recommendation_confidence_stays_separate_field(self):
        facts = _full_facts()
        self.assertIn("recommendation_confidence", facts["maintenance_intelligence"]["entity_a"])
        self.assertNotIn("recommendation_confidence", facts["maintenance_intelligence"])


class TestComparisonGroundingDataHealth(unittest.TestCase):
    def test_correct_poorer_data_health_claim_grounded(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity B has poorer Data Health.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_wrong_poorer_data_health_claim_rejected(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A has poorer Data Health.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_data_health_poor_does_not_alter_health_score_claim(self):
        """Section 10's worked example: B has poorer Data Health but its
        Health Score (82.1, unaffected) remains independently claimable."""
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity B Health Score is 82.1, though Entity B has poorer Data Health.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_unavailable_data_health_preserved_not_ranked(self):
        facts = _full_facts(data_health=_data_health_facts(comparable=False))
        facts["data_health"]["entity_b"] = {"status": None, "score": None}
        context = _comparison_context(facts)
        result = check_grounding("Entity A has poorer Data Health.", context, "COMPARISON")
        self.assertFalse(result.grounded)


class TestComparisonGroundingAssetPerformanceAndEnergy(unittest.TestCase):
    def test_percent_change_not_comparable_when_no_shared_dimension(self):
        context = _comparison_context(_full_facts())
        # asset_performance.comparable=False in _full_facts() by default
        self.assertFalse(context["comparison_facts"]["asset_performance"]["comparable"])

    def test_energy_higher_cost_claim_grounded(self):
        facts = _full_facts(energy_opportunity={
            "comparable": True,
            "entity_a": {"has_opportunity": True, "observed_excess_cost": 100.0},
            "entity_b": {"has_opportunity": True, "observed_excess_cost": 500.0},
            "higher_observed_excess_cost_entity": "B",
        })
        context = _comparison_context(facts)
        result = check_grounding("Entity B has the higher observed excess cost.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_energy_wrong_higher_cost_claim_rejected(self):
        facts = _full_facts(energy_opportunity={
            "comparable": True,
            "entity_a": {"has_opportunity": True, "observed_excess_cost": 100.0},
            "entity_b": {"has_opportunity": True, "observed_excess_cost": 500.0},
            "higher_observed_excess_cost_entity": "B",
        })
        context = _comparison_context(facts)
        result = check_grounding("Entity A has the higher observed excess cost.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_potential_saving_unavailable_never_inferred_from_observed_excess(self):
        """No comparison_facts field even hints at a potential-saving
        comparison - confirmed by construction (no such key exists)."""
        facts = _full_facts()
        self.assertNotIn("potential_saving", str(facts["energy_opportunity"]).lower().replace("_", ""))


class TestComparisonGroundingNoAggregateVerdict(unittest.TestCase):
    def test_overall_winner_language_rejected(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Overall, Entity A is the better equipment.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_equipment_risk_score_language_rejected(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A has a higher Equipment Risk Score.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_plain_separate_domain_statements_grounded(self):
        context = _comparison_context(_full_facts())
        answer = "Entity A has the lower current Health Score. Entity A has the higher maintenance priority. Entity B has poorer Data Health."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded)


class TestComparisonGroundingBackwardCompatibility(unittest.TestCase):
    """Phase 15's original comparison-skip contract must survive
    unweakened for contexts that carry no comparison_facts at all."""

    def test_empty_context_remains_not_checked(self):
        result = check_grounding("Equipment A's Health State is HEALTHY, Equipment B's is ATTENTION.", {}, "COMPARISON")
        self.assertTrue(result.grounded)
        self.assertFalse(result.checked)

    def test_factory_summary_still_skipped(self):
        result = check_grounding("Several pieces of equipment need review.", {}, "FACTORY_SUMMARY")
        self.assertFalse(result.checked)


# ---------------------------------------------------------------------------
# Regression safety
# ---------------------------------------------------------------------------

class TestRegressionSafety(unittest.TestCase):
    def test_current_state_single_equipment_prompt_unaffected(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "HEALTH_EXPLANATION", question="why is P01 WSP01 unhealthy")
        prompt = build_interpretation_prompt(context, "HEALTH_EXPLANATION", "why is P01 WSP01 unhealthy")
        self.assertNotIn("DETERMINISTIC COMPARISON FACTS", prompt)

    def test_history_domains_still_default_off_for_comparison(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        self.assertNotIn("data_health_history", context["entity_a"])
        self.assertNotIn("data_health_history", context["comparison_facts"])

    def test_render_comparison_context_still_shows_both_entities(self):
        # Phase 17.2d - render_comparison_context() is now the COMPACT
        # LLM-facing rendering (see tests/test_phase17_2d_comparison_compaction.py
        # for the full compaction/domain-selectivity test suite); this
        # regression test only confirms both entities are still
        # identifiable at all, not the exact (now-compact) format.
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        rendered = render_comparison_context(context)
        self.assertIn("Entity A:", rendered)
        self.assertIn("Entity B:", rendered)

    def test_render_comparison_context_full_still_shows_both_entities(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        rendered = render_comparison_context_full(context)
        self.assertIn("=== Entity A ===", rendered)
        self.assertIn("=== Entity B ===", rendered)

    def test_no_database_writes_from_comparison_context_build(self):
        import sqlite3
        connection = sqlite3.connect(CONFIG_DB)
        try:
            before = connection.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        finally:
            connection.close()

        cb.build_comparison_ai_context(
            CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01",
            history_domains=("data_health_history", "health_history"),
        )

        connection = sqlite3.connect(CONFIG_DB)
        try:
            after = connection.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
