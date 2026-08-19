from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dateutil.relativedelta import relativedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui import auth
from ui import health_data as hd
from ui import maintenance_intelligence_data as mid
from ui.data_access import (
    add_maintenance_entry,
    add_service_entry,
    get_equipment_list,
    get_maintenance_history,
    get_person_in_charge_options,
    get_service_history,
)


current_user = auth.current_user()
can_edit = auth.can_edit("admin", "engineer")

st.title("🛠️ Service & Maintenance")

if not can_edit:
    st.info("View-only - only Engineer and Administrator accounts can log service/maintenance work.")

CATEGORIES = [
    "Preventive Maintenance",
    "Repair",
    "Replacement",
    "Inspection",
    "Calibration",
]

# Real accounts now that the users table/login exists (see
# ui/data_access.py's get_person_in_charge_options) - display names of
# every active user, regardless of role, since anyone logged in could
# physically be the one who did the work.
PERSON_IN_CHARGE_OPTIONS = get_person_in_charge_options()

NEXT_DUE_INTERVALS = {
    "1 month": 1,
    "3 months": 3,
    "6 months": 6,
    "12 months": 12,
    "18 months": 18,
    "24 months": 24,
}

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

    if st.button("Save", type="primary", key="dialog_save", disabled=not auth.can_edit("admin", "engineer")):
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

    if st.button(
        "Save", type="primary", key="service_dialog_save", disabled=not auth.can_edit("admin", "engineer")
    ):
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

service_tab, maintenance_tab, intelligence_tab = st.tabs(["Service", "Maintenance", "Maintenance Intelligence"])

with service_tab:
    st.subheader("Service Records")

    if not equipment_options:
        st.info("No equipment configured yet - an Administrator needs to add some first.")

    if st.button(
        "+ New Service Record", type="primary", disabled=not can_edit or not equipment_options
    ):
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
        st.dataframe(service_df, hide_index=True, width="stretch", height=400)

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

    # Only genuinely actionable items belong here - equipment that's
    # OK (comfortably scheduled) or NOT SCHEDULED (no due date at all
    # yet) isn't upcoming or overdue, so it's noise in this view. Past
    # work for everything still lives in Maintenance History below.
    schedule_rows = [
        row
        for row in schedule_rows
        if row["status"] in ("OVERDUE", "DUE SOON")
        and row["equipment"]["id"] not in st.session_state["ignored_overdue"]
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
        height=400,
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

        # Phase 12.4 - read-only Equipment Health context (item 6). This
        # is informational only - it never creates/changes a maintenance
        # record, due date, or priority. Health ATTENTION does not imply
        # "maintenance due", and an on-schedule equipment is not
        # guaranteed HEALTHY - the two remain separate concepts.
        health_cols = st.columns([3, 1])
        with health_cols[0]:
            health = hd.compact_health_context(selected_equipment["id"])
            if health["assessed"]:
                st.markdown(
                    f"**Equipment Health:** {health['score']} · {health['state']} &nbsp;·&nbsp; "
                    f"**Confidence:** {health['confidence']} &nbsp;·&nbsp; Last assessed: {health['last_assessed']}"
                )
            else:
                st.caption("Equipment Health: Not Assessed")
        with health_cols[1]:
            if st.button("View Health Detail →", key=f"health_link_{selected_equipment['id']}"):
                st.query_params["equipment_id"] = str(selected_equipment["id"])
                st.switch_page("pages/19_Equipment_Health.py")

        if selected_row["status"] != "OVERDUE":
            st.caption(f"**{selected_equipment['display_name']}** is not overdue - nothing to action.")
        else:
            st.write(f"Selected: **{selected_equipment['display_name']}**")
            done_col, ignore_col = st.columns(2)

            with done_col:
                if st.button(
                    "✅ Mark as Done",
                    key=f"done_{selected_equipment['id']}",
                    disabled=not can_edit,
                ):
                    _mark_done_dialog(selected_equipment)

            with ignore_col:
                if st.button(
                    "🚫 Ignore",
                    key=f"ignore_{selected_equipment['id']}",
                    disabled=not can_edit,
                ):
                    st.session_state["ignored_overdue"].add(selected_equipment["id"])
                    st.rerun()

    st.divider()
    st.subheader("Log Maintenance Work")

    if not equipment_options:
        st.info("No equipment configured yet - an Administrator needs to add some first.")
    else:
        st.caption("All fields are required except Parts Replaced.")

        LOG_FORM_KEYS = (
            "log_equipment",
            "log_category",
            "log_description",
            "log_parts",
            "log_performed_by",
            "log_performed_at",
            "log_next_due_interval",
        )

        selected_name = st.selectbox(
            "Equipment", sorted(equipment_options), key="log_equipment", disabled=not can_edit
        )
        selected_equipment = equipment_options[selected_name]

        category = st.selectbox("Category", CATEGORIES, key="log_category", disabled=not can_edit)
        description = st.text_area(
            "What was done?",
            placeholder="e.g. Changed compressor oil",
            key="log_description",
            disabled=not can_edit,
        )
        parts_replaced = st.text_input(
            "Parts replaced (optional)", key="log_parts", disabled=not can_edit
        )
        performed_by = st.selectbox(
            "Performed By", PERSON_IN_CHARGE_OPTIONS, key="log_performed_by", disabled=not can_edit
        )
        performed_at = st.date_input(
            "Date performed", value=today, key="log_performed_at", disabled=not can_edit
        )

        next_due_choice = st.segmented_control(
            "Next due in",
            list(NEXT_DUE_INTERVALS.keys()),
            default="3 months",
            key="log_next_due_interval",
            disabled=not can_edit,
        )

        if st.button("Save Entry", type="primary", key="log_save", disabled=not can_edit):
            if not description.strip():
                st.error("Please describe what was done.")
            elif not next_due_choice:
                st.error("Please select a next-due interval.")
            else:
                next_due_input = performed_at + relativedelta(months=NEXT_DUE_INTERVALS[next_due_choice])
                add_maintenance_entry(
                    equipment_id=selected_equipment["id"],
                    category=category,
                    description=description.strip(),
                    performed_at=performed_at.isoformat(),
                    parts_replaced=parts_replaced.strip() or None,
                    performed_by=performed_by,
                    next_due_at=next_due_input.isoformat(),
                )
                # New work logged means whatever schedule state this
                # equipment had before (including a prior "Ignore") is
                # stale - let it reappear in Upcoming & Overdue reflecting
                # the just-logged update.
                st.session_state["ignored_overdue"].discard(selected_equipment["id"])
                st.success(f"Logged {category.lower()} for {selected_equipment['display_name']}.")

                for form_key in LOG_FORM_KEYS:
                    st.session_state.pop(form_key, None)
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
        st.dataframe(history_df, hide_index=True, width="stretch", height=400)

with intelligence_tab:
    st.subheader("Maintenance Intelligence")
    st.caption(
        "Deterministic engineering prioritization (Phase 13 - no LLM anywhere on this tab, no prediction, no "
        "Remaining Useful Life, no automatic work orders). **Maintenance Priority** combines already-persisted "
        "Equipment Health evidence with the maintenance schedule - it is NOT the Health Score itself. "
        "**NOT ASSESSED** means there is not enough contributing evidence to make an assessment at all - it is "
        "never the same as **ROUTINE** (evidence was assessed and currently shows no elevated concern). A "
        "confirmed overdue maintenance schedule always shows at least **Review**, even for otherwise Healthy "
        "equipment - shown with an explicit disclosure, never silently folded into the score."
    )

    intelligence_rows = mid.get_overview()

    if not intelligence_rows:
        st.info("No equipment is currently eligible for Maintenance Intelligence assessment.")
    else:
        summary = mid.summarize(intelligence_rows)
        kpi_cols = st.columns(5)
        kpi_cols[0].metric("Urgent Review", summary["urgent_review"])
        kpi_cols[1].metric("Priority", summary["priority"])
        kpi_cols[2].metric("Review", summary["review"])
        kpi_cols[3].metric("Routine", summary["routine"])
        kpi_cols[4].metric("Not Assessed", summary["not_assessed"])

        filter_cols = st.columns(3)
        with filter_cols[0]:
            plant_options = {p["code"].upper(): p["code"] for p in mid.get_plants()}
            plant_label = st.selectbox("Plant", ["All"] + list(plant_options), key="mi_plant_filter")
            mi_plant_code = None if plant_label == "All" else plant_options[plant_label]
        with filter_cols[1]:
            priority_label = st.selectbox(
                "Maintenance Priority", ["All", "Urgent Review", "Priority", "Review", "Routine", "Not Assessed"],
                key="mi_priority_filter",
            )
        with filter_cols[2]:
            type_options = {hd.equipment_type_label(t): t for t in mid.get_filter_options(intelligence_rows)["equipment_types"]}
            type_label = st.selectbox("Equipment type", ["All"] + list(type_options), key="mi_type_filter")
            mi_equipment_type = None if type_label == "All" else type_options[type_label]

        filtered_rows = [r for r in intelligence_rows if mi_plant_code is None or r["plant_code"] == mi_plant_code]
        filtered_rows = [r for r in filtered_rows if mi_equipment_type is None or r["equipment_type"] == mi_equipment_type]
        if priority_label != "All":
            wanted = priority_label.upper().replace(" ", "_")
            filtered_rows = [r for r in filtered_rows if r["maintenance_priority"] == wanted]

        filtered_rows = mid.sort_overview(filtered_rows)

        if not filtered_rows:
            st.caption("No equipment matches the current filters.")
        else:
            mi_table_rows = []
            for r in filtered_rows:
                mi_table_rows.append({
                    "Equipment": r["display_name"],
                    "Plant": r["plant_code"].upper(),
                    "Type": hd.equipment_type_label(r["equipment_type"]),
                    "Maintenance Priority": mid.priority_label(r["maintenance_priority"]),
                    "Priority Score": r["priority_score"],
                    "Confidence": mid.confidence_label(r["recommendation_confidence"]),
                    "Overdue Floor": "Yes" if r["floor_applied"] else "-",
                    "Health Score": hd.score_text(r) if r["health_score"] is not None else "Unavailable",
                    "Health State": r["health_band"] or "-",
                })
            mi_df = pd.DataFrame(mi_table_rows)
            mi_colors = {mid.priority_label(p): f"background-color: {c}; color: #1a1a1a" for p, c in mid.PRIORITY_BADGE_COLORS.items()}

            def _mi_highlight(row: pd.Series) -> list[str]:
                color = mi_colors.get(row["Maintenance Priority"], "")
                return [color] * len(row)

            st.dataframe(mi_df.style.apply(_mi_highlight, axis=1), hide_index=True, width="stretch", height=400)

            st.divider()
            st.markdown("**Equipment detail**")
            mi_label_by_key = {
                r["instance_key"]: f"[{mid.priority_label(r['maintenance_priority'])}] {r['plant_code'].upper()} - {r['display_name']}"
                for r in filtered_rows
            }
            mi_selected_key = st.selectbox(
                "Select equipment to inspect its full priority breakdown",
                list(mi_label_by_key), format_func=lambda k: mi_label_by_key[k], key="mi_detail_select",
            )
            mi_selected_row = next(r for r in filtered_rows if r["instance_key"] == mi_selected_key)
            mi_detail = mid.get_detail(
                mi_selected_row["plant_id"], mi_selected_row["plant_code"], mi_selected_row["equipment_type"], mi_selected_key,
            )
            mi_result = mi_detail["result"]
            mi_context = mi_detail["context"]

            st.markdown(f"### {mi_context.get('display_name') or mi_selected_key}")
            detail_cols = st.columns(4)
            detail_cols[0].markdown(f"**Maintenance Priority**  \n{mid.priority_badge_html(mi_result.maintenance_priority)}", unsafe_allow_html=True)
            detail_cols[1].markdown(f"**Priority Score**  \n{mi_result.priority_score:.1f}")
            detail_cols[2].markdown(f"**Confidence**  \n{mid.confidence_label(mi_result.recommendation_confidence)}")
            detail_cols[3].markdown(
                f"**Equipment Health**  \n{hd.score_text({'health_score': mi_result.health_score})} "
                f"({mi_result.health_band or 'Not Assessed'})"
            )

            if mi_result.floor_applied:
                st.warning(f"Minimum priority floor applied: {mi_result.floor_reason}")

            if mi_result.maintenance_priority == "NOT_ASSESSED":
                st.info(
                    "Not enough contributing evidence exists to make a Maintenance Priority assessment for this "
                    "equipment yet - this is distinct from Routine, which means evidence WAS assessed and shows "
                    "no elevated concern."
                )

            st.caption(f"Recent Movement: {mi_result.recent_movement.title()} · Criticality: {mi_result.criticality or 'Unconfigured'}")

            status = mi_result.maintenance_status
            if status["has_schedule_data"]:
                st.caption(f"Maintenance schedule: next due {status['next_due']}" + (
                    f" (overdue by {status['days_overdue']} day(s))" if status["days_overdue"] else " (on schedule)"
                ))
            else:
                st.caption("Maintenance schedule: no schedule data configured for this equipment.")

            if mi_result.recommended_checks:
                st.markdown("**Recommended checks**")
                for check in mi_result.recommended_checks:
                    st.markdown(f"- {check}")

            # Phase 15 - one lightweight Ask AI entry point (approved
            # cross-page integration set).
            if st.button("💬 Ask AI about this equipment", key=f"ask_ai_link_{mi_selected_key}"):
                st.switch_page(
                    "pages/1_Ask_AI.py",
                    query_params={
                        "ask_equipment": mi_selected_key,
                        "ask_label": mi_context.get("display_name") or mi_selected_key,
                    },
                )

            with st.expander("Priority factor breakdown"):
                for factor in mi_result.priority_factors:
                    max_text = f" / max {factor.max_contribution:.1f}" if factor.max_contribution is not None else ""
                    st.markdown(f"**{factor.dimension.replace('_', ' ').title()}** - contribution {factor.contribution:+.2f}{max_text}")
                    st.caption(factor.reason)

            if mi_result.limitations:
                st.caption("Limitations: " + " ".join(mi_result.limitations))

            st.caption(
                "Maintenance Priority is an engineering prioritization signal derived from already-persisted "
                "Equipment Health evidence and the maintenance schedule - never a predicted failure date, never "
                "Remaining Useful Life, never an automatically generated work order."
            )
