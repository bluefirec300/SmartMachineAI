import tempfile
import unittest
from pathlib import Path

from ai.event_store import EventStore


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = (
            tempfile.TemporaryDirectory()
        )

        self.database_path = (
            Path(self.temp_directory.name)
            / "test_machine_data.db"
        )

        self.store = EventStore(
            database_path=self.database_path
        )

    def tearDown(self):
        self.temp_directory.cleanup()

    @staticmethod
    def create_event(
        event_time="2026-07-26T09:00:00+00:00",
        tag="Pressure",
        severity="alarm",
        condition="high_alarm",
        equipment="compressor",
    ):
        return {
            "event_time": event_time,
            "detected_at": (
                "2026-07-26T09:00:01+00:00"
            ),
            "equipment": equipment,
            "tag": tag,
            "severity": severity,
            "condition": condition,
            "value": 9.2,
            "unit": "bar",
            "trend": "Rising rapidly",
            "message": (
                "Pressure is critically high."
            ),
            "address": "DM100",
            "samples": 10,
            "minimum": 7.5,
            "maximum": 9.2,
            "average": 8.4,
            "change": 1.7,
        }

    def test_database_is_created(self):
        self.assertTrue(
            self.database_path.exists()
        )

    def test_insert_event(self):
        event_id = self.store.insert_event(
            self.create_event()
        )

        self.assertIsNotNone(
            event_id
        )

        stored = self.store.get_event(
            event_id
        )

        self.assertEqual(
            stored["equipment"],
            "compressor",
        )

        self.assertEqual(
            stored["tag"],
            "Pressure",
        )

        self.assertEqual(
            stored["severity"],
            "alarm",
        )

        self.assertEqual(
            stored["value"],
            9.2,
        )

    def test_duplicate_event_is_ignored(self):
        event = self.create_event()

        first_id = self.store.insert_event(
            event
        )

        second_id = self.store.insert_event(
            event
        )

        self.assertIsNotNone(
            first_id
        )

        self.assertIsNone(
            second_id
        )

        self.assertEqual(
            self.store.count_events(),
            1,
        )

    def test_insert_multiple_events(self):
        events = [
            self.create_event(),
            self.create_event(
                event_time=(
                    "2026-07-26T09:05:00+00:00"
                ),
                tag="Temperature",
                severity="warning",
                condition="high_warning",
            ),
        ]

        inserted_ids = (
            self.store.insert_events(
                events
            )
        )

        self.assertEqual(
            len(inserted_ids),
            2,
        )

        self.assertEqual(
            self.store.count_events(),
            2,
        )

    def test_get_recent_events(self):
        self.store.insert_events(
            [
                self.create_event(
                    event_time=(
                        "2026-07-26T09:00:00+00:00"
                    )
                ),
                self.create_event(
                    event_time=(
                        "2026-07-26T09:10:00+00:00"
                    ),
                    tag="Temperature",
                    severity="warning",
                    condition="high_warning",
                ),
            ]
        )

        events = self.store.get_recent_events(
            limit=10
        )

        self.assertEqual(
            len(events),
            2,
        )

        self.assertEqual(
            events[0]["tag"],
            "Temperature",
        )

        self.assertEqual(
            events[1]["tag"],
            "Pressure",
        )

    def test_filter_by_equipment(self):
        self.store.insert_events(
            [
                self.create_event(),
                self.create_event(
                    event_time=(
                        "2026-07-26T09:10:00+00:00"
                    ),
                    tag="PumpCurrent",
                    equipment="pump",
                ),
            ]
        )

        events = self.store.get_recent_events(
            equipment="pump"
        )

        self.assertEqual(
            len(events),
            1,
        )

        self.assertEqual(
            events[0]["tag"],
            "PumpCurrent",
        )

    def test_filter_by_severity(self):
        self.store.insert_events(
            [
                self.create_event(),
                self.create_event(
                    event_time=(
                        "2026-07-26T09:10:00+00:00"
                    ),
                    tag="Temperature",
                    severity="warning",
                    condition="high_warning",
                ),
            ]
        )

        events = self.store.get_recent_events(
            severity="warning"
        )

        self.assertEqual(
            len(events),
            1,
        )

        self.assertEqual(
            events[0]["severity"],
            "warning",
        )

    def test_count_with_filters(self):
        self.store.insert_events(
            [
                self.create_event(),
                self.create_event(
                    event_time=(
                        "2026-07-26T09:10:00+00:00"
                    ),
                    tag="Temperature",
                    severity="warning",
                    condition="high_warning",
                ),
                self.create_event(
                    event_time=(
                        "2026-07-26T09:20:00+00:00"
                    ),
                    tag="Pressure",
                ),
            ]
        )

        pressure_count = (
            self.store.count_events(
                tag="Pressure"
            )
        )

        warning_count = (
            self.store.count_events(
                severity="warning"
            )
        )

        self.assertEqual(
            pressure_count,
            2,
        )

        self.assertEqual(
            warning_count,
            1,
        )

    def test_count_by_time_range(self):
        self.store.insert_events(
            [
                self.create_event(
                    event_time=(
                        "2026-07-20T09:00:00+00:00"
                    )
                ),
                self.create_event(
                    event_time=(
                        "2026-07-26T09:00:00+00:00"
                    )
                ),
            ]
        )

        count = self.store.count_events(
            start_time=(
                "2026-07-25T00:00:00+00:00"
            )
        )

        self.assertEqual(
            count,
            1,
        )

    def test_missing_required_field_fails(self):
        event = self.create_event()

        del event["tag"]

        with self.assertRaises(
            ValueError
        ):
            self.store.insert_event(
                event
            )

    def test_delete_all_events(self):
        self.store.insert_event(
            self.create_event()
        )

        deleted = (
            self.store.delete_all_events()
        )

        self.assertEqual(
            deleted,
            1,
        )

        self.assertEqual(
            self.store.count_events(),
            0,
        )


if __name__ == "__main__":
    unittest.main()
