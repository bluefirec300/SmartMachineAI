import unittest

from ui.ask_ai_ux import (
    SUGGESTED_QUESTIONS,
    fallback_reason_prefix,
    provider_status_label,
    stage_message,
)

"""
Phase 18 - AI User Experience. Pure-function tests for
ui/ask_ai_ux.py - deliberately no Streamlit anywhere in this file,
matching the module itself (no st import, no AppTest needed).
"""


class TestProviderStatusLabel(unittest.TestCase):
    def test_ollama_maps_to_local_ai(self):
        self.assertEqual(provider_status_label("ollama"), "local AI")

    def test_openai_maps_to_openai(self):
        self.assertEqual(provider_status_label("openai"), "OpenAI")

    def test_unknown_provider_passed_through(self):
        self.assertEqual(provider_status_label("some_future_provider"), "some_future_provider")

    def test_none_provider_maps_to_generic_ai(self):
        self.assertEqual(provider_status_label(None), "AI")

    def test_never_contains_a_url_or_key_looking_string(self):
        # Cheap guard against accidentally wiring a connection detail
        # into this label later.
        for provider in ("ollama", "openai", None, "anything"):
            label = provider_status_label(provider)
            self.assertNotIn("http", label.lower())
            self.assertNotIn("key", label.lower())


class TestStageMessage(unittest.TestCase):
    def test_generating_stage_names_the_provider(self):
        message = stage_message("generating_ai", "local AI")
        self.assertEqual(message, "Generating explanation with local AI...")

    def test_generating_stage_with_openai_label(self):
        message = stage_message("generating_ai", "OpenAI")
        self.assertIn("OpenAI", message)

    def test_validating_stage_message(self):
        message = stage_message("validating", "local AI")
        self.assertIn("validat", message.lower())

    def test_preparing_or_unknown_stage_falls_back_to_generic_working_message(self):
        for stage in ("preparing_context", None, "some_future_stage"):
            message = stage_message(stage, "local AI")
            self.assertEqual(message, "Working on your question...")

    def test_never_contains_a_percentage_or_eta(self):
        for stage in ("preparing_context", "generating_ai", "validating", None):
            message = stage_message(stage, "local AI")
            self.assertNotIn("%", message)
            self.assertNotIn("remaining", message.lower())
            self.assertNotIn("eta", message.lower())


class TestFallbackReasonPrefix(unittest.TestCase):
    def test_grounding_violation_gets_the_grounding_wording(self):
        prefix = fallback_reason_prefix(fallback_used=True, grounding_status="violation_detected")
        self.assertIn("validation", prefix)
        self.assertIn("deterministic evidence", prefix)

    def test_provider_fallback_gets_the_provider_wording(self):
        prefix = fallback_reason_prefix(fallback_used=True, grounding_status="not_applicable")
        self.assertIn("unavailable", prefix)
        self.assertIn("deterministic evidence", prefix)

    def test_grounded_normal_answer_gets_no_prefix(self):
        self.assertIsNone(fallback_reason_prefix(fallback_used=False, grounding_status="grounded"))

    def test_not_checked_no_fallback_gets_no_prefix(self):
        # e.g. an old-pipeline tag-level answer with a working provider -
        # grounding was never run, but nothing was ever rejected either.
        self.assertIsNone(fallback_reason_prefix(fallback_used=False, grounding_status="not_checked"))

    def test_grounding_violation_wording_differs_from_provider_wording(self):
        grounding_prefix = fallback_reason_prefix(fallback_used=True, grounding_status="violation_detected")
        provider_prefix = fallback_reason_prefix(fallback_used=True, grounding_status="not_applicable")
        self.assertNotEqual(grounding_prefix, provider_prefix)

    def test_grounding_violation_wins_even_if_fallback_used_were_somehow_false(self):
        # Defensive: violation_detected is checked first regardless of
        # fallback_used, since a real violation should never be masked.
        prefix = fallback_reason_prefix(fallback_used=False, grounding_status="violation_detected")
        self.assertIn("validation", prefix)


class TestSuggestedQuestions(unittest.TestCase):
    def test_between_four_and_six_questions(self):
        self.assertGreaterEqual(len(SUGGESTED_QUESTIONS), 4)
        self.assertLessEqual(len(SUGGESTED_QUESTIONS), 6)

    def test_all_are_non_empty_strings(self):
        for question in SUGGESTED_QUESTIONS:
            self.assertIsInstance(question, str)
            self.assertTrue(question.strip())

    def test_no_duplicate_questions(self):
        self.assertEqual(len(SUGGESTED_QUESTIONS), len(set(SUGGESTED_QUESTIONS)))


if __name__ == "__main__":
    unittest.main()
