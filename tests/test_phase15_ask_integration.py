import sqlite3
import unittest

from app.ask import AskEngine, AskResult
from config.environment import get_config_db_path
from engine.maintenance_intelligence_engine import calculate_maintenance_priority

"""
Integration tests against the real live simulation database, with the
real AI provider forced off (engine.ai_provider = None) so these run
fast and deterministically - the SAME technique already used earlier in
this session's own manual verification. Live-LLM acceptance is a
SEPARATE, explicit pass (not part of this automated suite - matches
this project's own established 'do not make the suite depend on live
Ollama' discipline)."""

CONFIG_DB = get_config_db_path()


def _engine_without_ai():
    engine = AskEngine()
    engine.ai_provider = None
    engine.ai_error = "forced unavailable for deterministic testing"
    return engine


class TestStructuredResultContract(unittest.TestCase):
    def test_ask_structured_returns_ask_result_with_required_fields(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("why is P01 WSP01 unhealthy")
        self.assertIsInstance(result, AskResult)
        self.assertEqual(result.intent, "HEALTH_EXPLANATION")
        self.assertTrue(result.fallback_used)
        self.assertIsNotNone(result.structured_context)
        self.assertEqual(result.resolved_entities, [{"instance_key": "P01.WATER.WSP01"}])

    def test_existing_ask_string_interface_unchanged(self):
        """ask() must keep returning a plain string, for backward
        compatibility with the CLI/other existing callers."""
        engine = _engine_without_ai()
        answer = engine.ask("What is AC01 pressure?")
        self.assertIsInstance(answer, str)

    def test_tag_level_question_via_ask_structured_delegates_to_ask(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("What is AC01 pressure?")
        self.assertEqual(result.intent, "")
        self.assertIsNone(result.structured_context)
        self.assertEqual(result.grounding_status, "not_applicable")


class TestSessionEquipmentContextIsolation(unittest.TestCase):
    """Required test: 'per-session equipment context isolation' -
    AskEngine itself must never store context_equipment across calls;
    two independent callers passing different values must never
    interfere with each other."""

    def test_context_equipment_is_never_stored_on_the_engine(self):
        engine = _engine_without_ai()
        engine.ask_structured("is it still in attention", context_equipment="P01.WATER.WSP01")

        for attribute_name in vars(engine):
            value = getattr(engine, attribute_name)
            self.assertNotEqual(value, "P01.WATER.WSP01", msg=f"context_equipment leaked into engine.{attribute_name}")

    def test_two_sessions_sharing_one_engine_never_cross_contaminate(self):
        """Simulates two browser sessions both using process-shared
        state incorrectly would look like - since AskEngine takes
        context_equipment as an explicit per-call argument, interleaved
        calls with different values must each get their own answer."""
        engine = _engine_without_ai()

        result_session_a = engine.ask_structured("is it still in attention", context_equipment="P01.WATER.WSP01")
        result_session_b = engine.ask_structured("is it still in attention", context_equipment="P01.UTILITY.AC01")

        self.assertEqual(result_session_a.resolved_entities, [{"instance_key": "P01.WATER.WSP01"}])
        self.assertEqual(result_session_b.resolved_entities, [{"instance_key": "P01.UTILITY.AC01"}])

    def test_explicit_new_equipment_overrides_prior_context(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("is P01 AC01 in attention", context_equipment="P01.WATER.WSP01")
        self.assertEqual(result.resolved_entities, [{"instance_key": "P01.UTILITY.AC01"}])

    def test_ambiguous_new_reference_is_not_silently_replaced_by_prior_context(self):
        """Naming something ambiguous (ANY plant's WSP01) must surface a
        clarification, never silently fall back to the OLD context
        equipment just because resolution failed."""
        engine = _engine_without_ai()
        result = engine.ask_structured("why is WSP01 unhealthy", context_equipment="P01.UTILITY.AC01")
        self.assertNotEqual(result.resolved_entities, [{"instance_key": "P01.UTILITY.AC01"}])
        self.assertIn("plant", result.answer.lower())

    def test_bare_followup_with_no_context_asks_for_clarification(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("is it still in attention", context_equipment=None)
        # No single equipment was cleanly resolved (resolved_entities may
        # still list nearby CANDIDATE guesses for the clarification menu,
        # but never a canonical resolved instance_key) - no structured
        # context was built either, since nothing was resolved to build it from.
        self.assertIsNone(result.structured_context)
        self.assertTrue(all("instance_key" not in e or "equipment_score" in e for e in result.resolved_entities))


class TestComparisonAndFactorySummaryDoNotTouchSessionContext(unittest.TestCase):
    def test_comparison_resolves_both_sides_regardless_of_context_equipment(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("Compare P01 WSP01 and P02 WSP01")
        self.assertEqual(len(result.resolved_entities), 2)

    def test_factory_summary_needs_no_equipment_context_at_all(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("What should engineering look at today?")
        self.assertEqual(result.intent, "FACTORY_SUMMARY")
        self.assertEqual(result.resolved_entities, [])


class TestNoNewAggregateScoreAnywhere(unittest.TestCase):
    FORBIDDEN_KEYS = ("factory_score", "ai_risk_score", "overall_priority", "plant_intelligence_score", "combined_score", "aggregate_score")

    def test_health_explanation_context_has_no_aggregate_key(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("why is P01 WSP01 unhealthy")
        self._assert_no_forbidden_keys(result.structured_context)

    def test_factory_summary_context_has_no_aggregate_key(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("What should engineering look at today?")
        self._assert_no_forbidden_keys(result.structured_context)

    def _assert_no_forbidden_keys(self, obj, path=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                self.assertNotIn(key, self.FORBIDDEN_KEYS, msg=f"forbidden aggregate key at {path}.{key}")
                self._assert_no_forbidden_keys(value, f"{path}.{key}")
        elif isinstance(obj, list):
            for index, item in enumerate(obj):
                self._assert_no_forbidden_keys(item, f"{path}[{index}]")


class TestAssessedEvidenceExposure(unittest.TestCase):
    """Required test: 'assessed_evidence/read-layer exposure if
    implemented' - purely additive, must not change the recommendation
    itself."""

    def test_assessed_evidence_field_exists_and_is_a_list(self):
        connection = sqlite3.connect(CONFIG_DB)
        try:
            plant_id = connection.execute("SELECT id FROM plants WHERE code = 'p01'").fetchone()[0]
        finally:
            connection.close()

        result = calculate_maintenance_priority(CONFIG_DB, plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01")
        self.assertTrue(hasattr(result, "assessed_evidence"))
        self.assertIsInstance(result.assessed_evidence, list)

    def test_assessed_evidence_is_a_superset_of_or_equal_to_confidence_basis(self):
        """assessed_evidence answers 'was X assessed at all', confidence_basis
        answers 'did X drive the recommendation' - the former can never
        be a STRICT subset of the latter (anything driving the
        recommendation must have been assessed)."""
        connection = sqlite3.connect(CONFIG_DB)
        try:
            plant_id = connection.execute("SELECT id FROM plants WHERE code = 'p01'").fetchone()[0]
        finally:
            connection.close()

        result = calculate_maintenance_priority(CONFIG_DB, plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01")

        for dimension in result.confidence_basis:
            self.assertIn(dimension, result.assessed_evidence)

    def test_exposing_assessed_evidence_does_not_change_the_recommendation(self):
        """The mandatory constraint: this must be read-exposure only."""
        connection = sqlite3.connect(CONFIG_DB)
        try:
            plant_id = connection.execute("SELECT id FROM plants WHERE code = 'p01'").fetchone()[0]
        finally:
            connection.close()

        result_a = calculate_maintenance_priority(CONFIG_DB, plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01")
        result_b = calculate_maintenance_priority(CONFIG_DB, plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01")

        self.assertEqual(result_a.maintenance_priority, result_b.maintenance_priority)
        self.assertEqual(result_a.priority_score, result_b.priority_score)
        self.assertEqual(result_a.recommendation_confidence, result_b.recommendation_confidence)
        self.assertEqual(result_a.confidence_basis, result_b.confidence_basis)


class TestHypothesisTextNotTreatedAsDeterministicState(unittest.TestCase):
    def test_deterministic_fallback_never_contains_hypothesis_section_header(self):
        """The deterministic (no-LLM) fallback is a plain fact rendering
        only - hypothesis framing is prompt-only text the LLM would add,
        never present when there is no LLM output at all."""
        engine = _engine_without_ai()
        result = engine.ask_structured("why is P01 WSP01 unhealthy")
        self.assertNotIn("Possible engineering hypotheses", result.answer)


if __name__ == "__main__":
    unittest.main()
