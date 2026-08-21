from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime

from engine.opportunity_targets import DISMISSAL_REASONS
from engine.savings_verification_targets import ACTION_CATEGORY_VALUES, DEFAULT_STABILIZATION_DAYS
from ui import auth
from ui import savings_verification_data as svd
from ui.currency_format import format_money
from ui.data_access import dismiss_opportunity_action, get_opportunities, get_opportunity_filter_options, get_plants
from ui.evidence_display import render_readable


st.title("Energy Opportunities")
st.caption(
    "Defensible, deterministic Engineering Investigation Priorities derived from Phase 9's anomaly findings "
    "(Rule 4 - no LLM anywhere on this page). An opportunity here means 'this pattern of abnormal energy use is "
    "worth an engineer's attention', never a confirmed fault, root cause, or guaranteed saving. "
    "Estimated Potential Saving is shown only when a specific, defensible calculation basis exists for that "
    "opportunity type - otherwise it is explicitly marked Unavailable, never guessed."
)

plants = get_plants()
filter_options = get_opportunity_filter_options()

filter_cols = st.columns(4)

with filter_cols[0]:
    status_label = st.selectbox("Status", ["New", "Dismissed", "All"], index=0)
    status = {"New": "NEW", "Dismissed": "DISMISSED", "All": None}[status_label]

with filter_cols[1]:
    plant_options = {p["code"].upper(): p["code"] for p in plants}
    plant_label = st.selectbox("Plant", ["All"] + list(plant_options), key="opportunities_plant")
    plant_code = None if plant_label == "All" else plant_options[plant_label]

with filter_cols[2]:
    category = st.selectbox("Category", ["All"] + filter_options["categories"])
    category = None if category == "All" else category

with filter_cols[3]:
    priority = st.selectbox("Priority", ["All"] + filter_options["priorities"])
    priority = None if priority == "All" else priority

opportunities = get_opportunities(status=status, plant_code=plant_code, category=category, priority=priority)

if not opportunities:
    st.info("No matching energy opportunities.")
    st.stop()

st.caption(f"{len(opportunities)} matching opportunity(ies). Sorted by Engineering Investigation Priority, then most recently updated.")

priority_colors = {
    "HIGH": "background-color: #ffb3b3; color: #1a1a1a",
    "MEDIUM": "background-color: #ffe6a3; color: #1a1a1a",
    "LOW": "background-color: #d6e9ff; color: #1a1a1a",
}


def _fmt_money(value, currency=None) -> str:
    formatted = format_money(value, currency)
    return formatted if formatted is not None else "Unavailable"


def _fmt_potential_saving(value, currency=None) -> str:
    """Estimated Potential Saving specifically uses 'Not yet estimable'
    rather than the generic 'Unavailable' - the null case here means
    'no defensible calculation basis exists yet', never 'the software
    failed to compute a value'. The stored value itself is unchanged
    (still NULL, never defaulted to 0)."""
    formatted = format_money(value, currency)
    return formatted if formatted is not None else "Not yet estimable"


def _fmt(value, decimals: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


rows = []
for o in opportunities:
    rows.append(
        {
            "Priority": o["priority"],
            "Plant": o["plant_code"].upper(),
            "Equipment": o["instance_key"],
            "Opportunity": o["title"],
            "Observed Estimated Excess Cost": _fmt_money(o["observed_excess_cost"], o.get("observed_cost_currency")),
            "Estimated Potential Saving": _fmt_potential_saving(o["estimated_potential_saving_period"], o.get("saving_currency")),
            "Confidence": o["confidence"],
            "Status": o["status"],
            "Occurrences": o["occurrence_count"],
            "Last Updated": o["last_updated"],
        }
    )

df = pd.DataFrame(rows)


def _highlight_priority(row: pd.Series) -> list[str]:
    color = priority_colors.get(row["Priority"], "")
    return [color] * len(row)


st.dataframe(
    df.style.apply(_highlight_priority, axis=1),
    width="stretch",
    height=450,
    column_config={
        "Opportunity": st.column_config.TextColumn("Opportunity", width="large"),
        "Priority": st.column_config.TextColumn("Priority", width="small"),
        "Status": st.column_config.TextColumn("Status", width="small"),
        "Confidence": st.column_config.TextColumn("Confidence", width="small"),
        "Observed Estimated Excess Cost": st.column_config.TextColumn("Observed Estimated Excess Cost", width="medium"),
        "Estimated Potential Saving": st.column_config.TextColumn("Estimated Potential Saving", width="medium"),
    },
)

st.divider()
st.subheader("Opportunity detail")

label_by_id = {
    o["id"]: f"[{o['priority']}] {o['plant_code'].upper()} - {o['instance_key']} - {o['title']} (updated {o['last_updated']})"
    for o in opportunities
}
selected_id = st.selectbox(
    "Select a row above to inspect its full evidence, calculation basis, and recommended engineering check",
    list(label_by_id),
    format_func=lambda i: label_by_id[i],
)
selected = next(o for o in opportunities if o["id"] == selected_id)

detail_cols = st.columns(3)
with detail_cols[0]:
    st.metric(
        "Confidence", selected["confidence"],
        help="Confidence in the underlying anomaly EVIDENCE that this opportunity is real - not a percentage, "
             "and independent of whether a dollar saving could be estimated. A 'Low' confidence opportunity with "
             "a real Estimated Potential Saving, and a 'High' confidence opportunity with 'Not yet estimable', "
             "are both possible and both legitimate.",
    )
    st.metric("Implementation difficulty", selected["implementation_difficulty"])
with detail_cols[1]:
    st.metric("Occurrences linked", selected["occurrence_count"])
    st.metric("Equipment criticality", selected["equipment_criticality"] or "Unconfigured")
with detail_cols[2]:
    st.metric("Status", selected["status"])
    st.metric("Category", selected["category"])

st.markdown("**Financial figures - kept explicitly separate:**")
fin_cols = st.columns(2)
with fin_cols[0]:
    st.markdown(f"**Observed Estimated Excess Cost:** {_fmt_money(selected['observed_excess_cost'], selected.get('observed_cost_currency'))}")
    st.caption(f"Observed excess energy: {_fmt(selected['observed_excess_energy_kwh'])} kWh, over "
               f"{selected['observed_period_start']} to {selected['observed_period_end']}. "
               "This is Phase 9's own interval-integrated, tariff-applied figure - a historical, already-occurred "
               "fact about abnormal energy use. It is NOT a potential, recoverable, guaranteed, or verified saving.")
with fin_cols[1]:
    if selected["estimated_potential_saving_period"] is None:
        st.markdown("**Estimated Potential Saving:** Not yet estimable", help=(
            "A potential saving is shown only when a defensible engineering target or justified calculation "
            "basis exists. Observed excess cost is not automatically treated as recoverable saving."
        ))
        st.caption(f"Reason: {selected['saving_unavailable_reason'] or 'Insufficient evidence to determine the realistically avoidable portion.'}")
    else:
        st.markdown(f"**Estimated Potential Saving:** {_fmt_money(selected['estimated_potential_saving_period'], selected.get('saving_currency'))}")
        st.caption(f"Basis: {selected['saving_basis']}")

if selected["annualization_method"] == "unavailable_conservative" or selected["estimated_monthly_saving"] is None:
    st.caption("Monthly/Annual Potential Saving: Not yet estimable - insufficient recurrence/duration evidence "
               "(or no configured operating schedule) to defensibly project beyond the observed period.")
else:
    st.caption(f"Estimated monthly saving: {_fmt_money(selected['estimated_monthly_saving'], selected.get('saving_currency'))} - "
               f"Estimated annual saving: {_fmt_money(selected['estimated_annual_saving'], selected.get('saving_currency'))} "
               f"(method: {selected['annualization_method']})")

st.markdown(f"**Recommended engineering check:** {selected['recommendation']}")
st.caption(f"Tariff provenance: {selected['tariff_provenance']} - Rule provenance: {selected['rule_provenance']}")

# Phase 15 - one lightweight Ask AI entry point (approved cross-page
# integration set).
if st.button("💬 Ask AI about this equipment", key=f"ask_ai_link_{selected['id']}"):
    st.switch_page(
        "pages/1_Ask_AI.py",
        query_params={"ask_equipment": selected["instance_key"], "ask_label": selected["instance_key"]},
    )

for label, key in (
    ("Evidence", "evidence_json"),
    ("Priority breakdown", "priority_breakdown_json"),
    ("Assumptions", "assumptions_json"),
    ("Limitations", "limitations_json"),
    ("Linked source anomaly IDs", "source_anomaly_ids"),
):
    raw = selected.get(key)
    with st.expander(label, expanded=(label == "Evidence")):
        if not raw:
            st.write("None recorded.")
            continue
        try:
            render_readable(json.loads(raw))
        except (TypeError, ValueError):
            st.write(raw)

if selected["status"] == "DISMISSED":
    st.info(f"Dismissed {selected['dismissed_at']} - reason: {selected['dismissal_reason']}"
            + (f" - comment: {selected['dismissal_comment']}" if selected["dismissal_comment"] else ""))
else:
    active_intervention = svd.get_active_intervention_for_opportunity(selected["id"])

    if active_intervention is not None:
        st.divider()
        st.subheader("Intervention already recorded")
        st.info(
            f"Status: **{svd.engineer_status_label(active_intervention['status'], None)}** - "
            f"{active_intervention['action_category']} recorded "
            f"{active_intervention['recorded_at']} by {active_intervention['recorded_by']}. "
            "See the Savings Verification page for full evidence and progress."
        )
    elif auth.can_edit("admin", "engineer"):
        action_col, dismiss_col = st.columns(2)

        with action_col:
            st.subheader("Record Intervention")
            st.caption(
                "Log an engineering action taken in response to this opportunity - equipment and plant context "
                "are inherited automatically, not re-entered. Use this AFTER the action has actually been "
                "implemented (or is being formally logged per your team's workflow) - this only **records** what "
                "you did; it does not perform, schedule, or execute any control action on the equipment."
            )
            with st.form(key=f"intervention_form_{selected['id']}"):
                st.markdown(f"**Equipment:** {selected['instance_key']} (Plant {selected['plant_code'].upper()})")
                st.markdown(f"**Opportunity:** {selected['title']}")
                st.caption(f"Observed Estimated Excess Cost: {_fmt_money(selected['observed_excess_cost'], selected.get('observed_cost_currency'))} - "
                           f"Estimated Potential Saving: {_fmt_money(selected['estimated_potential_saving_period'], selected.get('saving_currency'))}")

                action_category = st.selectbox("Action category", ACTION_CATEGORY_VALUES, key=f"category_{selected['id']}")
                description = st.text_area("Action description", placeholder="What was done?", key=f"desc_{selected['id']}")
                expected_effect = st.text_input("Expected effect (optional)", key=f"effect_{selected['id']}")

                now = datetime.now()
                date_col, time_col = st.columns(2)
                implemented_date = date_col.date_input("Implementation date", value=now.date(), key=f"date_{selected['id']}")
                implemented_time = time_col.time_input("Implementation time", value=now.time(), key=f"time_{selected['id']}")

                default_stabilization = DEFAULT_STABILIZATION_DAYS.get(action_category, 3)
                stabilization_days = st.number_input(
                    "Stabilization period (days)", min_value=0, value=default_stabilization, step=1, key=f"stab_{selected['id']}",
                    help="Established per-category default shown - editable, never fabricated.",
                )
                recorded_by = st.text_input("Recorded by", value=(auth.current_user() or {}).get("display_name", ""), key=f"by_{selected['id']}")

                submitted = st.form_submit_button("Record Intervention")
                if submitted:
                    implemented_at = datetime.combine(implemented_date, implemented_time)
                    if not description.strip():
                        st.error("Action description is required.")
                    elif not recorded_by.strip():
                        st.error("Recorded by is required.")
                    elif implemented_at > datetime.now():
                        st.error("Implementation timestamp cannot be in the future.")
                    else:
                        try:
                            svd.record_and_implement_intervention(
                                selected["id"], selected["plant_id"], selected["instance_key"], action_category,
                                description.strip(), recorded_by.strip(), expected_effect.strip() or None,
                                implemented_at, int(stabilization_days),
                            )
                            st.success("Intervention recorded. See the Savings Verification page to track progress.")
                            st.rerun()
                        except ValueError as error:
                            st.error(str(error))

        with dismiss_col:
            st.subheader("Dismiss this opportunity")
            st.caption("Dismissing preserves this row and its evidence/links permanently - it is never deleted. "
                       "If genuinely new anomaly evidence accumulates afterward, a fresh opportunity may be identified later.")
            with st.form(key=f"dismiss_form_{selected['id']}"):
                reason = st.selectbox("Reason", DISMISSAL_REASONS)
                comment = st.text_area("Comment (optional)")
                dismiss_submitted = st.form_submit_button("Dismiss")
                if dismiss_submitted:
                    dismiss_opportunity_action(selected["id"], reason, comment or None)
                    st.success("Dismissed.")
                    st.rerun()

st.caption(
    "No AI-generated text appears on this page. Terminology is fixed: 'Potential Saving' is never 'Verified', "
    "'Actual', or 'Guaranteed'; no root-cause or maintenance-action claims are made."
)
