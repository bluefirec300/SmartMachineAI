from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.rule_engine import RuleEngine
from database.database import DatabaseManager
from ui.data_access import CONFIG_DATABASE_PATH, MACHINE_DATABASE_PATH


# ---------------------------------------------------------------------------
# Experimental prototype - a SCADA-style mimic diagram, not wired into
# Home.py's navigation yet (run standalone: `streamlit run
# ui/pages/12_SCADA_Floor_Plan.py`). No real factory floor plan exists to
# copy - the room layout below is an imagined, plausible arrangement built
# from the existing tag-naming convention's own area codes (P01.UTILITY.*,
# P01.COLDROOM.*, P01.PROD.*, ...), not any real drawing. Every equipment
# instance, tag, and live value is real data from the running system -
# only the physical room positions/graphics are invented.
#
# Three rounds of refresh-behavior fixes:
# - v1: equipment clickable via raw SVG <a href="?..."> links - a real
#   browser navigation, reruns the *entire* script on every click.
# - v2: moved selection into an st.selectbox inside an @st.fragment - no
#   more full-page navigation, but the giant SVG (every equipment icon
#   baked into one huge markdown/HTML string) still got wholesale-
#   replaced on every refresh, which *still* visibly flashed - and
#   equipment stopped being directly clickable, which defeated the point.
# - v3: equipment became real st.button widgets, on the theory that
#   native widgets update more smoothly than replacing raw HTML. Also
#   added a purely decorative (non-interactive) background SVG for
#   atmosphere. Still flashed - confirmed by reading Streamlit's own
#   fragment.py source, not assumed: "the elements are cleared and
#   redrawn on each fragment rerun" is true for *everything* inside a
#   fragment, native widgets included. Putting almost the whole page's
#   content inside one auto-rerunning fragment was always going to look
#   like a full-page flash, regardless of what widget type was used.
#   The decorative SVG was also, per direct feedback, useless clutter
#   (a non-interactive building icon nobody could click) - removed.
#
# v4 (current): the flash itself is an inherent Streamlit fragment
# behavior, not fully avoidable while this much content lives inside one
# `run_every` fragment without a custom JS component (out of scope for
# a prototype). The practical mitigation actually taken: a slow refresh
# interval (delay is fine, per explicit confirmation) so the redraw
# happens rarely enough not to be disruptive, rather than chasing a
# flicker-free effect native Streamlit can't fully deliver here.
# ---------------------------------------------------------------------------

st.set_page_config(page_title="SCADA Floor Plan", page_icon="🏭", layout="wide")

FONT_STACK = "'Source Sans Pro', -apple-system, 'Segoe UI', sans-serif"
MONO_STACK = "'Source Code Pro', 'SF Mono', Consolas, monospace"

# How often the live section (equipment status + info board) refreshes.
# Streamlit fragments clear and redraw *all* of their content on every
# run_every tick, whether it's raw HTML or native widgets (confirmed
# from Streamlit's own runtime/fragment.py docstring) - there is no
# native way to update just the values that changed. A slow interval is
# the practical mitigation: infrequent enough that the redraw isn't
# disruptive, rather than chasing a flicker-free effect that isn't
# achievable here without a custom JS component.
REFRESH_SECONDS = 30

SEVERITY_COLOR = {
    "alarm": "#e5484d",
    "warning": "#f5a524",
    "normal": "#3dd68c",
    "not_evaluated": "#8a8f98",
    "no_data": "#4a4f58",
}
# "normal" must outrank "not_evaluated": _equipment_status()'s max()
# picks the first tag on a tie, and an equipment's alphabetically-first
# tag is often a STRING AlarmCode (always "not_evaluated" absent a
# fault) - if it tied with a real, in-range measurement tag, the
# AlarmCode would win the tie and the equipment would wrongly show
# "no threshold configured" despite having real configured limits.
SEVERITY_RANK = {"alarm": 4, "warning": 3, "normal": 2, "not_evaluated": 1, "no_data": 0}
SEVERITY_LABEL = {
    "alarm": "Alarm",
    "warning": "Warning",
    "normal": "Normal",
    "not_evaluated": "No threshold configured",
    "no_data": "No data yet",
}
SEVERITY_DOT = {"alarm": "🔴", "warning": "🟠", "normal": "🟢", "not_evaluated": "⚪", "no_data": "⚫"}
# Text color paired with each SEVERITY_COLOR background, chosen for
# contrast against that specific background (the light backgrounds -
# warning/normal/not_evaluated - need dark text; alarm's red and
# no_data's dark gray need light text).
SEVERITY_TEXT = {
    "alarm": "#ffffff",
    "warning": "#1a1206",
    "normal": "#062017",
    "not_evaluated": "#14181f",
    "no_data": "#e6edf3",
}

# One emoji glyph per equipment category, purely decorative. Falls back
# to a generic factory glyph for anything not listed, so a newly-enabled
# equipment type never breaks the page, it just looks generic until this
# dict is taught about it.
CATEGORY_ICON = {
    "air_compressor": "💨",
    "compressed_air_header": "🌬️",
    "chiller": "❄️",
    "chilled_water_pump": "💧",
    "water_supply_pump": "🚰",
    "water_supply": "🚰",
    "cold_room": "🧊",
    "bead_mill": "⚙️",
    "high_speed_disperser": "🌀",
    "mixer": "🔄",
    "filling_machine": "🧴",
    "dust_collector": "🌪️",
    "tank": "🛢️",
    "solvent_transfer": "🧪",
    "water_treatment_system": "🚿",
    "ro_di_system": "💧",
    "effluent_treatment": "♻️",
    "main_incomer": "⚡",
    "building_incomer": "⚡",
    "transformer": "🔌",
    "ups": "🔋",
    "generator": "⛽",
    "ahu": "🌬️",
    "mcc_room": "🖥️",
    "area_monitoring": "🌡️",
    "weather_node": "☁️",
    "building_water_meter": "💦",
    "fire_water_system": "🧯",
}
DEFAULT_ICON = "🏭"

# Which equipment "category" (the same p01_<category>_<instance> naming
# app/ask.py's _equipment_category_key() already relies on) belongs in
# which imagined room. Anything unmatched falls into an auto-added
# "Other" zone, so a newly-enabled equipment type never disappears from
# the map, it just starts out ungrouped until this dict is taught about it.
ZONES = [
    {
        "name": "Utilities Yard",
        "rect": (20, 20, 620, 190),
        "categories": {
            "air_compressor", "compressed_air_header", "chiller",
            "chilled_water_pump", "water_supply_pump", "water_supply",
        },
    },
    {
        "name": "Electrical Room",
        "rect": (660, 20, 300, 190),
        "categories": {
            "main_incomer", "building_incomer", "transformer", "ups", "generator",
        },
    },
    {
        "name": "Cold Storage",
        "rect": (980, 20, 200, 190),
        "categories": {"cold_room"},
    },
    {
        "name": "Production Floor",
        "rect": (20, 230, 460, 190),
        "categories": {
            "bead_mill", "high_speed_disperser", "mixer",
            "filling_machine", "dust_collector",
        },
    },
    {
        "name": "Tank Farm",
        "rect": (500, 230, 300, 190),
        "categories": {"tank", "solvent_transfer"},
    },
    {
        "name": "Water Treatment Plant",
        "rect": (820, 230, 360, 190),
        "categories": {"water_treatment_system", "ro_di_system", "effluent_treatment"},
    },
    {
        "name": "HVAC & Monitoring",
        "rect": (20, 440, 460, 160),
        "categories": {
            "ahu", "mcc_room", "area_monitoring", "weather_node", "building_water_meter",
        },
    },
    {
        "name": "Fire Safety",
        "rect": (500, 440, 300, 160),
        "categories": {"fire_water_system"},
    },
]


def _category_key(equipment_name: str) -> str:
    """p01_cold_room_cr01 -> cold_room (same convention as app/ask.py's
    _equipment_category_key - kept as a local copy since it's a one-line
    regex and this page is deliberately standalone, not yet wired into
    the rest of the app)."""
    match = re.match(r"^p\d+_(.+)_[a-z0-9]+$", equipment_name)
    return match.group(1) if match else equipment_name


def _short_code(equipment_name: str) -> str:
    """
    p01_air_compressor_ac01 -> AC01, p01_main_incomer_main -> MAIN. The
    instance code only, not the full display name - "Chilled Water Pump
    CHWP01 (P01)" doesn't fit in a compact button/icon, and the full
    name is still available via the display_name shown as the button's
    tooltip and in the zone/detail headings.
    """
    return equipment_name.rsplit("_", 1)[-1].upper()


@st.cache_data(ttl=5)
def _load_equipment(plant: str) -> list[dict]:
    connection = sqlite3.connect(CONFIG_DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        equipment_rows = connection.execute(
            "SELECT id, name, display_name FROM equipment WHERE name LIKE ? ORDER BY display_name",
            (f"{plant}_%",),
        ).fetchall()

        equipment = []

        for row in equipment_rows:
            tag_rows = connection.execute(
                "SELECT tag_name, unit, data_type FROM tags "
                "WHERE equipment_id = ? AND enabled = 1 ORDER BY tag_name",
                (row["id"],),
            ).fetchall()

            if not tag_rows:
                continue

            category = _category_key(row["name"])
            equipment.append(
                {
                    "name": row["name"],
                    "display_name": row["display_name"],
                    "category": category,
                    "icon": CATEGORY_ICON.get(category, DEFAULT_ICON),
                    "code": _short_code(row["name"]),
                    "tags": [dict(t) for t in tag_rows],
                }
            )
    finally:
        connection.close()

    return equipment


def _tag_severity(tag_name: str, data_type: str, latest: dict, latest_text: dict, rule_engine: RuleEngine) -> str:
    if data_type == "STRING":
        text_reading = latest_text.get(tag_name)
        if text_reading is None:
            return "no_data"
        if tag_name.endswith(".AlarmCode") and text_reading["value"] not in (None, "None"):
            return "alarm"
        return "not_evaluated"

    reading = latest.get(tag_name)
    if reading is None:
        return "no_data"

    result = rule_engine.evaluate_summary({"tag": tag_name, "current": reading["value"]})

    if result["condition"] == "not_evaluated":
        return "not_evaluated"

    return result["severity"]


def _equipment_status(equipment: dict, latest: dict, latest_text: dict, rule_engine: RuleEngine) -> dict:
    per_tag = [
        {
            **tag,
            "severity": _tag_severity(tag["tag_name"], tag["data_type"], latest, latest_text, rule_engine),
            "value": (latest_text.get(tag["tag_name"], {}).get("value") if tag["data_type"] == "STRING" else (latest.get(tag["tag_name"], {}) or {}).get("value")),
            "updated": (latest_text.get(tag["tag_name"], {}).get("time") if tag["data_type"] == "STRING" else (latest.get(tag["tag_name"], {}) or {}).get("time")),
        }
        for tag in equipment["tags"]
    ]

    worst = max(per_tag, key=lambda t: SEVERITY_RANK[t["severity"]])["severity"] if per_tag else "no_data"

    return {**equipment, "tags": per_tag, "overall_severity": worst}


def _svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _equipment_button_key(equipment_name: str) -> str:
    return f"scadabtn_{equipment_name}"


def _button_color_css(evaluated: list[dict]) -> str:
    """
    st.button has no built-in way to set a custom background color -
    only the fixed "primary"/"secondary" kinds. The documented,
    version-stable workaround is to wrap each button in its own
    st.container(key=...), which Streamlit gives a `st-key-<key>` CSS
    class, and target that with a plain <style> block - this is how
    the equipment boxes get their alarm/warning/normal coloring back
    (the original SVG-icon version had this via fill color; native
    st.button doesn't offer an equivalent parameter). One combined
    <style> block for every equipment button, injected once per
    render rather than one <style> tag per button.
    """
    rules = [
        f'.st-key-{_equipment_button_key(eq["name"])} button {{'
        f'background-color:{SEVERITY_COLOR[eq["overall_severity"]]} !important;'
        f'color:{SEVERITY_TEXT[eq["overall_severity"]]} !important;'
        f'border-color:{SEVERITY_COLOR[eq["overall_severity"]]} !important;'
        f'}}'
        for eq in evaluated
    ]
    return f"<style>{''.join(rules)}</style>"


def _render_legend() -> None:
    items = [
        ("alarm", "Alarm"),
        ("warning", "Warning"),
        ("normal", "Normal"),
        ("not_evaluated", "No threshold configured"),
        ("no_data", "No data yet"),
    ]
    cols = st.columns(len(items))
    for col, (key, label) in zip(cols, items):
        with col:
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:8px;font-family:{FONT_STACK};">'
                f'<div style="width:16px;height:16px;border-radius:4px;'
                f'background:{SEVERITY_COLOR[key]};flex-shrink:0;"></div>'
                f'<span style="font-size:13px;">{label}</span></div>',
                unsafe_allow_html=True,
            )


def _todays_energy_kwh(database: DatabaseManager, tag_name: str) -> float | None:
    """
    Energy_kWh is a monotonic running total (never resets), so "today's
    consumption" is the delta between its value at midnight and now -
    not the raw reading itself. Pulls historian rows since midnight and
    subtracts first-from-last; None if there's nothing logged yet today
    (e.g. right after a fresh start) rather than a misleading 0.
    """
    now = datetime.now()
    hours_since_midnight = max(
        0.05, (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600
    )

    try:
        rows = database.get_history(tag=tag_name, hours=hours_since_midnight, limit=3000)
    except Exception:
        return None

    if len(rows) < 2:
        return None

    return rows[-1]["value"] - rows[0]["value"]


def _board_heading(text: str) -> str:
    return (
        f'<div style="font-size:12px;font-weight:700;color:#c9d1d9;'
        f'margin:8px 0 2px;font-family:{FONT_STACK};">{text}</div>'
    )


def _compact_row(label: str, value: str, dot: str = "") -> str:
    """
    A single dense "label ... value" line - deliberately not the earlier
    card-per-stat design (11px label + 18px bold value + subtext, ~40px
    tall each). That looked fine for 4 rows but didn't scale once this
    became two full plants' worth of electrical, utility, and health
    data in one narrow column - this fits roughly 3x as many rows in
    the same vertical space. dot, when given, is a small leading status
    marker (e.g. a severity emoji) instead of a fixed-width icon column.
    """
    dot_html = f'<span style="margin-right:4px;">{dot}</span>' if dot else ""
    return (
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
        f'padding:2px 0;border-bottom:1px solid #1c2128;font-family:{FONT_STACK};">'
        f'<span style="font-size:11.5px;color:#8a8f98;">{label}</span>'
        f'<span style="font-size:12.5px;font-weight:600;white-space:nowrap;">{dot_html}{value}</span>'
        f'</div>'
    )


def _render_plant_board(plant: str, database: DatabaseManager, rule_engine: RuleEngine) -> None:
    """
    A fixed "main information board" section for one plant - the kind
    of always-visible summary a real SCADA overview screen keeps
    alongside its mimic diagram (accumulated consumption, live
    electrical readings, utility headers, overall plant health), rather
    than something you have to click into one piece of equipment to
    see. Called once per plant (see below) so both P01 and P02 are
    visible together, independent of which plant's floor plan is
    currently displayed. Rendered as dense single-line rows (see
    _compact_row) rather than the earlier one-stat-per-card layout -
    per explicit request to fit more information in less space now
    that this covers two full plants, not just the selected one.
    """
    equipment_list = _load_equipment(plant)
    latest = database.get_latest_all()
    latest_text = database.get_latest_text_all()
    evaluated = [_equipment_status(eq, latest, latest_text, rule_engine) for eq in equipment_list]

    incomer_prefix = f"{plant.upper()}.ELEC.MAIN"
    st.markdown(
        f'<div style="font-size:13px;font-weight:700;font-family:{FONT_STACK};">'
        f'⚡ {plant.upper()} Main Incomer</div>',
        unsafe_allow_html=True,
    )

    today_kwh = _todays_energy_kwh(database, f"{incomer_prefix}.Energy_kWh")
    power = latest.get(f"{incomer_prefix}.Power_kW")
    pf = latest.get(f"{incomer_prefix}.PF")
    freq = latest.get(f"{incomer_prefix}.Frequency")
    currents = [latest.get(f"{incomer_prefix}.Current_L{p}") for p in (1, 2, 3)]
    voltages = [
        latest.get(f"{incomer_prefix}.Voltage_L1L2"),
        latest.get(f"{incomer_prefix}.Voltage_L2L3"),
        latest.get(f"{incomer_prefix}.Voltage_L3L1"),
    ]

    rows = [
        _board_heading("Electrical"),
        _compact_row(
            "Today's consumption",
            f"{today_kwh:,.1f} kWh" if today_kwh is not None else "-",
        ),
        _compact_row("Power draw", f"{power['value']:.1f} kW" if power else "-"),
    ]

    if any(currents):
        current_text = " / ".join(f"{c['value']:.1f}" if c else "-" for c in currents)
        rows.append(_compact_row("Current L1/L2/L3", f"{current_text} A"))

    if any(voltages):
        voltage_text = " / ".join(f"{v['value']:.0f}" if v else "-" for v in voltages)
        rows.append(_compact_row("Voltage L1-2/L2-3/L3-1", f"{voltage_text} V"))

    rows.append(
        _compact_row(
            "Power factor / freq",
            f"{pf['value']:.2f} / {freq['value']:.1f} Hz" if pf and freq else "-",
        )
    )

    air_pressure = latest.get(f"{plant.upper()}.UTILITY.AIRHDR01.Pressure")
    air_flow = latest.get(f"{plant.upper()}.UTILITY.AIRHDR01.Flow")
    water_pressure = latest.get(f"{plant.upper()}.WATER.SYS01.HeaderPressure")
    water_level = latest.get(f"{plant.upper()}.WATER.SYS01.TankLevel")

    if air_pressure or water_pressure:
        rows.append(_board_heading("Utilities"))
        if air_pressure:
            rows.append(
                _compact_row(
                    "Compressed air header",
                    f"{air_pressure['value']:.1f} bar"
                    + (f" / {air_flow['value']:.0f} Nm³/h" if air_flow else ""),
                )
            )
        if water_pressure:
            rows.append(
                _compact_row(
                    "Water supply header",
                    f"{water_pressure['value']:.1f} bar"
                    + (f" / {water_level['value']:.0f}% tank" if water_level else ""),
                )
            )

    alarm_count = sum(1 for e in evaluated if e["overall_severity"] == "alarm")
    warning_count = sum(1 for e in evaluated if e["overall_severity"] == "warning")
    normal_count = sum(1 for e in evaluated if e["overall_severity"] == "normal")
    not_evaluated_count = sum(1 for e in evaluated if e["overall_severity"] == "not_evaluated")
    no_data_count = sum(1 for e in evaluated if e["overall_severity"] == "no_data")
    total = len(evaluated) or 1

    rows.append(_board_heading("Plant Health"))
    rows.append(
        _compact_row(
            "Equipment normal",
            f"{normal_count}/{len(evaluated)} ({round(100 * normal_count / total)}%)",
        )
    )
    rows.append(_compact_row("In alarm", str(alarm_count), dot=SEVERITY_DOT["alarm"] if alarm_count else ""))
    rows.append(_compact_row("In warning", str(warning_count), dot=SEVERITY_DOT["warning"] if warning_count else ""))
    if not_evaluated_count or no_data_count:
        rows.append(_compact_row("No threshold / no data", f"{not_evaluated_count} / {no_data_count}"))

    st.markdown("".join(rows), unsafe_allow_html=True)


def _render_equipment_detail(equipment: dict) -> None:
    counts: dict[str, int] = {}
    for tag in equipment["tags"]:
        counts[tag["severity"]] = counts.get(tag["severity"], 0) + 1

    summary_bits = [
        f"{counts[key]} {SEVERITY_LABEL[key].lower()}"
        for key in ("alarm", "warning")
        if counts.get(key)
    ]
    if summary_bits:
        st.warning(" and ".join(summary_bits) + " on this equipment.")
    else:
        st.success("Everything within configured limits.")

    columns = "14px 1fr 110px 150px"
    header = (
        f'<div style="display:grid;grid-template-columns:{columns};gap:12px;'
        f'padding:4px 0;color:#8a8f98;font-size:11px;text-transform:uppercase;'
        f'letter-spacing:0.05em;font-family:{FONT_STACK};">'
        f'<div></div><div>Tag</div><div style="text-align:right;">Value</div>'
        f'<div style="text-align:right;">Updated</div></div>'
    )
    rows = [header]

    for tag in sorted(equipment["tags"], key=lambda t: t["tag_name"]):
        color = SEVERITY_COLOR[tag["severity"]]
        value_text = tag["value"] if tag["value"] is not None else "-"
        unit = f" {tag['unit']}" if tag.get("unit") else ""
        updated = tag["updated"] or "never logged"

        rows.append(
            f'<div style="display:grid;grid-template-columns:{columns};gap:12px;'
            f'align-items:center;padding:6px 0;border-bottom:1px solid #21262d;'
            f'font-family:{FONT_STACK};">'
            f'<div style="width:10px;height:10px;border-radius:50%;background:{color};"></div>'
            f'<div style="font-family:{MONO_STACK};font-size:13px;">{_svg_escape(tag["tag_name"])}</div>'
            f'<div style="font-weight:600;font-size:13px;text-align:right;">{_svg_escape(str(value_text))}{unit}</div>'
            f'<div style="color:#8a8f98;font-size:12px;text-align:right;">{updated}</div>'
            f'</div>'
        )

    st.markdown("".join(rows), unsafe_allow_html=True)


st.title("🏭 SCADA Floor Plan (prototype)")
st.caption(
    "Experimental - not yet decided whether this becomes part of the operator-facing app. "
    "Room layout is imagined (no real factory drawing exists yet); every status/value shown "
    "is real, live data from the system. Click any equipment button below its room to see "
    "its live tag values."
)

top_cols = st.columns([1, 1, 2])
with top_cols[0]:
    plant = st.selectbox("Plant (floor plan)", ["p01", "p02"], format_func=lambda p: p.upper())
with top_cols[1]:
    live = st.checkbox(
        "Live updates",
        value=True,
        help=f"Refreshes equipment status and the info board every {REFRESH_SECONDS}s - only "
        "that section updates, nothing else on the page moves.",
    )

_render_legend()

if "scada_selected" not in st.session_state:
    st.session_state["scada_selected"] = None


@st.fragment(run_every=REFRESH_SECONDS if live else None)
def _live_section(plant: str) -> None:
    """
    Equipment status buttons + the selected equipment's detail + the
    info board - everything that needs live data. Runs on its own
    timer (or when a button inside it is clicked) without touching
    anything outside this function. Equipment are real st.button
    widgets now (not raw SVG/HTML) specifically because native widget
    reruns are far smoother than replacing a big HTML blob via
    st.markdown - the previous version's giant SVG-with-icons string
    got wholesale-replaced every refresh, which is visually
    indistinguishable from a full page reload even though it was
    technically fragment-scoped.
    """
    database = DatabaseManager(db_path=MACHINE_DATABASE_PATH)
    rule_engine = RuleEngine(database_path=CONFIG_DATABASE_PATH)

    equipment_list = _load_equipment(plant)
    latest = database.get_latest_all()
    latest_text = database.get_latest_text_all()

    evaluated = [_equipment_status(eq, latest, latest_text, rule_engine) for eq in equipment_list]
    by_name = {eq["name"]: eq for eq in evaluated}

    by_category: dict[str, list[dict]] = {}
    for eq in evaluated:
        by_category.setdefault(eq["category"], []).append(eq)

    placed_names = set()
    zone_groups = []
    for zone in ZONES:
        zone_equipment = []
        for category in sorted(zone["categories"]):
            for eq in by_category.get(category, []):
                zone_equipment.append(eq)
                placed_names.add(eq["name"])
        if zone_equipment:
            zone_groups.append((zone["name"], sorted(zone_equipment, key=lambda e: e["display_name"])))

    leftover = [eq for eq in evaluated if eq["name"] not in placed_names]
    if leftover:
        zone_groups.append(("Other", sorted(leftover, key=lambda e: e["display_name"])))

    st.markdown(_button_color_css(evaluated), unsafe_allow_html=True)

    floor_col, board_col = st.columns([3, 1])

    with floor_col:
        for zone_name, zone_equipment in zone_groups:
            with st.container(border=True):
                st.caption(zone_name.upper())
                button_cols = st.columns(min(6, len(zone_equipment)) or 1)
                for index, eq in enumerate(zone_equipment):
                    label = f'{eq["icon"]} {eq["code"]}'
                    with button_cols[index % len(button_cols)]:
                        with st.container(key=_equipment_button_key(eq["name"])):
                            if st.button(
                                label,
                                key=f"scada_btn_{plant}_{eq['name']}",
                                help=f'{eq["display_name"]} - {SEVERITY_LABEL[eq["overall_severity"]]}',
                                use_container_width=True,
                            ):
                                st.session_state["scada_selected"] = eq["name"]

        st.divider()

        selected = by_name.get(st.session_state.get("scada_selected"))
        if selected:
            st.subheader(selected["display_name"])
            _render_equipment_detail(selected)
        else:
            st.info("Click any equipment button above to see its live tag values.")

    with board_col:
        _render_plant_board("p01", database, rule_engine)
        st.divider()
        _render_plant_board("p02", database, rule_engine)
        st.caption(f"As of {datetime.now().strftime('%H:%M:%S')}")


_live_section(plant)
