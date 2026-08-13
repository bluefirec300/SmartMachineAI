# SCADA Floor Plan: flicker-free live view + main-app integration

## Purpose

The SCADA Floor Plan page (`ui/pages/12_SCADA_Floor_Plan.py`) currently
lives outside the main app, run standalone on port 8503, and its live
section visibly flashes every refresh cycle. This was root-caused
(2026-08-12, see CLAUDE.md "Round four") to a genuine Streamlit
architectural constraint: `st.fragment(run_every=...)` clears and
redraws *everything* inside it on every tick, native widgets included
- not fixable by changing widget types, only worked around by slowing
the refresh interval (currently 30s) to make the redraw infrequent
enough not to be disruptive.

This spec covers two changes, done together since they touch the same
file:

1. **Make the live view genuinely flicker-free**, refreshing every 2
   seconds, by moving the "live" part of the page out of Streamlit's
   own rerun mechanism entirely.
2. **Move the page into the main app** (port 8501), placed in the
   sidebar directly below "Ask AI", replacing its standalone-on-8503
   existence.

## Current implementation (for reference)

`ui/pages/12_SCADA_Floor_Plan.py` (666 lines) currently:
- Loads equipment/tags per plant from `config.db` (`_load_equipment`,
  cached 5s).
- Evaluates every tag's severity via `RuleEngine` (`_tag_severity`,
  `_equipment_status`).
- Renders a room-grouped grid of `st.button` equipment widgets, colored
  via injected CSS keyed to each button's `st.container(key=...)`.
- Renders a per-plant "info board" (electrical readings, utilities,
  plant health counts) for both P01 and P02.
- Renders a detail panel for whichever equipment was last clicked
  (`st.session_state["scada_selected"]`).
- All of the above lives inside one `@st.fragment(run_every=30)`.
- Runs standalone (`st.set_page_config` at module level, own login-free
  process on port 8503) - not registered in `ui/Home.py`'s navigation.

## Architecture

Two independent halves, replacing the single fragment:

### A. Background snapshot writer

A daemon thread, started exactly once per `streamlit.service` process
(guarded via `st.cache_resource`, which Streamlit caches across every
session sharing that process - not per-session, so only one writer
thread ever runs regardless of how many browser tabs/users are
connected). Every 2 seconds it:

1. Reads live data the same way `_live_section()` does today
   (`DatabaseManager.get_latest_all()`/`get_latest_text_all()`,
   `RuleEngine.evaluate_summary()`), for **both** P01 and P02.
2. Builds one JSON document (schema below) covering every equipment
   instance in both plants, plus both plants' info-board figures.
3. Writes it to `manuals/_scada_live.json` (see "Where the file lives"
   below), atomically (write to a temp file, then rename over the
   real path, so the browser never reads a half-written file).

Reuses all of today's data-gathering functions unchanged
(`_load_equipment`, `_tag_severity`, `_equipment_status`,
`_todays_energy_kwh`) - only *how the result reaches the browser*
changes.

### B. Static, JS-driven page

The page's Python code runs **once** per page load (no
`run_every`, no fragment) and renders:
- The static shell (page title, caption, legend, room layout, one
  button-shaped element per equipment tagged with
  `data-equipment="<name>"`, an empty detail-panel container, plant
  toggle, both info-board containers) - built directly from
  `_load_equipment()`'s zone/category grouping, same as today, just
  rendered once instead of every 30s.
- A `<script>` block (embedded via `st.components.v1.html()`, since
  Streamlit's `st.markdown` does not execute `<script>` tags) that:
  - Every 2 seconds, `fetch()`s `_scada_live.json` and updates: each
    equipment element's background color/severity, the currently-open
    detail panel's tag rows (if any equipment is selected), and both
    info boards' numbers - all via direct DOM updates, never
    re-rendering the whole shell.
  - Handles plant-toggle clicks and equipment clicks entirely
    client-side (show/hide the right zone group, populate the detail
    panel from the already-fetched JSON) - no Streamlit rerun, no
    server round-trip, for either interaction.
  - Tracks the snapshot's own `generated_at` timestamp; if it hasn't
    advanced for more than ~15 seconds (background thread died, or
    the file is stale), shows a small "data may be stale" notice
    instead of silently presenting old numbers as current.

Default state on load matches today's: P01 selected, no equipment
selected (the "click any equipment to see its values" placeholder
message shows in the detail panel).

### Where the file lives

This app already serves files directly to the browser via
`ui/static` (a symlink to `../manuals`, set up for in-browser manual
viewing - `.streamlit/config.toml`'s `enableStaticServing=True`).
Reused here rather than standing up a second static-serving mechanism:
the writer thread writes to `manuals/_scada_live.json` (leading
underscore - obviously not a real manual, in case anyone browses that
folder), reachable by the browser at `/app/static/_scada_live.json`.
`manuals/*` is already fully gitignored, so this generated,
constantly-changing file never risks being committed.

### JSON snapshot schema (sketch)

```json
{
  "generated_at": "2026-08-13 10:15:32",
  "plants": {
    "p01": {
      "zones": [
        {"name": "Utilities Yard", "equipment": ["p01_air_compressor_ac01", "..."]}
      ],
      "equipment": {
        "p01_air_compressor_ac01": {
          "display_name": "Air Compressor AC01 (P01)",
          "code": "AC01",
          "icon": "💨",
          "overall_severity": "normal",
          "tags": [
            {"tag_name": "P01.UTILITY.AC01.Pressure", "unit": "bar",
             "value": 7.24, "severity": "normal", "updated": "2026-08-13 10:15:30"}
          ]
        }
      },
      "board": {
        "today_kwh": 21.4, "power_kw": 26.1, "pf": 0.94, "frequency": 50.0,
        "currents": [41.2, 40.8, 41.5], "voltages": [415, 414, 416],
        "air_pressure": 7.1, "air_flow": 210.0,
        "water_pressure": 3.2, "water_level": 78.0,
        "alarm_count": 0, "warning_count": 1, "normal_count": 37,
        "not_evaluated_count": 0, "no_data_count": 0, "total": 38
      }
    },
    "p02": { "...": "same shape" }
  }
}
```

## Integration into the main app

- `ui/Home.py`: insert
  `st.Page("pages/12_SCADA_Floor_Plan.py", title="SCADA Floor Plan", icon="🗺️")`
  into `general_pages`, directly after the Ask AI entry. It
  automatically inherits `auth.require_login()`'s login gate like
  every other page - no page-specific auth code needed.
- `ui/pages/12_SCADA_Floor_Plan.py`: remove the module-level
  `st.set_page_config(...)` call (Home.py already owns this; calling
  it twice raises an error) and the "experimental prototype / run
  standalone on 8503" framing in the header comment and on-page
  caption - it's a normal page now, not a side experiment.
- The standalone port-8503 way of running this page is retired -
  no more "run standalone" instructions, no separate process to keep
  alive.

## Error handling

- **Snapshot file missing** (e.g. right after `streamlit.service`
  restarts, before the writer thread's first tick): the page shows
  "waiting for live data..." instead of a blank/broken panel.
- **Snapshot file unreadable/malformed**: same fallback message,
  logged to the browser console for debugging, never a JS exception
  visible to the user.
- **Snapshot stale** (`generated_at` not advancing): a visible "data
  may be stale" notice, per above - distinct from "waiting", since
  it means something WAS working and stopped.
- **Writer thread itself**: wrapped in a try/except per tick (one bad
  read/write must never kill the thread permanently, same discipline
  `app/plc_logger.py`'s per-tag try/except already follows) - logs the
  error and tries again next tick.

## Testing plan

1. `py_compile` on the modified page and `ui/Home.py`.
2. `streamlit.testing.v1.AppTest` - page renders with no exception,
   confirms the background thread singleton starts.
3. Real end-to-end check against the live running app (not just
   AppTest, since AppTest can't exercise real browser JS): confirm
   `manuals/_scada_live.json` is actually being written and its
   `generated_at` advances every ~2s; confirm the page loads inside
   the main app at the right sidebar position; confirm equipment
   click and plant-toggle both update the visible panel without a
   page reload (checked via browser dev tools / curl of the JSON
   alongside visual confirmation).
4. Full 95-test regression suite (expect the same pre-existing
   baseline failures, unrelated to this page).
5. `streamlit.service` restart to pick up the change, confirmed via
   its journal.

## Out of scope (explicitly not doing)

- A true bidirectional custom Streamlit component (React/npm build
  toolchain) - the plain embedded-JS + polling approach above achieves
  the flicker-free goal without new build tooling, consistent with
  this project's existing "no heavy new dependencies" preference.
- Editing/writing from this page (it was and remains read-only,
  same as Live Data).
- Any change to `plc_logger.service`/`event_monitor.service` - the
  snapshot writer is fully self-contained inside the Streamlit
  process, reusing existing read-only data-access functions.
