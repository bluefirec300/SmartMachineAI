from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_active_environment
from ui import energy_dashboard_data as d
from ui.csv_export import dataframe_to_csv_bytes, export_filename

"""
Phase 7 - Energy Dashboard & Plant Comparison. Presentation layer only:
every number on this page comes from ui.energy_dashboard_data, which
itself only ever calls engine.energy_kpi_engine (Phase 6) - see that
module's docstring. This page adds no KPI arithmetic of its own beyond
one explicitly-approved exception (equipment "Power Share" - a simple
percentage of an already-fetched total, not a new formula).
"""

st.title("⚡ Energy Dashboard")


# ---------------------------------------------------------------------------
# Formatting / rendering helpers
# ---------------------------------------------------------------------------

def _help_text(result: dict) -> str:
    if result["classification"] == "UNAVAILABLE":
        reason = "; ".join(result.get("missing_inputs") or []) or "no data available"
        return f"Unavailable - {reason}"

    lines = [f"{result['classification']} - {result.get('calculation_type') or '-'}"]
    if result.get("source_tags"):
        lines.append("Source: " + ", ".join(result["source_tags"]))
    if result.get("assumptions"):
        lines.append("; ".join(result["assumptions"]))
    return "\n".join(lines)


def _format_number(result: dict, decimals: int = 2) -> str:
    value = result.get("value")
    if result["classification"] == "UNAVAILABLE" or value is None:
        return "Unavailable"
    unit = result.get("unit") or ""
    return f"{value:,.{decimals}f} {unit}".strip()


def _format_cost(result: dict, currency_fallback: str = "") -> str:
    value = result.get("value")
    if result["classification"] == "UNAVAILABLE" or value is None:
        return "Unavailable"
    currency = result.get("unit") or currency_fallback or ""
    return f"{currency} {value:,.2f}".strip()


def kpi_card(column, label: str, result: dict, formatter=_format_number, **formatter_kwargs) -> None:
    with column:
        text = "Unavailable" if result["classification"] == "UNAVAILABLE" else formatter(result, **formatter_kwargs)
        st.metric(label, text, help=_help_text(result))


def _result_row(label: str, result: dict, notes: str = "") -> dict:
    if result["classification"] == "UNAVAILABLE" or result.get("value") is None:
        return {"Metric": label, "Value": "Unavailable", "Unit": "", "Classification": result["classification"], "Notes": notes}
    return {
        "Metric": label,
        "Value": result["value"],
        "Unit": result.get("unit") or "",
        "Classification": result["classification"],
        "Notes": notes,
    }


def build_main_energy_dataframe(
    plant_code: str,
    period_start: datetime,
    period_end: datetime,
    period_kpis: dict,
    month_kpis: dict,
    billing_peak: dict | None,
    normalized_cards: list[tuple[str, dict]],
) -> pd.DataFrame:
    """
    The same Main Energy numbers shown as KPI cards above, reshaped
    into a flat Metric/Value/Unit table for CSV export (Phase V1.5).
    Pure function of already-computed results - calls no data-access
    function itself, so it can't drift from what's on screen.
    """
    rows = [
        _result_row("Energy (selected period)", period_kpis["energy"]),
        _result_row("Cost (selected period)", period_kpis["cost"]),
        _result_row("Energy This Month", month_kpis["energy_month"]),
        _result_row("Cost This Month", month_kpis["cost_month"]),
        _result_row("Projected Month Cost", month_kpis["projected_month_cost"]),
        _result_row("Estimated Demand Charge", period_kpis["estimated_demand_charge"]),
        _result_row("Estimated Maximum Demand - Selected Period", period_kpis["estimated_maximum_demand"]),
    ]
    if billing_peak is None:
        rows.append(
            {"Metric": "Billing-Month Peak So Far", "Value": "Unavailable", "Unit": "", "Classification": "UNAVAILABLE", "Notes": ""}
        )
    else:
        rows.append(
            {
                "Metric": "Billing-Month Peak So Far",
                "Value": billing_peak["max_demand_kw"],
                "Unit": "kW",
                "Classification": "RECORDED",
                "Notes": f"Recorded {billing_peak['occurred_at']}",
            }
        )
    rows += [
        _result_row("Production Energy", period_kpis["production_energy_kwh"]),
        _result_row("Non-production Energy", period_kpis["non_production_energy_kwh"]),
        _result_row("Average Production-Period Demand", period_kpis["average_production_period_demand_kw"]),
        _result_row("Estimated Base Load", period_kpis["estimated_base_load"]),
        _result_row("After-hours Energy", period_kpis["after_hours"]),
    ]
    for normalized_label, normalized_result in normalized_cards:
        rows.append(_result_row(normalized_label, normalized_result))

    df = pd.DataFrame(rows)
    df.insert(0, "Plant", plant_code.upper())
    df.insert(1, "Period Start", period_start.strftime("%Y-%m-%d %H:%M:%S"))
    df.insert(2, "Period End", period_end.strftime("%Y-%m-%d %H:%M:%S"))
    df["Generated At"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return df


def build_equipment_power_breakdown_dataframe(plant_code: str, breakdown_df: pd.DataFrame) -> pd.DataFrame:
    """The on-screen equipment power breakdown table, reshaped for CSV export (Phase V1.5)."""
    df = breakdown_df.copy()
    df.insert(0, "Plant", plant_code.upper())
    df["Generated At"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return df


def _data_and_assumptions_expander(title: str, results: dict[str, dict]) -> None:
    with st.expander(f"Data & assumptions - {title}"):
        for key, result in results.items():
            if not isinstance(result, dict) or "classification" not in result:
                continue
            st.markdown(f"**{key}**: {result['classification']} / {result.get('calculation_type') or '-'}")
            if result.get("source_tags"):
                st.caption("Source tags: " + ", ".join(result["source_tags"]))
            if result.get("assumptions"):
                for assumption in result["assumptions"]:
                    st.caption(f"- {assumption}")
            if result["classification"] == "UNAVAILABLE" and result.get("missing_inputs"):
                st.caption("Missing: " + "; ".join(result["missing_inputs"]))


# ---------------------------------------------------------------------------
# Plant + time range selection
# ---------------------------------------------------------------------------

plants = d.list_plants()
if not plants:
    st.error("No plants configured yet - see Factory Configuration.")
    st.stop()

selector_cols = st.columns([1, 3])
with selector_cols[0]:
    plant_options = {p["code"].upper(): p["code"] for p in plants}
    plant_label = st.selectbox("Plant", list(plant_options), key="energy_dashboard_plant")
    plant_code = plant_options[plant_label]

with selector_cols[1]:
    range_choice = st.radio(
        "Range", ["Today", "Yesterday", "7 Days", "30 Days", "Custom"],
        horizontal=True, key="energy_dashboard_range",
    )

now = datetime.now()
today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

if range_choice == "Today":
    period_start, period_end = today_start, now
elif range_choice == "Yesterday":
    period_start, period_end = today_start - timedelta(days=1), today_start
elif range_choice == "7 Days":
    period_start, period_end = today_start - timedelta(days=6), now
elif range_choice == "30 Days":
    period_start, period_end = today_start - timedelta(days=29), now
else:
    custom_cols = st.columns(2)
    with custom_cols[0]:
        custom_start_date = st.date_input("From", value=(today_start - timedelta(days=7)).date(), key="energy_dashboard_custom_start")
    with custom_cols[1]:
        custom_end_date = st.date_input("To", value=today_start.date(), key="energy_dashboard_custom_end")
    period_start = datetime.combine(custom_start_date, datetime.min.time())
    period_end = min(datetime.combine(custom_end_date, datetime.min.time()) + timedelta(days=1), now)

# --- Provenance caption - unobtrusive, shown once, never repeated ----------
_provenance_parts = []
if get_active_environment() == "simulation":
    _provenance_parts.append("Simulation environment")
_tariff_provenance = d.get_active_tariff_provenance(plant_code)
if _tariff_provenance and _tariff_provenance["is_simulated"]:
    _rate = _tariff_provenance.get("energy_rate")
    _currency = _tariff_provenance.get("currency") or "?"
    _rate_text = f" ({_currency} {_rate:.2f}/kWh placeholder)" if _rate is not None else ""
    _provenance_parts.append(f"Tariff: Simulated{_rate_text}")
if _provenance_parts:
    st.caption("🔧 " + " · ".join(_provenance_parts))

st.divider()


# ---------------------------------------------------------------------------
# Current Demand - own fragment, short-TTL auto-refresh, isolated so the
# rest of the page never re-renders just to keep this one number fresh.
# ---------------------------------------------------------------------------

auto_refresh_demand = st.checkbox("Auto-refresh Current Demand", value=True, key="energy_dashboard_auto_refresh")


@st.fragment(run_every=d.CURRENT_TTL if auto_refresh_demand else None)
def _current_demand_fragment(plant_code: str) -> None:
    result = d.get_current_demand(plant_code)
    st.metric("Current Demand", _format_number(result), help=_help_text(result))


_current_demand_fragment(plant_code)


# ---------------------------------------------------------------------------
# Main Energy dashboard
# ---------------------------------------------------------------------------

st.subheader("Main Energy")

period_kpis = d.get_period_kpis(plant_code, period_start, period_end, now)
month_kpis = d.get_month_to_date_kpis(plant_code)
billing_peak = d.get_billing_month_peak(plant_code)

row1 = st.columns(3)
kpi_card(row1[0], "Energy (selected period)", period_kpis["energy"])
kpi_card(row1[1], "Cost (selected period)", period_kpis["cost"], formatter=_format_cost)
kpi_card(row1[2], "Energy This Month", month_kpis["energy_month"])

row2 = st.columns(3)
kpi_card(row2[0], "Cost This Month", month_kpis["cost_month"], formatter=_format_cost)
kpi_card(row2[1], "Projected Month Cost", month_kpis["projected_month_cost"], formatter=_format_cost)
kpi_card(row2[2], "Estimated Demand Charge", period_kpis["estimated_demand_charge"], formatter=_format_cost)

row3 = st.columns(3)
kpi_card(row3[0], "Estimated Maximum Demand - Selected Period", period_kpis["estimated_maximum_demand"])
if billing_peak is None:
    row3[1].metric(
        "Billing-Month Peak So Far", "Unavailable",
        help="Unavailable - energy_kpi_worker.service has not yet recorded a peak for this billing period",
    )
else:
    row3[1].metric(
        "Billing-Month Peak So Far", f"{billing_peak['max_demand_kw']:,.2f} kW",
        help=f"Recorded {billing_peak['occurred_at']} - a persisted running record for the current calendar-month "
             "billing period, DISTINCT from 'Estimated Maximum Demand - Selected Period' above (which reflects "
             "whatever range/plant you've currently selected).",
    )
row3[2].empty()

row4 = st.columns(3)
kpi_card(row4[0], "Production Energy", period_kpis["production_energy_kwh"])
kpi_card(row4[1], "Non-production Energy", period_kpis["non_production_energy_kwh"])
kpi_card(row4[2], "Average Production-Period Demand", period_kpis["average_production_period_demand_kw"])

row5 = st.columns(3)
kpi_card(row5[0], "Estimated Base Load", period_kpis["estimated_base_load"])
kpi_card(row5[1], "After-hours Energy", period_kpis["after_hours"])
row5[2].empty()

# --- Production-normalized metrics - only units actually produced ----------
intensity = period_kpis["production_intensity"]
normalized_cards = []
if "kwh_per_batch" in intensity and intensity["kwh_per_batch"]["classification"] != "UNAVAILABLE":
    normalized_cards.append(("kWh/batch", intensity["kwh_per_batch"]))
if "kwh_per_kg" in intensity and intensity["kwh_per_kg"]["classification"] != "UNAVAILABLE":
    normalized_cards.append(("kWh/kg", intensity["kwh_per_kg"]))
if "kwh_per_tonne" in intensity and intensity["kwh_per_tonne"]["classification"] != "UNAVAILABLE":
    normalized_cards.append(("kWh/tonne", intensity["kwh_per_tonne"]))
if "kwh_per_litre" in intensity and intensity["kwh_per_litre"]["classification"] != "UNAVAILABLE":
    normalized_cards.append(("kWh/litre", intensity["kwh_per_litre"]))

if normalized_cards:
    st.markdown("**Production-normalized (units actually produced this period only)**")
    cols = st.columns(len(normalized_cards))
    for col, (label, result) in zip(cols, normalized_cards):
        kpi_card(col, label, result, decimals=2)
else:
    st.caption("No completed production batches in the selected period - production-normalized metrics unavailable.")

# --- Trend chart ---
st.markdown("**Demand trend**")
if range_choice in ("Today", "Yesterday") or (period_end - period_start) <= timedelta(days=2):
    trend = d.get_power_trend(plant_code, period_start.strftime("%Y-%m-%d %H:%M:%S"), period_end.strftime("%Y-%m-%d %H:%M:%S"))
    if trend:
        import pandas as pd
        df = pd.DataFrame(trend)
        df["time"] = pd.to_datetime(df["time"])
        st.line_chart(df.set_index("time")["power_kw"], height=220)
    else:
        st.caption("No demand data in the selected period.")
else:
    daily = d.get_daily_summary_series(plant_code, period_start.strftime("%Y-%m-%d"), period_end.strftime("%Y-%m-%d"))
    if daily:
        import pandas as pd
        df = pd.DataFrame(daily)
        st.bar_chart(df.set_index("summary_date")[["energy_kwh"]], height=220)
        st.caption(f"{len(daily)} day(s) of finalized history available (energy_kpi_worker.service builds this daily).")
    else:
        st.caption("No finalized daily summaries yet in this range - energy_kpi_worker.service may not have run long enough, or hasn't been installed.")

# --- Equipment power breakdown ---
st.markdown("**Equipment power breakdown (current snapshot - Power Share, not accumulated energy)**")
breakdown = d.get_equipment_power_breakdown(plant_code)
if breakdown:
    breakdown_df = pd.DataFrame(breakdown)[["label", "power_kw", "power_share_pct"]]
    breakdown_df.columns = ["Equipment", "Power (kW)", "Power Share (%)"]
    st.dataframe(breakdown_df, hide_index=True, width="stretch")

    export_df = build_equipment_power_breakdown_dataframe(plant_code, breakdown_df)
    st.download_button(
        "⬇️ Download Equipment Power Breakdown CSV",
        data=dataframe_to_csv_bytes(export_df),
        file_name=export_filename(f"equipment_power_breakdown_{plant_code}"),
        mime="text/csv",
        help="Exports the live power snapshot above - this is a current reading, not tied to the selected date range.",
        key="download_equipment_power_breakdown_csv",
    )
else:
    st.caption("No live power readings available for a breakdown right now.")

_data_and_assumptions_expander("Main Energy", {**period_kpis, **month_kpis, **{"production_intensity_" + k2: v2 for k2, v2 in intensity.items()}})

# --- CSV export - Main Energy summary for the currently selected plant/period ---
main_energy_df = build_main_energy_dataframe(
    plant_code, period_start, period_end, period_kpis, month_kpis, billing_peak, normalized_cards
)

st.download_button(
    "⬇️ Download Main Energy CSV",
    data=dataframe_to_csv_bytes(main_energy_df),
    file_name=export_filename(f"energy_dashboard_{plant_code}"),
    mime="text/csv",
    help="Exports the Main Energy summary above for the currently selected plant and date range.",
)

st.divider()


# ---------------------------------------------------------------------------
# Utility Performance
# ---------------------------------------------------------------------------

st.subheader("Utility Performance")
tab_air, tab_chiller, tab_pumps, tab_water = st.tabs(["Compressed Air", "Chilled Water", "Pumps", "Water Treatment"])

with tab_air:
    air = d.get_compressed_air_kpis(plant_code, period_start, period_end, now)
    cols = st.columns(3)
    kpi_card(cols[0], "Total Compressor Energy", air["total_compressor_energy"])
    kpi_card(cols[1], "Header Air Volume", air["header_air_volume"])
    kpi_card(cols[2], "Header Specific Energy", air["header_specific_energy"], decimals=2)

    cols2 = st.columns(2)
    kpi_card(cols2[0], "Cost/Nm3 (header-level)", air["cost_per_nm3"], formatter=_format_cost)
    kpi_card(cols2[1], "Night/Base Air Consumption", air["night_base_air_consumption"])

    st.markdown("**Per-compressor loaded/unloaded**")
    per_compressor_rows = []
    for instance, stats in air["per_compressor"].items():
        per_compressor_rows.append({
            "Compressor": instance,
            "Loaded %": _format_number(stats["loaded_pct"], 1) if stats["loaded_pct"]["classification"] != "UNAVAILABLE" else "Unavailable",
            "Unloaded %": _format_number(stats["unloaded_pct"], 1) if stats["unloaded_pct"]["classification"] != "UNAVAILABLE" else "Unavailable",
            "Specific Energy": _format_number(stats["specific_energy_kwh_per_hour"], 2) if stats["specific_energy_kwh_per_hour"]["classification"] != "UNAVAILABLE" else "Unavailable",
        })
    import pandas as pd
    st.dataframe(pd.DataFrame(per_compressor_rows), hide_index=True, width="stretch")

    _data_and_assumptions_expander("Compressed Air", air)

with tab_chiller:
    chillers = d.get_chilled_water_kpis(plant_code)
    for instance, result in chillers.items():
        st.markdown(f"**{instance}**")
        cols = st.columns(6)
        kpi_card(cols[0], "Power", result["power_kw"])
        kpi_card(cols[1], "Cooling Output", result["cooling_output_kw"])
        kpi_card(cols[2], "COP", result["cop"])
        kpi_card(cols[3], "Supply Temp", result["supply_temp_c"])
        kpi_card(cols[4], "Return Temp / ΔT", result["delta_t_c"])
        kpi_card(cols[5], "Water Flow", result["water_flow_m3h"])
    _data_and_assumptions_expander("Chilled Water", {f"{inst}.{k2}": v2 for inst, res in chillers.items() for k2, v2 in res.items()})

with tab_pumps:
    pump_snapshot = d.get_pump_snapshot_kpis(plant_code)
    pump_energy = d.get_pump_specific_energy(plant_code, period_start, period_end, now)
    for instance, result in pump_snapshot.items():
        st.markdown(f"**{instance}**")
        cols = st.columns(6)
        kpi_card(cols[0], "Power", result["power_kw"])
        kpi_card(cols[1], "Flow", result["flow_m3h"])
        kpi_card(cols[2], "ΔP", result["delta_p_bar"], decimals=3)
        kpi_card(cols[3], "Flow/kW", result["flow_per_kw"], decimals=3)
        kpi_card(cols[4], "kWh/m3 (period)", pump_energy.get(instance, {"classification": "UNAVAILABLE", "missing_inputs": ["not computed"]}), decimals=2)
        kpi_card(cols[5], "Hydraulic Efficiency", result["efficiency_pct"])
    st.caption(
        "Hydraulic efficiency values are shown as calculated, including where they read unrealistically low - "
        "see the tooltip on each Efficiency card for the simulation-fidelity note (Phase 5 doesn't yet couple "
        "pump pressure/flow/power to each other). Not adjusted here."
    )
    _data_and_assumptions_expander(
        "Pumps",
        {**{f"{inst}.{k2}": v2 for inst, res in pump_snapshot.items() for k2, v2 in res.items()},
         **{f"{inst}.specific_energy": v2 for inst, v2 in pump_energy.items()}},
    )

with tab_water:
    water = d.get_water_treatment_kpis(plant_code, period_start, period_end, now)
    cols = st.columns(2)
    kpi_card(cols[0], "RO/DI (RO01) kWh/m3", water["RO01"], decimals=2)
    kpi_card(cols[1], "Water Treatment (SYS01) kWh/m3", water["SYS01"], decimals=2)
    _data_and_assumptions_expander("Water Treatment", water)

st.divider()


# ---------------------------------------------------------------------------
# Plant Comparison - no ranking/verdict language, ever.
# ---------------------------------------------------------------------------

st.subheader("Plant Comparison")
st.caption("Figures only - drawing conclusions about which plant is 'better' is left to you.")

comparison_rows = []
per_plant_period = {}
per_plant_air = {}
per_plant_intensity = {}
for p in plants:
    code = p["code"]
    per_plant_period[code] = d.get_period_kpis(code, period_start, period_end, now)
    per_plant_air[code] = d.get_compressed_air_kpis(code, period_start, period_end, now)
    per_plant_intensity[code] = per_plant_period[code]["production_intensity"]

plant_codes = [p["code"] for p in plants]
plant_column_labels = [p["code"].upper() for p in plants]


def _comparison_row(label: str, extractor) -> dict:
    row = {"Metric": label}
    for code, col_label in zip(plant_codes, plant_column_labels):
        result = extractor(code)
        row[col_label] = "Unavailable" if not result or result["classification"] == "UNAVAILABLE" else _format_number(result)
    return row


comparison_rows.append(_comparison_row("Current Demand", lambda c: d.get_current_demand(c)))
comparison_rows.append(_comparison_row("Energy (selected period)", lambda c: per_plant_period[c]["energy"]))
comparison_rows.append(_comparison_row("Cost (selected period)", lambda c: per_plant_period[c]["cost"]))
comparison_rows.append(_comparison_row("Production Energy", lambda c: per_plant_period[c]["production_energy_kwh"]))
comparison_rows.append(_comparison_row("Non-production Energy", lambda c: per_plant_period[c]["non_production_energy_kwh"]))
comparison_rows.append(_comparison_row("Estimated Base Load", lambda c: per_plant_period[c]["estimated_base_load"]))
comparison_rows.append(_comparison_row("Estimated Maximum Demand", lambda c: per_plant_period[c]["estimated_maximum_demand"]))
comparison_rows.append(_comparison_row("Compressed Air kWh/Nm3 (header)", lambda c: per_plant_air[c]["header_specific_energy"]))
comparison_rows.append(_comparison_row(
    "kWh/kg - based on kg-unit production in selected period", lambda c: per_plant_intensity[c].get("kwh_per_kg"),
))
comparison_rows.append(_comparison_row(
    "kWh/litre - based on litre-unit production in selected period", lambda c: per_plant_intensity[c].get("kwh_per_litre"),
))

import pandas as pd
st.dataframe(pd.DataFrame(comparison_rows), hide_index=True, width="stretch")
