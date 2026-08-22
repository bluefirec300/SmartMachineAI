import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

"""
(testing) - the Ask AI page's new "AI provider" radio (Ollama/Claude),
added alongside the pre-existing Response mode (Fast/Thorough) radio.
Same AppTest-against-the-real-live-simulation-database convention as
tests/test_phase18_ask_ai_page.py.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASK_AI_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "1_Ask_AI.py")


class ProviderSelectorDefaultStateTests(unittest.TestCase):
    def test_ollama_is_selected_by_default(self):
        at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))

        provider_radio = next(r for r in at.radio if r.label == "AI provider")
        self.assertEqual(provider_radio.value, "ollama")

    def test_response_mode_radio_is_visible_by_default(self):
        at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
        at.run()
        mode_radio = next(r for r in at.radio if r.label == "Response mode")
        self.assertIsNotNone(mode_radio)


class SwitchingToClaudeTests(unittest.TestCase):
    def test_selecting_claude_without_a_key_shows_a_warning_not_a_crash(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            at.run()

            provider_radio = next(r for r in at.radio if r.label == "AI provider")
            provider_radio.set_value("claude").run()

        self.assertFalse(bool(at.exception))
        warnings = " ".join(w.value for w in at.warning)
        self.assertIn("Claude (testing) isn't available", warnings)

    def test_selecting_claude_hides_response_mode_radio(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-test-only"}):
            at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            at.run()

            provider_radio = next(r for r in at.radio if r.label == "AI provider")
            provider_radio.set_value("claude").run()

        self.assertFalse(bool(at.exception))
        mode_radios = [r for r in at.radio if r.label == "Response mode"]
        self.assertEqual(mode_radios, [])
        captions = " ".join(c.value for c in at.caption)
        self.assertIn("Response mode doesn't apply to Claude", captions)

    def test_selecting_claude_with_a_valid_key_updates_the_engine_provider(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-test-only"}):
            at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            at.run()

            provider_radio = next(r for r in at.radio if r.label == "AI provider")
            provider_radio.set_value("claude").run()

        self.assertFalse(bool(at.exception))
        engine = at.session_state["ask_engine"]
        self.assertIsNotNone(engine.ai_provider)
        self.assertEqual(engine.ai_provider.provider, "claude (testing)")
        self.assertEqual([w.value for w in at.warning], [])


class SwitchingBackToOllamaTests(unittest.TestCase):
    def test_switching_claude_then_back_to_ollama_restores_response_mode(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-test-only"}):
            at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            at.run()

            provider_radio = next(r for r in at.radio if r.label == "AI provider")
            provider_radio.set_value("claude").run()
            provider_radio = next(r for r in at.radio if r.label == "AI provider")
            provider_radio.set_value("ollama").run()

        self.assertFalse(bool(at.exception))
        engine = at.session_state["ask_engine"]
        self.assertEqual(engine.ai_provider.provider, "ollama")
        mode_radio = next(r for r in at.radio if r.label == "Response mode")
        self.assertIsNotNone(mode_radio)


if __name__ == "__main__":
    unittest.main()
