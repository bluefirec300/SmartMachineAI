import time
import unittest
from unittest import mock

from ai.ai_provider import AIProvider
from ai.providers.provider_factory import ProviderFactory
from app.ask import AskEngine, AskResult
from config.config_manager import ConfigManager

"""
Phase V2.4 - Ask AI Fast/Thorough mode. Tests the model-routing plumbing
(ProviderFactory -> AIProvider -> AskEngine) added for the mode toggle,
and confirms the two hard invariants the phase explicitly requires:
instant-answer questions never touch either model, and switching modes
never weakens grounding/fallback or touches conversation/pending state.

Constructing OllamaProvider/AIProvider/AskEngine never makes a network
call by itself (confirmed by reading ai/providers/ollama_provider.py -
its __init__ only sets attributes) - so these tests exercise the real
routing chain, not mocks, without depending on live Ollama. Where an
actual .generate() call would be required (a real LLM-requiring
question), engine.ai_provider is forced to None first, exactly the
established "do not make the suite depend on live Ollama" convention
already used in tests/test_phase15_ask_integration.py.
"""

FAST_MODEL = "qwen2.5:3b"
THOROUGH_MODEL = "qwen2.5:7b"


class ProviderFactoryModelOverrideTests(unittest.TestCase):
    def test_model_override_wins_for_ollama(self):
        provider = ProviderFactory.create(provider_name="ollama", model_override=FAST_MODEL)
        self.assertEqual(provider.model, FAST_MODEL)

    def test_no_override_falls_back_to_existing_precedence(self):
        # Matches config/settings.ini's configured ollama_model (qwen2.5:7b)
        # unless OLLAMA_MODEL is set in this environment - proves routing
        # is completely unchanged when model_override is None (the
        # default for every pre-existing caller).
        config = ConfigManager()
        provider = ProviderFactory.create(provider_name="ollama", model_override=None)
        expected = config.ollama_model or "qwen2.5:7b"
        self.assertEqual(provider.model, expected)

    def test_model_override_is_ignored_for_openai(self):
        # Fast/Thorough names Ollama-specific models - applying one to
        # OpenAI would be meaningless, so it must have zero effect there.
        # Only OPENAI_API_KEY is patched (via environ, not a blanket
        # os.getenv patch) so OpenAIProvider's own unrelated "no key
        # configured" guard doesn't short-circuit this test before the
        # model-resolution logic under test even runs - OPENAI_MODEL
        # resolution itself is left genuinely exercised.
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "fake-key-for-routing-test-only"}):
            provider = ProviderFactory.create(provider_name="openai", model_override=FAST_MODEL)
        self.assertNotEqual(provider.model, FAST_MODEL)

    def test_empty_string_override_does_not_win(self):
        provider = ProviderFactory.create(provider_name="ollama", model_override="")
        self.assertNotEqual(provider.model, "")


class AIProviderModelOverrideTests(unittest.TestCase):
    def test_model_override_reaches_the_underlying_client(self):
        provider = AIProvider(provider_name="ollama", model_override=THOROUGH_MODEL)
        self.assertEqual(provider.model, THOROUGH_MODEL)

    def test_explicit_client_bypasses_model_override_entirely(self):
        # Dependency injection (an existing, pre-V2.4 capability) still
        # wins outright - model_override must never override an
        # explicitly-provided client.
        fake_client = mock.Mock()
        fake_client.model = "some-other-model"
        provider = AIProvider(client=fake_client, model_override=FAST_MODEL)
        self.assertEqual(provider.model, "some-other-model")


class AskEngineModeRoutingTests(unittest.TestCase):
    def test_fast_mode_selects_the_3b_model(self):
        engine = AskEngine(model_override=FAST_MODEL)
        self.assertIsNotNone(engine.ai_provider)
        self.assertEqual(engine.ai_provider.model, FAST_MODEL)

    def test_thorough_mode_selects_the_7b_model(self):
        engine = AskEngine(model_override=THOROUGH_MODEL)
        self.assertIsNotNone(engine.ai_provider)
        self.assertEqual(engine.ai_provider.model, THOROUGH_MODEL)

    def test_no_override_preserves_pre_v2_4_default_behavior(self):
        default_engine = AskEngine()
        overridden_engine = AskEngine(model_override=THOROUGH_MODEL)
        # Whatever the no-override engine resolves to, it must be
        # completely unaffected by the mere EXISTENCE of the mode
        # feature - identical to an explicit Thorough selection today,
        # since settings.ini's ollama_model is qwen2.5:7b.
        self.assertEqual(default_engine.ai_provider.model, overridden_engine.ai_provider.model)

    def test_set_model_override_changes_only_the_ai_provider(self):
        engine = AskEngine(model_override=THOROUGH_MODEL)
        engine._pending = {"kind": "candidate_selection", "candidates": ["a", "b"]}
        engine._history = ["a prior question"]

        engine.set_model_override(FAST_MODEL)

        self.assertEqual(engine.ai_provider.model, FAST_MODEL)
        # Untouched by the mode switch - this is the whole point of
        # set_model_override() existing instead of rebuilding AskEngine.
        self.assertEqual(engine._pending, {"kind": "candidate_selection", "candidates": ["a", "b"]})
        self.assertEqual(engine._history, ["a prior question"])

    def test_set_model_override_preserves_query_engine_and_database_identity(self):
        engine = AskEngine(model_override=THOROUGH_MODEL)
        query_engine_before = engine.query_engine
        database_before = engine.database

        engine.set_model_override(FAST_MODEL)

        self.assertIs(engine.query_engine, query_engine_before)
        self.assertIs(engine.database, database_before)


class AskEngineProviderSelectionTests(unittest.TestCase):
    """(testing) - AskEngine.set_provider(), added alongside the
    Claude provider so the Ask AI page can switch providers, not just
    models within Ollama. Same in-place-rebuild contract as
    set_model_override() above - only self.ai_provider/self.ai_error
    change."""

    def test_constructing_with_provider_name_none_is_unchanged_default_behavior(self):
        default_engine = AskEngine()
        explicit_engine = AskEngine(provider_name=None)
        self.assertEqual(default_engine.ai_provider.provider, explicit_engine.ai_provider.provider)

    def test_set_provider_switches_to_claude(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-routing-test-only"}):
            engine = AskEngine()
            engine.set_provider("claude")
        self.assertIsNotNone(engine.ai_provider)
        self.assertEqual(engine.ai_provider.provider, "claude (testing)")

    def test_set_provider_preserves_pending_and_history(self):
        engine = AskEngine()
        engine._pending = {"kind": "candidate_selection", "candidates": ["a", "b"]}
        engine._history = ["a prior question"]

        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-routing-test-only"}):
            engine.set_provider("claude")

        self.assertEqual(engine._pending, {"kind": "candidate_selection", "candidates": ["a", "b"]})
        self.assertEqual(engine._history, ["a prior question"])

    def test_set_provider_back_to_ollama_restores_it(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-routing-test-only"}):
            engine = AskEngine()
            engine.set_provider("claude")
        engine.set_provider("ollama", model_override=FAST_MODEL)
        self.assertEqual(engine.ai_provider.provider, "ollama")
        self.assertEqual(engine.ai_provider.model, FAST_MODEL)

    def test_missing_anthropic_key_sets_ai_error_not_a_crash(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            engine = AskEngine()
            engine.set_provider("claude")
        self.assertIsNone(engine.ai_provider)
        self.assertIn("ANTHROPIC_API_KEY", engine.ai_error)

    def test_set_model_override_after_set_provider_stays_on_claude(self):
        # set_model_override() must remember the provider set_provider()
        # switched to - it should never silently fall back to Ollama's
        # default provider just because a mode toggle fired afterward.
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-routing-test-only"}):
            engine = AskEngine()
            engine.set_provider("claude")
            engine.set_model_override(FAST_MODEL)  # ignored for Claude, but must not change provider
        self.assertEqual(engine.ai_provider.provider, "claude (testing)")


class AskResultModelFieldTests(unittest.TestCase):
    def test_model_defaults_to_none(self):
        result = AskResult(answer="x", intent="discovery")
        self.assertIsNone(result.model)

    def test_model_is_stored_when_provided(self):
        result = AskResult(answer="x", intent="root_cause", provider="ollama", model=FAST_MODEL)
        self.assertEqual(result.model, FAST_MODEL)


class InstantAnswerPathsUnaffectedByModeTests(unittest.TestCase):
    """
    Requirement 2's hard invariant: discovery/equipment_status/chitchat/
    factory-wide-timeline questions must skip the LLM entirely,
    regardless of which mode is selected.

    Verified by elapsed time, not by AskResult.provider/model being
    None - a live benchmark run during this phase showed those two
    fields are populated from "whatever provider the engine has
    configured" even on this instant/deterministic path (pre-existing
    behavior in app/ask.py's ask()/ask_structured() "intent is None"
    branch, not something this phase changed or should paper over with
    a wrong assertion). A real .generate() call takes minutes on this
    hardware (5-10 for root_cause, per CLAUDE.md) - completing in low
    single-digit seconds is conclusive proof no such call happened,
    and the answer text itself is checked for the known deterministic
    "Factory-wide status" template rather than free-form LLM prose.
    """

    INSTANT_PATH_TIME_BUDGET_SECONDS = 30.0  # generous vs. real LLM calls (minutes), tight vs. accidental LLM use

    def test_equipment_status_question_is_instant_in_fast_mode(self):
        engine = AskEngine(model_override=FAST_MODEL)
        start = time.time()
        result = engine.ask_structured("is everything ok")
        elapsed = time.time() - start

        self.assertLess(elapsed, self.INSTANT_PATH_TIME_BUDGET_SECONDS)
        self.assertIn("Factory-wide status", result.answer)

    def test_equipment_status_question_is_instant_in_thorough_mode(self):
        engine = AskEngine(model_override=THOROUGH_MODEL)
        start = time.time()
        result = engine.ask_structured("is everything ok")
        elapsed = time.time() - start

        self.assertLess(elapsed, self.INSTANT_PATH_TIME_BUDGET_SECONDS)
        self.assertIn("Factory-wide status", result.answer)

    def test_instant_answer_has_the_same_deterministic_shape_regardless_of_mode(self):
        # NOT an exact-text comparison - plc_logger.service keeps writing
        # fresh live readings in the background the whole time these
        # tests run, so two genuinely sequential queries against live
        # "current state" can legitimately differ in which tags are
        # currently in alarm/warning between calls. What must stay
        # identical is the deterministic TEMPLATE/shape itself.
        fast_engine = AskEngine(model_override=FAST_MODEL)
        thorough_engine = AskEngine(model_override=THOROUGH_MODEL)

        fast_result = fast_engine.ask_structured("is everything ok")
        thorough_result = thorough_engine.ask_structured("is everything ok")

        for result in (fast_result, thorough_result):
            self.assertIn("Factory-wide status", result.answer)
            self.assertIn("out of 575 monitored reading(s)", result.answer)


class GroundingAndFallbackUnaffectedByModeTests(unittest.TestCase):
    """
    Requirement 5: switching modes must never weaken grounding/fallback.
    Forces ai_provider to None (the established no-live-Ollama testing
    convention) to prove the deterministic-fallback path behaves
    identically no matter which model the engine was constructed with -
    the fallback doesn't even look at which model was configured.
    """

    def test_fallback_used_when_no_provider_regardless_of_configured_mode(self):
        for model in (FAST_MODEL, THOROUGH_MODEL):
            with self.subTest(model=model):
                engine = AskEngine(model_override=model)
                engine.ai_provider = None
                engine.ai_error = "forced unavailable for deterministic testing"

                result = engine.ask_structured("why is P01 WSP01 unhealthy")

                self.assertTrue(result.fallback_used)
                self.assertEqual(result.grounding_status, "not_applicable")
                self.assertIsNone(result.provider)
                self.assertIsNone(result.model)


if __name__ == "__main__":
    unittest.main()
