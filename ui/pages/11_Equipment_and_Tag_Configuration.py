from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.configuration_manager import ConfigurationManager
from config.environment import ENVIRONMENT_LABELS, get_active_environment
from engine.metadata_migrator import infer
from ui import auth
from ui import health_data as hd
from ui.data_access import CONFIG_DATABASE_PATH, get_equipment_list


auth.require_role("admin")

current_user = auth.current_user()

st.title("🧩 Equipment & Tag Configuration")
st.caption(
    f"Editing the **{ENVIRONMENT_LABELS[get_active_environment()]}** database. "
    "Create equipment and tags from scratch - useful for an actual deployment that "
    "starts empty, or to extend the simulation dataset. Admin-only page. Map real "
    "PLC addresses for new tags on the PLC Connectivity page afterwards."
)


@st.cache_resource
def _get_config_manager() -> ConfigurationManager:
    return ConfigurationManager(database_path=CONFIG_DATABASE_PATH)


config_manager = _get_config_manager()

DATA_TYPES = ["REAL", "INT", "BOOL", "STRING"]

# Every unit already used somewhere in this dataset, plus a blank
# option - a listbox instead of free text avoids "degC" vs "°C" vs
# "C" style near-duplicates that would otherwise silently fragment
# TAG_PROFILES/THRESHOLD_PROFILES lookups (both keyed partly by unit).
UNITS = [
    "", "%", "%RH", "A", "Hz", "L", "L/h", "L/kg", "L/min", "NTU", "Nm³", "Nm³/h",
    "PF", "Pa", "V", "bar", "code", "count", "h", "kW", "kWh", "kg", "mV", "mg/L",
    "min", "mm/s", "m³", "m³/h", "pH", "rpm", "s", "state", "°C", "°Cdp", "µS/cm",
]

# Data types with no meaningful engineering unit - the existing
# dataset uses these two conventions with zero exceptions (50 BOOL
# tags all "state", 25 STRING tags all "code"), so the Unit field
# auto-selects the right one and greys out rather than leaving it
# open to accidental free choice.
AUTO_UNIT_BY_DATA_TYPE = {"BOOL": "state", "STRING": "code"}

CADENCE_OPTIONS = {
    "Every poll (default)": (None, False),
    "Every 10 seconds": (10, False),
    "Every 1 minute": (60, False),
    "Every 5 minutes": (300, False),
    "On change only": (None, True),
}

PARAMETERS = ["low_alarm", "low_warning", "high_warning", "high_alarm"]
PARAMETER_LABELS = {
    "low_alarm": "Low Alarm",
    "low_warning": "Low Warning",
    "high_warning": "High Warning",
    "high_alarm": "High Alarm",
}


def _run_update(sql: str, params: tuple) -> None:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _get_full_tag(tag_name: str) -> dict:
    """
    ConfigurationManager.get_tags()/get_tag() don't select the concept
    fields (measurement/location/...) or logging cadence - this pulls
    the whole row for the Edit dialog.
    """
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM tags WHERE tag_name=?", (tag_name,)
        ).fetchone()
        return dict(row)
    finally:
        connection.close()


def _get_full_equipment(equipment_id: int) -> dict:
    """
    get_equipment_list() (ui/data_access.py) and
    ConfigurationManager.get_equipment() each select a different subset
    of columns - this pulls every column the Edit dialog needs in one
    place rather than reconciling the two.
    """
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM equipment WHERE id=?", (equipment_id,)
        ).fetchone()
        return dict(row)
    finally:
        connection.close()


# ------------------------------------------------------------------
# Equipment
# ------------------------------------------------------------------

st.subheader("Equipment")


@st.dialog("New Equipment")
def _new_equipment_dialog() -> None:
    name = st.text_input("Internal name (unique id)", key="new_eq_name")
    st.caption(
        "Format: lowercase, underscores, plant prefix - e.g. `p01_utility_pump04`. "
        "This is a stable internal id, never shown to end users and can't be changed later."
    )
    display_name = st.text_input("Display name (e.g. Utility Pump 04)", key="new_eq_display")
    description = st.text_area("Description", key="new_eq_desc")
    brand = st.text_input("Brand (optional)", key="new_eq_brand")
    model = st.text_input("Model (optional)", key="new_eq_model")
    device_number = st.text_input("Device number", key="new_eq_devnum")
    service_interval_days = st.number_input(
        "Service interval, days (optional, 0 = not scheduled)",
        min_value=0, value=0, step=1, key="new_eq_interval",
    )

    if st.button("Create", type="primary", key="new_eq_save"):
        if not name.strip() or not display_name.strip() or not device_number.strip():
            st.error("Internal name, display name, and device number are all required.")
        elif config_manager.get_equipment(name=name.strip()) is not None:
            st.error(f"Equipment '{name.strip()}' already exists.")
        else:
            config_manager.add_equipment(
                name=name.strip(),
                display_name=display_name.strip(),
                description=description.strip(),
                username=current_user["username"],
            )
            _run_update(
                "UPDATE equipment SET brand=?, model=?, device_number=?, "
                "service_interval_days=? WHERE name=?",
                (
                    brand.strip() or None,
                    model.strip() or None,
                    device_number.strip() or None,
                    service_interval_days or None,
                    name.strip(),
                ),
            )
            st.success(f"Created equipment '{display_name.strip()}'.")
            st.rerun()


@st.dialog("Edit Equipment")
def _edit_equipment_dialog(equipment: dict) -> None:
    st.caption(f"Internal name `{equipment['name']}` can't be changed once created.")
    display_name = st.text_input(
        "Display name", value=equipment["display_name"], key="edit_eq_display"
    )
    description = st.text_area(
        "Description", value=equipment.get("description") or "", key="edit_eq_desc"
    )
    brand = st.text_input("Brand (optional)", value=equipment.get("brand") or "", key="edit_eq_brand")
    model = st.text_input("Model (optional)", value=equipment.get("model") or "", key="edit_eq_model")
    device_number = st.text_input(
        "Device number (optional)", value=equipment.get("device_number") or "", key="edit_eq_devnum"
    )
    service_interval_days = st.number_input(
        "Service interval, days (optional, 0 = not scheduled)",
        min_value=0, value=equipment.get("service_interval_days") or 0, step=1, key="edit_eq_interval",
    )

    if st.button("Save Changes", type="primary", key="edit_eq_save"):
        if not display_name.strip():
            st.error("Display name is required.")
        else:
            _run_update(
                "UPDATE equipment SET display_name=?, description=?, brand=?, model=?, "
                "device_number=?, service_interval_days=? WHERE id=?",
                (
                    display_name.strip(),
                    description.strip() or None,
                    brand.strip() or None,
                    model.strip() or None,
                    device_number.strip() or None,
                    service_interval_days or None,
                    equipment["id"],
                ),
            )
            config_manager.write_audit_log(
                username=current_user["username"],
                action="update",
                entity_type="equipment",
                entity_name=equipment["name"],
            )
            st.success(f"Updated '{display_name.strip()}'.")
            st.rerun()


if st.button("+ New Equipment", type="primary"):
    _new_equipment_dialog()

equipment_list = get_equipment_list()
selected_equipment = None

if not equipment_list:
    st.caption("No equipment configured yet.")
else:
    equipment_df = pd.DataFrame(
        [
            {
                "Name": eq["display_name"],
                "Brand / Model": " / ".join(part for part in [eq["brand"], eq["model"]] if part) or "-",
                "Device #": eq["device_number"] or "-",
            }
            for eq in equipment_list
        ]
    )

    equipment_selection = st.dataframe(
        equipment_df,
        hide_index=True,
        width="stretch",
        height=300,
        on_select="rerun",
        selection_mode="single-row",
        key="equipment_table",
    )

    selected_equipment_positions = equipment_selection.selection.rows if equipment_selection else []
    selected_equipment = (
        equipment_list[selected_equipment_positions[0]] if selected_equipment_positions else None
    )

    if selected_equipment:
        # Phase 12.4 (item 7, optional) - a single compact, read-only
        # health line. Never editable here - Health Score is never a
        # manually-set equipment field.
        health = hd.compact_health_context(selected_equipment["id"])
        if health["assessed"]:
            st.caption(f"Current Health: {health['state']} · {health['score']} &nbsp;·&nbsp; Confidence: {health['confidence']} &nbsp;·&nbsp; Last assessed: {health['last_assessed']}")
        else:
            st.caption("Current Health: Not Assessed")

        edit_eq_col, delete_eq_col = st.columns(2)

        with edit_eq_col:
            if st.button("✏️ Edit Selected Equipment"):
                _edit_equipment_dialog(_get_full_equipment(selected_equipment["id"]))

        with delete_eq_col:
            if st.button("🗑 Delete Selected Equipment"):
                tag_count = len(config_manager.get_tags(equipment_name=selected_equipment["name"]))

                if tag_count:
                    st.error(
                        f"'{selected_equipment['display_name']}' still has {tag_count} tag(s) - "
                        "delete those first."
                    )
                else:
                    _run_update("DELETE FROM equipment WHERE id=?", (selected_equipment["id"],))
                    config_manager.write_audit_log(
                        username=current_user["username"],
                        action="delete",
                        entity_type="equipment",
                        entity_name=selected_equipment["name"],
                    )
                    st.success(f"Deleted '{selected_equipment['display_name']}'.")
                    st.rerun()

st.divider()
st.subheader("Tags")

if not selected_equipment:
    st.caption("Select an equipment above to manage its tags.")
else:
    st.write(f"Tags for **{selected_equipment['display_name']}**")

    def _suggest_tag_prefix(equipment_name: str) -> str:
        """
        Defaults the New Tag name field so the user isn't retyping
        "P01." (or a whole equipment prefix) from scratch every time.
        Prefers a sibling tag on the same equipment if one exists (full
        prefix reuse, e.g. "P01.UTILITY.PUMP04." - just add the signal
        name), else the plant prefix most other tags in the system
        already use, else nothing for a genuinely empty system.
        """
        sibling_tags = config_manager.get_tags(equipment_name=equipment_name)
        if sibling_tags and "." in sibling_tags[0]["tag_name"]:
            return sibling_tags[0]["tag_name"].rsplit(".", 1)[0] + "."

        all_tags = config_manager.get_tags()
        plant_prefixes = [t["tag_name"].split(".")[0] for t in all_tags if "." in t["tag_name"]]

        if not plant_prefixes:
            return ""

        from collections import Counter

        return Counter(plant_prefixes).most_common(1)[0][0] + "."

    @st.dialog("New Tag")
    def _new_tag_dialog(equipment: dict) -> None:
        tag_name = st.text_input(
            "Tag name (must be unique)",
            value=_suggest_tag_prefix(equipment["name"]),
            key="new_tag_name",
        )
        st.caption(
            "Format: `PLANT.AREA.INSTANCE.Signal` - e.g. `P01.UTILITY.PUMP04.DischargePressure`. "
            "Matching this pattern is what lets the simulator and dashboards group tags by equipment."
        )
        description = st.text_input("Description", key="new_tag_desc")
        data_type = st.selectbox("Data type", DATA_TYPES, key="new_tag_type")
        auto_unit = AUTO_UNIT_BY_DATA_TYPE.get(data_type)
        unit = st.selectbox(
            "Unit",
            UNITS,
            index=UNITS.index(auto_unit) if auto_unit else 0,
            # Keyed by data_type so switching REAL -> BOOL (etc.)
            # always re-defaults to the right auto-unit instead of
            # keeping whatever was selected before under a disabled,
            # now-stale widget - Streamlit only applies `index` the
            # first time a given key is used.
            key=f"new_tag_unit_{data_type}",
            disabled=auto_unit is not None,
            help=f"{data_type} tags always use '{auto_unit}'." if auto_unit else None,
        )
        cadence_choice = st.selectbox(
            "Logging cadence", list(CADENCE_OPTIONS), key="new_tag_cadence"
        )
        enabled = st.checkbox("Enabled", value=True, key="new_tag_enabled")

        st.divider()
        threshold_mode = st.radio(
            "Threshold monitoring",
            ["Log only (no thresholds)", "Enable thresholds"],
            key="new_tag_threshold_mode",
        )

        selected_sides: dict[str, float] = {}

        if threshold_mode == "Enable thresholds":
            if data_type != "REAL":
                st.caption("Thresholds only apply to REAL (numeric) tags - this tag will log only.")
            else:
                st.caption("Check the sides that apply and enter a value for each.")
                # 2x2, not 1x4 - four equal columns left "High Warning"
                # too narrow to fit on one line, so it wrapped to two
                # and pushed that column's input down relative to its
                # siblings (jagged row). Two wider columns per row give
                # every label enough room to stay on one line.
                for row_parameters in (PARAMETERS[:2], PARAMETERS[2:]):
                    columns = st.columns(2)
                    for column, parameter in zip(columns, row_parameters):
                        with column:
                            # Number input always renders (just disabled
                            # when unchecked) so every column is the same
                            # height - conditionally rendering it made
                            # checked/unchecked columns different heights
                            # and the row looked jagged.
                            use_side = st.checkbox(PARAMETER_LABELS[parameter], key=f"new_tag_use_{parameter}")
                            value = st.number_input(
                                PARAMETER_LABELS[parameter] + " value",
                                step=0.1, format="%.2f", key=f"new_tag_val_{parameter}",
                                disabled=not use_side,
                            )
                            if use_side:
                                selected_sides[parameter] = value

        if st.button("Create", type="primary", key="new_tag_save"):
            if not tag_name.strip():
                st.error("Tag name is required.")
            elif config_manager.get_tag(tag_name.strip()) is not None:
                st.error(f"Tag '{tag_name.strip()}' already exists.")
            else:
                effective_unit = auto_unit if auto_unit else unit.strip()
                config_manager.add_tag(
                    tag_name=tag_name.strip(),
                    equipment_name=equipment["name"],
                    description=description.strip(),
                    data_type=data_type,
                    unit=effective_unit,
                    enabled=enabled,
                    username=current_user["username"],
                )

                concepts = infer(tag_name.strip(), description.strip(), data_type)
                interval_seconds, log_on_change = CADENCE_OPTIONS[cadence_choice]
                _run_update(
                    "UPDATE tags SET measurement=?, location=?, signal_type=?, event_type=?, "
                    "threshold_type=?, logging_interval_seconds=?, log_on_change=? WHERE tag_name=?",
                    (
                        concepts.get("measurement"), concepts.get("location"),
                        concepts.get("signal_type"), concepts.get("event_type"),
                        concepts.get("threshold_type"), interval_seconds, int(log_on_change),
                        tag_name.strip(),
                    ),
                )

                for parameter, value in selected_sides.items():
                    config_manager.set_threshold(
                        tag_name=tag_name.strip(), parameter=parameter, value=value,
                        username=current_user["username"],
                    )

                st.success(f"Created tag '{tag_name.strip()}'.")
                st.rerun()

    @st.dialog("Edit Tag")
    def _edit_tag_dialog(tag: dict) -> None:
        st.caption(f"Tag name `{tag['tag_name']}` can't be changed once created - delete and recreate if it's genuinely wrong.")
        description = st.text_input(
            "Description", value=tag.get("description") or "", key="edit_tag_desc"
        )
        data_type = st.selectbox(
            "Data type", DATA_TYPES, index=DATA_TYPES.index(tag["data_type"]), key="edit_tag_type"
        )
        # Defensive: if this tag's current unit somehow isn't in the
        # standard list (shouldn't happen - UNITS was built from every
        # unit actually in use - but if it ever did, this avoids
        # silently blanking out real data just by opening the dialog).
        current_unit = tag.get("unit") or ""
        auto_unit = AUTO_UNIT_BY_DATA_TYPE.get(data_type)

        if auto_unit:
            default_unit = auto_unit
        elif data_type == tag["data_type"]:
            default_unit = current_unit
        else:
            default_unit = ""

        unit_options = UNITS if default_unit in UNITS else [default_unit, *UNITS]
        unit = st.selectbox(
            "Unit",
            unit_options,
            index=unit_options.index(default_unit),
            # Keyed by data_type - see the same note in _new_tag_dialog.
            key=f"edit_tag_unit_{data_type}",
            disabled=auto_unit is not None,
            help=f"{data_type} tags always use '{auto_unit}'." if auto_unit else None,
        )

        cadence_by_value = {v: k for k, v in CADENCE_OPTIONS.items()}
        current_cadence = cadence_by_value.get(
            (tag.get("logging_interval_seconds"), bool(tag.get("log_on_change"))),
            "Every poll (default)",
        )
        cadence_choice = st.selectbox(
            "Logging cadence", list(CADENCE_OPTIONS),
            index=list(CADENCE_OPTIONS).index(current_cadence), key="edit_tag_cadence",
        )
        enabled = st.checkbox("Enabled", value=bool(tag["enabled"]), key="edit_tag_enabled")

        st.divider()
        existing_thresholds = thresholds_by_tag.get(tag["tag_name"], {})
        has_any_threshold = any(existing_thresholds.get(p) is not None for p in PARAMETERS)
        threshold_mode = st.radio(
            "Threshold monitoring",
            ["Log only (no thresholds)", "Enable thresholds"],
            index=1 if has_any_threshold else 0,
            key="edit_tag_threshold_mode",
        )

        selected_sides: dict[str, float] = {}

        if threshold_mode == "Enable thresholds":
            if data_type != "REAL":
                st.caption("Thresholds only apply to REAL (numeric) tags - this tag will log only.")
            else:
                st.caption("Check the sides that apply and enter a value for each.")
                # 2x2, not 1x4 - see the matching comment in the "new
                # tag" form above for why.
                for row_parameters in (PARAMETERS[:2], PARAMETERS[2:]):
                    columns = st.columns(2)
                    for column, parameter in zip(columns, row_parameters):
                        with column:
                            was_set = existing_thresholds.get(parameter) is not None
                            use_side = st.checkbox(
                                PARAMETER_LABELS[parameter], value=was_set, key=f"edit_tag_use_{parameter}"
                            )
                            value = st.number_input(
                                PARAMETER_LABELS[parameter] + " value",
                                value=existing_thresholds.get(parameter) or 0.0,
                                step=0.1, format="%.2f", key=f"edit_tag_val_{parameter}",
                                disabled=not use_side,
                            )
                            if use_side:
                                selected_sides[parameter] = value

        if st.button("Save Changes", type="primary", key="edit_tag_save"):
            concepts = infer(tag["tag_name"], description.strip(), data_type)
            interval_seconds, log_on_change = CADENCE_OPTIONS[cadence_choice]
            effective_unit = auto_unit if auto_unit else unit.strip()

            _run_update(
                "UPDATE tags SET description=?, data_type=?, unit=?, enabled=?, "
                "measurement=?, location=?, signal_type=?, event_type=?, threshold_type=?, "
                "logging_interval_seconds=?, log_on_change=? WHERE tag_name=?",
                (
                    description.strip(), data_type, effective_unit, int(enabled),
                    concepts.get("measurement"), concepts.get("location"),
                    concepts.get("signal_type"), concepts.get("event_type"),
                    concepts.get("threshold_type"), interval_seconds, int(log_on_change),
                    tag["tag_name"],
                ),
            )

            # Any side that was previously set but isn't checked anymore
            # gets explicitly cleared, not just left alone.
            for parameter in PARAMETERS:
                if parameter in selected_sides:
                    config_manager.set_threshold(
                        tag_name=tag["tag_name"], parameter=parameter,
                        value=selected_sides[parameter], username=current_user["username"],
                    )
                elif existing_thresholds.get(parameter) is not None:
                    config_manager.set_threshold(
                        tag_name=tag["tag_name"], parameter=parameter,
                        value=None, username=current_user["username"],
                    )

            config_manager.write_audit_log(
                username=current_user["username"],
                action="update",
                entity_type="tag",
                entity_name=tag["tag_name"],
            )
            st.success(f"Updated '{tag['tag_name']}'.")
            st.rerun()

    if st.button("+ New Tag", type="primary"):
        _new_tag_dialog(selected_equipment)

    tags = config_manager.get_tags(equipment_name=selected_equipment["name"])
    thresholds_by_tag = {row["tag_name"]: row for row in config_manager.get_thresholds()}

    if not tags:
        st.caption("No tags for this equipment yet.")
    else:
        def _threshold_summary(tag_name: str) -> str:
            existing = thresholds_by_tag.get(tag_name, {})
            sides = [PARAMETER_LABELS[p] for p in PARAMETERS if existing.get(p) is not None]
            return ", ".join(sides) if sides else "log only"

        cadence_by_value = {v: k for k, v in CADENCE_OPTIONS.items()}

        def _cadence_label(tag_name: str) -> str:
            full_tag = _get_full_tag(tag_name)
            key = (full_tag.get("logging_interval_seconds"), bool(full_tag.get("log_on_change")))
            return cadence_by_value.get(key, "Every poll (default)")

        tags_df = pd.DataFrame(
            [
                {
                    "Tag Name": tag["tag_name"],
                    "Type": tag["data_type"],
                    "Unit": tag["unit"] or "-",
                    "Enabled": "Yes" if tag["enabled"] else "No",
                    "Cadence": _cadence_label(tag["tag_name"]),
                    "Thresholds": _threshold_summary(tag["tag_name"]),
                }
                for tag in tags
            ]
        )

        tag_selection = st.dataframe(
            tags_df,
            hide_index=True,
            width="stretch",
            height=300,
            on_select="rerun",
            selection_mode="single-row",
            key="tags_table",
        )

        selected_tag_positions = tag_selection.selection.rows if tag_selection else []

        if selected_tag_positions:
            selected_tag = tags[selected_tag_positions[0]]

            edit_tag_col, delete_tag_col = st.columns(2)

            with edit_tag_col:
                if st.button("✏️ Edit Selected Tag"):
                    _edit_tag_dialog(_get_full_tag(selected_tag["tag_name"]))

            with delete_tag_col:
                if st.button("🗑 Delete Selected Tag"):
                    _run_update("DELETE FROM tags WHERE tag_name=?", (selected_tag["tag_name"],))
                    config_manager.write_audit_log(
                        username=current_user["username"],
                        action="delete",
                        entity_type="tag",
                        entity_name=selected_tag["tag_name"],
                    )
                    st.success(f"Deleted '{selected_tag['tag_name']}' (and its address mapping/thresholds).")
                    st.rerun()

st.divider()

with st.expander("Recent changes (audit log)"):
    audit_rows = config_manager.get_audit_log(limit=50)
    relevant_rows = [row for row in audit_rows if row["entity_type"] in ("equipment", "tag")]

    if not relevant_rows:
        st.caption("No equipment/tag changes recorded yet.")
    else:
        for row in relevant_rows:
            st.write(
                f"- {row['timestamp']} — **{row['action']}** {row['entity_type']} "
                f"`{row['entity_name']}` (by {row['username']})"
            )
