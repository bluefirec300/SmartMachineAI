import unittest

from ai.event_engine import (
    EventEngine,
    format_events,
)


class FakeEquipmentKnowledge:
    def __init__(self):
        self.reload_count = 0

    def get_all(self):
        return {
            "compressor": {
                "main_tags": [
                    "Pressure",
                    "Temperature",
                ],
                "related_tags": [
                    "FlowRate",
                ],
            },
            "pump": {
                "main_tags": [
                    "PumpCurrent",
                ],
                "related_tags": [],
            },
        }

    def reload(self):
        self.reload_count += 1


class EventEngineTests(unittest.TestCase):
    def setUp(self):
        self.knowledge = (
            FakeEquipmentKnowledge()
        )

        self.engine = EventEngine(
            equipment_knowledge=self.knowledge
        )

    def test_alarm_creates_event(self):
        rules = [
            {
                "tag": "Pressure",
                "severity": "alarm",
                "condition": "high_alarm",
                "message": (
                    "Pressure is critically high."
                ),
            }
        ]

        summaries = [
            {
                "tag": "Pressure",
                "address": "DM100",
                "updated": (
                    "2026-07-26T09:00:00"
                ),
                "current": 9.2,
                "unit": "bar",
                "samples": 10,
                "trend": "Rising rapidly",
                "average": 8.4,
                "minimum": 7.5,
                "maximum": 9.2,
                "change": 1.7,
            }
        ]

        events = self.engine.detect(
            rules,
            summaries,
            detected_at=(
                "2026-07-26T09:00:01+00:00"
            ),
        )

        self.assertEqual(
            len(events),
            1,
        )

        event = events[0]

        self.assertEqual(
            event["equipment"],
            "compressor",
        )

        self.assertEqual(
            event["tag"],
            "Pressure",
        )

        self.assertEqual(
            event["severity"],
            "alarm",
        )

        self.assertEqual(
            event["condition"],
            "high_alarm",
        )

        self.assertEqual(
            event["value"],
            9.2,
        )

        self.assertEqual(
            event["unit"],
            "bar",
        )

        self.assertEqual(
            event["event_time"],
            "2026-07-26T09:00:00",
        )

    def test_warning_creates_event(self):
        rules = [
            {
                "tag": "Temperature",
                "severity": "warning",
                "condition": "high_warning",
                "message": (
                    "Temperature is above normal."
                ),
            }
        ]

        summaries = [
            {
                "tag": "Temperature",
                "current": 72.0,
                "unit": "°C",
                "trend": "Rising",
            }
        ]

        events = self.engine.detect(
            rules,
            summaries,
            detected_at=(
                "2026-07-26T09:10:00+00:00"
            ),
        )

        self.assertEqual(
            len(events),
            1,
        )

        self.assertEqual(
            events[0]["equipment"],
            "compressor",
        )

        self.assertEqual(
            events[0]["severity"],
            "warning",
        )

    def test_normal_result_is_ignored(self):
        rules = [
            {
                "tag": "Pressure",
                "severity": "normal",
                "condition": "within_limits",
                "message": (
                    "Pressure is normal."
                ),
            }
        ]

        events = self.engine.detect(
            rules,
            [],
        )

        self.assertEqual(
            events,
            [],
        )

    def test_not_evaluated_is_ignored(self):
        rules = [
            {
                "tag": "UnknownTag",
                "severity": "normal",
                "condition": "not_evaluated",
                "message": (
                    "No threshold configured."
                ),
            }
        ]

        events = self.engine.detect(
            rules,
            [],
        )

        self.assertEqual(
            events,
            [],
        )

    def test_unknown_tag_uses_factory(self):
        rules = [
            {
                "tag": "ExternalSensor",
                "severity": "alarm",
                "condition": "high_alarm",
                "message": (
                    "External sensor alarm."
                ),
            }
        ]

        summaries = [
            {
                "tag": "ExternalSensor",
                "current": 100,
                "unit": "",
            }
        ]

        events = self.engine.detect(
            rules,
            summaries,
        )

        self.assertEqual(
            len(events),
            1,
        )

        self.assertEqual(
            events[0]["equipment"],
            "factory",
        )

    def test_related_tag_maps_to_equipment(self):
        rules = [
            {
                "tag": "FlowRate",
                "severity": "warning",
                "condition": "low_warning",
                "message": (
                    "Flow rate is low."
                ),
            }
        ]

        summaries = [
            {
                "tag": "FlowRate",
                "current": 12.0,
                "unit": "L/min",
            }
        ]

        events = self.engine.detect(
            rules,
            summaries,
        )

        self.assertEqual(
            events[0]["equipment"],
            "compressor",
        )

    def test_missing_summary_still_creates_event(self):
        rules = [
            {
                "tag": "PumpCurrent",
                "severity": "alarm",
                "condition": "high_alarm",
                "message": (
                    "Pump current is critically high."
                ),
            }
        ]

        events = self.engine.detect(
            rules,
            [],
            detected_at=(
                "2026-07-26T09:20:00+00:00"
            ),
        )

        self.assertEqual(
            len(events),
            1,
        )

        self.assertEqual(
            events[0]["equipment"],
            "pump",
        )

        self.assertIsNone(
            events[0]["value"],
        )

        self.assertEqual(
            events[0]["event_time"],
            "2026-07-26T09:20:00+00:00",
        )

    def test_format_events(self):
        events = [
            {
                "equipment": "compressor",
                "tag": "Pressure",
                "severity": "alarm",
                "condition": "high_alarm",
                "value": 9.2,
                "unit": "bar",
            }
        ]

        text = format_events(
            events
        )

        self.assertIn(
            "[ALARM]",
            text,
        )

        self.assertIn(
            "compressor / Pressure",
            text,
        )

        self.assertIn(
            "high alarm",
            text,
        )

        self.assertIn(
            "9.2 bar",
            text,
        )

    def test_format_empty_events(self):
        self.assertEqual(
            format_events([]),
            "No significant events detected.",
        )

    def test_reload_rebuilds_mapping(self):
        self.engine.reload()

        self.assertEqual(
            self.knowledge.reload_count,
            1,
        )

        self.assertEqual(
            self.engine.get_equipment_for_tag(
                "Pressure"
            ),
            "compressor",
        )


if __name__ == "__main__":
    unittest.main()
