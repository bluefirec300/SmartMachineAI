from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.savings_verification_targets import ACTION_CATEGORY_VALUES
from ui import savings_verification_data as svd
from ui.currency_format import currency_symbol, format_money
from ui.data_access import get_opportunities, get_plants


st.title("Savings Verification")
st.caption(
    "Engineer-recorded interventions on Energy Opportunities, and the deterministic evidence-driven verification "
    "outcome for each (Phase 11.1-11.4 - no LLM anywhere in this pipeline). A finding here is either "
    "**Verified** (measured, comparable before/after evidence supports a real energy reduction), "
    "**Not Verified** (comparable evidence existed but did not support the claim), or "
    "**More Evidence Required** (not yet enough comparable data - the system automatically re-checks as new "
    "evidence accumulates; verification is never forced and never guaranteed)."
)

all_interventions = svd.get_interventions()
summary = svd.get_summary(all_interventions)

kpi_cols = st.columns(6)
kpi_cols[0].metric("Active Interventions", summary["active"])
kpi_cols[1].metric("Awaiting Verification", summary["awaiting_verification"])
kpi_cols[2].metric("Verified", summary["verified"])
kpi_cols[3].metric("More Evidence Required", summary["more_evidence_required"])
kpi_cols[4].metric("Rejected", summary["rejected"])
if summary["verified"] > 0:
    kpi_cols[5].metric(
        "Verified Savings (sum of verified periods)",
        f"{currency_symbol(summary['verified_savings_currency'])} {summary['verified_savings_total']:,.2f}".strip(),
    )
else:
    # Deliberately no "RM 0.00" here - with zero VERIFIED interventions, a
    # dollar figure (even a correct zero) reads as "the system verified
    # savings were zero", not "nothing has been verified yet" (item 6).
    kpi_cols[5].metric("Verified Savings", "No verified interventions yet")
st.caption(
    "Verified Savings is the direct sum of each verified intervention's own measured period total (its LATEST "
    "evaluation only, never summed across earlier More-Evidence-Required attempts) - never an estimate, never "
    "annualized, never mixed with opportunity-level Estimated Potential Saving figures."
)
if summary["verified_without_cost_count"] > 0:
    st.caption(
        f"{summary['verified_without_cost_count']} verified intervention(s) have a verified energy saving but no "
        "tariff was available to convert it to a cost figure for that period - not included in the total above."
    )

open_opportunities_count = len(get_opportunities(status="NEW"))
dashboard_cols = st.columns(3)
by_period = svd.get_verified_savings_by_period(all_interventions)
dashboard_cols[0].metric(
    "Verified Savings This Month",
    f"{currency_symbol(by_period['currency'])} {by_period['this_month_total']:,.2f}" if by_period["this_month_count"] > 0 else "No verified interventions yet",
    help="Bucketed by each verified result's own verification period end date - the date the saving was actually observed, not the date the worker happened to evaluate it.",
)
dashboard_cols[1].metric(
    "Verified Savings This Year",
    f"{currency_symbol(by_period['currency'])} {by_period['this_year_total']:,.2f}" if by_period["this_year_count"] > 0 else "No verified interventions yet",
)
dashboard_cols[2].metric(
    "Open Opportunities", open_opportunities_count,
    help="Energy Opportunities currently in NEW status (not yet dismissed or acted on) - see the Energy Opportunities page.",
)
st.caption(
    "Projected Annual Savings is deferred - the backend does not yet provide a defensible annualization method "
    "for VERIFIED savings (Phase 10's opportunity-level annualization is a separate, non-comparable estimate). "
    "This will not be added to the UI until such a method exists in the backend."
)

if not all_interventions:
    st.info(
        "No interventions have been recorded yet.\n\n"
        "Review an Energy Opportunity and select **Record Intervention** after an engineering action has been implemented."
    )
    st.stop()

st.divider()

plants = get_plants()
filter_cols = st.columns(3)

with filter_cols[0]:
    status_options = {
        "All": None, "Planned": "PLANNED", "Implemented": "IMPLEMENTED",
        "Stabilizing / Awaiting Verification": "VERIFICATION_PENDING",
        "Verification In Progress": "VERIFICATION_IN_PROGRESS", "Completed": "COMPLETED",
    }
    status_label = st.selectbox("Lifecycle status", list(status_options))
    status = status_options[status_label]

with filter_cols[1]:
    plant_options = {p["code"].upper(): p["code"] for p in plants}
    plant_label = st.selectbox("Plant", ["All"] + list(plant_options), key="verification_plant")
    plant_code = None if plant_label == "All" else plant_options[plant_label]

with filter_cols[2]:
    outcome_options = {"All": None, "Verified": "VERIFIED", "Not Verified": "REJECTED", "More Evidence Required": "INCONCLUSIVE"}
    outcome_label = st.selectbox("Latest verification outcome", list(outcome_options))
    outcome = outcome_options[outcome_label]

interventions = svd.get_interventions(status=status, plant_code=plant_code, outcome=outcome)

if not interventions:
    st.info("No interventions match the current filters.")
    st.stop()

st.caption(f"{len(interventions)} matching intervention(s).")

status_colors = {
    "Verified": "background-color: #b3ffb3; color: #1a1a1a",
    "Not Verified": "background-color: #ffb3b3; color: #1a1a1a",
    "More Evidence Required": "background-color: #ffe6a3; color: #1a1a1a",
    "Verification In Progress": "background-color: #d6e9ff; color: #1a1a1a",
    "Stabilizing / Awaiting Verification": "background-color: #e6e6e6; color: #1a1a1a",
}


def _fmt_money(value, currency=None) -> str:
    formatted = format_money(value, currency)
    return formatted if formatted is not None else "Unavailable"


rows = []
for iv in interventions:
    latest = iv["latest_result"]
    rows.append({
        "Status": iv["engineer_status"],
        "Plant": iv["plant_code"].upper(),
        "Equipment": iv["instance_key"],
        "Opportunity": iv["opportunity_title"] or "-",
        "Action": iv["action_category"],
        "Implemented": iv["implemented_at"] or "-",
        "Latest evaluation": latest["evaluated_at"] if latest else "-",
        "Verified saving": svd.verified_saving_summary(iv)["display"],
        "Recorded by": iv["recorded_by"],
    })

df = pd.DataFrame(rows)


def _highlight(row: pd.Series) -> list[str]:
    color = status_colors.get(row["Status"], "")
    return [color] * len(row)


st.dataframe(df.style.apply(_highlight, axis=1), width="stretch", height=400)

st.divider()
st.subheader("Intervention detail")

label_by_id = {
    iv["id"]: f"[{iv['engineer_status']}] {iv['plant_code'].upper()} - {iv['instance_key']} - {iv['action_category']} (recorded {iv['recorded_at']})"
    for iv in interventions
}
selected_id = st.selectbox("Select an intervention to inspect its full evidence and verification history", list(label_by_id), format_func=lambda i: label_by_id[i])
selected = svd.get_intervention(selected_id)

overview_cols = st.columns(3)
with overview_cols[0]:
    # Markdown, not st.metric() - these can be long enum/identifier
    # strings (e.g. "VERIFICATION_IN_PROGRESS", a full equipment
    # instance key) that read oversized/awkward at st.metric()'s large
    # KPI-number font size.
    st.markdown(f"**Status**  \n{selected['engineer_status']}")
    st.markdown(f"**Action category**  \n{selected['action_category']}")
with overview_cols[1]:
    st.metric("Plant", selected["plant_code"].upper())
    st.markdown(f"**Equipment**  \n{selected['instance_key']}")
with overview_cols[2]:
    st.markdown(f"**Recorded by**  \n{selected['recorded_by']}")
    st.metric("Occurrences verified", len(svd.get_verification_history(selected["id"])))

st.markdown(f"**Action description:** {selected['action_description']}")
if selected["expected_effect"]:
    st.caption(f"Engineer's expected effect: {selected['expected_effect']}")

# --- Related opportunity traceability (item 9) - human-readable summary,
# not a duplicated record; raw IDs stay out of the primary label. ---
opportunity = selected.get("opportunity") or {}
if opportunity:
    with st.expander(f"Related Opportunity: {opportunity.get('title') or '-'}"):
        st.markdown(f"**Category:** {opportunity.get('category') or '-'} &nbsp;&nbsp; **Status:** {opportunity.get('status') or '-'}")
        st.markdown(f"**Observed Estimated Excess Cost:** {_fmt_money(opportunity.get('observed_excess_cost'), opportunity.get('observed_cost_currency'))}")
        st.caption("Full opportunity evidence, confidence, and dismissal history remain on the Energy Opportunities "
                    "page - this is a summary for traceability only, not a separate copy of that record.")

# --- Stabilization visibility (item 9) ---
stabilization = svd.get_stabilization_info(selected)
st.markdown("**Stabilization**")
stab_cols = st.columns(3)
stab_cols[0].markdown(f"Implemented: `{stabilization['implemented_at']}`")
stab_cols[1].markdown(f"Stabilization: `{stabilization['stabilization_days']} day(s)`")
stab_cols[2].markdown(f"Verification eligible: `{stabilization['eligible_after_start']}`")
if not stabilization["eligible"]:
    st.info(f"Stabilizing - approximately {stabilization['days_remaining']} day(s) remaining before verification evidence becomes eligible.")

# --- Financial separation (item 12) ---
st.divider()
st.markdown("**Financial figures - kept explicitly separate:**")
fin_cols = st.columns(2)
with fin_cols[0]:
    est_value = opportunity.get("estimated_potential_saving_period")
    st.markdown(f"**Estimated Savings (opportunity-level):** {_fmt_money(est_value, opportunity.get('saving_currency')) if est_value is not None else 'Not yet estimable'}")
    st.caption("From the linked Energy Opportunity - a prospective estimate only, never a measured result.")
with fin_cols[1]:
    latest = selected["latest_result"]
    saving = svd.verified_saving_summary(selected)
    if selected["latest_outcome"] == "VERIFIED" and latest and latest.get("verified_cost") is not None:
        st.markdown(f"**Verified Saving (measured, this period only):** {saving['display']}")
        st.caption(f"Measured over {latest['verification_period_start']} to {latest['verification_period_end']} - {saving['explanation']}")
    else:
        st.markdown(f"**Verified Saving:** {saving['display']}")
        st.caption(saving["explanation"])

# --- Verification history (items 10, 26) ---
st.divider()
st.subheader("Verification history")
st.caption("Append-only - every evaluation is preserved, shown chronologically. A terminal result never hides prior evaluations.")

history = svd.get_verification_history(selected["id"])
if not history:
    st.info("No evaluations have run yet - the system will evaluate automatically once stabilization has elapsed and evidence accumulates.")
else:
    outcome_labels = {"VERIFIED": "✅ Verified", "REJECTED": "❌ Not Verified", "INCONCLUSIVE": "🟡 More Evidence Required"}
    for i, evaluation in enumerate(history, start=1):
        with st.expander(f"Evaluation {i} - {outcome_labels.get(evaluation['result'], evaluation['result'])} - {evaluation['evaluated_at']}", expanded=(i == len(history))):
            evidence_meta = {}
            if evaluation.get("evidence_json"):
                try:
                    evidence_meta = json.loads(evaluation["evidence_json"])
                except (TypeError, ValueError):
                    evidence_meta = {}

            detail_cols = st.columns(3)
            detail_cols[0].metric("Evidence quality", evaluation.get("confidence") or "-")
            detail_cols[1].metric("Before (reference)", f"{evidence_meta.get('before_basis'):.2f} {evidence_meta.get('basis_unit', '')}" if evidence_meta.get("before_basis") is not None else "-")
            detail_cols[2].metric("After (verification)", f"{evidence_meta.get('after_basis'):.2f} {evidence_meta.get('basis_unit', '')}" if evidence_meta.get("after_basis") is not None else "-")

            if evaluation.get("confidence") in svd.EVIDENCE_QUALITY_EXPLANATIONS:
                st.caption(svd.EVIDENCE_QUALITY_EXPLANATIONS[evaluation["confidence"]])

            money_cols = st.columns(3)
            money_cols[0].markdown(f"**Energy saving:** {evaluation['verified_energy_kwh']:.2f} kWh" if evaluation["verified_energy_kwh"] is not None else "**Energy saving:** Unavailable")
            pct = evidence_meta.get("energy_saving_percentage")
            money_cols[1].markdown(f"**Percentage saving:** {pct:.2f}%" if pct is not None else "**Percentage saving:** Unavailable")
            money_cols[2].markdown(f"**Cost saving:** {_fmt_money(evaluation['verified_cost'], evaluation.get('verified_cost_currency'))}")

            if evidence_meta.get("usable_reference_sample_count") is not None:
                st.caption(f"Usable comparable periods: {evidence_meta.get('usable_reference_sample_count')} reference / {evidence_meta.get('usable_after_sample_count')} verification.")

            # --- Disclosed limitations (item 19) - e.g. partial tariff coverage,
            # maintenance-window exclusion - read verbatim from the backend,
            # never recomputed or paraphrased here. ---
            limitations = []
            if evaluation.get("limitations_json"):
                try:
                    limitations = json.loads(evaluation["limitations_json"])
                except (TypeError, ValueError):
                    limitations = []
            if limitations:
                with st.expander("Disclosed limitations", expanded=False):
                    for item in limitations:
                        st.caption(f"- {item}")

            if evaluation["result"] == "INCONCLUSIVE":
                st.warning(
                    f"{evaluation['reason']}\n\n"
                    "This does not mean the intervention failed - the system will automatically re-evaluate when "
                    "materially new evidence becomes available (more comparable data, improved context matching, or "
                    "reduced maintenance-window contamination). Eventual verification is not guaranteed."
                )
            elif evaluation["result"] == "REJECTED":
                st.error(
                    f"{evaluation['reason']}\n\n"
                    "Sufficient comparable evidence existed, but the measured result did not support a positive "
                    "saving claim. This is a real measurement outcome, not a software failure."
                )
            else:
                st.success(evaluation["reason"])

            with st.expander("Full evidence detail"):
                st.json(evidence_meta)

st.caption(
    "No AI-generated conclusions appear anywhere on this page. Verification outcomes (VERIFIED/REJECTED/"
    "INCONCLUSIVE) and lifecycle transitions are set exclusively by the backend evidence engine and worker - "
    "this page never provides a manual 'force verify' action."
)
