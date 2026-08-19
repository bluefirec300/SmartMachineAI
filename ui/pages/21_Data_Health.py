from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui import data_health_fleet_data as dhfd
from ui import data_health_history_data as dhhd
from ui.data_health_data import status_badge_html

"""
Phase 16.4 - factory-wide Data Health overview. This is a TELEMETRY
TRUSTWORTHINESS view, not Equipment Health/Maintenance Priority/Asset
Performance/PLC alarm state/connection health/sensor-failure diagnosis
- see the page caption below and this phase's completion report for the
full boundary. Every number on this page comes from
engine.data_health_fleet.calculate_fleet_data_health(), which itself
only aggregates the unchanged Phase 16.1 per-equipment engine - nothing
is recalculated here.
"""

st.title("Data Health")
st.caption(
    "Deterministic telemetry-trustworthiness overview (Phase 16.1/16.4 - no LLM, no prediction anywhere on this "
    "page). Answers 'which equipment's telemetry can currently be trusted', NOT 'which equipment is mechanically "
    "healthy' - a machine can be perfectly fine with POOR Data Health (its sensors are the problem), and a "
    "genuinely faulty machine can have GOOD Data Health (its sensors are working perfectly). See the Equipment "
    "Health page for machine condition."
)

if "dh_fleet_refresh_requested" not in st.session_state:
    st.session_state["dh_fleet_refresh_requested"] = False

top_cols = st.columns([3, 1])
with top_cols[1]:
    if st.button("🔄 Refresh now", help="Recomputes Data Health for every equipment instance - this is the same cost as the automatic refresh, just requested early."):
        dhfd.force_refresh()

data = dhfd.get_fleet_data_health()

with top_cols[0]:
    st.caption(
        f"As of {data['as_of']} - refreshed automatically at most every {dhfd.FLEET_CACHE_TTL_SECONDS // 60} "
        f"minute(s); this snapshot may be up to that old. Data Health does not require second-by-second freshness."
    )

if data["failures"]:
    st.warning(
        f"{len(data['failures'])} equipment instance(s) could not be evaluated and are shown as UNAVAILABLE below "
        f"(never silently dropped, never assumed GOOD): "
        + ", ".join(f["instance_key"] for f in data["failures"])
    )

if data["equipment_count"] == 0:
    st.info("No equipment is currently eligible for Data Health evaluation.")
    st.stop()

# --- Fleet-wide distribution (item 6) - counts only, deliberately NEVER
# averaged into one "Factory Data Confidence" number, which would mask
# individual bad equipment behind good ones. ---
st.subheader("Fleet Data Confidence Distribution")
dist_cols = st.columns(5)
dist_cols[0].metric("Equipment Evaluated", data["equipment_count"])
dist_cols[1].metric("GOOD", data["good_count"])
dist_cols[2].metric("DEGRADED", data["degraded_count"])
dist_cols[3].metric("POOR", data["poor_count"])
dist_cols[4].metric("UNAVAILABLE", data["unavailable_count"])

issue_cols = st.columns(4)
issue_cols[0].metric("Missing Tags", data["missing_tag_count"])
issue_cols[1].metric("Stale Tags", data["stale_tag_count"])
issue_cols[2].metric("Invalid Tags", data["invalid_tag_count"])
issue_cols[3].metric("Continuity Gaps", data["gap_count"])

# --- Filters ---
options = dhfd.filter_options(data["equipment"])
filter_cols = st.columns(5)
with filter_cols[0]:
    plant_label = st.selectbox("Plant", ["All"] + [p.upper() for p in options["plants"]])
    plant_code = None if plant_label == "All" else plant_label.lower()
with filter_cols[1]:
    area = st.selectbox("Area", ["All"] + options["areas"])
with filter_cols[2]:
    system = st.selectbox("System", ["All"] + options["systems"])
with filter_cols[3]:
    type_options = {r["equipment_type_label"]: r["equipment_type"] for r in data["equipment"]}
    type_label = st.selectbox("Equipment type", ["All"] + sorted(type_options))
    equipment_type = None if type_label == "All" else type_options[type_label]
with filter_cols[4]:
    status_filter = st.selectbox("Data Confidence status", ["All", "GOOD", "DEGRADED", "POOR", "UNAVAILABLE"])

rows = data["equipment"]
rows = [r for r in rows if plant_code is None or r["plant_code"] == plant_code]
rows = [r for r in rows if area == "All" or r["area_name"] == area]
rows = [r for r in rows if system == "All" or r["system_name"] == system]
rows = [r for r in rows if equipment_type is None or r["equipment_type"] == equipment_type]
rows = [r for r in rows if status_filter == "All" or r["confidence_status"] == status_filter]

overview_tab, hierarchy_tab, issue_tab, history_tab, recurring_tab = st.tabs([
    "Equipment Overview", "Plant / Area / System Grouping", "Issue View", "History & Trends", "Recurring Issues",
])

# ---------------------------------------------------------------------------
# Equipment Overview - already sorted worst-first by
# engine.data_health_fleet.attention_sort_key() (item 10).
# ---------------------------------------------------------------------------
with overview_tab:
    st.caption(
        f"Showing {len(rows)} of {data['equipment_count']} equipment, sorted worst-first: UNAVAILABLE, then POOR, "
        "then DEGRADED, then GOOD - within the same status, lowest Data Confidence score first, then largest "
        "missing/invalid/timestamp/gap/stale issue count. A deterministic sort order, not a new priority score."
    )

    table_df = pd.DataFrame([
        {
            "Equipment": r["display_name"],
            "Plant": (r["plant_code"] or "-").upper(),
            "Area": r["area_name"] or "-",
            "System": r["system_name"] or "-",
            "Type": r["equipment_type_label"],
            "Data Confidence": r["confidence_score_text"],
            "Status": r["confidence_status"],
            "Freshness": r["component_display"]["freshness"],
            "Availability": r["component_display"]["availability"],
            "Validity": r["component_display"]["validity"],
            "Continuity": r["component_display"]["continuity"],
            "Required": r["required_tag_count"],
            "Available": r["available_tag_count"],
            "Missing": len(r["missing_tags"]),
            "Stale": len(r["stale_tags"]),
            "Invalid": len(r["invalid_tags"]),
            "Gaps": len(r["gaps"]),
            "Frozen Candidates": len(r["frozen_candidates"]),
            "Source": r["source_driver"] or "Unknown",
            "As Of": r["as_of"],
        }
        for r in rows
    ])
    st.dataframe(table_df, width="stretch", hide_index=True)
    st.caption(
        "Source identifies the configured data source only and is not a live connection-health indication."
    )

    st.markdown("**Drill down**")
    label_by_key = {r["instance_key"]: f"{r['display_name']} ({r['instance_key']})" for r in rows}
    if label_by_key:
        selected_key = st.selectbox("Select equipment for full Data Health detail", list(label_by_key), format_func=lambda k: label_by_key[k])
        selected_row = next(r for r in rows if r["instance_key"] == selected_key)
        st.markdown(status_badge_html(selected_row["confidence_status"]), unsafe_allow_html=True)
        if selected_row["equipment_id"] is not None:
            if st.button("View full Data Health detail on Equipment Health page →"):
                st.switch_page("pages/19_Equipment_Health.py", query_params={"equipment_id": selected_row["equipment_id"]})
        else:
            st.caption("This equipment could not be resolved to a configured equipment record, so a detail link is not available.")
            if selected_row["evaluation_error"]:
                st.caption(f"Reason: {selected_row['evaluation_error']}")

# ---------------------------------------------------------------------------
# Plant / Area / System / Equipment Type grouping (item 9) - transparent
# counts only, no artificial ranking beyond the group's own POOR+UNAVAILABLE
# count (a clearly-defined rule, not a hidden score).
# ---------------------------------------------------------------------------
with hierarchy_tab:
    def _grouped_table(groups: dict[str, dict[str, int]], label: str) -> pd.DataFrame:
        table = pd.DataFrame([
            {
                label: key,
                "Equipment": counts["equipment"],
                "GOOD": counts["GOOD"],
                "DEGRADED": counts["DEGRADED"],
                "POOR": counts["POOR"],
                "UNAVAILABLE": counts["UNAVAILABLE"],
                "Missing Tags": counts["missing_tags"],
                "Stale Tags": counts["stale_tags"],
                "Invalid Tags": counts["invalid_tags"],
                "Continuity Gaps": counts["gaps"],
                "_needs_attention": counts["POOR"] + counts["UNAVAILABLE"],
            }
            for key, counts in groups.items()
        ])
        if table.empty:
            return table
        return table.sort_values("_needs_attention", ascending=False).drop(columns="_needs_attention")

    st.caption("Sorted by POOR + UNAVAILABLE equipment count (a transparent, explicitly-defined rule - not a hidden ranking).")

    st.markdown("**By Plant**")
    st.dataframe(_grouped_table(data["by_plant"], "Plant"), width="stretch", hide_index=True)
    st.markdown("**By Area**")
    st.dataframe(_grouped_table(data["by_area"], "Area"), width="stretch", hide_index=True)
    st.markdown("**By System**")
    st.dataframe(_grouped_table(data["by_system"], "System"), width="stretch", hide_index=True)
    st.markdown("**By Equipment Type**")
    by_type_labeled = {dhfd.equipment_type_label(k): v for k, v in data["by_equipment_type"].items()}
    st.dataframe(_grouped_table(by_type_labeled, "Equipment Type"), width="stretch", hide_index=True)

# ---------------------------------------------------------------------------
# Issue-centric view (item 13) - tag-level problems across the fleet.
# Deliberately does NOT diagnose "sensor failed"/"PLC offline" - only
# reports the same deterministic per-tag evidence already computed by
# Phase 16.1, flattened to one row per (tag, issue).
# ---------------------------------------------------------------------------
with issue_tab:
    ISSUE_LABELS = {
        "MISSING": "Missing (never reported any value)",
        "STALE": "Stale",
        "INVALID": "Invalid value",
        "CONTINUITY_GAP": "Continuity gap",
        "TIMESTAMP_ISSUE": "Timestamp issue",
        "FROZEN_CANDIDATE": "Frozen candidate - advisory",
        "INDETERMINATE_CHANGE_ONLY": "Indeterminate - change-only logging",
    }

    all_issue_types = sorted({issue["issue"] for issue in data["issues"]})
    issue_type_filter = st.multiselect(
        "Issue type", all_issue_types, default=all_issue_types, format_func=lambda k: ISSUE_LABELS.get(k, k),
    )

    filtered_instance_keys = {r["instance_key"] for r in rows}
    issue_rows = [
        issue for issue in data["issues"]
        if issue["issue"] in issue_type_filter and issue["instance_key"] in filtered_instance_keys
    ]

    if not issue_rows:
        st.info("No telemetry issues match the current filters.")
    else:
        issue_df = pd.DataFrame([
            {
                "Tag": issue["tag_name"],
                "Equipment": issue["display_name"],
                "Plant": (issue["plant_code"] or "-").upper(),
                "Issue": ISSUE_LABELS.get(issue["issue"], issue["issue"]),
                "Last Value": issue.get("last_value"),
                "Last Update": issue.get("last_time") or "Never logged",
                "Logging Mode": issue["logging_mode"],
                "Source": issue["source_driver"] or "Unknown",
                "Detail": issue.get("detail") or "-",
            }
            for issue in issue_rows
        ])
        st.dataframe(issue_df, width="stretch", hide_index=True)

    st.caption(
        "'Frozen candidate' is advisory evidence only - a continuous measurement unchanged for an unusually long "
        "time while related telemetry on the same equipment keeps updating normally. It is never a confirmed "
        "sensor failure. 'Indeterminate - change-only logging' tags only report when their value changes, so no "
        "recent row does not mean the tag has stopped communicating - they are never shown as Stale."
    )

# ---------------------------------------------------------------------------
# History & Trends (Phase 16.5) - reads engine.data_health_history via
# ui/data_health_history_data.py, which itself only reads the already-
# persisted data_health_snapshots table (written independently by
# app/data_health_history_worker.py, every 300s, on real change or a
# 24h heartbeat). NEVER recalculates Data Health, NEVER predicts, NEVER
# fabricates history before persistence began.
# ---------------------------------------------------------------------------
with history_tab:
    st.caption(
        "Historical Data Health, persisted independently by the Data Health history worker - separate from the "
        "live current-state evaluation above. Purely backward-looking (no prediction, no Remaining Useful Life, "
        "no forecast anywhere on this tab)."
    )

    recorded_since = dhhd.first_recorded_at()

    if not recorded_since:
        st.info(
            "No Data Health history has been recorded yet - the data_health_history_worker service persists its "
            "first snapshot on its first 300-second cycle."
        )
    else:
        st.caption(f"History recorded since {recorded_since}. Periods before this are never fabricated.")

        window_days = st.select_slider("Time window", options=[1, 3, 7, 14, 30], value=7, key="dh_history_window_days")
        start, end = dhhd.default_window(days=window_days)

        label_by_key_hist = {r["instance_key"]: f"{r['display_name']} ({r['instance_key']})" for r in rows}

        if not label_by_key_hist:
            st.info("No equipment matches the current filters.")
        else:
            selected_hist_key = st.selectbox(
                "Select equipment", list(label_by_key_hist), format_func=lambda k: label_by_key_hist[k],
                key="dh_history_equipment",
            )
            selected_hist_row = next(r for r in rows if r["instance_key"] == selected_hist_key)
            eq_history = dhhd.get_equipment_history(selected_hist_row["plant_code"], selected_hist_key, start, end)

            duration = eq_history["duration"]
            if duration.get("insufficient_history"):
                st.info("No Data Health history has been recorded yet for this equipment in the selected window.")
            else:
                st.markdown("**Data Health Status Distribution** (time-based, never a sample count)")
                dist_cols = st.columns(5)
                dist_cols[0].metric("GOOD", f"{duration['percentages']['GOOD']}%")
                dist_cols[1].metric("DEGRADED", f"{duration['percentages']['DEGRADED']}%")
                dist_cols[2].metric("POOR", f"{duration['percentages']['POOR']}%")
                dist_cols[3].metric("UNAVAILABLE", f"{duration['percentages']['UNAVAILABLE']}%")
                if duration["no_history_percentage"]:
                    dist_cols[4].metric(
                        "No History", f"{duration['no_history_percentage']}%",
                        help="Portion of the selected window before any snapshot existed for this equipment - "
                             "never fabricated as any status.",
                    )
                st.caption(
                    "The most recently recorded status is assumed to hold until the next recorded change "
                    "(snapshots are written on a real change or a 24h heartbeat, so silence between two snapshots "
                    "means the state persisted) - and through to the end of the selected window unless a later "
                    "change is recorded."
                )

            component_history = eq_history["component_history"]
            if component_history:
                st.markdown("**Data Confidence & Component History**")
                chart_df = pd.DataFrame(component_history).set_index("computed_at")[
                    ["confidence_score", "freshness_score", "availability_score", "validity_score", "continuity_score"]
                ]
                st.line_chart(chart_df)
                st.caption("Exactly as persisted - never smoothed/interpolated, so a short POOR/UNAVAILABLE dip stays visible.")
            else:
                st.caption("No snapshots recorded for this equipment in the selected window.")

            transitions = eq_history["transitions"]
            st.markdown(f"**Status Transitions** ({len(transitions)} in window)")
            if transitions:
                transition_rows = [
                    {
                        "At": t["at"], "From": t["from_status"], "To": t["to_status"],
                        "Changes": "; ".join(f"{c['field']}: {c['from']} → {c['to']}" for c in t["changes"]) or "-",
                    }
                    for t in transitions
                ]
                st.dataframe(pd.DataFrame(transition_rows), width="stretch", hide_index=True)
            else:
                st.caption("No status transitions recorded in the selected window.")

        st.divider()
        st.markdown("**Fleet-wide time-in-status (grouped)**")
        st.caption("Percentages within each group are of that group's own total observed snapshot time - never a single averaged historical score.")

        fleet_history = dhhd.get_fleet_history(start, end)

        def _grouped_duration_table(groups: dict, label: str) -> pd.DataFrame:
            table_rows = []
            for key, bucket in groups.items():
                total = sum(bucket["seconds"].values()) or 1.0
                table_rows.append({
                    label: key, "Equipment": bucket["equipment"],
                    "GOOD %": round(bucket["seconds"]["GOOD"] / total * 100, 1),
                    "DEGRADED %": round(bucket["seconds"]["DEGRADED"] / total * 100, 1),
                    "POOR %": round(bucket["seconds"]["POOR"] / total * 100, 1),
                    "UNAVAILABLE %": round(bucket["seconds"]["UNAVAILABLE"] / total * 100, 1),
                    "Transitions": bucket["transitions"],
                })
            return pd.DataFrame(table_rows)

        st.markdown("_By Plant_")
        st.dataframe(_grouped_duration_table(fleet_history["by_plant"], "Plant"), width="stretch", hide_index=True)
        st.markdown("_By Equipment Type_")
        by_type_labeled = {dhfd.equipment_type_label(k): v for k, v in fleet_history["by_equipment_type"].items()}
        st.dataframe(_grouped_duration_table(by_type_labeled, "Equipment Type"), width="stretch", hide_index=True)

# ---------------------------------------------------------------------------
# Recurring Issues (Phase 16.5) - "inactive -> active = new occurrence"
# counting (engine.data_health_history.recurring_issues()) - never a
# raw snapshot-row count (a 24h heartbeat would otherwise inflate an
# ongoing, unchanged issue into a fake repeat), never a diagnosis.
# ---------------------------------------------------------------------------
with recurring_tab:
    st.caption(
        "How many times each telemetry issue type turned from inactive to active, per equipment - never how many "
        "snapshot rows mention it (a still-ongoing issue plus a 24h heartbeat would otherwise look like repeat "
        "occurrences). Never a diagnosis - counts only."
    )

    recorded_since_recur = dhhd.first_recorded_at()

    if not recorded_since_recur:
        st.info("No Data Health history has been recorded yet.")
    else:
        recur_window_days = st.select_slider("Time window", options=[1, 3, 7, 14, 30], value=7, key="dh_recurring_window_days")
        recur_start, recur_end = dhhd.default_window(days=recur_window_days)
        recur_fleet_history = dhhd.get_fleet_history(recur_start, recur_end)

        RECURRING_ISSUE_LABELS = {
            "MISSING": "Missing", "STALE": "Stale", "INVALID": "Invalid",
            "CONTINUITY_GAP": "Continuity Gap", "TIMESTAMP_ISSUE": "Timestamp Issue",
            "INDETERMINATE_CHANGE_ONLY": "Indeterminate (change-only)", "FROZEN_CANDIDATE": "Frozen Candidate (advisory)",
        }

        recurrence_rows = []
        for r in recur_fleet_history["equipment"]:
            if r["insufficient_history"]:
                continue
            total = sum(r["recurrence"].values())
            if total == 0:
                continue
            row = {
                "Equipment": r["display_name"], "Plant": r["plant_code"].upper(),
                "Area": r["area_name"] or "-", "System": r["system_name"] or "-",
                "Type": dhfd.equipment_type_label(r["equipment_type"]),
            }
            for issue, count in r["recurrence"].items():
                row[RECURRING_ISSUE_LABELS.get(issue, issue)] = count
            row["Total Occurrences"] = total
            recurrence_rows.append(row)

        if not recurrence_rows:
            st.info("No recurring telemetry issues recorded in the selected window.")
        else:
            recurrence_df = pd.DataFrame(recurrence_rows).sort_values("Total Occurrences", ascending=False)
            st.dataframe(recurrence_df, width="stretch", hide_index=True)

        st.caption(
            "'Frozen Candidate' recurrence is advisory evidence only - never a confirmed sensor failure count. "
            "'Indeterminate (change-only)' recurrence rarely applies (a tag's logging mode does not usually "
            "change) - shown for completeness."
        )
