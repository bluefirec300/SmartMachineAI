import unittest

from ai.query_dispatcher import (
    DispatchResult,
    QueryDispatcher,
    QueryType,
)


class QueryDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.dispatcher = QueryDispatcher()

    def test_current_pressure_question(self):
        result = self.dispatcher.dispatch(
            "What is the current water pressure?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.CURRENT_DATA,
        )

    def test_current_temperature_without_current_word(self):
        result = self.dispatcher.dispatch(
            "What is the compressor temperature?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.CURRENT_DATA,
        )

    def test_threshold_question(self):
        result = self.dispatcher.dispatch(
            "What is the water pressure high alarm setpoint?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.THRESHOLD,
        )

    def test_configured_limit_question(self):
        result = self.dispatcher.dispatch(
            "Show the configured compressor pressure limits."
        )

        self.assertEqual(
            result.query_type,
            QueryType.THRESHOLD,
        )

    def test_alarm_setting_question(self):
        result = self.dispatcher.dispatch(
            "What is the high alarm configured at?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.THRESHOLD,
        )

    def test_recent_event_question(self):
        result = self.dispatcher.dispatch(
            "What happened recently?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.TIMELINE,
        )

    def test_latest_alarm_question(self):
        result = self.dispatcher.dispatch(
            "Show the latest alarms."
        )

        self.assertEqual(
            result.query_type,
            QueryType.TIMELINE,
        )

    def test_before_alarm_question(self):
        result = self.dispatcher.dispatch(
            "What happened before the latest alarm?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.TIMELINE,
        )

    def test_root_cause_question(self):
        result = self.dispatcher.dispatch(
            "What caused the compressor to stop?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.ROOT_CAUSE,
        )

    def test_why_alarm_question(self):
        result = self.dispatcher.dispatch(
            "Why did the water pressure alarm occur?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.ROOT_CAUSE,
        )

    def test_root_cause_checked_before_timeline(self):
        result = self.dispatcher.dispatch(
            "Why did the latest alarm happen?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.ROOT_CAUSE,
        )

    def test_threshold_checked_before_current_data(self):
        result = self.dispatcher.dispatch(
            "What is the current high pressure alarm limit?"
        )

        self.assertEqual(
            result.query_type,
            QueryType.THRESHOLD,
        )

    def test_route_can_identify_current_query(self):
        route = {
            "equipment": "water",
            "measurements": [
                "pressure",
            ],
            "tags": [
                "WaterPressure",
            ],
            "intent": "current",
        }

        result = self.dispatcher.dispatch(
            question="water reading",
            route=route,
        )

        self.assertEqual(
            result.query_type,
            QueryType.CURRENT_DATA,
        )

    def test_general_question(self):
        result = self.dispatcher.dispatch(
            "Explain how a centrifugal pump works."
        )

        self.assertEqual(
            result.query_type,
            QueryType.GENERAL,
        )

    def test_empty_question(self):
        result = self.dispatcher.dispatch(
            "   "
        )

        self.assertEqual(
            result.query_type,
            QueryType.GENERAL,
        )

        self.assertEqual(
            result.confidence,
            0.0,
        )

    def test_dispatch_result_type(self):
        result = self.dispatcher.dispatch(
            "What is the current tank level?"
        )

        self.assertIsInstance(
            result,
            DispatchResult,
        )

        self.assertIsInstance(
            result.query_type,
            QueryType,
        )

    def test_reason_is_provided(self):
        result = self.dispatcher.dispatch(
            "Show recent events."
        )

        self.assertTrue(
            result.reason
        )

    def test_confidence_range(self):
        questions = [
            "What is the current pressure?",
            "Show the alarm thresholds.",
            "What happened recently?",
            "Why did the pump stop?",
            "Explain motor bearings.",
        ]

        for question in questions:
            result = self.dispatcher.dispatch(
                question
            )

            self.assertGreaterEqual(
                result.confidence,
                0.0,
            )

            self.assertLessEqual(
                result.confidence,
                1.0,
            )


if __name__ == "__main__":
    unittest.main()
