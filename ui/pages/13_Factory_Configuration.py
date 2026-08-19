from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.database import DatabaseManager
from engine.configuration_completeness import calculate_overall_completeness
from engine.energy_tariff import (
    SIMULATED_TARIFF_NOTICE,
    calculate_energy_cost,
    create_tariff,
    get_current_tariff,
    get_tariff_history,
)
from ui import auth
from ui.data_access import CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH
from ui.scada_floor_plan_data import _todays_accumulated_total
from ui.factory_config_data import (
    create_area,
    create_non_production_period,
    create_shift,
    create_system,
    delete_non_production_period,
    delete_shift,
    get_areas_for_plant,
    get_classified_equipment,
    get_factory,
    get_non_production_periods,
    get_plants,
    get_shifts,
    get_systems_for_area,
    update_area,
    update_equipment_metadata,
    update_factory,
    update_plant,
    update_system,
)


current_user = auth.current_user()
can_edit = auth.can_edit("admin")

st.title("🏭 Factory Configuration")
st.caption(
    "Factory/Plant/Area/System structure and equipment engineering metadata - "
    "the contextual information sensors alone can't provide. Viewable by every "
    "signed-in role; editing is Administrator-only."
)

if not can_edit:
    st.info("View-only - only Administrator accounts can edit factory/plant/engineering configuration.")


def _field_label(field: str) -> str:
    return field.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Configuration Completeness - deterministic, weighted, equipment-type-aware
# (engine/configuration_completeness.py). Computed live on every page load -
# no persisted/cached score, no LLM involved. This is the same calculator
# later phases (Data Health, AI confidence) will call directly.
# ---------------------------------------------------------------------------

completeness = calculate_overall_completeness(CONFIG_DATABASE_PATH)

st.subheader(f"Configuration Completeness: {completeness['overall_percent']}%")
st.progress(completeness["overall_percent"] / 100)

summary_cols = st.columns(3)
with summary_cols[0]:
    factory_pct = completeness["factory"]["percent"] if completeness["factory"] else 0
    st.metric("Factory profile", f"{factory_pct}%")
with summary_cols[1]:
    plant_pcts = [p["percent"] for p in completeness["plants"]]
    avg_plant = round(sum(plant_pcts) / len(plant_pcts)) if plant_pcts else 0
    st.metric("Plants (avg)", f"{avg_plant}%")
with summary_cols[2]:
    equipment_pcts = [e["percent"] for e in completeness["equipment"]]
    avg_equipment = round(sum(equipment_pcts) / len(equipment_pcts)) if equipment_pcts else 0
    st.metric("Equipment (avg)", f"{avg_equipment}%")

if completeness["top_missing"]:
    with st.expander(f"What's missing - top {len(completeness['top_missing'])} most important items", expanded=False):
        for item in completeness["top_missing"]:
            st.write(f"- **{item['label']}** ({item['scope']}): {_field_label(item['field'])} (weight {item['weight']})")
else:
    st.success("No configuration gaps found among the weighted fields tracked.")

st.divider()

tab_factory, tab_plants, tab_areas, tab_equipment, tab_shifts, tab_shutdowns, tab_tariff = st.tabs(
    [
        "Factory Profile",
        "Plants",
        "Areas & Systems",
        "Equipment Metadata",
        "Operating Schedules",
        "Shutdown / Maintenance Windows",
        "Energy Tariff",
    ]
)


# ---------------------------------------------------------------------------
# Factory Profile
# ---------------------------------------------------------------------------

with tab_factory:
    factory = get_factory()

    if factory is None:
        st.warning(
            "No factory record exists yet - run "
            "`python -m engine.factory_structure_migrator` first."
        )
    else:
        with st.form("factory_profile_form"):
            name = st.text_input("Factory name", value=factory["name"] or "", disabled=not can_edit)
            company = st.text_input("Company", value=factory["company"] or "", disabled=not can_edit)
            country = st.text_input("Country", value=factory["country"] or "", disabled=not can_edit)
            currency = st.text_input("Currency (e.g. MYR, USD)", value=factory["currency"] or "", disabled=not can_edit)
            timezone = st.text_input("Timezone (e.g. Asia/Kuala_Lumpur)", value=factory["timezone"] or "", disabled=not can_edit)
            factory_type = st.text_input("Factory type", value=factory["factory_type"] or "", disabled=not can_edit)
            floor_area = st.number_input(
                "Floor area (m²)", value=float(factory["floor_area_m2"]) if factory["floor_area_m2"] else 0.0,
                min_value=0.0, disabled=not can_edit,
            )

            if st.form_submit_button("Save factory profile", disabled=not can_edit):
                update_factory(
                    factory["id"], current_user["username"],
                    name=name or None, company=company or None, country=country or None,
                    currency=currency or None, timezone=timezone or None,
                    factory_type=factory_type or None,
                    floor_area_m2=floor_area or None,
                )
                st.success("Factory profile saved.")
                st.rerun()


# ---------------------------------------------------------------------------
# Plants
# ---------------------------------------------------------------------------

with tab_plants:
    for plant in get_plants():
        with st.container(border=True):
            st.markdown(f"**{plant['code'].upper()}**")

            with st.form(f"plant_form_{plant['id']}"):
                name = st.text_input("Plant name", value=plant["name"] or "", key=f"plant_name_{plant['id']}", disabled=not can_edit)
                description = st.text_area("Description", value=plant["description"] or "", key=f"plant_desc_{plant['id']}", disabled=not can_edit)
                floor_area = st.number_input(
                    "Floor area (m²)", value=float(plant["floor_area_m2"]) if plant["floor_area_m2"] else 0.0,
                    min_value=0.0, key=f"plant_area_{plant['id']}", disabled=not can_edit,
                )
                production_capacity = st.text_input(
                    "Production capacity", value=plant["production_capacity"] or "",
                    key=f"plant_cap_{plant['id']}", disabled=not can_edit,
                )
                active = st.checkbox("Active", value=bool(plant["active"]), key=f"plant_active_{plant['id']}", disabled=not can_edit)

                if st.form_submit_button("Save", disabled=not can_edit, key=f"plant_save_{plant['id']}"):
                    update_plant(
                        plant["id"], current_user["username"],
                        name=name or None, description=description or None,
                        floor_area_m2=floor_area or None, production_capacity=production_capacity or None,
                        active=1 if active else 0,
                    )
                    st.success(f"{plant['code'].upper()} saved.")
                    st.rerun()


# ---------------------------------------------------------------------------
# Areas & Systems
# ---------------------------------------------------------------------------

with tab_areas:
    st.caption(
        "Areas/Systems seeded from the current simulation's room layout are marked "
        "\"inferred\" - editing or adding one marks it \"confirmed\"."
    )

    plants = get_plants()
    plant_choice = st.selectbox(
        "Plant", plants, format_func=lambda p: p["code"].upper(), key="areas_plant_select",
    )

    if plant_choice:
        for area in get_areas_for_plant(plant_choice["id"]):
            badge = "🟢 confirmed" if area["source"] == "confirmed" else "⚪ inferred (simulation)"
            with st.expander(f"{area['name']} — {badge}"):
                with st.form(f"area_form_{area['id']}"):
                    area_name = st.text_input("Area name", value=area["name"], key=f"area_name_{area['id']}", disabled=not can_edit)
                    area_desc = st.text_area("Description", value=area["description"] or "", key=f"area_desc_{area['id']}", disabled=not can_edit)
                    if st.form_submit_button("Save area", disabled=not can_edit, key=f"area_save_{area['id']}"):
                        update_area(area["id"], area_name, area_desc or None, current_user["username"])
                        st.success("Area saved and marked confirmed.")
                        st.rerun()

                st.markdown("**Systems in this area**")
                for system in get_systems_for_area(area["id"]):
                    sys_badge = "🟢 confirmed" if system["source"] == "confirmed" else "⚪ inferred (simulation)"
                    with st.form(f"system_form_{system['id']}"):
                        st.caption(sys_badge)
                        system_name = st.text_input("System name", value=system["name"], key=f"sys_name_{system['id']}", disabled=not can_edit)
                        system_desc = st.text_area("Description", value=system["description"] or "", key=f"sys_desc_{system['id']}", disabled=not can_edit)
                        if st.form_submit_button("Save system", disabled=not can_edit, key=f"sys_save_{system['id']}"):
                            update_system(system["id"], system_name, system_desc or None, current_user["username"])
                            st.success("System saved and marked confirmed.")
                            st.rerun()

                if can_edit:
                    with st.form(f"new_system_form_{area['id']}"):
                        st.caption("Add a new system to this area")
                        new_system_name = st.text_input("New system name", key=f"new_sys_name_{area['id']}")
                        if st.form_submit_button("Add system", key=f"new_sys_add_{area['id']}"):
                            if new_system_name.strip():
                                create_system(area["id"], new_system_name.strip(), None, current_user["username"])
                                st.success(f"System '{new_system_name}' added.")
                                st.rerun()
                            else:
                                st.error("Enter a system name.")

        if can_edit:
            with st.form(f"new_area_form_{plant_choice['id']}"):
                st.caption(f"Add a new area to {plant_choice['code'].upper()}")
                new_area_name = st.text_input("New area name", key=f"new_area_name_{plant_choice['id']}")
                if st.form_submit_button("Add area", key=f"new_area_add_{plant_choice['id']}"):
                    if new_area_name.strip():
                        create_area(plant_choice["id"], new_area_name.strip(), None, current_user["username"])
                        st.success(f"Area '{new_area_name}' added.")
                        st.rerun()
                    else:
                        st.error("Enter an area name.")


# ---------------------------------------------------------------------------
# Equipment Engineering Metadata
# ---------------------------------------------------------------------------

with tab_equipment:
    plants = get_plants()
    plant_choice = st.selectbox(
        "Plant", plants, format_func=lambda p: p["code"].upper(), key="equipment_plant_select",
    )

    if plant_choice:
        equipment_list = get_classified_equipment(plant_choice["id"])
        equipment_options = {e["display_name"]: e for e in equipment_list}

        selected_name = st.selectbox("Equipment", sorted(equipment_options), key="equipment_metadata_select")
        equipment = equipment_options[selected_name]

        from engine.configuration_completeness import calculate_equipment_completeness

        eq_completeness = calculate_equipment_completeness(equipment)
        st.caption(
            f"{equipment['area_name'] or '-'} → {equipment['system_name'] or '-'} · "
            f"Asset ID {equipment['device_number'] or '-'} · "
            f"Completeness: **{eq_completeness['percent']}%**"
        )
        if eq_completeness["missing"]:
            missing_text = ", ".join(_field_label(f) for f, _ in eq_completeness["missing"][:6])
            st.caption(f"Most important missing: {missing_text}")

        with st.form(f"equipment_metadata_form_{equipment['id']}"):
            col1, col2 = st.columns(2)
            with col1:
                equipment_type = st.text_input("Equipment type", value=equipment["equipment_type"] or "", disabled=not can_edit)
                brand = st.text_input("Manufacturer", value=equipment["brand"] or "", disabled=not can_edit)
                model = st.text_input("Model", value=equipment["model"] or "", disabled=not can_edit)
                serial_number = st.text_input("Serial number", value=equipment["serial_number"] or "", disabled=not can_edit)
                installation_date = st.text_input(
                    "Installation date (YYYY-MM-DD)", value=equipment["installation_date"] or "", disabled=not can_edit,
                )
                commission_date = st.text_input(
                    "Commission date (YYYY-MM-DD)", value=equipment["commission_date"] or "", disabled=not can_edit,
                )
                criticality = st.selectbox(
                    "Criticality", ["", "Low", "Medium", "High", "Critical"],
                    index=(["", "Low", "Medium", "High", "Critical"].index(equipment["criticality"])
                           if equipment["criticality"] in ["", "Low", "Medium", "High", "Critical"] else 0),
                    disabled=not can_edit,
                )
            with col2:
                rated_power = st.number_input("Rated power (kW)", value=float(equipment["rated_power"] or 0), min_value=0.0, disabled=not can_edit)
                rated_voltage = st.number_input("Rated voltage (V)", value=float(equipment["rated_voltage"] or 0), min_value=0.0, disabled=not can_edit)
                rated_current = st.number_input("Rated current (A)", value=float(equipment["rated_current"] or 0), min_value=0.0, disabled=not can_edit)
                rated_flow = st.number_input("Rated flow", value=float(equipment["rated_flow"] or 0), min_value=0.0, disabled=not can_edit)
                rated_pressure = st.number_input("Rated pressure", value=float(equipment["rated_pressure"] or 0), min_value=0.0, disabled=not can_edit)
                rated_capacity = st.number_input("Rated capacity", value=float(equipment["rated_capacity"] or 0), min_value=0.0, disabled=not can_edit)
                replacement_cost = st.number_input("Replacement cost", value=float(equipment["replacement_cost"] or 0), min_value=0.0, disabled=not can_edit)
                expected_life_years = st.number_input("Expected life (years)", value=float(equipment["expected_life_years"] or 0), min_value=0.0, disabled=not can_edit)
                normal_operating_hours = st.number_input("Normal operating hours/day", value=float(equipment["normal_operating_hours"] or 0), min_value=0.0, max_value=24.0, disabled=not can_edit)

            operating_pattern = st.selectbox(
                "Operating pattern",
                ["", "24_7", "shift_based", "seasonal", "on_demand"],
                index=(["", "24_7", "shift_based", "seasonal", "on_demand"].index(equipment["operating_pattern"])
                       if equipment["operating_pattern"] in ["", "24_7", "shift_based", "seasonal", "on_demand"] else 0),
                format_func=lambda v: {"": "(not set)", "24_7": "Runs 24/7", "shift_based": "Follows plant shift schedule",
                                        "seasonal": "Seasonal", "on_demand": "On demand"}[v],
                disabled=not can_edit,
            )

            if st.form_submit_button("Save equipment metadata", disabled=not can_edit):
                update_equipment_metadata(
                    equipment["id"], current_user["username"],
                    equipment_type=equipment_type or None, brand=brand or None, model=model or None,
                    serial_number=serial_number or None, installation_date=installation_date or None,
                    commission_date=commission_date or None, criticality=criticality or None,
                    rated_power=rated_power or None, rated_voltage=rated_voltage or None,
                    rated_current=rated_current or None, rated_flow=rated_flow or None,
                    rated_pressure=rated_pressure or None, rated_capacity=rated_capacity or None,
                    replacement_cost=replacement_cost or None, expected_life_years=expected_life_years or None,
                    normal_operating_hours=normal_operating_hours or None,
                    operating_pattern=operating_pattern or None,
                )
                st.success(f"{selected_name} metadata saved.")
                st.rerun()


# ---------------------------------------------------------------------------
# Operating Schedules (shifts)
# ---------------------------------------------------------------------------

with tab_shifts:
    st.caption("Shifts can be factory-wide defaults or specific to one plant.")

    plants = get_plants()
    scope_options = {"Factory-wide default": None, **{p["code"].upper(): p["id"] for p in plants}}
    scope_choice = st.selectbox("Scope", list(scope_options), key="shift_scope_select")
    scope_plant_id = scope_options[scope_choice]

    for shift in get_shifts(scope_plant_id):
        cols = st.columns([3, 2, 2, 3, 1])
        cols[0].write(f"**{shift['name']}**")
        cols[1].write(shift["start_time"])
        cols[2].write(shift["end_time"])
        cols[3].write(shift["days_of_week"])
        if can_edit and cols[4].button("🗑", key=f"delete_shift_{shift['id']}"):
            delete_shift(shift["id"], current_user["username"])
            st.rerun()

    if can_edit:
        with st.form(f"new_shift_form_{scope_choice}"):
            st.caption(f"Add a shift for: {scope_choice}")
            shift_name = st.text_input("Shift name (e.g. Day Shift)")
            col1, col2 = st.columns(2)
            with col1:
                start_time = st.text_input("Start time (HH:MM)", value="08:00")
            with col2:
                end_time = st.text_input("End time (HH:MM)", value="16:00")
            days = st.multiselect(
                "Working days", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                default=["Mon", "Tue", "Wed", "Thu", "Fri"],
            )
            if st.form_submit_button("Add shift"):
                if shift_name.strip() and days:
                    create_shift(scope_plant_id, shift_name.strip(), start_time, end_time, ",".join(days), current_user["username"])
                    st.success(f"Shift '{shift_name}' added.")
                    st.rerun()
                else:
                    st.error("Enter a shift name and at least one working day.")


# ---------------------------------------------------------------------------
# Shutdown / Maintenance Windows
# ---------------------------------------------------------------------------

with tab_shutdowns:
    plants = get_plants()
    scope_options = {"Factory-wide": None, **{p["code"].upper(): p["id"] for p in plants}}
    scope_choice = st.selectbox("Scope", list(scope_options), key="shutdown_scope_select")
    scope_plant_id = scope_options[scope_choice]

    for period in get_non_production_periods(scope_plant_id):
        cols = st.columns([2, 3, 3, 3, 1])
        cols[0].write(f"**{period['period_type']}**")
        cols[1].write(period["start_datetime"])
        cols[2].write(period["end_datetime"])
        cols[3].write(period["description"] or "")
        if can_edit and cols[4].button("🗑", key=f"delete_period_{period['id']}"):
            delete_non_production_period(period["id"], current_user["username"])
            st.rerun()

    if can_edit:
        with st.form(f"new_period_form_{scope_choice}"):
            st.caption(f"Add a shutdown/maintenance window for: {scope_choice}")
            period_type = st.selectbox("Type", ["shutdown", "maintenance_window", "holiday"])
            col1, col2 = st.columns(2)
            with col1:
                start_datetime = st.text_input("Start (YYYY-MM-DD HH:MM:SS)")
            with col2:
                end_datetime = st.text_input("End (YYYY-MM-DD HH:MM:SS)")
            description = st.text_input("Description")
            if st.form_submit_button("Add"):
                if start_datetime.strip() and end_datetime.strip():
                    create_non_production_period(
                        scope_plant_id, period_type, start_datetime.strip(), end_datetime.strip(),
                        description or None, current_user["username"],
                    )
                    st.success(f"{period_type} added.")
                    st.rerun()
                else:
                    st.error("Enter both a start and end date/time.")


# ---------------------------------------------------------------------------
# Energy Tariff (Phase 3) - versioned, never overwritten in place; see
# engine/energy_tariff.py for the append-only history model. A scope with
# no tariff ever entered shows a clearly marked SIMULATION TARIFF (the
# roadmap's own RM 0.50/kWh example) rather than a blank/broken state.
# ---------------------------------------------------------------------------

with tab_tariff:
    st.caption(
        "Convert energy into cost. Entering a new tariff here starts a new "
        "version effective from the date given - the previous tariff for this "
        "scope is preserved as history, never overwritten, so past calculations "
        "keep using whatever rate was actually in effect at the time."
    )

    plants = get_plants()
    scope_options = {"Factory-wide": None, **{p["code"].upper(): p["id"] for p in plants}}
    scope_choice = st.selectbox("Scope", list(scope_options), key="tariff_scope_select")
    scope_plant_id = scope_options[scope_choice]

    current_tariff = get_current_tariff(
        CONFIG_DATABASE_PATH, scope_plant_id,
        current_user["username"] if current_user else None,
    )

    if current_tariff["is_simulated"]:
        st.warning(SIMULATED_TARIFF_NOTICE)

    display_currency = current_tariff["currency"] or "?"

    # Today's cost preview - reuses the SCADA Floor Plan's own reset-safe
    # accumulation helper (handles a simulator/service restart resetting
    # the Energy_kWh counter mid-day) rather than re-implementing it.
    historian = DatabaseManager(db_path=MACHINE_DATABASE_PATH)

    if scope_plant_id is None:
        today_kwh = 0.0
        any_plant_data = False
        for plant in plants:
            plant_kwh = _todays_accumulated_total(historian, f"{plant['code'].upper()}.ELEC.MAIN.Energy_kWh")
            if plant_kwh is not None:
                today_kwh += plant_kwh
                any_plant_data = True
        if not any_plant_data:
            today_kwh = None
    else:
        plant_code = next(p["code"] for p in plants if p["id"] == scope_plant_id)
        today_kwh = _todays_accumulated_total(historian, f"{plant_code.upper()}.ELEC.MAIN.Energy_kWh")

    today_cost = calculate_energy_cost(today_kwh, current_tariff)

    preview_cols = st.columns(3)
    with preview_cols[0]:
        st.metric(
            "Current rate",
            f"{display_currency} {current_tariff['energy_rate']:.4f}/kWh" if current_tariff["energy_rate"] is not None else "-",
        )
    with preview_cols[1]:
        st.metric("Today's energy so far", f"{today_kwh:,.1f} kWh" if today_kwh is not None else "-")
    with preview_cols[2]:
        st.metric("Today's cost so far", f"{display_currency} {today_cost:,.2f}" if today_cost is not None else "-")

    st.divider()

    mode_choice = st.radio(
        "Configuration mode", ["Simple", "Advanced"], horizontal=True,
        index=1 if current_tariff.get("mode") == "advanced" else 0,
        key=f"tariff_mode_radio_{scope_choice}", disabled=not can_edit,
    )

    with st.form(f"tariff_form_{scope_choice}"):
        currency = st.text_input("Currency", value=current_tariff["currency"] or "MYR", disabled=not can_edit)
        energy_rate = st.number_input(
            "Electricity rate (per kWh)", value=float(current_tariff["energy_rate"] or 0.0),
            min_value=0.0, format="%.4f", disabled=not can_edit,
        )

        peak_rate = off_peak_rate = peak_start = peak_end = None
        maximum_demand_charge = contract_maximum_demand = fixed_monthly_charge = None
        surcharge_percent = tax_percent = None
        billing_cycle = "calendar_month"

        if mode_choice == "Advanced":
            col1, col2 = st.columns(2)
            with col1:
                peak_rate = st.number_input(
                    "Peak rate (per kWh)", value=float(current_tariff.get("peak_rate") or 0.0),
                    min_value=0.0, format="%.4f", disabled=not can_edit,
                )
                peak_start = st.text_input("Peak start (HH:MM)", value=current_tariff.get("peak_start") or "08:00", disabled=not can_edit)
                maximum_demand_charge = st.number_input(
                    f"Maximum demand charge ({currency}/kW)", value=float(current_tariff.get("maximum_demand_charge") or 0.0),
                    min_value=0.0, disabled=not can_edit,
                )
                fixed_monthly_charge = st.number_input(
                    "Fixed monthly charge", value=float(current_tariff.get("fixed_monthly_charge") or 0.0),
                    min_value=0.0, disabled=not can_edit,
                )
                surcharge_percent = st.number_input(
                    "Surcharge (%)", value=float(current_tariff.get("surcharge_percent") or 0.0),
                    min_value=0.0, disabled=not can_edit,
                )
            with col2:
                off_peak_rate = st.number_input(
                    "Off-peak rate (per kWh)", value=float(current_tariff.get("off_peak_rate") or 0.0),
                    min_value=0.0, format="%.4f", disabled=not can_edit,
                )
                peak_end = st.text_input("Peak end (HH:MM)", value=current_tariff.get("peak_end") or "22:00", disabled=not can_edit)
                contract_maximum_demand = st.number_input(
                    "Contract maximum demand (kW)", value=float(current_tariff.get("contract_maximum_demand") or 0.0),
                    min_value=0.0, disabled=not can_edit,
                )
                tax_percent = st.number_input(
                    "Tax (%)", value=float(current_tariff.get("tax_percent") or 0.0),
                    min_value=0.0, disabled=not can_edit,
                )
                billing_cycle = st.selectbox(
                    "Billing cycle", ["calendar_month", "custom"],
                    index=0 if current_tariff.get("billing_cycle") != "custom" else 1,
                    disabled=not can_edit,
                )

        effective_date = st.text_input(
            "Effective from (YYYY-MM-DD)", value=datetime.now().strftime("%Y-%m-%d"), disabled=not can_edit,
        )

        if st.form_submit_button("Save new tariff version", disabled=not can_edit):
            if effective_date.strip():
                create_tariff(
                    CONFIG_DATABASE_PATH, scope_plant_id, effective_date.strip(), current_user["username"],
                    mode="advanced" if mode_choice == "Advanced" else "simple",
                    currency=currency or None, energy_rate=energy_rate or None,
                    peak_rate=peak_rate or None, off_peak_rate=off_peak_rate or None,
                    peak_start=peak_start or None, peak_end=peak_end or None,
                    maximum_demand_charge=maximum_demand_charge or None,
                    contract_maximum_demand=contract_maximum_demand or None,
                    fixed_monthly_charge=fixed_monthly_charge or None,
                    surcharge_percent=surcharge_percent or None, tax_percent=tax_percent or None,
                    billing_cycle=billing_cycle, is_simulated=False,
                )
                st.success(f"New tariff saved, effective {effective_date.strip()}. The previous tariff for this scope is preserved as history.")
                st.rerun()
            else:
                st.error("Enter an effective date.")

    with st.expander("Tariff history for this scope"):
        history = get_tariff_history(CONFIG_DATABASE_PATH, scope_plant_id)
        if not history:
            st.caption("No tariff history yet.")
        for row in history:
            status = "SIMULATED" if row["is_simulated"] else "confirmed"
            expiry = row["expiry_date"] or "current"
            rate_text = f"{row['currency']} {row['energy_rate']}/kWh" if row["energy_rate"] is not None else "no rate set"
            st.write(f"- **{row['effective_date']} → {expiry}**: {rate_text} ({row['mode']}, {status})")
