import unittest

import pandas as pd
from pypdf import PdfReader
from io import BytesIO

from ui.pdf_export import build_pdf_report, export_filename

"""
Phase V2.6 - PDF Reporting. Unit tests for the shared, generic PDF
builder (ui/pdf_export.py) - valid PDF bytes, correct text content
(verified by actually parsing the PDF back with pypdf, not just
checking byte length), XML-escaping of special characters, row
highlighting, and the empty-data edge case. Page-level integration
(row caps, which columns become header parameters) is covered in
tests/test_event_records_csv_export.py and
tests/test_energy_dashboard_csv_export.py, which already load each
page's real export-preparation logic.
"""


def _extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


class BuildPdfReportTests(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame(
            [
                {"Equipment": "AC01", "Value": "112.3 psi", "Severity": "ALARM"},
                {"Equipment": "CR01", "Value": "5.2 C", "Severity": "WARNING"},
            ]
        )

    def test_produces_valid_pdf_bytes(self):
        pdf_bytes = build_pdf_report("Test Report", {}, self.df)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))

    def test_pdf_is_parseable_and_has_one_page(self):
        pdf_bytes = build_pdf_report("Test Report", {}, self.df)
        reader = PdfReader(BytesIO(pdf_bytes))
        self.assertEqual(len(reader.pages), 1)

    def test_title_appears_in_extracted_text(self):
        pdf_bytes = build_pdf_report("My Special Report Title", {}, self.df)
        self.assertIn("My Special Report Title", _extract_text(pdf_bytes))

    def test_parameters_appear_in_extracted_text(self):
        pdf_bytes = build_pdf_report("Test Report", {"Plant": "P01", "Severity": "All"}, self.df)
        text = _extract_text(pdf_bytes)
        self.assertIn("P01", text)
        self.assertIn("Severity", text)

    def test_column_headers_and_row_values_appear(self):
        pdf_bytes = build_pdf_report("Test Report", {}, self.df)
        text = _extract_text(pdf_bytes)
        self.assertIn("Equipment", text)
        self.assertIn("AC01", text)
        self.assertIn("112.3 psi", text)

    def test_generated_at_defaults_to_now_when_not_given(self):
        pdf_bytes = build_pdf_report("Test Report", {}, self.df)
        self.assertIn("Generated", _extract_text(pdf_bytes))

    def test_explicit_generated_at_is_used_verbatim(self):
        pdf_bytes = build_pdf_report("Test Report", {}, self.df, generated_at="2026-01-01 00:00:00")
        self.assertIn("2026-01-01 00:00:00", _extract_text(pdf_bytes))

    def test_empty_dataframe_does_not_crash(self):
        pdf_bytes = build_pdf_report("Test Report", {}, pd.DataFrame())
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertIn("No matching data", _extract_text(pdf_bytes))

    def test_ampersand_and_angle_brackets_are_escaped_not_dropped(self):
        # reportlab's Paragraph interprets a small XML-like markup
        # subset - unescaped "&"/"<"/">" in real cell data would either
        # render wrong or raise, not just look slightly off.
        df = pd.DataFrame([{"Message": "Pressure high & rising, threshold <100>"}])
        pdf_bytes = build_pdf_report("Test Report", {}, df)
        text = _extract_text(pdf_bytes)
        self.assertIn("Pressure high & rising", text)
        self.assertIn("<100>", text)

    def test_highlight_column_does_not_change_cell_content(self):
        pdf_bytes_with_highlight = build_pdf_report("Test Report", {}, self.df, highlight_column="Severity")
        pdf_bytes_without = build_pdf_report("Test Report", {}, self.df)
        self.assertEqual(_extract_text(pdf_bytes_with_highlight), _extract_text(pdf_bytes_without))

    def test_unrecognized_highlight_value_does_not_crash(self):
        df = pd.DataFrame([{"Severity": "UNKNOWN_VALUE"}])
        pdf_bytes = build_pdf_report("Test Report", {}, df, highlight_column="Severity")
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))

    def test_missing_highlight_column_does_not_crash(self):
        pdf_bytes = build_pdf_report("Test Report", {}, self.df, highlight_column="NotAColumn")
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))

    def test_wide_dataframe_uses_landscape_orientation(self):
        # Indirect check via page size in points: landscape A4 is wider
        # than tall; portrait A4 is taller than wide.
        wide_df = pd.DataFrame([{f"col{i}": "x" for i in range(8)}])
        pdf_bytes = build_pdf_report("Test Report", {}, wide_df)
        reader = PdfReader(BytesIO(pdf_bytes))
        box = reader.pages[0].mediabox
        self.assertGreater(float(box.width), float(box.height))

    def test_narrow_dataframe_uses_portrait_orientation(self):
        narrow_df = pd.DataFrame([{"a": "x", "b": "y"}])
        pdf_bytes = build_pdf_report("Test Report", {}, narrow_df)
        reader = PdfReader(BytesIO(pdf_bytes))
        box = reader.pages[0].mediabox
        self.assertLess(float(box.width), float(box.height))


class ExportFilenameTests(unittest.TestCase):
    def test_has_prefix_and_pdf_extension(self):
        name = export_filename("event_records")
        self.assertTrue(name.startswith("event_records_"))
        self.assertTrue(name.endswith(".pdf"))

    def test_custom_extension_is_honored(self):
        name = export_filename("event_records", extension="txt")
        self.assertTrue(name.endswith(".txt"))


if __name__ == "__main__":
    unittest.main()
