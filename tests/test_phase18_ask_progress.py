import unittest
from unittest.mock import MagicMock

from app.ask import AskEngine
from config.environment import get_config_db_path
from ui.ask_ai_ux import SUGGESTED_QUESTIONS, fallback_reason_prefix

"""
Phase 18 - AI User Experience. Integration tests for AskEngine's
optional on_progress callback and its interaction with the existing
fallback/grounding contract, against the real live simulation
database with the real AI provider forced off/mocked (the same
technique established in tests/test_phase15_ask_integration.py) - fast
and deterministic, no live Ollama call.
"""

CONFIG_DB = get_config_db_path()


def _engine_without_ai():
    engine = AskEngine()
    engine.ai_provider = None
    engine.ai_error = "forced unavailable for deterministic testing"
    return engine


def _engine_with_mock_provider(generate_return="Entity A Health Score is fine.", generate_side_effect=None):
    engine = AskEngine()
    mock_provider = MagicMock()
    mock_provider.provider = "ollama"
    mock_provider.model = "qwen2.5:7b"
    if generate_side_effect is not None:
        mock_provider.generate.side_effect = generate_side_effect
    else:
        mock_provider.generate.return_value = generate_return
    engine.ai_provider = mock_provider
    return engine


class TestProgressCallbackIsOptional(unittest.TestCase):
    def test_existing_callers_without_on_progress_still_work(self):
        engine = _engine_without_ai()
        # No on_progress argument at all - must behave exactly as before.
        result = engine.ask_structured("What is AC01 pressure?")
        self.assertEqual(result.intent, "")

    def test_ask_string_interface_has_no_progress_parameter_requirement(self):
        engine = _engine_without_ai()
        answer = engine.ask("What is AC01 pressure?")
        self.assertIsInstance(answer, str)


class TestGenuineStagesOnly(unittest.TestCase):
    def test_old_pipeline_path_never_emits_validating(self):
        # The old ask()/IndustrialQueryEngine pipeline never calls
        # check_grounding() - "validating" would be dishonest here.
        engine = _engine_with_mock_provider()
        stages = []
        engine.ask_structured("What is AC01 pressure?", on_progress=stages.append)
        self.assertIn("generating_ai", stages)
        self.assertNotIn("validating", stages)

    def test_provider_none_short_circuit_never_emits_generating_ai(self):
        # No provider configured at all - no generation is ever
        # attempted for the Phase 15 domain path, so "generating_ai"
        # would be a lie.
        engine = _engine_without_ai()
        stages = []
        engine.ask_structured("Compare P01 CHL01 and P02 CHL01", on_progress=stages.append)
        self.assertNotIn("generating_ai", stages)
        self.assertNotIn("validating", stages)

    def test_domain_path_with_working_provider_emits_generating_then_validating(self):
        engine = _engine_with_mock_provider()
        stages = []
        engine.ask_structured("Compare P01 CHL01 and P02 CHL01", on_progress=stages.append)
        self.assertIn("generating_ai", stages)
        self.assertIn("validating", stages)
        self.assertLess(stages.index("generating_ai"), stages.index("validating"))

    def test_provider_error_emits_generating_but_never_validating(self):
        # The provider raised - there is no answer to validate.
        engine = _engine_with_mock_provider(generate_side_effect=RuntimeError("connection refused"))
        stages = []
        result = engine.ask_structured("Compare P01 CHL01 and P02 CHL01", on_progress=stages.append)
        self.assertIn("generating_ai", stages)
        self.assertNotIn("validating", stages)
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.grounding_status, "not_applicable")

    def test_preparing_context_always_emitted_first(self):
        engine = _engine_without_ai()
        stages = []
        engine.ask_structured("How is AC01 doing?", on_progress=stages.append)
        self.assertEqual(stages[0], "preparing_context")


class TestFallbackWordingDistinguishable(unittest.TestCase):
    def test_provider_failure_and_grounding_rejection_produce_different_prefixes(self):
        provider_failure_engine = _engine_with_mock_provider(generate_side_effect=RuntimeError("timeout"))
        failure_result = provider_failure_engine.ask_structured("Compare P01 CHL01 and P02 CHL01")

        rejection_engine = _engine_with_mock_provider(generate_return="Overall, Entity A is the better equipment.")
        rejection_result = rejection_engine.ask_structured("Compare P01 CHL01 and P02 CHL01")

        failure_prefix = fallback_reason_prefix(failure_result.fallback_used, failure_result.grounding_status)
        rejection_prefix = fallback_reason_prefix(rejection_result.fallback_used, rejection_result.grounding_status)

        self.assertIsNotNone(failure_prefix)
        self.assertIsNotNone(rejection_prefix)
        self.assertNotEqual(failure_prefix, rejection_prefix)

    def test_rejected_ai_wording_is_never_present_in_the_final_answer(self):
        # Known-caught pattern per ai/grounding_guard.py's own test suite
        # (TestFalseAcceptanceProtectionUnweakened) - an aggregate
        # "overall winner" verdict the guard never allows through.
        unsafe_claim = "Overall, Entity A is the better equipment."
        engine = _engine_with_mock_provider(generate_return=unsafe_claim)
        result = engine.ask_structured("Compare P01 CHL01 and P02 CHL01")
        self.assertEqual(result.grounding_status, "violation_detected")
        self.assertNotIn(unsafe_claim, result.answer)


class TestSuggestedQuestionsRouteThroughNormalPipeline(unittest.TestCase):
    def test_every_suggested_question_resolves_without_exception(self):
        for question in SUGGESTED_QUESTIONS:
            engine = _engine_without_ai()
            result = engine.ask_structured(question)
            self.assertIsNotNone(result.answer)

    def test_suggested_questions_do_not_fall_into_a_clarification_menu(self):
        # Each one was hand-verified to resolve cleanly (see
        # ui.ask_ai_ux.SUGGESTED_QUESTIONS' docstring) - confirm that
        # remains true as a regression guard, not just "returns
        # something."
        for question in SUGGESTED_QUESTIONS:
            engine = _engine_without_ai()
            result = engine.ask_structured(question)
            self.assertNotIn("Which one did you mean", result.answer)
            self.assertNotIn("couldn't confidently match", result.answer)


class TestSessionContextAndAmbiguityUnaffected(unittest.TestCase):
    def test_context_equipment_still_never_stored_on_the_engine(self):
        engine = _engine_without_ai()
        engine.ask_structured("is it still in attention", context_equipment="P01.WATER.WSP01")
        self.assertFalse(hasattr(engine, "context_equipment"))

    def test_explicit_new_equipment_overrides_session_context(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("How is AC01 doing?", context_equipment="P01.WATER.WSP01")
        # AC01 was explicitly named - it must win over the supplied
        # session context, not WSP01.
        self.assertNotIn("WSP01", str(result.resolved_entities))

    def test_ambiguous_equipment_still_returns_a_clarification_style_answer(self):
        engine = _engine_without_ai()
        result = engine.ask_structured("Compare CHL01 and CHL02")
        self.assertIn("ambiguous", result.answer.lower())


if __name__ == "__main__":
    unittest.main()
