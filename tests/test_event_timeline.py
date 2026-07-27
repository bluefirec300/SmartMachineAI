import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai.event_timeline import (
    EventTimeline,
    format_event_context,
    format_event_timeline,
    format_timeline_event,
)


class EventTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = (
            tempfile.TemporaryDirectory()
        )

        self.database_path = (
            Path(self.temp_directory.name)
            / "machine_data.db"
        )

        self._create_database()

        self.timeline = EventTimeline(
            database_path=self.database_path
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
                        6.5,
                        "bar",
                        "Rising",
                        "Water pressure is high.",
                        "SIM.DM141",
                        10,
                        5.8,
                        6.5,
                        6.2,
                        0.7,
                        "2026-07-26T10:00:01",
                    ),
                    (
                        "2026-07-26T10:05:00",
                        "2026-07-26T10:05:01",
                        "water",
                        "WaterPressure",
                        "alarm",
                        "high_alarm",
                        8.2,
                        "bar",
                        "Rising",
                        "Water pressure is critically high.",
                        "SIM.DM141",
                        10,
                        6.5,
                        8.2,
                        7.4,
                        1.7,
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

    def test_get_recent_events(self):
        events = self.timeline.get_recent_events(
            limit=2
        )

        self.assertEqual(
            len(events),
            2,
        )

        self.assertEqual(
            events[0]["tag"],
            "TransferPumpRun",
        )

        self.assertEqual(
            events[1]["condition"],
            "high_alarm",
        )

    def test_filter_by_equipment(self):
        events = self.timeline.get_recent_events(
            equipment="water"
        )

        self.assertEqual(
            len(events),
            2,
        )

        self.assertTrue(
            all(
                event["equipment"] == "water"
                for event in events
            )
        )

    def test_filter_by_tag(self):
        events = self.timeline.get_recent_events(
            tag="WaterPressure"
        )

        self.assertEqual(
            len(events),
            2,
        )

    def test_filter_by_severity(self):
        events = self.timeline.get_recent_events(
            severity="alarm"
        )

        self.assertEqual(
            len(events),
            1,
        )

        self.assertEqual(
            events[0]["condition"],
            "high_alarm",
        )

    def test_get_latest_event(self):
        event = self.timeline.get_latest_event()

        self.assertIsNotNone(
            event
        )

        self.assertEqual(
            event["tag"],
            "TransferPumpRun",
        )

    def test_get_events_between(self):
        events = self.timeline.get_events_between(
            start_time="2026-07-26T10:00:00",
            end_time="2026-07-26T10:05:00",
        )

        self.assertEqual(
            len(events),
            2,
        )

        self.assertEqual(
            events[0]["condition"],
            "high_warning",
        )

        self.assertEqual(
            events[1]["condition"],
            "high_alarm",
        )

    def test_get_context_around_event(self):
        context = (
            self.timeline.get_context_around_event(
                event_id=2,
                before_count=1,
                after_count=1,
            )
        )

        self.assertEqual(
            context["target"]["condition"],
            "high_alarm",
        )

        self.assertEqual(
            len(context["before"]),
            1,
        )

        self.assertEqual(
            context["before"][0]["condition"],
            "high_warning",
        )

        self.assertEqual(
            len(context["after"]),
            1,
        )

        self.assertEqual(
            context["after"][0]["condition"],
            "stopped",
        )

    def test_unknown_event_context(self):
        context = (
            self.timeline.get_context_around_event(
                event_id=999
            )
        )

        self.assertIsNone(
            context["target"]
        )

        self.assertEqual(
            context["before"],
            [],
        )

        self.assertEqual(
            context["after"],
            [],
        )

    def test_format_timeline_event(self):
        event = self.timeline.get_latest_event()

        text = format_timeline_event(
            event
        )

        self.assertIn(
            "2026-07-26 10:06:00",
            text,
        )

        self.assertIn(
            "[WARNING]",
            text,
        )

        self.assertIn(
            "TransferPumpRun",
            text,
        )

    def test_format_event_timeline(self):
        events = self.timeline.get_recent_events(
            limit=3
        )

        text = format_event_timeline(
            events
        )

        first_position = text.find(
            "10:00:00"
        )

        second_position = text.find(
            "10:05:00"
        )

        third_position = text.find(
            "10:06:00"
        )

        self.assertLess(
            first_position,
            second_position,
        )

        self.assertLess(
            second_position,
            third_position,
        )

    def test_format_event_context(self):
        context = (
            self.timeline.get_context_around_event(
                event_id=2,
                before_count=1,
                after_count=1,
            )
        )

        text = format_event_context(
            context
        )

        self.assertIn(
            "Events before:",
            text,
        )

        self.assertIn(
            "Target event:",
            text,
        )

        self.assertIn(
            "Events after:",
            text,
        )

        self.assertIn(
            "high alarm",
            text,
        )


if __name__ == "__main__":
    unittest.main()
