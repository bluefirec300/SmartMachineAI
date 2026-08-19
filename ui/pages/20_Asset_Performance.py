from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui import asset_performance_data as apd
from ui import health_data as hd


st.title("Asset Performance & Reliability Analytics")
st.caption(
    "Deterministic, backward-looking engineering analytics (Phase 14 - no LLM, no prediction, no Remaining Useful "
    "Life anywhere on this page). Every dimension compares CURRENT operation against its own established historical "
    "reference, under Phase 8's own context-matched conditions - never two different machines against each other, "
    "never a future forecast. **Insufficient Evidence** and **Not Classified** are honest states, never fabricated "
    "as Stable or Degrading."
)

attention_tab, detail_tab = st.tabs(["Factory Attention Ranking", "Equipment Detail"])

all_rows = apd.get_overview()

with attention_tab:
    st.subheader("Asset Attention Ranking")
    st.caption(
        "Ranks equipment by degradation magnitude, persistence, criticality, and maintenance-effectiveness context "
        "ONLY - evidence quality is never part of this score. Equipment with genuinely insufficient evidence shows "
        "a distinct **Insufficient Evidence** state instead of a fabricated score - missing telemetry never makes "
        "an asset appear mechanically worse."
    )

    plants = apd.get_plants()
    plant_options = {p["code"].upper(): p["code"] for p in plants}
    plant_label = st.selectbox("Plant", ["All"] + list(plant_options), key="ap_attention_plant")
    plant_code = None if plant_label == "All" else plant_options[plant_label]

    ranking = apd.sort_attention_ranking(apd.get_attention_ranking(plant_code))

    if not ranking:
        st.info("No Asset Performance data has been persisted yet - the asset_performance_worker service persists an initial observation for every eligible dimension as its rotation reaches it.")
    else:
        ranked_count = sum(1 for r in ranking if r["attention_state"] == "RANKED")
        insufficient_count = len(ranking) - ranked_count
        kpi_cols = st.columns(3)
        kpi_cols[0].metric("Equipment Ranked", ranked_count)
        kpi_cols[1].metric("Insufficient Evidence", insufficient_count)
        kpi_cols[2].metric("Total Equipment", len(ranking))

        table_rows = []
        for r in ranking:
            table_rows.append({
                "Equipment": r["display_name"],
                "Plant": r["plant_code"].upper(),
                "Area": r["area_name"] or "-",
                "Criticality": r["criticality"] or "Unconfigured",
                "Attention": "Insufficient Evidence" if r["attention_state"] != "RANKED" else f"{r['attention_score']:.1f}",
                "Driving Dimension": apd.target_key_label(r["worst_dimension_target_key"]),
                "State": apd.state_label(r["worst_dimension_state"]),
                "Evidence": r["evidence_quality"] or "-",
                "Maintenance Context": apd.effectiveness_label(r["maintenance_effectiveness"]) if r["maintenance_effectiveness"] else "-",
            })
        df = pd.DataFrame(table_rows)
        st.dataframe(df, hide_index=True, width="stretch", height=420)
        st.caption(
            "Sorted by attention score (highest first), then Insufficient Evidence equipment listed separately - "
            "never interleaved as if directly comparable to a scored result."
        )

        st.divider()
        st.markdown("**Why this ranking - component breakdown**")
        ranked_only = [r for r in ranking if r["attention_state"] == "RANKED"]
        if ranked_only:
            label_by_instance = {r["instance_key"]: f"{r['display_name']} ({r['plant_code'].upper()})" for r in ranked_only}
            selected_instance = st.selectbox("Select equipment", list(label_by_instance), format_func=lambda k: label_by_instance[k], key="ap_attention_detail")
            selected = next(r for r in ranked_only if r["instance_key"] == selected_instance)
            comp_cols = st.columns(4)
            comp_cols[0].metric("Degradation", selected["degradation_component"])
            comp_cols[1].metric("Persistence", selected["persistence_component"])
            comp_cols[2].metric("Criticality", selected["criticality_component"])
            comp_cols[3].metric("Maintenance Context", selected["maintenance_ineffectiveness_component"])
            st.caption(selected["reason"])
            if selected["excluded_insufficient_dimension_count"]:
                st.caption(f"{selected['excluded_insufficient_dimension_count']} other dimension(s) for this equipment were excluded from this score - insufficient evidence, not counted as poor performance.")
        else:
            st.caption("No equipment currently has a ranked (evidence-adequate) attention score.")

with detail_tab:
    st.subheader("Equipment Detail")

    if not all_rows:
        st.info("No Asset Performance data has been persisted yet.")
        st.stop()

    filter_cols = st.columns(4)
    with filter_cols[0]:
        plant_options2 = {p["code"].upper(): p["code"] for p in apd.get_plants()}
        plant_label2 = st.selectbox("Plant", ["All"] + list(plant_options2), key="ap_detail_plant")
        plant_code2 = None if plant_label2 == "All" else plant_options2[plant_label2]
    with filter_cols[1]:
        filter_options = apd.get_filter_options(all_rows)
        type_options = {hd.equipment_type_label(t): t for t in filter_options["equipment_types"]}
        type_label = st.selectbox("Equipment type", ["All"] + list(type_options), key="ap_detail_type")
        equipment_type = None if type_label == "All" else type_options[type_label]
    with filter_cols[2]:
        state_label_choice = st.selectbox("State", ["All", "Improving", "Stable", "Degrading", "Significantly Degrading", "Insufficient Evidence", "Not Classified"], key="ap_detail_state")
    with filter_cols[3]:
        show_informational = st.checkbox("Show informational-only dimensions", value=False, key="ap_detail_informational")

    rows = [r for r in all_rows if plant_code2 is None or r["plant_code"] == plant_code2]
    rows = [r for r in rows if equipment_type is None or r["equipment_type"] == equipment_type]
    if state_label_choice != "All":
        wanted = state_label_choice.upper().replace(" ", "_").replace("(INFORMATIONAL)", "").strip()
        rows = [r for r in rows if r["performance_state"] == wanted]
    if not show_informational:
        rows = [r for r in rows if r["participates_in_degradation"]]

    summary = apd.summarize(all_rows)
    kpi_cols = st.columns(6)
    kpi_cols[0].metric("Improving", summary["improving"])
    kpi_cols[1].metric("Stable", summary["stable"])
    kpi_cols[2].metric("Degrading", summary["degrading"])
    kpi_cols[3].metric("Significantly Degrading", summary["significantly_degrading"])
    kpi_cols[4].metric("Insufficient Evidence", summary["insufficient_evidence"])
    kpi_cols[5].metric("Not Classified", summary["not_classified"])

    st.divider()

    if not rows:
        st.info("No dimensions match the current filters.")
        st.stop()

    st.caption(f"{len(rows)} matching dimension(s).")

    STATE_COLORS = {apd.state_label(s): f"background-color: {c}; color: #1a1a1a" for s, c in apd.STATE_BADGE_COLORS.items()}
    table_rows = []
    for r in rows:
        table_rows.append({
            "Equipment": r["display_name"],
            "Plant": r["plant_code"].upper(),
            "Type": hd.equipment_type_label(r["equipment_type"]),
            "Dimension": apd.target_key_label(r["target_key"]),
            "Reference": r["reference_value"],
            "Observed": r["observed_value"],
            "Change": f"{r['percent_change']:+.1f}%" if r["percent_change"] is not None else "Unavailable",
            "State": apd.state_label(r["performance_state"]),
            "Evidence": r["evidence_quality"],
            "Sustained": "Yes" if r["sustained_degradation"] else "-",
            "Last Assessed": hd.format_timestamp(r["computed_at"]),
        })
    df = pd.DataFrame(table_rows)

    def _highlight(row: pd.Series) -> list[str]:
        color = STATE_COLORS.get(row["State"], "")
        return [color] * len(row)

    st.dataframe(df.style.apply(_highlight, axis=1), hide_index=True, width="stretch", height=420)

    st.divider()
    st.markdown("**Dimension detail**")
    label_by_key = {
        f"{r['instance_key']}||{r['target_key']}": f"{r['display_name']} - {apd.target_key_label(r['target_key'])} [{apd.state_label(r['performance_state'])}]"
        for r in rows
    }
    selected_key = st.selectbox("Select a dimension to inspect", list(label_by_key), format_func=lambda k: label_by_key[k], key="ap_detail_select")
    selected_instance_key, selected_target_key = selected_key.split("||")
    selected_row = next(r for r in rows if r["instance_key"] == selected_instance_key and r["target_key"] == selected_target_key)

    detail = apd.get_detail(selected_row["plant_id"], selected_instance_key, selected_target_key)
    if detail is None:
        st.warning("No persisted observation found for this dimension.")
        st.stop()

    latest = detail["latest"]
    st.markdown(f"### {detail['context'].get('display_name') or selected_instance_key} - {apd.target_key_label(selected_target_key)}")

    dimension_def = None
    from engine.performance_targets import PERFORMANCE_DIMENSION_REGISTRY
    dimension_def = PERFORMANCE_DIMENSION_REGISTRY.get(f"{selected_row['equipment_type']}|{selected_target_key}")
    if dimension_def:
        st.caption(dimension_def.description)

    score_cols = st.columns(4)
    score_cols[0].markdown(f"**State**  \n{apd.state_badge_html(latest['performance_state'])}", unsafe_allow_html=True)
    score_cols[1].markdown(f"**Reference**  \n{latest['reference_value']:.2f} {latest['unit'] or ''}" if latest["reference_value"] is not None else "**Reference**  \nUnavailable")
    score_cols[2].markdown(f"**Observed**  \n{latest['observed_value']:.2f} {latest['unit'] or ''}" if latest["observed_value"] is not None else "**Observed**  \nUnavailable")
    score_cols[3].markdown(f"**Change**  \n{latest['percent_change']:+.1f}%" if latest["percent_change"] is not None else "**Change**  \nUnavailable")

    st.caption(f"Evidence: {latest['evidence_quality']} · {latest['sample_count']} recent / {latest['reference_sample_count']} reference comparable sample(s) · Direction: {latest['direction']}")

    # Phase 15 - one lightweight Ask AI entry point (approved cross-page
    # integration set).
    if st.button("💬 Ask AI about this equipment", key=f"ask_ai_link_{selected_instance_key}"):
        st.switch_page(
            "pages/1_Ask_AI.py",
            query_params={
                "ask_equipment": selected_instance_key,
                "ask_label": detail["context"].get("display_name") or selected_instance_key,
            },
        )

    if latest["sustained_degradation"]:
        st.warning(f"Sustained degradation - {latest['consecutive_degrading_observations']} consecutive degrading observations.")
    st.markdown(f"**Explanation:** {latest['reason']}")
    if latest["limitations"]:
        st.caption("Limitations: " + " ".join(latest["limitations"]))

    st.divider()
    st.markdown("**History - reference vs. observed**")
    history = detail["history"]
    if len(history) >= 2:
        history_df = pd.DataFrame([
            {"computed_at": h["computed_at"], "Reference": h["reference_value"], "Observed": h["observed_value"]}
            for h in history
        ]).set_index("computed_at")
        st.line_chart(history_df, height=280)
        st.caption("Backward-looking only - no projected/forecast line is ever drawn beyond the last real observation.")
    else:
        st.caption("Not enough persisted history yet to chart a trend (this is the only recorded observation so far).")

    maintenance_comparisons = detail["maintenance_comparisons"]
    if maintenance_comparisons:
        st.divider()
        st.markdown("**Maintenance before/after**")
        for comparison in maintenance_comparisons:
            pre_text = "Unavailable" if comparison["pre_value"] is None else f"{comparison['pre_value']:.2f}"
            post_text = "Unavailable" if comparison["post_value"] is None else f"{comparison['post_value']:.2f}"
            st.markdown(
                f"Maintenance performed {hd.format_timestamp(comparison['performed_at'])}: "
                f"**{apd.effectiveness_label(comparison['effectiveness_result'])}** "
                f"(pre {pre_text} → post {post_text}, {comparison['evidence_quality']} evidence)"
            )
            st.caption(comparison["reason"])

    st.caption(
        "Every figure above is read directly from a persisted Phase 14 observation - never recalculated in "
        "Streamlit, never a prediction, never a diagnosis. Reference and observed values are both robust, "
        "context-matched medians from Phase 8's own baseline engine."
    )
