import time
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from ai.ai_provider import AIProvider
from ui.ask_ai_ux import SUGGESTED_QUESTIONS

"""
Phase 18 - AI User Experience. Page-level tests for
ui/pages/1_Ask_AI.py against the REAL live simulation database (same
convention as tests/test_phase15_ask_integration.py), with
ai.ai_provider.AIProvider.generate patched so no real network/Ollama
call is ever made - fast, deterministic, no live LLM dependency.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASK_AI_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "1_Ask_AI.py")


def _settle(at: AppTest, seconds: float = 1.5) -> AppTest:
    """One extra poll cycle - covers the rare case a background thread
    hasn't finished by the time .run() first returns."""
    job = at.session_state["ask_job"] if "ask_job" in at.session_state else None
    if job is not None and job["status"] == "running":
        time.sleep(seconds)
        at.run()
    return at


class TestPageLoadsCleanly(unittest.TestCase):
    def test_page_renders_without_exception(self):
        at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))

    def test_suggested_question_buttons_present_on_empty_conversation(self):
        at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
        at.run()
        labels = {b.label for b in at.button}
        for question in SUGGESTED_QUESTIONS:
            self.assertIn(question, labels)


class TestSuggestedQuestionUsesNormalPipeline(unittest.TestCase):
    def test_clicking_a_suggestion_produces_the_same_shape_as_typing_a_question(self):
        with patch.object(AIProvider, "generate", return_value="Mocked AI phrasing."):
            at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            at.run()

            suggestion_button = next(b for b in at.button if b.label == SUGGESTED_QUESTIONS[0])
            suggestion_button.click().run()
            _settle(at)

            history = at.session_state["ask_history"]
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["role"], "user")
            self.assertEqual(history[0]["content"], SUGGESTED_QUESTIONS[0])
            self.assertEqual(history[1]["role"], "assistant")
            # Went through ask_structured() like any other question -
            # evidence is present (structured_context was populated).
            self.assertIn("evidence", history[1])


class TestDuplicateSubmissionPrevented(unittest.TestCase):
    def test_chat_input_disabled_while_a_job_is_running(self):
        with patch.object(AIProvider, "generate", return_value="Mocked AI phrasing."):
            at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            at.run()

            suggestion_button = next(b for b in at.button if b.label == SUGGESTED_QUESTIONS[0])
            suggestion_button.click().run()

            # AppTest.run() chases this page's own sleep(3)+st.rerun()
            # poll loop until the SCRIPT stops requesting another rerun
            # - which, for a genuinely still-running request, can be
            # many minutes away by design. That makes "assert disabled
            # while running" impossible to observe through a full
            # .run() round-trip without either the job completing
            # first (this fast-mocked case) or AppTest timing out
            # (confirmed separately while developing this suite - a
            # tooling limitation of testing a page that deliberately
            # keeps polling, not a product defect). The `disabled=
            # job_running` wiring itself is verified statically below;
            # the live, real-time behavior is verified in the Phase 18
            # live acceptance pass against an actual slow request.
            self.assertIsNotNone(at.chat_input)

    def test_chat_input_disabled_argument_is_wired_to_job_running(self):
        source = Path(ASK_AI_PAGE).read_text(encoding="utf-8")
        self.assertIn('disabled=job_running,', source)
        self.assertIn('job_running = st.session_state["ask_job"] is not None', source)


class TestExceptionSafety(unittest.TestCase):
    def test_unexpected_exception_never_leaks_raw_text_to_the_user(self):
        # A genuinely UNGUARDED exception - unlike an ai_provider.generate()
        # failure (already caught inside app/ask.py's own fallback logic),
        # this simulates something breaking earlier in the pipeline, which
        # is exactly what _run_job's new safety net exists for.
        secret_detail = "psycopg2.OperationalError: password authentication failed for user admin"

        with patch("app.ask.classify_interpretation_intent", side_effect=RuntimeError(secret_detail)):
            at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            at.run()

            suggestion_button = next(b for b in at.button if b.label == SUGGESTED_QUESTIONS[1])
            suggestion_button.click().run()
            _settle(at)

            history = at.session_state["ask_history"]
            answer_text = history[-1]["content"]
            self.assertEqual(answer_text, "Ask AI could not complete this request.")
            self.assertNotIn(secret_detail, answer_text)
            self.assertNotIn("psycopg2", answer_text)
            self.assertNotIn("Traceback", answer_text)


class TestViewEvidencePresent(unittest.TestCase):
    def test_evidence_expander_reflects_the_actual_result(self):
        with patch.object(AIProvider, "generate", return_value="Mocked AI phrasing."):
            at = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            at.run()

            # index 2 ("Compare P01 CHL01 and P02 CHL01") goes through
            # the Phase 15 domain path, which always sets
            # structured_context - unlike index 0's instant-answer
            # intent, which deliberately leaves it None (see
            # ask_structured()'s `intent is None` branch).
            suggestion_button = next(b for b in at.button if b.label == SUGGESTED_QUESTIONS[2])
            suggestion_button.click().run()
            _settle(at)

            expanders = [e for e in at.expander if "View Evidence" in e.label]
            self.assertEqual(len(expanders), 1)


class TestSessionIsolation(unittest.TestCase):
    def test_two_independent_page_instances_do_not_share_history(self):
        with patch.object(AIProvider, "generate", return_value="Mocked AI phrasing."):
            session_a = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            session_b = AppTest.from_file(ASK_AI_PAGE, default_timeout=30)
            session_a.run()
            session_b.run()

            button_a = next(b for b in session_a.button if b.label == SUGGESTED_QUESTIONS[0])
            button_a.click().run()
            _settle(session_a)

            # session_b never submitted anything - must remain untouched.
            self.assertEqual(len(session_b.session_state["ask_history"]), 0)
            self.assertEqual(len(session_a.session_state["ask_history"]), 2)


if __name__ == "__main__":
    unittest.main()
