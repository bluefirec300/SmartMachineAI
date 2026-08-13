from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.scada_floor_plan_data import (
    SEVERITY_COLOR,
    SEVERITY_DOT,
    SEVERITY_LABEL,
    SEVERITY_TEXT,
    _load_equipment,
)

# ---------------------------------------------------------------------------
# SCADA-style mimic diagram - part of the main app since 2026-08-13. No real
# factory floor plan exists to copy - the room layout is an imagined,
# plausible arrangement built from the tag-naming convention's own area
# codes (P01.UTILITY.*, P01.COLDROOM.*, P01.PROD.*, ...). Every equipment
# instance, tag, and live value shown is real data from the running system -
# only the physical room positions are invented.
#
# Architecture (see docs/superpowers/specs/2026-08-13-scada-floor-plan-live-
# view-design.md for the full design rationale): a background thread
# (ui/scada_snapshot_writer.py, started below) writes a JSON snapshot of
# both plants to manuals/_scada_live.json every 2 seconds. This page renders
# its shell ONCE (no st.fragment, no run_every - three earlier rounds of
# fixes confirmed, against Streamlit's own fragment.py source, that
# run_every fragments redraw everything inside them on every tick,
# native widgets included, which is what caused the visible flashing this
# rewrite exists to fix). The embedded <script> below polls that JSON file
# directly and updates the DOM in place - no Streamlit rerun of any kind is
# involved in the live updates, so nothing can flash.
# ---------------------------------------------------------------------------

from ui.scada_snapshot_writer import start_snapshot_writer

start_snapshot_writer()

FONT_STACK = "'Source Sans Pro', -apple-system, 'Segoe UI', sans-serif"
MONO_STACK = "'Source Code Pro', 'SF Mono', Consolas, monospace"

st.title("\U0001F3ED SCADA Floor Plan")
st.caption(
    "Room layout is imagined (no real factory drawing exists); every status/value shown "
    "is real, live data from the system, refreshed every 2 seconds. Click any equipment "
    "below its room to see its live tag values."
)

legend_cols = st.columns(5)
for col, key in zip(legend_cols, ["alarm", "warning", "normal", "not_evaluated", "no_data"]):
    with col:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:8px;font-family:{FONT_STACK};">'
            f'<div style="width:16px;height:16px;border-radius:4px;'
            f'background:{SEVERITY_COLOR[key]};flex-shrink:0;"></div>'
            f'<span style="font-size:13px;">{SEVERITY_LABEL[key]}</span></div>',
            unsafe_allow_html=True,
        )


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_plant_shell_html(plant: str) -> str:
    """
    Static (never-changing) DOM structure for one plant: zone containers
    with one equipment "card" div per equipment instance, each tagged
    with data-name so the polling script can find and update it. Colors
    start at SEVERITY_COLOR["no_data"] since no live snapshot has been
    fetched yet on first paint.
    """
    from ui.scada_floor_plan_data import group_into_zones

    equipment_list = _load_equipment(plant)
    zone_groups = group_into_zones(equipment_list)

    zones_html = []
    for zone_name, zone_equipment in zone_groups:
        cards = []
        for eq in zone_equipment:
            cards.append(
                f'<div class="scada-card" id="scada-eq-{_escape(eq["name"])}" '
                f'data-name="{_escape(eq["name"])}" data-plant="{plant}" '
                f'title="{_escape(eq["display_name"])} - waiting for live data..." '
                f'style="background:{SEVERITY_COLOR["no_data"]};color:{SEVERITY_TEXT["no_data"]};">'
                f'{eq["icon"]} {_escape(eq["code"])}</div>'
            )
        zones_html.append(
            f'<div class="scada-zone"><div class="scada-zone-title">{_escape(zone_name.upper())}</div>'
            f'<div class="scada-zone-cards">{"".join(cards)}</div></div>'
        )

    display_style = "" if plant == "p01" else "display:none;"
    return f'<div class="scada-plant" id="scada-plant-{plant}" style="{display_style}">{"".join(zones_html)}</div>'


shell_html = "".join(_build_plant_shell_html(plant) for plant in ("p01", "p02"))
severity_colors_json = json.dumps(SEVERITY_COLOR)
severity_text_json = json.dumps(SEVERITY_TEXT)
severity_label_json = json.dumps(SEVERITY_LABEL)
severity_dot_json = json.dumps(SEVERITY_DOT)

page_html = f"""
<style>
  body {{ margin:0; background:#0d1117; color:#e6edf3; font-family:{FONT_STACK}; }}
  .scada-toolbar {{ display:flex; gap:8px; align-items:center; margin-bottom:12px; }}
  .scada-toolbar button {{
    background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px;
    padding:6px 16px; font-size:13px; cursor:pointer; font-family:{FONT_STACK};
  }}
  .scada-toolbar button.active {{ background:#3d5afe; border-color:#3d5afe; }}
  #scada-stale-banner {{
    display:none; background:#3a2a06; color:#f5a524; border:1px solid #f5a524;
    border-radius:6px; padding:8px 12px; margin-bottom:12px; font-size:13px;
  }}
  .scada-layout {{ display:flex; gap:24px; align-items:flex-start; }}
  .scada-floor {{ flex:3; }}
  .scada-boards {{ flex:1; min-width:260px; }}
  .scada-zone {{ border:1px solid #30363d; border-radius:8px; padding:10px 12px; margin-bottom:12px; }}
  .scada-zone-title {{ font-size:12px; color:#8a8f98; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:8px; }}
  .scada-zone-cards {{ display:flex; flex-wrap:wrap; gap:8px; }}
  .scada-card {{
    padding:8px 14px; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer;
    border:1px solid rgba(255,255,255,0.15); user-select:none;
  }}
  .scada-card:hover {{ filter:brightness(1.1); }}
  #scada-detail {{ border-top:1px solid #30363d; padding-top:12px; margin-top:8px; }}
  .scada-detail-row {{
    display:grid; grid-template-columns:14px 1fr 110px 150px; gap:12px;
    align-items:center; padding:6px 0; border-bottom:1px solid #21262d; font-size:13px;
  }}
  .scada-detail-header {{ color:#8a8f98; font-size:11px; text-transform:uppercase; letter-spacing:0.05em; }}
  .scada-dot {{ width:10px; height:10px; border-radius:50%; }}
  .scada-board-heading {{ font-size:12px; font-weight:700; color:#c9d1d9; margin:8px 0 2px; }}
  .scada-board-row {{
    display:flex; justify-content:space-between; align-items:baseline; padding:2px 0;
    border-bottom:1px solid #1c2128; font-size:12px;
  }}
  .scada-board-row .label {{ font-size:11.5px; color:#8a8f98; }}
  .scada-board-row .value {{ font-size:12.5px; font-weight:600; white-space:nowrap; }}
</style>

<div id="scada-stale-banner">⚠️ Live data may be stale - the background updater hasn't reported in recently.</div>

<div class="scada-toolbar">
  <button id="scada-tab-p01" class="active" onclick="scadaSetPlant('p01')">P01</button>
  <button id="scada-tab-p02" onclick="scadaSetPlant('p02')">P02</button>
  <span id="scada-asof" style="font-size:12px;color:#8a8f98;margin-left:8px;">waiting for live data...</span>
</div>

<div class="scada-layout">
  <div class="scada-floor">
    {shell_html}
    <div id="scada-detail">
      <div id="scada-detail-body" style="font-size:13px;color:#8a8f98;">
        Click any equipment above to see its live tag values.
      </div>
    </div>
  </div>
  <div class="scada-boards">
    <div style="font-weight:700;font-size:13px;">⚡ P01 Main Incomer</div>
    <div id="scada-board-p01"></div>
    <hr style="border-color:#30363d;margin:12px 0;">
    <div style="font-weight:700;font-size:13px;">⚡ P02 Main Incomer</div>
    <div id="scada-board-p02"></div>
  </div>
</div>

<script>
const SEVERITY_COLOR = {severity_colors_json};
const SEVERITY_TEXT = {severity_text_json};
const SEVERITY_LABEL = {severity_label_json};
const SEVERITY_DOT = {severity_dot_json};

let scadaCurrentPlant = "p01";
let scadaSelectedEquipment = null;
let scadaLastGeneratedAt = null;
let scadaLastAdvanceTime = Date.now();
let scadaLatestSnapshot = null;

function scadaEscapeHtml(text) {{
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}}

function scadaSetPlant(plant) {{
  scadaCurrentPlant = plant;
  document.getElementById("scada-plant-p01").style.display = plant === "p01" ? "" : "none";
  document.getElementById("scada-plant-p02").style.display = plant === "p02" ? "" : "none";
  document.getElementById("scada-tab-p01").classList.toggle("active", plant === "p01");
  document.getElementById("scada-tab-p02").classList.toggle("active", plant === "p02");
  if (scadaSelectedEquipment && !scadaSelectedEquipment.startsWith(plant + "_")) {{
    scadaSelectedEquipment = null;
    document.getElementById("scada-detail-body").innerHTML =
      "Click any equipment above to see its live tag values.";
  }}
}}

function scadaFormatNumber(value, decimals) {{
  if (value === null || value === undefined) return "-";
  return Number(value).toFixed(decimals);
}}

function scadaBoardHeading(text) {{
  return '<div class="scada-board-heading">' + scadaEscapeHtml(text) + '</div>';
}}

function scadaBoardRow(label, value, dot) {{
  const dotHtml = dot ? '<span style="margin-right:4px;">' + scadaEscapeHtml(dot) + '</span>' : '';
  return '<div class="scada-board-row"><span class="label">' + scadaEscapeHtml(label) +
    '</span><span class="value">' + dotHtml + scadaEscapeHtml(value) + '</span></div>';
}}

function scadaRenderBoard(plant, board) {{
  let html = scadaBoardHeading('Electrical');
  html += scadaBoardRow('Today\\'s consumption',
    board.today_kwh !== null ? scadaFormatNumber(board.today_kwh, 1) + ' kWh' : '-');
  html += scadaBoardRow('Power draw',
    board.power_kw !== null ? scadaFormatNumber(board.power_kw, 1) + ' kW' : '-');
  if (board.currents.some(c => c !== null)) {{
    const currentText = board.currents.map(c => c !== null ? scadaFormatNumber(c, 1) : '-').join(' / ');
    html += scadaBoardRow('Current L1/L2/L3', currentText + ' A');
  }}
  if (board.voltages.some(v => v !== null)) {{
    const voltageText = board.voltages.map(v => v !== null ? scadaFormatNumber(v, 0) : '-').join(' / ');
    html += scadaBoardRow('Voltage L1-2/L2-3/L3-1', voltageText + ' V');
  }}
  html += scadaBoardRow('Power factor / freq',
    (board.pf !== null && board.frequency !== null)
      ? scadaFormatNumber(board.pf, 2) + ' / ' + scadaFormatNumber(board.frequency, 1) + ' Hz' : '-');

  if (board.air_pressure !== null || board.water_pressure !== null) {{
    html += scadaBoardHeading('Utilities');
    if (board.air_pressure !== null) {{
      let text = scadaFormatNumber(board.air_pressure, 1) + ' bar';
      if (board.air_flow !== null) text += ' / ' + scadaFormatNumber(board.air_flow, 0) + ' Nm³/h';
      html += scadaBoardRow('Compressed air header', text);
    }}
    if (board.water_pressure !== null) {{
      let text = scadaFormatNumber(board.water_pressure, 1) + ' bar';
      if (board.water_level !== null) text += ' / ' + scadaFormatNumber(board.water_level, 0) + '% tank';
      html += scadaBoardRow('Water supply header', text);
    }}
  }}

  const total = board.total || 1;
  html += scadaBoardHeading('Plant Health');
  html += scadaBoardRow('Equipment normal',
    board.normal_count + '/' + board.total + ' (' + Math.round(100 * board.normal_count / total) + '%)');
  html += scadaBoardRow('In alarm', String(board.alarm_count), board.alarm_count ? SEVERITY_DOT.alarm : '');
  html += scadaBoardRow('In warning', String(board.warning_count), board.warning_count ? SEVERITY_DOT.warning : '');
  if (board.not_evaluated_count || board.no_data_count) {{
    html += scadaBoardRow('No threshold / no data', board.not_evaluated_count + ' / ' + board.no_data_count);
  }}

  document.getElementById('scada-board-' + plant).innerHTML = html;
}}

function scadaRenderDetail(equipmentName) {{
  if (!scadaLatestSnapshot) return;
  const plant = equipmentName.startsWith('p01_') ? 'p01' : 'p02';
  const eq = scadaLatestSnapshot.plants[plant].equipment[equipmentName];
  if (!eq) return;

  const alarmCount = eq.tags.filter(t => t.severity === 'alarm').length;
  const warningCount = eq.tags.filter(t => t.severity === 'warning').length;
  let summary;
  if (alarmCount || warningCount) {{
    const bits = [];
    if (alarmCount) bits.push(alarmCount + ' alarm');
    if (warningCount) bits.push(warningCount + ' warning');
    summary = '<div style="color:#f5a524;">' + bits.join(' and ') + ' on this equipment.</div>';
  }} else {{
    summary = '<div style="color:#3dd68c;">Everything within configured limits.</div>';
  }}

  let rows = '<div class="scada-detail-row scada-detail-header"><div></div><div>Tag</div>' +
    '<div style="text-align:right;">Value</div><div style="text-align:right;">Updated</div></div>';
  const sortedTags = [...eq.tags].sort((a, b) => a.tag_name.localeCompare(b.tag_name));
  for (const tag of sortedTags) {{
    const value = tag.value !== null && tag.value !== undefined ? tag.value : '-';
    const unit = tag.unit ? ' ' + tag.unit : '';
    const updated = tag.updated || 'never logged';
    rows += '<div class="scada-detail-row">' +
      '<div class="scada-dot" style="background:' + SEVERITY_COLOR[tag.severity] + ';"></div>' +
      '<div style="font-family:monospace;font-size:13px;">' + scadaEscapeHtml(tag.tag_name) + '</div>' +
      '<div style="font-weight:600;font-size:13px;text-align:right;">' + scadaEscapeHtml(value) + scadaEscapeHtml(unit) + '</div>' +
      '<div style="color:#8a8f98;font-size:12px;text-align:right;">' + scadaEscapeHtml(updated) + '</div></div>';
  }}

  document.getElementById('scada-detail-body').innerHTML =
    '<h4 style="margin:4px 0 8px;">' + scadaEscapeHtml(eq.display_name) + '</h4>' + summary + rows;
}}

function scadaApplySnapshot(snapshot) {{
  scadaLatestSnapshot = snapshot;

  if (snapshot.generated_at !== scadaLastGeneratedAt) {{
    scadaLastGeneratedAt = snapshot.generated_at;
    scadaLastAdvanceTime = Date.now();
    document.getElementById('scada-stale-banner').style.display = 'none';
  }}
  document.getElementById('scada-asof').textContent = 'As of ' + snapshot.generated_at;

  for (const plant of ['p01', 'p02']) {{
    for (const [name, eq] of Object.entries(snapshot.plants[plant].equipment)) {{
      const card = document.getElementById('scada-eq-' + name);
      if (!card) continue;
      card.style.background = SEVERITY_COLOR[eq.overall_severity];
      card.style.color = SEVERITY_TEXT[eq.overall_severity];
      card.title = eq.display_name + ' - ' + SEVERITY_LABEL[eq.overall_severity];
    }}
    scadaRenderBoard(plant, snapshot.plants[plant].board);
  }}

  if (scadaSelectedEquipment) {{
    scadaRenderDetail(scadaSelectedEquipment);
  }}
}}

function scadaPoll() {{
  fetch('/app/static/_scada_live.json?_=' + Date.now())
    .then(response => {{
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    }})
    .then(scadaApplySnapshot)
    .catch(error => {{
      console.error('SCADA snapshot fetch failed:', error);
    }});

  if (Date.now() - scadaLastAdvanceTime > 15000) {{
    document.getElementById('scada-stale-banner').style.display = 'block';
  }}
}}

document.addEventListener('click', function (event) {{
  const card = event.target.closest('.scada-card');
  if (!card) return;
  scadaSelectedEquipment = card.dataset.name;
  scadaRenderDetail(scadaSelectedEquipment);
}});

scadaPoll();
setInterval(scadaPoll, 2000);
</script>
"""

components.html(page_html, height=1400, scrolling=True)
