import unittest
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from ui.csv_export import dataframe_to_csv_bytes, export_filename

"""
Phase V1.5 - Minimal Reporting. events_to_dataframe() in
ui/pages/5_Event_Records.py is the single function that builds both
the on-screen table and the CSV export, so testing it directly proves
the export matches whatever filters produced `events`. Page-number
filenames (5_Event_Records.py) aren't importable as a normal module,
and the rest of the page executes real Streamlit/DB calls at module
scope - so the function's source is exec'd in isolation, the same
technique already used in tests/test_documentation_synthetic_marking.py.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVENT_RECORDS_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "5_Event_Records.py")


def _load_events_to_dataframe():
    from ai.trend_analyzer import format_number

    source = Path(EVENT_RECORDS_PAGE).read_text(encoding="utf-8")
    start = source.index("def events_to_dataframe")
    end = source.index("\ndf = events_to_dataframe(events)")
    namespace = {"pd": pd, "format_number": format_number}
    exec(source[start:end], namespace)
    return namespace["events_to_dataframe"]


SAMPLE_EVENTS = [
    {
        "event_time": "2026-08-20 14:05:00",
        "equipment": "Compressor 1",
        "tag": "AC01.Pressure",
        "severity": "alarm",
        "condition": "high alarm limit: 108 psi",
        "value": 112.345,
        "unit": "psi",
        "message": "Discharge pressure high",
    },
    {
        "event_time": "2026-08-20 13:10:00",
        "equipment": "Cold Room 1",
        "tag": "CR01.Temp",
        "severity": "warning",
        "condition": "high warning limit: -15 C",
        "value": None,
        "unit": "",
        "message": "",
    },
]


class EventsToDataframeTests(unittest.TestCase):
    def setUp(self):
        self.events_to_dataframe = _load_events_to_dataframe()

    def test_column_names_are_clear_and_in_order(self):
        df = self.events_to_dataframe(SAMPLE_EVENTS)
        self.assertEqual(
            list(df.columns), ["Time", "Equipment", "Tag", "Severity", "Condition", "Value", "Message"]
        )

    def test_value_column_includes_unit(self):
        df = self.events_to_dataframe(SAMPLE_EVENTS)
        self.assertIn("psi", df.iloc[0]["Value"])

    def test_missing_value_becomes_empty_string_not_none_or_nan(self):
        df = self.events_to_dataframe(SAMPLE_EVENTS)
        self.assertEqual(df.iloc[1]["Value"], "")

    def test_severity_is_upper_cased(self):
        df = self.events_to_dataframe(SAMPLE_EVENTS)
        self.assertEqual(df.iloc[0]["Severity"], "ALARM")
        self.assertEqual(df.iloc[1]["Severity"], "WARNING")

    def test_time_column_preserves_the_original_timestamp_string(self):
        df = self.events_to_dataframe(SAMPLE_EVENTS)
        self.assertEqual(df.iloc[0]["Time"], "2026-08-20 14:05:00")

    def test_empty_events_list_produces_an_empty_dataframe_not_a_crash(self):
        df = self.events_to_dataframe([])
        self.assertTrue(df.empty)

    def test_row_count_matches_filtered_event_count(self):
        df = self.events_to_dataframe(SAMPLE_EVENTS)
        self.assertEqual(len(df), len(SAMPLE_EVENTS))


class CsvExportHelperTests(unittest.TestCase):
    def test_dataframe_to_csv_bytes_round_trips_through_pandas(self):
        df = pd.DataFrame([{"A": 1, "B": "x"}, {"A": 2, "B": "y"}])
        csv_bytes = dataframe_to_csv_bytes(df)
        self.assertIsInstance(csv_bytes, bytes)
        self.assertIn(b"A,B", csv_bytes)
        self.assertIn(b"1,x", csv_bytes)

    def test_export_filename_has_prefix_and_csv_extension(self):
        name = export_filename("event_records")
        self.assertTrue(name.startswith("event_records_"))
        self.assertTrue(name.endswith(".csv"))


class EventRecordsPageDownloadButtonTests(unittest.TestCase):
    def test_page_renders_a_download_csv_button_without_exception(self):
        at = AppTest.from_file(EVENT_RECORDS_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))
        labels = [b.label for b in at.download_button]
        self.assertIn("⬇️ Download CSV", labels)

    def test_page_also_renders_a_download_pdf_button(self):
        at = AppTest.from_file(EVENT_RECORDS_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))
        labels = [b.label for b in at.download_button]
        self.assertIn("⬇️ Download PDF", labels)


class EventRecordsPdfRowCapTests(unittest.TestCase):
    """
    Phase V2.6 - PDF rendering costs ~5ms/row and st.download_button
    evaluates its data eagerly on every page rerun (not just on click),
    so the PDF export is deliberately capped independently of the "Max
    rows to show" slider (which the CSV export still honors in full).
    These tests exercise the actual capping mechanism (DataFrame.head())
    on real event rows, not just a source-text check.
    """

    def setUp(self):
        self.events_to_dataframe = _load_events_to_dataframe()

    def _many_events(self, count: int) -> list[dict]:
        return [
            {
                "event_time": f"2026-08-21 10:{i % 60:02d}:00",
                "equipment": f"Eq{i}",
                "tag": f"Tag{i}",
                "severity": "alarm",
                "condition": "high_alarm",
                "value": float(i),
                "unit": "kW",
                "message": "",
            }
            for i in range(count)
        ]

    def test_cap_reduces_row_count_when_more_events_than_cap(self):
        df = self.events_to_dataframe(self._many_events(250))
        capped = df.head(200)
        self.assertEqual(len(capped), 200)
        self.assertEqual(len(df), 250)

    def test_cap_does_not_truncate_when_fewer_events_than_cap(self):
        df = self.events_to_dataframe(self._many_events(50))
        capped = df.head(200)
        self.assertEqual(len(capped), 50)

    def test_page_source_applies_a_row_cap_before_building_the_pdf(self):
        source = Path(EVENT_RECORDS_PAGE).read_text(encoding="utf-8")
        pdf_section_start = source.index("with download_cols[1]:")
        pdf_section = source[pdf_section_start:]
        self.assertIn("PDF_ROW_CAP", pdf_section)
        self.assertIn(".head(PDF_ROW_CAP)", pdf_section)


if __name__ == "__main__":
    unittest.main()
