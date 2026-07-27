import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai.threshold_reader import (
    ThresholdReader,
    format_threshold,
    format_thresholds,
)


class ThresholdReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = (
            tempfile.TemporaryDirectory()
        )

        self.database_path = (
            Path(self.temp_directory.name)
            / "config.db"
        )

        self._create_database()

        self.reader = ThresholdReader(
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
                CREATE TABLE thresholds
                (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag_name TEXT NOT NULL UNIQUE,
                    low_warning REAL,
                    low_alarm REAL,
                    high_warning REAL,
                    high_alarm REAL
                )
                """
            )

            connection.executemany(
                """
                INSERT INTO thresholds
                (
                    tag_name,
                    low_warning,
                    low_alarm,
                    high_warning,
                    high_alarm
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        "WaterPressure",
                        2.0,
                        1.0,
                        6.0,
                        8.0,
                    ),
                    (
                        "TankLevel",
                        20.0,
                        10.0,
                        90.0,
                        98.0,
                    ),
                    (
                        "CompressorPressure",
                        5.5,
                        4.5,
                        8.0,
                        9.0,
                    ),
                    (
                        "CompressorTemperature",
                        None,
                        None,
                        80.0,
                        95.0,
                    ),
                ],
            )

    def test_get_all_thresholds(self):
        thresholds = (
            self.reader.get_all_thresholds()
        )

        self.assertEqual(
            len(thresholds),
            4,
        )

    def test_get_exact_threshold(self):
        threshold = self.reader.get_threshold(
            "WaterPressure"
        )

        self.assertIsNotNone(
            threshold
        )

        self.assertEqual(
            threshold["high_alarm"],
            8.0,
        )

    def test_get_threshold_ignores_spaces(self):
        threshold = self.reader.get_threshold(
            "water pressure"
        )

        self.assertIsNotNone(
            threshold
        )

        self.assertEqual(
            threshold["tag_name"],
            "WaterPressure",
        )

    def test_get_threshold_ignores_case(self):
        threshold = self.reader.get_threshold(
            "TANKLEVEL"
        )

        self.assertIsNotNone(
            threshold
        )

        self.assertEqual(
            threshold["tag_name"],
            "TankLevel",
        )

    def test_unknown_threshold_returns_none(self):
        threshold = self.reader.get_threshold(
            "UnknownTag"
        )

        self.assertIsNone(
            threshold
        )

    def test_find_thresholds(self):
        thresholds = self.reader.find_thresholds(
            "compressor"
        )

        tag_names = {
            threshold["tag_name"]
            for threshold in thresholds
        }

        self.assertEqual(
            tag_names,
            {
                "CompressorPressure",
                "CompressorTemperature",
            },
        )

    def test_find_by_words(self):
        thresholds = self.reader.find_by_words(
            [
                "compressor",
                "pressure",
            ]
        )

        self.assertEqual(
            len(thresholds),
            1,
        )

        self.assertEqual(
            thresholds[0]["tag_name"],
            "CompressorPressure",
        )

    def test_get_configured_values(self):
        threshold = self.reader.get_threshold(
            "WaterPressure"
        )

        configured = (
            self.reader.get_configured_values(
                threshold
            )
        )

        self.assertEqual(
            configured,
            {
                "low_alarm": 1.0,
                "low_warning": 2.0,
                "high_warning": 6.0,
                "high_alarm": 8.0,
            },
        )

    def test_format_threshold(self):
        threshold = self.reader.get_threshold(
            "WaterPressure"
        )

        text = format_threshold(
            threshold,
            unit="bar",
        )

        self.assertIn(
            "WaterPressure configured thresholds:",
            text,
        )

        self.assertIn(
            "Low warning: 2 bar",
            text,
        )

        self.assertIn(
            "High alarm: 8 bar",
            text,
        )

    def test_format_threshold_without_low_limits(self):
        threshold = self.reader.get_threshold(
            "CompressorTemperature"
        )

        text = format_threshold(
            threshold,
            unit="°C",
        )

        self.assertNotIn(
            "Low warning",
            text,
        )

        self.assertIn(
            "High warning: 80 °C",
            text,
        )

        self.assertIn(
            "High alarm: 95 °C",
            text,
        )

    def test_format_multiple_thresholds(self):
        thresholds = (
            self.reader.find_thresholds(
                "compressor"
            )
        )

        text = format_thresholds(
            thresholds,
            units={
                "CompressorPressure": "bar",
                "CompressorTemperature": "°C",
            },
        )

        self.assertIn(
            "CompressorPressure",
            text,
        )

        self.assertIn(
            "CompressorTemperature",
            text,
        )

        self.assertIn(
            "9 bar",
            text,
        )

        self.assertIn(
            "95 °C",
            text,
        )


if __name__ == "__main__":
    unittest.main()
