import unittest

from ai.router import route_question


class TestQuestionRouter(unittest.TestCase):

    def test_trend_questions(self):
        questions = [
            "Is the compressor pressure dropping?",
            "Is the pressure falling?",
            "Has the temperature increased?",
            "Is the tank level decreasing?",
            "Show the pressure trend.",
            "How has compressor pressure changed?",
            "Is compressor pressure stable?",
            "Is pressure fluctuating?",
        ]

        for question in questions:
            with self.subTest(question=question):
                route = route_question(question)

                self.assertEqual(
                    route["intent"],
                    "trend",
                )

                self.assertEqual(
                    route["history_limit"],
                    20,
                )

    def test_current_questions(self):
        questions = [
            "What is the compressor pressure?",
            "Show current pressure.",
            "Current temperature?",
            "What is the tank level?",
        ]

        for question in questions:
            with self.subTest(question=question):
                route = route_question(question)

                self.assertEqual(
                    route["intent"],
                    "current",
                )

                self.assertEqual(
                    route["history_limit"],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
