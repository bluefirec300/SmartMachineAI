import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ai.observation_engine import (
    ObservationEngine,
    format_observations,
)
from ai.router import route_question
from ai.rule_engine import (
    RuleEngine,
    format_rule_results,
)
from ai.trend_analyzer import analyse_history
from database.database import DatabaseManager


class TestSmartMachinePipeline(unittest.TestCase):
    """
    Integration test covering:

    question
        -> router
        -> temporary historian
        -> trend analyser
        -> observation engine
        -> engineering rule engine

    No PLC connection and no AI provider are used.
    """

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()

        self.database_path = (
            Path(self.temp_directory.name)
            / "machine_data.db"
        )

        self.database = DatabaseManager(
            db_path=self.database_path
        )

        self.base_time = datetime.now() - timedelta(
            minutes=10
        )

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def save_series(
        self,
        tag: str,
        address: str,
        values: list[float],
    ) -> None:
        for index, value in enumerate(values):
            self.database.save_tag(
                tag=tag,
                address=address,
                value=value,
                timestamp=(
                    self.base_time
                    + timedelta(minutes=index)
                ),
            )

    def read_history(
        self,
        tags: list[str],
        limit: int,
    ) -> dict[str, list[dict]]:
        history = {}

        for tag in tags:
            rows = self.database.get_history(
                tag=tag,
                hours=24,
                limit=limit,
            )

            if rows:
                history[tag] = rows

        return history

    def test_compressor_pressure_alarm_pipeline(
        self,
    ) -> None:
        question = (
            "Show me the compressor pressure trend."
        )

        route = route_question(question)

        self.assertEqual(
            route["equipment"],
            "compressor",
        )
        self.assertEqual(
            route["intent"],
            "trend",
        )
        self.assertEqual(
            route["history_limit"],
            20,
        )
        self.assertIn(
            "CompressorPressure",
            route["tags"],
        )

        # Pressure falls from a normal value into the
        # configured low-alarm region.
        self.save_series(
            tag="CompressorPressure",
            address="DM100",
            values=[
                7.0,
                6.5,
                6.0,
                5.0,
                4.0,
            ],
        )

        history = self.read_history(
            tags=route["tags"],
            limit=route["history_limit"],
        )

        self.assertIn(
            "CompressorPressure",
            history,
        )

        summaries = analyse_history(history)

        self.assertEqual(
            len(summaries),
            1,
        )

        pressure_summary = summaries[0]

        self.assertEqual(
            pressure_summary["tag"],
            "CompressorPressure",
        )
        self.assertEqual(
            pressure_summary["current"],
            4.0,
        )
        self.assertEqual(
            pressure_summary["samples"],
            5,
        )
        self.assertIn(
            pressure_summary["trend"],
            {
                "Falling",
                "Falling rapidly",
            },
        )

        observation_engine = ObservationEngine()

        observations = observation_engine.build(
            summaries=summaries,
            intent=route["intent"],
        )

        observation_context = format_observations(
            observations
        )

        self.assertTrue(observations)
        self.assertIn(
            "Compressor discharge pressure",
            observation_context,
        )
        self.assertIn(
            "4 bar",
            observation_context,
        )

        rule_engine = RuleEngine()

        self.assertEqual(
            rule_engine.get_source(),
            "database",
        )

        rule_results = rule_engine.evaluate(
            summaries
        )

        self.assertEqual(
            len(rule_results),
            1,
        )

        pressure_result = rule_results[0]

        self.assertEqual(
            pressure_result["tag"],
            "CompressorPressure",
        )
        self.assertEqual(
            pressure_result["severity"],
            "alarm",
        )
        self.assertEqual(
            pressure_result["condition"],
            "low_alarm",
        )

        rule_context = format_rule_results(
            rule_results,
            include_normal=False,
        )

        self.assertIn(
            "[ALARM]",
            rule_context,
        )
        self.assertIn(
            "critically low",
            rule_context,
        )


if __name__ == "__main__":
    unittest.main()
