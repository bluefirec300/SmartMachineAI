from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui import data_health_data as dhd
from ui import health_data as hd
from ui import maintenance_intelligence_data as mid
from ui.csv_export import dataframe_to_csv_bytes
from ui.csv_export import export_filename as export_csv_filename
from ui.pdf_export import build_pdf_report
from ui.pdf_export import export_filename as export_pdf_filename


st.title("Equipment Health")
st.caption(
    "Deterministic engineering prioritization (Phase 12.1-12.2 - no LLM anywhere on this page). "
    "**Health Score** answers 'how healthy does available evidence indicate this equipment is?' - "
    "**Assessment Confidence** answers 'how much evidence do we currently have?' These are always shown "
    "separately, never combined into one percentage. A high score with LOW confidence means 'appears healthy, "
    "but on limited evidence' - not 'definitely healthy'. Equipment with INSUFFICIENT evidence shows no "
    "numeric score at all - never a fabricated 100."
)

all_rows = hd.get_overview()

if not all_rows:
    st.info("No equipment health assessments have been recorded yet - the equipment_health_worker service "
            "persists an initial assessment for every eligible equipment on its first run.")
    st.stop()

# Phase 12.4 - reuses the ONE shared color contract (ui.health_data.BAND_BADGE_COLORS)
# rather than a second hardcoded copy; derived into the CSS-declaration
# format pandas' Styler.apply() needs for row-tinting specifically.
BAND_COLORS = {band: f"background-color: {hex_color}; color: #1a1a1a" for band, hex_color in hd.BAND_BADGE_COLORS.items()}

_score_text = hd.score_text
_state_text = hd.state_text
_confidence_cell = hd.confidence_text
_state_badge = hd.state_badge_html


plants = hd.get_plants()
filter_options = hd.get_filter_options(all_rows)

filter_cols = st.columns(6)
with filter_cols[0]:
    plant_options = {p["code"].upper(): p["code"] for p in plants}
    plant_label = st.selectbox("Plant", ["All"] + list(plant_options))
    plant_code = None if plant_label == "All" else plant_options[plant_label]
with filter_cols[1]:
    area = st.selectbox("Area", ["All"] + filter_options["areas"])
with filter_cols[2]:
    system = st.selectbox("System", ["All"] + filter_options["systems"])
with filter_cols[3]:
    type_options = {hd.equipment_type_label(t): t for t in filter_options["equipment_types"]}
    type_label = st.selectbox("Equipment type", ["All"] + list(type_options))
    equipment_type = None if type_label == "All" else type_options[type_label]
with filter_cols[4]:
    band_label = st.selectbox("Health state", ["All", "Healthy", "Monitor", "Attention", "Investigate", "Insufficient data"])
with filter_cols[5]:
    confidence_label = st.selectbox("Confidence", ["All", "High", "Medium", "Low", "Insufficient"])

sort_label = st.radio("Sort by", ["Lowest health score first", "Most recently changed first", "Equipment name"], horizontal=True)

rows = [r for r in all_rows if plant_code is None or r["plant_code"] == plant_code]
rows = [r for r in rows if area == "All" or r["area_name"] == area]
rows = [r for r in rows if system == "All" or r["system_name"] == system]
rows = [r for r in rows if equipment_type is None or r["equipment_type"] == equipment_type]
if band_label == "Insufficient data":
    rows = [r for r in rows if r["health_score"] is None]
elif band_label != "All":
    rows = [r for r in rows if r["health_band"] == band_label.upper()]
if confidence_label != "All":
    rows = [r for r in rows if r["assessment_confidence"] == confidence_label.upper()]

# --- KPI summary (item 1) - computed over the FULL unfiltered set, so
# filtering the table never changes the factory-wide overview numbers. ---
summary = hd.summarize(all_rows)
kpi_cols = st.columns(6)
kpi_cols[0].metric("Total Monitored", summary["total"])
kpi_cols[1].metric("Healthy", summary["healthy"])
kpi_cols[2].metric("Monitor", summary["monitor"])
kpi_cols[3].metric("Attention", summary["attention"])
kpi_cols[4].metric("Investigate", summary["investigate"])
kpi_cols[5].metric("Insufficient Data", summary["insufficient"])

avg_cols = st.columns(2)
if summary["average_score"] is not None:
    avg_cols[0].metric(
        "Average Health Score",
        summary["average_score"],
        help=f"Mean of the {summary['average_score_count']} equipment with a real numeric score - "
             f"excludes the {summary['insufficient']} INSUFFICIENT equipment entirely (never averaged in as 0 or 100).",
    )
else:
    avg_cols[0].metric("Average Health Score", "Unavailable")
conf = summary["confidence_counts"]
avg_cols[1].markdown(
    f"**Assessment confidence:** {conf['HIGH']} High &nbsp;·&nbsp; {conf['MEDIUM']} Medium &nbsp;·&nbsp; "
    f"{conf['LOW']} Low &nbsp;·&nbsp; {conf['INSUFFICIENT']} Insufficient"
)
st.caption("Confidence reflects how much evidence supports each assessment - a separate dimension from the health score itself, never physical condition.")

st.divider()

if not rows:
    st.info("No equipment matches the current filters.")
    st.stop()

st.caption(f"{len(rows)} matching equipment.")


def _sort_key_lowest_score(r):
    return (r["health_score"] is None, r["health_score"] if r["health_score"] is not None else 0)


if sort_label == "Lowest health score first":
    rows = sorted(rows, key=_sort_key_lowest_score)
elif sort_label == "Most recently changed first":
    rows = sorted(rows, key=lambda r: r["computed_at"], reverse=True)
else:
    rows = sorted(rows, key=lambda r: r["display_name"])

table_rows = []
for r in rows:
    table_rows.append({
        "Equipment": r["display_name"],
        "Plant": r["plant_code"].upper(),
        "Area": r["area_name"] or "-",
        "System": r["system_name"] or "-",
        "Type": hd.equipment_type_label(r["equipment_type"]),
        "Health Score": _score_text(r),
        "Health State": _state_text(r),
        "Confidence": _confidence_cell(r),
        "Last Assessed": hd.format_timestamp(r["computed_at"]),
        "Recent Movement": hd.TREND_LABELS.get(r["trend"], r["trend"]),
        "Primary Factor": hd.factor_label(r["dominant_factor_id"]),
    })

df = pd.DataFrame(table_rows)


def _highlight(row: pd.Series) -> list[str]:
    color = BAND_COLORS.get(row["Health State"], "")
    return [color] * len(row)


st.dataframe(
    df.style.apply(_highlight, axis=1),
    width="stretch",
    height=450,
    column_config={
        "Equipment": st.column_config.TextColumn("Equipment", width="medium"),
        "Last Assessed": st.column_config.TextColumn("Last Assessed", width="medium"),
        "Primary Factor": st.column_config.TextColumn("Primary Factor", width="medium"),
    },
)

# Later-optional-work item (Phase V2.9 follow-up) - CSV/PDF export of
# the fleet-wide table above, same pattern as Event Records/Energy
# Dashboard (Phase V2.6). Exports exactly `df` as already built/
# filtered/sorted above - no recomputation. The per-equipment detail
# section below is interactive drill-down, not tabular in the same
# way, and stays out of scope for this export (matches how Event
# Records' PDF only ever covers its own table, not every expander on
# the page).
report_parameters = {
    "Plant": plant_label,
    "Area": area,
    "System": system,
    "Equipment type": type_label,
    "Health state": band_label,
    "Confidence": confidence_label,
    "Sort": sort_label,
    "Matching equipment": str(len(rows)),
}

download_cols = st.columns(2)

with download_cols[0]:
    st.download_button(
        "⬇️ Download CSV",
        data=dataframe_to_csv_bytes(df),
        file_name=export_csv_filename("equipment_health"),
        mime="text/csv",
        help="Exports exactly the rows shown above, with the current filters applied.",
    )

with download_cols[1]:
    st.download_button(
        "⬇️ Download PDF",
        data=build_pdf_report(
            "Equipment Health Report",
            report_parameters,
            df,
            highlight_column="Health State",
            highlight_colors=hd.BAND_BADGE_COLORS,
        ),
        file_name=export_pdf_filename("equipment_health"),
        mime="application/pdf",
        help="Printable report with the active filters shown at the top, row-shaded by the same "
        "health-state colors used on screen.",
    )

st.divider()
st.subheader("Equipment detail")

label_by_key = {
    r["instance_key"]: f"[{_state_text(r)}] {r['plant_code'].upper()} - {r['display_name']} ({_score_text(r)})"
    for r in rows
}

# --- Deep-link support (item 5) - a page with a pure-Streamlit
# (non-JS-sandboxed) equipment selector, e.g. Service & Maintenance,
# may set ?equipment_id=N and st.switch_page() here to pre-select that
# equipment. Only used to pick a default index - never bypasses the
# filters above, so a linked equipment outside the current filter view
# is honestly reported rather than silently forcing filters to change.
default_index = 0
linked_equipment_id = st.query_params.get("equipment_id")
if linked_equipment_id:
    try:
        linked_equipment_id = int(linked_equipment_id)
        keys = list(label_by_key)
        match_index = next((i for i, r in enumerate(rows) if r["equipment_id"] == linked_equipment_id), None)
        if match_index is not None:
            default_index = match_index
        else:
            st.info("The equipment linked from another page isn't in the current filtered view - adjust the filters above or pick it directly below.")
    except (TypeError, ValueError):
        pass

selected_key = st.selectbox(
    "Select equipment to inspect its full factor breakdown and history",
    list(label_by_key), index=default_index, format_func=lambda k: label_by_key[k],
)
selected_summary = next(r for r in rows if r["instance_key"] == selected_key)
detail = hd.get_detail(selected_summary["plant_id"], selected_key)

if detail is None:
    st.warning("No assessment found for this equipment.")
    st.stop()

latest = detail["latest"]
context = detail["context"]
readable_type = hd.equipment_type_label(selected_summary["equipment_type"])

# --- Identity header (item 3) - markdown, not st.metric, so long
# engineering identifiers (equipment name, area, system) wrap instead
# of truncating with "...". ---
st.markdown(f"### {context.get('display_name') or selected_key}")
identity_cols = st.columns(3)
identity_cols[0].markdown(f"**Plant / Area**  \n{selected_summary['plant_code'].upper()} / {context.get('area_name') or '-'}")
identity_cols[1].markdown(f"**System / Type**  \n{context.get('system_name') or '-'} / {readable_type}")
criticality_text = context.get("criticality") or "Unconfigured"
identity_cols[2].markdown(f"**Criticality**  \n{criticality_text}")
if not context.get("criticality"):
    identity_cols[2].caption("Not configured - does not affect the Health Score.")

# --- Phase 13 cross-reference (item 27) - Maintenance Priority is a
# SEPARATE, deterministic conclusion that combines this Health
# assessment with the maintenance schedule - never recomputed or
# reinterpreted here, only read via the shared engine. Informational
# only - never changes anything on this page. ---
priority_cols = st.columns([3, 1])
with priority_cols[0]:
    priority_context = mid.compact_priority_context(
        selected_summary["plant_id"], selected_summary["plant_code"], selected_summary["equipment_type"], selected_key,
    )
    st.markdown(
        f"**Maintenance Priority:** {priority_context['priority_label']} &nbsp;·&nbsp; "
        f"**Confidence:** {priority_context['confidence']}"
        + (f" &nbsp;·&nbsp; *{priority_context['floor_reason']}*" if priority_context["floor_applied"] else "")
    )
with priority_cols[1]:
    if st.button("View Maintenance Intelligence →", key=f"mi_link_{selected_key}"):
        st.switch_page("pages/4_Service_and_Maintenance.py")

# Phase 15 - one lightweight Ask AI entry point (approved cross-page
# integration set). Carries the resolved instance_key via
# st.switch_page()'s own query_params argument - read back by
# ui/pages/1_Ask_AI.py, never re-guessed by name matching there.
if st.button("💬 Ask AI about this equipment", key=f"ask_ai_link_{selected_key}"):
    st.switch_page(
        "pages/1_Ask_AI.py",
        query_params={"ask_equipment": selected_key, "ask_label": context.get("display_name") or selected_key},
    )

# ---------------------------------------------------------------------------
# Phase 16.2 - Data Health section. DELIBERATELY separate from Equipment
# Health below (item E): Equipment Health asks "is the machine/process
# healthy?" - Data Health asks "can the telemetry feeding that assessment
# actually be trusted right now?" A mechanically unhealthy piece of
# equipment can have GOOD data health, and a mechanically fine one can
# have POOR data health - this section never implies otherwise. Evaluated
# ONCE per page render for the selected equipment only (item Q) - no
# historian scan, no per-row queries, no repeated evaluation.
# ---------------------------------------------------------------------------
st.divider()
st.subheader("📡 Data Health")
st.caption(
    "Deterministic (Phase 16.1 - no LLM anywhere in this section, not AI-generated). Answers a DIFFERENT "
    "question than Equipment Health below: not 'is the equipment healthy', but 'can the telemetry feeding "
    "this and every other analysis on this equipment currently be trusted'. These are independent findings."
)

dh_result = dhd.get_equipment_data_health(selected_key)

data_health_cols = st.columns([1, 3])
with data_health_cols[0]:
    st.metric("Data Confidence", dh_result["confidence_score_text"])
    st.markdown(dhd.status_badge_html(dh_result["confidence_status"]), unsafe_allow_html=True)
with data_health_cols[1]:
    if dh_result["reasons"]:
        st.markdown("**Why this confidence:**")
        for reason in dh_result["reasons"]:
            st.caption(f"- {reason}")
    st.caption(
        f"Source: {dh_result['source_label']} - {dh_result['source_note']}"
        if dh_result["source_note"] else f"Source: {dh_result['source_label']}"
    )

with st.expander("How Data Confidence is calculated"):
    st.markdown(
        "A deterministic score (0-100, never AI-generated) combining four independently measured components:\n\n"
        "- **35% Freshness** - is the latest sample recent enough for each required tag's own configured logging cadence?\n"
        "- **25% Availability** - has each required tag ever produced a value at all?\n"
        "- **25% Validity** - is the latest value a real, finite number (never NULL/NaN/infinite/malformed)?\n"
        "- **15% Continuity** - are there abnormal gaps in the recent sampling history?\n\n"
        "A component that genuinely does not apply to a tag's logging mode (e.g. Continuity for a "
        "change-only tag) is shown as N/A, never a fabricated score - and when Data Confidence itself "
        "cannot be legitimately calculated (no telemetry to evaluate), it shows **Not Available**, never a "
        "fabricated 0."
    )
    component_cols = st.columns(4)
    for index, component_name in enumerate(("freshness", "availability", "validity", "continuity")):
        component_cols[index].metric(component_name.capitalize(), dh_result["component_display"][component_name])

st.markdown("**Required telemetry summary**")
telemetry_cols = st.columns(4)
telemetry_cols[0].metric("Required Tags", dh_result["required_tag_count"])
telemetry_cols[1].metric("Available", dh_result["available_tag_count"])
telemetry_cols[2].metric("Fresh", dh_result["fresh_tag_count"])
telemetry_cols[3].metric("Stale", len(dh_result["stale_tags"]))

issue_lines = []
if dh_result["missing_tags"]:
    issue_lines.append(f"Missing (never reported any value): {', '.join(dh_result['missing_tags'])}")
if dh_result["invalid_tags"]:
    issue_lines.append(f"Invalid latest value: {', '.join(dh_result['invalid_tags'])}")
if dh_result["gaps"]:
    gap_tags = ", ".join(sorted({g["tag"] for g in dh_result["gaps"]}))
    issue_lines.append(f"{len(dh_result['gaps'])} sampling gap(s) detected: {gap_tags}")
if dh_result["timestamp_issues"]:
    ts_tags = ", ".join(sorted({i["tag"] for i in dh_result["timestamp_issues"]}))
    issue_lines.append(f"{len(dh_result['timestamp_issues'])} timestamp issue(s) (future timestamp or non-monotonic): {ts_tags}")
if dh_result["frozen_candidates"]:
    issue_lines.append(
        f"Frozen candidate (advisory only - NOT a confirmed sensor fault): {', '.join(dh_result['frozen_candidates'])}"
    )

for line in issue_lines:
    st.caption(f"⚠️ {line}")

with st.expander(f"Tag-level detail ({len(dh_result['tags'])} required tag(s))"):
    if not dh_result["tags"]:
        st.caption(
            dh_result["limitations"][0] if dh_result["limitations"]
            else "No required-tag telemetry registry applies to this equipment."
        )
    else:
        detail_df = pd.DataFrame([
            {
                "Tag": tag["tag_name"],
                "Description": tag["description"],
                "Latest Value": tag["last_value"],
                "Last Update": tag["last_time"],
                "Age": tag["age"],
                "Logging": f"{tag['logging_mode']} ({tag['logging_interval']})",
                "Freshness": tag["freshness"],
                "Availability": tag["availability"],
                "Validity": tag["validity"],
                "Continuity": tag["continuity"],
                "Frozen Candidate": tag["frozen_candidate"],
                "Timestamp Issue": tag["timestamp_issue"],
            }
            for tag in dh_result["tags"]
        ])
        st.dataframe(detail_df, width="stretch")

        if dh_result["frozen_candidates"]:
            st.caption(
                "**Frozen candidate** means a continuous measurement has remained unchanged for an unusually "
                "long time while related telemetry on the same equipment continues updating normally. This is "
                "advisory evidence only - Phase 16.1 does not diagnose this as a confirmed sensor fault, and it "
                "does not affect the Data Confidence score above."
            )
        if dh_result["indeterminate_freshness_tags"]:
            st.caption(
                "**Indeterminate freshness** applies to tags that only log when their value changes - the "
                "absence of a recent row does not prove the tag has stopped communicating, so it is never "
                "displayed as Stale."
            )

for limitation in dh_result["limitations"]:
    st.caption(f"ℹ️ {limitation}")

st.divider()

score_cols = st.columns(4)
score_cols[0].metric("Health Score", _score_text(selected_summary), help="0-100, higher is better. An engineering prioritization signal - not a failure prediction.")
score_cols[1].markdown(f"**Health State**  \n{_state_badge(_state_text(selected_summary))}", unsafe_allow_html=True)
score_cols[2].markdown(f"**Assessment Confidence**  \n{_confidence_cell(selected_summary)}")
if latest["assessment_confidence"] == "LOW":
    score_cols[2].caption("Limited supporting history/evidence - score is provisional.")
score_cols[3].markdown(f"**Last Assessed**  \n{hd.format_timestamp(latest['computed_at'])}")

st.markdown(
    f"**Coverage:** {latest['usable_factor_count']} of {latest['applicable_factor_count']} applicable factors usable"
)
st.caption(
    "Coverage and Assessment Confidence are related but not identical - full coverage does not by itself mean "
    "HIGH confidence, since the historical depth/context quality behind each factor is also weighed."
)
st.caption(f"Model version: `{latest['health_model_version']}` - Change reason for this snapshot: `{latest['change_reason']}`")
if latest["missing_factors"]:
    st.caption(f"Factors without usable evidence: {', '.join(hd.factor_label(f) for f in latest['missing_factors'])}")

# --- Factor breakdown (item 3/10) - the actual reconciliation, not a summary. ---
st.divider()
st.markdown("**Why this score - factor breakdown**")

penalized = sorted([f for f in detail["factors"] if f["status"] == "penalized"], key=lambda f: f["penalty"], reverse=True)
clean = [f for f in detail["factors"] if f["status"] == "clean"]
missing = [f for f in detail["factors"] if f["status"] == "missing"]

if selected_summary["health_score"] is None:
    st.warning(
        "No numeric Health Score is shown - too few applicable factors currently have usable evidence "
        f"({latest['usable_factor_count']} of {latest['applicable_factor_count']}). This is never presented as "
        "either a healthy or an unhealthy result - it means the assessment cannot be defensibly made yet."
    )
else:
    lines = ["Starting condition: **100**", ""]
    for f in penalized:
        lines.append(f"- {hd.factor_label(f['factor_id'])}: **-{f['penalty']:.2f}**  \n  *({hd.family_label(f['factor_family'])} factor)*")
    lines.append("")
    lines.append(f"Final Health Score: **{selected_summary['health_score']:.1f}**")
    st.markdown("\n".join(lines))
    if not penalized:
        st.caption("No factor currently contributes a penalty - all usable evidence is clean.")

with st.expander(f"Technical detail - all {len(detail['factors'])} factors (canonical IDs, evidence, provenance)"):
    for f in penalized + clean + missing:
        icon = {"penalized": "🔶", "clean": "✅", "missing": "⚪"}[f["status"]]
        st.markdown(
            f"{icon} **{hd.factor_label(f['factor_id'])}** - penalty {f['penalty']:.2f} / max {f['maximum_penalty']:.1f}  \n"
            f"`{f['factor_id']}` &nbsp;·&nbsp; Family: {hd.family_label(f['factor_family'])} ({f['factor_family']}) &nbsp;·&nbsp; Status: {f['status']}"
        )
        st.caption(
            f"{f['reason']}  \n"
            f"Source: {f['evidence_source']} - Value: {f['evidence_value'] or '-'} - "
            f"Evidence timestamp: {hd.format_timestamp(f['evidence_timestamp'])} - Confidence: {f['evidence_confidence'] or '-'} - "
            f"Provenance: {f['provenance']}"
        )

# --- Grounded change explanation (item 5/11) ---
st.divider()
st.markdown("**What changed recently**")
change = detail["change_explanation"]
if change is None:
    st.caption("Not enough history yet to explain a change - this is the only (or first) recorded assessment.")
else:
    from_text = "Unavailable" if change["from_score"] is None else f"{change['from_score']:.1f}"
    to_text = "Unavailable" if change["to_score"] is None else f"{change['to_score']:.1f}"
    st.markdown(
        f"Score changed from **{from_text}** ({hd.format_timestamp(change['from_computed_at'])}) to "
        f"**{to_text}** ({hd.format_timestamp(change['to_computed_at'])})."
    )
    if change["factor_deltas"]:
        for d in change["factor_deltas"][:5]:
            st.markdown(f"- {hd.format_change_delta(d)}")
            st.caption(f"`{d['factor_id']}` ({d['from_status']} -> {d['to_status']}) - {d['reason']}")
    else:
        st.caption("No individual factor's penalty changed by a meaningful amount between these two assessments.")

# --- History (item 4/13) - score and confidence kept visually and semantically separate. ---
st.divider()
st.markdown("**Health Score history**")
trend = detail["trend"]
st.caption(
    f"Recent Movement: **{hd.TREND_LABELS.get(trend['direction'], trend['direction'])}** "
    + (f"({trend['previous_score']:.1f} -> {trend['latest_score']:.1f}, "
       f"{hd.format_timestamp(trend['observation_period_start'])} to {hd.format_timestamp(trend['observation_period_end'])})"
       if trend["direction"] not in ("INSUFFICIENT_HISTORY",) else "")
    + " - a descriptive comparison of historical health assessments only, never a prediction of future condition."
)

history_df = pd.DataFrame(
    [{"computed_at": h["computed_at"], "Health Score": h["health_score"]} for h in detail["history"]]
).set_index("computed_at")
if history_df["Health Score"].notna().any():
    st.line_chart(history_df, height=250)
    st.caption("Health bands for reference: 85-100 Healthy · 70-84 Monitor · 50-69 Attention · 0-49 Investigate. Gaps in the line mean no numeric score was available for that assessment (INSUFFICIENT coverage) - never interpolated.")
else:
    st.caption("No scored history yet to chart (all recorded assessments are INSUFFICIENT so far).")

conf_col, band_col = st.columns(2)
with conf_col:
    st.markdown("**Confidence history**")
    if detail["confidence_transitions"]:
        for t in detail["confidence_transitions"]:
            st.caption(f"{hd.format_timestamp(t['at'])}: {t['from_confidence']} -> {t['to_confidence']}")
    else:
        st.caption("No confidence change recorded in this window - confidence improving does not mean the equipment itself improved.")
with band_col:
    st.markdown("**Health state history**")
    if detail["band_transitions"]:
        for t in detail["band_transitions"]:
            st.caption(f"{hd.format_timestamp(t['at'])}: {t['from_band']} -> {t['to_band']}")
    else:
        st.caption("No health-state change recorded in this window.")

st.caption(
    "No AI-generated text appears on this page. Every figure above is read directly from the persisted "
    "Phase 12.2 assessment history - never recalculated in Streamlit, never a prediction, never a diagnosis."
)
