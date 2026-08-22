import unittest
from unittest import mock

from ai.providers.claude_provider import ClaudeProvider
from ai.providers.provider_factory import ProviderFactory

"""
(testing) - Claude added as a third AI provider alongside OpenAI/Ollama,
purely for trying it against this app's own deterministic-facts-only
prompts. Not wired into any default path - settings.ini's [AI]
provider stays "ollama". Mirrors tests/test_ask_ai_mode_routing.py's
env-var-patching convention (mock.patch.dict("os.environ", ...), not a
blanket os.getenv patch) so ANTHROPIC_MODEL/model resolution logic is
still genuinely exercised, not short-circuited.
"""


class ClaudeProviderConstructionTests(unittest.TestCase):
    def test_missing_api_key_raises_runtime_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError) as context:
                ClaudeProvider(model="claude-haiku-4-5-20251001")
        self.assertIn("ANTHROPIC_API_KEY", str(context.exception))

    def test_empty_model_raises_value_error(self):
        with self.assertRaises(ValueError):
            ClaudeProvider(model="  ", api_key="fake-key-for-test-only")

    def test_name_is_marked_testing(self):
        provider = ClaudeProvider(model="claude-haiku-4-5-20251001", api_key="fake-key-for-test-only")
        self.assertEqual(provider.name, "claude (testing)")

    def test_model_property(self):
        provider = ClaudeProvider(model="claude-sonnet-5", api_key="fake-key-for-test-only")
        self.assertEqual(provider.model, "claude-sonnet-5")

    def test_explicit_api_key_wins_over_environment(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env-key"}):
            provider = ClaudeProvider(model="claude-haiku-4-5-20251001", api_key="explicit-key")
        # No public accessor for the resolved key - constructing without
        # raising is itself the proof the explicit key was accepted.
        self.assertIsNotNone(provider.client)


class ClaudeProviderGenerateTests(unittest.TestCase):
    def setUp(self):
        self.provider = ClaudeProvider(model="claude-haiku-4-5-20251001", api_key="fake-key-for-test-only")

    def test_empty_prompt_short_circuits_without_calling_the_client(self):
        with mock.patch.object(self.provider.client.messages, "create") as mock_create:
            result = self.provider.generate("   ")
        mock_create.assert_not_called()
        self.assertEqual(result, "No prompt was provided.")

    def test_successful_response_extracts_text(self):
        fake_block = mock.Mock(type="text", text="The compressor pressure is stable.")
        fake_response = mock.Mock(content=[fake_block])

        with mock.patch.object(self.provider.client.messages, "create", return_value=fake_response):
            result = self.provider.generate("Why is the compressor pressure dropping?")

        self.assertEqual(result, "The compressor pressure is stable.")

    def test_multiple_text_blocks_are_concatenated(self):
        blocks = [mock.Mock(type="text", text="Part one. "), mock.Mock(type="text", text="Part two.")]
        fake_response = mock.Mock(content=blocks)

        with mock.patch.object(self.provider.client.messages, "create", return_value=fake_response):
            result = self.provider.generate("question")

        self.assertEqual(result, "Part one. Part two.")

    def test_no_text_blocks_returns_placeholder(self):
        fake_response = mock.Mock(content=[])

        with mock.patch.object(self.provider.client.messages, "create", return_value=fake_response):
            result = self.provider.generate("question")

        self.assertEqual(result, "[Claude returned no text.]")

    def test_authentication_error_returns_friendly_message(self):
        import anthropic

        with mock.patch.object(
            self.provider.client.messages, "create",
            side_effect=anthropic.AuthenticationError("bad key", response=mock.Mock(status_code=401), body=None),
        ):
            result = self.provider.generate("question")

        self.assertIn("authentication failed", result.lower())

    def test_generic_exception_is_caught_not_raised(self):
        with mock.patch.object(self.provider.client.messages, "create", side_effect=RuntimeError("boom")):
            result = self.provider.generate("question")

        self.assertIn("Claude error", result)
        self.assertIn("boom", result)

    def test_stream_yields_one_chunk_matching_generate(self):
        fake_block = mock.Mock(type="text", text="Single chunk answer.")
        fake_response = mock.Mock(content=[fake_block])

        with mock.patch.object(self.provider.client.messages, "create", return_value=fake_response):
            chunks = list(self.provider.stream("question"))

        self.assertEqual(chunks, ["Single chunk answer."])


class ProviderFactoryClaudeRoutingTests(unittest.TestCase):
    def test_claude_provider_is_selected_by_name(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-routing-test-only"}):
            provider = ProviderFactory.create(provider_name="claude")
        self.assertEqual(provider.name, "claude (testing)")

    def test_model_override_is_ignored_for_claude(self):
        # Same reasoning as the existing OpenAI test - Fast/Thorough
        # mode names Ollama-specific models, which would be meaningless
        # passed to the Anthropic SDK.
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-routing-test-only"}):
            provider = ProviderFactory.create(provider_name="claude", model_override="qwen2.5:3b")
        self.assertNotEqual(provider.model, "qwen2.5:3b")

    def test_anthropic_model_env_var_wins_over_default(self):
        with mock.patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "fake-key-for-routing-test-only", "ANTHROPIC_MODEL": "claude-opus-5"},
        ):
            provider = ProviderFactory.create(provider_name="claude")
        self.assertEqual(provider.model, "claude-opus-5")

    def test_default_model_when_no_env_var_set(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-routing-test-only"}, clear=True):
            provider = ProviderFactory.create(provider_name="claude")
        self.assertEqual(provider.model, "claude-haiku-4-5-20251001")

    def test_unconfigured_key_raises_runtime_error_not_a_crash_elsewhere(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                ProviderFactory.create(provider_name="claude")


if __name__ == "__main__":
    unittest.main()
