from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.data_access import (
    add_maintenance_entry,
    get_equipment_list,
    get_maintenance_history,
)


st.set_page_config(page_title="Maintenance - SmartMachineAI", page_icon="🛠️", layout="wide")
st.title("🛠️ Maintenance")

CATEGORIES = [
    "Preventive Maintenance",
    "Repair",
    "Replacement",
    "Inspection",
    "Calibration",
]

DUE_SOON_WINDOW_DAYS = 14


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


STATUS_ORDER = {"OVERDUE": 0, "DUE SOON": 1, "OK": 2, "NOT SCHEDULED": 3}
STATUS_ICONS = {"OVERDUE": "🔴", "DUE SOON": "🟠", "OK": "🟢", "NOT SCHEDULED": "⚪"}

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


equipment_list = get_equipment_list()
today = date.today()

st.subheader("Upcoming & Overdue Maintenance")

schedule_rows = []
for equipment in equipment_list:
    due = _next_due(equipment)
    status = _status_for(due, today)
    days_left = (due - today).days if due else None
    schedule_rows.append(
        {
            "equipment": equipment,
            "due": due,
            "status": status,
            "days_left": days_left,
        }
    )

schedule_rows.sort(
    key=lambda row: (
        STATUS_ORDER[row["status"]],
        row["due"] or date.max,
    )
)

schedule_rows = [
    row
    for row in schedule_rows
    if row["equipment"]["id"] not in st.session_state["ignored_overdue"]
]

for row in schedule_rows:
    equipment = row["equipment"]
    status = row["status"]
    brand_model = " / ".join(part for part in [equipment["brand"], equipment["model"]] if part)
    due_text = row["due"].isoformat() if row["due"] else "not scheduled"

    if row["days_left"] is None:
        detail = ""
    elif row["days_left"] < 0:
        detail = f" ({abs(row['days_left'])} days overdue)"
    else:
        detail = f" ({row['days_left']} days left)"

    line_col, done_col, ignore_col = st.columns([6, 1, 1])

    with line_col:
        st.write(
            f"{STATUS_ICONS[status]} **{equipment['display_name']}** "
            f"({brand_model}) — next due: {due_text}{detail} — last serviced: "
            f"{equipment['last_serviced_at'] or 'unknown'}"
        )

    if status == "OVERDUE":
        with done_col:
            if st.button("✅ Done", key=f"done_{equipment['id']}"):
                _mark_done_dialog(equipment)

        with ignore_col:
            if st.button("🚫 Ignore", key=f"ignore_{equipment['id']}"):
                st.session_state["ignored_overdue"].add(equipment["id"])
                st.rerun()

st.divider()
st.subheader("Log Maintenance Work")

equipment_options = {eq["display_name"]: eq for eq in equipment_list}
selected_name = st.selectbox("Equipment", sorted(equipment_options))
selected_equipment = equipment_options[selected_name]

category = st.selectbox("Category", CATEGORIES)
description = st.text_area("What was done?", placeholder="e.g. Changed compressor oil")
parts_replaced = st.text_input("Parts replaced (optional)")
performed_by = st.text_input("Performed by (optional)")
performed_at = st.date_input("Date performed", value=today)

skip_next_due = st.checkbox("Don't set a next-due date (leave existing schedule as is)")
suggested_next_due = _next_due(selected_equipment) or today
next_due_input = None
if not skip_next_due:
    next_due_input = st.date_input("Next due date", value=suggested_next_due, key="next_due_input")

if st.button("Save Entry", type="primary"):
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
        st.success(f"Logged {category.lower()} for {selected_equipment['display_name']}.")
        st.rerun()

st.divider()
st.subheader("Maintenance History")

history_filter = st.selectbox(
    "Show history for", ["All equipment"] + sorted(equipment_options), key="history_filter"
)
filter_equipment_id = (
    None if history_filter == "All equipment" else equipment_options[history_filter]["id"]
)
history_rows = get_maintenance_history(filter_equipment_id)

if not history_rows:
    st.caption("No maintenance logged yet.")
else:
    for entry in history_rows:
        next_due_text = f" — next due {entry['next_due_at']}" if entry["next_due_at"] else ""
        parts_text = f" — parts: {entry['parts_replaced']}" if entry["parts_replaced"] else ""
        by_text = f" (by {entry['performed_by']})" if entry["performed_by"] else ""
        st.write(
            f"- **{entry['performed_at']}** — {entry['equipment_display_name']} — "
            f"[{entry['category']}] {entry['description']}{parts_text}{next_due_text}{by_text}"
        )
