import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai.event_timeline import EventTimeline
from ai.timeline_query import (
    TimelineQuestionHandler,
    is_timeline_question,
    requested_context_counts,
    requested_event_limit,
    requested_severity,
    route_equipment,
    route_tag,
)


class TimelineQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = (
            tempfile.TemporaryDirectory()
        )

        self.database_path = (
            Path(self.temp_directory.name)
            / "machine_data.db"
        )

        self._create_database()

        timeline = EventTimeline(
            database_path=self.database_path
        )

        self.handler = (
            TimelineQuestionHandler(
                timeline=timeline
            )
        )

    def tearDown(self):
        self.temp_directory.cleanup()

    def _create_database(self):
        with sqlite3.connect(
            self.database_path
        ) as connection:
            connection.execute(
                """
                CREATE TABLE machine_events
                (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time TEXT NOT NULL,
                    detected_at TEXT,
                    equipment TEXT,
                    tag TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    value REAL,
                    unit TEXT,
                    trend TEXT,
                    message TEXT,
                    address TEXT,
                    samples INTEGER,
                    minimum REAL,
                    maximum REAL,
                    average REAL,
                    change REAL,
                    created_at TEXT
                )
                """
            )

            connection.executemany(
                """
                INSERT INTO machine_events
                (
                    event_time,
                    detected_at,
                    equipment,
                    tag,
                    severity,
                    condition,
                    value,
                    unit,
                    trend,
                    message,
                    address,
                    samples,
                    minimum,
                    maximum,
                    average,
                    change,
                    created_at
                )
                VALUES
                (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        "2026-07-26T10:00:00",
                        "2026-07-26T10:00:01",
                        "water",
                        "WaterPressure",
                        "warning",
                        "high_warning",
                        3.1,
                        "bar",
                        "Rising",
                        "Water pressure warning.",
                        "SIM.DM141",
                        10,
                        2.8,
                        3.1,
                        2.95,
                        0.3,
                        "2026-07-26T10:00:01",
                    ),
                    (
                        "2026-07-26T10:05:00",
                        "2026-07-26T10:05:01",
                        "water",
                        "WaterPressure",
                        "alarm",
                        "high_alarm",
                        3.5,
                        "bar",
                        "Rising",
                        "Water pressure alarm.",
                        "SIM.DM141",
                        10,
                        3.1,
                        3.5,
                        3.3,
                        0.4,
                        "2026-07-26T10:05:01",
                    ),
                    (
                        "2026-07-26T10:06:00",
                        "2026-07-26T10:06:01",
                        "pump",
                        "TransferPumpRun",
                        "warning",
                        "stopped",
                        0.0,
                        "",
                        "No trend",
                        "Transfer pump stopped.",
                        "SIM.DM150",
                        1,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        "2026-07-26T10:06:01",
                    ),
                ],
            )

    def test_detects_timeline_question(self):
        self.assertTrue(
            is_timeline_question(
                "What happened recently?"
            )
        )

        self.assertTrue(
            is_timeline_question(
                "Show the latest alarms"
            )
        )

    def test_rejects_current_value_question(self):
        self.assertFalse(
            is_timeline_question(
                "What is the current water pressure?"
            )
        )

    def test_requested_alarm_severity(self):
        self.assertEqual(
            requested_severity(
                "Show the latest alarms"
            ),
            "alarm",
        )

    def test_requested_warning_severity(self):
        self.assertEqual(
            requested_severity(
                "Show recent warnings"
            ),
            "warning",
        )

    def test_requested_limit_from_number(self):
        self.assertEqual(
            requested_event_limit(
                "Show the latest 3 events"
            ),
            3,
        )

    def test_latest_alarm_uses_one_event(self):
        self.assertEqual(
            requested_event_limit(
                "What was the latest alarm?"
            ),
            1,
        )

    def test_context_before_only(self):
        before_count, after_count = (
            requested_context_counts(
                "What happened before the latest alarm?"
            )
        )

        self.assertEqual(
            before_count,
            5,
        )

        self.assertEqual(
            after_count,
            0,
        )

    def test_route_equipment(self):
        route = {
            "equipment": "water",
            "tags": [
                "WaterPressure",
            ],
        }

        self.assertEqual(
            route_equipment(
                route
            ),
            "water",
        )

    def test_factory_route_has_no_equipment_filter(self):
        route = {
            "equipment": "factory",
            "tags": [],
        }

        self.assertIsNone(
            route_equipment(
                route
            )
        )

    def test_route_single_tag(self):
        route = {
            "equipment": "water",
            "tags": [
                "WaterPressure",
            ],
        }

        self.assertEqual(
            route_tag(
                route
            ),
            "WaterPressure",
        )

    def test_recent_events_answer(self):
        route = {
            "equipment": "factory",
            "tags": [],
        }

        answer = self.handler.answer(
            question="What happened recently?",
            route=route,
        )

        self.assertIn(
            "WaterPressure",
            answer,
        )

        self.assertIn(
            "TransferPumpRun",
            answer,
        )

    def test_latest_alarm_answer(self):
        route = {
            "equipment": "factory",
            "tags": [],
        }

        answer = self.handler.answer(
            question="What was the latest alarm?",
            route=route,
        )

        self.assertIn(
            "[ALARM]",
            answer,
        )

        self.assertIn(
            "WaterPressure",
            answer,
        )

        self.assertNotIn(
            "TransferPumpRun",
            answer,
        )

    def test_water_alarm_filter(self):
        route = {
            "equipment": "water",
            "tags": [
                "WaterPressure",
            ],
        }

        answer = self.handler.answer(
            question=(
                "What was the last water "
                "pressure alarm?"
            ),
            route=route,
        )

        self.assertIn(
            "WaterPressure",
            answer,
        )

        self.assertIn(
            "3.5 bar",
            answer,
        )

    def test_context_before_latest_alarm(self):
        route = {
            "equipment": "factory",
            "tags": [],
        }

        answer = self.handler.answer(
            question=(
                "What happened before "
                "the latest alarm?"
            ),
            route=route,
        )

        self.assertIn(
            "Events before:",
            answer,
        )

        self.assertIn(
            "Target event:",
            answer,
        )

        self.assertIn(
            "high warning",
            answer,
        )

        self.assertNotIn(
            "Events after:",
            answer,
        )


if __name__ == "__main__":
    unittest.main()
