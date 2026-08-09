from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.data_access import (
    add_maintenance_entry,
    add_service_entry,
    get_equipment_list,
    get_maintenance_history,
    get_service_history,
)


st.set_page_config(page_title="Service & Maintenance - SmartMachineAI", page_icon="🛠️", layout="wide")
st.title("🛠️ Service & Maintenance")

CATEGORIES = [
    "Preventive Maintenance",
    "Repair",
    "Replacement",
    "Inspection",
    "Calibration",
]

# Placeholder until a real user-management feature exists (explicitly
# scoped as "develop it later" - not built here). Swap this for a real
# list (e.g. a `users` table) once that feature lands; nothing else in
# this page needs to change when it does.
PERSON_IN_CHARGE_OPTIONS = [
    "Ahmad Faizal",
    "Siti Nurhaliza",
    "Kumar Raj",
    "Wong Mei Ling",
    "Tan Wei Jian",
]

DUE_SOON_WINDOW_DAYS = 14

STATUS_ORDER = {"OVERDUE": 0, "DUE SOON": 1, "OK": 2, "NOT SCHEDULED": 3}
STATUS_ICONS = {"OVERDUE": "🔴", "DUE SOON": "🟠", "OK": "🟢", "NOT SCHEDULED": "⚪"}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _next_due(equipment: dict) -> date | None:
    explicit = _parse_date(equipment.get("next_due_at"))
    if explicit is not None:
        return explicit

    last_serviced = _parse_date(equipment.get("last_serviced_at"))
    interval = equipment.get("service_interval_days")
    if last_serviced is not None and interval:
        return last_serviced + timedelta(days=int(interval))

    return None


def _status_for(due: date | None, today: date) -> str:
    if due is None:
        return "NOT SCHEDULED"
    days_left = (due - today).days
    if days_left < 0:
        return "OVERDUE"
    if days_left <= DUE_SOON_WINDOW_DAYS:
        return "DUE SOON"
    return "OK"


if "ignored_overdue" not in st.session_state:
    # Session-only dismissal, not persisted - "ignore" just clears an
    # overdue item from view for now, unlike "done" which writes a
    # real maintenance_log record. Resets on page reload by design.
    st.session_state["ignored_overdue"] = set()


@st.dialog("Mark maintenance as done")
def _mark_done_dialog(equipment: dict) -> None:
    st.write(f"**{equipment['display_name']}**")

    category = st.selectbox("Category", CATEGORIES, key="dialog_category")
    report = st.text_area(
        "Maintenance report",
        placeholder="e.g. Changed compressor oil, replaced air filter",
        key="dialog_report",
    )
    performed_at = st.date_input("Date performed", value=date.today(), key="dialog_performed_at")
    suggested_next_due = _next_due(equipment) or date.today()
    next_due = st.date_input("Next due date", value=suggested_next_due, key="dialog_next_due")

    if st.button("Save", type="primary", key="dialog_save"):
        if not report.strip():
            st.error("Please describe what was done.")
        else:
            add_maintenance_entry(
                equipment_id=equipment["id"],
                category=category,
                description=report.strip(),
                performed_at=performed_at.isoformat(),
                next_due_at=next_due.isoformat(),
            )
            st.session_state["ignored_overdue"].discard(equipment["id"])
            st.rerun()


@st.dialog("New Service Record")
def _new_service_dialog(equipment_options: dict) -> None:
    selected_name = st.selectbox("Equipment", sorted(equipment_options), key="service_dialog_equipment")
    selected_equipment = equipment_options[selected_name]
    person = st.selectbox("Person in Charge", PERSON_IN_CHARGE_OPTIONS, key="service_dialog_person")
    performed_at = st.date_input("Date performed", value=date.today(), key="service_dialog_date")
    report = st.text_area(
        "Service report",
        placeholder="e.g. Replaced air filter, checked belt tension",
        key="service_dialog_report",
    )

    if st.button("Save", type="primary", key="service_dialog_save"):
        if not report.strip():
            st.error("Please describe what was done.")
        else:
            add_service_entry(
                equipment_id=selected_equipment["id"],
                person_in_charge=person,
                description=report.strip(),
                performed_at=performed_at.isoformat(),
            )
            # A service visit bumps last_serviced_at, which can change
            # the computed due date (see _next_due) - clear any stale
            # "Ignore" so Upcoming & Overdue reflects the update.
            st.session_state["ignored_overdue"].discard(selected_equipment["id"])
            st.rerun()


equipment_list = get_equipment_list()
equipment_options = {eq["display_name"]: eq for eq in equipment_list}
today = date.today()

service_tab, maintenance_tab = st.tabs(["Service", "Maintenance"])

with service_tab:
    st.subheader("Service Records")

    if st.button("+ New Service Record", type="primary"):
        _new_service_dialog(equipment_options)

    service_history_filter = st.selectbox(
        "Show history for", ["All equipment"] + sorted(equipment_options), key="service_history_filter"
    )
    service_filter_id = (
        None if service_history_filter == "All equipment" else equipment_options[service_history_filter]["id"]
    )
    service_rows = get_service_history(service_filter_id)

    if not service_rows:
        st.caption("No service records yet.")
    else:
        service_df = pd.DataFrame(
            [
                {
                    "Date Performed": row["performed_at"],
                    "Equipment": row["equipment_display_name"],
                    "Person in Charge": row["person_in_charge"] or "-",
                    "Report": row["description"],
                }
                for row in service_rows
            ]
        )
        st.dataframe(service_df, hide_index=True, width="stretch")

with maintenance_tab:
    st.subheader("Upcoming & Overdue Maintenance")

    schedule_rows = []
    for equipment in equipment_list:
        due = _next_due(equipment)
        status = _status_for(due, today)
        days_left = (due - today).days if due else None
        schedule_rows.append(
            {"equipment": equipment, "due": due, "status": status, "days_left": days_left}
        )

    schedule_rows.sort(key=lambda row: (STATUS_ORDER[row["status"]], row["due"] or date.max))

    schedule_rows = [
        row
        for row in schedule_rows
        if row["equipment"]["id"] not in st.session_state["ignored_overdue"]
    ]

    schedule_df = pd.DataFrame(
        [
            {
                "Status": f"{STATUS_ICONS[row['status']]} {row['status']}",
                "Equipment": row["equipment"]["display_name"],
                "Brand / Model": " / ".join(
                    part for part in [row["equipment"]["brand"], row["equipment"]["model"]] if part
                ),
                "Next Due": row["due"].isoformat() if row["due"] else "not scheduled",
                "Last Serviced": row["equipment"]["last_serviced_at"] or "unknown",
            }
            for row in schedule_rows
        ]
    )

    schedule_selection = st.dataframe(
        schedule_df,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="schedule_table",
    )

    selected_positions = schedule_selection.selection.rows if schedule_selection else []

    if not selected_positions:
        st.caption("Select a row above to mark it done or ignore it (only available for overdue items).")
    else:
        selected_row = schedule_rows[selected_positions[0]]
        selected_equipment = selected_row["equipment"]

        if selected_row["status"] != "OVERDUE":
            st.caption(f"**{selected_equipment['display_name']}** is not overdue - nothing to action.")
        else:
            st.write(f"Selected: **{selected_equipment['display_name']}**")
            done_col, ignore_col = st.columns(2)

            with done_col:
                if st.button("✅ Mark as Done", key=f"done_{selected_equipment['id']}"):
                    _mark_done_dialog(selected_equipment)

            with ignore_col:
                if st.button("🚫 Ignore", key=f"ignore_{selected_equipment['id']}"):
                    st.session_state["ignored_overdue"].add(selected_equipment["id"])
                    st.rerun()

    st.divider()
    st.subheader("Log Maintenance Work")

    selected_name = st.selectbox("Equipment", sorted(equipment_options), key="log_equipment")
    selected_equipment = equipment_options[selected_name]

    category = st.selectbox("Category", CATEGORIES, key="log_category")
    description = st.text_area("What was done?", placeholder="e.g. Changed compressor oil", key="log_description")
    parts_replaced = st.text_input("Parts replaced (optional)", key="log_parts")
    performed_by = st.text_input("Performed by (optional)", key="log_performed_by")
    performed_at = st.date_input("Date performed", value=today, key="log_performed_at")

    skip_next_due = st.checkbox("Don't set a next-due date (leave existing schedule as is)", key="log_skip_next_due")
    suggested_next_due = _next_due(selected_equipment) or today
    next_due_input = None
    if not skip_next_due:
        next_due_input = st.date_input("Next due date", value=suggested_next_due, key="log_next_due_input")

    if st.button("Save Entry", type="primary", key="log_save"):
        if not description.strip():
            st.error("Please describe what was done.")
        else:
            add_maintenance_entry(
                equipment_id=selected_equipment["id"],
                category=category,
                description=description.strip(),
                performed_at=performed_at.isoformat(),
                parts_replaced=parts_replaced.strip() or None,
                performed_by=performed_by.strip() or None,
                next_due_at=next_due_input.isoformat() if next_due_input else None,
            )
            # New work logged means whatever schedule state this
            # equipment had before (including a prior "Ignore") is
            # stale - let it reappear in Upcoming & Overdue reflecting
            # the just-logged update.
            st.session_state["ignored_overdue"].discard(selected_equipment["id"])
            st.success(f"Logged {category.lower()} for {selected_equipment['display_name']}.")
            st.rerun()

    st.divider()
    st.subheader("Maintenance History")

    history_filter = st.selectbox(
        "Show history for", ["All equipment"] + sorted(equipment_options), key="maintenance_history_filter"
    )
    filter_equipment_id = (
        None if history_filter == "All equipment" else equipment_options[history_filter]["id"]
    )
    history_rows = get_maintenance_history(filter_equipment_id)

    if not history_rows:
        st.caption("No maintenance logged yet.")
    else:
        history_df = pd.DataFrame(
            [
                {
                    "Date Performed": row["performed_at"],
                    "Equipment": row["equipment_display_name"],
                    "Category": row["category"],
                    "Description": row["description"],
                    "Parts Replaced": row["parts_replaced"] or "-",
                    "Next Due": row["next_due_at"] or "-",
                    "Performed By": row["performed_by"] or "-",
                }
                for row in history_rows
            ]
        )
        st.dataframe(history_df, hide_index=True, width="stretch")
