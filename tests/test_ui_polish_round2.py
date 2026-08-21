import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from ui import table_style as ts
from ui.evidence_display import render_readable

"""
UI polish - round 2, direct user feedback after the first pass:
- Asset Performance/Data Health green vs. grey too hard to tell apart
  -> palette brightened to match Event Records/Anomalies exactly.
- Evidence/Assumptions rendered as raw st.json() (internal field names,
  brackets/quotes) on Anomalies and Energy Opportunities -> readable
  labeled text instead.
- Several kWh figures shown with 3-4 decimals -> capped at 2.
- st.metric() used for long enum/identifier strings (Savings
  Verification, Anomalies Finding detail) reads oversized -> markdown.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANOMALIES_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "16_Anomalies.py")


class TestPaletteMatchesEstablishedConvention(unittest.TestCase):
    """Regression guard: the palette must stay bright/matching Event
    Records & Anomalies, not drift back to the low-contrast muted
    version this replaced."""

    def test_positive_tier_matches_event_records_alarm_free_green(self):
        background, _text = ts._PALETTE[ts.POSITIVE]
        self.assertEqual(background, "#b3ffb3")

    def test_warning_tier_matches_event_records_alarm_red(self):
        background, _text = ts._PALETTE[ts.WARNING]
        self.assertEqual(background, "#ffb3b3")

    def test_caution_tier_matches_event_records_warning_amber(self):
        background, _text = ts._PALETTE[ts.CAUTION]
        self.assertEqual(background, "#ffe6a3")

    def test_positive_and_neutral_are_clearly_different_backgrounds(self):
        positive_bg, _ = ts._PALETTE[ts.POSITIVE]
        neutral_bg, _ = ts._PALETTE[ts.NEUTRAL]
        self.assertNotEqual(positive_bg, neutral_bg)
        # Not just different strings - meaningfully different lightness/hue,
        # not two near-identical greys/greens.
        self.assertNotEqual(positive_bg[:3], neutral_bg[:3])


class TestReadableEvidenceRendering(unittest.TestCase):
    """render_readable() never shows raw Python/JSON punctuation for
    the shapes anomaly_engine.py and opportunity_engine.py actually
    produce (verified against real stored evidence_json)."""

    @patch("ui.evidence_display.st")
    def test_list_of_strings_renders_as_bullets(self, mock_st):
        render_readable(["deviation threshold: 4.38x MAD", "persistence required: 5 periods"])
        calls = [c.args[0] for c in mock_st.markdown.call_args_list]
        self.assertEqual(calls, ["- deviation threshold: 4.38x MAD", "- persistence required: 5 periods"])
        mock_st.json.assert_not_called()

    @patch("ui.evidence_display.st")
    def test_nested_dict_with_notes_list_renders_readable(self, mock_st):
        render_readable({
            "context_used": {"loaded_state": "loaded", "hour_bucket": "04-08"},
            "notes": ["Engineering limit also exceeded as of 2026-08-16 07:45:10 (warning_exceeded)"],
            "fault_window_noted": True,
        })
        rendered = " ".join(c.args[0] for c in mock_st.markdown.call_args_list)
        self.assertIn("Engineering limit also exceeded", rendered)
        self.assertIn("Yes", rendered)  # fault_window_noted: True -> "Yes"
        # No raw internal field name should appear un-relabeled with its
        # underscore intact right next to a colon (i.e. it was humanized).
        self.assertNotIn("fault_window_noted:", rendered)
        mock_st.json.assert_not_called()

    @patch("ui.evidence_display.st")
    def test_empty_value_shows_none_recorded_not_empty_brackets(self, mock_st):
        render_readable([])
        mock_st.write.assert_called_with("None recorded.")

    @patch("ui.evidence_display.st")
    def test_plain_string_falls_back_to_write(self, mock_st):
        render_readable("a plain non-JSON string")
        mock_st.write.assert_called_with("a plain non-JSON string")


class TestAnomaliesPageEvidenceIsReadable(unittest.TestCase):
    def test_evidence_expander_never_shows_raw_json_dict_syntax(self):
        at = AppTest.from_file(ANOMALIES_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))

        # Not every environment necessarily has a seeded anomaly with
        # nested evidence, so this is a smoke check: if the page
        # rendered a Finding detail section at all, its evidence text
        # must never contain raw dict/JSON punctuation patterns like
        # `{"key":` or `context_used` (an internal field name that
        # should have been humanized/relabeled).
        markdown_text = " ".join(m.value for m in at.markdown)
        self.assertNotIn('{"', markdown_text)
        self.assertNotIn("context_used", markdown_text)


class TestFindingColumnWidened(unittest.TestCase):
    def test_finding_column_has_explicit_width_config(self):
        source = (PROJECT_ROOT / "ui" / "pages" / "16_Anomalies.py").read_text(encoding="utf-8")
        self.assertIn('"Finding": st.column_config.TextColumn("Finding", width="large")', source)


class TestKwhDecimalCap(unittest.TestCase):
    """Every kWh-denominated display site touched in this round caps
    at 2 decimals - guards against a future edit silently reintroducing
    3-4 decimal noise."""

    def test_energy_dashboard_has_no_kwh_decimals_beyond_2(self):
        source = (PROJECT_ROOT / "ui" / "pages" / "15_Energy_Dashboard.py").read_text(encoding="utf-8")
        self.assertNotIn("decimals=4", source)
        self.assertNotIn('"Header Specific Energy", air["header_specific_energy"], decimals=3', source)
        self.assertNotIn('"RO/DI (RO01) kWh/m3", water["RO01"], decimals=3', source)
        self.assertNotIn('"Water Treatment (SYS01) kWh/m3", water["SYS01"], decimals=3', source)

    def test_savings_verification_energy_saving_capped_at_2_decimals(self):
        source = (PROJECT_ROOT / "ui" / "pages" / "18_Savings_Verification.py").read_text(encoding="utf-8")
        self.assertIn("verified_energy_kwh']:.2f} kWh", source)
        self.assertNotIn("verified_energy_kwh']:.3f} kWh", source)

    def test_savings_verification_engine_reason_strings_capped_at_2_decimals(self):
        source = (PROJECT_ROOT / "engine" / "savings_verification_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("absolute_energy_saving_kwh']:.3f}", source)
        self.assertIn("absolute_energy_saving_kwh']:.2f}", source)


class TestLongTextNotRenderedAsMetric(unittest.TestCase):
    def test_savings_verification_status_and_equipment_use_markdown(self):
        source = (PROJECT_ROOT / "ui" / "pages" / "18_Savings_Verification.py").read_text(encoding="utf-8")
        self.assertIn('st.markdown(f"**Status**', source)
        self.assertIn('st.markdown(f"**Equipment**', source)
        self.assertNotIn('st.metric("Status", selected["engineer_status"])', source)

    def test_anomalies_threshold_provenance_uses_markdown(self):
        source = (PROJECT_ROOT / "ui" / "pages" / "16_Anomalies.py").read_text(encoding="utf-8")
        self.assertIn('st.markdown(f"**Threshold provenance**', source)
        self.assertNotIn('st.metric("Threshold provenance"', source)


if __name__ == "__main__":
    unittest.main()
