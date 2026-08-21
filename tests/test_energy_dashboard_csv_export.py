import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

"""
Phase V1.5 - Minimal Reporting. build_main_energy_dataframe() and
build_equipment_power_breakdown_dataframe() in
ui/pages/15_Energy_Dashboard.py are pure functions of already-computed
KPI results (the same dicts the on-screen st.metric cards already use)
- they call no data-access function themselves, so testing them
directly proves the CSV can't drift from what's on screen. Source is
exec'd in isolation (same technique as
tests/test_documentation_synthetic_marking.py and
tests/test_event_records_csv_export.py), since the page file's numeric
prefix isn't importable and the rest of the module executes real
Streamlit/DB calls at module scope.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENERGY_DASHBOARD_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "15_Energy_Dashboard.py")


def _load_export_functions():
    source = Path(ENERGY_DASHBOARD_PAGE).read_text(encoding="utf-8")
    start = source.index("def _result_row")
    end = source.index("\ndef _data_and_assumptions_expander")
    namespace = {"pd": pd, "datetime": datetime}
    exec(source[start:end], namespace)
    return namespace["build_main_energy_dataframe"], namespace["build_equipment_power_breakdown_dataframe"]


def _available(value, unit="kWh"):
    return {"classification": "COMPUTED", "value": value, "unit": unit}


def _unavailable():
    return {"classification": "UNAVAILABLE", "value": None, "missing_inputs": ["no data"]}


class BuildMainEnergyDataframeTests(unittest.TestCase):
    def setUp(self):
        self.build_main_energy_dataframe, _ = _load_export_functions()
        self.period_start = datetime(2026, 8, 20, 0, 0, 0)
        self.period_end = datetime(2026, 8, 21, 0, 0, 0)

        self.period_kpis = {
            "energy": _available(123.456, "kWh"),
            "cost": _available(45.6, "MYR"),
            "estimated_demand_charge": _available(10.0, "MYR"),
            "estimated_maximum_demand": _available(50.0, "kW"),
            "production_energy_kwh": _available(80.0, "kWh"),
            "non_production_energy_kwh": _available(43.456, "kWh"),
            "average_production_period_demand_kw": _available(20.0, "kW"),
            "estimated_base_load": _available(5.0, "kW"),
            "after_hours": _unavailable(),
        }
        self.month_kpis = {
            "energy_month": _available(1000.0, "kWh"),
            "cost_month": _available(400.0, "MYR"),
            "projected_month_cost": _available(1200.0, "MYR"),
        }

    def test_column_names_are_clear_with_units_and_timestamps(self):
        df = self.build_main_energy_dataframe(
            "p01", self.period_start, self.period_end, self.period_kpis, self.month_kpis, None, []
        )
        expected_columns = {"Plant", "Period Start", "Period End", "Metric", "Value", "Unit", "Classification", "Notes", "Generated At"}
        self.assertTrue(expected_columns.issubset(set(df.columns)))

    def test_plant_and_period_are_stamped_on_every_row(self):
        df = self.build_main_energy_dataframe(
            "p01", self.period_start, self.period_end, self.period_kpis, self.month_kpis, None, []
        )
        self.assertTrue((df["Plant"] == "P01").all())
        self.assertTrue((df["Period Start"] == "2026-08-20 00:00:00").all())
        self.assertTrue((df["Period End"] == "2026-08-21 00:00:00").all())

    def test_available_metric_carries_its_value_and_unit(self):
        df = self.build_main_energy_dataframe(
            "p01", self.period_start, self.period_end, self.period_kpis, self.month_kpis, None, []
        )
        row = df[df["Metric"] == "Energy (selected period)"].iloc[0]
        self.assertEqual(row["Value"], 123.456)
        self.assertEqual(row["Unit"], "kWh")

    def test_unavailable_metric_is_labeled_unavailable_not_blank_or_zero(self):
        df = self.build_main_energy_dataframe(
            "p01", self.period_start, self.period_end, self.period_kpis, self.month_kpis, None, []
        )
        row = df[df["Metric"] == "After-hours Energy"].iloc[0]
        self.assertEqual(row["Value"], "Unavailable")

    def test_billing_peak_none_is_reported_as_unavailable(self):
        df = self.build_main_energy_dataframe(
            "p01", self.period_start, self.period_end, self.period_kpis, self.month_kpis, None, []
        )
        row = df[df["Metric"] == "Billing-Month Peak So Far"].iloc[0]
        self.assertEqual(row["Value"], "Unavailable")

    def test_billing_peak_present_carries_value_unit_and_occurred_at_in_notes(self):
        billing_peak = {"max_demand_kw": 77.5, "occurred_at": "2026-08-19 15:30:00"}
        df = self.build_main_energy_dataframe(
            "p01", self.period_start, self.period_end, self.period_kpis, self.month_kpis, billing_peak, []
        )
        row = df[df["Metric"] == "Billing-Month Peak So Far"].iloc[0]
        self.assertEqual(row["Value"], 77.5)
        self.assertEqual(row["Unit"], "kW")
        self.assertIn("2026-08-19 15:30:00", row["Notes"])

    def test_production_normalized_cards_are_included_when_present(self):
        normalized_cards = [("kWh/batch", _available(2.5, "kWh/batch"))]
        df = self.build_main_energy_dataframe(
            "p01", self.period_start, self.period_end, self.period_kpis, self.month_kpis, None, normalized_cards
        )
        self.assertIn("kWh/batch", df["Metric"].values)

    def test_different_plant_code_is_reflected_in_export(self):
        df = self.build_main_energy_dataframe(
            "p02", self.period_start, self.period_end, self.period_kpis, self.month_kpis, None, []
        )
        self.assertTrue((df["Plant"] == "P02").all())


class BuildEquipmentPowerBreakdownDataframeTests(unittest.TestCase):
    def setUp(self):
        _, self.build_breakdown_dataframe = _load_export_functions()

    def test_plant_column_and_original_columns_preserved(self):
        breakdown_df = pd.DataFrame(
            [{"Equipment": "Compressor 1", "Power (kW)": 12.3, "Power Share (%)": 40.0}]
        )
        df = self.build_breakdown_dataframe("p01", breakdown_df)
        self.assertIn("Plant", df.columns)
        self.assertIn("Generated At", df.columns)
        self.assertEqual(df.iloc[0]["Plant"], "P01")
        self.assertEqual(df.iloc[0]["Equipment"], "Compressor 1")
        self.assertEqual(df.iloc[0]["Power (kW)"], 12.3)

    def test_original_dataframe_is_not_mutated(self):
        breakdown_df = pd.DataFrame([{"Equipment": "Pump 1", "Power (kW)": 5.0, "Power Share (%)": 10.0}])
        self.build_breakdown_dataframe("p01", breakdown_df)
        self.assertNotIn("Plant", breakdown_df.columns)


class EnergyDashboardPageDownloadButtonTests(unittest.TestCase):
    def test_page_renders_a_main_energy_download_button_without_exception(self):
        at = AppTest.from_file(ENERGY_DASHBOARD_PAGE, default_timeout=60)
        at.run()
        self.assertFalse(bool(at.exception))
        labels = [b.label for b in at.download_button]
        self.assertIn("⬇️ Download Main Energy CSV", labels)

    def test_page_also_renders_main_energy_and_breakdown_pdf_buttons(self):
        at = AppTest.from_file(ENERGY_DASHBOARD_PAGE, default_timeout=60)
        at.run()
        self.assertFalse(bool(at.exception))
        labels = [b.label for b in at.download_button]
        self.assertIn("⬇️ Download Main Energy PDF", labels)
        self.assertIn("⬇️ Download Equipment Power Breakdown PDF", labels)


class MainEnergyPdfTableShapeTests(unittest.TestCase):
    """
    Phase V2.6 - the Main Energy PDF pulls Plant/Period Start/Period
    End/Generated At out of main_energy_df into the report's header
    parameters (shown once) rather than repeating them on every printed
    row, unlike the CSV export's flat shape. Same underlying values,
    just restructured for a readable printed table - verified here by
    reproducing the page's own drop-columns logic against a real
    build_main_energy_dataframe() result.
    """

    def setUp(self):
        self.build_main_energy_dataframe, _ = _load_export_functions()
        self.period_kpis = {
            "energy": _available(123.456, "kWh"),
            "cost": _available(45.6, "MYR"),
            "estimated_demand_charge": _available(10.0, "MYR"),
            "estimated_maximum_demand": _available(50.0, "kW"),
            "production_energy_kwh": _available(80.0, "kWh"),
            "non_production_energy_kwh": _available(43.456, "kWh"),
            "average_production_period_demand_kw": _available(20.0, "kW"),
            "estimated_base_load": _available(5.0, "kW"),
            "after_hours": _unavailable(),
        }
        self.month_kpis = {
            "energy_month": _available(1000.0, "kWh"),
            "cost_month": _available(400.0, "MYR"),
            "projected_month_cost": _available(1200.0, "MYR"),
        }

    def test_header_metadata_columns_are_droppable_leaving_only_metric_columns(self):
        df = self.build_main_energy_dataframe(
            "p01", datetime(2026, 8, 20), datetime(2026, 8, 21), self.period_kpis, self.month_kpis, None, []
        )
        table_df = df.drop(columns=["Plant", "Period Start", "Period End", "Generated At"])
        self.assertEqual(set(table_df.columns), {"Metric", "Value", "Unit", "Classification", "Notes"})

    def test_dropping_header_columns_does_not_lose_any_rows(self):
        df = self.build_main_energy_dataframe(
            "p01", datetime(2026, 8, 20), datetime(2026, 8, 21), self.period_kpis, self.month_kpis, None, []
        )
        table_df = df.drop(columns=["Plant", "Period Start", "Period End", "Generated At"])
        self.assertEqual(len(table_df), len(df))


if __name__ == "__main__":
    unittest.main()
