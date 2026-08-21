# FACTORY AI — Development Status

This document tracks progress against `FACTORY_AI_CURRENT_SYSTEM_REPORT.md`
(the original baseline audit, never overwritten) and the master
development roadmap. Updated after every completed phase.

---

## Phase 1 — Factory Data Foundation

**Completed:** 2026-08-15

### Features Added
- `factory` table (Factory → Plant → Area → System → Equipment → Tag hierarchy, level 1). Modeled as a normal multi-row-capable entity (auto-increment PK, no singleton constraint) even though exactly one row is seeded today - future factories/sites need no schema change.
- `plants` table (hierarchy level 2), FK to `factory`. Seeded from the two plant codes (`p01`, `p02`) already present in `equipment.name`'s naming convention.
- `areas` table (hierarchy level 3), FK to `plants`. **Plant-scoped** - Plant 1's "Utilities Yard" and Plant 2's "Utilities Yard" are two distinct rows (`UNIQUE(plant_id, name)`), never shared/ambiguous across plants.
- `systems` table (hierarchy level 4), FK to `areas` (and therefore transitively plant-scoped the same way).

### Features Extended
- `equipment` table gained 17 new nullable columns: `plant_id`, `area_id`, `system_id`, `equipment_type`, `serial_number`, `installation_date`, `commission_date`, `rated_power`, `rated_voltage`, `rated_current`, `rated_flow`, `rated_pressure`, `rated_capacity`, `criticality`, `replacement_cost`, `expected_life_years`, `normal_operating_hours`.
- Existing `equipment.device_number` is reused as Asset ID - no duplicate `asset_id` column added, per explicit direction.

### Database Changes
- File: `database/simulation/config.db` (the active environment). `database/actual/config.db` **not yet migrated** - it's currently empty/unused, so this was deferred; run `python -m engine.factory_structure_migrator --database database/actual/config.db` before that environment is used for real, to keep both environments' schemas in sync.
- Automatic backup taken before the live migration: `database/simulation/config_before_factory_structure_20260815_141410.db`.
- `machine_data.db` (`plc_data`, `plc_text_data`, `machine_events`) - **untouched**, zero rows modified, no schema change. `tags`/`tag_addresses`/`thresholds` also untouched.
- Row counts after migration: 1 factory, 2 plants, 16 areas (8 per plant), 26 systems (13 per plant), 83 equipment rows (unchanged - only columns added, no rows added/removed). 76 of 83 equipment rows have plant/area/system/equipment_type populated; 7 legacy/orphan rows (identified in the original audit, no `p0N_` prefix) intentionally left `NULL`.
- Migration is idempotent - re-running produces identical results, verified directly.

### Important Files Changed
- **New:** `engine/factory_structure_migrator.py` - the migration script (follows the existing `equipment_metadata_migrator.py`/`maintenance_migrator.py` pattern: automatic timestamped backup, additive `ALTER TABLE`, idempotent).
- **New:** `FACTORY_AI_DEVELOPMENT_STATUS.md` (this file).
- No other application code was modified - `ui/`, `app/`, `ai/`, `engine/concept_extractor.py`, `engine/industrial_query_engine.py`, and `simulator/` are all unchanged. Nothing reads the new tables/columns yet (that starts in later phases), so this phase is additive-only with zero behavioral change to any existing page or answer.

### New Configuration
None - no new environment variables, settings.ini entries, or config files.

### New KPIs
None - Phase 1 is data-model only, per its own stated objective. No calculation logic was added.

### New Simulation Features
None - `simulator/tag_dataset_model.py` was not touched.

### Known Issues
- **Area/System taxonomy provenance:** the 16 areas / 26 systems were seeded from `ui/scada_floor_plan_data.py`'s existing `ZONES` room-grouping (already shown on the live SCADA Floor Plan page), split into finer-grained systems per the roadmap's own examples. This is **inferred classification for simulation purposes, not a confirmed real-factory engineering structure**. Every seeded row is stamped `source = 'inferred_from_simulation'` specifically so it can be found and reviewed later:
  ```sql
  SELECT * FROM areas WHERE source = 'inferred_from_simulation';
  SELECT * FROM systems WHERE source = 'inferred_from_simulation';
  ```
  A `source = 'confirmed'` value is reserved for once a plant engineer reviews and validates the real structure - no code currently sets this value.
- The 7 orphaned legacy equipment rows (`chiller`, `cold_room`, `tank`, `pump`, `energy`, `water`, `air` - flagged in the original audit as having zero tags) remain unclassified (`NULL` plant/area/system) and their disposition (delete vs. keep vs. reclassify) is still an open question, unchanged from the audit.
- **`database/actual/config.db` has not been migrated** (see Database Changes above) - **reminder carried forward into Phase 2 and still outstanding**: before the "actual" environment is ever used for a real deployment, run BOTH:
  ```bash
  python -m engine.factory_structure_migrator --database database/actual/config.db
  python -m engine.operating_schedule_migrator --database database/actual/config.db
  ```

### Deferred Work
- No admin UI exists yet to view/edit the new Factory/Plant/Area/System records or the new equipment metadata fields - that is Phase 2's explicit objective, not Phase 1's.
- No existing code (Ask AI, SCADA Floor Plan, reports) reads the new columns yet - they were deliberately left wired to their existing naming-convention-based logic in this phase, to keep Phase 1 zero-risk to current behavior. Wiring them in is future-phase work (Ask AI context expansion is Phase 17; SCADA Floor Plan wiring wasn't explicitly scheduled but would be a natural fit alongside Phase 7's energy dashboard work).
- Real values for company/country/currency/timezone/floor area/production capacity/equipment ratings/serial numbers/installation dates are all still `NULL` - nothing in the current system has this data. They'll be populated via Phase 2's configuration UI or manual entry.

### Next Recommended Phase
Phase 2 — Factory & Engineering Configuration (per the roadmap: build the admin UI to populate factory profile, plant configuration, and operating schedules on top of the schema this phase created).

---

## Verification performed (Phase 1)

1. Full regression suite: `python -m unittest discover tests` → 110 tests, 5 failures - identical to the pre-existing documented baseline (all in dead/unimported code paths), zero new failures.
2. `AppTest` on Live Data, Equipment & Tag Configuration, SCADA Floor Plan, and Ask AI pages - all render with no exception.
3. Migration tested first against a throwaway copy of the live database (not the real one), verified in full detail (table contents, row counts, per-equipment backfill correctness, orphan rows correctly left NULL), then re-run twice to confirm idempotency, before being run against the real `database/simulation/config.db`.
4. Confirmed zero rows changed in `machine_data.db` and zero rows changed in `tags`/`tag_addresses`/`thresholds`.
5. No new Python dependencies - pure SQLite schema work using the standard library only.

---

## Phase 2 — Factory & Engineering Configuration

**Completed:** 2026-08-15

### Features Added
- **`ui/pages/13_Factory_Configuration.py`** - new page, visible to every signed-in role, edit controls restricted to Administrator only (`can_edit("admin")` - deliberately narrower than Setpoints' admin+engineer, per confirmed direction). Six tabs: Factory Profile, Plants, Areas & Systems, Equipment Metadata, Operating Schedules, Shutdown/Maintenance Windows.
- **Configuration Completeness** - a weighted, equipment-type-aware, automatically-calculated score (never manually entered). Shown at the top of the new page: overall %, a Factory/Plants/Equipment breakdown, and a ranked "what's missing, in order of importance" list. Backed by a new standalone calculation module, `engine/configuration_completeness.py` - pure Python, no LLM (per Rule 4), returns structured data rather than HTML so it's directly reusable by later phases (Data Health, AI confidence), exactly as scoped.
  - Weighting is two-tier: administrative fields (names, codes, manufacturer/model text) score low; engineering-significant fields (ratings, criticality, capacity) score higher.
  - **Equipment-type-aware**: an `EQUIPMENT_TYPE_WEIGHT_OVERRIDES` dict (same profile-dict pattern already used for `TAG_PROFILES`/`THRESHOLD_PROFILES`/Phase 1's `CATEGORY_TO_AREA_SYSTEM`) boosts the fields that actually matter per type - e.g. `rated_power`/`rated_pressure`/`rated_flow` for an Air Compressor, `rated_power`/`rated_capacity` for a Chiller - and zeroes out fields that don't physically apply (a Weather Node or Area Monitoring sensor is never penalized for missing `rated_power`/`rated_voltage`/etc.).
  - Live result on the real database at completion: **12% overall** (Factory 11%, Plants 20% avg, Equipment 12% avg) - expected, since almost none of the new Phase 1 fields have real values yet. The ranked missing-list correctly leads with the highest-weight gaps (e.g. Air Compressor `rated_power`/`rated_flow`/`rated_pressure`, all weight 4).
  - Score is computed live on every page load, never persisted/cached - simplest correct option at the current data size (83 equipment rows).
- **`shift_definitions`** table - named shifts (start/end time, working days as a plain list, active flag), each either factory-wide (`plant_id IS NULL`) or scoped to one plant. Supports multiple shifts per plant, not just one start/stop pair.
- **`non_production_periods`** table - shutdowns, maintenance windows, and holidays under one table (`period_type` distinguishes them), each a labeled datetime range, factory-wide or plant-scoped.
- **Area/System provenance is now user-correctable in the UI**, per requirement #2: every area/system shows an "🟢 confirmed" or "⚪ inferred (simulation)" badge; editing a name/description (or creating a new area/system) sets `source = 'confirmed'` automatically. Verified directly against `ui/factory_config_data.py`'s `update_area()`/`update_system()`/`create_area()`/`create_system()`.

### Features Extended
- `equipment` table gained **one** new nullable column, `operating_pattern` (`24_7` / `shift_based` / `seasonal` / `on_demand`) - deliberately the only equipment schema change this phase; every other Phase 2 field need was already covered by Phase 1's columns.
- `ui/Home.py` - one new nav entry (`Factory Configuration`, in the general/"Engineer" group, not the Admin-only hidden group).

### Database Changes
- New tables: `shift_definitions`, `non_production_periods` (migration: `engine/operating_schedule_migrator.py`, same backup+additive pattern as every prior migrator).
- `equipment.operating_pattern` column added.
- Applied to `database/simulation/config.db` only. Backup taken: `database/simulation/config_before_operating_schedule_20260815_143807.db`.
- **`database/actual/config.db` still not migrated** - now needs BOTH the Phase 1 and Phase 2 migrators before that environment is used (see the reminder added to the Phase 1 section above).
- `machine_data.db`, `tags`, `tag_addresses`, `thresholds` - untouched, same as Phase 1.
- Migration tested on a throwaway copy first, re-run twice to confirm idempotency, before being applied to the live database - same discipline as Phase 1.

### Important Files Changed
- **New:** `engine/operating_schedule_migrator.py`, `engine/configuration_completeness.py`, `ui/factory_config_data.py`, `ui/pages/13_Factory_Configuration.py`, `tests/test_configuration_completeness.py`.
- **Modified:** `ui/Home.py` (nav entry only).
- No changes to Ask AI (`app/ask.py`, `engine/concept_extractor.py`, `engine/industrial_query_engine.py`), the simulator, KPI/trend calculation, or the historian - confirmed via `git status` at the end of this phase, per requirement #10.

### New Configuration
None - no new environment variables or settings.ini entries. All configuration entered through the new page is stored in the database tables above.

### New KPIs
None in the energy/production sense (that's later phases) - Configuration Completeness is a new *meta*-metric about the configuration itself, not a factory-operations KPI.

### New Simulation Features
None - `simulator/tag_dataset_model.py` untouched.

### Known Issues
- The chiller-type "cooling capacity" question is resolved by reusing the generic `rated_capacity` field (per confirmed direction) - its meaning is genuinely different per equipment type (cooling capacity for a Chiller, volume for a Tank, backup capacity for a UPS). Anyone building on this later needs to know that `rated_capacity` is contextual, not a single well-typed physical quantity.
- Operating-schedule data capture only - no after-hours-energy detection logic uses `shift_definitions`/`non_production_periods`/`operating_pattern` yet (that's explicitly future analytics work, not Phase 2's job).
- No per-equipment shift assignment - equipment only distinguishes "runs 24/7" from "follows its plant's shift schedule" via `operating_pattern`; it does not point at a specific shift record. Flagged in the approved plan as a deliberate simplicity trade-off.

### Deferred Work
- Real values for every new field are still mostly unfilled (12% overall completeness) - that's expected; Phase 2 built the capability to enter them, not the data itself.
- Energy tariff configuration - explicitly Phase 3, not touched here.
- Wiring Configuration Completeness into a future Data Health page / AI confidence signal - the calculator was built reusable for this, but nothing calls it outside the new Factory Configuration page yet.

### Next Recommended Phase
Phase 3 — Energy Tariff & Cost Configuration (per the roadmap).

---

## Verification performed (Phase 2)

1. Full regression suite: `python -m unittest discover tests` → 124 tests (14 new, for `engine/configuration_completeness.py`), 5 failures - identical pre-existing baseline, zero new failures.
2. `AppTest` on the new Factory Configuration page plus Live Data, Setpoints, Service & Maintenance, Equipment & Tag Configuration, SCADA Floor Plan, and Ask AI - all render with no exception; the new page's widget count/values (6 tabs, correct completeness metrics matching a direct manual calculation) verified directly, not just "no crash."
3. Both new migrations tested on throwaway database copies first, re-run twice each to confirm idempotency, before being applied to the live database with automatic backups.
4. `git status` confirms only `ui/Home.py` was modified among existing files - Ask AI, simulator, and historian code paths are completely untouched.
5. No new Python dependencies.

---

## Phase 3 — Energy Tariff & Cost Configuration

**Completed:** 2026-08-15

### Features Added
- **`energy_tariffs` table** - append-only/versioned tariff records, per scope (factory-wide via `plant_id IS NULL`, or a specific plant). `mode` distinguishes `simple` (currency + a single `energy_rate`) from `advanced` (adds `peak_rate`/`off_peak_rate`/`peak_start`/`peak_end`/`maximum_demand_charge`/`contract_maximum_demand`/`fixed_monthly_charge`/`surcharge_percent`/`tax_percent`/`billing_cycle` - advanced mode is a superset of simple mode's fields, not a different shape).
- **`engine/energy_tariff.py`** - the versioning + cost-calculation engine, pure Python, no LLM (Rule 4):
  - `create_tariff()` never edits an existing row's rate fields - it closes out (sets `expiry_date` on) whichever tariff was previously open-ended for that scope, then inserts the new one as the new open-ended record. **Verified directly**: a tariff entered for "2026-06-01 onward" leaves the prior tariff's own rate fields completely untouched, only its `expiry_date` gets set to `2026-06-01`.
  - `get_tariff_for_date()` - historical lookup, returns whichever tariff row was actually in effect on a given date. **Verified directly**: a March 2026 lookup and a July 2026 lookup against the same tariff history correctly return the pre- and post-change rates respectively, and the exact boundary date correctly picks up the new rate, not the old one.
  - `get_current_tariff()` - returns the scope's currently-effective tariff, **auto-generating and persisting a marked-simulated tariff** (`is_simulated=1`, MYR 0.50/kWh - the roadmap's own worked example) the first time a scope with no configured tariff is accessed, so the UI/any cost calculation always has a real number rather than a blank state. Verified this auto-generated row is created once and reused on subsequent calls, not recreated every time.
  - `calculate_energy_cost(kwh, tariff)` - flat-rate `kwh × energy_rate`. Deliberately does not attempt to split cost by peak/off-peak kWh yet (that needs per-interval consumption data that isn't computed anywhere in the system - explicitly Phase 6 Energy KPI Engine's job); advanced-mode tariffs already store the peak/off-peak/demand-charge fields now so that future work has them available without another schema change.
- **New "Energy Tariff" tab** added to the existing `ui/pages/13_Factory_Configuration.py` (extended, not a new page - per Rule 1) - scope selector (Factory-wide/P01/P02), the `SIMULATION TARIFF` banner when applicable (exact roadmap wording), a live "today's energy so far / today's cost so far" preview (reuses `ui.scada_floor_plan_data._todays_accumulated_total` - the same reset-safe accumulation helper the SCADA board uses, not a re-implementation), a Simple/Advanced mode toggle, the versioned save-new-tariff form, and a read-only tariff history list per scope.
  - Live result against the real database at completion: factory-wide today = 349.6 kWh × MYR 0.50 = **MYR 174.80** - correct, verified via `AppTest`.

### Features Extended
None beyond the new tab above - no existing table/page schema changed.

### Database Changes
- New table: `energy_tariffs` (migration: `engine/energy_tariff_migrator.py`, same backup+additive pattern as every prior migrator).
- Applied to `database/simulation/config.db` only. Backup taken: `database/simulation/config_before_energy_tariff_20260815_151615.db`.
- **`database/actual/config.db` still not migrated** - now needs all three migrators (Phase 1, 2, 3) before that environment is used. Reminder updated accordingly.
- `machine_data.db`, `tags`, `tag_addresses`, `thresholds`, and every other pre-existing table - untouched.
- One real row now exists in the live `energy_tariffs` table: a factory-wide simulated tariff, auto-created by `get_current_tariff()` during this phase's own `AppTest` verification run (harmless, expected side effect of exercising the real lazy-creation code path against live data - not manually seeded).

### Important Files Changed
- **New:** `engine/energy_tariff_migrator.py`, `engine/energy_tariff.py`, `tests/test_energy_tariff.py`.
- **Modified:** `ui/pages/13_Factory_Configuration.py` (new tab + imports only - the page itself is not new this phase).
- No changes to `ui/Home.py` this phase (no new page, so no new nav entry needed). No changes to Ask AI, simulator, KPI/trend calculation, or the historian - confirmed via `git status`.

### New Configuration
None - no new environment variables or settings.ini entries.

### New KPIs
"Today's cost so far" (a genuine, working RM-denominated figure, not just a config form) - the first real energy-to-money conversion in the system. Full Cost Today/Cost Month/Projected Month Cost/Maximum Demand Charge KPIs remain Phase 6's job.

### New Simulation Features
None.

### Known Issues
- Advanced-mode peak/off-peak/demand-charge fields are captured and stored but **not yet used in any calculation** - `calculate_energy_cost()` always uses the flat `energy_rate` regardless of mode. This is a deliberate, scoped simplification (noted in the code and here), not an oversight - splitting cost by actual peak/off-peak consumption needs per-interval kWh data Phase 6 will build.
- Tariff scopes (factory-wide, P01, P02) are fully independent with no fallback/inheritance - a plant with no plant-specific tariff configured does **not** fall back to the factory-wide tariff; it gets its own independently-tracked simulated tariff instead. Simpler and already fully tested, but worth knowing if inheritance is later expected.
- "Today's cost so far" for the factory-wide scope sums P01 + P02's own `Energy_kWh` deltas independently, which is a genuine two-plant total - note this is different from the pre-existing Ask AI factory-wide power/water fallback convention (`FACTORY_TOTAL_POWER_TAG` etc.), which deliberately only ever reports P01 for historical reasons unrelated to this phase. Not a bug, just two different "factory-wide" conventions coexisting in different parts of the app - worth reconciling later if it causes confusion.

### Deferred Work
- Real tariff entry - the live system currently only has the auto-generated simulated tariff; an admin still needs to enter the actual utility tariff via the new UI for real figures.
- Peak/off-peak cost splitting, maximum demand charge calculation, monthly billing-cycle-aware totals - all Phase 6.
- Wiring tariff-based cost into the SCADA Floor Plan's info board or Ask AI - not done this phase (kept scoped to the new tariff tab's own preview, per "do not change Ask AI/SCADA... unless required" carried over from Phase 2's discipline).

### Next Recommended Phase
Phase 4 — Production Context (per the roadmap).

---

## Verification performed (Phase 3)

1. Full regression suite: `python -m unittest discover tests` → 138 tests (14 new, for `engine/energy_tariff.py`), 5 failures - identical pre-existing baseline, zero new failures.
2. `AppTest` on the Factory Configuration page (now 7 tabs) plus Live Data, Setpoints, Service & Maintenance, Equipment & Tag Configuration, SCADA Floor Plan, and Ask AI - all render with no exception; the tariff tab's actual computed values (simulated-tariff banner, MYR 0.5000/kWh rate, 349.6 kWh today, MYR 174.80 cost) verified directly against real live data, not just "no crash."
3. Migration tested on a throwaway database copy first, re-run twice to confirm idempotency, before being applied to the live database with an automatic backup.
4. Versioning correctness verified with dedicated tests: creating a new tariff never mutates the old row's rate fields (only sets its `expiry_date`), and a historical lookup for a date before the change returns the old rate while a lookup on/after the boundary date returns the new one.
5. `git status` confirms Ask AI, simulator, and historian code paths are completely untouched this phase; `ui/Home.py` also untouched (no new page created).
6. No new Python dependencies.

---

## Phase 4 — Production Context

**Completed:** 2026-08-15

### Features Added
- **Hierarchy: Product Category → Product → Batch/Production Run**, three new tables (`product_categories`, `products`, `production_batches`), matching the same FK-based modeling already used for the Factory→Plant→Area→System hierarchy (Phase 1) rather than bare text columns.
- **`products`** - product code, name, category, variant/colour (deliberately product-level, not a separate category per colour), standard batch size, unit of measure (never assumed to be tonnes - the seed catalog deliberately mixes `litre` and `kg`), optional recipe reference (a label/pointer only - no formulation ingredients stored or planned), active flag, `source` provenance. Seeded with the roadmap's own 9 example categories and ~19 illustrative products.
- **`production_batches`** - batch code, product/plant/area/system/equipment/shift FKs (`plant_id`/`area_id`/`system_id` copied directly from the equipment's own Phase 1 columns, `shift_id` resolved against Phase 2's `shift_definitions` - **no duplicate shift system**, none created), status (`planned`/`running`/`completed`/`cancelled`/`interrupted`), full datetime `start_time`/`end_time` (precise enough for later energy-window correlation), planned/actual/good/reject quantities, its own `unit_of_measure` (preserves the batch's actually-recorded unit even if it were ever to differ from the product's default), downtime minutes + reason, `source` provenance.
- **`simulator/production_batch_simulator.py`** - a new, independent simulator module (lives in `simulator/`, same tick-driven/seeded-RNG design as `tag_dataset_model.py`) that generates the actual batch activity. Every "does this equipment already have a running batch" check queries the database directly, never in-memory state - this is what makes it safe across a restart.
- **`app/production_simulator.py` + `production_simulator.service`** (systemd, per your explicit direction - not a Streamlit lazy-start thread) - an always-on background process, independent of the UI. Holds an exclusive `flock()` on `logs/production_simulator.lock` for its entire lifetime, so a second instance started by accident exits immediately rather than running a duplicate generator. **The systemd unit itself still needs installing** - see the commands at the end of this report.
- **`ui/pages/14_Production_Context.py`** - Products (admin-editable master data), Production Batches (a browsable/filterable read-only log - deliberately not an analytics dashboard, per your explicit scope limit), and Simulator (live status, all-time status counts).
- **Realistic, varied simulated production, verified against real live data**: over a 500-tick test run, 213 completed / 234 terminal batches (**91% completed**), 16 interrupted (6.8%), 5 cancelled (2.1%) - completed is overwhelmingly the dominant outcome, matching "not unrealistically common" for the rarer states, verified directly rather than assumed. Real variety confirmed: different plants, products, quantities, units (litre vs kg), and durations.

### Features Extended
None - no existing table gained a new column this phase (Phase 1's equipment `plant_id`/`area_id`/`system_id` were reused as-is, not modified).

### Database Changes
- New tables: `product_categories`, `products`, `production_batches` (migration: `engine/production_migrator.py`, same backup+additive+idempotent pattern as every prior migrator).
- Applied to `database/simulation/config.db` only. Backup: `database/simulation/config_before_production_20260815_160519.db`.
- **`database/actual/config.db` still not migrated** - now needs all four migrators (Phases 1-4) before real use. Reminder updated again.
- No column was added for a future "standard energy intensity" or product density - this project's additive-migration pattern already means either can be added later with zero redesign; noted explicitly rather than silently skipped, per your direction to keep the schema extensible without inventing the field now.

### Important Files Changed
- **New:** `engine/production_migrator.py`, `simulator/production_batch_simulator.py`, `app/production_simulator.py`, `ui/production_data.py`, `ui/pages/14_Production_Context.py`, `tests/test_production_batch_simulator.py`.
- **Modified:** `ui/Home.py` (nav entry only).
- No changes to Ask AI, the existing PLC-tag simulator (`simulator/tag_dataset_model.py`), `app/plc_logger.py`, or the historian - confirmed via `git status`. `simulator/production_batch_simulator.py` is a wholly new sibling file, not a modification of the existing simulator.

### New Configuration
None beyond what's entered through the new page.

### New KPIs
None yet - Production Context provides the raw structured data (batches, quantities, timestamps) that Phase 6's actual KPIs (kWh/batch, kWh/tonne, etc.) will consume later.

### New Simulation Features
- `production_simulator.service` - a fourth always-on background process (alongside `plc_logger`, `event_monitor`, `streamlit`), generating believable production batch history independent of the UI.

### Known Issues
- **The pre-existing `ProductCode`/`BatchNumber`/`GoodCount`/`RejectCount` PLC-style tags are explicitly legacy/cosmetic as of this phase** - confirmed by reading `tag_dataset_model.py` directly: `BatchNumber`/`ProductCode` are STRING tags fixed at simulator startup and never change again, and `GoodCount`/`RejectCount` only increment on a rare per-cycle chance with no batch concept behind them. They are **not** synchronized with `production_batches` in Phase 4, by explicit direction - `production_batches` is the authoritative simulated production context from this phase onward. Reconciling the two into one production reality is Phase 5 Simulation Realism's job.
- **No production-to-energy correlation exists anywhere in this phase** - confirmed both by design (the simulator module never references `plc_data`/`Power_kW`/`Energy_kWh`) and by an explicit automated test (`test_tick_never_writes_to_plc_data_or_touches_any_electrical_tag`) asserting those strings don't even appear in the module's source. That connection is Phase 5's deliberate job.
- 'Interrupted' is modeled as a rare **terminal** outcome (like 'cancelled', just implying an external disruption rather than a deliberate stop) rather than a resumable mid-batch pause - a batch that gets interrupted ends there, with downtime recorded, and the equipment becomes free to start a new batch. This was a deliberate simplification to keep restart-safety trivial (no multi-tick "paused" state to reconcile after a service restart) - flagged here in case a resumable-interruption model is expected later.
- `'planned'` is a fully supported status (schema + explicit unit test coverage) but the live simulator doesn't currently use it in practice - every simulated batch starts directly in `'running'`. Noted honestly rather than silently glossed over.

### Deferred Work
- The `production_simulator.service` systemd unit needs installing (commands below) - the simulator is currently only running as a manually-launched background process in this session.
- `database/actual/config.db` migration for all four phases.
- Reconciling the legacy PLC-style production tags with `production_batches` into one production reality - Phase 5.
- Production-to-energy correlation - Phase 5, deliberately not touched here.
- Product density (kg/L) for normalizing litre-based paint products into mass-based KPIs - schema is ready for it (an additive column later), not implemented now, per explicit direction.

### Next Recommended Phase
Phase 5 — Simulation Realism (per the roadmap - this is where production and energy get deliberately connected, and where the legacy PLC-style production tags get reconciled with `production_batches`).

---

## Verification performed (Phase 4)

1. Full regression suite: `python -m unittest discover tests` → 150 tests (12 new, for `production_batch_simulator`), 5 failures - identical pre-existing baseline, zero new failures.
2. `AppTest` on the new Production Context page plus every previously-verified page - all render with no exception; the new page's actual widget values (3 tabs, correct "Running: 7" metric, correct simulator-running success banner) verified against real live data.
3. Migration tested on a throwaway database copy first, re-run twice to confirm idempotency, before being applied to the live database with an automatic backup.
4. **Single-instance lock verified with a real two-process test**: started one process holding the lock, then attempted a second real `python -m app.production_simulator` process while the first held it - the second correctly detected the held lock and exited immediately (confirmed via process-exit-code check, not just reading the code).
5. **Restart-safety verified with a real process kill + restart**, not just a unit test: let the live simulator generate 7 running batches, killed the process, restarted it, and confirmed directly against the database afterward: zero duplicate running batches per equipment, all 7 original batches correctly continued progressing (one reached a genuine `interrupted` state naturally during this test), exactly one process running throughout.
6. Realistic status distribution confirmed via a real 500-tick run against a throwaway database copy: 91% completed / 6.8% interrupted / 2.1% cancelled, with genuine variety in plant/product/quantity/unit/duration - not just theoretically possible, actually observed.
7. `git status` confirms Ask AI, the existing PLC-tag simulator, `plc_logger`, and the historian are completely untouched.
8. No new Python dependencies (`fcntl` is Python standard library).

### To install `production_simulator.service` (still outstanding)

```bash
sudo tee /etc/systemd/system/production_simulator.service > /dev/null << 'EOF'
[Unit]
Description=SmartMachineAI Production Batch Simulator
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/test/SmartMachineAI
ExecStart=/home/test/SmartMachineAI/venv/bin/python -m app.production_simulator
Restart=always
RestartSec=5
User=test

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now production_simulator.service
sudo systemctl status production_simulator.service --no-pager
```

Once installed, stop the manually-launched instance from this session (it will hold the lock and block the service from starting otherwise):
```bash
pkill -f "app.production_simulator"
```
then let systemd start the real, always-on one via the commands above.

**`production_simulator.service` installed and verified 2026-08-15.** Confirmed via `journalctl`: first start attempt collided with the manually-launched session process still holding the lock (correctly refused to start a second instance, exited 1), systemd's `Restart=always`/`RestartSec=5` retried 5 seconds later, and the retry succeeded - real, unplanned confirmation of the restart-safety design working exactly as built, not just the earlier synthetic two-process test. Phase 4 is now fully accepted.

---

## Phase 5 — Simulation Realism

**Completed:** 2026-08-15

### Features Added
- **`simulator/plant_context.py`** - a new shared-context module the PLC-tag simulator consults once per update cycle. Holds: response-speed tiers, the diurnal ambient environment model, per-instance efficiency/deterioration helpers, the Main Incomer aggregation boundary, and production-state synchronization. `canonical_key()`/`instance_key()` (tag-naming helpers) moved here from `simulator/tag_dataset_model.py`, which now imports and re-exports them unchanged - no external caller (the Simulator Control UI page) needed any change.
- **Diurnal outdoor ambient environment** (item 5) - `P0X.ENV01.Temperature`/`.Humidity` now track a smooth day/night cycle (peak ~3pm, trough ~pre-dawn) instead of a flat random band, via `plant_context.ambient_target_temperature()`/`ambient_target_humidity()`. `HVAC.AHU.FanPower` gets a modest additional push as outdoor conditions get more demanding - temperature-dominant, humidity a smaller secondary nudge (`hvac_demand_push_fraction()`), no full psychrometric calculation, per explicit scope confirmation.
- **Per-instance stable efficiency jitter** (item 2) - every equipment instance gets a fixed ±7% efficiency multiplier (`plant_context.efficiency_factor()`, seeded by instance key, never re-rolled) applied to its own power/current readings. Independent of plant, so a specific P02 unit can land better than its P01 counterpart even while P02 trends worse in aggregate (verified directly in `tests/test_plant_context.py`).
- **One small plant-level structural modifier** (item 2) - P02's unmetered base load runs ~20% higher than P01's (`PLANT_BASE_LOAD_KW`), representing older general infrastructure. This is the only plant-wide difference; everything else is per-instance jitter, not a blanket penalty.
- **Deterioration state model** (items 3 & 9) - a fixed, seeded ~18% of instances (`DETERIORATION_ELIGIBLE_FRACTION`) are selected once at construction as deterioration-eligible; the rest stay `"stable"` forever. Eligible instances start `"deteriorating"` and their genuine wear indicators (Vibration/BearingTemp/MotorCurrent) drift slowly upward via a new `_InstanceFaultState.advance_deterioration()`. A `DETERIORATION_TIME_ACCELERATION_FACTOR` constant (500x, clearly commented SIMULATION-ONLY) scales only this internal wear accumulator's advance rate - never real historian timestamps or any other tag's timing. `force_recover_deterioration()` (mirrors the existing `force_start()`/`force_recover()` fault-system pattern) resets an instance to `"recovered"` - no scenario-control UI wired to it yet (Phase 23's job), just the state and hook, per explicit scope confirmation.
- **Response-speed tiers** (item 8) - `plant_context.response_speed_multiplier()` scales each tag's configured mean-reversion rate by how fast that physical quantity genuinely settles: electrical (power/current/voltage) fastest, compressed-air pressure/flow fast-medium, vibration fairly fast, chilled-water/cold-room/MCC-room temperatures explicitly slower than the generic "temperature" default, applied as a multiplier on top of the existing per-tag profile rather than replacing it.
- **Explicit Main Incomer aggregation boundary** (item 6) - `Power_kW` for each plant's `ELEC.MAIN` is no longer an independent random walk. It's computed in a new third simulation pass (`_update_main_incomer_power()`, after every contributing tag is finalized for the cycle) as: the sum of an explicit, enumerated include list (Air Compressors, Chillers, Chilled Water Pumps, Water Supply Pumps, Cold Room compressors, AHU fan, Dust Collector fan, RO/DI system, Wastewater blower, production equipment motors, Filling Machine, small monitoring-room loads) plus one unmetered base-load term (tank agitators, solvent pumps, lighting/building services/losses - not individually tag-metered). Explicitly **excludes** the Generator (a backup generation source, not a load), the Transformer and UPS `LoadPct` tags (sub-metering/pass-through views of load already counted via the end loads above - including them would double-count), and the Fire Water System (diesel-driven, no electrical draw). `ELEC.INCOMER01` (a separate, smaller pre-existing tag set on the same electrical tree) was deliberately left untouched - out of this phase's explicit scope, flagged under Known Issues below rather than silently ignored.
- **Energy_kWh now integrates from its paired Power_kW-style tag** (related improvement under item 6) instead of a flat random increment, so "today's kWh" (used by the Energy Tariff cost preview and the SCADA info board) now means something real. Reads the previous cycle's already-committed power reading, so tag-processing order never matters.
- **Production synchronization** (items 1 & 4) - `plant_context.get_production_state()` reads `production_batches` directly once per tag-simulator cycle (no snapshot layer, per explicit direction). For the 8 production-linked instances (Bead Mill/Disperser/Mixer x2 plants, Filling Machine x2 plants): `RunStatus` now reflects a genuinely running batch instead of independent random noise; `BatchNumber`/`ProductCode` now show the real batch code/product code (or `"IDLE"`) instead of a value fixed forever at simulator startup; idle instances relax their power/current/speed toward a lower idle band instead of sitting at full-load values. Filling Machine's `GoodCount`/`RejectCount` (the one case where a discrete item count makes engineering sense) are derived from the real batch's `good_quantity`/`reject_quantity` divided by its own current `ContainerSize` reading, holding their last value while idle rather than resetting to 0. Mill/Disperser/Mixer have no such count tags at all - discrete counts don't make engineering sense for them, per explicit confirmation - their production state shows through `RunStatus`/`BatchNumber`/`ProductCode`/idle-vs-running load instead.

### Features Extended
- `simulator/tag_dataset_model.py` - `_InstanceFaultState` gained `efficiency_factor`, `deterioration_eligible`/`deterioration_state`/`deterioration_level`, `advance_deterioration()`, `force_recover_deterioration()`. `_update_real()`/`_update_bool()`/`_update_int()`/`update_values()` all extended to wire in the above (see diff for full detail) - existing fault-cycle behavior (dormant/developing/faulted/recovering), normal process noise, and reversion/inertia are all preserved unchanged for every tag not specifically touched by an item above (confirmed via the unchanged 5-failure regression baseline and a 1500-cycle extended live-style run with zero errors).
- `simulator/production_batch_simulator.py`, `app/production_simulator.py` - **not modified** - Phase 5 only added a new *reader* of `production_batches` (the tag simulator), the production simulator itself is untouched.

### Database Changes
- **None.** Phase 5 is simulator-logic-only - no new tables, no new columns, no migration script. `database/simulation/config.db` / `machine_data.db` schemas are byte-identical to before this phase.

### Important Files Changed
- **New:** `simulator/plant_context.py`, `tests/test_plant_context.py` (24 tests), `tests/test_tag_dataset_model.py` (16 tests - first-ever test coverage for `TagDatasetSimulator`/`_InstanceFaultState`).
- **Modified:** `simulator/tag_dataset_model.py` (see Features Extended above).
- No changes to Ask AI, the UI, PLC drivers, the historian, or any migrator - confirmed via `git status` and the unchanged regression baseline.

### New Configuration
- `simulator/plant_context.py`'s module-level constants are the "configuration" for this phase (deliberately plain Python constants, not settings.ini/UI-editable, matching this simulator's existing style): `DETERIORATION_TIME_ACCELERATION_FACTOR` (500x, simulation-only), `DETERIORATION_ELIGIBLE_FRACTION` (0.18), `EFFICIENCY_JITTER_RANGE` (0.93-1.07), `PLANT_BASE_LOAD_KW` (p01: 8.0, p02: 9.6).

### New KPIs
None directly - Phase 5 makes existing readings (Main Incomer Power_kW, Energy_kWh, production tags) genuinely correlated and realistic, which is what later KPI phases (kWh/batch, kWh/tonne) need to be meaningful, but no new calculated metric was added in this phase itself.

### New Simulation Features
- Diurnal ambient environment, per-instance efficiency/deterioration, explicit Main Incomer aggregation, and production-tag synchronization - all detailed under Features Added above. Together these are what make simulated data now flow through genuine cross-tag/cross-equipment relationships instead of every tag being independent noise, directly per the roadmap's Phase 5 objective.

### Known Issues
- **`ELEC.INCOMER01` (a separate, smaller, pre-existing tag set - `Energy_kWh`/`PF`/`Power_kW` only, no current/voltage/breaker) was left untouched.** It keeps its old independent random-walk behavior, not wired into the new aggregation-boundary model. Flagged honestly rather than silently left inconsistent - worth a deliberate decision later on whether it represents a genuinely separate secondary incomer (keep as-is) or a legacy/redundant duplicate of `ELEC.MAIN` (candidate for cleanup), neither of which was in this phase's explicit scope.
- **A real, pre-existing data-quality bug was found (not introduced by this phase) and partially worked around**: `P0X.WT.RO01.Power_kW`'s `tags.measurement` column is mis-classified as `"pressure"` instead of `"power"` (an `engine/metadata_migrator.py` inference quirk from an earlier phase). This meant it was both pushed in the wrong direction during a fault (`FAULT_DIRECTION_DOWN` treats "pressure" as a down-moving signal) and - more consequentially for this phase - it slipped past a measurement-only floor guard and could read slightly negative during an idle/fault excursion, caught during a 4,000-cycle extended-run sweep. Worked around by flooring known power/current tag-name suffixes directly (`_update_real()`'s final clamp) regardless of the `measurement` classification, so the symptom is fixed for every current and future tag with this shape of naming, not just this one instance - but the underlying `measurement` mis-classification itself was **not** corrected (that's `metadata_migrator.py`/inference territory, a bigger blast radius than this phase's scope; noted here for a future, deliberate fix).
- Mill/Disperser/Mixer's `BearingTemp`/`ProcessTemp` don't cool down/relax toward an idle-appropriate value while their instance is idle (only `Power_kW`/`MotorCurrent`/`Speed` do, via `PRODUCTION_LOAD_SENSITIVE_MEASUREMENTS`) - a real motor's bearing temperature would gradually fall after stopping, not stay on its normal running-band random walk. Judged an acceptable simplification for this phase (not a wrong-direction error, just an unmodeled thermal-decay curve) rather than added complexity beyond what was asked.
- Tank agitators (`TANK.TK.AgitatorCurrent`) and the Solvent Transfer pump (`SOLV.SYS.PumpCurrent`) are not individually included in the Main Incomer boundary (per item 7's "no new tags, document instrumentation gaps honestly" direction) - they're folded into the single unmetered base-load term rather than itemized. Documented here so a future AI answer can honestly describe Main Incomer allocation as partly inferred rather than claim every kW is individually measured.

### Deferred Work
- Reconciling the `ELEC.INCOMER01` tag set with the new Main Incomer model (see Known Issues).
- Correcting `WT.RO01.Power_kW`'s `measurement` classification at the source (see Known Issues) - a `metadata_migrator.py`-level fix, deliberately out of this phase's scope.
- Mill/Disperser/Mixer idle thermal decay for `BearingTemp`/`ProcessTemp` (see Known Issues) - a refinement, not a correctness bug.
- Phase 23's scenario-control UI (manually triggering/recovering deterioration from the UI, the way the existing Simulator Control page already does for faults) - the underlying state and `force_recover_deterioration()` hook are ready for it; no UI was built this phase, per explicit scope confirmation.
- `database/actual/config.db` - still needs all four prior migrators (Phases 1-4) before real use; Phase 5 added no new migration, so this reminder is unchanged from Phase 4.

### Next Recommended Phase
Phase 6 (per the roadmap - likely where the newly-real production/energy correlation from this phase starts feeding actual KPI calculations, e.g. kWh/batch, kWh/tonne).

---

## Verification performed (Phase 5)

1. **Full regression suite**: `python -m unittest discover tests` → 190 tests (150 existing + 24 new in `test_plant_context.py` + 16 new in `test_tag_dataset_model.py`), 5 failures - identical pre-existing baseline (`test_equipment_knowledge`/`test_hybrid_router`/`test_pipeline`, all confirmed dead code), zero new failures. Re-confirmed via `grep -E "^(FAIL|ERROR)"`, not just the count, per this doc's own standing discipline on that.
2. **New unit tests** (40 total): tag-naming helpers, response-speed ordering, diurnal ambient bounds/direction, efficiency-jitter determinism/range/plant-independence, deterioration eligibility fraction/determinism, the full Main Incomer include/exclude boundary (every explicitly-excluded category individually asserted absent), production-state DB read graceful degradation when tables don't exist, `_InstanceFaultState.advance_deterioration()`/`force_recover_deterioration()` behavior in isolation, and `TagDatasetSimulator`-level integration tests (Main Incomer aggregation against a real minimal tag set including deliberately-seeded exclusions, Energy_kWh integration, RunStatus/BatchNumber/ProductCode/GoodCount/RejectCount against real `production_batches` rows created via the real `start_batch()`).
3. **AppTest sweep** of every page - all render with no exception except `6_Simulator_(testing_only).py`, which times out under `AppTest` due to a **pre-existing** `time.sleep(5)`/`st.rerun()` blocking auto-refresh loop (the same pattern already fixed on Live Data/SCADA Floor Plan in earlier sessions, but never applied to this temporary dev-only page) - confirmed via `git log`/`git status` that this file was untouched by Phase 5, so this is a known pre-existing `AppTest` limitation on that one page, not a regression.
4. **Restart-safety / determinism check**: two independently-constructed `TagDatasetSimulator` instances against the same database derive the identical efficiency factor and deterioration eligibility for the same instance (seeded from the instance key, not per-process randomness) - confirms a `plc_logger` restart can never silently re-roll an instance's efficiency/deterioration identity.
5. **Extended live-style run**: 1,500+ combined cycles (alternating `TagDatasetSimulator.update_values()` with real `production_batch_simulator.tick()` calls at a realistic relative cadence) against a throwaway copy of the real live `database/simulation/config.db` - zero exceptions; Main Incomer Power_kW stayed in a plausible ~395-490 kW range for both plants; all 15 real deterioration-eligible instances in that dataset showed genuine upward drift (one sample instance: 0.0 → 0.29 over ~17.4 simulated days, matching the configured 500x acceleration factor's math exactly); a real negative-power-reading edge case was caught this way (see Known Issues) and fixed, then re-verified clean across 5 more seeded trials.
6. **Direct sanity check against a snapshot of the real live database** (before the extended run): confirmed a real in-progress production batch's `BatchNumber` (`P01-MILL01-...`) appeared correctly on the corresponding tag, confirmed `ENV01.Temperature` drifting slowly toward its diurnal target rather than snapping instantly (response-speed tiers working as intended), confirmed Main Incomer Power_kW magnitude was consistent with a manual sum of its real contributing tags' live values.
7. `git status` confirms no file outside `simulator/plant_context.py` (new), `simulator/tag_dataset_model.py` (modified), and the two new test files was touched.
8. No new Python dependencies.

**`plc_logger.service` needs a restart to pick up this phase's code** - it only imports its Python modules once at process start, same as every prior phase's code change:
```bash
sudo systemctl restart plc_logger.service
```

**Phase 5 fully accepted 2026-08-15** - `plc_logger.service` restarted and live-verified (Main Incomer readings, production-batch sync on `RunStatus`/`BatchNumber` all confirmed against real data), `production_simulator.service` confirmed installed and continuously running.

---

## Phase 6 — Energy KPI Engine

**Completed:** 2026-08-15

### Features Added
- **`engine/energy_kpi_engine.py`** - the deterministic calculation engine (Rule 4 - LLM never computes a KPI, only explains one). Every function returns a standard contract: `value`, `unit`, `period_start`/`period_end`, `source_tags`, `classification` (`DIRECT`/`ESTIMATED`/`UNAVAILABLE`), `calculation_type` (`MEASURED`/`CALCULATED`/`ESTIMATED`/`None`), `assumptions`, `missing_inputs` - the same shape Phase 16's Data Health system and Ask AI will need later, built in now per explicit direction, with no Phase 16 UI built.
- **Electrical hierarchy resolved**: audited directly (not guessed) that `ELEC.MAIN` is the only tag any existing code (`ui/scada_floor_plan_data.py`, the Energy Tariff cost preview) already relies on - `ELEC.INCOMER01` is referenced nowhere in application code. `ELEC.MAIN` is Phase 6's sole authoritative plant incomer; `ELEC.INCOMER01` is never read.
- **Factory/plant energy KPIs**: Current Demand, Energy Today/Yesterday/Month, Cost Today/Month (day-by-day tariff-aware - correctly splits a mid-period tariff change instead of applying today's rate retroactively), Projected Month Cost (always `ESTIMATED`+`ESTIMATED`), Estimated Maximum Demand (configurable rolling-window average, default 15 minutes, **always labeled "Estimated"**, never presented as official utility billing demand), Estimated Demand Charge (`UNAVAILABLE` when the tariff has no `maximum_demand_charge` configured - never fabricated).
- **Tariff fallback**: plant-specific tariff first, falling back to the factory-wide (`plant_id IS NULL`) tariff if none is configured for that plant - mirrors the Factory Configuration page's own Scope choice. Found necessary live: the only tariff actually configured today is factory-wide, and per-plant KPIs would have gone `UNAVAILABLE` without this fallback.
- **Production/non-production split** (four distinct, never-conflated concepts, per explicit direction): Production Energy and Non-production Energy (`DIRECT`, split by real `production_batches` state, Phase 4 - not a nominal shift schedule), Average Production-Period Demand (renamed from my own draft's "Production Energy Intensity" per your correction - it's an average demand, not a normalized intensity), Average Non-production Demand, and Estimated Base Load (`ESTIMATED` - a low-percentile floor during non-production intervals, distinct from the plain average). Overlapping production batches are merged into disjoint intervals first, so simultaneous batches on different equipment never double-count plant energy (explicitly tested).
- **After-hours Consumption**: correctly `UNAVAILABLE` - `shift_definitions` is empty for both plants (Phase 2's schedule concept was built but never populated). Becomes available with zero code changes once shifts are configured.
- **Production Energy Intensity** (the genuine normalized metrics): kWh/batch, kWh/kg, kWh/litre, kWh/tonne - kg and litre production are never combined. kWh/tonne and RM/tonne are `DIRECT` for kg-unit products (4 of 19), `UNAVAILABLE` for litre-unit products (15 of 19) - no density assumed, per explicit direction; recorded as a future data-model requirement, not implemented.
- **Compressor KPIs**: Loaded%/Unloaded% and specific energy (kWh/running-hour) per compressor (`DIRECT`, from `LoadedHours`/`UnloadedHours`/`RunningHours`/`Energy_kWh`). Header-level specific energy (Σcompressor kWh ÷ header flow, `DIRECT`) - prefers the header's cumulative `FlowTotal` meter, falls back to numerically integrating the instantaneous `Flow` tag where `FlowTotal` doesn't exist (found live: P02's header has no `FlowTotal` tag at all, a real pre-existing asymmetry with P01 - both plants now work). Per-compressor kWh/Nm3 is never presented as a true measurement (no per-compressor flow meter exists) - only as an explicitly `ESTIMATED` inferred allocation. Night/base air consumption (`ESTIMATED`, same low-percentile method as Base Load).
- **Chiller KPIs**: Cooling output and COP, `DIRECT`, from each chiller's own `WaterFlow`/`SupplyTemp`/`ReturnTemp`/`Power_kW`. **Simulation-environment instrumentation definition, explicitly not a real-plant assumption** (per your correction): `CHL01.WaterFlow`/`CHL02.WaterFlow` represent each chiller's own dedicated flow, not a shared header - documented in-code as something a real PLC deployment must verify before enabling.
- **Pump KPIs** (`CHWP`/`WSP` only - the only pumps with both suction and discharge pressure measured): hydraulic power and hydraulic efficiency, `DIRECT`, with the exact derivation documented (`ΔP(bar) × Q(m³/h) × 0.027778`). flow/kW always available as a simpler operational indicator.
- **Water treatment**: `WT.RO01` kWh/m³ `DIRECT` (numerically integrated - no cumulative meter exists for it). `WT.SYS01` kWh/m³ correctly `UNAVAILABLE` - it has no electrical measurement tag at all, confirmed by audit, never manufactured.
- **`app/energy_kpi_worker.py`** - a separate, standalone `energy_kpi_worker.service` (per explicit direction - kept apart from `event_monitor.service`'s alarm/event responsibility), following the exact `production_simulator.service` pattern (`flock()` single-instance lock, restart-safe by construction - every tick recomputes its current state fresh from the database, never trusting in-memory state). Handles the only two persisted KPIs: incremental Maximum Demand record-keeping (per plant per calendar-month billing period) and the daily summary rollup (written only for a fully-finalized past day, idempotent - safe to call every tick without duplicating rows).

### Features Extended
- **`ui/scada_floor_plan_data.py`'s `_todays_accumulated_total()`** - its body now delegates to `energy_kpi_engine.accumulated_positive_delta()` (the same reset-safe logic, generalized to an arbitrary `[start, end]` window). Zero call-site changes anywhere - both consumers (`scada_floor_plan_data.py` itself, `13_Factory_Configuration.py`'s tariff preview) import the function exactly as before. Verified byte-identical output against live data before/after.
- `simulator/tag_dataset_model.py`, `engine/energy_tariff.py` - **not modified**. `energy_tariff.py`'s period-aware, tariff-fallback-aware cost splitting turned out to fit more naturally as `energy_kpi_engine.py`'s own `cost_for_period()`/`_effective_tariff()` (which already need both the historian and the tariff table) - so Phase 3's file needed zero changes, better than originally planned (KEEP EXISTING instead of EXTEND EXISTING).

### Database Changes
- New tables (via `engine/energy_kpi_migrator.py`, same backup+additive+idempotent pattern as every prior migrator): `energy_kpi_maximum_demand` (one row per plant per billing period per demand-interval setting), `energy_kpi_daily_summary` (one row per plant per finalized day). Deliberately the only two persisted KPIs - everything else recomputes on demand (kept minimal per explicit direction).
- Applied to `database/simulation/config.db`. Backup: `database/simulation/config_before_energy_kpi_20260815_181300.db`.
- `database/actual/config.db` still needs all five migrators (Phases 1-3, 4's production migrator, this phase's) before real use - reminder carried forward again.
- `machine_data.db` - untouched, zero rows modified, no schema change.

### Important Files Changed
- **New:** `engine/energy_kpi_engine.py`, `engine/energy_kpi_migrator.py`, `app/energy_kpi_worker.py`, `tests/test_energy_kpi_engine.py` (41 tests).
- **Modified:** `ui/scada_floor_plan_data.py` (`_todays_accumulated_total()` internals only - see Features Extended).
- No changes to Ask AI, PLC drivers, the simulator, or any other migrator - confirmed via `git status` and the unchanged regression baseline.

### New Configuration
- `engine/energy_kpi_engine.py`'s module constants (plain Python, matching this project's established style for this kind of setting): `DEFAULT_DEMAND_INTERVAL_MINUTES` (15, configurable per-call), `WATER_DENSITY_KG_PER_M3` (1000), `WATER_SPECIFIC_HEAT_KJ_PER_KGK` (4.186), `PUMP_EFFICIENCY_IMPLAUSIBLE_THRESHOLD_PCT` (105), `STALE_READING_SECONDS` (300), `BASE_LOAD_PERCENTILE` (10).

### New KPIs
All of §"Features Added" above - the full list the roadmap's Phase 6 spec requested, each with an explicit `DIRECT`/`ESTIMATED`/`UNAVAILABLE` classification grounded in what's actually measured, not assumed.

### New Simulation Features
None - Phase 6 reads existing simulated/historized data, it doesn't generate any.

### Known Issues
- **`P02.UTILITY.AIRHDR01.FlowTotal` doesn't exist at all** (only P01's header has it) - a genuine pre-existing tag-dataset asymmetry between plants, found live during Phase 6 verification. Worked around with a numerical-integration fallback (see Features Added) rather than left silently broken for P02.
- **`WT.RO01.Power_kW`'s pre-existing `measurement="pressure"` misclassification** (flagged in Phase 5's report) is inherited unchanged - Phase 6's water-treatment KPI keys off the tag name, not the `measurement` field, so it's unaffected, but the underlying metadata bug itself remains uncorrected, per explicit direction to keep this phase's scope to the KPI engine, not metadata inference.
- **After-hours Consumption is `UNAVAILABLE`** for both plants - `shift_definitions` has zero rows configured. Not a code gap; becomes `DIRECT` automatically once shifts are entered on the Factory Configuration page.
- **Pump hydraulic efficiency reads unrealistically low** (2-4%) against live simulated data - the formula is correct engineering, but the simulator's `SuctionPressure`/`DischargePressure`/`Flow`/`Power_kW` aren't causally coupled to each other yet (Phase 5 didn't couple these specific quantities), so ΔP often lands small relative to flow/power by chance. A known simulation-fidelity limitation, not a Phase 6 calculation defect - flagged rather than silently left looking like a bug.
- **A real bug was found and fixed during live verification** (not left in): `latest_with_timestamp()` originally used `get_history(hours=24, limit=500)` and took the last row, which silently returned the *oldest* reading in a 24-hour lookback whenever more than 500 samples existed in that window (true for every 10s-cadence tag) - `Current Demand` was reporting a 6-hour-stale value as fresh. Fixed by using `DatabaseManager.get_latest()`, the correct existing method, instead of reinventing it. Caught specifically because live data was checked, not just synthetic unit tests.
- Two additional construction bugs (wrong `unit_prefix` breaking `WT.RO01`'s tag name, an unused `start`/`end` signature mismatch on `chiller_cop()`) were found the same way and fixed before this report - noted here for the discipline, not because either is still present.

### Deferred Work
- `database/actual/config.db` migration for all five schema-bearing phases.
- Reconciling `WT.RO01.Power_kW`'s `measurement` misclassification at the source.
- Shift-window-aware After-hours Consumption logic (straightforward once real shift data exists to test against).
- Wiring `DEFAULT_DEMAND_INTERVAL_MINUTES` to a UI setting rather than a code constant - not needed since Phase 6 has no dashboard yet (Phase 7's job).
- `energy_kpi_worker.service` needs installing (commands below) - not yet running continuously.

### Next Recommended Phase
Phase 7 - Energy Dashboard / Plant Comparison (per the roadmap) - the KPI engine built this phase is what it will render; explicitly not built yet, per your direction.

---

## Verification performed (Phase 6)

1. **Full regression suite**: `python -m unittest discover tests` → 231 tests (230 + 1 added during a live-verification fix), 5 failures - identical pre-existing baseline, zero new failures.
2. **41 new unit tests** covering every KPI category plus every edge case explicitly requested: zero/near-zero flow -> `UNAVAILABLE` not divide-by-zero (compressor header, chiller, pump), negative ΔT -> `UNAVAILABLE` not a misleading COP, negative ΔP -> `UNAVAILABLE`, implausible >105% pump efficiency -> flagged `ESTIMATED` with the raw figure still visible (not silently hidden), missing/stale required inputs -> `UNAVAILABLE` (verified fresh vs. 2-hour-stale vs. entirely missing), overlapping production batches merged into one disjoint interval so plant energy is never double-counted (explicitly tested against a synthetic overlap), a mid-period tariff change correctly split at the day boundary, restart-safety (two independent "ticks" against identical data never duplicate or regress a persisted Maximum Demand record).
3. **AppTest sweep** of every page - identical result to Phase 5 (all pass except the pre-existing, already-documented `6_Simulator_(testing_only).py` timeout, confirmed untouched by this phase via `git status`).
4. **Live verification against the real running system** (not just synthetic tests) - this is what actually caught the three real bugs listed under Known Issues: ran the new debug CLI (`python -m engine.energy_kpi_engine --plant p01`/`p02`) against live data repeatedly through the fix cycle, cross-checked `_todays_accumulated_total()`'s delegated output against the new engine's `energy_for_period()` for byte-identical equality, manually verified `kwh_per_kg × 1000 == kwh_per_tonne` on real production data, manually verified the P02 header-flow fallback against real data after the asymmetry was found.
5. Migration tested on a throwaway database copy (via SQLite's own `.backup()` API, not a raw `cp`, after a raw copy of an actively-written database produced a "database disk image is malformed" error mid-testing - a copy-methodology lesson worth carrying forward) re-run twice for idempotency, before being applied to the live database with an automatic backup.
6. `git status` confirms no file outside the ones listed under "Important Files Changed" was touched.
7. No new Python dependencies.

### To install `energy_kpi_worker.service` (outstanding)

```bash
sudo tee /etc/systemd/system/energy_kpi_worker.service > /dev/null << 'EOF'
[Unit]
Description=SmartMachineAI Energy KPI Worker (Maximum Demand + Daily Summary)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/test/SmartMachineAI
ExecStart=/home/test/SmartMachineAI/venv/bin/python -m app.energy_kpi_worker
Restart=always
RestartSec=5
User=test

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now energy_kpi_worker.service
sudo systemctl status energy_kpi_worker.service --no-pager
```

---

## Phase 7 — Energy Dashboard & Plant Comparison

**Completed:** 2026-08-15

### Features Added
- **`ui/pages/15_Energy_Dashboard.py`** - a new Energy Dashboard page (sidebar: directly after Live Data, per explicit placement direction - expected to be a frequently-used page). Plant selector + time-range selector (Today/Yesterday/7 Days/30 Days/Custom), all sections below driven from the one selected `(plant, start, end)` - no section computes its own period boundaries.
- **`ui/energy_dashboard_data.py`** - the caching/orchestration layer, architecturally identical in role to `ui/scada_floor_plan_data.py`/`ui/production_data.py`: it calls `engine.energy_kpi_engine` and adds `st.cache_data` caching, and nothing else. Verified by two dedicated tests: a source-inspection test asserting none of the engine's own formula fingerprints (`0.027778`, `4.186`, `WATER_DENSITY_KG_PER_M3`, `PUMP_EFFICIENCY_IMPLAUSIBLE_THRESHOLD_PCT`) appear in the UI layer, and a "swap the engine function, watch the displayed value change" test proving the UI holds no independent copy of the logic.
- **Tiered caching** (per your explicit TTL guidance): Current Demand and other instantaneous readings ~12s; anything ending "now" (Today, month-to-date) 30s; fully-elapsed historical periods (Yesterday, a completed Custom range) 1 hour - live-verified: Current Demand visibly changed across its 12s TTL boundary (438.54 -> 438.58 kW after 13s), while a repeated call for a historical period issued zero additional historian queries (query-counting test + live confirmation).
- **Current Demand isolated in its own `@st.fragment(run_every=...)`** (mirrors `2_Live_Data.py`'s existing flicker-free-refresh pattern) so keeping that one number fresh never re-renders the rest of the page.
- **7/30-Day trend charts read `energy_kpi_daily_summary` directly** (Phase 6's persisted rollup) instead of recomputing per day from raw historian data - exactly what that table was built for.
- **"Estimated Maximum Demand - Selected Period" and "Billing-Month Peak So Far" kept as two separately-labeled cards**, never merged - the former reflects whatever range/plant is currently selected, the latter reads the persisted `energy_kpi_maximum_demand` record for the current calendar-month billing period. Shows "Unavailable" (reason: `energy_kpi_worker.service` hasn't recorded a peak yet) until that service is installed and has run.
- **KPI transparency**: every card is `st.metric(..., help=...)` carrying `classification`/`calculation_type`/source tags/assumptions in the hover tooltip - zero extra vertical space by default. A collapsed "Data & assumptions" expander per section for the full dump. `UNAVAILABLE` KPIs render the literal text "Unavailable", never `0` - verified explicitly (`test_current_demand_unavailable_never_renders_as_zero`).
- **Utility Performance** as four sub-tabs (Compressed Air / Chilled Water / Pumps / Water Treatment, per your explicit navigability preference over expanders). Pump hydraulic efficiency is shown exactly as calculated, including the unrealistically-low simulated values - per your explicit instruction, not adjusted, with the simulation-fidelity limitation surfaced only through the tooltip.
- **Plant Comparison** - a single table, P01 | P02, with an explicit "no ranking/verdict language" design rule enforced by a dedicated test scanning the page source for banned phrases ("more efficient", "better", "worse", "winner", ...). Production-normalized rows are labeled with their basis ("kWh/kg - based on kg-unit production in selected period") and show `—`/Unavailable rather than forcing a comparison when one plant didn't produce that unit type in the period.
- **Equipment power breakdown** - a ranked table of current Power_kW share per Main-Incomer-contributing tag (reuses Phase 5's existing aggregation boundary, computes no new aggregation). Explicitly labeled **"Power Share"**, never "Energy Share" - it's an instantaneous snapshot, not a time-integrated contribution, per your explicit correction.
- **Provenance caption** - one unobtrusive `st.caption` line at the top ("🔧 Simulation environment · Tariff: Simulated (...)")," never repeated per-section.

### Features Extended
- **`engine/energy_kpi_engine.py`** - Phase 7 implementation surfaced several small gaps against the already-approved Phase 6 KPI matrix (a display page needs the raw components a formula function only used internally, not just the final derived number). Filled as engine additions, not UI arithmetic: `total_compressor_energy()`, `header_air_volume()` (extracted from `header_specific_energy()` into a shared `_header_air_volume()` helper, DRying up the FlowTotal/Flow fallback logic), `compressed_air_cost_per_nm3()`, `pump_specific_energy()` (period-integrated kWh/m3, distinct from the existing instantaneous hydraulic-efficiency calc). `chiller_cop()` and `pump_hydraulic_efficiency()` extended to also return their raw measured components (`power_kw`, `water_flow_m3h`, `supply_temp_c`, `return_temp_c`, `delta_t_c` / `flow_m3h`, `delta_p_bar`) as their own displayable `_kpi()` entries alongside the existing derived results - all covered by new tests (9 added).
- **`cost_for_period()` - a real bug found and fixed during Phase 7 live verification**: when only part of a requested period had a tariff configured (the live tariff was only entered today, so days before that had none), the function silently returned a much-too-low total with no indication it was incomplete - "Cost This Month" showed the same figure as "Cost Today" with nothing to explain why. Fixed to track energy that had no tariff in effect and add an explicit `"PARTIAL TOTAL: N kWh ... excluded ... true cost is understated"` assumption whenever this happens, rather than silently presenting a partial figure as complete. Classification stays `DIRECT` (the figure itself is correct for what it represents), but the caveat is now impossible to miss in the tooltip. Covered by a new regression test.
- `engine/energy_tariff.py`, `simulator/tag_dataset_model.py` - **not modified**.
- Two engine helpers made public (dropped their leading underscore) since Phase 7 now calls them across the module boundary: `effective_tariff()` (was `_effective_tariff`, the plant-then-factory-wide tariff fallback) - `_parse`/`_fmt` were NOT exposed; the UI layer uses `datetime.strptime(..., k.TIME_FORMAT)` directly instead, since those were trivial enough not to warrant a public API surface.
- `ui/Home.py` - nav entry added directly after Live Data (per explicit placement direction), plus one descriptive bullet in the overview text.

### Database Changes
None - Phase 7 reads existing Phase 6 tables, writes nothing.

### Important Files Changed
- **New:** `ui/energy_dashboard_data.py`, `ui/pages/15_Energy_Dashboard.py`, `tests/test_energy_dashboard_data.py` (20 tests).
- **Modified:** `engine/energy_kpi_engine.py` (gap-fill additions + the cost_for_period bug fix + the public rename, all detailed above), `ui/Home.py` (nav entry only).
- No changes to Ask AI, the simulator, any migrator, or any other existing UI page - confirmed via `git status` and the unchanged regression baseline.

### New Configuration
`ui/energy_dashboard_data.py`'s `CURRENT_TTL`/`TODAY_TTL`/`HISTORICAL_TTL` constants (12s/30s/3600s) - plain Python constants matching this project's established style, not yet wired to a UI setting (no dashboard-settings page exists to put one in; noted as a possible future refinement, not needed now).

### New KPIs
None beyond the small Phase 6 engine gap-fills listed under Features Extended - Phase 7 itself is presentation-only, per its own stated scope.

### Known Issues
- **`energy_kpi_worker.service` is still not installed** - "Billing-Month Peak So Far" and the 7/30-day trend charts correctly show "Unavailable"/empty rather than fabricating data, but won't have real content until it's running (install commands in the Phase 6 section above, unchanged).
- **After-hours Energy remains `UNAVAILABLE`** (Phase 6's own known issue, inherited unchanged) - `shift_definitions` still has zero rows configured for either plant.
- **Pump hydraulic efficiency displays unrealistically low** on live data (2-5%) - not touched or corrected in Phase 7, per your explicit instruction; the tooltip/assumptions text explains why (Phase 5 doesn't yet couple pump pressure/flow/power to each other).
- The intraday demand trend chart currently renders every raw sample for "Today"/"Yesterday" (up to ~8,640 points at 10s cadence for a full day) rather than downsampling - performant in practice (`st.line_chart` handles this volume fine) but flagged as a possible future refinement if a full day's chart ever feels sluggish.

### Deferred Work
- Installing `energy_kpi_worker.service`.
- Downsampling the intraday trend chart (only if it ever proves necessary).
- Wiring the cache TTL constants to a settings UI.
- Everything explicitly out of Phase 7's scope per your direction: baseline engine, anomaly detection, opportunity/savings detection, equipment-health scoring, predictive maintenance, Ask AI energy reasoning, AI-generated recommendations, Phase 21's full Energy Flow visualization.

### Next Recommended Phase
Whichever phase your roadmap defines next after Phase 7 - Phase 7's own scope is now complete pending your review.

---

## Verification performed (Phase 7)

1. **Full regression suite**: `python -m unittest discover tests` -> 261 tests (231 existing + 10 new engine tests for the gap-fill functions/bug fix + 20 new dashboard-data tests), 5 failures - identical pre-existing baseline, zero new failures.
2. **20 new `tests/test_energy_dashboard_data.py` tests**: value-parity (data layer matches direct engine calls, including a "swap the engine function and watch the result change" proof), source-inspection (no engine formula constants duplicated in the UI, every major KPI category traced to an explicit engine call), no-ranking-language scan of the page source, live-vs-historical period dispatch, unavailable-KPI rendering, simulated-tariff flagging, mixed kg/litre production handling, and query-volume/caching behavior (repeated calls within a TTL window issue zero additional historian queries, different plants/periods each correctly trigger their own query).
3. **10 new `tests/test_energy_kpi_engine.py` tests** for the Phase 6 engine gap-fills and the `cost_for_period()` partial-total fix.
4. **`AppTest` sweep** of every page - identical result to Phases 5/6 (all pass except the pre-existing, already-documented `6_Simulator_(testing_only).py` timeout, confirmed untouched).
5. **Live verification against the real running system**: cross-checked the new dashboard's data layer against the Phase 6 debug CLI for both plants side-by-side (Energy Today, Cost Today, Estimated Maximum Demand, Header Specific Energy all matched to within normal live-drift); this is what caught both the `cost_for_period()` partial-total bug and the `canonical_key`/`instance_key` module-location bug (fixed while building the equipment breakdown) before they shipped.
6. **Cache-freshness verified live, not just unit-tested**: Current Demand's value visibly changed across its ~12s TTL boundary (438.54 -> 438.58 kW after a 13s sleep); a repeated call for a historical period was confirmed (via a query-counting wrapper) to issue zero additional historian queries on the second call.
7. `git status` confirms no file outside the ones listed under "Important Files Changed" was touched.
8. No new Python dependencies (`pandas` was already a transitive Streamlit dependency, used here only for `st.dataframe`/`st.line_chart`/`st.bar_chart` inputs exactly as other pages already do).

---

## Phase 8 — Baseline Engine

**Completed:** 2026-08-15

### Features Added
- **`engine/baseline_targets.py`** - a curated target registry (not an automatic sweep of all 625 tags, per explicit direction). `EquipmentTypeProfile`s define context dimensions/downsampling/window defaults once per equipment type; `discover_targets()` expands each profile against whichever instances actually exist (queried from `tags`, never hardcoded per-instance) - 84 concrete targets discovered live for P01 across 10 equipment types (plant energy, air compressor, air header, chiller, chilled water pump, water supply pump, AHU, cold room, production process, filling). Pump hydraulic efficiency and true per-compressor kWh/Nm³ are deliberately absent from every profile, per your explicit exclusion.
- **`engine/baseline_engine.py`** - the calculation engine (Rule 4 - no LLM). Full pipeline per target: bounded historical fetch (never before `MODERN_DATA_BOUNDARY` = 2026-08-09) → threshold-based fault-interval reconstruction from raw values + configured limits (not simulator internals, not `machine_events` alone - see Known Issues) → 5/15-minute representative-median downsampling → context matching with progressive dimension-dropping (a single mechanism implementing the full A/B/C fallback hierarchy, plus a dedicated same-product → category → operating-state matcher for production equipment) → single-pass status/confidence classification (`unavailable` / `bootstrap` / `mature`+`Low`/`Medium`/`High`) → robust median/P10-P90/MAD statistics → dual reference/recent baseline with drift%.
- **Bootstrap/mature resolution (item 5)**: a target with enough representative samples to compute a defensible distribution but poor day diversity now returns a real, honestly-labeled `bootstrap`/`Low` value instead of a contradictory "20 samples qualifies but confidence says Insufficient" - confirmed via a dedicated test reproducing your exact "10,000 samples, one afternoon" scenario (capped at `bootstrap`/`Low` regardless of the sample count).
- **Dual reference/recent baseline** - adaptive, non-overlapping once enough history exists (verified: at 90 simulated days available, reference/recent windows are exactly adjacent, reference capped at 60 days); collapses to one shared window while history is young (today's real system: both windows are identical, `windows_overlap: true`, documented in the result rather than hidden). A synthetic slow-drift test confirms the reference baseline does NOT chase the recent baseline (recent read >5kW higher than reference under a deliberate 0.3kW/day drift over 90 days).
- **`engine/baseline_migrator.py`** - `baseline_context_summary` (per-context-bucket rows only, never per-sample). Applied to live `database/simulation/config.db` with backup.
- **`app/baseline_worker.py`** - a separate `baseline_worker.service`, one target per tick, round-robin across every plant's registry, naturally accumulating context-bucket coverage over many cycles rather than one exhaustive sweep. **Tick interval set from a real measurement, not a guess**: a full pass over P01's 84 targets took **213.3s (2.54s/target average, 0 errors)** against the live ~7.35M-row historian - confirming the "never on the request path" requirement was correct, and grounding `TICK_INTERVAL_SECONDS = 5.0` (leaves headroom above the measured average; a full two-plant, 168-target rotation completes in ~14 minutes).
- **Context bucketing** - practical bands, not per-minute granularity: load/ambient as tercile-of-observed-range (3 buckets), 4-hour blocks (6/day), weekday/weekend. Air compressors use a `loaded_state` boolean (LoadStatus) instead of a continuous load tercile - more physically accurate for fixed-speed equipment.
- **Self-reference guard (item 10, generalized)**: any context dimension whose source tag equals the target's own tag is automatically dropped before matching - checked for every raw target, not just derived KPIs (e.g. `LoadPct`'s own baseline never uses `load_bucket`, which is itself derived from `LoadPct`).
- **Fault exclusion**: primary method reconstructs abnormal intervals directly from raw historian values + each tag's own configured `thresholds` (general, and works against a real plant with no simulator at all) - `machine_events` was confirmed (by reading `app/event_monitor.py` directly) to only record fault *starts*, never ends, so it could not have been the primary source. Maintenance windows (`service_log`/`maintenance_log`) excluded via a best-effort equipment-name match, ±30 min. `simulator_fault_commands` explicitly NOT used, per your direction (UTC/ISO timestamps vs. the historian's local/space-separated convention - see Known Issues).
- **Debug CLI** (`python -m engine.baseline_engine --plant p01 [--equipment-type ...]`) - the only interface built this phase, per explicit scope limit. No dashboard, no Energy Dashboard changes.

### Features Extended
None - `engine/energy_kpi_engine.py` untouched; its `TIME_FORMAT`/`WATER_DENSITY_KG_PER_M3`/`WATER_SPECIFIC_HEAT_KJ_PER_KGK` constants are imported and reused by the derived-KPI calculators (COP, cooling output) rather than re-derived.

### Database Changes
- New table: `baseline_context_summary` (via `engine/baseline_migrator.py`, same backup+additive+idempotent pattern as every prior migrator). Applied to `database/simulation/config.db`. Backup: `database/simulation/config_before_baseline_20260815_200657.db`.
- `database/actual/config.db` still needs all six schema-bearing migrators before real use - reminder carried forward again.

### Important Files Changed
- **New:** `engine/baseline_targets.py`, `engine/baseline_engine.py`, `engine/baseline_migrator.py`, `app/baseline_worker.py`, `tests/test_baseline_engine.py` (27 tests).
- **Modified:** none outside the new files above.
- No changes to any UI page, Ask AI, the simulator, Phase 6/7's energy engine, or any other migrator - confirmed via `git status` and the unchanged regression baseline. `AppTest` sweep unaffected (Phase 8 built no UI).

### New Configuration
`engine/baseline_engine.py`'s module constants (plain Python, matching this project's established style): `DEFAULT_NORMAL_RANGE_PERCENTILES = (10, 90)` (the one central definition - a dedicated test confirms no second copy of any percentile pair is hardcoded elsewhere), `MODERN_DATA_BOUNDARY = 2026-08-09`, the bootstrap/medium/high coverage thresholds, `FAULT_EXCLUSION_BUFFER_MINUTES = 2`, `HOUR_BLOCK_HOURS = 4`.

### New KPIs
None - Phase 8 computes *expected* values for existing measurements/KPIs, it doesn't add new ones.

### Known Issues
- **Timezone/timestamp-format mismatch, found during Phase 8's audit (not previously documented anywhere)**: `plc_data`/`machine_events`/`production_batches` use local `Asia/Kuala_Lumpur` (+08:00) time via `datetime.now().strftime(...)`; `simulator_fault_commands` and several audit-log-style tables (`config/user_manager.py`, `config/configuration_manager.py`, `ui/data_access.py`, `config/plc_connection_manager.py`) use `datetime.utcnow().isoformat()` (UTC, ISO-`T`). Never compare these directly without an explicit, tested conversion - Phase 8 deliberately avoids the mismatched tables entirely rather than risk an untested conversion, per your explicit direction. Worth adding to this document's own landmine list.
- **A tiny legacy timestamp sliver** (967 of ~7.35M `plc_data` rows, 2026-07-25/26, ISO-`T` format) predates the current tag dataset by ~2 weeks - inert given `MODERN_DATA_BOUNDARY` bounds every lookback window well clear of it.
- **`MODERN_DATA_BOUNDARY` is a hardcoded date**, deliberately - a real deployment should replace it with its own actual historian-start boundary (e.g. a commissioning-date config value) rather than depend on this constant forever; kept as one named constant specifically so that's a one-line change, not a scattered rewrite.
- **`production_simulator.service` has only been continuously running since 2026-08-15** - same-product baselines are `bootstrap`/`Low` today by honest necessity, not a bug (confirmed live: `P01.PROD.DISP01`'s targets all matched at Level B/`product_category`, 1 distinct day, exactly as the Phase 8 audit predicted).
- **Pump hydraulic efficiency, per-compressor kWh/Nm³** - excluded from every target profile (see Features Added).
- **`WT.RO01.Power_kW`'s inherited `measurement` mis-classification** (Phase 5/6) doesn't affect this phase (targets are keyed by tag name/thresholds, not `measurement`).
- **`equipment.rated_power`/`criticality` remain 0% filled** - load/context bucketing stays relative-to-own-observed-range (tercile), never %-of-rated.
- **`energy_kpi_worker.service` and `baseline_worker.service` are both still not installed** as of this report - install commands for both are below.

### Deferred Work
- Installing `baseline_worker.service` (and the still-outstanding `energy_kpi_worker.service` from Phase 6/7).
- `database/actual/config.db` migration for all six schema-bearing phases.
- Extending the target registry beyond the initial curated set (straightforward - add a raw/derived target name to the relevant `EquipmentTypeProfile`, no engine change needed).
- Everything explicitly reserved for Phase 9 per your direction: interpreting deviation magnitude, persistence-over-time, anomaly severity, red/yellow/green judgments, energy-opportunity recommendations, AI explanations. Phase 8 answers only "what's expected under comparable conditions, and how trustworthy is that," never "is this a problem."

### Next Recommended Phase
Phase 9 - Anomaly Detection (per the roadmap), consuming Phase 8's `compute_baseline()` output directly.

---

## Verification performed (Phase 8)

1. **Full regression suite**: `python -m unittest discover tests` → 288 tests (261 existing + 27 new), 5 failures - identical pre-existing baseline, zero new failures.
2. **27 new tests** covering: downsampling collapsing correlated adjacent samples, the exact "10,000 samples/one afternoon" scenario capped at `bootstrap`/Low, the full classification matrix, robust median/MAD vs. a skewed naive mean, the configurable-Normal-Range proof (a changed percentile pair visibly changes `_summarize()`'s output), context-bucket graceful broadening, the self-reference guard, in-limit-but-uncommon values never excluded vs. genuine threshold breaches correctly excluded, adaptive reference/recent window non-overlap and capping, full-pipeline chiller baselines (same-context similarity, ambient-context sensitivity, insufficient-history → `unavailable`, fault-spike exclusion), P01/P02 independence, slow-drift reference-lag, missing-context graceful fallback, and same-product-preferred-over-category production matching.
3. **`AppTest` sweep** - unaffected (Phase 8 built no UI); identical to Phase 7's result (all pass except the pre-existing, already-documented `6_Simulator_(testing_only).py` timeout).
4. **Live verification against the real running system** (not just synthetic data) - this is what caught two real bugs before they shipped:
   - `discover_targets()`'s instance-key extraction had an off-by-one, generating one bogus "instance" per *signal name* instead of per equipment unit (168 phantom targets instead of 84) - caught immediately on the first live run, fixed, re-verified.
   - `_current_context()` required a context reading to fall in the *exact* current 5-minute bucket, which a 300s-cadence tag (`ENV01.Temperature`) routinely misses by design - silently produced `ambient_bucket=unknown` for chillers even when a valid, recent ambient reading genuinely existed. Fixed by using `DatabaseManager.get_latest()` (the same method the Phase 6/7 stale-reading bug was fixed with) for current-context tag lookups instead of requiring exact bucket alignment.
   - Real, sensible baselines confirmed live across every equipment type after both fixes - e.g. `P01.UTILITY.CHL01.cop`: expected 5.501 (range 4.962-6.109), 619 representative samples across 7 distinct days, `mature`/Low; `P01.UTILITY.AIRHDR01.compressed_air_specific_energy`: expected 0.973 kWh/Nm³ (range 0.655-0.998), 752 samples across 7 days; `P01.UTILITY.CHWP01.flow_per_kw`: expected 2.497 (range 2.340-2.637), 746 samples across 7 days. (Per your explicit instruction, these are the *actual* computed figures from this implementation, not the plan's illustrative placeholders.)
5. **Real timing measurement** (not assumed): 213.3s / 84 targets / 0 errors for a full P01 pass - directly grounds the worker's `TICK_INTERVAL_SECONDS`.
6. Migration tested on a throwaway database copy (via SQLite's `.backup()` API) twice for idempotency, before being applied to the live database with an automatic backup.
7. `git status` confirms no file outside "Important Files Changed" was touched.
8. No new Python dependencies.

### To install `baseline_worker.service` (outstanding, alongside the still-outstanding `energy_kpi_worker.service` from Phase 6/7)

```bash
sudo tee /etc/systemd/system/baseline_worker.service > /dev/null << 'EOF'
[Unit]
Description=SmartMachineAI Baseline Engine Worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/test/SmartMachineAI
ExecStart=/home/test/SmartMachineAI/venv/bin/python -m app.baseline_worker
Restart=always
RestartSec=5
User=test

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now baseline_worker.service
sudo systemctl status baseline_worker.service --no-pager
```

---

## Phase 9 — Anomaly Detection Engine

**Completed:** 2026-08-15

### Features Added
- **`engine/anomaly_targets.py`** - curated rule registry, mirroring Phase 8's `baseline_targets.py` discipline (every rule maps to an *existing* Phase 8 baseline target - no new instrumentation invented). `AnomalyRule` frozen dataclass with a mandatory `threshold_provenance` field (`USER_CONFIGURED`/`ENGINEERING_RULE`/`MANUFACTURER_REFERENCE`/`STATISTICAL`/`SIMULATION_TUNING`) - every one of the 33 rules is honestly tagged `SIMULATION_TUNING` (engineering judgment for this dev-stage simulation, explicitly never to be presented later as a manufacturer/real-plant limit). 33 rules across plant energy (2), air compressors (1), air header (3, incl. 1 drift), chillers (6), chilled water pumps (6, incl. 1 drift), water supply pumps (6, incl. 1 drift), AHU (3, incl. 1 drift), cold rooms (2), production equipment (3, incl. 1 drift), filling (1). Pump hydraulic efficiency and per-compressor kWh/Nm³ explicitly excluded from every registry entry, per your clarification.
- **`engine/anomaly_migrator.py`** - `anomalies` table (one row per anomaly *occurrence*, never per evaluation), plus a **partial unique index** `idx_anomalies_one_open ON anomalies(plant_id, instance_key, target_key, rule_key) WHERE status = 'OPEN'` enforcing at-most-one-OPEN-row per exact condition while allowing unlimited RESOLVED rows (real recurrence history, never a mutable counter as the only record) - directly verified via a manual insert/conflict/resolve/reopen test before being trusted.
- **`engine/anomaly_engine.py`** - the core deterministic (zero LLM calls) evaluation engine. Reads *only* Phase 8's already-persisted `baseline_context_summary` rows - never recomputes a baseline. Key mechanisms:
  - **Bucket-based, tick-independent persistence**: `last_completed_bucket_start()`/`_recent_completed_buckets()` reuse Phase 8's own `_bucket_start()`, so a worker ticking every ~60s re-checking a still-in-progress 5-minute bucket sees the *identical* last-completed-bucket every tick - persistence only ever advances on a genuinely new completed bucket. Recomputed fresh from real historian data on every call (never a stored counter), which makes it restart-safe by construction for free - directly proven by a test that shows two independent calls against identical seeded data return identical persistence counts.
  - **Anti-flapping**: separate open/resolve conditions with independent persistence requirements and a resolve-band margin (`resolve_band_margin_pct`) - a single recovered sample surrounded by abnormal ones never resolves; only N *consecutive* within-band buckets do.
  - **Bootstrap/Low-confidence gating**: `_effective_open_thresholds()` scales both the deviation threshold and the required persistence by `rule.bootstrap_multiplier` (1.75) when the underlying baseline is `bootstrap` status - confirmed live (see bug #1 below) to actually work end-to-end, not just look right at the threshold-calculation level.
  - **Severity** (`classify_severity()`): INFORMATION/ATTENTION/WARNING/HIGH, never CRITICAL for pure statistics, floor-bumped to WARNING when an engineering limit is *also* exceeded - kept fully independent of `confidence`.
  - **Fault/maintenance policy A/B/C**: (A) an anomaly already open before an engineering-limit breach stays open, annotated, never duplicated; (B) a first-ever evaluation while an engineering alarm is already active on the exact same tag is suppressed as redundant; (C) a maintenance window suppresses opening a new candidate and pauses (never resets) an already-open one's persistence, always annotated.
  - **Interval-integrated excess energy/cost** (`bucket_excess_energy_cost()`): `max(actual-expected,0) * (aggregation_minutes/60)` per bucket, tariff applied per-bucket's own effective date (Phase 6's `effective_tariff()`), summed only across buckets that actually contributed to the open persistence - never a `latest_kw × total_duration` extrapolation. Returns `Unavailable` (`None`) cost, not a fabricated one, when no tariff is configured for a contributing date.
  - **Drift rules** (`evaluate_drift_rule()`): reads Phase 8's `reference`/`recent` baseline rows directly, requires both `mature` and genuinely non-overlapping windows, requires *two distinct* qualifying snapshots (different `computed_at`) before opening, neutral "no cause is inferred" language only. **Disclosed limitation**: because Phase 8 only persists the *latest* snapshot per bucket (no history), the pre-open "more than one evaluation" check uses a small process-local cache rather than full DB-reconstructed restart-safety - documented in the module docstring; once OPEN, a drift row's ongoing state is fully DB-persisted/restart-safe like every other anomaly.
  - **Occurrence/recurrence**: `count_prior_occurrences()` always does a real `COUNT(*)` of RESOLVED rows - never trusts a bare incremented field.
  - **Dict-driven writes** (`_write_anomaly_row()`) - avoids positional-placeholder miscounting on the 41-column table.
- **`app/anomaly_worker.py`** - separate worker service (own `fcntl.flock()` lock, `Restart=always` pattern matching every prior worker). One full pass = every rule × every matching discovered target × both plants, every tick (unlike Phase 8's baseline worker, which processes one target per tick - anomaly evaluation reads cheap indexed summaries, not raw historian data, so a full sweep per tick is affordable - confirmed by real measurement below). Drift rules self-throttle (cheap DB read + `computed_at` comparison, no-ops immediately if nothing changed), so no separate slow-cadence loop was needed for them.
- **`ui/pages/16_Anomalies.py`** ("Anomalies", not "Alarms" - deliberately distinct terminology from the engineering alarm system) - minimal read-only page with the requested **status filter (Open/Resolved/All, default Open)**, plus plant/category/severity filters, a severity-color-coded table, and a detail panel showing full evidence/assumptions/data-limitations JSON per selected row. Added to `ui/Home.py`'s sidebar directly after Event Records.
- **`ui/data_access.py`** extended with `get_plants()`, `get_anomalies()`, `get_anomaly_filter_options()` - read-only, following the file's existing plain-function convention; Phase 9's only write path is the worker, never the UI.

### Features Extended
- **`engine/baseline_targets.py`**: `plant_energy`'s `context_dimensions` reordered (bug fix, see below) and `non_production_demand_kw` added to its `derived_targets` - the one narrowly-scoped Phase 8 registry extension you approved specifically for Phase 9. (`production_energy_intensity`, the second proposed extension, was explicitly **not** built - it didn't fit Phase 8's per-bucket architecture cleanly and was descoped rather than forced in; noted under Deferred Work.)
- **`engine/baseline_engine.py`**: added `exclude_faults: bool = True` to `_fetch_clean_downsampled()`/`_fetch_target_series()` (default preserves exact Phase 8 behavior; Phase 9's `_actual_bucket_value()` passes `False` so it sees genuine current fault readings instead of having them filtered out - fault-exclusion belongs only in *learning* what's normal, never in *observing* what's happening right now). Also moved `context_bucket_key()` here (from a private duplicate in `app/baseline_worker.py`) as a shared public function, so the worker's writes and the anomaly engine's reads can never drift apart in encoding.

### Database Changes
- New table: `anomalies` (via `engine/anomaly_migrator.py`, same backup+additive+idempotent pattern as every prior migrator, plus the partial unique index described above). Applied to `database/simulation/config.db`. Backup: `database/simulation/config_before_anomaly_20260815_210440.db`.
- `database/actual/config.db` still needs all seven schema-bearing migrators before real use.

### Important Files Changed
- **New:** `engine/anomaly_targets.py`, `engine/anomaly_migrator.py`, `engine/anomaly_engine.py`, `app/anomaly_worker.py`, `ui/pages/16_Anomalies.py`, `tests/test_anomaly_engine.py` (53 tests).
- **Modified:** `engine/baseline_targets.py` (bug fix + one new derived target), `engine/baseline_engine.py` (`exclude_faults` parameter + `context_bucket_key()` relocation), `app/baseline_worker.py` (updated import after the relocation), `ui/data_access.py` (3 new read functions), `ui/Home.py` (nav entry + overview text).

### New Configuration
`app/anomaly_worker.py`'s `TICK_INTERVAL_SECONDS = 60.0` - set from a real measured full-pass cycle time (see Verification below), not a guess. `engine/anomaly_targets.py`'s `AnomalyRule.bootstrap_multiplier` default (1.75) - reused from Phase 8's own bootstrap-gating philosophy.

### New KPIs
None in the KPI-engine sense - Phase 9 classifies existing measurements/KPIs as normal/abnormal, it doesn't compute new physical quantities. The one new number type is **estimated excess energy/cost**, explicitly labeled `estimated_*` and gated to `energy_relevant` rules only - never presented as a KPI, saving, or verified figure.

### Known Issues
- **Two real bugs found and fixed during live/test verification** (see Verification below for detail): (1) a bootstrap-status baseline's lookback window was sized from the *un-adjusted* rule persistence requirement, making it structurally impossible to ever open against a bootstrap baseline for any rule where the bootstrap-adjusted persistence exceeded the raw one; (2) `estimated_excess_energy_kwh`/`estimated_excess_cost` were gated by `rule.category == "ENERGY" AND rule.energy_relevant` instead of `energy_relevant` alone, silently zeroing out financial-impact calculation for 9 of the 11 `energy_relevant=True` rules (every compressor/chiller/pump/AHU-fan/production-motor/filling power-deviation rule, all category `UTILITY`/`PRODUCTION`, not `ENERGY`) - only the 2 `plant_energy` rules were ever getting a cost figure. Both fixed, both covered by a new regression test.
- **Drift rules' pre-open persistence is not fully restart-safe** (disclosed limitation, not a bug) - see Features Added above. A process restart mid-candidate loses the "have we seen a second distinct qualifying snapshot" memory and starts the 2-snapshot count over; this only affects the *first-time-opening* path, never an already-OPEN drift row.
- **`energy_kpi_worker.service`, `baseline_worker.service`, and now `anomaly_worker.service` are all still not installed** as of this report (no sudo/TTY access this session) - install commands below. Because `baseline_worker.service` has never run continuously, `database/simulation/config.db`'s `baseline_context_summary` table has **zero rows** as of this report - Phase 9's live worker sweeps against the real system therefore correctly evaluate every rule×target as `skipped` ("baseline unavailable"), which is the *correct*, honest behavior given the actual data available, not a Phase 9 defect. Full lifecycle behavior (open → update → resolve → recur) was verified via the seeded test suite instead, where it's fully exercised and directly checked against real engine output.
- **`production_energy_intensity`** (the second proposed Phase 8 registry extension) was descoped rather than force-fit - see Features Extended.

### Deferred Work
- Installing `anomaly_worker.service` (alongside the still-outstanding `baseline_worker.service`/`energy_kpi_worker.service`).
- Once `baseline_worker.service` is running long enough to accumulate real `baseline_context_summary` coverage, re-run the live worker sweep to capture genuine live-data worked examples (opened/updated/resolved) - today's worked examples are real engine output against deliberately-seeded test data, not fabricated, but not yet exercised against live accumulated history either.
- `database/actual/config.db` migration for all seven schema-bearing phases.
- Everything explicitly reserved for later phases per your direction: energy-opportunity translation, savings verification, maintenance recommendations, equipment-health prioritization, AI explanations of *why* an anomaly occurred. Phase 9 answers only "what is abnormal, how persistent, what evidence supports it," never root cause or recommended action.

### Next Recommended Phase
Phase 10 (per the Master Development Roadmap) - translating defensible Phase 9 anomalies into energy-opportunity findings, strictly downstream of (never overlapping with) Phase 9's detection scope.

---

## Verification performed (Phase 9)

1. **Full regression suite**: `python -m unittest discover tests` → 341 tests (288 existing + 53 new), 5 failures - confirmed identical to the pre-existing baseline by running the same 3 failing modules (`test_equipment_knowledge`, `test_hybrid_router`, `test_pipeline`) against a `git stash` of every Phase 5-9 change; the failures reproduce unchanged with none of this work present, so they're pre-existing and unrelated, not new regressions.
2. **53 new tests** covering: bucket-boundary/day-boundary correctness, bucket-not-tick idempotence, restart-safety reconstruction, single-outlier non-opening, 3-consecutive-bucket opening, sustained-recovery-required-to-resolve, no-flapping, bootstrap-requires-stronger-evidence (both threshold and persistence), bootstrap opens marked `provisional`, baseline-unavailable skip, engineering-alarm-already-active suppression (policy B), pre-existing-anomaly-stays-linked-when-limit-later-crossed (policy A), same-condition updates not duplicates, resolved-can-recur-as-a-new-row with correct `occurrence_count`, maintenance-window suppression (policy C), threshold-provenance present in every stored row, no-root-cause/no-savings language scan, P01/P02 independence, interval-integration math (not extrapolation), tariff-Unavailable-without-configuration, tariff-transition-uses-each-bucket's-own-rate, operating-state gating, drift 2-distinct-snapshots-before-opening, drift unchanged-snapshot no-op (both pre-open and post-open), drift resolves-when-no-longer-qualifying, drift neutral-language, and the partial-unique-index DB-level constraint.
3. **`AppTest`** - `ui/pages/16_Anomalies.py` verified twice: once against a synthetic seeded row (table renders, severity coloring applies, detail panel/evidence expanders populate correctly, then the row was deleted to leave the live DB clean) and once against the genuine empty live state (`st.info` renders, no exception). `ui/Home.py`/`ui/data_access.py` changes compile-checked; the nav addition follows the exact existing `st.Page` pattern.
4. **Live verification against the real running system** (not just synthetic data) - caught the two real bugs documented above:
   - Bug #1 (bootstrap window-sizing) was caught by a deliberately-designed test (`test_bootstrap_open_is_marked_provisional`) that seeded exactly the bootstrap-adjusted persistence requirement's worth of abnormal buckets and got `candidate` instead of the expected `opened` - traced to `evaluate_deviation_rule()` sizing its lookback window from the raw, un-adjusted `rule.persistence_periods_open` before baseline status (and therefore the *effective* threshold) was even known. Fixed by sizing the window from the worst-case (bootstrap-multiplier-adjusted) persistence requirement up front; re-verified both the fixed test and the full suite pass.
   - Bug #2 (energy-relevant gating) was caught by manually inspecting a worked-example anomaly's full JSON output (see below) and noticing `estimated_excess_energy_kwh: null` despite a configured tariff and a `energy_relevant=True` rule - traced to an extra, inconsistent `rule.category == "ENERGY"` condition alongside the `energy_relevant` check, contradicting the field's own stated purpose (its dataclass comment literally says "item 18 - gates estimated-excess-cost", singular). Fixed by removing the redundant category check; added a regression test (`test_energy_relevant_utility_category_rule_still_gets_excess_cost`) using a `category=UTILITY` rule specifically, so this can't silently regress again.
   - Live worker sweep against the real `database/simulation/{config,machine_data}.db` (3 consecutive cycles, ~2s apart): 15.7-16.0s each, `{'skipped': 134}` every time (134 = 33 rules × matching targets × 2 plants), 0 errors, 0 anomaly rows written (correctly bounded - nothing baseline-backed exists yet, see Known Issues), `plc_logger.service`/`event_monitor.service`/`streamlit.service` confirmed still `active` throughout, and the historian's latest write timestamp confirmed current - no interference with existing services.
   - Migration (`anomaly_migrator.py`) tested against a throwaway database copy twice for idempotency, plus a direct manual insert/conflict/resolve/reopen test proving the partial unique index behaves exactly as designed, before being applied to the live database with an automatic backup.
5. **Two real worked examples** (actual engine output against deliberately-seeded, controlled data - not the live system, since `baseline_context_summary` has zero live rows as explained under Known Issues; not fabricated placeholders either):
   - **Deviation, opened**: `P01.UTILITY.CHL01` chiller power held at 78.4 kW for 3 consecutive 5-minute buckets against a baseline of median 52.3 kW (range 45.0-59.0, MAD 3.4, mature/High) → opened with `severity=HIGH`, `normalized_deviation≈7.68`, `deviation_percent≈49.9%`, `estimated_excess_energy_kwh=6.525` (= (78.4-52.3) kW × 3 buckets × 5/60 h), `estimated_excess_cost=3.393 MYR` at a 0.52 MYR/kWh tariff, `threshold_provenance=SIMULATION_TUNING` visible in both the row and its assumptions text.
   - **Drift, opened**: `P01.UTILITY.CHWP01` pump flow-per-kW recent baseline (2.65) vs. reference baseline (3.10) across two distinct snapshots 5 minutes apart → first snapshot produced `drift_candidate` (no row written), second distinct snapshot opened with `severity=ATTENTION`, `deviation_percent≈-14.5%`, evidence carrying both baselines' history windows, assumptions explicitly stating *"no cause is inferred - this reports that recent behavior has moved away from the historical reference, nothing more."*
6. `git status`/`git diff --stat` confirms no file outside "Important Files Changed" was touched (checked against the pre-existing separate uncommitted WIP noted in `CLAUDE.md`, which remains untouched).
7. No new Python dependencies.

### To install `anomaly_worker.service` (alongside the still-outstanding `baseline_worker.service`/`energy_kpi_worker.service`)

```bash
sudo tee /etc/systemd/system/anomaly_worker.service > /dev/null << 'EOF'
[Unit]
Description=SmartMachineAI Anomaly Detection Worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/test/SmartMachineAI
ExecStart=/home/test/SmartMachineAI/venv/bin/python -m app.anomaly_worker
Restart=always
RestartSec=5
User=test

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now anomaly_worker.service
sudo systemctl status anomaly_worker.service --no-pager
```

---

## Phase 10 — Energy Opportunity Engine

**Completed:** 2026-08-15

### Features Added
- **`engine/opportunity_targets.py`** - curated rule registry (10 rules), mirroring Phase 9's `anomaly_targets.py` discipline exactly. Every rule maps to exactly one `energy_relevant=True` Phase 9 anomaly rule (10 of Phase 9's 11 energy-relevant rules - the 11th, the compressed-air specific-energy `DRIFT` rule, is deliberately deferred - see Known Issues). **Per your Phase 10 correction (item 1), every single rule declares `saving_basis="unavailable"`** - after working through each rule individually as instructed ("justify that rule individually rather than enabling this generically"), none could defensibly claim a computed Potential Saving today: no engineering-target configuration mechanism exists anywhere in this schema (only safety warning/alarm thresholds do), and the one candidate for a baseline-as-target justification (the drift rule) would need a new, unverified Nm³-throughput calculation - deferred rather than shipped unverified. The `saving_basis` enum (`engineering_target_delta` / `justified_baseline_target` / `unavailable`) and the dispatch function (`compute_potential_saving()`) are fully built and ready for a future rule the moment real supporting data exists - this is a data-availability limitation, not a missing feature.
- **`engine/opportunity_migrator.py`** - `energy_opportunities` table, same backup+additive+idempotent pattern, plus a **partial unique index** `idx_energy_opportunities_one_new ON energy_opportunities(plant_id, instance_key, rule_key) WHERE status = 'NEW'` (mirrors Phase 9's proven `anomalies` index exactly) - directly verified via a manual insert/conflict/dismiss/reopen test before being trusted, same as Phase 9's.
- **`engine/opportunity_engine.py`** - the core deterministic (zero LLM calls, source-inspection tested) engine. Reads *only* Phase 9's persisted `anomalies` rows - never recomputes excess energy/cost, never touches the historian. Key mechanisms:
  - **Qualification gate**: an anomaly only becomes opportunity evidence if its rule is `energy_relevant=True` *and* Phase 9 actually populated a positive `estimated_excess_energy_kwh` on the row - a hard structural exclusion, not a soft downgrade, so CONDITION-only findings (vibration, bearing temp, COP, filter DP drift, etc.) can never become opportunities (confirmed by a registry-structure test, not just by convention).
  - **Potential Saving (item 1)**: `compute_potential_saving()` always returns `unavailable` + a stated reason today (see Features Added above) - the Observed Estimated Excess Energy/Cost figure (reused verbatim from Phase 9) is always shown as evidence regardless, kept as two visibly separate fields end-to-end (DB columns, evidence dict, UI) per your terminology requirement.
  - **Annualization (item 2)**: `compute_annualization()` requires either a configured schedule (checked, but none exists anywhere in this database, so this path is architecturally present but unexercised) or **both** `>= MIN_OCCURRENCES_FOR_ANNUALIZATION` (5) distinct linked anomaly occurrences **and** `>= MIN_OBSERVATION_SPAN_DAYS_FOR_ANNUALIZATION` (30) days of span - configurable named constants, never a bare `>=2`. Since `estimated_potential_saving_period` is always `None` today, monthly/annual projections are always `None` too - the eligibility gate itself is independently tested with synthetic inputs to prove two occurrences (or ten occurrences over five days) never qualify, while five occurrences over thirty-plus days does.
  - **"Engineering Investigation Priority" (item 3)**: `compute_priority()` keeps a `potential_saving_component` (weight ×3, always 0 when saving is Unavailable) strictly separate from an `observed_impact_component` (weight ×1, always populated from real observed cost) - plus confidence/recurrence/severity/difficulty/criticality components, all exposed in a stored `priority_breakdown_json` so the UI can show the full breakdown, not just a label. Directly tested: two opportunities with identical observed cost score differently once one has a real computed potential saving and the other doesn't - the unavailable one can never look "financially comparable."
  - **Deduplication/recurrence**: at most one `NEW` row per `(plant_id, instance_key, rule_key)` (DB-enforced). A recurring anomaly condition (new anomaly id, same rule/instance) **appends** to `source_anomaly_ids` and **recomputes** cumulative `observed_excess_energy_kwh`/`cost` fresh from the current values of every linked anomaly id (never an incremental add) - correctly reflects an OPEN anomaly's growing excess figure without ever double-counting.
  - **Dismiss lifecycle (item 4)**: `dismiss_opportunity()` validates the reason against `DISMISSAL_REASONS` (`NOT_ACTIONABLE`/`EXPECTED_OPERATION`/`KNOWN_ISSUE`/`INSUFFICIENT_EVIDENCE`/`OTHER`), sets `status='DISMISSED'`/`dismissed_at`/`dismissal_reason`/optional `dismissal_comment` - never deletes, evidence and links fully preserved. Genuinely new evidence (an anomaly id not previously linked, first-detected after the dismissal timestamp) can create a **fresh** `NEW` row later, explicitly excluding the old (already-dismissed) evidence from the new row's totals - tested directly, including the "no new evidence yet" case correctly producing no row at all.
  - **Confidence**: derived from the representative linked anomaly's own `baseline_confidence`, downgraded one tier when `baseline_level == "C"` (weak context-matching) - no longer downgraded for "inferred recoverable fraction" (removed along with `full_excess`, since nothing is inferred anymore - saving is either a real number or explicitly Unavailable).
  - **Non-production gating**: `compressor_non_production_air_opportunity` only qualifies when the contributing anomaly's plant was predominantly in a non-production state at that time (reuses Phase 8's own production-state lookup) - directly tested for both the qualifying and non-qualifying (production-state) case.
- **`app/opportunity_worker.py`** - separate, deliberately low-frequency service (own `fcntl.flock()` lock, `Restart=always`), reading only `anomalies`/`energy_tariffs`/bounded `production_batches` queries - no historian access at all.
- **`ui/pages/17_Energy_Opportunities.py`** ("Energy Opportunities", read-only table + exactly one write action) - Priority/Plant/Equipment/Opportunity/Observed Estimated Excess Cost/Estimated Potential Saving/Confidence/Status columns, sorted by priority score then recency; a detail panel showing Observed Estimated Excess Cost and Estimated Potential Saving as two visually separate fields (with the Unavailable reason shown directly beneath when applicable, per your terminology requirement), plus evidence/priority-breakdown/assumptions/limitations expanders and the linked source anomaly ids. The one approved write action - **Dismiss** (reason + optional comment, gated to `admin`/`engineer` roles via `ui.auth.can_edit()`) - verified through the real UI via `AppTest`: session simulated as an `engineer`, the form submitted, the row correctly moved to `DISMISSED` with reason/timestamp recorded and all evidence preserved, then re-verified absent from the default `NEW` view.
- **`ui/data_access.py`** extended with `get_plants()` (shared with the Anomalies page), `get_opportunities()`, `get_opportunity_filter_options()`, `dismiss_opportunity_action()` - the dismiss function is the only write path exposed to the UI, delegating to `engine.opportunity_engine.dismiss_opportunity()` for the actual validation/logic.

### Features Extended
None - Phase 9's `anomalies` table/engine/worker are entirely untouched (confirmed via `git status`/`git diff --stat`); Phase 6/8's KPI/tariff/baseline engines are read-only consumed, not modified.

### Database Changes
- New table: `energy_opportunities` (via `engine/opportunity_migrator.py`). Applied to `database/simulation/config.db`. Backup: `database/simulation/config_before_opportunity_20260815_220127.db`.
- `database/actual/config.db` still needs all eight schema-bearing migrators before real use.

### Important Files Changed
- **New:** `engine/opportunity_targets.py`, `engine/opportunity_migrator.py`, `engine/opportunity_engine.py`, `app/opportunity_worker.py`, `ui/pages/17_Energy_Opportunities.py`, `tests/test_opportunity_engine.py` (31 tests).
- **Modified:** `ui/data_access.py` (4 new functions), `ui/Home.py` (nav entry + overview text).

### New Configuration
`app/opportunity_worker.py`'s `TICK_INTERVAL_SECONDS = 900.0` (15 min) - a deliberate design choice (avoid unnecessary financial-projection churn, per your own direction), not a performance necessity - real measurement showed a full 10-rule pass completes in well under half a second even at a realistic seeded scale (see Verification below). `engine/opportunity_engine.py`'s `MIN_OCCURRENCES_FOR_ANNUALIZATION = 5` / `MIN_OBSERVATION_SPAN_DAYS_FOR_ANNUALIZATION = 30` - tunable, disclosed judgment constants, not physics.

### New KPIs
None - Phase 10 classifies/prioritizes existing Phase 9 findings, it doesn't compute new physical quantities. `observed_excess_energy_kwh`/`observed_excess_cost` are Phase 9's own figures, summed across linked occurrences, never recomputed.

### Known Issues
- **Every opportunity in this build shows Potential Saving: Unavailable** - a direct, deliberate consequence of your item-1 correction, not an oversight. This is disclosed prominently rather than hidden.
- **The compressed-air specific-energy drift rule was deliberately deferred**, not built - it's Phase 9's only `energy_relevant` `DRIFT`-type rule, and drift rules never get a Phase-9-computed excess energy figure (interval integration doesn't apply to a point-in-time reference-vs-recent comparison). A real opportunity here would need a new Nm³-throughput calculation to convert a kWh/Nm³ delta into an actual kWh figure - real new physics that hasn't been built or verified. Left for a future rule addition rather than shipped unverified.
- **Schedule-based annualization is architecturally present but unexercised** - `shift_definitions`/`non_production_periods` are both empty in the live database, so `compute_annualization()`'s schedule-available branch has no real data to consult and always falls through to the recurrence-based/unavailable path.
- **`baseline_context_summary` and `anomalies` are both still empty on the live system** (`baseline_worker`/`anomaly_worker` never installed) - `energy_opportunities` is therefore also empty live, for the same honest reason as Phase 9's report. All lifecycle behavior (qualify → open → recur → dismiss → supersede) was verified via the seeded test suite and direct-call smoke tests instead.
- **`energy_kpi_worker.service`, `baseline_worker.service`, `anomaly_worker.service`, and now `opportunity_worker.service` are all still not installed** (no sudo/TTY access this session, re-confirmed by direct `systemctl` inspection, not assumed) - install commands below.
- **A real bug found and fixed during live/UI verification**: `ui/pages/17_Energy_Opportunities.py`'s Dismiss form used `auth.can_edit()` with no arguments - since `can_edit()`'s signature is `can_edit(*roles_allowed_to_edit)` and an empty tuple never matches any role, this silently hid the Dismiss form from *every* user, including admins/engineers. Caught by simulating an `engineer` session via `AppTest` and noticing the "Dismiss this opportunity" subheader never appeared; fixed to `auth.can_edit("admin", "engineer")`, matching every other page's exact usage (`ui/pages/3_Setpoints.py`, `4_Service_and_Maintenance.py`, etc.) - re-verified the form and the full dismiss action afterward, both working correctly.

### Deferred Work
- Installing `opportunity_worker.service` (alongside the three still-outstanding workers from Phases 6/8/9).
- The compressed-air specific-energy drift opportunity rule, once a real Nm³-throughput calculation is designed and verified.
- Schedule-based annualization, once `shift_definitions`/`non_production_periods` have real configured data.
- An `engineering_target_delta`-based rule, once a real target-configuration mechanism exists anywhere in the schema (none does today - only safety thresholds).
- Everything explicitly reserved for Phase 11 per your direction: investigation/approval workflow states, before/after baselines, measured verified savings. Phase 10 ends at "we have identified a defensible potential opportunity" (today: "...and a defensible potential *saving* amount, specifically, is not yet determinable") - never savings verification.

### Next Recommended Phase
Awaiting your explicit instruction, per "Do not expand into Phase 11" and "STOP before Phase 11."

---

## Verification performed (Phase 10)

1. **Full regression suite**: `python -m unittest discover tests` → 372 tests (341 existing + 31 new), 5 failures - identical, pre-existing, unrelated (same `test_equipment_knowledge`/`test_hybrid_router`/`test_pipeline` failures reproduced in every phase's regression run since Phase 8).
2. **31 new tests** covering: registry structural checks (every opportunity rule sources an energy-relevant anomaly rule; no CONDITION-only rule has a matching opportunity rule; every rule currently declares `unavailable`), qualification (qualifying anomaly creates an opportunity, no qualifying anomaly produces none, below-minimum-impact never opens, an anomaly without excess energy never qualifies), deduplication (repeated evaluation of the same open anomaly never duplicates; a recurring anomaly appends and accumulates without double-counting - Worked Example D, below), saving-unavailable (a stated reason always accompanies `Unavailable`; Observed Excess and Potential Saving stay independently visible; no forbidden saving/root-cause language anywhere in stored text), annualization safeguards (no saving → no annualization; two occurrences never qualify; one short event is never extrapolated 24×365; sufficient occurrences+span does enable a projection; sufficient occurrences with insufficient span still doesn't), priority semantics (unavailable saving never gets potential-saving credit; two opportunities with identical observed cost score differently once one has a real computed saving; every documented breakdown component is present; UNKNOWN difficulty is neutral; absent criticality never fabricated), dismiss lifecycle (dismiss preserves the row/evidence; invalid reasons rejected; new evidence after dismissal creates a fresh row without resurrecting old evidence; duplicate-NEW rejected at the DB level), non-production gating (both the qualifying and non-qualifying case), P01/P02 independence, and source-inspection checks confirming zero LLM/PLC-control imports in both the engine and the worker.
3. **`AppTest`** - `ui/pages/17_Energy_Opportunities.py` verified three times: genuine empty live state (no exception), a synthetic seeded opportunity (table/detail panel/expanders render correctly), and a full **Dismiss action exercised through the real UI** with a simulated `engineer` session (form submitted, DB row correctly updated, then the smoke-test row deleted to leave the live DB clean) - this is what caught the `can_edit()` bug documented above.
4. **Live verification against the real running system**: 3 consecutive `opportunity_worker` cycles against `database/simulation/{config,machine_data}.db` - 0.034-0.044s each, `{}` (nothing to evaluate, correctly, since `anomalies` is still empty live), 0 errors; `plc_logger`/`event_monitor`/`streamlit`/`production_simulator` all confirmed `active` throughout; historian's latest write timestamp confirmed current; `anomalies`/`energy_opportunities` row counts confirmed still 0 on the live system (expected, honest, matches Phase 9's own live state).
5. **Real performance measurement at realistic scale** (not just the near-empty live system): a throwaway DB seeded with 90 anomaly rows (9 RESOLVED + 1 OPEN per opportunity rule, across all 10 rules) - first pass 0.377s (10 opportunities opened, each correctly linking 9 anomalies), subsequent passes 0.156-0.159s (`unchanged`, no unnecessary writes) - directly grounds `TICK_INTERVAL_SECONDS`.
6. Migration tested against a throwaway database copy for idempotency, plus a direct manual insert/conflict/dismiss/reopen test proving the partial unique index behaves exactly as designed, before being applied to the live database with an automatic backup.
7. `git status`/`git diff --stat` confirms no file outside "Important Files Changed" was touched.
8. No new Python dependencies.

### Worked examples (real engine output against seeded test data, not fabricated - `anomalies`/`baseline_context_summary` are empty on the live system, same disclosed limitation as Phase 9)

- **Example A - qualifying, Potential Saving Unavailable**: `P01.UTILITY.CHL01` `chiller_power_opportunity`, sourced from a RESOLVED `chiller_power_deviation` anomaly (`estimated_excess_energy_kwh=6.5`, `estimated_excess_cost=RM3.38`, `baseline_level=B`, `baseline_confidence=High`) → qualifies → `confidence=High` (no Level-C downgrade) → `saving_basis=unavailable`, `estimated_potential_saving_period=null`, reason: *"This opportunity type has no configured engineering target and its context-matched baseline is not automatically treated as an achievable target..."* → `priority=LOW`, `priority_score=4.0`, breakdown `{potential_saving_component: 0, observed_impact_component: 0, confidence_component: 3, severity_component: 1, ...}` → `status=NEW`.
- **Example B - anomaly, never an opportunity (structural)**: `pump_vibration_increase`/`chiller_cop_low`/`ahu_filter_dp_drift`/`production_vibration_drift` (CONDITION-category, `energy_relevant=False`) have **no corresponding `OpportunityRule` in the registry at all** - confirmed by `TestRegistry.test_no_condition_only_anomaly_rules_have_opportunity_rules` - these findings can structurally never produce an opportunity row, remaining visible only on the Anomalies page.
- **Example C - Potential Saving Unavailable is universal in this build, but never silently substituted**: every opportunity's `estimated_potential_saving_period` is `None` while `observed_excess_cost`/`observed_excess_energy_kwh` remain real, non-null figures - directly tested (`test_observed_excess_still_shown_as_evidence_despite_saving_unavailable`) and shown on the UI as two independently-labeled fields, exactly matching your terminology requirement.
- **Example D - recurring, cumulative, no double-counting**: the same `P01.UTILITY.CHL01` chiller opportunity from Example A, one day later, absorbs a second, newly-OPENED `chiller_power_deviation` occurrence (`estimated_excess_energy_kwh=9.1`, `estimated_excess_cost=RM4.73`) → **same row** (`id` unchanged) → `occurrence_count: 1 → 2`, `observed_excess_cost: RM3.38 → RM8.11` (3.38+4.73, exactly once each), `source_anomaly_ids: [1] → [1, 2]` - re-evaluating with nothing new in between correctly returned `"unchanged"` (no write).

### To install `opportunity_worker.service` (alongside the three still-outstanding workers)

```bash
sudo tee /etc/systemd/system/opportunity_worker.service > /dev/null << 'EOF'
[Unit]
Description=SmartMachineAI Energy Opportunity Worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/test/SmartMachineAI
ExecStart=/home/test/SmartMachineAI/venv/bin/python -m app.opportunity_worker
Restart=always
RestartSec=5
User=test

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now opportunity_worker.service
sudo systemctl status opportunity_worker.service --no-pager
```

---

## Autonomous Development Session

Started 2026-08-15, per your temporary autonomous-development authorization (roadmap approval-gate rule 2 suspended for this session only, subject to the stop conditions in that authorization).

**Phase 9 — STARTED** (already in progress at authorization time, per your explicit "Phase 9 approval/clarification instructions already provided remain authoritative")
**Phase 9 — COMPLETED** 2026-08-15 - see the full section above. Two real bugs found and fixed during verification (bootstrap window-sizing; energy-relevant cost-gating), both regression-tested. No stop condition encountered.

**Phase 10 — STOPPED** 2026-08-15 (immediately, before any implementation). Reason: **no written "Master Development Roadmap" document exists anywhere in this repository** - confirmed by a filesystem search (`find . -iname "*roadmap*"`, plus checking `FACTORY_AI_CURRENT_SYSTEM_REPORT.md`/this file for any reference to a roadmap artifact) - zero matches. Every phase actually implemented in this project so far (Phases 5-9, the ones covered by this session) was specified exclusively by you writing a detailed multi-point message directly in chat before implementation began (23 points for Phase 6, 35 for Phase 8, 36 for Phase 9) - there has never been a standing document to read for "the next phase's requirements." Phase 10 has only ever been described, in this session, as one sentence: "translate defensible anomalies into energy opportunities." That is not enough to safely implement - Phase 10 is specifically the point where the system starts making financially-consequential claims ("opportunity," and Phase 11's "savings" after it), which is exactly the category of decision every prior phase needed your detailed scoping to bound correctly (terminology constraints, verification standards, what counts as "defensible," what must stay `Unavailable`). This matches the autonomous authorization's own stop condition 6 ("Ambiguous engineering decision... cannot be derived from existing project data, configured engineering information, manufacturer/reference information, or the approved roadmap") and section 4's instruction not to fabricate requirements merely to keep a phase moving.

**Decision needed from you:** please provide the same kind of detailed spec for Phase 10 you've provided for every phase so far (or point me to wherever the roadmap actually lives, if it exists outside this repo/session and I've simply never seen it) - I don't want to guess at financial-claim boundaries on your behalf. The application is left in its last verified-working state: Phase 9 complete, all tests passing, no code changes made toward Phase 10.

**Phase 10 — STARTED** 2026-08-15, following your detailed 32-point spec plus 5 required design corrections (removing the generic `full_excess` methodology; tightening annualization to configurable minimum evidence; separating "Engineering Investigation Priority" into potential-saving vs. observed-impact components; approving the Dismiss lifecycle action; confirming the rest of the architecture).
**Phase 10 — COMPLETED** 2026-08-15 - see the full section above. One real bug found and fixed during UI/AppTest verification (`auth.can_edit()` called with no role arguments, silently hiding the Dismiss action from every user). No stop condition encountered. Per your explicit instruction, stopping here - not proceeding into Phase 11.

**Phase 10.5 — ATTEMPTED** 2026-08-16, following your detailed 28-item commissioning/soak-test spec (issued outside the autonomous-mode framing - a directly-supervised task). **Result: PARTIAL** - see the full section below. No code bugs found; the sole blocker is the pre-existing, unchanged no-sudo/no-TTY constraint preventing systemd installation. Per your explicit instruction, stopping here after this report - not starting Phase 11.

**Phase 10.6 — COMPLETED** 2026-08-16 - production systemd deployment of all four analytics workers, executed by you (sudo) from unit files prepared in `deploy/systemd/`. **Result: PASS.** All four `ACTIVE`+`ENABLED`, restart-tested via a real `sudo systemctl restart`, zero duplicate records, zero regressions, the two real opportunities from Phase 10.5 confirmed to survive the restart intact. This resolves Phase 10.5's sole PARTIAL reason. Per your explicit instruction, stopping here - not starting Phase 11.

---

# Phase 10.5 — Pipeline Commissioning & Soak Test

**Attempted:** 2026-08-16. **Result: PARTIAL** (see "Commissioning result" below for the exact, single reason). **UPDATE 2026-08-16 (Phase 10.6): the sole blocking reason - systemd installation requiring sudo - has since been resolved. All four workers are now installed, `ACTIVE`, and `ENABLED` under systemd. See the "Phase 10.6 — Production Service Deployment" section below for full verification. Phase 10.5's finding stands as-is otherwise: every one of the other 26 criteria it verified remains valid and unchanged.**

### Services before (inspected directly, not assumed)
`plc_logger.service`, `production_simulator.service`, `event_monitor.service`, `streamlit.service` - all `loaded`/`enabled`/`active (running)`, confirmed via `systemctl status`. `energy_kpi_worker.service`, `baseline_worker.service`, `anomaly_worker.service`, `opportunity_worker.service` - all confirmed genuinely absent ("Unit ... could not be found").

### Services installed
**None.** `sudo -n true` fails ("a password is required") - this session has no passwordless sudo and no TTY to supply one interactively, the same standing constraint documented in every phase's report since Phase 6. This is the single reason commissioning is PARTIAL rather than PASS - every one of your other 26 success criteria was independently verified (see below); "all required workers installed/enabled" as systemd units specifically could not be attempted.

### What was done instead
All four analytics workers were run as real, long-lived **background processes** (`nohup python3 -u -m app.<worker> &`, same project venv/user as every other process) against the **live** `database/simulation/{config,machine_data}.db` for a continuous ~18-minute observation window, with periodic live inspection throughout - not systemd-managed, but otherwise identical code, identical database, identical concurrency conditions to a real deployment. This is left **running** for you to continue observing (per your own instruction 20) - PIDs and how to stop them are listed at the end of this section.

### Services after (still running as I write this)
| Worker | PID | Uptime at last check | Status |
|---|---|---|---|
| `energy_kpi_worker` | 256137 | ~19 min | running, 0 errors |
| `baseline_worker` | 256140 | ~19 min | running, 0 errors |
| `anomaly_worker` | 256193 | ~18 min | running, 0 errors |
| `opportunity_worker` | 256196 | ~18 min | running, 0 errors |

### Restart safety (item 4) - directly tested, not assumed
- **`energy_kpi_worker`**: killed and restarted mid-run. `energy_kpi_maximum_demand`/`energy_kpi_daily_summary` row counts identical before/after (2/2) - no duplicated demand intervals, no duplicated daily rows.
- **`baseline_worker`**: killed and restarted mid-run. `baseline_context_summary` row count identical before/after restart (12/12 at the moment of the test), a direct duplicate-key query (`GROUP BY plant_id, instance_key, target_key, baseline_type, context_bucket_key HAVING COUNT(*)>1`) returned empty both before and after - confirms the `ON CONFLICT ... DO UPDATE` upsert is genuinely idempotent across a real restart, not just in the unit tests. No full destructive rebuild observed - it resumed its round-robin rotation cleanly.
- **`anomaly_worker`** / **`opportunity_worker`**: killed and restarted mid-run. `anomalies` count identical before/after (1/1 at the moment of that specific test); both processes came back to `ACTIVE` and resumed normal cycling immediately, logging "33 rules registered.../10 opportunity rules registered..." exactly as at first start. The deeper claims (an OPEN anomaly's bucket-based persistence surviving a restart without duplication or incorrect reset, an opportunity's `source_anomaly_ids` staying idempotent across a restart) are the exact things `tests/test_anomaly_engine.py`'s `test_restart_safety_candidate_persistence_reconstructed_identically` and `tests/test_opportunity_engine.py`'s dedup tests already prove deterministically against seeded data - re-confirmed passing in this same session's regression run (see below). The live system's real anomaly count was too small during the actual restart moment to add further evidence beyond what those tests already establish, and per your instruction 6, no synthetic rows were inserted into the live DB to manufacture a bigger live test case.

### Single-instance protection (item 10) - directly tested
Attempted a duplicate start of all four workers while the originals were running: all four printed *"Another `<worker>` instance already holds `logs/<worker>.lock` - refusing to start a second one. Exiting."* and exited cleanly (`fcntl.flock(..., LOCK_EX | LOCK_NB)`, the same mechanism proven for `baseline_worker`/`production_simulator` since Phase 8). No manual process ever ran concurrently with itself.

### Pipeline order verification (item 5) - real data observed flowing through every stage that had enough evidence
- `plc_data historian` → growing continuously throughout (8,391,530 → 8,411,436 rows over the observation window, ~1,150 rows/min, consistent with the pre-existing rate - unaffected by the four new workers).
- `historian → energy KPI` → **populated**: `energy_kpi_maximum_demand` (455.65 kW P01 / 451.42 kW P02, billing period 2026-08-01 to 2026-09-01) and `energy_kpi_daily_summary` (2026-08-15: 2,425.12 kWh / RM1,212.56 for P01) both real, computed values - confirmed via the Energy Dashboard's "Billing-Month Peak So Far" metric now reading `455.65 kW` instead of `Unavailable` (item 16, directly verified via `AppTest`).
- `historian + context → baseline_context_summary` → **populated and growing**: 0 → 89 rows over the window; confidence distribution `{Low: 89}` (young single-day history, exactly as every prior phase's audit predicted - not a defect); `baseline_status` `{bootstrap: 24, mature: 62}`.
- `baseline + current measurements → anomaly` → **populated**: 6 real anomaly rows appeared live (`P01.UTILITY.AC01` compressor power deviation, `P01.UTILITY.CHL02` chiller power deviation, others), split `{OPEN: 2, RESOLVED: 4}` - a genuine open→resolve lifecycle was observed live, not just in tests. `suppressed`/`candidate` counts appeared correctly in the cycle logs (engineering-alarm-co-occurrence and persistence-building working as designed).
- `anomaly → energy opportunity` → **correctly still empty**, for an honest, directly-confirmed reason: the one `energy_relevant` anomaly that opened live (the compressor) was manually checked against `compressor_non_production_air_opportunity`'s qualification and correctly returned `[]` - the plant was in a **production** state at that time, so the non-production gate correctly excluded it (item 28's exact false-positive scenario, observed live, not just in a unit test). Per your instruction 5: **"Pipeline operational, downstream output pending sufficient evidence/history."**

### Do-not-lower-confidence-requirements confirmation (item 6)
No baseline minimum history, anomaly persistence, or opportunity qualification threshold was changed to produce results. No schedule/criticality/engineering target was fabricated. No synthetic anomaly or opportunity row was inserted into the live database at any point during this soak test (the smoke-test rows inserted-then-deleted during Phase 9/10's own UI verification predate this instruction and are documented in those phases' own reports).

### Database concurrency (item 7)
Zero occurrences of "database is locked", write contention, failed commits, or corruption across the entire observation window and across a full 372-test regression run executed *while all four workers were actively running concurrently* (a genuine concurrency stress test, not a synthetic one). `config.db` has no stray `-wal`/`-shm` files (rollback-journal mode, clean). `machine_data.db`'s `-wal` file (972KB) is expected/normal for its write-heavy historian role.

### CPU and memory (item 8)
System: 8 cores, 31GB RAM. Load average ranged 4.0-5.5 (out of 8) throughout - never starved `plc_logger` (steady ~11% CPU throughout, historian write rate unaffected), `streamlit` (~54% CPU, confirmed responsive via `AppTest` throughout), `event_monitor`, or `production_simulator`. Memory: 5.7-5.9GB used, 26GB available throughout - no growth trend suggesting a leak over the observation window. Per-worker: `baseline_worker` ~30-40% CPU (expected - it's the one continuously active worker, 5s tick), `anomaly_worker` ~25-40% CPU during its ~20s cycles then idle, `energy_kpi_worker`/`opportunity_worker` both under 5% (infrequent, cheap ticks).

### Baseline worker scheduling (item 9)
Single instance confirmed (see above). Its `time.sleep(tick_interval)` (5s) sits *after* each `process_one_target()` call in a plain sequential loop - there is no possibility of overlapping runs by construction (no threading/async, one call at a time). No runaway/overlapping full-pass behavior observed.

### Logs (item 11)
Startup lines are informative (`"<n> rules registered across <p> plant(s)"`) but real-time-visible only when launched with `python3 -u` (unbuffered) - under `nohup ... &` with default buffering, `print()` output sits in an 4-8KB OS pipe/stdio buffer and doesn't appear until it fills, which would make `journalctl`-based diagnosis under systemd much slower to show live activity than expected. **This is worth a one-line fix when the systemd units are eventually created**: add `Environment=PYTHONUNBUFFERED=1` to each unit file (shown in the install commands below) - not a code change, a unit-file setting. `anomaly_worker`/`opportunity_worker` only print a `"cycle: ..."` line when something noteworthy happened (opened/updated/error) - confirmed non-flooding across ~18 minutes of continuous ticking (60+ silent cycles for `anomaly_worker` alone produced only a handful of log lines).

### Timezone consistency (item 12)
Directly sampled every active-pipeline table's live timestamp columns simultaneously and compared against the wall clock: `plc_data.time`, `energy_kpi_maximum_demand`/`energy_kpi_daily_summary`, `baseline_context_summary.computed_at`, and `anomalies.first_detected`/`last_seen`/`last_bucket_start` are **all** the same local, space-separated `%Y-%m-%d %H:%M:%S` format, consistent with the system clock at the moment of sampling. No mismatch found anywhere in the active pipeline - the documented UTC/ISO-T mismatch remains confined to the tables this pipeline was always designed to never touch (`simulator_fault_commands`, audit logs).

### Daily boundary test (item 13)
The soak window did not cross midnight, so this was verified deterministically against existing test coverage instead of the system clock (never changed): `tests/test_anomaly_engine.py::TestBucketHelpers::test_day_boundary_bucket_is_correct` and `tests/test_baseline_engine.py`'s `MODERN_DATA_BOUNDARY`/window tests - both re-confirmed passing in this session's regression run.

### Historian integrity (item 14)
Row count strictly increasing throughout (8,391,530 → 8,411,436). Last 500 rows confirmed monotonic non-decreasing by timestamp. Enabled tag count unchanged at 625 throughout - no ingestion behavior change. No gaps observed in the timestamp series sampled.

### UI smoke test (item 15)
All 9 requested pages verified via `AppTest` while all four workers were actively running: Live Data, Energy Dashboard, Factory Configuration, Production Context, Anomalies, Energy Opportunities, Service & Maintenance, Ask AI, SCADA Floor Plan - **zero exceptions**, all render in well under a second except Energy Dashboard (6.4s, KPI aggregation) and Ask AI (3.8s, LLM-adjacent setup) - both consistent with their known cost, not new slowdowns. Anomalies page confirmed showing the real live anomaly ("1 matching row(s)" at the time of that check).

### Energy Dashboard validation (item 16)
"Billing-Month Peak So Far" now reads `455.65 kW` (was `Unavailable` before `energy_kpi_worker` existed) - directly confirmed via `AppTest`. Older historical daily summaries (before today) remain unavailable, honestly, since no deterministic backfill mechanism exists (and none was built here, per item 24's scope limit) - documented, not silently hidden.

### Baseline worker real pass (item 17)
A full round-robin cycle through all 170 (plant, target) pairs takes ~170 × 5s ≈ 14 minutes; the ~18-minute soak window covered more than one full rotation. 89 `baseline_context_summary` rows accumulated (some targets produce both a `recent` and a `reference` row once windows separate, most still collapsed to one row given the young history). 0 errors logged. Confidence distribution: 100% `Low` today (single day of real history - expected, matches every prior phase's own prediction, not a regression). CPU impact consistent with the measurement above - no unexpected degradation versus the Phase 8 benchmark (213.3s/84 targets/one plant ≈ 2.54s/target average then; this session's mixed cadence is consistent with that per-target cost).

### Anomaly worker commissioning (item 18)
33 rules × matching targets × 2 plants evaluated every ~60s tick, cycle duration 19-22s (consistent with Phase 9's own 16-18s measurement, slightly higher with more baseline coverage now feeding more `candidate` evaluations). Over the soak window: 6 anomalies opened, 4 resolved (2 remain OPEN), multiple `suppressed` (engineering-alarm co-occurrence), and a steadily growing `candidate` count (27-32 by the end) as more targets reach bootstrap/mature status. 0 errors. Not judged by anomaly count, per your instruction - this count reflects genuinely young history reaching statistical maturity live, not a flood (no cycle exceeded single-digit new/updated/resolved actions).

### Opportunity worker commissioning (item 19)
10 rules × 2 plants evaluated at startup and would tick again at the 900s mark (soak window ended before a second natural tick completed). At its one completed evaluation, the only `energy_relevant` anomaly present (the compressor) was correctly excluded by the non-production gate - 0 opportunities created, 0 errors. This is the expected, correct outcome, not a gap - directly matches item 5/19's "Pipeline operational, downstream output pending sufficient evidence" guidance.

### Soak test (item 20)
~18 minutes of continuous, actively-observed concurrent operation of all four workers against the live system (multiple check-ins throughout, not a single before/after snapshot) - not the requested 1-2 hours; this session cannot claim more than what was actually observed. **All four workers are left running** for you to continue observing, per your own instruction; see "how to stop" below.

### Database row-growth review (item 21)
| Table | Start | End | Why |
|---|---|---|---|
| `plc_data` | 8,391,530 | 8,411,436 | Continuous historian ingestion, unaffected by the new workers. |
| `energy_kpi_maximum_demand` | 0 | 2 | One row per plant per billing period - correctly bounded, not a per-tick log. |
| `energy_kpi_daily_summary` | 0 | 2 | One row per plant per day - correctly bounded. |
| `baseline_context_summary` | 0 | 89 | One row per (plant, instance, target, baseline_type, context_bucket_key) - upserted, not appended, growth reflects genuinely new context buckets being covered for the first time. |
| `anomalies` | 0 | 6 | One row per anomaly *occurrence* (open→resolve→reopen), never per evaluation tick - confirmed event/entity-table growth, not an evaluation log (thousands of ticks occurred; 6 rows resulted). |
| `energy_opportunities` | 0 | 0 | No qualifying, non-gated evidence existed yet - correct. |

### Backups (item 22)
No schema modification occurred or was required during this phase - correctly, since Phase 10.5 is operational-only. No migration was run.

### No Phase 11 functionality (item 23)
Confirmed - no workflow states, approval, before/after baselines, or savings verification were touched or added.

### Unrelated technical debt (item 24)
Not touched: `WT.RO01` metadata inference, pump hydraulic-efficiency simulation, missing schedules/criticality/per-compressor airflow, the legacy timestamp sliver - none of these prevented the active pipeline from operating, so none were addressed, per your explicit scope limit.

### Failure/STOP conditions (item 25)
**None encountered.** No locking/contention, no corruption, no CPU starvation, no memory leak signs, no overlapping baseline runs, no duplicate anomaly/opportunity generation after restart, no timestamp inconsistency, no historian gaps, no crash loops, no Streamlit regression, no architecture requiring redesign.

### Successful commissioning criteria (item 26) - checked against your exact list
- ❌ **All required workers installed** (systemd) - blocked, no sudo this session (see above - the only failing criterion).
- ❌ **All required workers enabled** (systemd) - same blocker.
- ✅ All required workers remain active (as background processes) throughout the soak window.
- ✅ Restart safety verified (all four).
- ✅ Single-instance behavior verified (all four).
- ✅ Historian continues uninterrupted.
- ✅ Energy KPI worker processes normally.
- ✅ Baseline worker completes real passes (more than one full rotation).
- ✅ Anomaly worker evaluates baseline-backed targets (real opens/resolves observed).
- ✅ Opportunity worker evaluates anomaly records (correctly excluded the one candidate, for a real, verified reason).
- ✅ No major SQLite contention.
- ✅ UI remains responsive.
- ✅ No new regression failures.
- ✅ No duplicate lifecycle records.
- ✅ Resource usage acceptable.
- ✅ Status documentation updated (this section).

### Bugs found
None - a genuine, positive result of this soak test (distinct from Phase 9/10's own build-time bugs, both already fixed and re-confirmed clean here).

### Improvement identified, not yet applied
`PYTHONUNBUFFERED=1` should be added to each future systemd unit file (see Logs, item 11) - a unit-file setting, not a code change, included in the install commands below.

### Unresolved issues / operational risk
Only the sudo/systemd installation blocker - once you run the install commands below (identical pattern to Phase 6/8/9's own outstanding workers, now including `PYTHONUNBUFFERED=1`), this commissioning's remaining checklist items would be satisfied by the exact same running processes, just under systemd's supervision instead of a manual background shell.

### To install all four analytics workers as systemd services (with the logging fix from item 11)

```bash
for NAME in energy_kpi_worker baseline_worker anomaly_worker opportunity_worker; do
sudo tee "/etc/systemd/system/${NAME}.service" > /dev/null << EOF
[Unit]
Description=SmartMachineAI $(echo $NAME | sed 's/_/ /g' | sed -r 's/(^|\s)(\w)/\U\2/g') Worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/test/SmartMachineAI
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/test/SmartMachineAI/venv/bin/python -m app.${NAME}
Restart=always
RestartSec=5
User=test

[Install]
WantedBy=multi-user.target
EOF
done

sudo systemctl daemon-reload
sudo systemctl enable --now energy_kpi_worker.service baseline_worker.service anomaly_worker.service opportunity_worker.service
sudo systemctl status energy_kpi_worker.service baseline_worker.service anomaly_worker.service opportunity_worker.service --no-pager
```

**Before running the above**, stop the four manually-started background processes this session left running (or let systemd's own instance conflict with the `fcntl` lock file harmlessly refuse to double-start once both are up - safer to stop the manual ones first):

```bash
kill 256137 256140 256193 256196   # energy_kpi_worker, baseline_worker, anomaly_worker, opportunity_worker (manual PIDs from this session)
```

*(Superseded by Phase 10.6 below - the actual deployment used prepared unit files from `deploy/systemd/` rather than this generic heredoc, for exact-provenance traceability.)*

---

# Phase 10.6 — Production Service Deployment

**Completed:** 2026-08-16. **Result: PASS.**

### Services installed
| Service | Source unit path | Deployed to |
|---|---|---|
| `energy_kpi_worker.service` | `deploy/systemd/energy_kpi_worker.service` | `/etc/systemd/system/energy_kpi_worker.service` |
| `baseline_worker.service` | `deploy/systemd/baseline_worker.service` | `/etc/systemd/system/baseline_worker.service` |
| `anomaly_worker.service` | `deploy/systemd/anomaly_worker.service` | `/etc/systemd/system/anomaly_worker.service` |
| `opportunity_worker.service` | `deploy/systemd/opportunity_worker.service` | `/etc/systemd/system/opportunity_worker.service` |

Installed and enabled by you (this session has no sudo) via the exact commands handed off earlier in this phase. Unit files were built from the real conventions found in the project's existing `plc_logger.service`/`event_monitor.service`/`streamlit.service` (inspected directly, not guessed): `User=test`, `WorkingDirectory=/home/test/SmartMachineAI`, `ExecStart=/home/test/SmartMachineAI/venv/bin/python -m app.<worker>`, `Restart=always`, `RestartSec=5`, `Environment=PYTHONUNBUFFERED=1` (added deliberately per Phase 10.5's own log-buffering finding).

### Verified configuration (via `systemctl show`, not assumed from the unit file text)
All four: `User=test`, `WorkingDirectory=/home/test/SmartMachineAI`, `ExecStart` pointing at `/home/test/SmartMachineAI/venv/bin/python -m app.<worker>`, `Environment=PYTHONUNBUFFERED=1`, `Restart=always`, `RestartUSec=5s`, `UnitFileState=enabled`.

### ACTIVE / ENABLED status
```
energy_kpi_worker.service     active / enabled
baseline_worker.service       active / enabled
anomaly_worker.service        active / enabled
opportunity_worker.service    active / enabled
```

### Journal verification
`journalctl -u <service> -n 50` for all four: startup lines appear in real time (confirms `PYTHONUNBUFFERED=1` actually works under systemd, unlike the Phase 10.5 manual-process buffering finding), correct target/rule counts logged (`"170 (plant, target) pairs"`, `"33 rules registered"`, `"10 opportunity rules registered"`), zero tracebacks, zero database errors, zero SQLite locking errors, cycles logged and progressing (`anomaly_worker` completed multiple `"cycle: ..."` entries with real `opened`/`updated`/`suppressed`/`candidate` counts; `opportunity_worker`'s very first cycle correctly showed `{'updated': 2, ...}` - it found and updated, not duplicated, the two opportunities Phase 10.5 had already created).

### Single-instance verification
Exactly one process per worker confirmed via `pgrep -af` (immediately after installation, and again post-restart). The Phase 10.5 manual background processes were cleanly stopped before installation (confirmed via `ps`, zero remaining) - no manual/systemd process pair ever coexisted. A live duplicate-start attempt (`python3 -u -m app.baseline_worker` while the systemd-managed instance was running) correctly printed *"Another baseline_worker instance already holds .../baseline_worker.lock - refusing to start a second one. Exiting."* - the `fcntl.flock()` mechanism works identically whether the first instance is systemd- or manually-managed, exactly as designed.

### Restart verification (a real `sudo systemctl restart`, not a process kill)
You executed `sudo systemctl restart energy_kpi_worker.service baseline_worker.service anomaly_worker.service opportunity_worker.service` directly. Verified afterward:
- All four returned to `active` immediately.
- All four PIDs changed (258908→261655, 258937→261658, 258974→261657, 259013→261659) - proving a genuine restart, not a no-op.
- Journal shows a clean `Stopped ... / Started ...` pair for each, zero errors.
- Exactly one process per worker, confirmed again post-restart.
- **No duplicate DB records of any kind** - direct `GROUP BY ... HAVING COUNT(*)>1` queries against `baseline_context_summary` (composite key), `anomalies` (OPEN conditions), and `energy_opportunities` (NEW conditions) all returned empty, before and after.
- **The two real opportunities Phase 10.5 produced (`P01.UTILITY.CHL02` chiller, `P01.WATER.WSP01` water-supply-pump) survived the restart intact** - same row `id`s (2 and 3), still `status=NEW`, still `saving_basis=unavailable` exactly as required, `observed_excess_cost` correctly *grew* (RM22.53→RM36.85, RM2.06→RM2.33 by the end of verification) because their underlying anomalies remained OPEN and kept accumulating real excess - the cumulative-recompute-from-linked-ids logic working correctly under real continuous production operation, not just in tests.
- Historian uninterrupted throughout (`plc_data` kept growing, latest timestamp stayed current).
- Zero SQLite contention/errors.

### Short post-restart observation
~90 seconds of direct observation after the restart: `plc_data` +2,402 rows, `anomaly_worker` logged two further real cycles with real activity (`{'updated': 7, ...}`, `{'updated': 3, ...}`), `baseline_worker` resumed its rotation, zero errors in the journal since the restart timestamp. Per your instruction, this was a bounded check, not a repeat of the full soak.

### Database continuity (full session, install → restart → post-restart)
| Table | Phase 10.5 end | Post-install | Post-restart | Final |
|---|---|---|---|---|
| `plc_data` | 8,411,436 | 8,441,163 | 8,479,242 | 8,484,580 |
| `baseline_context_summary` | 89 | 216 | 343 | 343 |
| `anomalies` | 6 | 21 | 28 | 28 |
| `energy_opportunities` | 0 | 2 | 6 | 6 |

No destructive initialization occurred at any point - `baseline_context_summary` grew via upsert (duplicate-key query empty throughout), `anomalies`/`energy_opportunities` grew as genuine new event-table rows (open→resolve→recur lifecycle), never as an evaluation log.

### Regression result
372 tests (identical count to the Phase 10.5 benchmark - Phase 10.6 added no code, only deployment artifacts, so no new tests were expected or added), same 5 pre-existing, unrelated failures (`test_equipment_knowledge`/`test_hybrid_router`/`test_pipeline`), **0 new regressions**. Run while all four systemd-supervised workers were actively cycling concurrently - a second, independent concurrency confirmation beyond Phase 10.5's own.

### Important end-to-end evidence (carried forward from late Phase 10.5, now confirmed durable across a real systemd deployment and restart)
> The complete analytics chain subsequently generated two genuine energy opportunity records autonomously during continued live operation. Both retained `saving_basis=unavailable` because no defensible verified-savings basis was yet available. This is expected behavior and must not be converted into an estimated or fabricated saving value.

By the end of this phase, the count had grown organically to 6 real opportunities (both plants represented), every one still correctly `saving_basis=unavailable` - the qualification/dedup/restart-safety logic held under real, continuous, systemd-supervised production conditions, not just under test or manual-process conditions. This distinction (observed excess is real and growing; potential saving remains honestly undetermined) is exactly the boundary the future Savings Verification phase needs to respect.

### Remaining issues
None found during this phase. Reboot-time startup was **not** tested (deliberately, per your instruction 8 - `enabled` status is the establishing criterion for this phase; an actual reboot test remains available separately if desired). `database/actual/config.db` still needs all eight schema-bearing migrators before real deployment - unchanged, unrelated to this phase.

### Verification performed (Phase 10.6)
1. `systemctl is-active`/`is-enabled` for all four - all `active`/`enabled`.
2. `systemctl show` for `User`/`WorkingDirectory`/`ExecStart`/`Environment`/`Restart`/`RestartUSec`/`UnitFileState` - all correct.
3. `systemctl status`/`journalctl -n 50` for all four - zero errors, real-time log visibility confirmed.
4. Single-instance protection re-verified live (duplicate-start attempt correctly refused).
5. Full database continuity/duplicate-key checks before and after a real `sudo systemctl restart`.
6. The two Phase-10.5-era real opportunities confirmed to survive the restart intact, by row `id`.
7. Full regression suite (372 tests) run concurrently with all four workers active - 0 new regressions.
8. `git status`/`git diff --stat` confirms only `deploy/systemd/*.service` (new) and this status document were touched - no analytics logic was modified, per your explicit instruction.

---

# Phase 11 — Savings Verification

## Architecture decision

Approved design (scoping only, no code): `energy_opportunities → savings_interventions → savings_verification_results`, with one required adjustment to the original proposal - **intervention records and verification-result history are separated into two tables, not one.** `savings_interventions` answers "what action did the engineer take?" (a single row per action, updated in place as its coarse lifecycle advances). `savings_verification_results` answers "what did the verification engine conclude at a particular evaluation?" (append-only - one row per evaluation attempt, never updated, so an earlier evaluation's financial evidence can never be destroyed by a later one).

**Non-overlapping vocabulary, by design**: `savings_interventions.status` (`PLANNED`/`IMPLEMENTED`/`VERIFICATION_PENDING`/`VERIFICATION_IN_PROGRESS`/`COMPLETED`) never contains a verification outcome; `savings_verification_results.result` (`VERIFIED`/`REJECTED`/`INCONCLUSIVE`) never contains a process-phase word. `COMPLETED` is deliberately neutral - it means "the verification process has concluded," not "it succeeded"; the actual outcome is only ever found by reading the latest `savings_verification_results` row for that intervention. This directly resolves the "avoid contradictory duplicate state" requirement - each vocabulary has exactly one owner.

---

## Phase 11.1 — Savings Verification Domain Model + Schema

**Completed:** 2026-08-16.

### Scope discipline
Implemented ONLY schema, migration, vocabulary/registry constants, and plain CRUD domain helpers. **No calculation, no reference-window comparison, no verification engine, no worker, no service, no UI, no automatic status progression.** Confirmed by source inspection (a dedicated test scans `engine/savings_verification_domain.py` for any write to `energy_opportunities`/`anomalies` - none exist) and by the fact that both new tables are verified empty on the live database immediately after migration.

### Files created
- `engine/savings_verification_targets.py` - vocabulary constants (intervention status, verification result, confidence tier) and the action-category registry.
- `engine/savings_verification_migrator.py` - the schema migration (same backup+additive+idempotent pattern as every prior migrator).
- `engine/savings_verification_domain.py` - plain data-access helpers (`create_intervention`, `mark_implemented`, `update_intervention_status`, `find_active_intervention`, `get_intervention`, `list_interventions`, `record_verification_result`, `get_latest_verification_result`, `list_verification_history`) plus a read-only debug CLI. No calculation logic anywhere in this file.
- `tests/test_savings_verification.py` (34 tests).

### Schema added

```sql
CREATE TABLE savings_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES energy_opportunities(id),
    plant_id INTEGER NOT NULL REFERENCES plants(id),
    equipment_id INTEGER REFERENCES equipment(id),
    instance_key TEXT NOT NULL,
    action_category TEXT NOT NULL,
    action_description TEXT NOT NULL,
    expected_effect TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    implemented_at TEXT,
    stabilization_days INTEGER,
    status TEXT NOT NULL DEFAULT 'PLANNED',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE savings_verification_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intervention_id INTEGER NOT NULL REFERENCES savings_interventions(id),
    evaluated_at TEXT NOT NULL,
    reference_period_start TEXT, reference_period_end TEXT,
    verification_period_start TEXT, verification_period_end TEXT,
    verification_method TEXT,
    result TEXT NOT NULL,
    reason TEXT,
    verified_energy_kwh REAL, verified_cost REAL, verified_cost_currency TEXT,
    tariff_provenance TEXT,
    confidence TEXT,
    evidence_json TEXT NOT NULL, assumptions_json TEXT NOT NULL, limitations_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Compared to the original Phase 11 proposal's suggested field list for a single combined table, fields describing *what was evaluated and concluded* (`verification_method`, `reference_period_*`, `verification_period_*`, `result`, `confidence`, `evidence_json`, financial figures) moved entirely to `savings_verification_results`; `savings_interventions` kept only fields describing *the action itself*. Nothing was duplicated across both tables.

### Indexes/constraints
- `idx_savings_interventions_one_active` - **partial unique index** on `(opportunity_id) WHERE status IN ('PLANNED','IMPLEMENTED','VERIFICATION_PENDING','VERIFICATION_IN_PROGRESS')` - at most one *active* intervention per opportunity, unlimited terminal (`COMPLETED`) history. Same proven pattern as Phase 9's `anomalies`/Phase 10's `energy_opportunities` partial indexes. Verified directly at the raw-SQL level (bypassing the Python helper) that the constraint itself, not just the wrapper, rejects a duplicate.
- `idx_savings_interventions_lookup` on `(plant_id, status)`.
- `idx_savings_verification_results_lookup` on `(intervention_id, evaluated_at)` - supports "get full history for this intervention" and "get latest evaluation" without a table scan.
- All FKs reference existing tables only (`energy_opportunities`, `plants`, `equipment`, and `savings_verification_results` → `savings_interventions`) - no new tables were required as FK targets.

### Domain/status constants
`INTERVENTION_STATUS_VALUES` = `PLANNED, IMPLEMENTED, VERIFICATION_PENDING, VERIFICATION_IN_PROGRESS, COMPLETED` (`INTERVENTION_TERMINAL_STATUSES = (COMPLETED,)`). `VERIFICATION_RESULT_VALUES` = `VERIFIED, REJECTED, INCONCLUSIVE`. `CONFIDENCE_VALUES` = `STRONG, CONTEXT_MATCHED, LIMITED, INSUFFICIENT` (a tiered classification, never a numeric score - the tiering *logic* itself is Phase 11.3's job; only the vocabulary exists today). A dedicated test confirms the two status vocabularies never intersect.

### Action categories
`CONTROL_ADJUSTMENT`, `SETPOINT_CHANGE`, `SCHEDULE_CHANGE`, `MAINTENANCE`, `REPAIR`, `EQUIPMENT_REPLACEMENT`, `PROCESS_CHANGE`, `OPERATOR_PRACTICE`, `OTHER` (9 total - deliberately small/generic, not "dozens"). Each carries a disclosed, tunable `DEFAULT_STABILIZATION_DAYS` suggestion (1-7 days depending on category) - a judgment call in the same honest `SIMULATION_TUNING`-equivalent spirit as every prior phase's thresholds, not derived from real data (none exists yet), applied only as a starting suggestion by a later phase, never written automatically.

### Migration behavior
Additive (`CREATE TABLE IF NOT EXISTS`), idempotent (re-run 3+ times in testing, zero duplicate schema/errors), automatic timestamped backup by default (`config_before_savings_verification_<timestamp>.db`), zero destructive operations, zero rows inserted. Applied to the live `database/simulation/config.db` with backup `config_before_savings_verification_20260816_100548.db`; both new tables confirmed **empty** immediately after, live `energy_opportunities` (7 rows) and `anomalies` (69 rows, both still growing organically from the running Phase 10.6 pipeline) confirmed **byte-for-byte unchanged** by a direct before/after row comparison in tests.

### Timestamp convention
All new columns use the project's established local, space-separated `%Y-%m-%d %H:%M:%S` format (imported directly from `engine.baseline_engine.TIME_FORMAT`, not redefined) - explicitly **not** `audit_log`'s UTC/ISO-`T` convention, confirmed both by direct code review (`audit_log.timestamp` sample: `'2026-07-26T03:25:16'` vs. this phase's `'2026-08-16 10:02:04'`) and by a dedicated test asserting no `'T'` character appears in any Phase 11.1 timestamp.

### Tests added (34)
Migration (fresh DB, existing DB, idempotency ×3 runs, backup file creation, both tables empty post-migration), FK behavior (intervention→opportunity, result→intervention), vocabulary (non-overlapping status sets, exact expected value sets, terminal-status set), action-category registry (size bounds, `OTHER` fallback, stabilization-days presence, unknown-code handling), intervention creation (starts `PLANNED`, category/description/recorded_by validation, timestamp format), duplicate-active prevention (Python-level and raw-SQL-level), terminal-allows-new-intervention, `mark_implemented` transition + rejection-if-not-`PLANNED`, append-only verification history (the exact 3-evaluation worked example from your spec, `get_latest_verification_result`, invalid result/confidence rejection, **no financial value defaulted to zero**, structural guard that no update/delete helper exists for verification results), and a source-inspection guard confirming this module never writes to `energy_opportunities`/`anomalies`.

### Test results
34/34 new tests pass.

### Regression comparison
406 tests total (372 existing + 34 new - exact expected sum), same 5 pre-existing unrelated failures (`test_equipment_knowledge`/`test_hybrid_router`/`test_pipeline`), **0 errors, 0 new regressions**. Not attempted to fix the 5 pre-existing failures, per your explicit instruction.

### Confirmations
- **Existing Phase 10.6 opportunity/anomaly data preserved**: verified both by direct before/after row comparison in a test and by live inspection immediately after migration.
- **No verification calculation, worker, or UI implemented**: confirmed by scope (files listed above contain no calculation/scoring/window-comparison logic whatsoever) and by a source-inspection test.
- **No financial values fabricated or defaulted to zero**: `verified_energy_kwh`/`verified_cost` remain `NULL` unless a caller explicitly supplies a real value - directly tested for the `INCONCLUSIVE` case.
- **No historical verification records fabricated, no existing opportunity retroactively marked verified**: both new tables are empty on the live database; nothing in this phase writes to `energy_opportunities`.

### Issues / design decisions discovered during implementation
None required a design change from the approved plan. One genuinely new finding, already flagged in the Phase 11 design proposal and reconfirmed here by direct inspection: `audit_log`'s UTC/ISO-`T` timestamp convention is a real, live landmine distinct from every analytics table built since Phase 6 - Phase 11.1's tables deliberately never touch it.

---

## Phase 11.2 — Reference Window + Context-Matching Evidence Engine

**Completed:** 2026-08-16. Deterministic, read-only evidence *selection*, not a financial calculation. No new schema.

### What was reused vs. built new
Audited first, then confirmed by direct code inspection: `engine.baseline_engine.compute_window_baseline(target, historian, config_database_path, window_start, window_end, current_context)` **already accepts an arbitrary window and a fixed context vector** - contrary to the Phase 11 design proposal's assumption that new context-matching/robust-statistics code would be needed, this function does exactly that, unmodified. Reused as-is: context matching (`_match_generic`/`_match_production`), robust median/MAD (`_summarize`), bootstrap/mature classification (`_classify`), fault exclusion (`_abnormal_intervals_from_thresholds`, applied automatically inside `compute_window_baseline` via `_fetch_clean_downsampled`'s default `exclude_faults=True`), the representative-context lookup (`_current_context`), maintenance-window reconstruction (`_maintenance_intervals`), and the entire opportunity→anomaly→baseline-target registry chain (`opportunity_targets.get_rule`, `anomaly_targets.get_rule`, `baseline_targets.discover_targets`). **Zero lines of `engine/baseline_engine.py` were modified.** New code is purely additive orchestration in `engine/savings_verification_evidence.py`: freezing two windows relative to `implemented_at` (Phase 8's own reference/recent split is relative to "now", which doesn't fit a fixed intervention date) and combining both windows' independent results into one evidence-quality verdict.

**Baseline relationship, stated explicitly**: `compute_window_baseline()` is reused for its computational machinery, not its *purpose* - Phase 8's own reference/recent windows (relative to "now") are never consulted; Phase 11.2 always computes two fresh windows anchored to the specific intervention. "Baseline == verified reference" was explicitly rejected as an assumption, per your direction.

### Reference-window algorithm
`compute_reference_window(implemented_at, reference_window_days_max)` → `(implemented_at - reference_window_days_max days, implemented_at - 1 second)`. Frozen by construction - a pure function of `implemented_at` alone, with no dependency on "now" whatsoever (proven by a test asserting two calls at different evaluation times return byte-identical windows). `reference_window_days_max` is reused directly from the target's own existing `BaselineTarget` field - no new constant.

### BEFORE boundary semantics
`end = implemented_at - 1 second`. Historian range queries (`DatabaseManager.get_history_range`) are inclusive on both ends (`time >= start AND time <= end`) - subtracting exactly one second guarantees a sample recorded at the literal implementation instant can never leak into BEFORE, tested directly.

### AFTER/stabilization boundary semantics
`eligible_after_start = implemented_at + stabilization_days` (inclusive - a sample at exactly this instant counts). `after_end = now` at evaluation time (the window legitimately grows as real time passes; each call is still fully deterministic for a *given* `now`). Returns `None` (stabilization not finished) whenever `now < eligible_after_start` - this one condition also correctly and uniformly handles a future `implemented_at` (a data-entry edge case) without any special-casing. `stabilization_days=0` correctly produces a zero-gap boundary with no overlap or double-counting (BEFORE ends strictly before `implemented_at`, AFTER begins at-or-after it).

### Context dimensions supported
Exactly what Phase 8 already tracks per equipment type, reused unmodified - `load_bucket`, `ambient_bucket`, `hour_bucket`, `day_type`, `production_state`, `loaded_state`, and (for production equipment) `product_code`/`product_category`/`running_state`. No new dimension was introduced. Weather/external APIs: explicitly not attempted (no instrumentation exists) - never even considered a candidate.

### Evidence-quality rules
Reuses Phase 8's own bootstrap/mature/Level A-D vocabulary rather than inventing a new numeric score (no percentage anywhere):
- `INSUFFICIENT` - either window is `unavailable` (below Phase 8's own minimum-sample gate).
- `LIMITED` - either window only reaches `bootstrap`, **or** both are `mature` but share zero matched context dimension (each side individually well-sampled is not the same as the two sides being comparable to each other - a real, deliberate refinement beyond a simple per-window quality copy).
- `CONTEXT_MATCHED` - both mature, at least one shared dimension, but not both at context Level A/B.
- `STRONG` - both mature, both Level A/B, and a shared matched dimension (or the target genuinely has no context dimensions to begin with - tested explicitly so an empty-context target is never wrongly penalized for "missing" dimensions it was never meant to have).

### Exclusion rules
- **Fault periods** (threshold breaches): excluded automatically, inherited for free from `compute_window_baseline`.
- **Stabilization gap**: structurally excluded by the frozen boundary math itself (falls in neither window).
- **Future/post-intervention data leaking into BEFORE**: structurally impossible by construction (tested).
- **Maintenance windows**: reused `_maintenance_intervals()` to compute an overlap *count*, reported in `exclusion_summary`/`limitations` - **not surgically subtracted from the sample counts**, because `compute_window_baseline()`'s public return doesn't expose the individual matched bucket timestamps needed to filter post-hoc without duplicating its internal fetch logic. Disclosed as a known limitation (see item Q) rather than silently ignored or worked around with new duplicate code.
- **Statistical anomalies (Phase 9)**: deliberately **never** used to exclude data, per your explicit direction - reported as an overlap count only. An anomaly's existence is not grounds for exclusion; the entire point of an intervention is often to correct exactly the condition an anomaly flagged.
- **Missing/invalid/sparse data**: falls out naturally as a low sample count, driving `INSUFFICIENT` - no separate special-casing needed.

### Domain result structure (NOT persisted anywhere)
A plain dict returned by `compute_verification_evidence()`: `intervention_id`, `target_key`/`instance_key`/`equipment_type`, `reference_start`/`reference_end`, `eligible_after_start`/`after_end`, `reference_sample_count`/`after_sample_count`, `reference_status`/`after_status`/`reference_level`/`after_level`, `matching_dimensions_used`, `exclusion_summary` (maintenance/anomaly overlap counts), `evidence_quality`, `assumptions`, `limitations`, a human-readable `reason` string, `computed_at`. No new table was added - per your instruction to justify persistence before adding schema, and given this phase produces no financial claim to make durable yet, computation-on-demand was judged sufficient; Phase 11.3 will decide whether *its own* output needs different treatment.

### Tests added (37, `tests/test_savings_verification_evidence.py`)
Frozen reference boundary (including determinism across repeated evaluation times), no post-intervention leakage, stabilization exclusion (data inside the gap never counted), eligible-AFTER boundary (exact-instant inclusion, one-second-before exclusion, `stabilization_days=0` zero-gap correctness, unusually long stabilization, future `implemented_at`), evidence-quality tiering (all four tiers, isolated from real context noise), missing-context degradation, insufficient evidence (zero BEFORE, zero AFTER, only-one-side-has-data), exclusion-count reporting for both maintenance and anomaly overlap (with an explicit test proving anomaly-overlapping samples are never excluded, only counted), target resolution (correct + unresolvable-opportunity cases), determinism (identical repeated calls), **no database mutation**, **no `savings_verification_results` row ever inserted**, **intervention status never auto-advanced**, and source-inspection guards confirming no financial calculation and no calls to `record_verification_result`/`update_intervention_status` anywhere in the module.

### Regression result
406 existing tests (Phase 11.1 baseline) + 37 new = 443 total expected. See Verification below for the confirmed run.

### Explicit statement
**No verified savings, potential savings, ROI, payback, or avoided cost were calculated anywhere in Phase 11.2.** `compute_verification_evidence()`'s return value contains no financial field at all - confirmed by a dedicated test scanning the serialized result for `verified_energy`/`verified_cost`/`saving`/`roi`/`payback`/`avoided_cost`. No `savings_verification_results` row was written by any code path, live or test. No intervention's `status` was advanced beyond what a human explicitly set via Phase 11.1's `mark_implemented()`.

---

## Phase 11.3 — Verified Energy + Cost Savings Calculation Engine

**Completed:** 2026-08-16. First phase permitted to compute a financial verification claim - gated hard on evidence quality, never on the mere existence of a numerical difference.

### Audit summary
Reused directly: `engine.savings_verification_evidence`'s window/context-matching (Phase 11.2, entirely unmodified), `engine.baseline_engine`'s `_summarize` (robust median/MAD), `_classify` (bootstrap/mature), `_distinct_days`, `_maintenance_intervals`, `_fetch_target_series`, `_current_context`, `_empty_window_result`; `engine.energy_kpi_engine.effective_tariff()` for every tariff lookup (no second tariff calculator). Deliberately **not** reused: `engine.anomaly_engine.bucket_excess_energy_cost()` - it clamps to `max(delta, 0)`, correct for Phase 9's "excess is never negative" semantics but wrong here, where a negative delta (the intervention made things worse) must be preserved exactly, never clamped.

### Baseline timestamp exposure change (item 2)
The smallest possible additive change to `engine/baseline_engine.py`'s `compute_window_baseline()`: a new `matched_bucket_starts` key (the already-locally-computed `matched_buckets` list, just not previously returned) added to both `_empty_window_result()` (`[]`) and the success-path return dict (`sorted(matched_buckets)`). **Every pre-existing key, value, and type is unchanged** - proven by 5 new dedicated regression tests in `tests/test_baseline_engine.py` (field presence/consistency, empty-result correctness, and an explicit re-run of `compute_baseline()` - Phase 8's own public entry point and the only other real caller - showing byte-identical behavior to before the change). Full `test_baseline_engine.py` suite (32 tests, 27 original + 5 new) passes unchanged.

### Maintenance exclusion (items 2/12)
`_exclude_maintenance_contaminated()` filters a window's `matched_bucket_starts` against `_maintenance_intervals()` (reused, unmodified). The surviving bucket subset is then **reclassified from scratch** via `_recompute_window_after_exclusion()` - reusing `_summarize`/`_classify`/`_distinct_days` exactly as the original computation did, so a maintenance-thinned window's `status`/`confidence`/`median` are genuinely recomputed, not guessed or downgraded by a fixed heuristic. `level` and `diversity_dimensions` are reused from the original (pre-exclusion) result rather than re-derived - documented explicitly as a deliberate simplification (re-deriving them would require re-fetching the full context-by-bucket map a second time for no material benefit, since removing a handful of contaminated buckets doesn't change *which* context dimensions were achievable, only how many samples survive within them). The evidence-quality verdict is then **recomputed** via Phase 11.2's own `classify_evidence_quality()` on the adjusted windows - if it drops below `CONTEXT_MATCHED`, the result is forced to `INCONCLUSIVE` even if the pre-exclusion evidence was strong. Exclusion counts are always reported in `exclusion_summary`, never hidden.

### Normalization (item 4)
**`context_matched_power_median`** - both BEFORE and AFTER values are a robust median **power** (kW) computed only across Phase 8's own context-matched buckets (same load/ambient/hour/production-state/product bucket, as applicable). This sidesteps "different operating duration" pitfalls entirely, since kW is duration-independent by construction - no separate "energy per operating hour/batch/quantity" normalization was needed or built. An energy-per-batch normalization was considered and explicitly rejected for this initial implementation (see Known Limitations) rather than invented without validating production-quantity data completeness.

### Energy saving formula (item 5)
Per surviving AFTER bucket: `kw_delta = before_median - actual_kw_at_bucket`; `kwh_delta = kw_delta * (aggregation_minutes / 60)`; summed across every surviving AFTER bucket → `absolute_energy_saving_kwh`. **Never clamped to zero** - directly tested with a scenario where AFTER consumes *more* than BEFORE, confirming a real negative value is returned and the result is `REJECTED`, not hidden or zeroed.

### Percentage saving formula (item 5)
`(before_median - after_median) / before_median * 100`, using both windows' post-exclusion medians (a robust, duration-independent "typical % change" figure) - a **distinct, complementary** number from the absolute kWh figure (a real integrated total), both exposed separately. Below `MIN_POWER_DENOMINATOR_KW` (0.5 kW), the percentage is reported `None` with a stated reason - never a meaningless/huge ratio from a near-zero denominator; the absolute kWh figure remains valid regardless (it doesn't depend on division).

### Cost/tariff calculation (item 6)
Per priced AFTER bucket, `effective_tariff()` (reused, Phase 6) is looked up for that bucket's own date, summed only across buckets that actually had a configured rate. If **zero** buckets had a tariff, cost is `None`/Unavailable while energy remains valid (directly tested). If **some** buckets lacked a tariff, the partial total is still reported with an explicit "partial, not complete" limitation - mirroring Phase 7's own `cost_for_period()` precedent for disclosed partial totals, not a new pattern.

### Evidence-quality gating (item 9)
Exactly your recommended rule, applied **twice** - once on Phase 11.2's original evidence, once again after maintenance exclusion: `STRONG`/`CONTEXT_MATCHED` → eligible for `VERIFIED`/`REJECTED`; `LIMITED`/`INSUFFICIENT` → `INCONCLUSIVE`, unconditionally, regardless of what a raw calculation might otherwise show. No weakening of this rule was needed or applied.

### VERIFIED/REJECTED/INCONCLUSIVE semantics (item 8)
`VERIFIED`: evidence gate passed **and** `absolute_energy_saving_kwh > 0`. `REJECTED`: evidence gate passed **and** the value is `<= 0` (zero is explicitly `REJECTED`, not `VERIFIED` - directly tested) - always retained and explained, never presented as a software failure. `INCONCLUSIVE`: evidence gate failed, either originally (Phase 11.2) or after maintenance exclusion - covers insufficient/incomparable evidence, unfinished stabilization, and (implicitly) any state where a defensible calculation basis doesn't exist.

### Result/domain structure (item 10)
A plain dict from `calculate_verified_savings()`: `intervention_id`, `evaluated_at`, both windows, `evidence_quality` (original) and `evidence_quality_after_exclusion`, `matching_dimensions_used`, `exclusion_summary` (four counts: pre-existing maintenance/anomaly overlap from Phase 11.2, plus the two new maintenance-*excluded* counts), `usable_reference_sample_count`/`usable_after_sample_count`, `normalization_method`/`before_basis`/`after_basis`/`basis_unit`, `absolute_energy_saving_kwh`/`energy_saving_percentage`/`energy_saving_basis_bucket_count`, `cost_saving`/`cost_currency`/`tariff_provenance`, `estimated_saving`/`realization_ratio` (comparison-only), `verification_result`, `assumptions`, `limitations`, `reason`. No opaque score, no AI-generated confidence percentage anywhere.

### Persistence (item 11)
Strictly separated: `calculate_verified_savings()` is pure and read-only (proven by a dedicated no-mutation test checking row counts across five tables before/after). `persist_verification_result()` is a distinct, always-explicit second call, appending one row to `savings_verification_results` via Phase 11.1's existing `record_verification_result()` (already append-only by design - no schema change needed). Never updates a prior row (directly tested: two persisted evaluations for the same intervention produce two separate, independently-readable rows). Never calls `update_intervention_status()` - an intervention's coarse lifecycle state is left entirely to a future phase to advance, exactly as instructed. **Phase 11.1's schema required zero changes** to represent every Phase 11.3 result field - `evidence_json` absorbs the structured metadata (normalization, basis values, sample counts, exclusion counts, realization ratio) that doesn't have its own dedicated column.

### Estimated-vs-verified separation (item 7)
`compute_realization_ratio(estimated, verified)` is a pure comparison function (`None` whenever the estimate is missing, `None`, or `<= 0`), and `attach_estimated_comparison()` only *reads* `energy_opportunities.estimated_potential_saving_period` (still always `None` today, per Phase 10) - directly tested that no `energy_opportunities` field is ever mutated by either function or by persistence.

### Tests added (37 new: 32 in `tests/test_savings_verification_engine.py` + 5 baseline-engine regression tests)
Positive/negative/zero saving (with exact-formula verification), zero/tiny denominator handling, all four evidence-quality tiers (including a scenario using the REAL registry-resolved chiller target, not a mock, showing genuine context-matching failure correctly gates to `INCONCLUSIVE`), missing/available tariff, maintenance exclusion (partial - still reaches a result; total - forces `INCONCLUSIVE`, explicitly distinguished from never having had enough evidence to begin with), anomaly presence retained/reported/never-excluding, determinism, no-mutation, append-only persistence history, no status auto-advancement, no opportunity-estimate mutation, realization-ratio edge cases, and source-inspection guards (no writes to `energy_opportunities`/`anomalies`/`baseline_context_summary`, no `update_intervention_status` calls, no use of the clamping excess-cost helper).

### Full regression result
See Verification below for the confirmed complete-suite run.

### Live read-only dry-run result (item 17)
Real historian data, `P01.UTILITY.CHL02`, hypothetical `implemented_at` 6 hours before evaluation time, **no fake intervention created, nothing persisted**: reference window reached `mature`/Level A (15 samples, 5 distinct days, matched on `load_bucket`/`ambient_bucket`/`hour_bucket`); the 6-hour AFTER window correctly reached only `bootstrap` (17 samples, but only 1 distinct day) → `evidence_quality=LIMITED` → **`INCONCLUSIVE`**, exactly as expected given the live system's genuinely young post-Phase-10.6 history. Zero declared maintenance windows exist for this instance. No thresholds were adjusted to force a different outcome.

### Known limitations
- Energy-per-batch/production-quantity normalization was considered and **not built** - `context_matched_power_median` was judged sufficient (and safer) for this initial implementation, since Phase 8's context-matching already provides equivalent "comparable conditions" control for production equipment via product/category/running-state matching, without needing to validate `production_batches` quantity-field completeness across arbitrary verification windows.
- `level`/`diversity_dimensions` are reused (not re-derived) during post-exclusion reclassification - documented as a deliberate, disclosed simplification, not an oversight.
- Real live verification remains `INCONCLUSIVE`/blocked on genuinely young history, same honest situation as every prior phase's live state - this is the correct behavior, not a gap.

---

## Phase 11.4 — Savings Verification Worker + Intervention Lifecycle Orchestration

**Status:** implementation, tests, and regression complete. **Systemd deployment pending your installation** (this session has no sudo) - see below.

### Reused components
`engine.savings_verification_evidence.compute_verification_evidence()`/`compute_after_window()` (Phase 11.2, unmodified), `engine.savings_verification_engine.calculate_verified_savings()`/`persist_verification_result()` (Phase 11.3, unmodified), `engine.savings_verification_domain`'s full CRUD API (Phase 11.1, unmodified - `get_intervention`, `update_intervention_status`, `get_latest_verification_result`, `list_verification_history`, `list_interventions`), and the exact `fcntl.flock()`/systemd unit pattern proven by all four Phase 10.6 workers. No savings mathematics were redesigned - orchestration only.

### Architecture
New `engine/savings_verification_orchestration.py` (business logic: eligibility, materially-new-evidence gating, lifecycle transitions) + new `app/savings_verification_worker.py` (thin entry point: lock, loop, call `run_cycle()`, log, sleep - contains zero calculation logic, confirmed by a source-inspection test).

### Eligibility rules
An intervention is a candidate if `status != COMPLETED` and `implemented_at IS NOT NULL` (excludes `PLANNED` and terminal interventions entirely - never even queried further). Among candidates, true eligibility is determined by reusing `ev.compute_after_window()` unchanged - stabilization-not-finished and future-`implemented_at` are both naturally handled by the same single check (`now < eligible_after_start`), never a second copy of that arithmetic.

### Stabilization behavior
Identical to Phase 11.2's own boundary (`implemented_at + stabilization_days`, inclusive) - the worker never redefines or duplicates this.

### Materially-new-evidence definition
Compares the freshly-computed Phase 11.2 evidence's raw `after_sample_count`/`reference_sample_count`/`evidence_quality` against the most recently PERSISTED result's stored metadata. New evidence = a strictly larger AFTER sample count, OR a changed reference sample count, OR a changed evidence-quality tier. Never worker run count or elapsed clock time alone.

**Real bug found and fixed during Phase 11.4's own performance measurement** (not merely written, caught by testing): when `calculate_verified_savings()` is gated at its *first* evidence-quality check (before maintenance-exclusion recomputation ever runs), it correctly leaves `usable_reference_sample_count`/`usable_after_sample_count` as `None` in the persisted row - that stage genuinely never executed. The comparison logic was treating `None` as `0`, making every subsequent cycle look like materially new evidence even with byte-identical data - exactly the repeated-duplicate-INCONCLUSIVE-row flood this phase was explicitly required to prevent. **Fixed entirely within the orchestration layer** (Phase 11.3's engine/domain files were not touched): `evaluate_intervention()` now enriches the result dict with Phase 11.2's own raw sample counts before persistence, whenever the calculation-engine counts are `None` - a benign, well-justified addition that also makes the persisted record itself more informative. A dedicated regression test reproduces the exact scenario and confirms the fix (first evaluation persists, an immediate re-evaluation with unchanged data correctly skips, history stays at 1 row).

### Retry policy
First eligible evaluation always runs. `INCONCLUSIVE` retries only when materially new evidence exists (never automatically, never on a timer). `VERIFIED`/`REJECTED` are terminal for automatic verification - the intervention transitions to `COMPLETED` and is filtered out of every future candidate list entirely.

### Duplicate suppression / idempotency
Directly enforced by the materially-new-evidence gate - tested explicitly (run once → persists; immediate rerun with unchanged evidence → no new row; genuinely new AFTER data → a new row is allowed).

### Single-instance / concurrency protection
The exact same `fcntl.flock()` pattern as every other worker (`logs/savings_verification_worker.lock`) - no new locking mechanism. Verified with a **real subprocess-level test**: one worker process started, a second genuine `python -m app.savings_verification_worker` invocation attempted while the first holds the lock, confirmed to print the standard refusal message and exit - not merely unit-tested, actually spawned as a real OS process.

### Failure isolation
Each candidate intervention is processed inside `run_cycle()`'s own try/except - one intervention's exception is logged (with `intervention_id`/`opportunity_id`/`instance_key`/processing stage) and the cycle continues to the next. `evaluate_intervention()` wraps the calculation and persistence calls separately, re-raising with a stage-tagged message (`"calculation stage failed for..."` / `"persistence stage failed for..."`) so failures are diagnosable without guessing which step broke. A software failure is never persisted as an `INCONCLUSIVE` engineering result - directly tested (a failed calculation produces zero rows in `savings_verification_results`).

### Intervention lifecycle transition matrix
```
PLANNED                    -> never touched (implemented_at is None)
IMPLEMENTED                -> VERIFICATION_PENDING   (first sight, unconditional, evidence-independent)
VERIFICATION_PENDING       -> VERIFICATION_IN_PROGRESS (only once stabilization has elapsed AND a real evaluation begins)
VERIFICATION_IN_PROGRESS   -> (stays) on INCONCLUSIVE  (never terminal - item 4)
VERIFICATION_IN_PROGRESS   -> COMPLETED  on VERIFIED or REJECTED, ONLY after persistence succeeds
COMPLETED                  -> terminal; filtered out before any evaluation is attempted
```
Directly tested: `PLANNED` never jumps to `COMPLETED`; a future `implemented_at` never enters verification; `INCONCLUSIVE` never marks `COMPLETED` and the intervention remains eligible for a later real evaluation; `VERIFIED`/`REJECTED` both correctly transition to `COMPLETED`; a failed persistence call leaves the lifecycle unchanged (never falsely `COMPLETED`); a `COMPLETED` intervention processed again does nothing.

### Result persistence ordering
`calculate → persist_verification_result() (append-only, reused unmodified from Phase 11.3) → lifecycle transition` - strictly in that order, enforced by plain Python control flow (an exception in either of the first two steps means the transition line is structurally never reached, not merely "usually" skipped).

### Worker cadence
`TICK_INTERVAL_SECONDS = 3600` (1 hour), grounded in real measurement: a full cycle across 20 seeded interventions took **1.10s** for a genuine first-time evaluation pass (all 20 persisted) and **0.36-0.39s** for a pure re-check pass correctly finding no new evidence (all 20 skipped) - both orders of magnitude below the 1-hour tick, chosen deliberately conservative (matching the instruction's own suggestion) since verification evidence matures over days, not minutes, and cadence is explicitly not what controls persistence (the materially-new-evidence gate does).

### Systemd unit
`deploy/systemd/savings_verification_worker.service` - `User=test`, `WorkingDirectory=/home/test/SmartMachineAI`, `Environment=PYTHONUNBUFFERED=1`, `ExecStart=/home/test/SmartMachineAI/venv/bin/python -m app.savings_verification_worker`, `Restart=always`, `RestartSec=5` - byte-for-byte the same convention as the four Phase 10.6 services, only the module name differs.

### Tests added (26: 25 new + 1 regression for the bug above)
Eligibility (zero interventions, `PLANNED` not eligible, future `implemented_at` not eligible, stabilization unfinished, terminal `COMPLETED` skipped), first evaluation + full lifecycle transition sequence, `INCONCLUSIVE`-not-terminal (persists but doesn't complete, remains re-evaluable, append-only history across re-evaluation), materially-new-evidence (immediate rerun no duplicate, new AFTER bucket allows retry, increased coverage allows retry, the early-gate bug's regression case), failure isolation (one intervention's failure doesn't stop another, failed calculation produces no fake row, failed persistence doesn't mark `COMPLETED`), append-only history / deterministic ordering / no mutation of `energy_opportunities`/`anomalies`, a real subprocess-level single-instance lock test, and source-inspection guards (no LLM/PLC imports, no direct writes to other tables, no calculation logic in the app layer).

### Full regression result
506 tests (480 existing + 26 new, exact expected sum), same 5 pre-existing unrelated failures, **0 new regressions**.

### Live validation
`savings_interventions`: 0 rows. `savings_verification_results`: 0 rows. Confirmed directly, not assumed - correctly reflects that no engineer has recorded a real intervention yet (Phase 11.1-11.3 never generate one automatically, by design). A direct `run_cycle()` call against the live database returned `eligible_interventions=0, evaluated=0, persisted=0` in ~2ms - exactly the "successful commissioning outcome" your own spec describes for this state, not a defect.

### Existing pipeline health
All 8 processes confirmed `active` immediately before this section was written: `plc_logger`, `event_monitor`, `streamlit`, `production_simulator`, `energy_kpi_worker`, `baseline_worker`, `anomaly_worker`, `opportunity_worker`. Historian confirmed still growing (8,728,276 rows, latest timestamp current). No fake intervention or demonstration data was ever inserted into the live database.

### Known limitations / decisions for Phase 11.5
- The materially-new-evidence comparison uses raw (pre-maintenance-exclusion) counts as its primary signal when post-exclusion counts aren't yet available - a disclosed, deliberate simplification (see the bug writeup above), not a defect.
- `level`/`diversity_dimensions` reuse (Phase 11.3's own disclosed simplification) is unchanged and unaffected by this phase.
- No UI was built, per explicit scope - Phase 11.5's job.

---

## Phase 11.5 — Engineer Intervention & Savings Verification UI

**Status:** implementation, tests, full regression, and live validation complete. Streamlit page files were modified after `streamlit.service` last started - a service restart is needed to see the new page/nav in the running app (command below).

### Reused components
Every read/write path delegates to existing, already-accepted engines - no domain, evidence, calculation, or worker logic was reimplemented in Streamlit: `engine.savings_verification_domain.create_intervention()`/`mark_implemented()` (Phase 11.1, the UI's ONLY two write calls), `dom.get_intervention()`/`find_active_intervention()`/`get_latest_verification_result()`/`list_verification_history()` (Phase 11.1, reads), `engine.savings_verification_evidence.compute_after_window()` (Phase 11.2, reused unchanged for stabilization-eligibility display - no second stabilization formula), `engine.savings_verification_targets`'s vocabulary constants (`ACTION_CATEGORY_VALUES`, `DEFAULT_STABILIZATION_DAYS`, status/result/confidence constants - never redefined). Source-inspection tests confirm zero calls to `effective_tariff`, `compute_window_baseline`, `calculate_verified_savings`, `_summarize`, `_classify`, or `update_intervention_status` anywhere in the three new/modified UI files.

### Architecture
New `ui/savings_verification_data.py` (presentation-layer data-access module, mirrors `ui/data_access.py`'s established convention: reads query directly, writes delegate entirely to the Phase 11.1 domain API). New `ui/pages/18_Savings_Verification.py` (the verification-tracking page: KPI cards, filterable overview table, intervention detail with stabilization/financial-separation/append-only verification history). Modified `ui/pages/17_Energy_Opportunities.py` (added a `Record Intervention` form alongside the existing `Dismiss` form, gated by an active-intervention check so an opportunity with an intervention already recorded shows its status instead of a second form). Modified `ui/Home.py` (added the new page to `st.navigation` and an overview bullet).

### Workflow
Opportunity → (engineer clicks into an opportunity's detail on the Energy Opportunities page) → `Record Intervention` form, pre-filled with the opportunity's equipment/plant context (never re-entered) → on submit, `record_and_implement_intervention()` calls `dom.create_intervention()` then `dom.mark_implemented()` in one step → the opportunity's detail view immediately shows "Intervention already recorded" instead of the form (duplicate-submission protection, backed by the Phase 11.1 partial unique index, and pre-checked via `find_active_intervention()` before the form even renders) → the Savings Verification page shows the intervention's lifecycle status, and once Phase 11.4's worker has run, its evidence-driven outcome.

### Form fields and validation
Only real Phase 11.1 schema fields are collected: action category (from `ACTION_CATEGORY_VALUES`), action description (required, non-empty after `.strip()`), expected effect (optional), implementation date/time (defaults to now, editable, rejected if in the future), stabilization period (defaults to the action category's `DEFAULT_STABILIZATION_DAYS`, editable, `min_value=0`), recorded by (defaults to the logged-in user's display name, required). All validation errors render as `st.error()` messages, never a raw traceback - confirmed by a dedicated test that submits with an empty description and checks `at.exception` is empty.

### Lifecycle/status presentation
`engineer_status_label(status, latest_outcome)` implements the exact mapping table specified: `PLANNED`→Planned, `IMPLEMENTED`→Implemented, `VERIFICATION_PENDING`→"Stabilizing / Awaiting Verification", `VERIFICATION_IN_PROGRESS`→"Verification In Progress" (or "More Evidence Required" when the latest result is `INCONCLUSIVE`), `COMPLETED`+`VERIFIED`→Verified, `COMPLETED`+`REJECTED`→"Not Verified". This is presentation-only - the underlying backend status/result columns are never renamed or reinterpreted, and the UI never sets `VERIFICATION_PENDING`/`VERIFICATION_IN_PROGRESS`/`COMPLETED` itself (those transitions are exclusively Phase 11.4's worker's job, confirmed both by code (only `create_intervention`/`mark_implemented` are called) and by a source-inspection test banning any `update_intervention_status(` call in the UI files).

### Stabilization presentation
`get_stabilization_info()` reuses `ev.compute_after_window()` verbatim to determine eligibility and remaining days - no second, UI-owned stabilization formula. The detail view shows implemented-at, stabilization-days, and eligible-after-start explicitly, plus an `st.info()` "Stabilizing - approximately N day(s) remaining" message while ineligible.

### Verification history presentation
Append-only, rendered chronologically (oldest first, matching `list_verification_history()`'s own `ORDER BY evaluated_at ASC`), each evaluation in its own expander (the most recent expanded by default) - a terminal `VERIFIED`/`REJECTED` result never hides earlier `INCONCLUSIVE` evaluations. No verification mathematics are recalculated in Streamlit - every figure shown (`verified_energy_kwh`, `verified_cost`, percentage, evidence-quality tier) is read directly from the persisted `savings_verification_results` row.

### INCONCLUSIVE / VERIFIED / REJECTED presentation
`INCONCLUSIVE` renders as `st.warning()` with explicit "does not mean the intervention failed" language and a note that the system automatically re-evaluates on new evidence, without promising eventual verification. `VERIFIED` renders as `st.success()` and shows Estimated Savings (opportunity-level, from the linked `energy_opportunities` row) and Verified Saving (measured, this period only) as two separately labeled fields - never merged, never annualized (the backend does not compute an annualized verified figure, so none is shown or invented in the UI). `REJECTED` renders as `st.error()` with explicit "not a software failure" language and is never auto-deleted, hidden, or reopened.

### Evidence-quality presentation
`EVIDENCE_QUALITY_EXPLANATIONS` gives a one-line plain-English caption for each of the four real backend enum values (`STRONG`/`CONTEXT_MATCHED`/`LIMITED`/`INSUFFICIENT`) - no new scoring system, no invented percentage confidence.

### KPI cards and filters
Six cards: Active Interventions, Awaiting Verification, Verified, More Evidence Required, Rejected, Verified Savings (the direct sum of each `VERIFIED` intervention's own measured-period cost - never mixed with opportunity-level Estimated Potential Saving). Three filters (Lifecycle status, Plant, Latest verification outcome) - all cleanly supported by existing columns, no new query complexity.

### Empty-state behavior
With zero interventions (the real, current live-database state), the page shows a professional `st.info()` message ("No interventions have been recorded yet... Review an Energy Opportunity and select **Record Intervention**...") and stops before rendering the table/filters. No demo or fake intervention data was ever inserted anywhere, including during testing (all UI tests run against a throwaway temp-directory database via `CONFIG_DATABASE_PATH` monkeypatching).

### Read/write safety
Confirmed by source-inspection tests: no `systemctl` call anywhere in the UI (worker visibility is read-only from DB state), and the only write calls in the entire UI layer are `dom.create_intervention()` and `dom.mark_implemented()` - no `update_intervention_status(` call, and no "Force Verification"/"Mark Savings Verified"/"Accept Savings" action exists anywhere.

### Tests added (31, all passing)
`TestStatusLabels` (8, pure function, including a check that the exact backend constant strings are accepted), `TestStabilizationInfo` (4, pure function, reusing `ev.compute_after_window()`), `TestSavingsVerificationPage` (9, `AppTest`-based: empty state, KPI cards, lifecycle-status rendering, `INCONCLUSIVE`/`VERIFIED`/`REJECTED` rendering, chronological multi-evaluation history, evidence-quality captions, filters), `TestOpportunityInterventionIntegration` (11, `AppTest`-based: existing page functionality preserved, form visible/hidden by role, a real end-to-end submission creating a real intervention row, empty-description rejection with no traceback, stabilization widget's `min=0`, duplicate-submission protection, and three source-inspection guards for force-verify/systemctl/duplicated math).

### Full regression result
537 tests (506 existing + 31 new, exact expected sum), same 5 pre-existing unrelated failures (`test_compressor_aliases`, `test_typo_tolerant_aliases`, `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline` - all pre-existing equipment-alias/typo-tolerance issues, unrelated to this phase), **0 new regressions**.

### Live validation
Both pages loaded directly against the real live `database/simulation/config.db` (not a test double) via `AppTest`: the Savings Verification page rendered its empty state cleanly (`savings_interventions` is genuinely 0 rows) with `at.exception` empty; the Energy Opportunities page rendered its 1 real dataframe row plus the `Record Intervention` form with `at.exception` empty. Confirmed directly before and after both loads: `savings_interventions` and `savings_verification_results` both remained at 0 rows - page load creates nothing.

### Existing pipeline health
All 9 processes confirmed `active` immediately before this section was written: `plc_logger`, `production_simulator`, `event_monitor`, `energy_kpi_worker`, `baseline_worker`, `anomaly_worker`, `opportunity_worker`, `savings_verification_worker`, `streamlit`. Historian confirmed still growing (6,998,412 rows, latest timestamp current). `journalctl` for `streamlit.service` over the last 15 minutes showed no new SQLite errors or tracebacks.

### Action needed from you
`streamlit.service` started (2026-08-15 11:30) before these UI files were last modified (2026-08-16 12:40-12:41) - Streamlit's own hot-reload only affects an actively open browser tab, not a headless systemd service's page/nav registry. Restart it to see the new "Savings Verification" page in the sidebar and the updated Energy Opportunities page:
```bash
sudo systemctl restart streamlit.service
```

### Known limitations / decisions for later phases
- No manual "force verify"/approve/override action exists anywhere, per explicit scope - any such capability needs its own future governance phase.
- No annualized verified-savings figure is shown, because the backend does not compute one - showing one would require new backend calculation logic, out of scope here.
- Executive dashboards, reports, PDF certificates, notifications, approval workflows, multi-user permissions, digital signatures, and any PLC/control writes were explicitly not built, per the phase's "DO NOT IMPLEMENT YET" list.

---

## Phase 11.6 — Savings Verification Completion & Acceptance

**Status:** implementation, tests, full regression, and live validation complete. **Phase 11 declared COMPLETE** (see verdict below). Streamlit files modified after the service's last restart - one more restart is needed (command below).

### Scope
This phase invented no new savings mathematics - every engine/domain/worker file from Phase 11.1-11.4 is untouched. Work was UI terminology/clarity, traceability, one real UI gap fix (limitations disclosure was never rendered), a defensible dashboard-scope audit, and a dedicated end-to-end acceptance test suite proving the complete chain (Opportunity → Intervention → Stabilization → Evidence → Calculation → Worker → History → Engineer-Facing Result) behaves correctly together, not just as individually-tested components.

### UI terminology improvements
- **"Unavailable" → "Not yet estimable"** for Estimated Potential Saving specifically (table and detail view, `ui/pages/17_Energy_Opportunities.py`'s new `_fmt_potential_saving()`), plus the Monthly/Annual Potential Saving caption - communicates "no defensible calculation basis exists yet," never "the software failed." The stored value is unchanged (still `NULL`, never defaulted to 0) - only the label changed.
- **Observed Estimated Excess Cost** caption strengthened to explicitly state it is a historical, already-occurred fact, NOT a potential/recoverable/guaranteed/verified saving (item 7).
- **Confidence** metric now carries a `help=` tooltip clarifying it measures evidence quality of the finding, independent of whether a dollar saving could be estimated (reusing `engine/opportunity_engine.py`'s own real `compute_confidence()` semantics - no new scoring system, no fabricated percentage).
- **Record Intervention** form caption now explicitly states the action must be logged AFTER it was actually implemented, and that the system records the action, never performs/schedules/executes it (items 10-11).

### "Not yet estimable" / distinguishing types of "Unavailable" (items 3-5)
Verified Saving on the Savings Verification page/table is no longer a single generic "Unavailable" - `ui/savings_verification_data.py`'s new `verified_saving_summary()` derives one of four distinct, honest presentations from already-known backend state, never guessing a reason the backend doesn't provide:
- `VERIFIED` with a real cost → the actual figure.
- `VERIFIED` with `verified_cost IS NULL` (no tariff for that period) → **"Tariff unavailable"**, distinct from a generic gap.
- `REJECTED` → **"Not applicable"** (a real evaluation concluded, just not a positive claim).
- `INCONCLUSIVE` or no evaluation yet → **"Waiting for verification"**.

### Verified Savings zero-state handling (item 6)
The KPI card no longer shows `RM 0.00 (0 verified)` when zero interventions are verified - it shows **"No verified interventions yet"**, with no dollar figure at all, so it can never be misread as "the system verified savings were zero." When verified interventions genuinely exist and sum to a real figure (including a real `RM 0.00`), the existing branch renders that real total unchanged - a genuine zero-sum verified result is still presented as a real result, per the spec's explicit distinction.

### Financial terminology rules (item 2) - re-confirmed, unchanged
Observed Estimated Excess Cost, Estimated Potential Saving, and Verified Saving remain three permanently separate concepts, enforced by a new source-inspection test (`TestOpportunityVsVerifiedSeparation`) banning any `"Total Savings"` label and any single line summing two-or-more of `observed_excess_cost`/`estimated_potential_saving_*`/`verified_cost` together anywhere in the two UI pages, the UI data-access module, or the Phase 11.1/11.3 engine files.

### Traceability improvements (item 9)
- Energy Opportunities' "Intervention already recorded" notice now also shows the action category, giving more at-a-glance context without a second record.
- Savings Verification's intervention detail view gained a **"Related Opportunity"** expander (category, status, Observed Estimated Excess Cost) - a human-readable summary, not a duplicated copy of the opportunity record; raw IDs remain out of every primary label.

### Real UI gap found and fixed: limitations disclosure
Auditing item 19 (maintenance exclusion) and item 18C (partial tariff coverage) against the live UI revealed that `savings_verification_results.limitations_json` - which is exactly where Phase 11.3 discloses both "partial tariff coverage" and "maintenance-contaminated buckets excluded" - was **never rendered anywhere in the Savings Verification page**, despite being computed and persisted correctly the whole time. Fixed by adding a "Disclosed limitations" expander to each verification-history evaluation, reading `limitations_json` verbatim (no recalculation, no paraphrasing). Covered by `TestTariffAcceptance.test_c_partial_tariff_coverage_disclosed_not_hidden` and `TestMaintenanceExclusionAcceptance`.

### Dashboard scope audit (item 23)
Per the Master Roadmap's four proposed dashboard items, audited each against what the backend can defensibly support today:
- **Verified Savings This Month / This Year — IMPLEMENTED.** `svd.get_verified_savings_by_period()` buckets already-final `verified_cost` values by each result's own `verification_period_end` (the real date the saving was observed) - never `evaluated_at` (when the worker happened to run, which has no engineering meaning). This is pure aggregation of already-computed numbers, not new verification mathematics, and defensible because `verification_period_end` already exists on every persisted row.
- **Open Opportunities — IMPLEMENTED**, reusing the existing `get_opportunities(status="NEW")` count exactly as the spec permitted ("may reuse existing opportunity counts if meaningful").
- **Projected Annual Savings — EXPLICITLY DEFERRED.** The backend has no defensible annualization method for VERIFIED savings (Phase 10's opportunity-level annualization is a separate, non-comparable estimate). A visible caption states this on the page itself, so the deferral is transparent rather than a silent omission.

### No double-counting of verified savings (item 24)
By construction, `get_summary()` and `get_verified_savings_by_period()` both read only each intervention's LATEST verification result (`dom.get_latest_verification_result()`, `ORDER BY evaluated_at DESC, id DESC LIMIT 1`) - earlier `INCONCLUSIVE` rows in an intervention's append-only history are structurally excluded from every aggregate, never sub-totaled in. Under Phase 11.4's real lifecycle, a `VERIFIED` result is always terminal (`COMPLETED`, filtered from all future evaluation), so a second `VERIFIED` row for the same intervention cannot occur through the real worker path at all. `TestNoDoubleCounting` additionally proves the aggregation function is safe even against an adversarial append-only history with three `VERIFIED`-looking rows for one intervention (RM 1,000 × 3, constructed directly via the domain API, not achievable through the real worker) - the aggregate is confirmed to be exactly RM 1,000, never RM 3,000.

### End-to-end acceptance tests added (14, all passing, `tests/test_savings_verification_e2e.py`)
All tests drive the REAL domain API, REAL orchestration/worker path, and REAL UI data-access layer against an isolated throwaway test database (never the live DB - item 26):
- **VERIFIED** (item 13): full chain confirmed - lifecycle reaches `COMPLETED`, verified energy/cost retained, history append-only (1 row), opportunity's own estimate unchanged, UI status "Verified", UI's `verified_saving_summary()` shows the real figure, `get_summary()` sums it correctly.
- **REJECTED** (item 14): negative saving survives unclamped, lifecycle reaches `COMPLETED`, UI status "Not Verified", UI shows "Not applicable" (no "failure" language), excluded from the Verified Savings KPI (not summed as zero).
- **INCONCLUSIVE + re-evaluation** (item 15): lifecycle stays `VERIFICATION_IN_PROGRESS` (never falsely `COMPLETED`), UI status "More Evidence Required", UI shows "Waiting for verification", no verified-saving amount claimed; then materially new evidence is added and a genuine re-evaluation reaches `VERIFIED`, with both evaluations preserved.
- **Append-only history** (item 16): INCONCLUSIVE → INCONCLUSIVE → VERIFIED, all 3 rows preserved, chronological, never overwritten, UI history read matches exactly, aggregate uses only the terminal VERIFIED row.
- **Duplicate suppression** (item 17): 3 repeated worker cycles with unchanged evidence produce exactly 1 evaluation row and 1 verified saving; a separate test confirms a genuinely new-evidence retry on a still-open `INCONCLUSIVE` intervention correctly produces a second row.
- **Tariff acceptance** (item 18, A/B/C): (A) tariff available → real cost; (B) tariff unavailable → energy still `VERIFIED`, cost stays `None` (never defaulted to 0), UI shows "Tariff unavailable" and the summary's new `verified_without_cost_count` correctly flags it as excluded from the cost KPI; (C) tariff effective only partway through the AFTER window → a real partial cost figure plus a disclosed "partial total" limitation, confirmed to round-trip verbatim through the UI's `get_verification_history()`.
- **Maintenance exclusion through the full workflow** (item 19): light contamination (1 of 25 AFTER days) still reaches a real `VERIFIED`/`REJECTED` result with the exclusion disclosed in `evidence_json`; total contamination (every AFTER day) correctly downgrades to `INCONCLUSIVE`, lifecycle stays non-terminal, UI shows "More Evidence Required."
- **No control actions** (item 11): source-inspection guard confirming zero references to PLC drivers, `write_tag`, `set_setpoint`, `reset_alarm`, start/stop, or maintenance-command issuance anywhere in the three UI files.
- **UI-reads-backend-authoritative**: re-confirmed by the existing Phase 11.5 `test_no_verification_mathematics_duplicated_in_ui` guard (unchanged, still passing) plus this phase's own separation test.

### Full regression result
551 tests (537 existing + 14 new, exact expected sum), same 5 pre-existing unrelated failures (`test_compressor_aliases`, `test_typo_tolerant_aliases`, `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`), **0 new regressions**.

### Live validation
Both pages loaded directly against the real live `database/simulation/config.db` via `AppTest`, `at.exception` empty on both: the Savings Verification page rendered its empty state (unchanged verbatim text, per item 22's explicit "preserve" instruction), all 9 KPI/dashboard metrics rendered correctly ("Verified Savings" → "No verified interventions yet", "Open Opportunities" → 7, matching the live count), and the Energy Opportunities page's real 7-row table showed "Not yet estimable" for every row's Estimated Potential Saving (all currently unavailable, matching the exact behavior the live screenshot described) while Observed Estimated Excess Cost remained a real RM figure on every row. Confirmed directly before and after both loads: `savings_interventions` and `savings_verification_results` both remained at 0 rows - page load creates nothing.

### Service health
All 9 processes confirmed `active`: `plc_logger`, `production_simulator`, `event_monitor`, `energy_kpi_worker`, `baseline_worker`, `anomaly_worker`, `opportunity_worker`, `savings_verification_worker`, `streamlit`. Historian confirmed still growing (7,019,244 rows, latest timestamp current). `journalctl` for `savings_verification_worker.service` and `streamlit.service` over the last 20 minutes showed no new SQLite errors or tracebacks.

### Action needed from you
`streamlit.service` was last restarted 2026-08-16 13:08, before this phase's UI file edits (13:22-13:26) - restart it again to pick up the new terminology/dashboard cards:
```bash
sudo systemctl restart streamlit.service
```

### Remaining Phase 11 limitations (unchanged, disclosed, not defects)
- No annualized verified-savings figure anywhere (opportunity-level annualization remains a separate, non-comparable estimate) - Projected Annual Savings stays explicitly deferred until the backend provides a defensible method.
- No manual force-verify/approve/override action exists, by design - would need its own governance phase.
- Root-cause LLM grounding-fidelity limitations (documented since Phase 7) are unrelated to and unaffected by Phase 11.
- Executive dashboards, PDF certificates, notifications, approval workflows, multi-user permissions, digital signatures, and any PLC/control writes remain explicitly out of scope.

### Phase 11 completion criteria - checked against the spec's own list
Intervention domain works; engineer action can be recorded; stabilization semantics work; evidence selection works; maintenance contamination handled (light and total); before/after verification works; negative savings retained (never clamped); tariff conversion works when available and is honestly absent when not (never defaulted to 0); insufficient evidence stays `INCONCLUSIVE`; re-evaluation works with materially new evidence; duplicate result suppression works; append-only audit history works; verification worker `ACTIVE`+`ENABLED` (confirmed via `systemctl`); engineer UI works; estimated vs. verified values remain distinct (never summed); unavailable values are honestly communicated ("Not yet estimable" / "Tariff unavailable" / "Not applicable" / "Waiting for verification", never a bare "Unavailable" where a real reason is known); verified-saving aggregation does not double-count (proven both by real lifecycle structure and by an adversarial unit test); no UI-side verification arithmetic (source-inspection guarded); no fabricated live data (confirmed 0/0 before and after every live validation step); full regression has 0 new failures; historian/services remain healthy. **Every criterion is met.**

### PHASE 11 — SAVINGS VERIFICATION: **COMPLETE**

---

## Phase 12.1 — Equipment Health Domain Model, Evidence Registry & Deterministic Scoring Engine

**Status:** domain model, registry, evidence assembly, and calculation engine complete, tested, and live-validated (read-only). No schema/migration added (not needed - see Persistence below). No worker, no UI - deliberately deferred to Phase 12.2+ per your own preferred split.

### Reused systems (audited first, per Rule 1)
`engine.anomaly_engine._equipment_id_for_instance()` / `find_open_anomaly()` / `count_prior_occurrences()` (Phase 9, unmodified - the exact same equipment-resolution and real-RESOLVED-row recurrence counting), `engine.anomaly_targets.get_rule()` (Phase 9's rule registry, used to map a health factor's `source_rule_key` to its `baseline_target_key` - never a duplicated mapping), `engine.baseline_targets.EQUIPMENT_TYPE_PROFILES`/`PRODUCTION_PROCESS_AREA_CODES`/`_instances_for_area_code()` (Phase 8, unmodified - equipment-instance discovery reused verbatim), `engine.energy_kpi_engine.is_stale()` (Phase 6, unmodified - reused for two new system-wide feed-freshness gates), the `anomalies`/`baseline_context_summary`/`maintenance_log`/`equipment`/`machine_events` tables (all read-only, zero writes). No anomaly detection, baseline computation, maintenance scheduling, or alarm logic was reimplemented anywhere - Phase 12.1 introduces no new raw measurement and no new instrumentation.

### Health philosophy (item 3) - Score and Confidence are separate outputs
**Health Score** answers "how much engineering attention does available evidence suggest?" **Assessment Confidence/Coverage** answers "how complete and trustworthy is this assessment?" These are never blended: `HealthResult.health_score`/`health_band` and `HealthResult.assessment_confidence`/`coverage_status` are independent fields. The single most important guard, directly tested: **zero evidence does NOT produce Health Score 100** - it produces `health_score = None` ("Unavailable") with `coverage_status = "INSUFFICIENT"`.

### Health bands (item 4) - centralized in `engine/health_targets.py`
```
85-100  HEALTHY
70-84   MONITOR
50-69   ATTENTION
0-49    INVESTIGATE
```
Half-open floating boundaries (`score >= low`, checked highest-to-lowest), not integer buckets - a real bug was caught during live validation where a genuine float score (69.7) fell in the gap between integer bounds 69 and 70 before this fix. No CRITICAL/FAILURE IMMINENT/ABOUT TO FAIL language anywhere, per your explicit instruction.

### Score availability gate (item 19)
`COVERAGE_INSUFFICIENT` (usable/applicable factor ratio < 0.34, or capped down by weak statistical confidence) → `health_score = None`, never a misleading number. `COVERAGE_LOW` → a numeric score is shown but `provisional = True`. `COVERAGE_MEDIUM`/`COVERAGE_HIGH` → a normal, non-provisional numeric score. The ratio-based tier is additionally capped down by the WORST statistical confidence among the anomaly-sourced factors actually used - a numerically-complete-looking assessment built entirely on Low-confidence baselines can never be reported as HIGH.

### Health factor registry (`engine/health_targets.py`) - item 6
A `HealthFactorDefinition` dataclass (factor_id, equipment_type, family, source_type, source_rule_key, max_penalty, description, provenance) in one central `HEALTH_FACTOR_REGISTRY` dict, mirroring Phase 8/9/10's proven curated-registry pattern exactly. Every numeric weight (severity base penalties, confidence multipliers, recency decay, recurrence step/cap, maintenance/events penalty shapes, family caps, coverage ratio thresholds) lives in this ONE file - nothing scattered. `health_model_version = "1.0"` for future traceability (item 24).

### Supported equipment types and factor families (items 7, 20)
8 equipment types (matching Phase 8's own profiles, minus `plant_energy`/`air_header` which are explicitly out of scope per item 30 - not single equipment assets): `air_compressor`, `chiller`, `chilled_water_pump`, `water_supply_pump`, `ahu`, `cold_room`, `production_process`, `filling`. Every factor maps to a REAL, already-baselined-and-detected Phase 8/9 signal:

| Equipment | CONDITION | PERFORMANCE | ELECTRICAL_LOAD | PROCESS | MAINTENANCE | EVENTS |
|---|---|---|---|---|---|---|
| air_compressor | *(none available)* | - | power | - | ✓ | ✓ |
| chiller | - | COP | power | cooling output, supply/return temp, waterflow (4) | ✓ | ✓ |
| chilled/water_supply_pump | vibration, bearing temp, flow/kW drift | - | power | flow, delta-P | ✓ | ✓ |
| ahu | filter DP drift | - | fan power | supply air temp | ✓ | ✓ |
| cold_room | *(none available)* | - | compressor power | room temp | ✓ | ✓ |
| production_process | vibration drift | - | motor power | process temp | ✓ | ✓ |
| filling | *(none available)* | - | power | *(none available)* | ✓ | ✓ |

**Honestly disclosed gaps, not overlooked**: `air_compressor`, `cold_room`, and `filling` have no CONDITION-category anomaly rule today (no vibration/discharge-temp anomaly is baselined for compressors; no condition rule for cold rooms; filling only has a Power_kW rule) - a missing CONDITION factor for these types shows up honestly via `missing_factors`, never fabricated or substituted with an unrelated signal.

### Factor weights/max penalties and provenance (items 6, 31)
Every factor's `provenance = "SIMULATION_TUNING"` - identical vocabulary to Phase 9's own `threshold_provenance` (`USER_CONFIGURED`/`ENGINEERING_RULE`/`MANUFACTURER_REFERENCE`/`STATISTICAL`/`SIMULATION_TUNING`), so this is never presented as a manufacturer limit. Central constants: `SEVERITY_BASE_PENALTY` (INFORMATION=2, ATTENTION=6, WARNING=12, HIGH=20), `CONFIDENCE_MULTIPLIER` (High=1.0, Medium=0.75, Low=0.4 - a confidence not in this table is excluded from evidence entirely, per item 13), `RECENCY_DECAY_DAYS=30`, `RECURRENCE_STEP=0.06`/`RECURRENCE_MAX_MULTIPLIER=1.6`, `MAINTENANCE_OVERDUE_BASE_PENALTY=3.0`/`PER_DAY=0.15`, `EVENTS_PENALTY_PER_QUALIFYING_EVENT=2.0`.

### Correlated-evidence / double-count prevention (item 9) - family caps
`FAMILY_MAX_PENALTY`: CONDITION=30, PERFORMANCE=18, PROCESS=15, ELECTRICAL_LOAD=12, EVENTS=12, MAINTENANCE=8 (sums to 95, deliberately leaving a floor of 5). **A real design gap was found and fixed during testing**: the family cap initially only reduced the aggregate score, while individual `FactorResult.contribution` values still showed their pre-cap (uncapped) numbers - meaning "sum of displayed contributions" would NOT match the actual score deduction the moment a cap engaged, violating item 5's "the final score must be reconstructable from its components." Fixed in `engine/health_engine.py`'s `_apply_family_caps()`: when a family's raw total exceeds its cap, every penalized factor in that family is now scaled down **proportionally** so displayed contributions always sum exactly to the real score impact. Directly tested (`TestDoubleCountingPrevention`): three water-supply-pump CONDITION factors that would raw-sum to 42 (15+15+12) are confirmed capped to exactly 30, with each factor's own contribution reduced proportionally rather than the cap being invisible.

### Recency (item 10)
An OPEN anomaly always contributes its full (capped) penalty. A RESOLVED anomaly's residual contribution decays **linearly** from 1.0 (just resolved) to 0.0 at 30 days - long-resolved findings stop mattering to the current score without being erased from history. Directly tested at all three states (open / recently-resolved / long-resolved).

### Recurrence (item 11)
Uses `anomaly_engine.count_prior_occurrences()`'s real count of RESOLVED rows for the exact same rule+instance+target - never a bare mutable counter. Strengthens a factor's contribution by up to 60% (`RECURRENCE_MAX_MULTIPLIER=1.6`), always still bounded by the factor's own `max_penalty` and its family cap - recurrence sharpens attention, it never grows a penalty unbounded.

### Alarm/event integration (item 14)
Counts qualifying `machine_events` alarm/warning rows for THIS equipment's own tags only (matched by real tag prefix, e.g. `P01.WATER.WSP01.%` - never the unreliable/legacy `machine_events.equipment` bucket label), within a 30-day lookback, gated by a system-wide feed-freshness check. **A real bug was found and fixed during live validation**: `event_monitor` re-logs a new row roughly every polling cycle a tag REMAINS outside its threshold - a raw row count treated one enduring condition as hundreds of separate incidents (a live pump showed 1,670 "events" in 30 days from just 12 genuinely distinct tag/condition combinations), saturating this factor for nearly every piece of equipment and making it useless for prioritization. Fixed by counting **DISTINCT (tag, condition) pairs** instead of raw rows - directly mirroring the project's own established fix for the identical noise pattern (`ai/prompt_builder.py`'s `_condense_evidence()`, documented in CLAUDE.md). Regression-tested (`TestEventsFactorCountsDistinctConditions`): 50 repeated rows of the same condition now correctly count as 1, not 50.

### Maintenance-status integration (item 15)
Mirrors `ui/pages/4_Service_and_Maintenance.py`'s own `_next_due()` formula exactly (explicit `next_due_at`, else `last_serviced_at + service_interval_days`) - reproduced as a small function in `engine/health_evidence.py` rather than imported from a UI page (wrong dependency direction for an `engine/` module). Overdue maintenance contributes a modest, capped penalty (family cap 8) with reason text explicitly stating "a risk/attention factor, not evidence of physical degradation" - never presented as mechanical evidence. Missing schedule data (no `next_due_at` and no `last_serviced_at`+`service_interval_days`) is reported as a `missing` factor, never fabricated as "overdue" or silently treated as "clean."

### Criticality handling (item 16)
`HealthResult.criticality` is populated (or `None`, matching Phase 10's own established `_equipment_criticality()` precedent - never fabricated) purely as an **informational** field. It is read exactly once, after the score is already computed, and never appears in any penalty/coverage formula. Directly tested: identical evidence with vs. without a configured `criticality` value produces byte-identical `health_score`.

### Missing/stale data handling (items 17, 36) - two system-wide freshness gates
Distinct from an equipment's own "nothing recent for me" (legitimate good evidence): a **pipeline-freshness** check runs once per calculation pass. **A real design flaw was found and fixed during testing**: the first version checked `MAX(anomalies.last_seen)` for anomaly-feed freshness - but a genuinely healthy equipment population can have ZERO anomaly rows forever (nothing ever crossed a threshold), which is indistinguishable from "the anomaly worker never ran" if `anomalies` were the only signal. Fixed to check `MAX(baseline_context_summary.computed_at)` instead - baselines are written on every `baseline_worker` cycle **unconditionally**, regardless of whether an anomaly is ever found, making it the correct liveness proxy. The events-feed check uses `MAX(machine_events.event_time)` (a genuinely event-driven feed, appropriately checked directly), thresholded at 72 hours. When a feed is stale, the affected factors become `missing` (not `clean`, not penalized) and a limitation is disclosed - data quality never masquerades as either machine deterioration or machine health.

### Coverage/confidence methodology (item 18)
`applicable_factor_count` = every factor the registry defines for this equipment type, regardless of data availability. `usable_factor_count` = factors that could actually be evaluated (status != `missing`). `missing_factors` lists exactly which ones couldn't (e.g. `"4 of 6 applicable factors currently have usable evidence"` is directly reconstructable from these two counts). Coverage tier = `min(ratio-based tier, worst-used-statistical-confidence tier)` - never an opaque AI-style percentage.

### Known metric exclusions (item 32, carried forward)
The known-invalid pump hydraulic-efficiency relationship (Phase 8's own documented exclusion) was NOT reintroduced - confirmed by a dedicated source-inspection test. No per-compressor airflow, no true discharge-temperature anomaly rule for compressors, and the young historian/baseline-history issue (confirmed live: 18 of 34 live equipment instances currently show `LOW`/provisional confidence) are all pre-existing, disclosed characteristics of this dev-stage system - none were worked around or hidden by Phase 12.1.

### Persistence decision (item 25)
**No new schema, no migration.** The engine is CALCULATE-only (mirrors Phase 11.3's `engine/savings_verification_engine.py` precedent exactly) - every result is computed on demand from existing `anomalies`/`baseline_context_summary`/`maintenance_log`/`equipment`/`machine_events` data and returned as an in-memory `HealthResult`, never written anywhere. A worker + history/snapshot table is explicitly Phase 12.2's job, only if actually needed then.

### No prediction, no root cause, no LLM, no control writes (items 26-29) - confirmed
No Remaining Useful Life, failure probability, predicted failure date, or ML classifier output is computed anywhere. No named mechanical fault (bearing failure, impeller damage, refrigerant shortage, etc.) is ever produced - factor `reason` strings state evidence only (e.g. "High severity from an open anomaly..."), confirmed by a runtime test asserting the actual generated reason text never contains such phrases. Zero LLM calls anywhere (source-inspection guarded). Zero PLC/control-write calls anywhere (source-inspection guarded: no `driver_factory`, `write_tag`, `set_setpoint`, `reset_alarm`, `systemctl`, etc.).

### Tests added (38, all passing, `tests/test_health_engine.py`)
Registry sanity (band boundary coverage including the exact float-gap bug found, source-inspection guards for known-invalid metrics/LLM/PLC/table-writes); Case A (healthy, substantial evidence → high score); the critical "zero evidence ≠ 100" guard; Case B (persistent condition + recurrence → lower score, no root-cause claim in the actual generated reason text); Case C (an energy-only anomaly on an unregistered rule_key structurally cannot affect health, plus a registry-shape guard that no power/load rule is ever classified CONDITION); Case D (insufficient data → `None`, never 100); Case E (maintenance-overdue-only → moderate, explicitly non-physical-evidence wording); double-counting prevention (family cap actually engages and factor contributions are proportionally scaled); recency (open/recent/long-resolved); recurrence (increases up to a cap, never unbounded); confidence gating (Low < High contribution; Insufficient excluded entirely); the baseline-maturity distinction between "clean" and "missing"; maintenance missing-vs-overdue-vs-on-schedule; criticality never affecting score; P01/P02 and equipment-instance isolation; determinism (repeated calls, unchanged data, identical results); score bounds (0-100 even under extreme seeded evidence) and the exact reconstructability equation (`health_score == 100 - family-capped penalty sum`); coverage counting; the events-factor distinct-condition regression guard; both freshness gates; and a full before/after snapshot proving `calculate_health()` never writes to any Phase 8-11 table.

### Full regression result
589 tests (551 existing + 38 new, exact expected sum), same 5 pre-existing unrelated failures (`test_compressor_aliases`, `test_typo_tolerant_aliases`, `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`), **0 new regressions**.

### Live read-only validation (real examples, both plants, no fake data ever persisted)
34 eligible equipment instances across P01+P02 (all 8 supported types represented), **all 34 scored successfully, 0 errors**. Representative real results:
- `P01.WATER.WSP01` (water_supply_pump): **60.1 - ATTENTION (provisional, LOW confidence)** - bearing temp condition (-9.9), power load (-10.0), flow process (-8.0, decayed), repeated events (-12.0, capped).
- `P01.WATER.WSP02` (water_supply_pump): **88.0 - HEALTHY (HIGH confidence)** - only the events-family contribution, otherwise clean across all 8 factors.
- `P02.UTILITY.CHWP02` (chilled_water_pump): **75.2 - MONITOR (provisional)** - bearing temp condition (-12.8, recurrence-strengthened, occurrence #24).
- `P01.PROD.DISP01`/`MILL01`/`MIX01` (production_process): **88.0 - HEALTHY (HIGH confidence)** each.
- `P02.COLDROOM.CR01`/`CR02` (cold_room): usable 2-3 of 4 factors (no CONDITION rule exists for this type, honestly disclosed via `missing_factors`).
Band distribution across all 34: 19 HEALTHY, 12 MONITOR, 3 ATTENTION, 0 INVESTIGATE. Confidence distribution: 18 LOW/provisional, 16 HIGH - reported honestly (young baseline history, a pre-existing, disclosed characteristic, not a Phase 12.1 defect).

### False-positive review (item 36)
Directly inspected a live OPEN anomaly's own `evidence_json` (`P01.WATER.WSP01` power deviation): `baseline_level: "A"` (the best available context-match quality) with `context_used: {"load_bucket": "low", "hour_bucket": "12-16"}` - confirming the elevated reading is flagged only relative to OTHER times at the same load bucket and hour, not a raw increase explainable by legitimately higher demand. The health engine inherits Phase 8/9's context-matching for free (it never re-judges a raw value itself) - directly confirmed against real live data, not merely assumed from the architecture.

### Performance
Both plants combined: 34 equipment instances scored in **4.29 seconds** total, zero historian (`plc_data`) queries anywhere in the health engine - only bounded reads against `anomalies`/`baseline_context_summary`/`maintenance_log`/`machine_events` (indexed, low-row-count tables). No repeated million-row rescans.

### Database safety
No migration, no schema change, no historian rewrite - nothing to make safe beyond the read-only guarantee itself (confirmed by a dedicated before/after snapshot test).

### Existing services
All 9 confirmed `active` after this work: `plc_logger`, `production_simulator`, `event_monitor`, `energy_kpi_worker`, `baseline_worker`, `anomaly_worker`, `opportunity_worker`, `savings_verification_worker`, `streamlit`. Historian confirmed still growing. No new service was created in 12.1, per your explicit preference.

### Files created
`engine/health_targets.py`, `engine/health_evidence.py`, `engine/health_engine.py` (includes a read-only debug CLI: `python -m engine.health_engine --plant p01 [--instance P01.WATER.WSP01]`), `tests/test_health_engine.py`.

### Files modified
None (no existing file required a change for Phase 12.1).

### Known limitations / proposed Phase 12.2 scope
- No persistence/history yet - every call recomputes from current evidence; a worker + score-history/snapshot table (only if genuinely needed) is Phase 12.2's job.
- `FACTORY_AI_CURRENT_SYSTEM_REPORT.md` (dated 2026-08-15) predates Phases 8-11 entirely and is now significantly stale (states "Anomaly detection: NOT IMPLEMENTED", "Baselines: NOT IMPLEMENTED", etc.) - noted for awareness, not corrected in this phase (out of scope).
- `air_compressor`/`cold_room`/`filling` have no CONDITION-family factor today - would need a genuinely new Phase 9 anomaly rule (e.g. compressor discharge temperature) to close, which is out of Phase 12.1's scope (no new anomaly detection was to be built here).
- The EVENTS family is currently a weaker discriminator than CONDITION/PERFORMANCE/PROCESS - under current threshold/simulator tuning, most equipment shows 5-12 distinct concurrently-breached warning-level conditions, saturating its family cap for nearly everyone. This is an honest, disclosed characteristic of current threshold tuning (a pre-existing, out-of-scope issue), not a Phase 12.1 defect - the constant was deliberately NOT re-tuned further to chase a "nicer" spread, since that would be the same "adjusting numbers to produce an attractive result" your instructions explicitly ruled out, just applied to my own registry instead of the simulator.

---

## Phase 12.2 — Equipment Health Worker, Persistence & Historical Tracking

**Status:** schema, domain layer, orchestration, historical/trend API, and worker all built, tested, and live-validated (real writes to the live database, backed up first). Full regression clean. **Service not yet installed** - commands below, awaiting your `sudo` execution.

### Phase 12.1 components reused, unmodified
`engine/health_engine.calculate_health()` / `discover_health_targets()` (called as-is - zero edits to `engine/health_targets.py`, `engine/health_evidence.py`, or `engine/health_engine.py`, confirmed by a dedicated source-inspection sentinel test), `HealthResult`/`FactorResult`'s exact field shapes (persisted verbatim, not re-derived), the accepted score/band/confidence/coverage semantics (0-100 score, HEALTHY/MONITOR/ATTENTION/INVESTIGATE bands, HIGH/MEDIUM/LOW/INSUFFICIENT confidence, the INSUFFICIENT→`None` score gate) - all directly re-verified against live data this phase, unchanged.

### Worker pattern reused
Mirrors `engine/savings_verification_orchestration.py` + `app/savings_verification_worker.py`'s exact split (business logic in `engine/`, thin entry point in `app/`) and `app/opportunity_worker.py`'s dynamic multi-plant discovery (`SELECT id, code, name FROM plants` - never hardcoded `p01`/`p02`). Same `fcntl.flock()` single-instance lock, same `Restart=always`/`PYTHONUNBUFFERED=1` systemd convention, same per-item try/except failure isolation, same "worker orchestrates, engine calculates" split every prior Phase 8-11 worker uses.

### Database tables added (`engine/health_migrator.py`)
- **`equipment_health_snapshots`** - APPEND-ONLY, one row per persisted assessment (`health_score` nullable - INSUFFICIENT assessments persist with a real row and `NULL` score, never silently dropped), `health_model_version`, `change_reason`, JSON-encoded `limitations`/`assumptions`/`missing_factors` (small, bounded lists - never AI prose).
- **`equipment_health_factor_snapshots`** - one row per registry factor per snapshot, linked via `health_snapshot_id`, carrying `penalty`/`maximum_penalty`/`evidence_source`/`evidence_value`/`evidence_timestamp`/`evidence_confidence`/`reason`/`provenance` - everything needed to explain a historical score without consulting today's registry (directly tested: a future registry change to `FAMILY_MAX_PENALTY` was simulated and confirmed NOT to alter an already-persisted factor's stored `maximum_penalty`).

### Indexes added
- `idx_health_snapshots_instance_time (plant_id, instance_key, computed_at)` - the "history for one equipment over a time range" query shape.
- `idx_health_snapshots_plant_time (plant_id, computed_at)` - "plant-wide latest state" listings.
- `idx_health_factor_snapshots_parent (health_snapshot_id)` - "every factor row for a selected snapshot" detail view.

### Migration/backup procedure
Additive only (`CREATE TABLE IF NOT EXISTS`), idempotent (run twice in a test, byte-identical schema, zero data loss), zero rows inserted by the migration itself. **Backup method deliberately changed from the project's usual `shutil.copy2()` convention to SQLite's own Online Backup API** (`sqlite3.Connection.backup()`) per your explicit Phase 12.x instruction ("use the SQLite backup API... do not raw-copy an actively written DB") - `config.db` now has more concurrent writers (8 workers + Streamlit) than when the `shutil.copy2` convention was established, so the Online Backup API's safety against concurrent writes is the more conservative choice here. Applied to the live database: `database/simulation/config_before_health_persistence_20260816_144501.db`. Confirmed after migration: `anomalies` row count unchanged (287), zero destructive changes to any Phase 1-11 table.

### Worker architecture (`engine/health_orchestration.py` + `app/equipment_health_worker.py`)
`run_cycle()`: discovers plants dynamically → `discover_health_targets()` per plant (Phase 12.1, unmodified) → `evaluate_and_maybe_persist()` per instance, each wrapped in its own try/except (one equipment's failure is logged and skipped, never aborts the cycle) → aggregates `{eligible, evaluated, scored, insufficient, persisted, unchanged, errors, duration_seconds}`.

### Calculation cadence
`TICK_INTERVAL_SECONDS = 300` (5 min) - health is a derived signal built from `anomaly_worker`'s own data (60s cycle, but individual anomaly transitions are gated by 5-15 min persistence-period buckets), so scores cannot change meaningfully faster than that; 5 minutes sits deliberately between `anomaly_worker`'s 60s (real-time evidence) and `opportunity_worker`'s 900s (aggregated/derived signal, "should not churn"). A full live pass across all 34 equipment measured 4.4-5.8s across 5 real consecutive cycles - two orders of magnitude below the tick interval.

### Persistence / change-detection rules (`engine/health_persistence_targets.py`, `engine/health_orchestration.has_material_change()`)
Checked in a fixed, deterministic order (first match wins): no prior snapshot → `initial`; score `None`→real → `recovered_from_insufficient`; real→`None` → `became_insufficient`; `|Δscore| >= SCORE_CHANGE_THRESHOLD (3.0 points)` → `score_changed`; band differs → `band_changed`; confidence differs → `confidence_changed`; coverage differs → `coverage_changed`; provisional flag differs → `provisional_changed`; the SET of currently-`penalized` factor_ids differs from the latest snapshot's → `factor_composition_changed`; otherwise → `unchanged` (no write). All thresholds centralized in one file, never scattered through worker code.

### Heartbeat
`HEARTBEAT_MAX_INTERVAL_HOURS = 24` - even fully unchanged equipment gets a fresh snapshot at least once a day, preserving trend continuity ("assessment continued running and remained healthy") without daily-churn spam for equipment that never changes cycle-to-cycle. Directly tested at both sides of the boundary.

### Atomic snapshot/factor persistence (item 9)
`engine/health_domain.persist_snapshot()` writes the parent row and every factor row inside ONE `sqlite3` transaction, `commit()`-ed once at the end; any exception triggers `rollback()` before re-raising. **Directly tested with a real constraint violation** (not a mock): a factor's `family` set to `None` mid-list causes a genuine `sqlite3.IntegrityError` after the parent row and one factor row are already staged in the transaction - confirmed the whole transaction rolls back, leaving zero rows in either table, never a misleading partial snapshot.

### NULL/INSUFFICIENT persistence behavior
Explicitly tested and confirmed: `health_score = NULL` persists as a real row (not a missing one), `health_band = NULL` alongside it, `coverage_status = 'INSUFFICIENT'` recorded - historical reporting can distinguish "no assessment was run" (no row at all) from "assessment ran, evidence was insufficient" (a real row with `NULL` score). `engine/health_history.get_history()` returns these `NULL` values verbatim - never coerced to 0, 100, or carried forward from the previous score.

### Restart safety
`run_cycle()` always starts by reading the LATEST persisted state from the database (`dom.get_latest_snapshot()`), never trusts in-memory state - a fresh worker process (after a real restart or the first-ever run) makes exactly the same persist/skip decisions as a long-running one. Directly tested (and confirmed live): two consecutive `run_cycle()` calls with unchanged evidence produce `persisted=0` on the second call, not a duplicate-explosion.

### Historical retrieval API (`engine/health_history.py`) - the Phase 12.3 backend contract
`get_latest()`, `get_history(hours=/days=/start=/end=)`, `get_factor_detail(snapshot_id)`, `compute_trend()`, `band_transitions()`, `confidence_transitions()` - all pure read functions over `engine/health_domain.py`'s CRUD layer. A future Streamlit page consumes THIS module (or a thin `ui/health_data.py` wrapper matching every prior phase's convention) - never queries the two new tables directly.

### Trend calculation semantics (item 14) - explicitly NOT prediction
`compute_trend()` compares the latest snapshot's score against the most recent PRIOR snapshot that also had a real numeric score (skipping over `NULL`/INSUFFICIENT gaps, which have nothing to compare). Direction is `IMPROVING`/`STABLE`/`DETERIORATING` using the same `SCORE_CHANGE_THRESHOLD` as the persistence gate (one consistent "is this a real change" judgment, not two different arbitrary numbers), or `INSUFFICIENT_HISTORY` when there's nothing comparable yet. Purely descriptive and backward-looking ("score decreased from 88 to 71 over 3 days") - confirmed by source-inspection that no RUL/failure-probability/forecasting language appears anywhere in this module.

### Band-transition handling
`band_transitions()` returns consecutive-pair band changes (`from_band`/`to_band`/`at`) within a bounded window - a plain historical fact, never an automatically-raised alarm (no `machine_events` write, no notification anywhere in this phase).

### Confidence-transition handling
`confidence_transitions()` is computed entirely independently of `compute_trend()`'s score-based direction - directly tested with a scenario where the score is held exactly constant while confidence changes, confirming the score trend reports `STABLE` while a real confidence transition is still recorded. Confidence improvement is never interpreted as physical machine improvement.

### Model-version persistence
`health_model_version` (currently `"1.0"`, from Phase 12.1's own registry, unmodified) is stored on every snapshot row - a future scoring-methodology change would be immediately distinguishable in history without needing to guess which rows used which rules.

### Historical traceability
Every factor row carries its own `maximum_penalty`/`reason`/`provenance`/`evidence_timestamp`/`evidence_confidence` as they were AT CALCULATION TIME - a historical result is fully explainable without consulting today's `engine/health_targets.py` registry. Directly tested: patching in a hypothetical future `FAMILY_MAX_PENALTY` change after a snapshot was persisted does not alter what that snapshot's stored factor rows report.

### Database growth estimate — revised after sustained production observation
The original estimate (60-140 snapshot rows/day) was based on a ~90-second manual sample and is now superseded by a much better-calibrated figure: **10 real `equipment_health_worker.service` cycles over ~45 minutes** (14:54-15:39, plus one more immediately after the systemd restart) produced **11 genuine `score_changed` transitions** (1, 1, 2, 0, 0, 1, 1, 1, 2, then 1 more post-restart) out of 34 equipment per cycle, entirely from real simulator/anomaly-worker activity - no data manipulation. That is **~14.7 material changes/hour ≈ roughly 320-350 `equipment_health_snapshots` rows/day** (once the one-time 34-row `initial` bootstrap is past) and, at the observed ~5.4 factor rows/snapshot average, **roughly 1,700-1,900 `equipment_health_factor_snapshots` rows/day**. Annualized: roughly 117,000-128,000 snapshot rows/year and 620,000-690,000 factor rows/year.

**Why this is higher than the original estimate, explained rather than hidden:** the first 90-second sample happened to catch an unusually quiet window. Sustained observation shows nearly all 34 equipment currently sit at LOW/provisional statistical confidence (young baseline history, disclosed since Phase 12.1) with scores that naturally drift near the `SCORE_CHANGE_THRESHOLD` (3.0-point) boundary as new evidence arrives each anomaly-worker cycle - crossing that boundary slightly more often than the original short sample suggested. This is real, honestly-reported behavior, not a design defect: the change-detection and heartbeat rules are working exactly as specified (each cycle still shows the large majority of equipment `unchanged`, e.g. 33/34 or 32/34 per cycle), and the revised annual figures remain small relative to `plc_data`'s multi-million-row scale and require no immediate retention action. Persistence rules were **not** altered to change this number.

### Tests added (33, all passing, `tests/test_health_persistence.py`)
Migration (additive, idempotent, Phase 9 data untouched, SQLite backup API used); atomic persistence (first assessment, INSUFFICIENT-with-NULL-score, provisional flag, model version/provenance, a REAL rollback via genuine constraint violation, historical independence from a simulated future registry change); every change-detection transition type (score/band/confidence/coverage/provisional/factor-composition/became-insufficient/recovered-from-insufficient/unchanged); heartbeat (both sides of the 24h boundary); restart safety (no duplicate explosion); failure isolation (one equipment's exception doesn't stop the cycle, confirmed the failing equipment has no snapshot while its sibling does); P01/P02 plant isolation; historical query (chronological order, NULL preserved, factor detail correct); trend/band-transition/confidence-transition semantics (including the score-stable-but-confidence-changed independence case); engine-result-unchanged-by-persistence; and source-inspection guards (no historian rescan, no LLM, no PLC, no work-order/maintenance-recommendation language, no automatic backfill, Phase 12.1 files untouched).

### Full regression result
622 tests (589 existing + 33 new, exact expected sum), same 5 pre-existing unrelated failures (`test_compressor_aliases`, `test_typo_tolerant_aliases`, `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`), **0 new regressions**.

### Live worker validation (real database, backed up first)
5 consecutive real `run_cycle()` calls against the live database: cycle 1 (first ever) → 34/34 evaluated, 34 scored, 0 insufficient, **34 persisted** (all `initial`), 0 errors, 4.64s. Cycle 2 (immediate rerun) → **0 persisted**, 34 unchanged, 4.79s - confirmed no duplicate explosion. Cycles 3-5 (spaced ~3s apart) → mostly unchanged, with **one genuine, naturally-occurring `score_changed` transition observed** on `P01.COLDROOM.CR01` (72.5 → 75.7, MONITOR both before and after) - real simulator/anomaly-worker activity, not manufactured. No fake data was ever inserted; no simulator/threshold state was touched to produce this.

### Historical query examples (6 equipment types, real live data)
| Equipment | Type | Score | Band | Confidence | Provisional | Usable/Applicable |
|---|---|---|---|---|---|---|
| P01.UTILITY.AC01 | air_compressor | 80.0 | MONITOR | LOW | Yes | 3/3 |
| P01.UTILITY.CHL02 | chiller | 69.6 | ATTENTION | LOW | Yes | 8/8 |
| P01.WATER.WSP01 | water_supply_pump | 60.1 | ATTENTION | LOW | Yes | 8/8 |
| P01.HVAC.AHU01 | ahu | 77.2 | MONITOR | LOW | Yes | 5/5 |
| P01.COLDROOM.CR01 | cold_room | 72.5→75.7 | MONITOR | LOW | Yes | 3/4 |
| P01.PROD.MILL01 | production_process | 88.0 | HEALTHY | HIGH | No | 5/5 |

### Factor reconciliation example (real live data, item 28)
`P01.WATER.WSP01`, snapshot id 9, stored `health_score = 60.1`:
```
wsp_vibration_condition (CONDITION)          clean        0.00
wsp_bearing_temp_condition (CONDITION)       penalized    9.92
wsp_flow_per_kw_condition (CONDITION)        clean        0.00
wsp_power_load (ELECTRICAL_LOAD)             penalized   10.00
wsp_flow_process (PROCESS)                   penalized    8.00
wsp_delta_p_process (PROCESS)                clean        0.00
water_supply_pump_maintenance_overdue        clean        0.00
water_supply_pump_repeated_events (EVENTS)   penalized   12.00
                                              ─────────────────
                                Sum of penalties:    39.92
                                100 − 39.92 =         60.08 → rounds to 60.1
                                Stored health_score:  60.1  ✓ exact match
```

### Performance
Manual pre-deployment: 4.4-5.8s per full 34-equipment cycle. **Real systemd-managed cycles** (post-deployment, from the journal): 6.895s, 4.99s, 3.89s, 3.837s, 4.079s, 4.36s, 4.08s, 3.836s, 4.405s, 4.0s (post-restart) - averaging **~4.3s**, consistent with the Phase 12.1 calculation-only baseline (4.29s). Persistence overhead is negligible; no material calculation-performance degradation under real systemd supervision. Zero `plc_data` (historian) queries anywhere in the health worker or persistence layer.

---

## Phase 12.2 — Production Deployment & Commissioning Verification

**Status: COMPLETE.** `equipment_health_worker.service` installed, verified ACTIVE + ENABLED, restarted under systemd supervision, and re-verified - all with zero data loss, zero duplication, zero corruption.

### A. Service status
- Unit source: `/home/test/SmartMachineAI/deploy/systemd/equipment_health_worker.service`. Installed unit: `/etc/systemd/system/equipment_health_worker.service` - confirmed **byte-for-byte identical** via `diff`.
- `systemctl is-active` → `active`. `systemctl is-enabled` → `enabled`.
- `User=test`, `WorkingDirectory=/home/test/SmartMachineAI`, `ExecStart=/home/test/SmartMachineAI/venv/bin/python -m app.equipment_health_worker` (correct venv interpreter), `Environment=PYTHONUNBUFFERED=1`, `Restart=always`, `RestartUSec=5s` - all confirmed via `systemctl show`, byte-identical to every other worker's convention.
- `NRestarts=0` both before and after the deliberate restart (the restart was a clean, requested stop/start - not a crash-triggered `Restart=always` recovery).

### B. Restart result
Pre-restart PID **295678** → post-restart PID **299366** (confirmed changed). Journal shows a completely clean sequence: `Stopping...` → `Deactivated successfully` → `Stopped` (`Consumed 40.575s CPU time`) → `Started` → immediate clean first cycle (4.0s, `persisted: 1, unchanged: 33, errors: 0`). No traceback, no restart loop, no manual `kill`/`pkill` used - a genuine `sudo systemctl restart` as requested.

### C. Single-instance result
Exactly one `equipment_health_worker` process at all times (`ps aux` confirmed before and after restart). The lock file (`logs/equipment_health_worker.lock`) is held exclusively (`lsof` shows `3wW`) by the current PID. A live manual duplicate-start attempt (`python -m app.equipment_health_worker` from the CLI while the systemd instance held the lock) correctly printed the refusal message and exited immediately (exit code 1) - `fcntl.flock()` protection confirmed effective under systemd deployment, and no stray process was left behind.

### D. Live cycle result
Real systemd-managed first cycle: `eligible=34, evaluated=34, scored=34, insufficient=0, persisted=1, unchanged=33, errors=0, duration=6.895s`. Ten cycles observed total (nine before the restart, one after) - every single one showed `eligible=evaluated=34`, `errors=0`, and `persisted` between 0 and 2 (never a mass burst).

### E. Snapshot row counts
Pre-restart baseline (recorded before requesting restart): **37**. Immediately after restart: **45** (37 + 7 real cycles' worth of genuine `score_changed` transitions accumulated while awaiting your confirmation, + 1 more from the first post-restart cycle). Distinct equipment represented: **34 both before and after** - no equipment appeared, disappeared, or duplicated.

### F. Factor row counts
Pre-restart: 207. Post-restart: **244**. Zero orphaned factor rows (no parent), zero snapshots with missing factor rows, zero snapshots where the factor-row count didn't match `applicable_factor_count` - checked both before and after the restart, both clean.

### G. Duplicate suppression
The restart itself added **exactly one** new snapshot (`P01.UTILITY.CHWP03`, `score_changed`, real evidence-driven), not a 34-row burst - confirmed by inspecting every row with `computed_at >= restart time`. `change_reason` distribution after restart: `{'initial': 34, 'score_changed': 11}` - every row has an accounted-for reason, none attributable to "the worker woke up" or "the service restarted" alone.

### H. Health-score reconciliation (current live data, post-restart)
`P01.UTILITY.CHWP03`, snapshot id 45, stored `health_score = 87.2`:
```
chwp_flow_process (PROCESS)                   penalized    0.80
chilled_water_pump_repeated_events (EVENTS)   penalized   12.00
                                               ────────────────
                                 Sum of penalties:   12.80
                                 100 − 12.80 =        87.20
                                 Stored health_score: 87.2  ✓ exact match
```
A second independent example (`P01.UTILITY.CHWP01`, snapshot 36, pre-restart): penalties summed to 12.99, `100 − 12.99 = 87.01` → stored `87.0` - also an exact match.

### I. Score vs. confidence semantics reconfirmed
Live: 0 of 34 equipment currently `NULL`/INSUFFICIENT (all have enough evidence today) - `NULL`-score persistence is verified via the isolated deterministic test fixture (`test_insufficient_assessment_persists_with_null_score`, still passing), per your own explicit instruction not to corrupt live data to force this case. `provisional=True` persists correctly for LOW-coverage equipment (confirmed both live and via test). `health_model_version='1.0'` on 100% of rows; `provenance='SIMULATION_TUNING'` on 100% of factor rows - both unchanged by the restart.

### J. History API / trend / confidence-transition checks
All 6 requested equipment types re-verified against current live data via `engine/health_history.py` (never direct table queries). `P01.COLDROOM.CR01` (the one equipment with >1 snapshot at check time) showed a real `IMPROVING` trend (72.5→75.7, +3.2) with purely descriptive fields (`direction`/`latest_score`/`previous_score`/`absolute_change`/`observation_period_start`/`observation_period_end`) - no failure-prediction language anywhere, confirmed both by reading the actual returned dict and by the existing source-inspection test. Band/confidence-transition functions re-confirmed independent of the score-trend function (test: score held constant while confidence changes → score trend reports `STABLE`, confidence transition still recorded).

### K. Heartbeat / concurrency / historian
`HEARTBEAT_MAX_INTERVAL_HOURS = 24.0`, `SCORE_CHANGE_THRESHOLD = 3.0` reconfirmed from the live constants module. `journalctl` across `plc_logger`/`energy_kpi_worker`/`baseline_worker`/`anomaly_worker`/`opportunity_worker`/`savings_verification_worker`/`equipment_health_worker` since the health worker's install showed **zero** "database is locked"/busy/failed-commit/corruption errors. Historian (`plc_data`) confirmed growing continuously through the entire deployment/restart window: 9,018,743 → 9,019,538 (pre-restart) → 9,076,675 (post-restart) - no gap, no interruption attributable to the health worker.

### L. All 10 services
`plc_logger`, `production_simulator`, `event_monitor`, `energy_kpi_worker`, `baseline_worker`, `anomaly_worker`, `opportunity_worker`, `savings_verification_worker`, `equipment_health_worker`, `streamlit` - **all confirmed `active`**, both before and after the restart.

### M. Final regression (post-deployment)
622 tests (589 existing + 33 new, exact expected sum), same 5 pre-existing unrelated failures (`test_compressor_aliases`, `test_typo_tolerant_aliases`, `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`), **0 new regressions** - re-run in full after production installation and restart, not merely carried over from the pre-install run.

### Known limitations / proposed Phase 12.3 scope
- No UI yet, per explicit scope - Phase 12.3's job, consuming `engine/health_history.py` as its backend contract.
- No live INSUFFICIENT equipment currently exists (all 34 have enough evidence today) - that persistence path remains verified exclusively via the isolated test fixture, per your own explicit instruction not to corrupt live data to force one.
- No retention/downsampling policy implemented yet - the revised growth estimate (~320-350 snapshot rows/day, ~1,700-1,900 factor rows/day) remains small relative to `plc_data`'s scale, but is real, sustained activity worth re-checking after several more days of production running, and worth planning a retention policy for well before this table reaches historian-scale row counts.

---

## Phase 12.3 — Equipment Health UI

**Status:** implementation, tests, full regression, and live validation complete. Read-only page - no schema/worker/scoring change of any kind. Streamlit predates these files - a restart is needed to see the new page (command below).

### Architecture
`ui/health_data.py` (new, read-only data-access layer, mirrors `ui/savings_verification_data.py`'s established convention) + `ui/pages/19_Equipment_Health.py` (new page, added to `ui/Home.py`'s nav). Neither file imports `engine.health_engine` (the scoring module) at all - confirmed by a dedicated test - the page reads exclusively what `equipment_health_worker.service` has already persisted via `engine.health_domain`/`engine.health_history` (both Phase 12.2, unmodified). No scoring, persistence-threshold, confidence-classification, or change-detection logic is reimplemented in the UI layer.

### Overview page (item 1)
KPI row (Total Monitored / Healthy / Monitor / Attention / Investigate / Insufficient Data, computed over the full unfiltered set so filtering the table never moves the factory-wide numbers) + Average Health Score (mean of only the equipment with a real numeric score - INSUFFICIENT equipment is excluded from the average entirely, never averaged in as 0) + a compact confidence-tier breakdown, followed by a filterable/sortable table (Plant, Area, System, Equipment type, Health state, Confidence, plus a sort selector: lowest score first / most recently changed / equipment name).

### Performance (item 8)
The overview issues a small, FIXED number of bulk queries regardless of equipment count - one `list_latest_snapshots_for_plant()` call per plant (2 today), one joined equipment/area/system/plant hierarchy query, one window-function query for trend direction across all equipment at once, and one `IN (...)` query for each row's dominant factor - never one query per equipment (no N+1 pattern). Detailed factor/history data loads only when an equipment is selected in the detail section. Live measured load time: ~3.3s for the full 34-equipment overview (dominated by Streamlit's own script-execution overhead, not query volume).

### Health Score vs. Assessment Confidence (item 2)
Preserved exactly as Phase 12.1/12.2 defined it: the page header explicitly states the semantic distinction before showing any numbers. `NULL`/INSUFFICIENT renders as **"Unavailable"** in the score column and **"Insufficient Data"** in the state column - never 0, never 100, and visually distinct (grey) from every real health band. `provisional=True` is shown as an explicit "Yes" column plus inline next to the confidence metric in the detail view - a high score with LOW/provisional confidence is never presented with the same visual weight as a HIGH-confidence result.

### Equipment detail (item 3) - the factor reconciliation
Exactly the "Starting condition: 100 → contributing evidence → Final Health Score" presentation requested, built directly from `engine.health_domain.get_factor_snapshots()`'s own stored rows (factor_id, family, penalty, evidence source/value/timestamp/confidence, provenance) - no generic/invented factor labels. An expander below lists every factor (penalized, clean, AND missing) with its full stored evidence, so an engineer can see not just what hurt the score but also what evidence a "clean" or "missing" result was actually based on. Directly tested: the sum of persisted penalized-factor rows independently reconciles to the stored `health_score` for a real live degraded pump.

### History visualization (item 4)
Health Score history: `st.line_chart` over `engine.health_history.get_history()`'s real persisted values (gaps for `NULL`/INSUFFICIENT periods render as genuine chart gaps, never interpolated or coerced). Confidence and health-state history are shown as **separate transition lists** (`confidence_transitions()`/`band_transitions()`), not forced into the same numeric chart - confidence has no defensible 0-100 scale, and charting it as one would violate item 2's "never interchangeable percentages" rule. Trend wording uses exactly `Improving`/`Stable`/`Declining`/`Insufficient history` (backend `DETERIORATING` is presentation-mapped to "Declining" for this page only - backend vocabulary itself is untouched) - confirmed by a runtime test that no failure-prediction/RUL/probability language appears anywhere on the rendered page.

### Change explanation (item 5) - grounded, not LLM-generated
`ui/health_data._explain_latest_change()` diffs the two most recently persisted snapshots' own stored factor rows (via `get_factor_detail()`, already-persisted, never recalculated) and reports which factor(s) changed penalty by how much - every line traces to a real stored row. No LLM call anywhere in this module or page (source-inspection guarded). Live smoke-tested against `P01.COLDROOM.CR01`'s real accumulated history: score moved 79.7 -> 72.5 (-7.2), and the explanation correctly attributed the entire change to `cr_room_temp_process` (+7.2 penalty, matching exactly) with no other factor implicated - not an approximation.

### Engineering-first UX / consistency (items 6-7)
Follows `ui/pages/16_Anomalies.py`'s exact established convention: title+caption header, `st.columns()` filter row, color-coded `st.dataframe` (band-tinted rows, same color-mapping technique as every prior Phase 9-11 page), `st.selectbox`-driven detail section below, `st.expander` for verbose evidence detail. No new component library, no gauges/animations/gradients - `st.metric`/`st.dataframe`/`st.line_chart`/`st.caption` only, matching the plain, information-dense style of every existing page.

### Tests added (11, all passing, `tests/test_health_ui.py`)
Empty state (zero assessments survives cleanly); healthy/high-confidence rendering; LOW-confidence/provisional rendering; degraded equipment with independent factor-sum reconciliation; `NULL`/INSUFFICIENT rendering (never 0/100, excluded from the average); chronological history with score/confidence kept separate (score held constant while confidence changes -> trend still reports STABLE); no-prediction-language runtime guard (checks the actual rendered page text, not source code, avoiding the self-referential-docstring false-positive class encountered repeatedly in earlier phases); source-inspection guard confirming `engine.health_engine` is never imported by either new file; equipment-type filter narrows the table correctly; lowest-score-first sort works; existing pages' navigation source unaffected.

### Full regression result
633 tests (622 existing + 11 new, exact expected sum), same 5 pre-existing unrelated failures (`test_compressor_aliases`, `test_typo_tolerant_aliases`, `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`), **0 new regressions**.

### Live validation (real production data)
Page loaded directly against the live database via `AppTest` - `at.exception` empty, 34/34 real equipment rendered, KPI cards showed real live counts (20 Healthy / 11 Monitor / 3 Attention / 0 Investigate / 0 Insufficient, average score 83.4). All three pre-existing pages sharing this session's navigation (`Savings Verification`, `Energy Opportunities`, `Anomalies`) re-confirmed loading with no exception - the new page addition did not disturb them.

### Action needed from you
`streamlit.service` predates these files - restart to see the new "Equipment Health" page in the sidebar:
```bash
sudo systemctl restart streamlit.service
```

### Known limitations / proposed Phase 12.4+ scope
- No executive/plant-comparison dashboard, no PDF/report export - out of explicit scope for this phase.
- The "dominant factor" shown in the overview table is the single highest-penalty factor only, computed in one bulk query - the full breakdown (all factors, all evidence) is intentionally deferred to the detail view.
- No alerting/notification wiring from health-state transitions - explicitly out of scope (a band transition is a fact, never an automatically-raised alarm).

---

## Phase 12.3A — Equipment Health UI Polish

**Status:** complete, tested, live-validated against real current data. Presentation-only - zero backend/engine/worker/schema changes.

### Scope confirmation
No file under `engine/health_*.py`, `app/equipment_health_worker.py`, `deploy/systemd/equipment_health_worker.service`, or the database schema was touched. The UI remains a strictly read-only consumer of Phase 12.1/12.2's persisted results - confirmed by the same "never imports `engine.health_engine`" source-inspection test from Phase 12.3, still passing unchanged.

### Files modified
`ui/health_data.py` (added a centralized presentation-label layer - no existing function changed), `ui/pages/19_Equipment_Health.py` (layout/wording rewrite). No other file touched.

### Centralized label mapping (item 5) - audited, not guessed
`ui/health_data.py` gained `EQUIPMENT_TYPE_LABELS` (all 8 supported types), `FACTOR_LABELS` (all 44 factor_ids currently defined across `engine.health_targets.HEALTH_FACTOR_REGISTRY` - pulled directly from the live registry, not inferred from naming convention), `FAMILY_LABELS`, and `format_timestamp()`/`format_change_delta()` helpers. A test (`TestLabelMapping.test_every_registered_factor_id_has_a_human_label`) asserts every registry factor_id has an explicit mapping, not just a fallback. Unknown (future) IDs safely fall back to a title-cased, underscore-stripped version rather than failing - tested directly.

### Detail-header truncation fix (item 3)
The identity/context header (equipment name, plant/area, system/type, criticality) now renders via `st.markdown` (which wraps) instead of `st.metric` (which clips long values with "..."). Live-confirmed against the exact example from your report: `Water Supply Pump WSP01 (P02)` now shows its full name, `P02 / Utilities Yard`, `Water Supply / Water Supply Pump`, and a fully-readable `16 Aug 2026 17:20:47`-style timestamp - none truncated.

### Human-readable equipment types (item 4)
Overview table's Type column and the equipment-type filter dropdown both show `Water Supply Pump`/`Chiller`/`Air Compressor`/etc. - the filter dropdown maps `{label: canonical}` internally so filtering still matches the real stored value. The canonical value (`water_supply_pump`) is never renamed in the database and remains visible in the detail view's System/Type line.

### Human-readable factor labels (item 5) - the most important change
Factor breakdown, technical-detail expander, and "what changed recently" section all show e.g. `Repeated Abnormal Events`, `Electrical Load / Power Behaviour`, `Bearing Temperature Condition`, `Flow / Process Condition` instead of the internal `water_supply_pump_repeated_events`/`wsp_power_load`/`wsp_bearing_temp_condition`/`wsp_flow_process` IDs - live-confirmed on `P01.WATER.WSP01`'s real current factor breakdown.

### Technical traceability preserved (item 6)
The canonical `factor_id`, raw `factor_family` code, penalty/max-penalty, evidence source/value/timestamp/confidence, and provenance all remain fully visible inside the "Technical detail" expander (renamed from the prior "Evidence detail" for clarity) - each entry shows both the human label AND the backend code (e.g. "Bearing Temperature Condition ... `wsp_bearing_temp_condition` · Family: Condition (CONDITION)"). Directly tested that the canonical ID string appears verbatim on the rendered page.

### Health-state visual hierarchy (item 7)
The detail view's Health State now renders as a small colored badge (reusing the exact same band-color mapping as the overview table's row tinting - HEALTHY green, MONITOR blue, ATTENTION amber, INVESTIGATE red) via a compact inline-styled `st.markdown` span - no new charting/UI library, no gauges/animations. `MONITOR` intentionally uses the same calm blue as before, never alarm-red.

### Score vs. Confidence (item 8) - unchanged semantics, clearer presentation
The overview table and detail view both now show confidence and provisional status together as one compact string (`LOW · Provisional`) rather than a separate wide column, with an explanatory caption ("Limited supporting history/evidence - score is provisional.") appearing specifically when confidence is LOW. Confidence is never rendered as a percentage or color-coded in a way that implies poor machine condition.

### Coverage visibility (item 9)
A dedicated `**Coverage:** X of Y applicable factors usable` line now appears directly below the confidence caption, with its own explanatory caption clarifying it is related to but not identical with Assessment Confidence (full coverage does not by itself imply HIGH confidence - historical depth/context-match quality still matters, exactly as Phase 12.1 defined it).

### Factor breakdown readability (item 10)
Unchanged reconciliation math (`100 - penalties = score`, still sourced entirely from persisted rows), but now: human factor label as the primary text, family shown in italics underneath, sorted by penalty magnitude descending (largest first, unchanged from Phase 12.3). Live-confirmed exact reconciliation: `100 - 12.00 - 10.00 - 8.48 - 0.80 = 68.72` -> displayed `68.7`, matching the persisted `health_score` exactly.

### "What changed recently" wording (item 11)
Now phrased as `<Factor Label> improved/worsened by X.X health point(s).` (direction derived purely from the sign of the already-computed persisted delta - no new arithmetic), with the canonical ID, status transition, and full persisted reason still shown directly below in a caption. Live-confirmed: `Flow / Process Condition improved by 7.2 health point(s).`

### "Recent Movement" wording (item 12)
The word "Trend" is replaced with "Recent Movement" everywhere on this page only - `engine.health_history.compute_trend()`'s function name, return dict keys, and the `IMPROVING`/`STABLE`/`DETERIORATING`/`INSUFFICIENT_HISTORY` backend enum are completely unchanged. The explanatory sentence was also strengthened: "a descriptive comparison of historical health assessments only, never a prediction of future condition."

### History chart legend (item 13)
A compact text caption below the existing `st.line_chart` now states the accepted band boundaries ("Health bands for reference: 85-100 Healthy · 70-84 Monitor · 50-69 Attention · 0-49 Investigate") - no new charting dependency, no threshold overlay lines, no interpolation. `NULL`/INSUFFICIENT gaps in the chart remain genuine gaps, explicitly called out in the caption.

### Overview table readability (item 14)
Human-readable Type column, Confidence+Provisional merged into one compact column, consistently formatted Last Assessed timestamps, explicit `column_config` widths for the Equipment/Last Assessed/Primary Factor columns so long values get more room without needing a wider table overall.

### Timestamp presentation (item 15)
`ui/health_data.format_timestamp()` renders every timestamp on this page (Last Assessed, evidence timestamps, change-explanation dates, transition-history dates, trend observation period) as `16 Aug 2026 17:20:47` - consistently, everywhere. The underlying stored value (`%Y-%m-%d %H:%M:%S`) is never altered, only its display.

### Criticality presentation (item 16)
Unchanged: still shows the honest `Unconfigured` (never fabricated as Low/Normal/Medium), now paired with an explicit caption - "Not configured - does not affect the Health Score." - reinforcing the Phase 12.1 architectural guarantee directly at the point where an engineer might otherwise wonder why it's blank.

### No new intelligence (item 17) - confirmed
No root-cause diagnosis, no failure prediction, no RUL, no automatic maintenance recommendation, no work-order generation, no LLM, no Ask AI integration, no PLC/control writes, and no new scoring factor were added anywhere in this phase.

### Performance (item 18)
Live measured: **3.42s** for the full 34-equipment overview (previous baseline: 3.3s) - no material degradation. Query architecture unchanged (same fixed set of bulk queries; overview never loads per-equipment factor/history detail).

### Tests added (7 new, all passing; 4 pre-existing tests updated for the intentional wording/column changes - total `tests/test_health_ui.py` now 18 tests)
Every registered factor_id has an explicit human label (live registry audit, not a sample); every registered equipment type has a human label; unknown factor/equipment-type IDs fall back safely without raising; label helpers never mutate the canonical backend registry; change-delta wording direction (`improved`/`worsened`) is correct; canonical factor ID and raw family code remain visible in the technical-detail expander. Updated: LOW-confidence rendering now checks the merged `"LOW · Provisional"` cell; the history-separation test checks `"Recent Movement: **Stable**"`; the equipment-type filter test uses the human-readable dropdown value.

### Full regression result
640 tests (633 existing + 7 new, exact expected sum), same 5 pre-existing unrelated failures (`test_compressor_aliases`, `test_typo_tolerant_aliases`, `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`), **0 new regressions**.

### Live validation (real, current production data - not hardcoded)
Loaded directly against the live database: `at.exception` empty, 34/34 equipment rendered, 3.42s load time. The exact equipment cited in your report - `Water Supply Pump WSP01 (P02)` - was located live via the actual filter/select flow (never hardcoded) and showed its CURRENT value (**68.7, ATTENTION, LOW · Provisional**) with a fully-readable header, a reconciling factor breakdown (`Repeated Abnormal Events -12.00`, `Electrical Load / Power Behaviour -10.00`, `Bearing Temperature Condition -8.48`, `Flow / Process Condition -0.80` -> 68.7 exactly), and a grounded change explanation (`Flow / Process Condition improved by 7.2 health point(s)`, score 61.5 -> 68.7). Chiller, Air Compressor, AHU, Cold Room, and Production Equipment (High-Speed Disperser) were each separately selected in the detail view with zero exceptions.

### Action needed from you
`streamlit.service` predates these files - restart to see the polished page:
```bash
sudo systemctl restart streamlit.service
```

### Remaining UI limitations (unchanged from Phase 12.3, not addressed here - out of scope)
- No executive/plant-comparison dashboard, no PDF/report export.
- No alerting/notification wiring from health-state transitions.
- The "dominant factor" shown in the overview table remains the single highest-penalty factor only (full breakdown stays in the detail view).

---

# Phase 12.4 — Equipment Health Integration & Final Acceptance

**Status:** complete, tested, live-validated. **Phase 12 declared COMPLETE** (see verdict below).

### Existing Phase 12 components audited/reused
All of Phase 12.1 (`health_targets`/`health_evidence`/`health_engine`), 12.2 (persistence schema, worker, history API), 12.3/12.3A (Equipment Health page, label mapping) treated as accepted and unmodified. This phase only ADDS new read-only cross-page consumers of `ui/health_data.py`'s existing contract - no scoring, persistence, or worker file was touched.

### Files created
`tests/test_health_integration.py` (15 new tests). No new engine/worker/schema file - this phase is UI integration only.

### Files modified
`ui/health_data.py` (added shared presentation functions + 4 new cross-page read helpers - see below), `ui/pages/19_Equipment_Health.py` (refactored to use the newly-shared functions instead of local copies; added deep-link query-param handling), `ui/scada_floor_plan_data.py` (added `equipment_id` to `_load_equipment()`'s output; `build_equipment_snapshot()` now attaches a compact `health` block per equipment via one bulk query), `ui/pages/12_SCADA_Floor_Plan.py` (JS detail panel renders the health block), `ui/pages/4_Service_and_Maintenance.py` (compact health context + "View Health Detail" deep-link at the selected-equipment point), `ui/pages/11_Equipment_and_Tag_Configuration.py` (one-line compact health caption, optional per item 7), `ui/Home.py` (factory-wide health summary counts).

### Pages integrated
SCADA Floor Plan (required), Service & Maintenance (required), Equipment & Tag Configuration (optional, implemented), Home (optional, implemented). Ask AI: confirmed untouched (AppTest smoke-passed, zero source changes).

### Shared health-summary/read architecture (item 9)
`ui/health_data.py` now exposes ONE contract every integration point uses:
- `score_text()`/`state_text()`/`confidence_text()`/`state_badge_html()` - presentation functions, relocated (not duplicated) from the Equipment Health page itself; the main page was refactored to call these same functions, confirmed behavior-identical by the full existing 18-test suite passing unchanged after the move.
- `get_latest_health_by_equipment_id(equipment_id)` - single-equipment lookup (Service & Maintenance, Equipment & Tag Configuration).
- `get_latest_health_by_equipment_ids(equipment_ids)` - ONE bulk query for many equipment at once (SCADA Floor Plan) - never a loop of single lookups.
- `get_health_summary_counts()` - lightweight factory-wide counts only, deliberately skipping the hierarchy/trend/dominant-factor enrichment `get_overview()` does (Home doesn't need equipment names) - one small query, not the ~3.4s full overview cost.
- `compact_health_context(equipment_id)` - pre-formatted, ready-to-render strings, distinguishing "Not Assessed" (no snapshot exists - `get_latest_health_by_equipment_id()` returns `None`) from "Insufficient Data" (a real snapshot exists with `health_score = NULL`).

All four read helpers key off the stable `equipment.id` - never display-name string parsing (item 5), and none of them import `engine.health_engine` or recalculate anything (confirmed by source-inspection tests).

### SCADA Floor Plan integration
`_load_equipment()` now includes each equipment's `id`. `build_equipment_snapshot()` calls `get_latest_health_by_equipment_ids()` exactly ONCE per plant per snapshot-writer tick (measured overhead: **3.1ms** for 38 equipment - negligible against the ~3.2s total tick, which is dominated by pre-existing `RuleEngine` tag evaluation, unrelated to this change). The JS detail panel (`scadaRenderDetail()`) renders a compact health block - Score/State/Confidence/Last Assessed - inside its own bordered box, explicitly labeled "separate from the PLC status above," using a dedicated `HEALTH_BAND_COLOR` palette that is never mixed with the existing `SEVERITY_COLOR` alarm palette (item 13). Unassessed equipment shows "Equipment Health: Not Assessed," never silently omitted or shown as healthy. **No deep-linking from SCADA** - the floor plan is a `st.components.v1.html()`-sandboxed custom JS component with no clean path to trigger a native Streamlit page switch from inside it; forcing one would require a fragile workaround, so per your own explicit instruction ("do not force it"), this is a documented, deliberate limitation, not an oversight.

### Service & Maintenance integration
At the exact point an engineer already selects one specific overdue/upcoming equipment, a compact read-only line now shows Health Score/State, Confidence, and Last Assessed, plus a "View Health Detail →" button. The button sets `st.query_params["equipment_id"]` and calls `st.switch_page("pages/19_Equipment_Health.py")` - real Streamlit-native deep-linking (unlike SCADA, this page has no JS sandbox). A source-inspection test confirms the health block never calls `add_maintenance_entry`/`add_service_entry`/any write function - it is purely informational, exactly as item 6 requires (Health does not create maintenance records, change due dates, or mark anything done).

### Equipment Configuration integration (optional, item 7)
One compact caption line ("Current Health: MONITOR · 82.4 · Confidence: ... · Last assessed: ...") at the selected-equipment point - no new editable field, no health value stored on the equipment record.

### Home integration (optional, item 8)
A small "Equipment Health" section showing the SAME 6 counts as the Equipment Health page's own KPI row (Monitored/Healthy/Monitor/Attention/Investigate/Insufficient Data) - real, current, persisted counts via `get_health_summary_counts()`. **No factory-wide or plant-wide "health score" was computed** - no averaging equipment into one number, no plant-vs-plant ranking (item 16), confirmed by source-inspection (no new scoring function anywhere in `ui/Home.py`).

### Navigation / deep-link behavior (item 5)
Implemented cleanly for the two pure-Streamlit integration points (Service & Maintenance ↔ Equipment Health) via `st.query_params` + `st.switch_page()`. The Equipment Health page reads `?equipment_id=N` on load and pre-selects that equipment in its detail dropdown - but only among equipment already passing the CURRENT filters; if the linked equipment is filtered out, an honest message is shown rather than silently overriding the user's filter choices.

### Health/Alarm separation (item 13)
Preserved everywhere: SCADA's health block uses its own palette/wording, never `SEVERITY_COLOR`/alarm vocabulary (source-inspection tested). No integrated page implies "ATTENTION health = active PLC alarm" or "no alarm = HEALTHY."

### Health/Maintenance separation (item 14)
Preserved everywhere: the Service & Maintenance health block is read-only and provably side-effect-free (tested); maintenance overdue remains a Phase 12.1 MAINTENANCE-family factor with its own modest, capped contribution - it does not automatically flip to a health state, and a health ATTENTION does not create a maintenance record.

### Score/Confidence/Provisional cross-page behavior (items 12, 24)
Every integration point shows Score, State, AND Confidence together (never score alone) - `compact_health_context()`'s `confidence` field already includes "· Provisional" when applicable, so no integration point can accidentally hide it. Verified live: a real LOW/provisional equipment (`P01.WATER.WSP01`, 60.1/ATTENTION/LOW · Provisional) displays identically via `compact_health_context()`, the bulk SCADA-style lookup, the Equipment Health overview, and the Equipment Health detail - all four independently confirmed equal.

### NULL/INSUFFICIENT behavior (item 23)
Directly tested with an isolated fixture (no live data corrupted): a real persisted snapshot with `health_score = NULL` renders as "Insufficient Data" / "Unavailable" across every cross-page helper - never 0, 100, or HEALTHY. A DIFFERENT case - no snapshot at all - correctly renders as "Not Assessed," a distinct, honest third state.

### Cross-page consistency test result (item 21)
`TestCrossPageConsistency` (isolated fixture) and a live spot-check (both in this report) confirm: for the same equipment, `compact_health_context()`, the bulk SCADA-style lookup, the Equipment Health overview row, and the Equipment Health detail's `latest` all report identical `health_score`, `health_band`, `assessment_confidence`, `provisional`, and `computed_at` - subject only to formatting. There is exactly one interpretation of health state across this application.

### End-to-end traceability test result (item 20)
`TestEndToEndTraceability` proves the full chain (seeded evidence → `calculate_health()` → `persist_snapshot()` → `compact_health_context()` → `get_detail()`) using only the real read-layer APIs - the detail view's factor rows independently sum to the exact persisted `health_score`, with zero UI-side scoring arithmetic.

### Equipment Health worker status
`equipment_health_worker.service`: **ACTIVE**, **ENABLED**, `NRestarts=0` (same PID throughout this phase's work - never restarted, not needed for verification). Journal shows continuous clean 5-minute cycles the entire time (`eligible=34, evaluated=34, scored=34, insufficient=0, errors=0` every cycle, `persisted` correctly varying 0-1 per cycle - genuine, unforced changes). Database: 74 snapshots, 385 factor rows, **0 orphaned factor rows** at final check.

### All service status
All 10 confirmed `active`: `plc_logger`, `production_simulator`, `event_monitor`, `energy_kpi_worker`, `baseline_worker`, `anomaly_worker`, `opportunity_worker`, `savings_verification_worker`, `equipment_health_worker`, `streamlit`.

### Historian/SQLite health
`plc_data` confirmed growing continuously during this phase's work (9,353,286 → 9,354,749 rows across a 20s window, no gap). `journalctl` across all 7 database-writing services showed zero "database is locked"/busy/corruption errors.

### Tests added
15 new tests (`tests/test_health_integration.py`): end-to-end traceability; cross-page consistency (4 independent read paths); NULL/INSUFFICIENT/"Not Assessed" three-way distinction; provisional rendering consistency; state-vocabulary guard (no GOOD/BAD/CRITICAL/FAIL/DANGER/ALARM); bulk-lookup query-count guard (≤2 SQL statements regardless of equipment count, via a real `sqlite3.trace_callback`, not just code review); and 6 source-inspection guards across every integrated file (no `health_engine` import/recalculation, no direct health-table writes, no historian query for health context, no LLM/PLC-control references, no maintenance-state mutation from the health block, health/alarm color separation, equipment_id-based linkage).

### Full regression result
655 tests (640 existing + 15 new, exact expected sum), same 5 pre-existing unrelated failures (`test_compressor_aliases`, `test_typo_tolerant_aliases`, `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`), **0 new regressions**. Phase 12.1-12.3A's own test suites (`test_health_engine.py`, `test_health_persistence.py`, `test_health_ui.py`) all still pass unchanged.

### AppTest result
All 10 requested pages loaded with **zero exceptions**: Home, SCADA Floor Plan, Service & Maintenance, Equipment & Tag Configuration, Equipment Health, Energy Dashboard, Anomalies, Energy Opportunities, Savings Verification, **Ask AI** (confirmed unaffected).

### Live validation examples (real, current data - 6 equipment types)
| Equipment | Type | Score | State | Confidence | Cross-page match |
|---|---|---|---|---|---|
| P01.WATER.WSP01 | water_supply_pump | 60.1 | ATTENTION | LOW · Provisional | ✓ |
| P01.UTILITY.CHL01 | chiller | 78.8 | MONITOR | LOW · Provisional | ✓ |
| P01.UTILITY.AC01 | air_compressor | 80.0 | MONITOR | LOW · Provisional | ✓ |
| P01.HVAC.AHU01 | ahu | 77.2 | MONITOR | LOW · Provisional | ✓ |
| P01.COLDROOM.CR01 | cold_room | 79.7 | MONITOR | LOW · Provisional | ✓ |
| P01.PROD.DISP01 | production_process | 88.0 | HEALTHY | HIGH | ✓ |

The degraded example (`P01.WATER.WSP01`) was independently confirmed identical across `compact_health_context()`, the SCADA-style bulk lookup, the Equipment Health overview, and the Equipment Health detail.

### Performance
Live measured: Equipment Health page 3.25s (baseline 3.3-3.4s, no degradation); SCADA Floor Plan Python-side render 0.42s with the new health bulk-query adding only ~3.1ms per tick; Service & Maintenance 0.72s; Home 0.47s (using the lightweight summary-counts query, not the full overview). No page requires materially more time than before this phase.

### Remaining Phase 12 limitations
- No deep-linking from SCADA Floor Plan into Equipment Health (JS-sandboxed component - documented, deliberate, not attempted per your own "don't force it" instruction).
- Equipment & Tag Configuration and Home integrations are intentionally minimal (one line / one summary row) per their own "optional, avoid clutter" scope.
- No retention/downsampling policy yet for the health history tables (unchanged from Phase 12.2's own disclosed limitation).

### Deferred Phase 13 items (explicitly NOT built)
Recommended-maintenance-action engine, maintenance priority optimizer, automatic work orders, suggested maintenance dates, spare-parts prediction, maintenance interval optimization, technician assignment, predictive maintenance, RUL, failure probability, predicted failure date - all explicitly out of Phase 12's scope, confirmed absent by source-inspection.

### Phase 12 completion criteria - checked against the spec's own list
Deterministic health engine works; score and confidence remain separate everywhere (including every new cross-page integration point); insufficient evidence never becomes healthy (verified at 3 levels: engine, persistence, and now every UI integration point); health factors remain explainable (factor breakdown + technical detail, reconciling exactly); correlated evidence remains capped (Phase 12.1 family caps, unchanged); health results persist correctly (atomic, append-only, unchanged); history/factor history remain traceable (unchanged, now also consumed cross-page); `equipment_health_worker` is ACTIVE + ENABLED with 0 restarts and clean cycles; Equipment Health UI works and is engineer-readable (Phase 12.3/12.3A, unchanged); cross-page current-health integration is consistent (directly tested and live-verified across 4 independent read paths); no duplicate calculation logic exists in the UI (confirmed by source-inspection across every file touched in Phase 12.3/12.3A/12.4); no predictive claim, no root-cause diagnosis, no automatic maintenance workflow, no LLM health calculation, no PLC/control writes anywhere; full regression has 0 new failures; historian/services remain healthy. **Every criterion is met.**

### PHASE 12 — EQUIPMENT HEALTH: **COMPLETE**

---

## Phase 13 — Maintenance Intelligence

**Completed:** 2026-08-16

### What this phase is
A deterministic Maintenance Priority signal per equipment, combining
Phase 12's already-persisted Equipment Health evidence with the
maintenance schedule - a SEPARATE conclusion from the Health Score
itself, never a renaming of it. Read-only/on-demand: no new table, no
migration, no worker, no new systemd service (stays at 10). No LLM, no
prediction, no Remaining Useful Life, no automatic work orders, no
automatic maintenance-interval changes.

### Files created
- `engine/maintenance_intelligence_targets.py` - centralized registry: 5-value priority vocabulary (`NOT_ASSESSED`/`ROUTINE`/`REVIEW`/`PRIORITY`/`URGENT_REVIEW`), illustrative `SIMULATION_TUNING` score bands/weights, the overdue-maintenance REVIEW floor constants, the evidence-aware confidence rank table, and a curated recommended-check registry keyed to Phase 12.1's own `factor_id` vocabulary (ELECTRICAL_LOAD-family factors explicitly excluded everywhere).
- `engine/maintenance_intelligence_engine.py` - `calculate_maintenance_priority()` / `calculate_all()`. Never calls `engine.health_engine.calculate_health()` - reads Phase 12's already-persisted `equipment_health_snapshots` / `equipment_health_factor_snapshots` rows via `engine.health_domain` / `engine.health_history`, plus one lightweight, already-existing evidence read (`engine.health_evidence.fetch_maintenance_evidence()`, no scoring math) for the overdue-confirmation gate. Debug CLI: `python -m engine.maintenance_intelligence_engine --plant p01`.
- `ui/maintenance_intelligence_data.py` - read-only UI data layer mirroring `ui/health_data.py`'s own convention; its own priority-badge color palette, deliberately distinct from both the alarm palette and `ui.health_data.BAND_BADGE_COLORS`.
- `tests/test_maintenance_intelligence.py` (28 tests).

### Files modified
- `ui/pages/4_Service_and_Maintenance.py` - new third "Maintenance Intelligence" tab (priority-sorted table + KPI row + detail-below, mirroring the Phase 9-12 page convention). No new page.
- `ui/pages/19_Equipment_Health.py` - one compact cross-reference line + "View Maintenance Intelligence →" button at the detail view (reads via `ui.maintenance_intelligence_data.compact_priority_context()`, purely informational).

### Addendum corrections implemented
1. **Overdue-maintenance REVIEW floor** - a confirmed overdue maintenance schedule (Phase 12.1's own MAINTENANCE-family "penalized" gate / a live `fetch_maintenance_evidence()` read) floors `maintenance_priority` at `REVIEW` when the normally-computed band would be `ROUTINE`, applied AFTER the score→band mapping. `priority_score` is always reported unfloored; `floor_applied`/`floor_reason` disclose the override explicitly. Equipment Health is never touched by this floor.
2. **NOT_ASSESSED vs ROUTINE** - `NOT_ASSESSED` fires only when NO dimension contributed evidence at all (neither health/condition, events, movement, criticality, nor a confirmed overdue schedule). A genuinely `HEALTHY`, adequately-assessed equipment with zero penalty contributions is `ROUTINE`, never `NOT_ASSESSED` - gated on `health_score is not None` (adequate evidence WAS assessed), not on whether the assessment happened to produce a nonzero number. This distinction is directly covered by `TestNotAssessedVsRoutine.test_healthy_and_adequately_assessed_with_zero_penalties_is_routine_not_not_assessed`.
3. **Evidence-aware confidence** - `recommendation_confidence` is the worst confidence tier among only the dimensions that actually contributed (health/condition, events, maintenance-overdue, recent movement, criticality) - never an unconditional `min()` across uninvolved dimensions. Non-statistical dimensions (events, maintenance-overdue, criticality) carry an inherently `HIGH` tier (deterministic facts), reusing Phase 12.1's own `_coverage_status()` convention. `confidence_basis` discloses which dimensions were used. Verified against all 4 worked examples in `TestEvidenceAwareConfidence`.

### Final Maintenance Priority states
`NOT_ASSESSED | ROUTINE | REVIEW | PRIORITY | URGENT_REVIEW` - illustrative `SIMULATION_TUNING` score bands 0-24/25-49/50-74/75-100 for the latter four; `NOT_ASSESSED` is not a member of the scored band table, it is a separate outcome selected before the mapping runs. Default sort (most-needs-attention first): `URGENT_REVIEW > PRIORITY > REVIEW > NOT_ASSESSED > ROUTINE`.

### Recommendation registry / recommended checks
Curated `"Inspect"/"Review"/"Verify"` wording only, keyed to Phase 12.1's own `factor_id`s for CONDITION/PERFORMANCE/PROCESS, generic-by-family for EVENTS/MAINTENANCE, a safe fallback for any future unmapped `factor_id`. `recommended_check_for_factor()` raises if ever called for an ELECTRICAL_LOAD factor - that evidence belongs to Energy Opportunities only, confirmed by `TestElectricalLoadExclusion` and a source-guard test that Phase 12.1's own ELECTRICAL_LOAD `factor_id`s never appear in the checks registry.

### Tests added / results
`tests/test_maintenance_intelligence.py`: 28 tests (registry sanity, NOT_ASSESSED-vs-ROUTINE guardrail, overdue floor incl. no-op-when-already-above/no-snapshot-but-overdue variants, all 4 evidence-aware confidence examples, ELECTRICAL_LOAD exclusion, result-contract shape, source guards) - **all passing**.

### Live validation (real production data, `database/simulation/config.db`, no fabricated fixtures)
`python -m engine.maintenance_intelligence_engine` run against both plants (34 real eligible equipment instances, the same population Phase 12 covers): zero crashes, floor correctly applying only to genuinely-overdue equipment (e.g. `P01.UTILITY.CHL01`, `P01.COLDROOM.CR01/CR02`, `P01.FILL.FILL01`), confidence correctly tracking real Phase 12 coverage state (several equipment show LOW confidence, correctly reflecting genuine LOW-coverage Phase 12 assessments, not a Phase 13 defect). `AppTest` renders of both `ui/pages/4_Service_and_Maintenance.py` (Maintenance Intelligence tab) and `ui/pages/19_Equipment_Health.py` (cross-link) against this same live database: **zero exceptions**, KPI counts (Review 9 / Routine 25 / Urgent Review 0 / Priority 0 / Not Assessed 0) matching the CLI's own tally, and the floor-disclosure warning ("Minimum priority floor applied: Scheduled maintenance is overdue.") correctly rendering for `P01.UTILITY.CHL01`'s detail view.

### Deviations from the approved plan/addendum
- The addendum's Example B illustration described health confidence as "not contributing" in a pure maintenance-overdue case. Implementation instead defines "the health/condition dimension contributed" as `health_score is not None` (adequate evidence was assessed) rather than "the health-derived score contribution was nonzero" - required to satisfy this message's own explicit guardrail that a HEALTHY, zero-penalty equipment must be `ROUTINE`, never `NOT_ASSESSED`. In every worked example this produces the same confidence outcome the addendum illustrated; it is a more consistent single definition of "contributing" shared by both the NOT_ASSESSED/ROUTINE decision and the confidence aggregation, rather than two separate definitions.
- Cross-link from Equipment Health is one-directional (Equipment Health → Service & Maintenance page) without deep-linking directly into the Maintenance Intelligence tab - Streamlit tabs have no native query-param addressing, and adding that plumbing wasn't part of the approved scope. Reverse direction (Service & Maintenance → Equipment Health) already existed from Phase 12.4 and is unchanged.
- `ui/maintenance_intelligence_data.get_overview()` computes one `calculate_maintenance_priority()` per equipment instance (no bulk/N+1-optimized query), matching the existing Service & Maintenance page's own per-equipment loop convention - reasonable at the current ~34-80 equipment scale for a page opened on demand (not auto-refreshing), unlike Phase 12's persisted-and-bulk-queried overview.

### Deferred / explicitly out of scope
Automatic work orders, automatic maintenance-interval optimization, predictive maintenance, RUL, failure probability, spare-parts prediction, technician assignment - all confirmed absent by source-inspection. A future `maintenance_intelligence_dismissals` table remains a possible, not-yet-designed Phase 13.1.

### Final semantic correction (post-implementation review)
A live/synthetic verification found that `recommendation_confidence` reused the SAME "was this dimension assessed" list that correctly drives ROUTINE vs `NOT_ASSESSED`, which let health's own confidence leak into a purely schedule-driven `REVIEW` (floor-only) recommendation even when health's rounded contribution to that recommendation was exactly zero. Corrected by splitting the single overloaded list into two:
- **`assessed_evidence`** - unchanged, still drives ROUTINE vs `NOT_ASSESSED` only. A valid Phase 12 assessment (`health_score is not None`) always counts here, even at zero penalty - "assessed, clean" must never look identical to "never assessed."
- **`recommendation_contributors`** - new, drives `confidence_basis`/`recommendation_confidence` only. Health/condition participates whenever the ordinary score-based path produced the recommendation, OR whenever it produced a real (already-rounded) nonzero contribution even under a floor - but is excluded when a confirmed-overdue floor is the sole reason for the recommendation and health's own rounded contribution is exactly zero. `events`/`maintenance_overdue`/`recent_movement`/`criticality` are unchanged (already gated on genuine materiality, never on assessedness).

No change to `priority_score`, score bands, the overdue floor's own behavior, Phase 12, or output vocabulary. 7 new regression tests added (`TestAssessedEvidenceVsRecommendationContributors`, Cases 1-6 plus one boundary case) - `tests/test_maintenance_intelligence.py` now 35 tests. Full suite: 690 tests (683 + 7 new), same 5 pre-existing unrelated failures, 0 new regressions.

### PHASE 13 — MAINTENANCE INTELLIGENCE: **COMPLETE**

---

## Phase 14 — Asset Performance & Reliability Analytics

**Completed:** 2026-08-17

### What this phase is
A deterministic, historical, backward-looking layer answering "how has measured engineering performance changed relative to valid historical behavior?" - a SEPARATE question from Equipment Health ("how healthy does it currently appear?") and Maintenance Priority ("does it need engineering attention now?"). No prediction, no RUL, no failure probability, no forecast, no MTBF/MTTR anywhere.

### Architecture - reuses Phase 8, never re-derives it
The central design decision: Phase 8's `engine.baseline_engine.compute_window_baseline()` (context-matched, robust-statistics, fault-excluded window comparison) and `._reference_recent_windows()` are called DIRECTLY by Phase 14, exactly as Phase 11.2/11.3 already reuse them across engine-module boundaries - an established, accepted pattern, not a new one. Phase 14 never recalculates a baseline itself. Evidence-quality tiering (STRONG/CONTEXT_MATCHED/LIMITED/INSUFFICIENT) is imported unmodified from `engine.savings_verification_evidence.classify_evidence_quality()` - the same vocabulary, not a parallel one.

### Files created
- `engine/performance_targets.py` - direction registry (below), performance-state/effectiveness vocabularies, all `SIMULATION_TUNING` thresholds (state-change/degrading/significantly-degrading cut points, sustained-degradation consecutive-observation count, persistence-materiality threshold, attention-ranking weights) - every constant named, centralized, documented.
- `engine/performance_engine.py` - pure calculate: `calculate_performance_observation()` (ongoing self-reference) and `calculate_maintenance_comparison()` (maintenance-anchored before/after). Never persists, never calls Phase 12/13's calculation entry points.
- `engine/performance_domain.py` - persistence read/write (mirrors `health_domain.py`).
- `engine/performance_orchestration.py` - materiality gating, consecutive-degrading-observation tracking, failure-isolated cycles, AND the round-robin one-work-item-per-tick support (`build_rotation()`/`process_one_work_item()`) the actual worker uses.
- `engine/performance_ranking.py` - Asset Attention Ranking, evidence-quality strictly separated from the score (see below).
- `engine/performance_migrator.py` - schema (below).
- `app/asset_performance_worker.py` + `deploy/systemd/asset_performance_worker.service` - the 11th service (approved, item J.1).
- `ui/asset_performance_data.py`, `ui/pages/20_Asset_Performance.py` - two tabs: Factory Attention Ranking and Equipment Detail.
- 7 test files (below).

### Files modified
- `ui/Home.py` - one navigation entry added ("Asset Performance").

### Database changes
Two new tables (additive, idempotent, SQLite Backup API migration - no Phase 1-13 table/migration touched):
- `asset_performance_observations` (append-only) - one row per persisted self-reference observation. Indexes: `(plant_id, instance_key, target_key, computed_at)`, `(plant_id, computed_at)`.
- `asset_performance_maintenance_comparisons` (append-only) - one row per persisted before/after comparison. Index: `(plant_id, instance_key, target_key, maintenance_log_id)`.

### Performance-dimension direction registry (item J.2 - full table for engineering review)
Mechanically derived from `engine.anomaly_targets.AnomalyRule.direction` (Phase 9) - "high"→LOWER_IS_BETTER, "low"→HIGHER_IS_BETTER, "both"→TARGET_RANGE - cross-confirmed against `engine.health_targets` factor descriptions (Phase 12.1) where both exist. Never guessed from a tag name. A dedicated test (`tests/test_performance_targets.py::TestDirectionMechanicallyMatchesAnomalyRules`) independently re-derives this table from Phase 9's own registry and fails on drift.

| Equipment Type | Target Key | Direction | Degradation-eligible | Source |
|---|---|---|---|---|
| plant_energy | Power_kW | LOWER_IS_BETTER | Yes | Phase 9 rule |
| plant_energy | non_production_demand_kw | LOWER_IS_BETTER | Yes | Phase 9 rule |
| air_compressor | Power_kW | LOWER_IS_BETTER | Yes | Phase 9 rule |
| air_compressor | Pressure | INFORMATIONAL_ONLY | No | none available |
| air_compressor | OutletTemp | INFORMATIONAL_ONLY | No | none available |
| air_header | Flow | LOWER_IS_BETTER | Yes | Phase 9 rule |
| air_header | Pressure | TARGET_RANGE | Yes | Phase 9 rule |
| air_header | compressed_air_specific_energy | LOWER_IS_BETTER | Yes | Phase 9 rule |
| chiller | Power_kW | LOWER_IS_BETTER | Yes | Phase 9 rule |
| chiller | LoadPct | INFORMATIONAL_ONLY | No | none available (context variable) |
| chiller | SupplyTemp | TARGET_RANGE | Yes | Phase 9 rule |
| chiller | ReturnTemp | TARGET_RANGE | Yes | Phase 9 rule |
| chiller | WaterFlow | TARGET_RANGE | Yes | Phase 9 rule |
| chiller | cop | HIGHER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| chiller | cooling_output_kw | TARGET_RANGE | Yes | Phase 9 rule |
| chilled_water_pump | Power_kW | LOWER_IS_BETTER | Yes | Phase 9 rule |
| chilled_water_pump | Flow | TARGET_RANGE | Yes | Phase 9 rule |
| chilled_water_pump | Frequency | INFORMATIONAL_ONLY | No | none available (operating-state variable) |
| chilled_water_pump | Vibration | LOWER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| chilled_water_pump | BearingTemp | LOWER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| chilled_water_pump | delta_p_bar | TARGET_RANGE | Yes | Phase 9 rule |
| chilled_water_pump | flow_per_kw | HIGHER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| water_supply_pump | Power_kW | LOWER_IS_BETTER | Yes | Phase 9 rule |
| water_supply_pump | Flow | TARGET_RANGE | Yes | Phase 9 rule |
| water_supply_pump | Frequency | INFORMATIONAL_ONLY | No | none available |
| water_supply_pump | Vibration | LOWER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| water_supply_pump | BearingTemp | LOWER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| water_supply_pump | delta_p_bar | TARGET_RANGE | Yes | Phase 9 rule |
| water_supply_pump | flow_per_kw | HIGHER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| ahu | FanPower | LOWER_IS_BETTER | Yes | Phase 9 rule |
| ahu | FanFrequency | INFORMATIONAL_ONLY | No | none available |
| ahu | FilterDP | LOWER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| ahu | SupplyAirTemp | TARGET_RANGE | Yes | Phase 9 rule |
| ahu | ReturnAirTemp | INFORMATIONAL_ONLY | No | none available |
| cold_room | RoomTemp | TARGET_RANGE | Yes | Phase 9 rule |
| cold_room | CompressorPower | LOWER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| production_process | MotorPower | LOWER_IS_BETTER | Yes | Phase 9 rule |
| production_process | MotorCurrent | INFORMATIONAL_ONLY | No | none available |
| production_process | ProcessTemp | TARGET_RANGE | Yes | Phase 9 rule |
| production_process | Vibration | LOWER_IS_BETTER | Yes | Phase 9 rule + Phase 12.1 factor |
| filling | Power_kW | LOWER_IS_BETTER | Yes | Phase 9 rule |

41/41 baseline targets covered, 33 degradation-eligible, 8 INFORMATIONAL_ONLY (never fabricated).

### Reference-selection behavior
Two modes, both built entirely on Phase 8's own `compute_window_baseline()`/`_reference_recent_windows()` - no competing baseline engine: (1) **ongoing self-reference** - Phase 8's own reference-vs-recent window split, refreshed every worker rotation; (2) **maintenance-anchored** - reference window frozen immediately before `maintenance_log.performed_at`, observed window eligible from `performed_at + 1 stabilization day` onward (generalizing Phase 11.2's intervention-anchored pattern).

### Context/normalization behavior
100% reused from Phase 8 (load/ambient/hour/production-state/product-category context matching, per equipment type) - Phase 14 introduces zero new normalization inputs. Where operating conditions can't be matched, evidence quality degrades to LIMITED/INSUFFICIENT rather than forcing a percentage from an incomparable comparison.

### Degradation-state logic
`classify_change()`: signed (direction-adjusted) percent change vs. reference. ≤ -5% → IMPROVING; (-5%, 15%) → STABLE; [15%, 30%) → DEGRADING; ≥30% → SIGNIFICANTLY_DEGRADING. TARGET_RANGE dimensions can never report IMPROVING (being at the reference already is the ideal - documented, not an oversight). INFORMATIONAL_ONLY dimensions report NOT_CLASSIFIED, distinct from INSUFFICIENT_EVIDENCE (good evidence, no defensible direction to judge it by).

### Persistence logic
Append-only, on material change (state/evidence-quality change or ≥5% percent-change shift) or 24h heartbeat - mirrors Phase 12.2's own convention exactly, with its own centrally-named `PERFORMANCE_OBSERVATION_MATERIAL_CHANGE_THRESHOLD_PCT`/`PERFORMANCE_HEARTBEAT_MAX_INTERVAL_HOURS`. Degradation persistence (item J.4) uses consecutive PERSISTED materially-degrading observations (`SUSTAINED_DEGRADATION_MIN_CONSECUTIVE_OBSERVATIONS = 3`, matching Phase 9's own modal persistence-period value) - never a calendar-duration guess; any human-readable "N days" in the UI is derived from real persisted `computed_at` timestamps.

### Worker/service architecture - a deliberate correction during implementation
Initially implemented as a full-sweep-per-cycle worker (Phase 12.2's pattern). Live testing against the real historian revealed Phase 14's per-target cost (raw `compute_window_baseline()` reads, two windows/target) is the SAME order of magnitude as `baseline_worker`'s own per-target cost (~2.5-5s/target), NOT Phase 12.2's cheap already-persisted-evidence reads (~0.1s/target). A full-sweep design would have kept the process "always busy," competing with `baseline_worker`/`anomaly_worker` for historian bandwidth - exactly what `baseline_worker`'s own docstring warns against. **Corrected to follow `baseline_worker`'s own established round-robin, one-work-item-per-5s-tick pattern instead** (`engine.performance_orchestration.build_rotation()`/`process_one_work_item()`) - a full rotation (~170 observation + 11 maintenance-comparison items, live-measured) takes several minutes, spread out, never one big sweep.

### Attention-ranking formula and component breakdown (mandatory correction implemented)
`attention_score = degradation_component + persistence_component + criticality_component + maintenance_ineffectiveness_component` - **evidence quality is deliberately ABSENT from this formula**. A dimension with INSUFFICIENT evidence is excluded from the calculation entirely (never contributes 0 as if "confirmed fine," never contributes a fabricated penalty). If EVERY dimension for an equipment is excluded this way, the equipment gets an explicit `INSUFFICIENT_EVIDENCE` attention_state with `attention_score=None` - never a fabricated low/neutral number. Evidence quality is reported as its own companion field (confidence in the DRIVING dimension) - a HIGH-degradation/LIMITED-evidence equipment is still ranked (LIMITED is usable evidence, just not the best tier), visibly marked LIMITED, never hidden or excluded. `degradation_component` is additionally gated on the dimension's own classified `performance_state` being DEGRADING/SIGNIFICANTLY_DEGRADING (not raw percent magnitude alone) - a real bug caught during test-writing (a STABLE-classified 1% "noise" change was contributing a nonzero score before this fix). Every component capped, explicit, `SIMULATION_TUNING`, fully returned for UI display - never an opaque single number. Criticality reused unmodified from `equipment.criticality`.

### Evidence-quality handling
Reused unmodified from Phase 11.2 (`STRONG`/`CONTEXT_MATCHED`/`LIMITED`/`INSUFFICIENT`) - never a parallel vocabulary.

### Maintenance before/after behavior
Frozen pre/post windows around `maintenance_log.performed_at` (1-day stabilization, 30-day bounded pre-window lookback), `compute_window_baseline()` on both, evidence-quality-classified, direction-aware `classify_effectiveness()` (IMPROVED/NO_MEASURABLE_CHANGE/WORSENED/INSUFFICIENT_EVIDENCE). Causal-language discipline enforced and tested: "Performance improved following maintenance," never "maintenance caused/fixed."

### UI changes
New page `20_Asset_Performance.py`, two tabs: **Factory Attention Ranking** (ranked table, KPIs, component-breakdown detail) and **Equipment Detail** (filterable dimension table, per-dimension history chart with reference-vs-observed lines, maintenance before/after markers) - both read-only, zero recalculation. Home.py gained one nav entry.

### Worker/service behavior
`asset_performance_worker.service` created (`deploy/systemd/asset_performance_worker.service`) - the 11th production service (approved, item J.1), `fcntl.flock` single-instance lock, `Restart=always`, round-robin 5s tick. **Not yet installed** (no sudo/TTY access this session) - handed off to you; migration was run directly against the live simulation database and a manual full-cycle run was used for live validation instead.

### Tests added
7 files, **99 tests**: `test_performance_targets.py` (6, incl. mechanical anomaly-rule-direction re-derivation), `test_performance_engine.py` (34, classification/percent-change edge cases + end-to-end observation/maintenance-comparison calculation against seeded historian data), `test_performance_persistence.py` (10, migration/insert/retrieval/append-only), `test_performance_orchestration.py` (26, materiality gating, consecutive-degrading tracking, idempotency, failure isolation, round-robin rotation), `test_performance_ranking.py` (9, the mandatory evidence/degradation separation), `test_performance_integration.py` (11, sentinels: no LLM, no predictive/RUL/MTBF/MTTR language, no consolidated score, Phase 12/13 untouched, no PLC/control writes), `test_performance_ui.py` (3, AppTest).

### Full regression result
789 tests (690 previous baseline + 99 new), same 5 pre-existing unrelated failures (`test_compressor_aliases` ×2, `test_typo_tolerant_aliases`, `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`), **0 new regressions**.

### Live simulator validation (real production data, `database/simulation/config.db`)
Migration run directly against the live database; a full `run_cycle()` executed once (604.8s, 170 observation targets + 11 maintenance comparisons across both plants, **0 failures**). Real persisted results:
- **Valid evidence + reference + comparison + classification**: `P01.UTILITY.CHL01.Power_kW` - reference 32.63 kW → observed 48.95 kW, **+50.0%**, **SIGNIFICANTLY_DEGRADING**, LOWER_IS_BETTER, LIMITED evidence (52 recent / 45 reference comparable samples).
- **Stable example**: `P01.UTILITY.AC01.Power_kW` - -0.7% change, STABLE.
- **Informational-only (correctly never classified)**: `P01.UTILITY.AC01.Pressure`/`OutletTemp` - NOT_CLASSIFIED despite real data existing.
- **Insufficient-evidence example**: `P01.HVAC.AHU01.FanPower` - "109 recent / 0 reference comparable sample(s)" - honestly reported, never fabricated as STABLE.
- **Maintenance before/after (insufficient-evidence case)**: all 11 real `maintenance_log` entries are dated exactly at `MODERN_DATA_BOUNDARY` (2026-08-09) - no pre-maintenance historian data exists before that boundary by construction, so every real comparison correctly reports `INSUFFICIENT_EVIDENCE` rather than fabricating a pre-value. A synthetic before/after IMPROVED/WORSENED/NO_MEASURABLE_CHANGE example is covered by `tests/test_performance_engine.py` instead (isolated fixture, real historian pipeline, not fabricated production data).
- **Attention ranking example**: `P01.UTILITY.CHL01`/`CHL02` topped the real ranking (score 46.2, driven by Power_kW SIGNIFICANTLY_DEGRADING, LIMITED evidence clearly marked) - confirmed via `ui.asset_performance_data.get_attention_ranking()`.
- **AppTest**: `ui/pages/20_Asset_Performance.py` rendered against this same live database with **zero exceptions** (both tabs, ranking-detail selection, dimension-detail selection with a real DEGRADING equipment). `ui/Home.py`, `ui/pages/19_Equipment_Health.py`, `ui/pages/4_Service_and_Maintenance.py` re-confirmed unaffected.
- `sustained_degradation_count = 0` on this first-ever run - honestly expected (3 consecutive PERSISTED observations haven't had time to accumulate yet on a brand-new table); the mechanism itself is proven via isolated-fixture tests.

### Known limitations
No real "before" maintenance evidence exists yet in this dataset (all `maintenance_log` entries dated at the modern-data boundary) - before/after comparisons will only show real IMPROVED/WORSENED results once genuine pre-boundary-relative maintenance events accumulate. `asset_performance_worker.service` is not yet installed (handoff pending your `sudo` commands). No retention/downsampling policy yet for the two new history tables (same disclosed limitation Phase 12.2 carries).

### Confirmation - Phase 12/13 unchanged
No file under Phase 12's or Phase 13's ownership was modified. `engine/performance_engine.py`/`performance_ranking.py` never call `health_engine.calculate_health()` or `maintenance_intelligence_engine.calculate_maintenance_priority()` (source-inspection tested) - confirmed by `tests/test_performance_integration.py::TestPhase12And13Unchanged`.

### Confirmation - no predictive/RUL functionality
No failure prediction, no RUL, no predicted failure date, no forecast/extrapolation claim (only backward-looking, already-elapsed window comparisons), no MTBF/MTTR, no ML forecasting, no LLM anywhere in Phase 14 - confirmed by source-inspection sentinel tests (`tests/test_performance_integration.py::TestNoPredictiveOrRULLanguage`).

### PHASE 14 — ASSET PERFORMANCE & RELIABILITY ANALYTICS: **COMPLETE**

---

## Post-Phase-14 cleanup — simulator operating realism, Live Data nav hide

**Completed:** 2026-08-17 (not a numbered phase - a targeted cleanup pass)

### 1. Simulator operating realism

**Reused the correct existing layer.** Phase 2's `shift_definitions` table has existed since Phase 2 but was never seeded or actually used to gate anything (confirmed: 0 rows, `production_batch_simulator._resolve_shift_id()` only ever returned `None`). Seeded one factory-wide "Weekday Production" row (09:00-18:00, Mon-Fri) via a new idempotent `engine/seed_shift_definitions.py`, and added `simulator/plant_context.is_within_active_shift()` to actually read it - no competing schedule mechanism introduced.

**Equipment treated individually, never one global multiplier:**
- **production_process/filling** (mixer/disperser/mill/filling machine) - already fully driven by `production_batches`; only changed HOW OFTEN new batches start (`production_batch_simulator.tick()` now scales `START_BATCH_PROBABILITY` by `plant_context.production_start_probability_factor(now, within_shift)`: 1.0 in-shift, 0.08 weekday off-hours, 0.30 Saturday, 0.08 Sunday). Since Phase 8's own `_plant_production_state_at()`/`_production_context_at()` read `production_batches` directly (confirmed by inspection), this change automatically and correctly flows into Phase 8's existing `production_state` context dimension - no separate Phase 8 change needed here.
- **air_compressor** - genuinely STOPS most of the time outside production hours (compressed-air demand is production-driven) - RunStatus now follows `plant_context.schedule_stoppable_on_probability()` (0.98 in-shift, 0.12 weekday off-hours, further scaled down Sat/Sun) instead of the generic ~99.8%-always-on branch.
- **chiller, chilled_water_pump, water_supply_pump, ahu, cold_room** - RunStatus **unchanged** (stay in the existing always-on branch - genuinely continuous-duty systems). Only their power/current baseline settles toward a lower, never-zero level outside hours, via `plant_context.schedule_load_factor()` - a per-equipment-type off-hours fraction (chiller/chilled water pump 0.55, water supply pump 0.30, AHU 0.45, cold room 0.75) further scaled by day-of-week (Saturday ×0.75, Sunday ×0.45 on top of the weekday-off-hours fraction).
- **Base building load** (`plant_context.plant_base_load_kw()`) - already had a day/night split; added a weekend factor (Saturday ×0.75, Sunday ×0.6) alongside it.
- No fire/life-safety/security equipment TYPE exists in the current tag-simulated dataset to inadvertently disable - confirmed by inspection of the full equipment-type registry.
- **Known, disclosed gap**: `air_header` (compressed-air network) Flow/Pressure is NOT yet schedule-adjusted - those measurements aren't in `PRODUCTION_LOAD_SENSITIVE_MEASUREMENTS` (power/current/speed only), so the header's own baseline doesn't yet shift with the compressors feeding it. Flagged as a natural follow-up, kept out of this pass to stay surgical.

**A real bug caught and fixed during live-value testing:** the first implementation applied the schedule adjustment as a separate ADDITIVE push term (mirroring `production_push`'s own shape). Live testing showed this could exceed the `[low-band, high+band]` trend clamp entirely, bottoming water-supply-pump power out at a hard 0 regardless of the intended factor - violating the explicit "never zero" requirement. **Fixed** by instead computing a schedule-adjusted `baseline` (a convex blend between the tag's `profile["low"]` and its normal baseline, weighted by the schedule factor) that the existing `reversion` term settles toward - bounded by construction between `profile["low"]` and the full baseline, never below, never zero.

**Phase 8 analytics protected against misreading the new realism as degradation** (the explicit risk this whole change was reviewed against): `day_type` (already an existing, proven context dimension - `cold_room`/`plant_energy`/`air_header` already used it) added to `air_compressor`, `chiller`, `chilled_water_pump`, `water_supply_pump`, `ahu`'s `context_dimensions` in `engine/baseline_targets.py`. Without this, weekday and weekend samples for these equipment types would blend into one context bucket and either mask a real anomaly or flag an ordinary weekend reading as one - exactly the risk under review. This is Phase 8's OWN existing mechanism, reused, not a competing normalization layer. Expected, honest side effect: some of these equipment types may show more `bootstrap`/`insufficient` baseline coverage initially until enough weekday+weekend history accumulates under the finer-grained context - this is Phase 8's own correct, intentional behavior for "not enough data yet," not a defect.

**Natural variation preserved:** existing per-tag noise, the exponential reversion dynamic (smooth transitions as the target baseline itself changes at shift boundaries, never an instant step), and 4 distinct tiers (in-shift / weekday off-hours / Saturday / Sunday) rather than a rigid two-level switch, satisfy the "not a rigid timer" requirement.

### Files created
`engine/seed_shift_definitions.py`, `tests/test_simulator_scheduling.py` (22 tests).

### Files modified
`simulator/plant_context.py` (scheduling section: `is_within_active_shift()`, `schedule_load_factor()`, `is_schedule_stoppable_canonical()`, `schedule_stoppable_on_probability()`, `production_start_probability_factor()`, weekend factor added to `plant_base_load_kw()`), `simulator/tag_dataset_model.py` (`self._within_shift` cached per cycle; schedule-adjusted baseline in `_update_real()`; stoppable-equipment branch in `_update_bool()`), `simulator/production_batch_simulator.py` (schedule-aware `start_probability` in `tick()`), `engine/baseline_targets.py` (`day_type` added to 5 equipment-type profiles), `ui/Home.py` (Live Data nav entry commented out, see below).

### Representative before/after simulated values (isolated deterministic test fixture, `unittest.mock.patch` on `datetime.now()`, 150 cycles per scenario)
| Scenario | Chiller | Chilled Water Pump | Water Supply Pump | AHU Fan | Cold Room Compressor | Air Compressor RunStatus |
|---|---|---|---|---|---|---|
| Weekday production (Mon 14:00) | 49.2 kW | 17.0 kW | 17.9 kW | 14.3 kW | 5.3 kW | 144/150 on |
| Weekday after-hours (Mon 23:00) | 43.0 kW | 14.5 kW | 12.9 kW | 11.8 kW | 4.8 kW | 16/150 on |
| Saturday 14:00 | 41.0 kW | 13.8 kW | 12.3 kW | 11.3 kW | 4.4 kW | 15/150 on |
| Sunday 14:00 | 38.7 kW | 12.8 kW | 11.7 kW | 10.7 kW | 4.0 kW | 11/150 on |

Continuous-duty equipment (chiller/pump/AHU/cold room) never drops to zero and never stops (RunStatus unaffected); air compressor genuinely stops most of the time outside production hours while still showing occasional on-cycles (natural variation, never a rigid 0). Ordering is consistently Weekday production > Weekday after-hours > Saturday > Sunday, per requirement.

### 2. Live Data hidden from navigation
Removed the `st.Page("pages/2_Live_Data.py", ...)` line from `ui/Home.py`'s `general_pages` list (commented out with an explanatory note, not deleted). `ui/pages/2_Live_Data.py` itself, its data access, and all supporting code are **completely untouched** (confirmed: zero git diff on the file). No other file in the app calls `st.switch_page("pages/2_Live_Data.py")` (repo-wide grep) - there is no dangling internal reference to break. `ui/Home.py` re-verified rendering with zero exceptions after the change (AppTest).

### 3. Phase 14 worker deployment - manual installation commands
`asset_performance_worker.service` still requires manual installation (no sudo access this session) - see the implementation report for the exact commands.

### Tests
22 new (`tests/test_simulator_scheduling.py`): pure scheduling-factor math (weekday/off-hours/Saturday/Sunday ordering, never-zero), real `shift_definitions` seeding/lookup, `TagDatasetSimulator`-level value shifts (chiller settles lower off-hours, air compressor mostly stops outside hours/mostly runs in-shift), `production_batch_simulator` schedule-aware start-rate, `day_type` context-dimension presence.

### Full regression result
811 tests (789 previous baseline + 22 new), same 5 pre-existing unrelated failures, **0 new regressions**. Existing simulator/baseline-engine test suites (`test_plant_context.py`, `test_production_batch_simulator.py`, `test_tag_dataset_model.py`, `test_baseline_engine.py` - 84 tests) all pass unchanged, with no assertion needing to be weakened or rewritten.

### Architectural concerns discovered
1. The additive-push-term bug above (found and fixed before delivery, not left for you to find).
2. `air_header` Flow/Pressure schedule-adjustment gap (disclosed, deliberately deferred - see above).
3. Adding `day_type` to 5 equipment types' context dimensions will, until enough weekday+weekend history accumulates, temporarily reduce some equipment's baseline coverage tier (mature→bootstrap territory) - an honest, expected, self-resolving side effect of Phase 8's own existing coverage-classification logic, not a defect requiring a fix.

### Follow-up — air_header schedule awareness + cross-equipment consistency review

**Completed:** 2026-08-17 (same cleanup pass, not a new phase)

**1. air_header now schedule-aware, without a second scheduling mechanism.** `plant_context.air_header_schedule_factor(measurement, now, within_shift)` - a dedicated, small function (not a change to `schedule_load_factor()`'s existing signature/behavior, zero risk to already-tested equipment), since Flow and Pressure need two DIFFERENT fractions under the same `UTILITY.AIRHDR` prefix:
- **Flow**: 1.0 in-shift → 0.20 weekday off-hours → further reduced Sat (×0.75) / Sun (×0.45) - "substantially lower," consistent with air compressors now being mostly stopped outside hours.
- **Pressure**: 1.0 in-shift → 0.92 weekday off-hours → 0.97/0.93 relative Sat/Sun multipliers (its OWN, much gentler pair, not the shared Saturday/Sunday constants - see below) - stays close to its controlled setpoint, never collapses.

**A real bug caught during this pass**: air_header's `Flow` TAG_PROFILE (60-100) is narrow relative to its own low bound - blending toward `profile["low"]` alone (the formula already accepted for chiller/pump/AHU/cold room) only ever produced a shallow ~20% dip, not "substantially lower." **Fixed** by introducing `SCHEDULE_WIDE_ANCHOR_PREFIXES` (`UTILITY.AC`, `UTILITY.AIRHDR`) and a shared `_schedule_blended_baseline()` helper: these narrow-band profiles blend toward `profile["low"] - band` (floored at 0 - the same floor the existing trend clamp already uses) instead of `profile["low"]` alone - still bounded, never zero, genuinely lower. The already-accepted formula for chiller/chilled water pump/water supply pump/AHU/cold room is **completely unchanged** (still anchors on `profile["low"]` directly).

Also caught: reusing the shared `SATURDAY_RELATIVE_FACTOR`/`SUNDAY_RELATIVE_FACTOR` (0.75/0.45) on Pressure's already-small 0.92 off-hours fraction would have compounded into a 31-59% pressure *sag* on weekends - directly contradicting "pressure should not simply collapse." Fixed with Pressure's own dedicated, much gentler weekend multipliers.

**2. Cross-equipment consistency review** - a real inconsistency found and fixed: `air_compressor`'s `RunStatus` was already correctly going to 0 most of the time outside production hours (previous pass), but its `Power_kW` had **no corresponding reduction at all** - it could show near-full-load power while nominally stopped. Fixed by adding `UTILITY.AC` to `SCHEDULE_OFF_HOURS_LOAD_FACTOR` (0.08, the lowest fraction of the group, via the same wide-anchor mechanism as air_header's Flow). All other reviewed items were confirmed already correct by construction, not requiring a code change:
- Saturday can never exceed weekday-production for any equipment - every configured off-hours fraction is `< 1.0` by construction (added a structural test asserting this over the whole registry).
- Sunday `<=` Saturday for every equipment type - deterministic given `SUNDAY_RELATIVE_FACTOR < SATURDAY_RELATIVE_FACTOR`.
- 24/7 equipment (chiller/chilled water pump/water supply pump/AHU/cold room) confirmed never reaching zero across 150 simulated cycles on a Sunday.
- Production batch start-rate confirmed rare (not "frequent") outside the configured shift, at all 4 representative time points.
- Building base load confirmed never dropping unrealistically close to zero even in the worst case (Sunday 3am compounds both the night and weekend factors).

**3. Representative operating-period values** (isolated deterministic fixture, `unittest.mock.patch` on `datetime.now()`, 150 cycles/scenario, seeded RNG):

| Scenario | Air Header Flow | Air Header Pressure | Air Compressor Power | Air Compressor RunStatus | Chiller (unchanged formula, for comparison) |
|---|---|---|---|---|---|
| Monday 10:00 (production) | 82.5 Nm3/h | 6.49 bar | 32.5 kW | 146/150 on | 48.9 kW |
| Monday 20:00 (after-hours) | 34.5 Nm3/h | 6.37 bar | 18.9 kW | 23/150 on | 42.6 kW |
| Saturday 10:00 | 31.5 Nm3/h | 6.33 bar | 18.6 kW | 15/150 on | 40.7 kW |
| Sunday 10:00 | 27.9 Nm3/h | 6.27 bar | 18.2 kW | 7/150 on | 38.4 kW |

Flow drops ~66% from production to Sunday while Pressure stays within ~3.4% of its production value throughout - "Flow should respond much more strongly than Pressure" confirmed numerically. Air compressor power now tracks its own RunStatus correctly (both drop together).

### Files modified (this follow-up)
`simulator/plant_context.py` (`air_header_schedule_factor()`, `AIR_HEADER_*` constants, `SCHEDULE_WIDE_ANCHOR_PREFIXES`, `UTILITY.AC` added to `SCHEDULE_OFF_HOURS_LOAD_FACTOR`), `simulator/tag_dataset_model.py` (`_schedule_blended_baseline()` helper, air_header elif branch in `_update_real()`). No other file touched - `engine/baseline_targets.py`'s `day_type` additions, the production-batch schedule, the compressor-stoppable mechanism, and Live Data's hidden-nav state are all unchanged from the previous pass, per explicit instruction.

### Tests
17 new (`tests/test_simulator_scheduling.py`, now 39 total): air_header pure-factor math (6), air_header live-value ordering/never-negative (4), cross-equipment structural consistency (3), air-compressor RunStatus/power consistency (1), 24/7-never-zero (1), base-load-never-near-zero (1), plus the 4 required representative time points (Monday 10:00/20:00, Saturday 10:00, Sunday 10:00) exercised across the above.

### Full regression result
828 tests (811 previous baseline + 17 new), same 5 pre-existing unrelated failures, **0 new regressions**. Full simulator-related suite (`test_plant_context.py`, `test_production_batch_simulator.py`, `test_tag_dataset_model.py`, `test_baseline_engine.py`, `test_simulator_scheduling.py` - 123 tests) passes unchanged.

### Remaining simulator realism concerns (disclosed, not fixed this pass)
- `air_header`'s Flow/Pressure are still not coupled to the SPECIFIC per-cycle compressor RunStatus outcome (schedule-average only, consistent with every other equipment's own relationship to its schedule, and with the explicit "do not tightly couple cycle-by-cycle" instruction) - a header pressure dip during a rare in-shift moment when compressors happen to be down is not separately modeled.
- Air compressor's own narrow TAG_PROFILES band (22-30 kW) means its off-hours floor (~14 kW) is a real but modest ~40-45% reduction, not vastly more dramatic - a defensible outcome given the wide-anchor fix, but a genuinely wider profile band would allow a sharper drop if ever desired.
- No fire/life-safety/security equipment type exists in the current tag-simulated dataset (reconfirmed, unchanged from the previous pass).

### Final commissioning and verification pass (production deployment, 2026-08-17)

Read-only, live-system verification against the actual production install
after all sudo deployment steps (`asset_performance_worker.service`
installed/enabled/started, `production_simulator.service` and
`streamlit.service` restarted). No code changes were made in this pass -
zero deployment-blocking defects were found.

- **asset_performance_worker.service**: active/enabled, unit file matches
  repo, single-instance lock verified (manual duplicate start correctly
  refused, exit 1), round-robin ticking confirmed live in the journal
  (~5s cadence), stable PID, ~42% average CPU (not saturated).
- **production_simulator.service / streamlit.service**: both restarted
  cleanly (new PIDs, `NRestarts=0`), schedule-aware simulator code
  confirmed live via a direct `production_batches` query (`shift_id=1`
  populated on new rows).
- **shift_definitions**: exactly one correctly-seeded `plant_id=NULL`
  "Weekday Production" row; idempotent re-seed re-confirmed.
- **Live schedule-state audit**: real time was Monday 2026-08-17, mid-shift
  - in-shift tag values (chiller, CHWP, WSP, AHU, cold room, air
    compressor, air header, Main Incomer) all matched expected full-load
    figures from prior isolated testing. Off-hours/Saturday/Sunday states
    are not observable live at this time of day, so the existing 39-test
    deterministic `tests/test_simulator_scheduling.py` suite (which
    freezes `datetime.now()` rather than manipulating the system clock)
    was re-run standalone as the substitute evidence: **39/39 passing**,
    covering continuous-duty equipment, air compressor, air header
    Flow/Pressure, production batch-start scaling, base building load,
    and cross-equipment schedule consistency (e.g. air compressor
    `RunStatus` vs `Power_kW` no longer contradict each other off-hours).
- **Phase 8 baseline `day_type` consequence**: honestly queried against
  the live database - the 5 equipment types with `day_type` added to
  their context dimensions show a real mixed bootstrap/mature/unavailable
  confidence distribution (expected, since `day_type` narrows which
  historical windows qualify as matching context); no confidence gate was
  weakened to paper over this.
- **Asset Performance data-flow chain / DB integrity**: `asset_performance_observations`
  and `asset_performance_maintenance_comparisons` growing append-only,
  zero duplicate rows, growth-then-idempotent-skip pattern matches the
  round-robin rotation design.
- **Maintenance comparison causal-language re-check**: 0 violations across
  all real maintenance-comparison rows in the live database.
- **Asset Attention Ranking semantics re-verified against the live DB**:
  of 38 equipment instances, 24 are `INSUFFICIENT_EVIDENCE` with
  `attention_score=None` (never fabricated, never 0), 14 are `RANKED`
  with a real numeric score (including legitimate `0.0` for equipment
  showing no degradation) - confirms the mandatory Phase 14 modification
  (evidence quality never inflates the score) is genuinely in effect in
  production, not just in unit tests.
- **Asset Performance UI smoke test** (Streamlit `AppTest` against the
  live database, zero exceptions): evidence quality (`LIMITED`/
  `INSUFFICIENT`) renders in its own column, separate from performance
  `State`; `INSUFFICIENT_EVIDENCE` dimensions show `Reference=NaN`,
  `Change=Unavailable` rather than a fabricated number; the page's own
  copy states "no LLM, no prediction, no Remaining Useful Life anywhere
  on this page"; no causal maintenance language found.
- **Live Data navigation**: confirmed still hidden (commented, not
  deleted) and the page file itself still exists untouched.
- **10-page Streamlit `AppTest` regression** (Home, SCADA Floor Plan,
  Energy Dashboard, Anomalies, Energy Opportunities, Savings
  Verification, Equipment Health, Service & Maintenance, Asset
  Performance, Ask AI): zero exceptions on all 10; Ask AI unchanged.
- **Historian continuity**: `plc_data` row count grew continuously and
  without duplication across repeated checks during this pass (final
  count 10,753,887 rows, `MAX(time)` tracking live).
- **SQLite concurrency**: journals for all 10 database-writing services
  swept over the preceding hour for `database is locked`/
  `OperationalError`/`disk I/O`/`malformed`/`corrupt` - zero hits.
- **Service inventory**: all 11 expected systemd services active and
  enabled.
- **Full regression suite**: **828 tests, same 5 pre-existing unrelated
  failures (`test_compressor_aliases` x2, `test_typo_tolerant_aliases`,
  `test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`
  - all pre-existing typo-correction/alias false positives, unrelated to
  Phase 14 or the simulator work), 0 new regressions.**

No genuine deployment-blocking defect was found, so no functional changes
were made during this pass. Phase 15 was explicitly NOT started.

---

## Phase 15 — AI Interpretation Layer

**Completed:** 2026-08-17

### What this phase is

Turns the structured deterministic engineering intelligence built in
Phases 6-14 into natural-language engineering assistance:
`Deterministic Analytics -> Structured AI Context -> LLM Interpretation
-> Engineer`. The LLM is explicitly never an engineering calculation
engine - it only explains, summarizes, and compares already-computed
results. The existing 10-intent tag-level Ask AI pipeline (Phase 1-era)
is completely unmodified and runs side by side with this new layer.

### Files created

- `ai/interpretation_intent.py` - domain-intent classifier (phrase-based,
  same architectural pattern as `engine/concept_extractor.py`).
- `ai/context_builder.py` - the Structured AI Context Layer
  (`build_equipment_ai_context()`, `build_comparison_ai_context()`,
  `build_factory_ai_context()`) - pure reads, no LLM inside, fully
  testable standalone.
- `ai/interpretation_prompt_builder.py` - Phase 15's own prompt
  templates, boundary language, and deterministic fallback renderer -
  deliberately separate from `ai/prompt_builder.py` (untouched).
- `ai/grounding_guard.py` - lightweight, deterministic post-generation
  check for unsupported authoritative claims.
- `tests/test_phase15_interpretation_intent.py`,
  `tests/test_phase15_grounding_guard.py`,
  `tests/test_phase15_prompt_builder.py`,
  `tests/test_phase15_equipment_resolution.py`,
  `tests/test_phase15_context_builder.py`,
  `tests/test_phase15_ask_integration.py` - 80 new tests.

### Files modified (all additive - no existing behavior changed)

- `engine/models.py` - added `EquipmentCandidate`, `EquipmentResolution`,
  `EquipmentComparisonResolution` dataclasses.
- `engine/industrial_query_engine.py` - added `resolve_equipment()`,
  `resolve_equipment_pair()`, `split_comparison_entities()`, and a
  Phase-15-only equipment-level plant-tie helper. `query()` and every
  existing tag-level method are byte-for-byte unchanged.
- `engine/maintenance_intelligence_engine.py` - added `assessed_evidence:
  list[str]` as a new field on `MaintenancePriorityResult`, populated
  from the already-computed local variable of the same name. No change
  to `maintenance_priority`, `priority_score`, `recommendation_confidence`,
  `confidence_basis`, or any persistence/scoring logic - confirmed by
  the existing 35 Phase 13 tests still passing unchanged.
- `app/ask.py` - added the `AskResult` dataclass and
  `AskEngine.ask_structured()` plus its private helpers. `AskEngine.ask()`
  (the existing string interface) is unchanged; `ask_structured()`
  delegates to it verbatim for every non-Phase-15 question.
- `ui/pages/1_Ask_AI.py` - now calls `ask_structured()`, shows resolved
  equipment/View Evidence/source timestamps, and reads one query param
  (`ask_equipment`) for cross-page entry. Session equipment context lives
  in `st.session_state["ask_equipment_context"]` only.
- `ui/pages/19_Equipment_Health.py`, `ui/pages/4_Service_and_Maintenance.py`
  (Maintenance Intelligence tab), `ui/pages/20_Asset_Performance.py`,
  `ui/pages/17_Energy_Opportunities.py` - one "💬 Ask AI about this
  equipment" button each, using `st.switch_page(..., query_params=...)`
  (the same `st.query_params` deep-link mechanism `19_Equipment_Health.py`
  already used for its own `?equipment_id=` cross-link, confirmed via
  audit before reuse rather than assumed).

### Existing Ask AI / provider components reused, unchanged

`ai/ai_provider.py`, `ai/providers/*` (Ollama/OpenAI switch, unchanged),
`engine/concept_extractor.py` + `engine/industrial_query_engine.py`'s
existing tag-level scoring (`_equipment_score()`, `_fuzzy_equipment_score()`,
`DESIGNATOR_PATTERN` all reused verbatim by the new equipment resolver),
`ai/event_store.py` (reused for the Events domain - the dead
`ai/event_timeline.py` was deliberately NOT resurrected),
`ui/health_data.py`, `ui/maintenance_intelligence_data.py`,
`ui/asset_performance_data.py`, `ui/data_access.py`,
`ui/savings_verification_data.py`, `engine/health_evidence.py`,
`simulator/plant_context.py` (`get_production_state()`,
`is_within_active_shift()`) - every domain read goes through an
already-existing Phase 6-14 function, never a new calculation.

### Structured AI Context architecture / intent-selective retrieval

`ai/context_builder.py`'s `DOMAINS_BY_INTENT` maps each of the 7
single-equipment intents to a bounded domain list (e.g.
`HEALTH_EXPLANATION` fetches only Health + Anomalies + Events;
`ENERGY_REVIEW` fetches only Energy Opportunity + Anomalies + Savings
Verification) - confirmed live and by automated test that unrelated
domains are never queried for a narrow question (e.g. `asset_performance`
is never present in a `HEALTH_EXPLANATION` context). `GENERAL_ENGINEERING_QUERY`
is the only intent that fetches the full bounded set, enabling genuine
cross-domain synthesis without over-fetching for narrow questions.
`FACTORY_SUMMARY` and `COMPARISON` use their own dedicated context
builders. Every domain sub-dict is `{"available": True, ...}` or
`{"available": False, "reason": "..."}` - never silently omitted.

### Canonical entity resolution

`IndustrialQueryEngine.resolve_equipment()` reuses the existing tag-level
designator/fuzzy scoring (zero duplicated matching logic) but resolves
to equipment identity (`instance_key`) rather than a tag. Confirmed live:
`"AC01 pressure"` still resolves via the completely unchanged tag-level
path; `"why is WSP01 unhealthy"` resolves via the new equipment path.

### Ambiguity handling

**Hard correction implemented and confirmed live**: unlike the tag-level
`_break_plant_ties()` (which defaults to the lexicographically-first
plant when none is named), `resolve_equipment()` NEVER silently picks a
plant. Live: `"Why is WSP01 unhealthy?"` (no plant named) ->
`clarification_required`, listing both `P01.WATER.WSP01` and
`P02.WATER.WSP01` as equal, unresolved candidates. An explicit plant
qualifier (`"P01 WSP01"`) resolves unambiguously. The `had_equipment_terms`
signal that distinguishes "a real (if ambiguous) equipment reference was
named" from "a bare follow-up naming nothing" was deliberately built on
the designator-pattern/high-score signal, NOT on raw `equipment_terms`
non-emptiness - the latter was found, before shipping, to be fooled by
the project's own standing "leftover generic word" landmine (a bare
follow-up like `"has it improved since yesterday"` leaves `('improved',)`
in `equipment_terms`, which would have made a context-less follow-up
look like it named real equipment).

### Multi-equipment comparison

`split_comparison_entities()` + `resolve_equipment_pair()` resolve each
side of a comparison INDEPENDENTLY (never the same text matched twice).
Confirmed live: `"Compare P01 WSP01 and P02 WSP01"` resolved to two
distinct entities (`P01.WATER.WSP01` / `P02.WATER.WSP01`) with fully
separate Health/Maintenance/Performance/Energy sections, no invented
combined score, and P02's genuinely `INSUFFICIENT_EVIDENCE` Asset
Performance state preserved honestly (not forced into a number) even
while P01's own Performance was a real, scored value in the same answer.

### Session-safe follow-up context

Verified `AskEngine` is already instantiated per browser session
(`st.session_state["ask_engine"]`, confirmed by reading
`ui/pages/1_Ask_AI.py` before designing this). Per the approved
correction, `context_equipment` is still an explicit, caller-supplied
parameter on every call - `AskEngine` never stores it as an attribute
(confirmed by a dedicated test that inspects every attribute on the
engine after a call and asserts none of them equal the passed-in
instance_key). The canonical value lives only in
`st.session_state["ask_equipment_context"]` in the UI. Live-confirmed
behavior: a bare follow-up ("what should maintenance check?") correctly
reused the prior turn's `P01.WATER.WSP01` context; a later explicit
`"Is P01 AC01 in attention?"` correctly overrode it to `P01.UTILITY.AC01`;
and naming an ambiguous equipment (`"WSP01"`) while a prior context was
set correctly surfaced the plant clarification rather than silently
reusing the old context.

### Equipment Health / Maintenance Intelligence / Asset Performance integration

Each reads its own already-persisted/on-demand result via the existing
UI data-access layer - never recalculates. `assessed_evidence` (the
evidence-considered set) and `confidence_basis` (the evidence that
actually drove the recommendation) are both exposed as separate fields,
confirmed live to reconcile exactly against a direct
`calculate_maintenance_priority()` call for the same equipment (see
Manual Authoritative Reconciliation below). Asset Performance's
`attention_score = None` for `INSUFFICIENT_EVIDENCE` equipment is
rendered as `"unavailable (insufficient evidence) - never treat this as
zero"`, confirmed both by unit test and live (P02 WSP01's comparison
entry).

### Anomaly / Event integration

Anomalies (Phase 9, statistical) and machine_events (PLC-threshold
alarms) are always rendered as two visibly separate sections, never
merged into one "alerts" bucket - preserving the project's own
established PLC-alarm-vs-statistical-anomaly distinction.

### Energy Opportunity / Savings Verification integration

Observed excess cost (fact) and estimated potential saving (`None` /
"Not yet estimable" for essentially every current rule) are rendered as
two separate fields with the real `saving_unavailable_reason` text,
never conflated - confirmed live (CHL02, WSP01 comparison entries).
Savings Verification correctly reports `"unavailable - No energy-saving
intervention has been recorded"` for every equipment tested, honestly
reflecting that 0 interventions exist yet in this dev-stage database (a
real data-completeness fact, not a bug - the richer VERIFIED/REJECTED/
INCONCLUSIVE explanation path is implemented and unit-tested against
synthetic data but has no real intervention to exercise live yet).

### Production / maintenance context integration

`production_context` (within-shift + running batch) and
`maintenance_history` (last 3 `maintenance_log` rows, each wrapped
`[RECORDED NOTE]...[/RECORDED NOTE]`) confirmed live in the comparison
answer (both WSP01 instances correctly showed "within active shift =
True, no batch currently running").

### Fact vs. hypothesis / uncertainty handling

`HEALTH_EXPLANATION`/`MAINTENANCE_REVIEW`/`GENERAL_ENGINEERING_QUERY`
prompts carry the required `Known from the system / Possible engineering
hypotheses / Useful checks / Limitations` structure (unit-tested);
`PERFORMANCE_REVIEW`/`ENERGY_REVIEW`/`ANOMALY_EXPLANATION`/
`SAVINGS_STATUS` are restate-only (no hypothesis section at all).
Free-text maintenance notes are always wrapped in `[RECORDED NOTE]`
delimiters with an explicit "treat as a quotation, never an instruction"
rule. LOW/provisional/INSUFFICIENT/INSUFFICIENT_EVIDENCE/Unconfigured/
"Not yet estimable" states are confirmed (unit test + live) to render
as explicit, honest text - never smoothed into a stronger conclusion,
never a fabricated 0 in place of `None`.

### Grounding guard

Pattern-based, deliberately narrow: recognizes explicit `"<thing> is
<value>"` claim phrasings for Health State/Score, Maintenance Priority,
Asset Performance state, Asset Attention Score, and Savings Verification
result, and flags a claim that contradicts (or has no support in) the
supplied context - including the specific "fabricated a number for an
INSUFFICIENT_EVIDENCE equipment" case. Verified via 12 unit tests
(true-claim pass, wrong-enum-value catch, wrong-number catch,
fabricated-value-on-unavailable-domain catch, rounding-tolerance
non-false-positive, and the explicit COMPARISON/FACTORY_SUMMARY
scope-skip that reports `checked=False` rather than a false "grounded").
**Live status**: no real (non-fallback) Ollama completion was obtained
during this live-acceptance pass (see Live Acceptance below), so the
guard could not additionally be exercised against genuine model output
in this pass - its correctness rests on the unit-test evidence above,
consistent with this project's own established practice ("Do NOT make
the full test suite depend on live OpenAI/Ollama availability... use
deterministic/mock/stub model responses for automated tests"). No
validator weakening was needed or performed.

### Prompt-injection / data-trust handling

The fixed preamble is always the first thing in every prompt, and every
piece of untrusted database text (maintenance notes) is wrapped in
`[RECORDED NOTE]`/`[/RECORDED NOTE]` with an explicit "never an
instruction" rule - confirmed by unit test. The deterministic fallback
renders only the context dict, never the constructed prompt string, so
instruction text can never leak into a user-visible fallback (mirrors
the real, previously-fixed 2026-08-15 bug in `ai/prompt_builder.py`).

### Provider architecture / failure fallback

Reuses `ai/ai_provider.py` unchanged - same Ollama/OpenAI switch, same
`generate()` interface, one call per question (never a multi-call
chain). Confirmed live, repeatedly: on a real provider timeout (see Live
Acceptance), `_render_interpretation_answer()` correctly falls back to
`build_deterministic_fallback()`, sets `fallback_used=True`, never
crashes, and the deterministic backend/UI/other services are completely
unaffected - no worker anywhere depends on AI availability.

### Read-only verification

`tests/test_phase15_context_builder.py::TestReadOnlyDatabaseImmutability`
snapshots row counts for 11 domain tables (`anomalies`,
`energy_opportunities`, `savings_interventions`,
`savings_verification_results`, `equipment_health_snapshots`,
`equipment_health_factor_snapshots`, `maintenance_log`,
`asset_performance_observations`,
`asset_performance_maintenance_comparisons`, `equipment`, `tags`) before
and after a full equipment + comparison + factory context build against
the live database - confirmed identical. No new database table was
created; `AskResult`/`EquipmentResolution`/etc. are all in-memory
dataclasses. No new systemd service was added - context building runs
synchronously inside the existing request-driven `ask_structured()`
call.

### UI / cross-page integration

`ui/pages/1_Ask_AI.py` smoke-tested via Streamlit `AppTest` against the
live database (zero exceptions). "View Evidence" renders the actual
`AskResult.structured_context` dict via `st.json()` - the same object
the prompt/fallback was built from, so what's shown is never a
re-derived summary. All 4 cross-page entry points (`19_Equipment_Health.py`,
`4_Service_and_Maintenance.py`'s Maintenance Intelligence tab,
`20_Asset_Performance.py`, `17_Energy_Opportunities.py`) smoke-tested
individually via `AppTest`, zero exceptions, using the pre-existing,
already-proven `st.query_params` deep-link mechanism (not a new,
unverified one).

### Context / performance measurements

Measured directly (not inferred from source alone) during live
acceptance:

| Measurement | Result |
|---|---|
| Entity resolution (`resolve_equipment()`) | ~0.1-0.3s (in-memory scoring over already-loaded tags) |
| Structured-context build (single equipment, e.g. HEALTH_EXPLANATION) | well under 1s - included in the 0.1-0.3s elapsed times observed for clarification-only responses, which skip the LLM entirely |
| Structured-context build (comparison, 2 equipment x up to 6 domains) | still sub-second - dominated entirely by SQLite reads over already-small (tens to low hundreds of rows) tables |
| Grounding-validation time | negligible (regex matching over the answer string only) |
| **Ollama generation time** | **every one of 7 real-equipment queries hit the existing 600s `OllamaProvider` read-timeout without completing**, confirmed via `HTTPConnectionPool... Read timed out. (read timeout=600)` in every case |
| Total user-visible request time (LLM path) | 600.3-600.8s per query, all correctly ending in a safe deterministic fallback, never a crash or hang |
| Total user-visible request time (clarification path, no LLM needed) | 0.1-0.3s |

**This is a local-model inference/hardware limitation, not a Phase 15
architecture defect.** It matches this project's own pre-existing,
already-documented characteristic (CPU-only `qwen2.5:7b` inference,
"root-cause answers can take up to ~10 minutes") - Phase 15's
cross-domain prompts are larger than a typical single-tag prompt, and in
this pass consistently exceeded the shared 600s timeout rather than
completing within it. Context building itself was never the bottleneck
- it was consistently sub-second in every case.

### Automated tests

80 new tests across 6 files: `test_phase15_interpretation_intent.py` (14
- all 8 required regression phrases plus the classic root-cause example
staying on the old pipeline, plus comparison-splitting), `test_phase15_grounding_guard.py`
(10), `test_phase15_prompt_builder.py` (15), `test_phase15_equipment_resolution.py`
(11 - ambiguous-plant clarification, bare-followup detection, ordered
multi-entity comparison), `test_phase15_context_builder.py` (14 -
intent-selective retrieval, focus_domains narrowing, NULL/INSUFFICIENT
preservation, DB immutability), `test_phase15_ask_integration.py` (16 -
structured result contract, session isolation, explicit-override,
assessed_evidence exposure, no-aggregate-score scan).

### Full regression

**908 tests total (828 existing + 80 new), same 5 pre-existing unrelated
failures (`test_compressor_aliases` x2, `test_typo_tolerant_aliases`,
`test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`
- all pre-existing typo-correction/alias false positives, confirmed
unrelated to Phase 15), 0 new regressions.**

### Live acceptance

Run against the real live `database/simulation/` database and the real,
running `ollama`/`qwen2.5:7b` provider (confirmed reachable via
`/api/tags` beforehand) - no simulator manipulation, no fake data.
Equipment coverage confirmed for all 6 required types: Water Supply Pump
(P01 + P02), Chiller, Air Compressor, AHU, Cold Room, and Production
Equipment (Filling Machine FILL01 - resolved to `P01.FILL.FILL01`,
`health_score=84.0`, `HEALTHY-adjacent MONITOR` band, notably
`assessment_confidence=HIGH` this time, confirming confidence isn't
uniformly LOW across equipment - it genuinely varies with real evidence
coverage) - plus factory-wide and comparison queries. Every
deterministic/routing/resolution behavior worked exactly as designed
(see sections above) - see "Context / performance measurements" for the
honest result on the LLM-phrasing step itself: every real-equipment
query (8 of 9 equipment-scoped queries) fell back to the deterministic
answer after a 600s provider timeout, and the ambiguous/clarification
queries (which never reach the LLM) resolved correctly and instantly.
Also confirmed live: a "why is X unhealthy" question against a genuinely
HEALTHY/MONITOR-band equipment (AHU, score 85.9; Cold Room, score 78.8;
Filling Machine, score 84.0) correctly returned the real state rather
than inventing an "unhealthy" narrative to match the question's own
framing - and Cold Room's/Filling Machine's maintenance-overdue factor
reason string correctly carried through Phase 12's own careful "a risk/
attention factor, not evidence of physical degradation" language
unaltered. Manual
authoritative reconciliation (item 3) was performed for Water Supply
Pump WSP01 (P01): `health_score=57.2`, `health_band=ATTENTION`,
`assessment_confidence=LOW`, `provisional=True`, `maintenance_priority=REVIEW`,
`priority_score=45.9`, `recommendation_confidence=LOW`,
`confidence_basis`/`assessed_evidence` both `['health_condition',
'events', 'recent_movement']` - every one of these was independently
re-queried directly from `ui.health_data.get_latest_health_by_equipment_id()`
and `ui.maintenance_intelligence_data.get_detail()` and matched exactly
against what the Phase 15 context/fallback answer displayed.

### Known limitations

- No real (non-fallback) Ollama completion was obtained during this
  live-acceptance pass - the grounding guard's correctness against
  genuine model output is therefore evidenced by its unit tests only,
  not by a live catch. This should be revisited once on faster
  hardware/a smaller-context deployment, consistent with this project's
  already-standing, pre-Phase-15 CPU-inference limitation.
- No real Savings Verification intervention exists yet in the live
  database, so the richer VERIFIED/REJECTED/INCONCLUSIVE explanation
  path is unit-tested against synthetic data only, not exercised live.
- `assessed_evidence` and `confidence_basis` were identical for every
  equipment checked live (no case where evidence was assessed but did
  NOT drive the recommendation was observed in this pass) - the
  distinction is real and tested (unit tests construct both cases), but
  wasn't naturally observed in live data during this pass.
- A generic equipment-type word with no designator code (e.g. "the
  chiller", ambiguous among 4 real instances) falls back to session
  context if one exists, rather than always forcing a fresh
  clarification - a disclosed, deliberate trade-off given the same
  "leftover generic word" landmine class this project has already
  documented five separate times; not one of the required regression
  cases.
- Criticality is `NULL`/"Unconfigured" for all 83 equipment rows today
  (confirmed again during this pass) - Phase 15 answers mentioning
  criticality will almost always say "Unconfigured," honestly.

### Confirmations

No deterministic Phase 6-14 calculation semantics changed. No new
AI worker/service added. No domain database writes added (verified by
automated immutability test). No prediction/RUL added. No authoritative
AI root-cause diagnosis added (root causes remain hypotheses, explicitly
labeled, never asserted as fact). No PLC/control capability added. No
automatic work orders added. No fake live production data inserted at
any point in this phase.

### Commissioning - genuine Ollama completion obtained

The live-acceptance pass above obtained zero real (non-fallback) Ollama
completions - every attempt hit the existing 600s `OllamaProvider`
read-timeout. Per explicit user direction, this was **not** treated as a
Phase 15 architecture defect: the AI-routes-through-Ollama design is
intentional, and this development VM is deliberately CPU-only/slow.
A dedicated commissioning pass followed to obtain and verify at least
one genuine completion.

**Timeout made configurable (performance/configuration only, no routing
change):** `ai/providers/provider_factory.py` now reads an optional
`OLLAMA_READ_TIMEOUT` environment variable (same env-var-first pattern
already used for `OLLAMA_URL`/`OLLAMA_MODEL`), passed through to the
existing `OllamaProvider(read_timeout=...)` parameter. Left unset, the
previous 600s default (set inside `ai/providers/ollama_provider.py`,
untouched) applies exactly as before - confirmed by a quick check that
`ProviderFactory.create('ollama')` still returns `read_timeout=600` with
the env var unset. No provider/routing architecture was changed; this is
purely a tunable timeout.

**Commissioning attempts:**
1. `OLLAMA_READ_TIMEOUT=1200`, question `"Why does P01 WSP01 need
   attention?"` - **routing gap found**: this question matched no Phase
   15 trigger phrase at all (`intent=''`) and silently fell through to
   the OLD tag-level pipeline (a real, genuine defect, not a timeout
   issue - confirmed by `provider='ollama'`/`fallback_used=False` yet
   `intent=''` and an answer shaped like the old tag-ambiguity menu, not
   a Phase 15 answer). **Fixed**: added `"need attention"`/`"needs
   attention"`/`"require attention"`/`"requires attention"` to
   `GENERAL_ENGINEERING_QUERY`'s trigger phrases in
   `ai/interpretation_intent.py` (cross-domain, since "attention" alone
   spans Health/Maintenance/Performance vocabulary, not one domain).
   1 new regression test added; full suite re-run: 909 tests, same 5
   pre-existing failures, 0 new regressions.
2. Re-ran the same question at `OLLAMA_READ_TIMEOUT=1200` with the fix -
   `intent='GENERAL_ENGINEERING_QUERY'` confirmed correct routing this
   time, but the larger cross-domain prompt still timed out
   (`fallback_used=True`) at 1200s.
3. `OLLAMA_READ_TIMEOUT=1800`, narrower question `"Why is P01 WSP01
   unhealthy?"` (`HEALTH_EXPLANATION` - the smallest real domain set,
   Health + Anomalies + Events) - **succeeded**: a genuine completion
   returned in 1437.3s, well inside the 1800s budget.

**Result (attempt 3):** `provider='ollama'`, `fallback_used=False`,
`intent='HEALTH_EXPLANATION'`. Entity resolution 0.113s, context build
0.026s, prompt size 3189 chars - both confirming, again, that
deterministic processing was never the bottleneck. Real answer (verbatim,
truncated by the existing, unmodified `num_predict: 200` generation cap
- not by the timeout):
> Known from the system:
> - The Equipment Health score for Water Supply Pump WSP01 (P01) is
>   63.4, with a State of ATTENTION.
> - The assessment confidence is LOW (provisional).
> - Top contributing factors include: wsp_bearing_temp_condition
>   (penalty 12.8), water_supply_pump_repeated_events (penalty 12.0),
>   wsp_flow_process (penalty 8.0).
> - An open statistical anomaly is present for Power_kW, with severity
>   ATTENTION and a deviation of 40.67%.
> - [recent bearing-temperature warnings listed]
>
> Possible engineering hypotheses:
> - Possible causes could include [truncated by the 200-token cap before
>   any specific cause was named]

**Grounding guard against this real output** - a genuine, disclosed gap
was found and fixed: the approved plan explicitly required "anomaly
severity/state" to be checked "at minimum," but the first
implementation's enum families covered only Health/Maintenance/
Performance/Verification. Re-running `check_grounding()` against this
real answer confirmed it would not have validated the "severity
ATTENTION" claim at all (a silent non-check, not a false pass). **Fixed**:
added an `anomaly_severity` family (`INFORMATION`/`ATTENTION`/`WARNING`/
`HIGH`, Phase 9's own vocabulary) checked as SET-membership against every
currently-open anomaly (there can be several at once, unlike the
single-verdict Health/Maintenance/Verification families) - and widened
the claim regex specifically because the real model phrased it "with
severity ATTENTION" (no "is"/"of" connector), which the original
"severity is/of X" pattern (copied from the other families) would have
missed entirely. Verified directly against the real captured answer
text: a reconstructed matching context now correctly returns
`grounded=True`; a reconstructed mismatched context correctly returns
`grounded=False` with the exact violation message. 2 new regression
tests added; full suite re-run: **911 tests, same 5 pre-existing
unrelated failures, 0 new regressions.**

**Manual authoritative reconciliation (real output):** every fact in the
"Known from the system" section traces to the frozen context handed to
the model - health score/band/confidence/provisional and all three
factor penalties (12.8/12.0/8.0) matched exactly (grounding guard
confirmed `grounded=True, violations=[]` against the exact frozen
context object). The anomaly severity claim was independently verified
against a reconstructed matching/mismatched pair using the real answer
text (above), since the frozen context itself wasn't persisted to disk.

**Fact vs. hypothesis (real output):** the "Known from the system" /
"Possible engineering hypotheses" section structure was correctly
produced. No unsupported root-cause claim was asserted - the hypothesis
section was truncated by the generation cap before naming any specific
cause, so this run does not independently prove hypothesis-phrasing
discipline on completed hypothesis text (a limitation, disclosed
honestly - the structure and boundary instructions are present and unit-
tested; a real, un-truncated hypothesis completion was not obtained in
this pass).

**Performance interpretation:** entity resolution and context building
were sub-second in every one of the 4 commissioning attempts (0.11-0.13s
and 0.024-0.027s respectively) - this remains a local-model-inference
limitation of the CPU-only VM, not evidence that Phase 15's deterministic
processing is slow. The successful completion (1437.3s for ~208 tokens)
is consistent with this project's own already-documented ~0.6-0.8
tokens/sec CPU-only inference characteristic (measured here: ~0.144
tokens/sec by a rough chars/4 approximation, on the low end of that
range but the same order of magnitude and same root cause).

**Fallback remains fully operational**: the provider-timeout fallback
path used throughout the original live-acceptance pass (8 real timeouts,
all handled safely) was not touched or weakened by this commissioning
work - both a successful completion and a safe timeout fallback are
confirmed, tested, working paths today.

### PHASE 15 — AI INTERPRETATION LAYER: **COMPLETE AND COMMISSIONED**

## Phase 16.1 — Data Health Core Engine (retrospective summary)

Concise factual summary added retrospectively during Phase 16.5 (the
original completion report was delivered in conversation only, never
appended to this file - a gap noted honestly in Phase 16.4's own
documentation). This is a factual record of the accepted implementation
state, not a rewrite of the original development narrative.

Built the deterministic Phase 16.1 Data Health engine
(`engine/data_health_targets.py`, `engine/data_health_engine.py`) -
completely independent from Equipment Health, never reading
`equipment_health_snapshots` and never read by `engine/health_engine.py`.
Four independently-measured components - Freshness (35%), Availability
(25%), Validity (25%), Continuity (15%) - combined into a single 0-100
Data Confidence score, with `GOOD`/`DEGRADED`/`POOR`/`UNAVAILABLE`
status bands (`GOOD ≥ 85`, `DEGRADED 60-84`, `POOR 1-59`,
`UNAVAILABLE` = score genuinely cannot be calculated, never fabricated
as 0). Required-tag resolution reuses `engine.baseline_targets.EQUIPMENT_TYPE_PROFILES`
unchanged (no new registry). Freshness thresholds are per-tag
(`logging_interval_seconds × 5`), with `log_on_change` tags reported as
`INDETERMINATE` rather than guessed stale. Frozen-candidate detection
is advisory-only, corroborated against sibling tags, and never affects
the score. Objective validity checking only (`None`/NaN/±inf/malformed)
- never alarm/warning thresholds reinterpreted as a validity range.
Continuity uses a bounded 4h lookback with a 5×-interval gap threshold.
Fixed two real bugs found during implementation: `equipment.equipment_type`
stores human-readable labels, not the registries' snake_case keys (fixed
via reverse area-code token matching); the frozen-candidate query window
originally clipped the earliest required sample (fixed with 2x query
headroom). Also fixed a pre-existing `ai/rule_engine.py` NaN/±infinity
bug found during this work. 26 engine tests + 9 RuleEngine regression
tests. No schema change, no persistence, on-demand calculation only.

## Phase 16.2 — Data Health UI Integration (retrospective summary)

Concise factual summary added retrospectively during Phase 16.5 (see
Phase 16.1's note above for why).

Added a clearly-separated "📡 Data Health" section to
`ui/pages/19_Equipment_Health.py` (new `ui/data_health_data.py` UI
adapter, read-only, calls the Phase 16.1 engine directly, never
recalculates) - visually and conceptually distinct from the Equipment
Health score above it on the same page, evaluated once per selected
equipment per page render. Shows the Data Confidence metric + status
badge, an expandable 4-component breakdown (N/A shown for inapplicable
components, never a fabricated score), a required-telemetry summary,
issue callouts (missing/invalid/gaps/timestamp issues/frozen
candidates), and an expandable tag-level detail table. Frozen
candidates always labeled "advisory only - NOT a confirmed sensor
fault"; source/driver always captioned as configured-source-only, never
a live connection-health claim. Explicitly decided NOT to integrate a
SCADA badge in this phase (documented reasoning: the SCADA snapshot
writer's 2-second cadence is incompatible with an uncached per-request
Data Health evaluation cost) - the first appearance of the SCADA
deferral that Phases 16.3/16.4/16.5 each independently reconfirmed with
their own evidence. 22 UI adapter/page tests. `ui/pages/2_Live_Data.py`
remained disabled throughout.

## Phase 16.3 — Data Health → AI Grounding Integration (retrospective summary)

Concise factual summary added retrospectively during Phase 16.5 (see
Phase 16.1's note above for why).

Integrated Data Health into the existing Phase 15 AI Interpretation
Layer as an additive extension - no parallel AI pipeline.
`ai/context_builder.py` now always attaches a compact `data_health`
section to a single-equipment context (calling
`engine.data_health_engine` directly, never the UI adapter) plus a
`requires_telemetry_confidence` flag computed from the already-
classified Phase 15 intent (`ai/interpretation_intent.py`) with a
narrow static-reference-phrase exception for genuinely non-telemetry
questions. `ai/interpretation_prompt_builder.py` renders Data Health in
the prompt with 8 explicit numbered rules (never recalculate/override
it, never conflate GOOD/POOR Data Health with machine
health/faultiness, never promote frozen candidates to confirmed
failures, never treat configured source as live connectivity, etc.),
plus conditional DEGRADED/POOR qualification instructions.
`ai/grounding_guard.py` gained 4 adversarial pattern checks enforcing
those same rules deterministically against the model's actual output.
`app/ask.py` short-circuits before ever calling the AI provider when a
telemetry-dependent question meets `UNAVAILABLE` Data Confidence -
returning a deterministic explanation instead. Core design principle
enforced throughout: Equipment Condition and Data Confidence are
independent dimensions, never conflatable in either direction. 32 new
tests (22 required scenarios + the 4 exact adversarial example strings
from the approved plan). No schema change, no persistence, no SCADA
integration, no plant-wide aggregation.

## Phase 16.4 — Data Health Fleet / Plant Overview

### What this phase is

A deterministic, factory-wide FLEET view over Phase 16.1's per-equipment
Data Health engine - "which equipment's telemetry can currently be
trusted", grouped by plant/area/system/equipment type, with a
worst-first attention ordering and a tag-level issue-centric view. This
is a data-TRUSTWORTHINESS overview, never Equipment Health, Maintenance
Priority, Asset Performance, PLC alarm state, a new AI score, or a
sensor-failure/connection-health diagnosis - Phase 16.1's engine and
its GOOD/DEGRADED/POOR/UNAVAILABLE vocabulary remain completely
unchanged and authoritative; this phase only aggregates its results.

Note: Phases 16.1/16.2/16.3's own completion reports were delivered in
conversation only and were never appended to this file - a pre-existing
documentation gap this phase did not attempt to backfill (out of the
scope actually authorized for Phase 16.4).

### Architecture audit findings (KEEP / EXTEND / MODIFY / BUILD NEW)

**KEEP existing, unchanged:** `engine/data_health_engine.py` and
`engine/data_health_targets.py` (authoritative, untouched - the fleet
layer calls `calculate_equipment_data_health()` once per equipment and
never recalculates any component); `ui/data_health_data.py` (untouched
single-equipment UI adapter, still used directly by the Equipment
Health page); `ui/pages/19_Equipment_Health.py`'s existing Data Health
section (untouched, reused via the existing `?equipment_id=` deep-link
convention rather than a second render implementation);
`engine.health_engine.discover_health_targets()` (reused unchanged for
equipment-population enumeration - the same population Phase 12's
Equipment Health already uses, since both key off the identical
`engine.baseline_targets.EQUIPMENT_TYPE_PROFILES` registry);
`ui.health_data.equipment_type_label()` (reused for engineer-readable
type labels); `st.cache_data(ttl=...)`, already established in
`ui/energy_dashboard_data.py`/`ui/scada_floor_plan_data.py`, reused
unchanged as the caching mechanism; `ui/scada_snapshot_writer.py`'s 2s
loop (untouched - Data Health integration deferred, see below);
`ui/pages/2_Live_Data.py` (untouched, remains disabled from
navigation).

**EXTEND existing:** `ui/Home.py` - one small, additive summary block.

**MODIFY existing:** `ui/Home.py` only - nothing else pre-existing was
changed.

**BUILD NEW:** `engine/data_health_fleet.py` (fleet aggregation engine),
`ui/data_health_fleet_data.py` (cached UI adapter), `ui/pages/21_Data_Health.py`
(fleet overview page), `tests/test_data_health_fleet_engine.py`,
`tests/test_data_health_fleet_ui.py`.

### Files created

- `engine/data_health_fleet.py` (374 lines) - `FleetDataHealthResult`
  dataclass, `calculate_fleet_data_health()`, `attention_sort_key()`,
  hierarchy/group aggregation, per-tag issue flattening. Zero Streamlit
  dependency, mirrors `engine/data_health_engine.py`'s own pure-function
  discipline.
- `ui/data_health_fleet_data.py` (150 lines) - `st.cache_data`-wrapped
  `get_fleet_data_health()`, `force_refresh()`, `filter_options()`,
  dataclass-to-dict conversion (drops raw `TagDataHealth` objects,
  reuses `ui.data_health_data`'s `status_label`/`confidence_score_text`/
  `component_score_text` and `ui.health_data.equipment_type_label`).
- `ui/pages/21_Data_Health.py` (253 lines) - the fleet overview page:
  distribution KPIs, filters, three tabs (Equipment Overview /
  Plant-Area-System Grouping / Issue View), drill-down deep-link to
  Equipment Health.
- `tests/test_data_health_fleet_engine.py` - 23 tests.
- `tests/test_data_health_fleet_ui.py` - 11 tests.

### Files modified

- `ui/Home.py` - added the `ui.data_health_fleet_data` import, a small
  read-only Data Health summary block (GOOD/DEGRADED/POOR/UNAVAILABLE
  counts, reusing the SAME cached fleet result the fleet page itself
  uses - never a second independent evaluation), and the new page's
  `st.Page(...)` navigation registration.

### Fleet evaluation architecture

`calculate_fleet_data_health(config_db, machine_db, plant_codes=None, now=None)`:
1. Enumerates eligible `(equipment_type, instance_key)` pairs via the
   unchanged `engine.health_engine.discover_health_targets()`, once per
   plant (defaults to every plant in the `plants` table).
2. Calls `engine.data_health_engine.calculate_equipment_data_health()`
   once per equipment, wrapped in try/except - one equipment's
   exception is recorded (`failures` list, honest error message) and
   that equipment becomes an UNAVAILABLE row; it never silently
   disappears and never silently becomes GOOD.
3. One bulk SQL query (`equipment JOIN plants/areas/systems`) enriches
   every already-fetched row with plant/area/system/display_name -
   never one hierarchy query per equipment.
4. Pure-Python aggregation over the already-fetched rows: fleet-wide
   status counts, `by_plant`/`by_area`/`by_system`/`by_equipment_type`
   group counts, fleet-wide issue counts, a flattened per-tag `issues`
   list (re-presenting each tag's already-computed dimensions - never a
   new classification), and a final worst-first sort via
   `attention_sort_key()`.

No component score, required-tag resolution, frozen-candidate
detection, or log_on_change handling is reimplemented anywhere in this
module - confirmed both by code inspection and by
`test_per_equipment_scores_are_never_recalculated`.

### Measured full-fleet evaluation cost (real live databases)

- Equipment count evaluated: **34** (across both plants; `p01`: 17,
  `p02`: 17).
- Full-fleet evaluation time: **~6.1-6.2s** (measured across 3 separate
  runs).
- Average per equipment: **~180ms**.
- Worst single equipment: **~300ms** (`P01.UTILITY.CHL02`).
- Concurrent-read probe during a live fleet evaluation: 32 independent
  historian queries completed successfully from a second thread while
  the fleet pass ran, max latency 73.4ms, avg 37.7ms - the historian
  stayed responsive throughout (every query here is a short-lived,
  bounded, indexed read/close, never a held transaction).

This directly ruled out recomputing on every Streamlit rerun (a rerun
fires on every widget interaction) and confirmed a cache is required.

### Cache/refresh strategy

`st.cache_data(ttl=120)` on `ui.data_health_fleet_data.get_fleet_data_health()`
- Option B from the audit ("short-lived cache reused across reruns"),
  the smallest safe architecture that satisfies both constraints (too
  slow for every rerun, does not need sub-minute freshness). ~19x
  headroom over the measured ~6.1s cost.
- Process-wide (Streamlit's own `cache_data` semantics) - every browser
  session/user shares one fleet result and one ~6s cost per refresh
  window, not one per session. `ui/Home.py`'s summary block calls the
  exact same function with the exact same arguments, so it is always a
  cache hit against the fleet page's own result, never a second
  independent evaluation (confirmed: `test_cache_reused_across_repeated_calls`).
- An explicit "🔄 Refresh now" button calls `force_refresh()`
  (`get_fleet_data_health.clear()` - clears only this one cached
  function, never a global cache wipe).
- `as_of` (the real `computed_at` timestamp from the underlying engine
  pass) is always displayed, with an explicit "may be up to N minutes
  old" caption - the cached result is never presented as if it were
  live.
- One equipment's evaluation failure is isolated at the engine layer
  (see above) and can never poison the rest of the cached fleet result.
- No new persistence, no schema change, no background worker - a plain
  request-scoped/TTL-cached function, matching every "no worker unless
  measurements force it" instruction in the approved plan. Measured
  cost (6.1s/34 equipment, single VM, 4 cores) did not come close to
  justifying one.

Measured page-render cost end to end (`AppTest`, real live database):
first (cold-cache) render **~7.5s** (6.1s fleet compute + ~1.4s
Streamlit/pandas rendering); a rerun/filter-change within the 120s
window **~0.5s** (adapter cache hit, ~2ms, plus rendering only).

### Fleet aggregation contract

`FleetDataHealthResult`: `as_of`, `equipment_count`, `assessed_count`
(excludes UNAVAILABLE), `good_count`/`degraded_count`/`poor_count`/
`unavailable_count`, `by_plant`/`by_area`/`by_system`/`by_equipment_type`
(each `{group_key: {equipment, GOOD, DEGRADED, POOR, UNAVAILABLE,
missing_tags, stale_tags, invalid_tags, gaps}}`), fleet-wide
`missing_tag_count`/`stale_tag_count`/`invalid_tag_count`/`gap_count`/
`timestamp_issue_count`/`indeterminate_freshness_count`/
`frozen_candidate_count`, `equipment` (the sorted per-equipment rows),
`issues` (flattened per-tag issue rows), `failures` (evaluation
exceptions, transparently surfaced). No hidden/blended aggregate score
of any kind exists anywhere in this contract (confirmed by
`test_no_hidden_aggregate_factory_score_field`).

### Plant/area/system grouping

Three grouped tables (By Plant / By Area / By System / By Equipment
Type) on the "Plant / Area / System Grouping" tab, each showing
Equipment/GOOD/DEGRADED/POOR/UNAVAILABLE/Missing Tags/Stale Tags/
Invalid Tags/Continuity Gaps. Sorted by `POOR + UNAVAILABLE` equipment
count descending - an explicitly-defined, transparent rule (documented
in the page's own caption), never a hidden weighted ranking.

### Equipment overview table

"Equipment Overview" tab: Equipment, Plant, Area, System, Type, Data
Confidence, Status, the four component scores (N/A shown where
inapplicable), Required/Available/Missing/Stale/Invalid tag counts,
Gaps, Frozen Candidates, Source, As Of - filterable by
Plant/Area/System/Equipment Type/Status. Sorted worst-first by
`attention_sort_key()` (below).

### Issue-centric view

"Issue View" tab: one row per (tag, issue) across the whole filtered
fleet - Tag, Equipment, Plant, Issue (Missing/Stale/Invalid/Continuity
Gap/Timestamp Issue/Frozen Candidate - advisory/Indeterminate -
change-only logging), Last Value/Update, Logging Mode, Source, Detail.
Filterable by issue type. Deliberately never diagnoses "sensor failed"
or "PLC offline" - every row is a direct re-presentation of a Phase
16.1 tag-level finding, not a new classification.

### Attention-ordering methodology

`engine.data_health_fleet.attention_sort_key()` - a plain, documented
sort tuple, never a new weighted score:
1. Status: UNAVAILABLE, then POOR, then DEGRADED, then GOOD.
2. Lowest Data Confidence score first (UNAVAILABLE/no-score sorts as
   worse than every real score - never fabricated as 0).
3. Largest missing-tag count first.
4. Any invalid-value tag present, before none.
5. Any timestamp issue present, before none.
6. Largest continuity-gap count first.
7. Largest stale-tag count first.
8. `instance_key`, for full determinism on an exact tie.

### Equipment drill-down

Reuses the existing Phase 16.2 Data Health section on
`ui/pages/19_Equipment_Health.py` via its already-existing
`?equipment_id=` deep-link convention
(`st.switch_page("pages/19_Equipment_Health.py", query_params={"equipment_id": ...})`)
- no second Data Health detail renderer was written. If an equipment
row could not be resolved to a configured `equipment_id` (an
evaluation failure with no prior successful resolution), the link is
omitted and the honest error reason is shown instead.

### UNAVAILABLE handling

Preserved exactly as Phase 16.1 defines it - `confidence_score` stays
`None` (never fabricated as 0), the real deterministic reason/
limitation is carried through unchanged. A genuine evaluation exception
(caught at the fleet layer) is distinguished in the `failures` list and
the row's `evaluation_error` field, separate from a "no registry
applies" or "zero available telemetry" UNAVAILABLE case that Phase
16.1 itself already returns cleanly.

### log_on_change handling

Preserved exactly - `INDETERMINATE_CHANGE_ONLY` is its own distinct
issue type, never folded into `STALE` anywhere in the fleet aggregation
or the issue-centric view (confirmed by
`test_stale_issue_never_generated_for_indeterminate_freshness_tag`).

### Frozen-candidate handling

Preserved exactly - "Frozen candidate - advisory" wording only, no
score penalty added anywhere in the fleet layer (confirmed by
`test_frozen_candidate_issue_wording_never_implies_confirmed_failure`
asserting none of "failed"/"failure"/"fault"/"broken" appear).

### Source/connectivity wording

The page's Equipment Overview tab repeats the same disclosed caption
Phase 16.2 already uses: "Source identifies the configured data source
only and is not a live connection-health indication." No new
connection-health claim of any kind was added.

### Failure isolation

Confirmed live (real fleet re-run with one equipment's
`calculate_equipment_data_health()` call patched to raise): fleet
result still returned all 34 equipment rows; the failing one became
UNAVAILABLE with the real exception message; every other equipment's
result was unaffected. `tests/test_data_health_fleet_engine.py::TestFailureIsolation`
covers this with a controlled fixture (2 equipment, one raising).

### Home integration status

**Implemented** - a 4-metric (GOOD/DEGRADED/POOR/UNAVAILABLE) summary
block, gated on `equipment_count > 0`, placed directly below the
existing Equipment Health summary block and visually separated by a
`st.divider()`. Calls the exact same cached adapter function as the
fleet page - confirmed never to trigger a second independent
evaluation.

### SCADA integration status and rationale (deferred, unchanged)

**Deferred, `ui/scada_snapshot_writer.py` left completely untouched.**
The Phase 16.2 deferral reasoning is reconfirmed, now with real
Phase-16.4 measurements: the SCADA writer's loop runs every 2 seconds
and is currently a handful of already-bulk-fetched queries per tick
(`database.get_latest_all()`/`get_latest_text_all()`, called once and
shared across both plants); the measured full-fleet Data Health cost is
~6.1s for 34 equipment - over 3x the entire tick interval on its own,
even before the writer's existing work. Reading a page-visit-triggered
cache is not a safe alternative either: SCADA runs continuously
regardless of whether any engineer has opened the Data Health page, so
a cache that is only warmed by page visits could silently go stale for
hours with no user-visible signal inside SCADA itself. A correct,
safe integration would require its own independently-scheduled refresh
- i.e. a new background worker - which the measured cost does not
justify building yet (per the explicit "no new worker unless
measurements force it" instruction).
`tests/test_data_health_fleet_engine.py::TestNoScadaIntegration` pins
this via a source-text assertion (`ui/scada_snapshot_writer.py` never
imports anything Data-Health-related).

### Confirmation Live Data remains disabled

`ui/pages/2_Live_Data.py` was not touched. `ui/Home.py`'s
`general_pages` list still has its `st.Page("pages/2_Live_Data.py", ...)`
line commented out, unchanged by this phase.

### Tests added

34 new tests: 23 in `tests/test_data_health_fleet_engine.py`
(aggregation correctness against a controlled 4-equipment/2-plant
fixture covering all four statuses, grouping correctness, attention
ordering, failure isolation, no-SCADA-integration, plus 4 tests against
the real live database), 11 in `tests/test_data_health_fleet_ui.py`
(adapter dict-conversion/no-raw-dataclass-leak/cache-reuse/force-refresh,
plus `AppTest` page-smoke checks for the new Data Health fleet page,
the modified Home page, and the untouched Ask AI / SCADA Floor Plan
pages).

### Phase 16.1/16.2/16.3 regression result

`tests.test_data_health_engine`, `tests.test_rule_engine_nan_fix`,
`tests.test_data_health_ui`, `tests.test_phase16_3_data_health_grounding`,
`tests.test_phase15_ask_integration`, `tests.test_phase15_grounding_guard`:
**117/117 pass, unchanged.**

### Complete regression result

**1034 tests total, 1029 passed, 5 failed, 0 errors, 0 skipped.**
(1000 pre-Phase-16.4 baseline + 34 new Phase 16.4 tests = 1034 - no test
silently dropped.) All 5 failures are the same pre-existing,
unrelated typo-correction/alias-classification failures documented in
every prior phase's own regression result
(`test_compressor_aliases` x2, `test_typo_tolerant_aliases`,
`test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`)
- **0 new regressions.**

### Live validation

Reconciled one equipment (`P01.WATER.WSP01`) across three independent
call paths against the real live database - fleet row, direct
`engine.data_health_engine.calculate_equipment_data_health()` call, and
`ui.data_health_data.get_equipment_data_health()` (the Equipment Health
page's own adapter) - all three returned an identical Data Confidence
score (91.0), status (GOOD), and component scores. All 8 currently-
eligible equipment types were present in the real live fleet result
(Water Supply Pump, Chiller, Air Compressor, AHU, Cold Room, Production
Equipment, Chilled Water Pump, Filling). The live system currently
shows GOOD for all 34 equipment - DEGRADED/POOR/UNAVAILABLE were each
validated via isolated, deterministic test fixtures instead (per the
explicit "do not manipulate live telemetry merely to make the UI
colorful" instruction), plus one genuine live UNAVAILABLE reproduction
via a mocked engine exception.

### Performance result

A. Single-equipment time: ~180ms average, ~300ms worst (measured
   across all 34 real equipment). B. Full-fleet time: ~6.1-6.2s (34
   equipment, 2 plants). C. Equipment evaluated: 34. D. Average per
   equipment: ~180ms. E. Cached-read reuse time: ~0.002s (vs ~5-6s
   uncached). F. Page first-load time (cold cache): ~7.5s. G. Page
   rerun/filter-change time (warm cache): ~0.5s. H. DB query behavior:
   every query bounded/indexed (`WHERE tag = ?` + `LIMIT`/time-window),
   confirmed responsive under concurrent read load during a live fleet
   pass (32 probes, max 73.4ms, avg 37.7ms). I. SCADA snapshot impact:
   not applicable - no SCADA integration implemented. J. Historian
   responsiveness during evaluation: confirmed via the concurrent-probe
   measurement above - no lockups, no timeouts.

### Remaining limitations / next architectural recommendation

- Phases 16.1-16.3's own completion reports were never appended to this
  file (a pre-existing gap, not created or fixed by this phase).
- The fleet page has no persisted history - a "was this equipment's
  Data Health better yesterday" question cannot currently be answered.
  Trending/history was explicitly out of scope for this phase and
  remains a separately-approvable future feature.
- SCADA integration remains deferred; if it becomes genuinely wanted,
  the honest next step is a small, independently-scheduled Data Health
  refresh mechanism (its own modest-interval background pass, e.g. tens
  of seconds to a few minutes, decoupled from the 2s SCADA tick) that
  SCADA could then safely READ from - not folding a 6-second-plus
  computation into the existing 2-second loop.
- The equipment population (34) is small enough that a linear per-
  equipment loop was clearly sufficient; if the tag dataset grows
  substantially, this cost should be re-measured before assuming the
  same architecture still holds.

### PHASE 16.4 STATUS: PASS
### READY FOR NEXT PHASE REVIEW: YES

## Phase 16.5 — Data Health History & Trend Intelligence

### Architecture (approved via checkpoint)

Option C (hybrid), approved after an explicit audit-first checkpoint:
persisted equipment-level Data Health snapshots (change-triggered + 24h
heartbeat), a new independent worker, historical read functions
building on the persisted table, and Phase 16.1/16.4's current-state
engine left completely unchanged. The checkpoint established that
retrospective reconstruction from historian data alone is unsafe (every
internal historian read in `engine/data_health_engine.py` has no upper
time bound, and tag configuration is not historically versioned), so
persisted snapshots are the sole authoritative historical record from
the moment persistence began - never fabricated for periods before
that.

The whole persistence/worker/history stack deliberately mirrors Phase
12.2's already-accepted Equipment Health pattern
(`engine/health_migrator.py` / `engine/health_persistence_targets.py` /
`engine/health_orchestration.py` / `app/equipment_health_worker.py` /
`engine/health_history.py`) rather than inventing a new one.

### Files created

- `engine/data_health_migrator.py` - `data_health_snapshots` table +
  two indexes, idempotent (`CREATE TABLE IF NOT EXISTS`), SQLite backup
  API before migrating.
- `engine/data_health_persistence_targets.py` - change-detection
  thresholds (`CONFIDENCE_SCORE_CHANGE_THRESHOLD=3.0`,
  `COMPONENT_SCORE_CHANGE_THRESHOLD=5.0`, exact-difference count
  fields), `HEARTBEAT_MAX_INTERVAL_HOURS=24.0`, `TICK_INTERVAL_SECONDS=300.0`,
  the recurring-issue field mapping.
- `engine/data_health_domain.py` - plain data-access layer
  (`persist_snapshot`, `get_latest_snapshot`, `get_snapshot_before`,
  `list_snapshots`, `list_latest_snapshots_for_plant`, `first_snapshot_time`).
- `engine/data_health_orchestration.py` - `has_material_change()`
  (ordered, documented change-detection rules), `_should_heartbeat()`,
  `evaluate_and_maybe_persist()`, `run_cycle()`.
- `engine/data_health_history.py` - the historical read-model contract:
  `get_history`, `get_latest`, `first_recorded_at`,
  `component_score_history`, `status_duration`, `status_transitions`,
  `recurring_issues`, `fleet_status_summary`.
- `app/data_health_history_worker.py` + `deploy/systemd/data_health_history_worker.service` -
  mirrors `app/equipment_health_worker.py` exactly (`fcntl.flock()`
  single-instance lock, `Restart=always`, 300s tick).
- `ui/data_health_history_data.py` - cached UI adapter
  (`st.cache_data(ttl=60)`, distinct from the fleet page's own 120s
  live-evaluation cache).
- `tests/test_data_health_history_engine.py` (41 tests),
  `tests/test_data_health_history_ui.py` (9 tests).

### Files modified

- `ui/pages/21_Data_Health.py` - two new tabs added ("History & Trends",
  "Recurring Issues") alongside the existing three (item 19's explicit
  "do not redesign the existing Current Fleet page" - those three are
  untouched).

### Schema/migration

One append-only table, `data_health_snapshots` - `confidence_score`/
component scores nullable (never coerced to 0), integer issue counts,
`change_reason`, `evaluation_error` (nullable - distinguishes a genuine
worker/evaluation failure from a legitimate engine-returned
`UNAVAILABLE`), `computed_at`/`created_at`. No raw `plc_data`
value/timestamp column anywhere (confirmed by a dedicated test) - the
historian remains sole authority for raw telemetry. Two indexes:
`(plant_id, instance_key, computed_at)` and `(plant_id, computed_at)`,
the same two shapes `equipment_health_snapshots` already uses for the
identical two query patterns. Migration applied to the real live
`database/simulation/config.db` (backed up first via the SQLite backup
API, per convention) - confirmed idempotent.

### Worker architecture

`app/data_health_history_worker.py`: single-instance `fcntl.flock()`
lock (`logs/data_health_history_worker.lock`), `Restart=always`
systemd unit, 300s (5 min) tick - the same interval and the same
quantitative justification `app/equipment_health_worker.py` already
established (a full-fleet pass, measured at 6.1-6.2s across 34
equipment, is ~2 orders of magnitude below the tick interval), plus the
confirmation that persistence itself only writes on real change or
heartbeat so tick rate does not translate into write pressure. Does
not depend on Streamlit or any page being open; the 120s
`ui/data_health_fleet_data.py` cache is never used as persistence
infrastructure - the two are entirely independent.

**Not yet started as a systemd service in this session** (no
sudo/TTY access here) - the service file and the `enable --now` command
are ready; starting it is a manual step for the user (suggested command
below). The worker's own logic was fully exercised via `run_cycle()`
calls in this session, including one real cycle against the live
database that persisted the system's actual first 34 snapshots.

### Snapshot/change policy

`has_material_change()` (ordered, mirrors `engine.health_orchestration`'s
own style): no previous snapshot → `initial`; `confidence_status`
differs → `status_changed`; `confidence_score` None-vs-real mismatch →
`became_unavailable`/`recovered_from_unavailable`; real score delta ≥
3.0 → `confidence_score_changed`; any component score
applicability mismatch or delta ≥ 5.0 → `component_applicability_changed`/`component_score_changed`;
any of the 7 integer issue counts differing (exact, no threshold -
these are already small discrete units) → `issue_count_changed`; else
`unchanged`. The 5.0-point component threshold is deliberately above
any possible floating-point/rounding noise (every component is already
rounded to 1 decimal) but below the smallest real change a single tag
flipping produces (100/required_tag_count points per tag).

### Heartbeat behavior

24h max interval (`HEARTBEAT_MAX_INTERVAL_HOURS`), identical value to
Phase 12.2's own. An evaluation failure is subject to the exact same
change-detection/heartbeat logic as any real result (via a synthetic
`UNAVAILABLE` stand-in) - a persistent failure gets exactly one
snapshot, then heartbeats every 24h, never one row per 300s tick.

### Historical read model

Per-equipment: `get_history` (chronological), `status_duration`,
`status_transitions`, `recurring_issues`, `component_score_history`.
Fleet-wide: `fleet_status_summary` (hierarchy-grouped: by plant/area/
system/equipment_type), reusing `discover_health_targets()` for the
exact same equipment population Phase 16.4's current-state fleet view
uses. Every function reads only the already-persisted
`data_health_snapshots` table - none recalculates Data Health.

### Status-duration methodology

Time-based, never sample-count-based. Each snapshot's status is
assumed to hold from its own `computed_at` until the next snapshot's
`computed_at` (valid specifically because snapshots are only ever
written on real change or heartbeat - silence between two genuinely
means "state persisted"). **Final interval**: the last snapshot at-or-
before the query's `end` is assumed to hold through `end` itself
(`final_interval_treatment="extended_to_end"`, always disclosed).
**Leading gap**: if `end`'s window `start` predates the equipment's
earliest snapshot, that leading portion is reported as an explicit
`no_history_seconds`/`no_history_percentage` bucket - never fabricated
as any real status. A "carry-in" snapshot (the one immediately before
`start`, if one exists) correctly anchors the window's leading edge
when persistence began before the query window. Equipment with zero
persisted history at all returns `insufficient_history=True` with all
percentages `None` - never 0%.

### Transition methodology

Consecutive-pair `confidence_status` comparison (mirrors
`engine.health_history.band_transitions()`), using the same carry-in
technique so a transition right at the window's start is detected
correctly. Each transition's `changes` field is a deterministic list
of structured `{field, from, to}` diffs across the 12 persisted score/
count columns - never AI-generated narrative text, and never names a
specific tag (only the aggregate counts this schema actually persists -
a disclosed, honest scope limit, not fabricated detail).

### Recurring-issue methodology

"Inactive → active = new occurrence": walks the ordered snapshot
sequence (with carry-in) per issue type, incrementing an occurrence
counter only when that issue's persisted count goes from 0 to >0 - an
issue remaining active across subsequent snapshots, including 24h-
heartbeat rows, never adds a further occurrence (verified directly by
`test_heartbeat_does_not_create_fake_recurrence`). The very first
observed row's own already-nonzero counts are never credited as a
fresh occurrence (we don't know when a pre-existing condition actually
began). Covers all 7 issue types: `MISSING`, `STALE`, `INVALID`,
`CONTINUITY_GAP`, `TIMESTAMP_ISSUE`, `INDETERMINATE_CHANGE_ONLY`,
`FROZEN_CANDIDATE`.

### UNAVAILABLE handling

`confidence_score`/component scores persist as SQL `NULL`, never
coerced to 0, at every layer (write, duration, transitions, recurrence,
UI). `evaluation_error` distinguishes a genuine worker/evaluation
failure (e.g. a database hiccup) from a legitimate engine-returned
`UNAVAILABLE` (e.g. "no applicable registry") - never translated into
"PLC offline"/"network failure"/"OPC failure"/"sensor failure" anywhere.

### log_on_change handling

Preserved exactly - `indeterminate_freshness_count` stays its own
column/issue type, never folded into `stale_tag_count` anywhere in
persistence, change-detection, or history (verified by
`test_indeterminate_change_only_never_counted_as_stale`).

### Frozen-candidate handling

Preserved exactly - `frozen_candidate_count` persists and recurs as
"Frozen Candidate (advisory)" only; no score penalty anywhere in this
phase's code, and the occurrence dict's own keys contain no failure/
fault language (verified by
`test_frozen_candidate_recurrence_never_labeled_sensor_failure`).

### UI integration

Extended `ui/pages/21_Data_Health.py` with two new tabs (5 total: the
original Equipment Overview / Plant-Area-System Grouping / Issue View,
now joined by History & Trends / Recurring Issues) - the three original
tabs are byte-for-byte untouched. **History & Trends**: honest "history
recorded since X" disclosure (or an explicit "not started yet" message
if the worker hasn't run), a 1/3/7/14/30-day window selector, a
per-equipment Data Health Status Distribution (time-based percentages,
never sample counts), an un-smoothed Data Confidence + component-score
line chart, a status-transition table, and a fleet-wide time-in-status
grouped-by-plant/equipment-type summary. **Recurring Issues**: a fleet-
wide table of occurrence counts per equipment per issue type, sorted by
total occurrences descending.

### Equipment Health page integration

None added this phase - `ui/pages/19_Equipment_Health.py` is completely
untouched (confirmed via `git diff --stat`, 0 changes, and a dedicated
AppTest smoke test). A future small history link is possible per the
approved plan but was not required and was not added, to keep this
phase's scope disciplined.

### SCADA confirmation

`ui/scada_snapshot_writer.py` completely untouched - confirmed by a
dedicated source-text test
(`test_scada_snapshot_writer_still_never_imports_data_health`). No
Data Health evaluation of any kind occurs inside the 2-second SCADA
loop.

### Source/connectivity wording

Unchanged - the same "reflects the CONFIGURED source only" disclosure
carries through to the persisted `source_driver` field and every
historical view; no connection-health subsystem was introduced.

### Tests added

50 new tests: 41 in `tests/test_data_health_history_engine.py`
(migration idempotency/schema, all `has_material_change()` trigger
types, heartbeat, no-write-when-unchanged, restart/duplicate
prevention, evaluation-failure isolation, history ordering/empty/
single-snapshot, status-duration including carry-in/final-interval/
leading-gap/insufficient-history, status transitions with structured
diffs, recurring issues including the heartbeat-non-inflation case,
frozen-candidate/log_on_change preservation, fleet hierarchy filtering,
no-hidden-aggregate-score, UNAVAILABLE-stays-null, no-SCADA-integration,
plus 2 tests against the real live database), 9 in
`tests/test_data_health_history_ui.py` (adapter behavior, cache
reuse/force-refresh, AppTest smoke checks for the extended page - 5
tabs present, zero exceptions - and the untouched Equipment Health
page).

### Previous Phase 16 regression

`tests.test_data_health_engine` (Phase 16.1), `tests.test_data_health_ui`
(Phase 16.2), `tests.test_phase16_3_data_health_grounding` (Phase 16.3),
`tests.test_data_health_fleet_engine`/`tests.test_data_health_fleet_ui`
(Phase 16.4): all pass unchanged, 0 modifications to any of those
files or the engine/UI code they test.

### Complete regression

**1084 tests total, 1079 passed, 5 failed, 0 errors, 0 skipped.** (1034
pre-Phase-16.5 baseline + 50 new Phase 16.5 tests = 1084, confirmed.)
Same 5 pre-existing, unrelated failures as every prior phase - 0 new
regressions in the final run.

**A genuine test-isolation bug was found and fixed during this phase's
own regression pass** (not a defect in any Phase 16.1-16.5 production
code): the very first AppTest smoke test ever written against
`ui/pages/12_SCADA_Floor_Plan.py` (added in Phase 16.4) renders a page
whose own body calls `ui.scada_floor_plan_data._load_equipment()`
synchronously - a function `@st.cache_data(ttl=5)`-memoized keyed only
on `plant`, not on `CONFIG_DATABASE_PATH`. That single render left the
shared, process-wide cache warm with real data for up to 5 seconds,
silently defeating `tests/test_scada_floor_plan_data.py`'s
`CONFIG_DATABASE_PATH` monkey-patch whenever it ran shortly afterward
in the same test process (2 failures, reproduced deterministically).
Root-caused via isolation (passed alone, failed only when preceded by
the SCADA smoke test) and fixed by patching `start_snapshot_writer()`
to a no-op and explicitly clearing the `_load_equipment` cache at the
end of that one smoke test (`tests/test_data_health_fleet_ui.py`) -
scoped to the test that introduced the shared-state footprint, not a
change to any production file.

### Live validation

The real live database's Data Health history now contains its actual
first 34 real snapshots (persisted by a real `run_cycle()` call against
`database/simulation/config.db`/`machine_data.db` during this session's
own commissioning pass - genuine production data, not a fixture). A
second real cycle run ~10 minutes later correctly persisted 0 new rows
(34/34 `unchanged`) - the change-detection/heartbeat policy verified
live, not just in unit tests.

### Worker performance (measured, real live databases)

Full-fleet cycle: 6.17-6.30s (consistent with Phase 16.4's own 6.1-6.2s
measurement - the worker calls the identical Phase 16.1 engine).
Stable-state cycle (no real change): 0 persisted / 34 unchanged.
Concurrent `config.db` read probes during an active write-capable
cycle: 35 successful probes, max latency 27.6ms, avg 9.4ms - no
lockups.

### Historical-query/UI performance (measured, real live databases)

Fleet history (34 equipment, 7-day window): 0.54s uncached, ~0.001s
cached. Single-equipment history: ~0.02s. Data Health page cold load
(fleet + history both uncached, 5 tabs rendered): ~11.3s. Warm rerun
(both caches hit): ~2.7s.

### Database/storage measurements (real live data)

34 rows persisted so far, ~165 bytes/row average, `config.db` total
14.20 MB (unchanged from Phase 16.4's own measurement at this scale -
the new table's real footprint so far is negligible). Consistent with
the architecture checkpoint's own projection (tens of thousands of
rows/year, tens of MB/year).

### Remaining limitations / next architectural recommendation

- The `data_health_history_worker.service` has not been started as a
  systemd service in this session (no sudo access here) - the user
  needs to run the enable/start command themselves (see below). Until
  it runs continuously, history only grows when a cycle is manually
  triggered.
- No retention/pruning exists yet (an intentional, documented decision
  at this scale, matching `equipment_health_snapshots`'s own precedent)
  - revisit only if row counts ever approach a scale where it matters.
- Transition `changes` are limited to the aggregate counts this schema
  persists (never a specific tag name) - a disclosed scope limit, not a
  bug; naming specific tags historically would require either
  persisting per-tag detail (a materially larger table) or a separate,
  not-yet-approved design.
- No Equipment Health page history link was added (not required this
  phase, but a natural, low-risk future extension reusing this exact
  read model).

### PHASE 16.5 STATUS: PASS
### READY FOR NEXT PHASE REVIEW: YES

## Phase 17.2b — Historical Evidence Interpretation & Grounding

(Phase 17.1's audit/design report and Phase 17.2a/17.2a.1's context-envelope
work were delivered in conversation only, matching this file's own
established pattern of occasionally lagging chat delivery - not
separately backfilled here since this phase's documentation instruction
scoped only Phase 17.2b itself.)

### Historical prompt contract

Extended `ai/interpretation_prompt_builder.py` (the same, unmodified
Phase 15 prompt path - no second architecture) with renderers for the
three Phase 17.2a historical domains (`data_health_history`,
`health_history`, `asset_performance_history`), each rendered into a
single, clearly delimited `=== HISTORICAL EVIDENCE === ... === END
HISTORICAL EVIDENCE ===` block placed AFTER all current-state domains -
never interleaved with them. A compact, reusable 5-rule "Historical
evidence rules" instruction block is appended to the prompt only when
at least one historical domain was actually fetched
(`context["request"]["history_domains_fetched"]` non-empty) - a
current-state-only question's prompt is byte-for-byte unchanged from
before this phase (confirmed by test).

### Current-vs-history separation

Every historical renderer's own header text states its backward-looking
nature explicitly ("BACKWARD-LOOKING ONLY - does not override the
current ... above") - current-state values are never overwritten,
current and historical sections are structurally separate keys in the
context AND structurally separate, clearly labeled sections in the
rendered prompt.

### Data Health no-history semantics

`_render_data_health_history()` renders the recorded status-duration
percentages (GOOD/DEGRADED/POOR/UNAVAILABLE) and the
no-recorded-history percentage as two STRUCTURALLY SEPARATE lines, with
the no-history line explicitly stating "this is NOT a
GOOD/DEGRADED/POOR/UNAVAILABLE status, it is simply unrecorded." The
grounding guard enforces this at the numeric level too: a claimed
"UNAVAILABLE for 87.3%" is checked against the real `percentages
["UNAVAILABLE"]` value (typically 0.0%), never against
`no_history_percentage` - so mislabeling no-history as any status is
caught by the ordinary numeric-mismatch mechanism, with no separate
validator needed.

### Health / Asset Performance historical interpretation

`_render_health_history()` renders direction/scores/band-transitions
with an explicit "does not override the current Health State/Score
above" disclaimer. `_render_asset_performance_history()` renders each
of the (now ≤3 total, per 17.2a.1) recent observations as an individual
past data point with an explicit "NOT a new trend calculation" header,
and renders maintenance comparisons with an explicit "association only,
NEVER proven causation" header.

### Historical grounding additions (`ai/grounding_guard.py`)

- `health_trend_direction` added as a new single-value enum family
  (reuses the existing enum-check mechanism exactly like `health_band`).
- `performance_state` set-membership now unions current-state dimension
  states with historical observation states (reuses the existing
  set-membership mechanism - no new one built).
- New `_historical_evidence_violations()` function: Data Health history
  status-duration percentage claims, no-history percentage claims,
  Health Score before/after claims ("moved from X to Y"), Asset
  Performance percent-change claims (checked against the real recorded
  observations), a non-causal-maintenance pattern
  (`_MAINTENANCE_CAUSAL_CLAIM_PATTERN`), and a forecasting/RUL-language
  pattern (`_FORECASTING_LANGUAGE_PATTERN`, gated on any historical
  domain being available). All gated on the real historical context
  being present - inert for every current-state-only question.

### Unavailable-history behavior

Every historical renderer independently checks its own domain's
`available` flag - one domain being unavailable never suppresses or
alters another domain's rendering or grounding (confirmed by test).

### Tests

35 new tests (`tests/test_phase17_2b_historical_grounding.py`) covering
current/historical prompt separation, all three historical renderers,
the required adversarial grounding cases (fabricated percentage,
no-history mislabeled as a status, reversed trend direction, wrong
historical score, forecasting/RUL language, fabricated performance
state, unauthorized maintenance causality claim), legitimate-answer
pass-through, and regression safety (history remains default-off, the
existing Phase 16.3 conflation check still works, COMPARISON/
FACTORY_SUMMARY remain skipped).

### Regression

Targeted: `test_phase15_grounding_guard`, `test_phase15_prompt_builder`,
`test_phase15_context_builder`, `test_phase15_ask_integration`,
`test_phase16_3_data_health_grounding`, `test_phase17_2a_context_history`,
`test_phase17_2b_historical_grounding` - **152/152 pass**. Full suite:
**1147 tests, 1142 passed, 5 failed (same pre-existing, unrelated
failures), 0 new regressions** (1112 + 35 new = 1147, confirmed).

### Performance

Context build: current-state-only ~0.226s, with all 3 historical
domains ~0.288s (~+0.06s). Prompt construction and grounding validation
are both sub-millisecond regardless of history (~0.0001-0.0002s) -
confirms the deterministic layer stays negligible next to real LLM
generation time. Prompt size: current-state-only ~5,283 chars (~1,321
tokens), with all 3 historical domains ~7,961 chars (~1,990 tokens) -
the incremental historical-rules-instruction cost itself is ~504 chars
(~126 tokens), on top of the ~495-token historical context data
established in 17.2a.1.

### Live validation

Real Ollama (`qwen2.5:7b`) completion against `P01.WATER.WSP01`, which
had genuine recorded evidence in all three historical domains at once -
satisfying the individual Data-Health/Health/Asset-Performance cases
and the combined case in a single real call. Question: "Has this
pump's data quality and health been reliable over the past week?"
Generated in 1713.4s (consistent with this VM's known ~0.6-0.8
tokens/sec CPU-only characteristic). Real answer (excerpt): "No
recorded Data Health history for 87.2% of the requested window...
Time-in-status over the RECORDED portion of the window: GOOD 12.8%...
Direction: DETERIORATING... Persisted Health Score moved from 71.1 to
66.2 (change -4.9)." **Grounding result: `grounded=True`, zero
violations** - every cited number/state (GOOD 12.8%, no-history 87.2%,
DETERIORATING, 71.1→66.2, -4.9) matched the persisted values exactly,
and critically the model correctly kept the 12.8% GOOD status and the
87.2% no-history figure as two separate, correctly-labeled facts - the
exact scenario this phase exists to protect against conflating. No
forecasting language, no causal claim (the response was truncated by
the existing 200-token generation cap before reaching Asset
Performance - a known, pre-existing Phase 15 characteristic, not a
17.2b regression).

### Remaining limitations

- Historical retrieval remains fully opt-in (`history_domains`
  defaults to none) - no automatic historical-intent routing exists
  yet; that is explicitly Phase 17.2e's job.
- No generalized time-scope resolver ("since maintenance", "last 24
  hours") - only the explicit bounded windows already supported by
  `ai/context_builder.py`'s `history_window_days` parameter.
- Not every field named in the approved plan has its own dedicated
  grounding pattern (e.g. `recurring_issues` counts, `evidence_quality`,
  individual band-transition pairs beyond the direction enum) -
  deliberately, per "protect authoritative structured values, not
  every prose statement"; the highest-risk claims (percentages, scores,
  direction, no-history conflation, forecasting, causality) are
  covered.
- The one real live completion was truncated by the existing 200-token
  generation cap before reaching the Asset Performance section - a
  pre-existing Phase 15 characteristic, unrelated to this phase.

### PHASE 17.2B STATUS: PASS
### READY FOR NEXT PHASE REVIEW: YES

---

## Phase 17.2c — Deterministic Comparison Interpretation

### Clarification on sub-phase lettering (factual addendum, not a rewrite)

Phase 17.1's own audit report proposed a provisional 17.2a/b/c/d/e
lettering (17.2a = context envelope, 17.2b = prompt builder, 17.2c =
grounding guard extension, 17.2d = comparison verdict, 17.2e =
automatic historical-intent routing). That proposal is **superseded**:
the actual Phase 17.2b request (see above) already absorbed the former
"17.2c" grounding-extension scope into itself. Phase 17.2c is now
**authoritatively redefined** (this section) as **Deterministic
Comparison Interpretation** - the work described below, unrelated to
the original 17.2c proposal's grounding-guard scope (already done) or
17.2d's comparison-verdict idea (this phase supersedes and subsumes
that too, since a deterministic comparison-facts contract IS the
verdict work, done properly). The prior "Remaining limitations" note
above referencing "Phase 17.2e's job" for automatic historical-intent
routing still stands unchanged - that piece remains a distinct, not-yet
-scheduled future step, not renamed by this addendum.

### Objective

Improve Ask AI comparison answers so conclusions rest on explicit
deterministic side-by-side evidence (`comparison_facts`) rather than
free LLM inference from two loosely-rendered entity blocks. Not a new
ranking engine, not a combined score, not automatic historical routing,
not RAG, not a second AI pipeline.

### Files modified

`ai/context_builder.py` (comparison-facts contract + `history_domains`
passthrough on `build_comparison_ai_context()`), `ai/interpretation_prompt_builder.py`
(comparison-facts renderer + strengthened COMPARISON instruction),
`ai/grounding_guard.py` (comparison-specific grounding path, replacing
the blanket COMPARISON skip for real comparison contexts),
`engine/industrial_query_engine.py` (one genuine gap found during
audit: `resolve_equipment_pair()` silently allowed both sides to
resolve to the same equipment; now returns a new `"duplicate_entity"`
status), `engine/models.py` (docstring update for the new status
value). `app/ask.py` was audited and needed **zero changes** - its
existing `if pair.status != "resolved"` fallback already handles the
new status correctly by construction.

### Existing components reused

Entity resolution (`resolve_equipment`/`resolve_equipment_pair`),
ordered A/B handling, plant isolation, ambiguity handling, session
handling, the Phase 17.2a/17.2b context/prompt/grounding architecture,
provider abstraction, one-call interpretation path. No second
comparison resolver, no parallel AI pipeline.

### Deterministic comparison-facts contract

`ai/context_builder._build_comparison_facts(entity_a, entity_b)` reads
only from the two already-built entity contexts - Health (score
difference, lower-score entity, band/confidence/provisional shown
separately, never reordered by them), Maintenance Intelligence
(priority ordering via the existing `engine.maintenance_intelligence_targets.PRIORITY_SORT_RANK`
- never a new ordering), Asset Performance (only dimensions present on
**both** equipment are compared, via `percent_change` - the
self-referential, unitless quantity Phase 8/14 already established as
each entity's own normalized deviation; raw `observed_value` stays
informational, never ranked), Data Health (Data Confidence score
comparison - the same 0-100 scale regardless of equipment type),
Energy Opportunity (observed excess cost only - `estimated_potential_saving`
is never even present in the contract, so no cross-field inference is
possible), Savings Verification (both results shown, never ranked).
Historical comparison facts (`data_health_history`/`health_history`)
are included only when `history_domains` caused **both** sides to
carry that domain - otherwise the key is absent entirely.

### Supported comparable domains / NOT_COMPARABLE behavior

Every domain fact carries `"comparable": bool`. Asset Performance's
comparability is decided purely by shared `target_key` intersection -
proven live: `P01.WATER.WSP01` vs `P01.UTILITY.CHL01` (pump vs chiller)
correctly returns `comparable=False`, `shared_dimensions=[]`, with zero
special-casing for equipment type (the type difference simply never
produces a key overlap). The prompt renders `NOT_COMPARABLE` explicitly
for any `comparable=False` domain, and the COMPARISON instruction tells
the model to say so plainly rather than guess.

### Duplicate-entity handling

Audit found a genuine gap: "compare WSP01 with WSP01" previously
resolved both sides to the same equipment with `status="resolved"`.
`resolve_equipment_pair()` now detects `entity_a.instance_key ==
entity_b.instance_key` and returns `status="duplicate_entity"` with an
explanatory message - caught by `app/ask.py`'s existing fallback logic
unmodified, confirmed live (no LLM call made).

### Grounding-guard extension

`check_grounding()`'s COMPARISON handling no longer blanket-skips:
when `context["comparison_facts"]` is present, a new
`_comparison_violations()` checks explicitly-labeled ("Entity A"/
"Entity B") numeric claims (Health Score, Data Health score) and
ordering claims (lower Health Score, higher Maintenance Priority,
poorer Data Health, higher observed excess cost, larger historical
Health decline) against the real facts, plus a blanket
overall-winner/combined-score/risk-score pattern. **Backward
compatibility preserved exactly**: a COMPARISON context with no
`comparison_facts` (e.g. the original Phase 15 test's empty `{}`
context) still returns `checked=False` - the original Phase 15
limitation is never weakened, only additively extended for real
comparison contexts, which always carry `comparison_facts` now.

### Tests

44 new tests (`tests/test_phase17_2c_comparison_interpretation.py`):
entity resolution/duplicate rejection, the full comparison-facts
contract against real live data, no-aggregate-verdict confirmation,
grounding for every comparison claim family (Health, Maintenance,
Data Health, Asset Performance, Energy), backward-compatibility for
the original Phase 15 skip contract, and regression safety (no DB
writes, current-state-only prompts unaffected, history stays opt-in).

### Regression

Targeted: 207/207 pass across every Phase 15/16.3/17.2a/17.2b/17.2c
grounding/context/prompt test file. Full suite: **1191 tests total
(1147 + 44 new, confirmed), 1186 passed, 5 failed, 0 errors** - same 5
pre-existing, unrelated failures as every prior phase, **0 new
regressions**.

### Live validation

Two real Ollama (`qwen2.5:7b`) comparison scenarios, run sequentially
against real live persisted data:

1. **P01.WATER.WSP01 vs P02.WATER.WSP01** (two Water Supply Pumps
   across P01/P02, different Health states, tied Data Health) - prompt
   12,802 chars. **Timed out after 1800s** (`OLLAMA_READ_TIMEOUT`) with
   no completion - a genuine, honestly-reported finding (this
   comparison prompt is larger than a single-equipment prompt, and
   pushed generation past even a generous extended budget on this
   CPU-only VM this run). The deterministic facts themselves were
   still verified correct (see the logged `comparison_facts` printout)
   - only the LLM generation step didn't finish in time. In the real
   Ask AI path this exact scenario would have hit the existing,
   already-tested provider-exception fallback
   (`_render_interpretation_answer()`'s try/except around
   `provider.generate()`), never a crash.
2. **P01.WATER.WSP01 vs P01.UTILITY.CHL01** (cross-equipment-type: pump
   vs chiller) - prompt 12,376 chars, generated in 1715.0s. **Real
   answer excerpt**: "P01 WSP01: A=66.2, B=77.8 (difference 11.6, lower
   score: Entity A)... P01 WSP01: REVIEW, P01 CHL01: REVIEW" - every
   cited number/state matched the persisted `comparison_facts` exactly
   (score difference 11.6, lower-score entity A, tied REVIEW priority
   correctly stated as tied rather than a false ordering claim).
   **Grounding result: `grounded=True`, `checked=True`, zero
   violations** - the new comparison-specific grounding path activated
   correctly on real output. The response was truncated by the existing
   200-token generation cap before reaching the Asset Performance
   section, so live confirmation of NOT_COMPARABLE wording in generated
   prose wasn't captured directly - the deterministic facts printout
   confirms the system itself correctly computed
   `asset_performance.comparable=False` for this cross-type pair, which
   is what the model would have been constrained to state.

### Remaining limitations

- No deterministic per-dimension comparison exists for Anomalies or
  Production Context - out of this phase's explicit domain list.
- Comparison grounding requires an explicit "A"/"Entity A"/"B"/"Entity
  B" label in the claim text to be checkable (mirrors the same
  documented false-negative-over-false-positive philosophy the rest of
  `ai/grounding_guard.py` already uses) - a claim phrased purely by
  equipment name/alias with no letter label is not checked.
- Automatic historical-intent routing for comparisons remains
  unscheduled, as before.

### PHASE 17.2C STATUS: PASS
### READY FOR NEXT PHASE REVIEW: YES

---

## Phase 17.2d — Comparison Context Compaction & Latency Optimization

### Objective

Phase 17.2c established correctness/grounding for comparisons, but live
Ollama performance was unacceptable - the full comparison prompt
rendered BOTH entities' complete `render_equipment_context()` dump
(every domain, every factor, every recommended check) plus the
deterministic comparison facts, at ~3,193 tokens for a routine two-
equipment comparison. This phase compacts the RENDERED prompt - never
the underlying deterministic facts, never grounding - so the LLM sees
the smallest safe interpretation context while `comparison_facts`
(what grounding checks against) stays exactly as complete as before.
Not a new AI architecture, not a new ranking engine, not a model
change - a context-compaction and latency phase only.

### Files modified

`ai/interpretation_prompt_builder.py` (compact comparison envelope:
new `_render_compact_entity_identity()` replaces the full per-entity
dump for comparisons; `_render_comparison_facts()` gained an optional
`render_domains` narrowing parameter; `render_comparison_context()` is
now the compact LLM-facing renderer; new `render_comparison_context_full()`
preserves the original Phase 17.2c full-dump rendering for the no-LLM
deterministic fallback, which has no model to phrase a compact summary
and must show the complete picture directly; `build_deterministic_fallback()`
updated to call it; the `COMPARISON` prompt instruction no longer names
a fixed 6-domain list, since a narrow question now renders fewer),
`ai/interpretation_intent.py` (new `comparison_domain_hint()` - reuses
the existing `_TRIGGER_PHRASES` per-intent phrase tuples, never a new
keyword list or classifier, plus one small explicit Data Health phrase
set since Data Health is cross-cutting and has no intent of its own),
`ai/context_builder.py` (new `DOMAINS_BY_INTENT["COMPARISON"]` entry -
5 domains instead of `GENERAL_ENGINEERING_QUERY`'s 9, dropping
anomalies/events/maintenance_history/production_context, which have no
comparison_facts contract and were never used by comparisons;
`build_comparison_ai_context()`'s `intent` default changed from
`"GENERAL_ENGINEERING_QUERY"` to `"COMPARISON"`; `comparison_domain_hint(question)`
now populates `context["request"]["comparison_domain_hint"]`;
`_maintenance_comparison_facts()` gained `recommended_checks` (bounded
to top 2) per side). `ai/grounding_guard.py` needed **zero changes** -
confirmed by running its full test suite unchanged (57/57 pass):
COMPARISON grounding reads `context["comparison_facts"]` directly,
never the rendered prompt text, so it was already unaffected by any
rendering-layer compaction.

### Where compaction happens (and where it deliberately does not)

The primary latency lever is RENDERED PROMPT SIZE, not database query
narrowing - context-build time is ~0.6-0.9s regardless, negligible next
to LLM generation time (1200-1800+s on this hardware). The DB-query
narrowing (9→5 domains) is still a real, secondary optimization, but
the dominant saving comes from replacing each entity's full
`render_equipment_context()` dump (every domain's full detail, ~2,000
tokens per pair) with a two-line compact identity block (equipment
name, instance key, plant/area/system - nothing more). Grounding never
needed a separate "richer context" structure (item 12's evaluation):
`_build_comparison_facts()` is called unconditionally against the full
5-domain entity data regardless of what the renderer narrows, so
`comparison_facts` is always complete - compaction is entirely confined
to the rendering layer.

### Compact comparison envelope

`render_comparison_context()` now renders: two one-line entity identity
blocks, then the deterministic comparison facts in the compact style
(`"Health: A: score=68.7, confidence=HIGH / B: score=82.1,
confidence=HIGH / Deterministic result: A lower by 13.4"` - concise
structured language, never cryptic abbreviations). NOT_COMPARABLE
dimensions are stated plainly, never guessed at.

### Domain selectivity

`comparison_domain_hint(question)` narrows which domains get RENDERED
(never which get computed) when a question clearly names one specific
domain (e.g. "compare health score of P01 WSP01 and P02 WSP01" ->
`("health",)`) - Data Health is always rendered regardless of the hint,
since Data Health qualification (item 9) must survive narrowing to any
other domain. A broad question (no domain named) renders every
supported domain, still in the compact style, never two full entity
dumps. **Documented, disclosed limitation**: the hint reuses
`_TRIGGER_PHRASES`'s existing per-intent phrase list verbatim rather
than inventing new vocabulary, so a phrasing not already in that
vocabulary (e.g. "compare **the health of** P01 WSP01..." - the exact
phrase "the health of" isn't one of `HEALTH_EXPLANATION`'s trigger
phrases) falls back to broad/unnarrowed rendering rather than guessing.
This was a deliberate choice over expanding the shared trigger-phrase
list, which would have a much larger blast radius across all 8 other
intents' classification.

### Prompt size measured (real live data)

| Scenario | Phase 17.2c (chars) | Phase 17.2d (chars) | Phase 17.2d (~tokens) | Reduction |
|---|---|---|---|---|
| Broad, WSP01 vs WSP02 | 12,802 | 4,769 | ~1,192 | 62.7% |
| Broad, WSP01 vs CHL01 | 12,376 | 4,675 | ~1,168 | 62.2% |
| Narrow (Health only) | n/a (new) | 3,958 | ~989 | n/a |
| Narrow (Data Health only) | n/a (new) | 3,753 | ~938 | n/a |
| Narrow (Savings Verification) | n/a (new) | 3,827 | ~956 | n/a |

Both budget targets from this phase's spec are met: broad comparisons
land at ~1,168-1,192 tokens (target ≤2,000), narrow comparisons at
~938-989 tokens (target ≤1,200).

### Tests

31 new tests (`tests/test_phase17_2d_comparison_compaction.py`):
`comparison_domain_hint()` unit tests (all 6 domain phrasings + broad-
returns-None + documented unmatched-phrasing-returns-None),
`_render_comparison_facts()` domain-selectivity unit tests (hand-built
facts - broad renders everything, narrow excludes unrelated domains,
Data Health always rendered even when narrowed elsewhere, NOT_COMPARABLE
rendered plainly, recommended checks bounded/rendered, history only
rendered when its owning current-state domain is wanted, compact style
confirmed non-cryptic), and real-live-DB integration tests (compact
identity present, full per-entity dump absent, broad still contains
every domain, narrow excludes unrelated domains, comparison_facts
unaffected by render narrowing, grounding still works with narrow
rendering, cross-equipment NOT_COMPARABLE gate preserved, duplicate-
entity still rejected before the LLM, deterministic fallback uses the
full non-compact renderer, no DB writes, prompt-size budget for both
broad and narrow, no prediction/RUL language removed from the compact
prompt). One existing Phase 17.2c test
(`test_render_comparison_context_still_shows_both_entities`) was
updated to match the now-compact format (its exact-format assertion
was tied to the old full-dump rendering by design); a new
`test_render_comparison_context_full_still_shows_both_entities` test
was added alongside it to keep that exact full-dump assertion alive
against the new `render_comparison_context_full()`.

### Regression

Targeted: 57/57 pass across `test_phase15_grounding_guard.py` +
`test_phase17_2c_comparison_interpretation.py` (confirms grounding
needed zero changes and the one updated test is correct). Full suite:
**1,223 tests total (1,191 baseline + 31 new + 1 updated), 1,218
passed, 5 failed, 0 errors** - the same 5 pre-existing, unrelated
alias-classification failures as every prior phase
(`test_compressor_aliases` ×2 sub-cases, `test_typo_tolerant_aliases`,
`test_clear_equipment_uses_rules`, `test_compressor_pressure_alarm_pipeline`),
**0 new regressions**.

### Live validation

Five real Ollama (`qwen2.5:7b`) comparison scenarios, run sequentially
against real live persisted data (`OLLAMA_READ_TIMEOUT=1800`):

1. **Broad, P01.WATER.WSP01 vs P02.WATER.WSP01** (repeat of the 17.2c
   scenario that also timed out) - prompt 4,769 chars (~1,192 tokens).
   **Timed out after 1800.1s again**, with no completion. This run
   substantially overlapped with this phase's own full regression suite
   running concurrently on the same 8-core CPU-only VM - a genuine
   confound acknowledged here rather than hidden. A clean, honest
   finding regardless: compacting the prompt did **not**, by itself,
   guarantee generation within the extended budget for this exact
   scenario on this hardware.
2. **Broad, P01.WATER.WSP01 vs P01.UTILITY.CHL01** (repeat of the 17.2c
   scenario that completed in 1715.0s) - prompt 4,675 chars (~1,168
   tokens), **generated in 1488.0s** (13.2% faster than the 17.2c
   baseline for the same scenario, despite a 62.2% prompt-size cut -
   see "Remaining bottleneck" below for why prompt size and generation
   time didn't scale together). `grounded=True`, `checked=True`, zero
   violations - every cited number matched the persisted
   `comparison_facts` exactly.
3. **Narrow (Health only), P01.WATER.WSP01 vs P02.WATER.WSP01** - new
   scenario, prompt 3,958 chars (~989 tokens), generated in 1246.2s.
   The model correctly stayed on Health only (no unrelated domains
   mentioned), correctly stated "Entity A's health score is lower by
   7.0" matching the persisted facts exactly. `grounded=True`,
   `checked=True`, zero violations.
4. **Narrow (Data Health only, poorer case), P01.WATER.WSP01 vs
   P01.UTILITY.CHL01** - new scenario, prompt 3,753 chars (~938
   tokens), generated in 1467.8s. The model's answer was factually
   correct (WSP01 Data Health 91.0 GOOD, CHL01 88.0 GOOD, both restated
   accurately) but **`grounded=False`** - see "Grounding false-positive
   discovered" below. `app/ask.py`'s existing fail-safe replaced the
   answer with the deterministic fallback; no incorrect information
   reached the user.
5. **Narrow (Savings Verification, expected unavailable),
   P01.WATER.WSP01 vs P02.WATER.WSP01** - new scenario, prompt 3,827
   chars (~956 tokens), generated in 1510.0s. The model correctly said
   "we cannot definitively state... there is no savings verification
   result for either pump," matching the persisted
   `comparison_facts.savings_verification.comparable=False` exactly -
   no fabricated verdict. `grounded=True`, `checked=True`, zero
   violations.

### Grounding false-positive discovered (pre-existing, not a Phase 17.2d regression)

Scenario 4's rejection was traced to
`ai/grounding_guard.py`'s `_COMPARISON_ENTITY_VALUE_PATTERNS["health_score"]`
regex: `\b(?:entity\s+)?([AB])\b.{0,40}?health\s+score\s+(?:is|of|was)\s+(-?\d+\.?\d*)`
is case-insensitive and has no `entity` requirement, so the English
indefinite article "**a**" in the model's sentence "WSP01 (P01) has
**a** Data Health score of 91.0" matched the `[AB]` group as if it
meant "Entity A" - the pattern then found "Health score of 91.0"
immediately after (a true substring of "Data Health score of 91.0")
and flagged it as a claimed **Health Score** of 91.0 for Entity A,
which doesn't match Entity A's real Health Score of 66.2 (91.0 is
actually Entity A's real Data Health score, correctly stated). This
regex is unmodified Phase 17.2c code - it was never exercised by
17.2c's own live validation (which only tested broad comparisons) and
was only exposed now because narrow Data-Health rendering leads the
model toward a phrasing style ("has a Data Health score of X") that
happens to collide with the bare-article ambiguity. **Not fixed in
this phase** - per this phase's explicit scope (compaction/latency
only, no grounding changes without the same rigor Phase 17.2c's own
grounding work went through) and the standing instruction to STOP after
Phase 17.2d for review, this is reported as a discovered limitation for
a future, dedicated grounding-hardening pass, not patched ad hoc.
Critically, the system's fail-safe behaved correctly throughout: the
false rejection cost a correct LLM phrasing, not correctness - the
engineer would have seen the accurate deterministic fallback instead.

### Runtime/Ollama diagnostics (informational only - no config changed)

- Model: `qwen2.5:7b`, 7.6B parameters, `Q4_K_M` quantization, 32,768
  token model-native context length.
- **`ai/providers/ollama_provider.py`'s `_payload()` hardcodes
  `num_ctx: 2048`** for every request (unrelated to the model's native
  32,768 limit) - Phase 17.2c's full comparison prompt (~3,193 tokens)
  **exceeded this 2,048 window**, forcing llama.cpp's context-shift
  mechanism (`--context-shift`, confirmed present in the running
  `llama-server` process arguments) to engage. This is a credible
  contributing factor to Phase 17.2c's 1715s/timeout results,
  independent of the CPU-only hardware constraint. Phase 17.2d's
  compacted prompts (938-1,192 tokens) now fit safely under this
  window with no context-shift involvement - reported here as a
  finding, **not changed**, since it's a cross-cutting provider setting
  affecting every intent, not just comparisons, and outside this
  phase's declared scope.
- No GPU present or used (confirmed - no `nvidia-smi`, no GPU-layer
  flags in the `llama-server` process arguments); this VM has **8 CPU
  cores / 32GB RAM** (Intel i7-1255U), not the 4-core/16GB this project
  document previously stated - a documentation staleness finding,
  corrected in this project's CLAUDE.md-equivalent notes going forward.
- `num_predict: 200` (output token cap, also hardcoded in the same
  payload) is unrelated to prompt size and was unchanged across every
  scenario.
- Live CPU contention observed during this validation: this phase's own
  full regression suite (1,223 tests, ~12 minutes) ran concurrently
  with part of Scenario 1's generation window, plus the always-on
  `plc_logger.service`/`event_monitor.service`/`streamlit.service`
  systemd units and this Claude Code session itself all compete for the
  same 8 cores continuously - `load average` was observed at 11.6 with
  only 8 cores available (i.e. the system was oversubscribed) during
  the validation window. This is disclosed as a measurement confound,
  not eliminated (no service was stopped to "clean up" the
  measurement, per the read-only/no-service-disruption discipline this
  project follows).

### Remaining bottleneck

Prompt compaction achieved its stated targets (≤2,000/≤1,200 tokens)
and removed a genuine context-window-overflow condition, but did
**not** proportionally reduce generation time - a 62% prompt reduction
(Scenario 2) yielded only a 13.2% generation-time improvement, and the
identical broad scenario that timed out in Phase 17.2c timed out again
here despite the smaller prompt. This confirms the hardware/runtime
itself - CPU-only inference on an oversubscribed 8-core VM, `qwen2.5:7b`
at Q4_K_M quantization, plus real contention from always-on services -
remains the dominant latency factor, exactly as Phase 17.2c already
warned and as this project's own documentation has stated since before
this phase began. No model change was made or is recommended as part
of this phase (per this phase's explicit no-model-change constraint);
a GPU/cloud-API upgrade, previously deferred pending pipeline-design
validation, is now the most credible next lever for latency, separate
from any further prompt engineering.

### Remaining limitations

- The grounding false-positive described above (bare-article/Entity-
  A-letter regex collision) remains unfixed, scoped to a future
  dedicated grounding-hardening pass.
- `comparison_domain_hint()`'s phrase-reuse limitation (documented
  above) means some intuitively domain-specific phrasings still render
  broad rather than narrow - a disclosed, accepted trade-off, not a bug.
- Generation latency, even for a compacted narrow single-domain
  comparison, remains 1200s+ on this hardware - compaction alone does
  not make comparisons fast enough for interactive use; see "Remaining
  bottleneck" above.
- Automatic historical-intent routing for comparisons remains
  unscheduled, unchanged from Phase 17.2c.

### PHASE 17.2D STATUS: PASS
### READY FOR NEXT PHASE REVIEW: YES

---

## Phase 17.2e — Comparison Grounding Hardening

**Completed:** date not recorded at the time; reconstructed 2026-08-19
from implementation evidence during a durability/documentation
checkpoint. This section was written after the fact - the objective
and evidence below come entirely from the code, its comments, and its
tests, not from a contemporaneous author account.

### Objective (from the code's own account)

Fix a false-rejection bug in `ai/grounding_guard.py`'s comparison
grounding checks, discovered live during Phase 17.2d Scenario 4. Every
comparison-specific entity/value extraction pattern used
`\b(?:entity\s+)?([AB])\b` - the "Entity " prefix was *optional*, so
under `re.IGNORECASE` a bare, ordinary English article "a" (e.g.
"...has **a** Data Health score of 91.0") could satisfy `([AB])\b` on
its own with no "Entity" word nearby, and be misread as a claim about
"Entity A" - producing a false grounding violation against an answer
that was actually factually correct.

### Fix

`ai/grounding_guard.py:350` - the optional-prefix pattern was replaced
with one that requires an explicit "Entity "/"entity " label before
the letter; a prior comment describing support for a "bare A/B" label
(no "Entity" word) was also removed, since under `IGNORECASE` a bare
letter can never distinguish a deliberate capital-A label from an
ordinary sentence-initial "A".

### Tests

`tests/test_phase17_2e_comparison_grounding_hardening.py` - 22 tests,
all passing in the regression run performed for this checkpoint:
- `TestReproduceArticleFalseRejection` - reproduces the exact live
  failure shape (written against the bug first, per the file's own
  stated convention).
- `TestAdversarialArticleAcrossAllPatternFamilies` - the same article-
  "a" hazard checked against every comparison fact family (health,
  maintenance priority, data health, energy opportunity, health
  history), not just the one that triggered the live bug.
- `TestLegitimateEntityReferencesStillWork` - genuine "Entity A"/
  "Entity B" labels (including mixed-case, and a bare article
  alongside a real label in the same sentence) remain checkable.
- `TestEquipmentNameOnlyClaimsRemainUnchecked` - confirms the
  pre-existing, deliberate false-negative-over-false-positive design
  (claims naming equipment instead of A/B are never checked, even if
  wrong) is unchanged by this fix, not a regression.
- `TestNumericGroundingUnweakened`,
  `TestNotComparableAndUnavailableUnweakened`,
  `TestDataHealthAndHealthDomainsTogether`,
  `TestFalseAcceptanceProtectionUnweakened` - confirm the fix narrowed
  the *false-positive* hazard without loosening any of grounding's
  existing false-negative protections (fabricated numbers, swapped A/
  B values, NOT_COMPARABLE claims, aggregate "overall winner"
  language) - all still correctly rejected.

### Verification performed for this checkpoint

Ran the full test file directly - all 22 tests pass. Also confirmed
via the full `tests/` suite run (1337 passed / 22 subtests passed / 5
failed, all 5 pre-existing and unrelated - see the regression section
of this checkpoint) that this file contributes zero failures.

### PHASE 17.2E STATUS: PASS (verification reconstructed after the fact,
evidence-based, not contemporaneously recorded)

---

## Phase 18.1a — Historian Retention & Backup Worker

**Completed:** date not recorded at the time; reconstructed 2026-08-19
from implementation evidence during a durability/documentation
checkpoint, same basis and caveat as Phase 17.2e above.

### Objective (from the code's own account)

Build the historian's retention (row cleanup) and backup mechanism as
a standalone worker, `app/historian_maintenance_worker.py`, deployed
via `deploy/systemd/historian_maintenance_worker.service` -
`WorkerType=simple`, running continuously alongside the other Phase
6-16 background workers.

### What exists

- **`database/backup.py`** - `backup_sqlite_database()` (SQLite Online
  Backup API - `sqlite3.Connection.backup()` - never a raw file copy
  of a database that may be under active write load) and
  `check_backup_preflight()` (disk-space safety gate: requires
  `database_size + database_size + minimum_free_reserve_bytes` free,
  the conservative ~2x-size formula, not the naive
  `free_space > database_size` check).
- **`app/historian_maintenance_worker.py`** - pure, testable functions
  (`run_retention_if_due`, `run_backup_if_due`, `_is_due`,
  `_prune_old_backups`, `_read_last_run`/`_write_last_run`,
  `read_last_backup_summary`) called from a `run_forever()` loop (the
  loop itself is untested, matching this project's established
  convention of only testing a worker's pure logic, e.g.
  `app/energy_kpi_worker.py`).
- **`database.DatabaseManager.backup()`** - delegates to
  `backup_sqlite_database()`.
- Retention and backup are independent settings - disabling one does
  not affect the other.

### Tests

`tests/test_historian_maintenance_worker.py` - 34 tests, all passing:
marker-file round-trip/due-interval logic; retention deletes only
rows older than the window and never touches `machine_events` or the
STRING-tag `plc_text_data` table; `backup_sqlite_database()` produces
a queryable, byte-for-byte-correct copy and does not leak
`plc_data`/`plc_text_data` tables into a `config.db`-shaped backup;
preflight's conservative 2x-size formula (explicitly tested against
the naive alternative to prove the stricter one is what's actually
used), raises on a missing source database, never creates the
destination directory itself, and works before that directory exists;
backup-pruning keeps only the N most recent files per prefix and never
touches a different prefix; and `run_backup_if_due()` backs up both
databases independently (one's failure doesn't block the other),
skips cleanly without deleting or attempting anything when preflight
fails, never prunes an existing good backup on a skipped attempt, and
writes/reads back a real JSON summary.

### Verification performed for this checkpoint

Ran `tests/test_historian_maintenance_worker.py` directly - 34/34
pass. `historian_maintenance_worker.service` confirmed live and
active (see Part I / service inventory in this checkpoint's report).

### PHASE 18.1a STATUS: PASS (verification reconstructed after the
fact, evidence-based, not contemporaneously recorded)

---

## Phase 18.1a.1 — Tariff Provenance Correction

**Completed:** date not recorded at the time; reconstructed 2026-08-19
from implementation evidence during a durability/documentation
checkpoint, same basis and caveat as Phase 17.2e/18.1a above.

### Objective (from the code's own account)

`engine/energy_tariff.py`'s `tariff_provenance_label()` docstring
names itself "Phase 18.1a.1" and describes the fix directly: map an
already-resolved tariff row's own `is_simulated` flag (set once, at
creation time, by the same Factory Configuration tariff form that
already sets/clears it) to an explicit `CONFIGURED`/`SIMULATION`
label - **never** a second, parallel provenance system, and never
inferred from a proxy such as currency (a MYR tariff is not
automatically "simulation"; a non-MYR tariff is not automatically
"real"). A tariff that could not be resolved for the requested scope/
date (`tariff=None`) returns `None`, never a guessed label - callers
are expected to leave their own cost/provenance fields "Unavailable"
in that case, per this project's existing never-fabricate discipline.

### What exists

`TARIFF_PROVENANCE_CONFIGURED`/`TARIFF_PROVENANCE_SIMULATION`
constants and `tariff_provenance_label()` in `engine/energy_tariff.py`,
actually consumed by two live engines -
`engine/opportunity_engine.py` and
`engine/savings_verification_engine.py` (confirmed by direct
`grep`, not assumed) - not an orphaned/unused helper.

### Tests

`tests/test_energy_tariff.py::TestTariffProvenanceLabel` - 4 tests:
`None` tariff returns `None`; `is_simulated=1` returns `SIMULATION`;
`is_simulated=0` returns `CONFIGURED`; a MYR-currency tariff with
`is_simulated=0` still returns `CONFIGURED` (proving currency is
never used as a provenance proxy). Also covered indirectly by
`tests/test_opportunity_engine.py` and
`tests/test_savings_verification_engine.py` wherever they exercise
tariff-dependent code paths.

### Verification performed for this checkpoint

Ran `tests/test_energy_tariff.py` directly - all pass. Confirmed via
`grep` that both consuming engines actually import and call
`tariff_provenance_label()`/the provenance constants, not just define
them.

### PHASE 18.1A.1 STATUS: PASS (verification reconstructed after the
fact, evidence-based, not contemporaneously recorded)

---

## Reconciliation note — "outstanding" worker installs (Phases 6-12)

Several earlier phase entries in this document (Phase 4 - `To install
production_simulator.service (still outstanding)`; Phase 6 - `To
install energy_kpi_worker.service (outstanding)`; Phase 8 - `To
install baseline_worker.service (outstanding, alongside...)`; Phase 9
- `To install anomaly_worker.service (alongside...)`; Phase 10 - `To
install opportunity_worker.service (alongside...)`; Phase 12.2's
commissioning entry) describe those workers as not yet installed under
systemd **at the time each of those phases was written**. That
historical text is left as-is below, since it accurately describes
state at that point in the project.

**Current state, confirmed live during this checkpoint (2026-08-19):**
all of `production_simulator.service`, `energy_kpi_worker.service`,
`baseline_worker.service`, `anomaly_worker.service`,
`opportunity_worker.service`, plus `savings_verification_worker`,
`equipment_health_worker`, `asset_performance_worker`,
`data_health_history_worker`, and `historian_maintenance_worker`, are
installed, `Restart=always`, and `active (running)` under systemd -
13 project services total, confirmed via `systemctl status` (see this
checkpoint's report, item I). None of the "outstanding" installs above
remain outstanding.

---

## BACKUP CAPABILITY PREPARED — AUTOMATIC BACKUP DISABLED

**Completed:** 2026-08-19

### Current storage constraint

This VM has 78GB total disk, ~33GB free at the time of this work. The
live historian (`database/simulation/machine_data.db`) is already
~2.6GB. A single scheduled backup cycle (config.db + machine_data.db)
consumes ~2.66GB, and the configured 7-backup retention would
eventually consume roughly 18-19GB just for machine-data backups -
non-trivial relative to available headroom. Automatic backup execution
is therefore deliberately **disabled by default** on this VM until
disk is expanded or a larger/external destination is configured - the
mechanism itself is fully implemented and tested.

### Backup architecture reused/created

Reused unchanged: `database/backup.py`'s `backup_sqlite_database()`
(SQLite Online Backup API, from Phase 18.1a) and
`app/historian_maintenance_worker.py`'s existing retention+backup
worker/service (also Phase 18.1a) - no second/parallel backup
mechanism was created. Extended: `database/backup.py` gained a new
disk-space preflight function; the worker gained a preflight gate
before each database's backup attempt and a small JSON summary marker
alongside its existing timing marker. New: `database/backup_status.py`
(read-only status model for a future Data Management panel).

### SQLite-safe backup method

Unchanged from Phase 18.1a - `sqlite3.Connection.backup()` (the Online
Backup API), never a raw file copy of the actively-written
`machine_data.db`.

### Configuration flag

`[HISTORIAN_MAINTENANCE] backup_enabled` in `config/settings.ini` -
now set to `false` (was `true`). `ConfigManager.historian_backup_enabled`'s
fallback (used if the key/section is ever missing) was also changed
from `True` to `False`, so a fresh or partially-upgraded settings.ini
can never silently enable backup by omission.

### Destination configuration

`[HISTORIAN_MAINTENANCE] backup_destination_dir` (unchanged key,
still `backups`, relative to project root, environment-scoped
subdirectory) - a future larger/external destination only requires
changing this one path, no code change.

### Retention configuration

`[HISTORIAN_MAINTENANCE] backup_retention_count` (unchanged, still 7)
- explicitly NOT treated as automatically safe for this VM; storage
requirements must be recalculated from current database sizes before
enabling (see `database/backup_status.get_backup_status()`'s
`estimated_retention_storage_bytes` field, which is always freshly
computed from today's actual database sizes, never cached).

### Disk-space safety gate

New `database/backup.check_backup_preflight()`, run before EACH
database's individual backup attempt. Conservative rule (documented in
its own docstring): `required_free_bytes = database_size (this
backup's own file) + database_size (working-capacity budget) +
minimum_free_reserve_bytes` - i.e. roughly 2x the source database's
current size plus a configured reserve, deliberately stricter than the
naive `free_space > database_size` check. On failure, the attempt is
skipped (never partial, never destructive) with
`reason=BACKUP_SKIPPED_INSUFFICIENT_DISK_SPACE`.

### Minimum reserve

`[HISTORIAN_MAINTENANCE] backup_minimum_free_reserve_gb = 5` (new key)
- `ConfigManager.historian_backup_minimum_free_reserve_bytes` converts
this to bytes.

### Existing backup inventory

`backups/simulation/` contains exactly 2 files from Phase 18.1a's
one-time live verification (before this follow-up disabled automatic
execution): `config_backup_20260819_155628.db` (17.8MB) and
`machine_data_backup_20260819_155628.db` (2.64GB), total ~2.66GB. No
new file was created during this follow-up. Separately, ~33 older
`*_before_*` migration-safety snapshot files (~807MB total) exist
across `database/`, `database/simulation/`, and `database/actual/` -
a different category (automatic pre-migration safety copies, not
scheduled backups), unpruned and untouched, per explicit instruction -
remains a future Data Management decision.

### Status/read-model support

`database/backup_status.get_backup_status()` - a pure, read-only
function returning capability/enabled state, destination, both
database sizes, backup directory size/count, free disk space,
configured retention, a freshly-recalculated storage estimate, the
configured reserve, and the last genuinely-recorded backup
attempt/summary (never a fabricated timestamp). No UI page was built
this follow-up - backend/read-helper only, as directed.

### Tests

34 new tests: 11 added to `tests/test_historian_maintenance_worker.py`
(7 for the preflight function, 4 for skip/no-destruction/JSON-summary
behavior in `run_backup_if_due()`), 16 in new
`tests/test_backup_status.py`, and 7 in new `tests/test_config_manager.py`
(including a direct integration check against the real project
`settings.ini` proving backup is genuinely disabled today, not just in
a fixture). All use small isolated SQLite fixtures (bytes to KB) - no
test creates or copies the real multi-GB historian.

### Live validation

Confirmed on this VM after restarting `historian_maintenance_worker.service`:
fresh startup log explicitly reads "Backup: disabled"; `backups/simulation/`
still contains only the same 2 pre-existing files (no new backup
created); `machine_data.db` continued growing normally under
`plc_logger.service` (still active) between before/after checks;
`get_backup_status()` against the real environment correctly reports
`automatic_backup_enabled: false` and an honest `last_backup_summary: null`
(the one pre-existing backup predates the summary-writing feature -
correctly not fabricated).

### Confirmation no new large live backup was created

Confirmed - `backups/simulation/`'s file listing and total directory
size are unchanged from before this follow-up began.

### Future activation requirements

Before setting `backup_enabled = true` again: (1) confirm the VM disk
has been expanded, or repoint `backup_destination_dir` at a
mounted filesystem with real headroom; (2) re-run
`get_backup_status()` against the then-current database sizes to
recompute `estimated_retention_storage_bytes` and confirm it fits
comfortably within free space plus the configured reserve; (3) restart
`historian_maintenance_worker.service` to pick up the change (config
changes in this project take effect on next restart, never live).

### PHASE STATUS: BACKUP CAPABILITY PREPARED, AUTOMATIC BACKUP DISABLED

---

## Phase 18 — AI User Experience

**Completed:** 2026-08-20

### Roadmap note (read this before Phase 18.1a/18.1a.1 above)

No "Master Development Roadmap" document exists anywhere in this
repository - re-confirmed during this phase (filesystem search, full-
text search for the specific requirement wording), same finding
already recorded at this document's own Phase 10 entry on 2026-08-15.
**Phase 18.1a and Phase 18.1a.1 (above) are an independent, off-
roadmap numbering branch - historian/backup and tariff-provenance
work, unrelated to AI User Experience.** They are left named exactly
as they already were; nothing about them was rewritten or
reinterpreted for this phase. "Phase 18 - AI User Experience" here
uses the detailed chat specification provided directly for this work
as its authoritative scope, matching this project's own established
convention (every real phase so far has been specified this way, per
the Phase 10 entry's own account) rather than a document that was
confirmed, twice now, not to exist.

### Objective

Make slow local AI inference feel acceptable in the Ask AI page,
without redesigning the accepted DETERMINISTIC SYSTEMS -> STRUCTURED
CONTEXT -> LLM INTERPRETATION -> GROUNDING GUARD -> ENGINEER
architecture, without a new AI pipeline, provider architecture, or
always-running service.

### Audit finding that shaped this phase's scope

Before writing any code, `ui/pages/1_Ask_AI.py` was audited directly
and found to already implement most of what a naive "Phase 18" might
have proposed building from scratch: session-scoped background-thread
execution (`threading.Thread`, daemon), a poll/rerun loop, an elapsed-
time counter, `st.chat_input(disabled=job_running)` duplicate-
submission prevention, full previous-answer visibility during a new
request, continuation across page navigation, a provider/model
caption, and a non-default-visible View Evidence expander - all
already correct, already live, already committed. This phase is
therefore a genuinely small refinement on top of that, not a rebuild -
confirmed against the file before writing anything.

### What was built

- **`ui/ask_ai_ux.py`** (new) - small, pure-Python, Streamlit-free
  helpers: `provider_status_label()`, `stage_message()`,
  `fallback_reason_prefix()`, and the verified `SUGGESTED_QUESTIONS`
  tuple. Directly unit-testable without Streamlit or AppTest, and kept
  entirely separate from `app/ask.py`'s deterministic layers.
- **`app/ask.py`** - added an optional `on_progress: Callable[[str],
  None] | None = None` parameter to `ask_structured()` and threaded it
  through `_answer_factory_summary()`, `_answer_comparison_interpretation()`,
  `_answer_equipment_interpretation()`, and `_render_interpretation_answer()`.
  Every existing caller that doesn't pass it is unaffected. Stages
  emitted only where genuinely true: `"preparing_context"` always
  first; `"generating_ai"` only immediately before the one place each
  path actually calls `ai_provider.generate()` (never emitted when a
  provider-None/Data-Confidence-UNAVAILABLE short-circuit skips
  generation entirely); `"validating"` only inside
  `_render_interpretation_answer()`, the only place
  `ai.grounding_guard.check_grounding()` is ever called - the older
  `ask()`/`IndustrialQueryEngine` pipeline (still the path for most
  real questions, including the slow root_cause ones) has no exposed
  grounding step, so it is treated as one honest "generating_ai" span
  rather than instrumented internally, and never emits "validating".
  No Streamlit import anywhere in this file.
- **`ui/pages/1_Ask_AI.py`** - rendering now calls
  `stage_message(job["stage"], provider_status_label(...))` instead of
  a single fixed "Still thinking" string; a `_submit()` helper is now
  the single entry point both `st.chat_input` and every suggested-
  question button call, so a suggestion is a normal Ask AI request
  through the identical pipeline (entity resolution, session context,
  deterministic routing, structured context, provider generation,
  grounding, fallback); `_run_job`'s exception handler now logs the
  real exception/traceback via `logging.getLogger(__name__).exception()`
  and shows only "Ask AI could not complete this request." to the
  user; a `_display_text()` helper prefixes `fallback_reason_prefix()`'s
  wording onto a fallback answer (provider-unavailable vs. grounding-
  rejected get distinguishable wording) for both the live render and
  what's stored into history; View Evidence gained resolved-equipment
  names, `data_limitations`, `provenance.context_built_at`, and a
  historical-context-used flag (all read from fields that already
  existed in `structured_context`, nothing invented) alongside the
  existing intent/provider/grounding/fallback caption, with the raw
  context JSON now inside its own nested "Technical detail" expander
  rather than sitting directly under View Evidence.

### Suggested questions - verified, not assumed

Every question in `ui.ask_ai_ux.SUGGESTED_QUESTIONS` was run against
the live `AskEngine` before being included; several natural phrasings
were tried and rejected because they actually misrouted (e.g. a bare
equipment code without a plant qualifier is genuinely ambiguous now
that P01 and P02 are both fully enabled). "Energy opportunity"-themed
phrasing was tried repeatedly, with and without a plant/area
qualifier, and never resolved cleanly - Ask AI's NLP resolver does not
yet reliably answer that class of question (Energy Opportunities is a
separate page/engine, not wired into the query engine) - excluded
rather than shown untested, per explicit instruction. Final set: "How
is AC01 doing?", "Why has P01 CHL01 power increased?", "Compare P01
CHL01 and P02 CHL01", "Show me the P01 CHL01 power trend", "How is
everything doing?".

### Deferred (explicitly, not silently dropped)

- **Caching/reuse of AI answers** - no trustworthy content-fingerprint
  mechanism exists yet in `structured_context`; building one safely is
  real scoped work, not a small addition. Engineering correctness over
  saving an inference call.
- **Cancellation** - the provider layer has no genuine cancel hook;
  killing the UI-side thread would not stop Ollama's own in-flight
  generation. A fake cancel button was explicitly rejected. Documented
  as a known limitation.
- **Multi-session Ollama concurrency management** - no evidence of a
  real contention problem; a global lock was not built to solve an
  unproven one.

### Tests

74 new tests, all passing:
- `tests/test_ask_ai_ux.py` (33) - pure-function coverage of
  `provider_status_label()`, `stage_message()` (including "never
  contains a percentage or ETA"), `fallback_reason_prefix()`, and
  `SUGGESTED_QUESTIONS`.
- `tests/test_phase18_ask_progress.py` (13) - `on_progress` is
  optional; genuine-stages-only across every path (old pipeline never
  emits "validating"; provider-None short-circuit never emits
  "generating_ai"; a working provider emits generating->validating in
  order; a provider exception emits "generating_ai" but never
  "validating"); provider-failure and grounding-rejection wording are
  distinguishable; a rejected AI claim never appears in the final
  answer; every suggested question resolves cleanly (not into a
  clarification menu); session-context override and ambiguity handling
  unchanged.
- `tests/test_phase18_ask_ai_page.py` (8) - `streamlit.testing.v1.AppTest`
  against the real page with `ai.ai_provider.AIProvider.generate`
  patched (no live Ollama dependency): page loads without exception;
  suggested-question buttons present; clicking a suggestion produces
  the identical history/evidence shape as typing a question; the
  `disabled=job_running` wiring is present (verified statically -
  AppTest's run-to-quiescence model cannot observe a genuinely still-
  running poll loop without either completing or timing out, a tooling
  limitation confirmed while developing this suite, not a product
  defect); an unguarded exception never leaks raw exception text into
  the chat; View Evidence renders for a domain-intent result; two
  independent `AppTest` sessions never share history.

Full regression after these changes: **1378 passed, 22 subtests
passed, 5 failed** - the same 5 pre-existing, unrelated legacy-path
failures as every baseline in this project's recent history (`+41`
tests over the prior 1337/22/5 baseline, zero new failures).

### Live acceptance

Ran directly against the real, live Ollama/`qwen2.5:7b` (not mocked) -
`"Why has P01 CHL01 power increased?"`, a root_cause-style question
via the old `ask()` pipeline:
- Stage sequence: `preparing_context` -> `generating_ai` (correctly no
  `validating` - this path never calls grounding).
- Total elapsed: 487.5s (~8.1 minutes) - consistent with this
  project's own documented 5-10 minute root-cause figure for this
  CPU-only hardware.
- `provider="ollama"`, `fallback_used=False`, real coherent answer
  referencing CHL01's actual current (48.42 kW) and average (51.63 kW)
  power.
- Provider-failure and grounding-rejection scenarios were verified via
  mocks (`tests/test_phase18_ask_progress.py`) rather than live
  requests, per the explicit instruction not to run excessive live
  Ollama calls when the rest can be validated deterministically.

**Not yet done:** `streamlit.service` has not been restarted, so the
deployed web app is still running the pre-Phase-18 code as of this
writing - this session has no sudo/TTY access (standing constraint,
see this document's Known Issues). The live acceptance above was run
directly against the edited files in-process, not through the running
service.

### Database / service

No schema change. No new worker, no new service, no `ai_worker.service`.
Ask AI remains fully read-only (no PLC/SCADA/work-order/maintenance
writes existed before this phase or exist after it).

### PHASE STATUS: PASS

---

## Phase V1.1 — Cleanup & Technical Debt Resolution

**Completed:** 2026-08-20

First phase of a small, fixed roadmap toward a V1 production
deployment (proposed the same day - see the review preceding this
entry in the conversation history; not separately reproduced here).
Deliberately cleanup-only - no engine, Ask AI, database schema, or
production behavior was touched.

### 1. Stray `factory_simulator.service` - investigated, source removed

Found running live (23h uptime, ~138MB RAM) and executing
`ai/simulator/factory_simulator.py` - a module this document's own
earlier audits already classified as confirmed dead code, but whose
*systemd service* had never been noticed or acted on before now.

Verified before touching anything:
- Zero Python-level importers anywhere in the live codebase (confirmed
  by a repo-wide grep, not assumed from the earlier audit alone).
- Writes to `database/machine_data.db` at the project ROOT - not
  `database/simulation/machine_data.db` or `database/actual/
  machine_data.db`, the only two paths `config/environment.py`'s
  `ENVIRONMENTS` mapping can ever resolve to. Confirmed via direct
  read of that mapping: the root-level path is structurally
  unreachable by the live application no matter which environment is
  active - this service's writes could never have leaked into real
  behavior, past or present.
- No `deploy/systemd/factory_simulator.service` file exists in the
  repo - the live unit was installed directly outside git, so there
  was nothing to remove from version control on that side.

**Removed from the repository:** `ai/simulator/factory_simulator.py`
(tracked, confirmed dead, `git rm`).

**NOT removed (outside this session's capability - no sudo/TTY, see
Known Issues):** the live systemd unit and process itself. **You need
to run this manually:**
```bash
sudo systemctl stop factory_simulator.service
sudo systemctl disable factory_simulator.service
sudo rm /etc/systemd/system/factory_simulator.service
sudo systemctl daemon-reload
```

**Flagged but not acted on:** the orphaned root-level
`database/machine_data.db` (~1.5GB and growing) and `database/
config.db` this service was writing into are now safe to delete once
the service is stopped, but deleting a multi-GB data file wasn't
explicitly authorized for this phase - left for a deliberate decision
later, matching this project's established caution around database-
file deletion. Two small untracked config files
(`config/simulator_command.txt`, `config/simulator_config.json`) that
existed only to feed this dead service are now fully orphaned too -
left in place (harmless, never committed, low priority).

### 2. `ai/ai_bridge.py` dead-code cluster - removed

Re-verified the disposition-audit findings from earlier in this
project against the CURRENT codebase (not assumed unchanged) via a
repo-wide import trace: for every module in `ai/`, checked who
imports it from `app/`, `ui/`, `engine/`, `database/`, `config/`,
`plc/`, and `simulator/` (i.e. genuinely live code, not another dead
module). Exactly 11 `ai/*.py` modules are live this way (`ai_provider`,
`context_builder`, `event_engine`, `event_store`, `grounding_guard`,
`interpretation_intent`, `interpretation_prompt_builder`,
`prompt_builder`, `root_cause_engine`, `rule_engine`, `trend_analyzer`),
plus `equipment_knowledge.py` (reached transitively through the live
`event_engine.py` - confirmed explicitly kept, NOT removed).

**Removed** (zero live importers, direct or transitive, confirmed
before deletion):
`ai/ai_bridge.py`, `ai/router.py`, `ai/query_dispatcher.py`,
`ai/threshold_reader.py`, `ai/timeline_query.py`,
`ai/question_understanding.py`, `ai/understanding/question_understanding.py`
(the whole now-empty `ai/understanding/` directory went with it - it
had no `__init__.py` and zero importers of its own),
`ai/observation_engine.py`, `ai/database_reader.py` - the last two
found only while tracing `ai_bridge.py`'s own imports, not part of the
original named list but genuinely part of the same isolated cluster.

**Also removed, discovered during verification, not originally
planned:** `ai/hybrid_router.py`. Deleting `ai/router.py` broke
`hybrid_router.py`'s own import of it (`ModuleNotFoundError` on test
collection) - caught by running `pytest --collect-only` before the
full suite, exactly the kind of thing that check is for. Verified
`hybrid_router.py` itself had zero live importers (only referenced by
its own test and a documentation comment in `ai/interpretation_intent.py`
- confirmed that reference is prose, not a code import) before removing
it too, rather than leaving a permanently-broken dead file behind.

**Test files removed alongside** (each exclusively tested a module
above; left in place they would either fail to collect or test nothing
real): `tests/test_query_dispatcher.py`, `tests/test_pipeline.py`,
`tests/test_router.py`, `tests/test_timeline_query.py`,
`tests/test_threshold_reader.py`, `tests/test_hybrid_router.py`.

**Explicitly kept, not touched:** `ai/equipment_knowledge.py` (live,
via `event_engine.py`); every other previously-identified dead-code
file NOT part of the ai_bridge cluster specifically (`candidate_
selection_parser.py`, `knowledge_aware_question_parser.py`,
`knowledge_engine.py`, `llm_question_parser.py`, `llm_route_adapter.py`,
`llm_semantic_router.py`, `project_context.py`, `retrieval_aware_
question_parser.py`, `semantic_router.py`, `semantic_tag_resolver.py`,
`developer_assistant.py`, `event_timeline.py`, `ai/simulator/
factory_simulator_backup.py` already removed earlier) - out of scope
for this specifically-named cleanup, left for a deliberate future
decision rather than swept up opportunistically.

### Verification

`python3 -c "import app.ask; import app.event_monitor; import
app.plc_logger; import ai.event_engine; import ai.equipment_knowledge"`
- all live entry points import cleanly after removal.
`pytest tests/ --collect-only` - clean, zero collection errors, 1363
tests (down from 1397+ before removal, reflecting the 6 deleted dead-
code-only test files).

### README.md

Rewritten from a stale "2.0.0-alpha.1... live PLC reading, historian
queries, LLM fallback, RAG will be added in later releases" description
(accurate for the project's very first milestone, actively misleading
about everything built since) to reflect the current architecture,
major features, project structure, and how to actually run the system
today - including a pointer to this document and `CLAUDE.md` for full
detail rather than duplicating it.

### PHASE STATUS: PASS

---

## Phase V1.2 — Backup, Restore & Historian Retention

**Completed:** 2026-08-21

### Objective

Make database storage safe for long-term factory use without letting
the active historian grow forever - keep recent raw data live, archive
older raw data automatically (verified before deletion), keep archived
data transparently queryable, enable and prove real backup/restore,
and add disk-space protection. Reused the existing backup/preflight/
retention infrastructure throughout rather than building a second
mechanism - no new database platform, per explicit direction.

### Storage/retention design

**New module: `database/historian_archive.py`.** One plain SQLite file
per fully-elapsed calendar month (`database/<env>/archive/plc_data_
YYYY-MM.db`, same `plc_data` schema as the live table), not one ever-
growing archive file and not a new database platform. A month is
"eligible" only once its own LAST day is older than the configured
retention cutoff (`now - retention_days`, default 90, unchanged
`config/settings.ini` key) - a month straddling the cutoff is left
alone until it's genuinely fully elapsed. A calendar month with zero
rows (e.g. a real logging gap) is silently skipped, never given a
wasted empty archive file.

**Safety invariant - archive, verify, THEN delete, never any other
order:** `archive_and_purge_eligible_months()` creates the month's
archive file, `verify_month_archive()` independently re-derives row
COUNT and MIN/MAX(time) from the LIVE table and compares them exactly
against the same query against the archive file, and `delete_month_
from_live()` - the only function in the module that deletes anything -
is only ever called after that verification passes. A failed or
skipped verification leaves the live month completely untouched and
retries next cycle; the orchestrator stops at the first failure/skip
rather than continuing to later months, favoring "retry safely later"
over "push forward into an unclear state."

**Disk-space protection (requirement 6):** archive creation reuses
`database.backup.check_backup_preflight()` unchanged (schema-agnostic;
originally built for the backup-readiness follow-up) - insufficient
free space skips the archive attempt entirely (no partial file, no
deletion) rather than risking filling the disk.

**Worker wiring:** `app/historian_maintenance_worker.py` gained
`run_historian_archive_if_due()`, called from `run_forever()` in place
of the old `run_retention_if_due()` (blind `DELETE ... WHERE time <
cutoff`, no archive). Both old function and `DatabaseManager.cleanup()`
are left completely unchanged and still individually tested - simply
no longer called in production, since running both together would
actively conflict (a blind delete could remove data the archive step
hadn't verified yet). New marker file `logs/historian_archive_last_
run.txt`, same restart-safe due-interval pattern as retention/backup
already used.

### How archived data remains queryable

`DatabaseManager.get_history()` and `get_history_range()` - the exact
two methods every existing caller already uses (`app/ask.py`,
`engine/baseline_engine.py`, `engine/energy_kpi_engine.py` in six
places, `ui/energy_dashboard_data.py`) - now transparently merge in
archived rows when the live table alone doesn't satisfy the requested
range and limit. Zero signature changes, zero caller-side changes -
every deterministic engine, Ask AI, and UI page that already called
these methods gained archive access automatically. Archived months are
always strictly older than whatever remains live (by construction), so
merging is a simple, cheap concatenation - no re-sort needed. The
common case (a query the live table alone can already satisfy) never
even checks whether an archive directory exists - confirmed by a
dedicated test (`test_query_within_retention_window_never_touches_
archive_dir`).

Requirement 4 ("long-range queries should use existing summaries where
practical") was already true before this phase and untouched by it -
the Energy Dashboard's own long-range chart already reads `energy_kpi_
daily_summary` (a pre-aggregated table), never raw `plc_data`, and
Equipment/Data Health history pages already read their own persisted
snapshot tables. This phase's archive-awareness exists for the raw-
sample callers (engines computing real deltas/accumulations, Ask AI
historical/comparison questions) that genuinely need actual readings,
not chart aggregates.

### Backup enabled and verified for real

Re-confirmed real headroom on this VM before flipping the flag: 32-35GB
free vs. ~11.5GB required for one full backup cycle at current database
sizes (config.db ~22MB, machine_data.db ~3GB). `config/settings.ini`'s
`backup_enabled` is now `true` (was `false` since the original backup-
readiness follow-up). Also structurally true now, not just today: this
phase's archiving keeps the live historian bounded to the retention
window going forward, rather than growing forever between backups -
the original reason backup was disabled.

**Ran a real backup cycle** (`run_backup_if_due()`, unmodified, against
the actual `database/simulation/{config,machine_data}.db`) - completed
in 17.1s, both databases `status: ok`:
```
config_backup_20260821_093713.db      (21.8 MB)
machine_data_backup_20260821_093713.db (3.08 GB)
```

### Real restore test (safe temporary location, never touched the live database)

1. Copied both fresh backup files to a temp scratch directory (outside
   the repo, outside `database/`).
2. `PRAGMA quick_check` on both restored files: `ok` (full page-by-page
   `integrity_check` on the 3GB file was tried first and takes several
   minutes - `quick_check` is the right-sized structural check for a
   file this size and is what's documented as the routine check below).
3. Row counts, restored vs. the live source: `equipment` 83=83, `tags`
   625=625, `users` 6=6, `thresholds` 374=374 (config.db - exact match).
   `plc_data`: restored 16,628,841 vs. live 16,633,784 at verification
   time - the ~5,000-row gap is the live system's own writes continuing
   in the minute between the backup and the check, not a data problem;
   an honest, expected gap for a point-in-time snapshot.
4. Spot-checked the restored file's first row byte-for-byte against the
   same row in the live source (exact match on time/tag/address/value).
5. Deleted the temp restore copies once verification passed - the real
   backup files themselves remain in `backups/simulation/`.

**Documented restore procedure** (for a genuine future restore, not
performed here beyond the safe verification above):
```bash
# 1. Stop the services that write to the database
sudo systemctl stop plc_logger.service event_monitor.service streamlit.service

# 2. Move the current (possibly damaged) database aside - never delete
#    it outright until the restored copy is confirmed working
mv database/simulation/machine_data.db database/simulation/machine_data.db.pre-restore
mv database/simulation/config.db database/simulation/config.db.pre-restore

# 3. Copy the desired backup into place
cp backups/simulation/machine_data_backup_<TIMESTAMP>.db database/simulation/machine_data.db
cp backups/simulation/config_backup_<TIMESTAMP>.db database/simulation/config.db

# 4. Sanity-check before trusting it
sqlite3 database/simulation/machine_data.db "PRAGMA quick_check;"
sqlite3 database/simulation/config.db "PRAGMA quick_check;"

# 5. Restart services
sudo systemctl start plc_logger.service event_monitor.service streamlit.service

# 6. Once confirmed healthy, remove the .pre-restore files
```
A restored database will be missing any writes that happened after
that backup's timestamp - expected for any point-in-time backup, not a
defect. Archived months (in `database/simulation/archive/`) are
separate files, untouched by a `machine_data.db` restore - back them up
independently if the archive directory itself is ever at risk.

### Review of the orphaned `database/machine_data.db` from Phase V1.1

Confirmed still exactly what Phase V1.1 found: **safe to remove, not
removed automatically here either, per instruction.** The stray
`factory_simulator.service` and its systemd unit are now fully gone
(you already ran the V1.1 cleanup commands) - the file is static (no
longer growing), still structurally unreachable by the live application
(`config/environment.py`'s `ENVIRONMENTS` mapping only ever resolves to
`database/simulation/` or `database/actual/`, never the bare root
path), and nothing reads or writes it. ~1.5GB (`database/machine_data.db`
+ `-wal`/`-shm`) plus the small stale `database/config.db` (~7.5MB) can
be reclaimed whenever you're ready:
```bash
rm database/machine_data.db database/machine_data.db-wal database/machine_data.db-shm database/config.db
```

### Tests

47 new/changed tests, all passing:
- `tests/test_historian_archive.py` (24 new) - month-boundary math,
  eligibility (including the legacy ISO-'T'-separator timestamp quirk,
  and a genuinely empty intermediate month), create/verify/delete in
  isolation, the full orchestration (never-deletes-before-verified,
  disk-space skip leaves data untouched, idempotent re-run, multiple
  months oldest-first), archived-history querying, and - the key proof
  - `DatabaseManager.get_history()`/`get_history_range()` transparently
  spanning archived and live data with no caller-side change.
- `tests/test_historian_maintenance_worker.py` (+5) - `run_historian_
  archive_if_due()`'s due-interval/marker behavior, disk-space safety,
  and that it genuinely archives-and-purges a real fully-elapsed month.
- All against small, isolated temporary databases with synthetic old
  data - the real live database has no month old enough to be eligible
  yet (data only goes back ~4 weeks; default retention is 90 days), so
  synthetic old data was the only way to safely exercise the full
  pipeline (matching this project's established no-destructive-testing-
  against-the-real-historian convention).

Full regression after these changes: see closeout report for the exact
current numbers.

### Files/schema changed

New: `database/historian_archive.py`, `tests/test_historian_archive.py`.
Changed: `database/database.py` (`get_history`/`get_history_range` -
archive-aware; `cleanup()`/`backup()` untouched), `app/historian_
maintenance_worker.py` (new `run_historian_archive_if_due()`; old
`run_retention_if_due()` kept, no longer called), `tests/test_
historian_maintenance_worker.py` (+5 tests), `config/settings.ini`
(`backup_enabled = true`). **No SQL schema change** - archive files
reuse the exact same `plc_data` table definition as the live database;
no new table was added to `config.db` or `machine_data.db` itself.

### Preserved, not touched

Deterministic engineering calculations, Ask AI's interpretation/
grounding pipeline, `machine_events`/anomalies/baseline/energy-KPI/
opportunity/savings-verification/health/performance/data-health tables
and their own persistence - none of this phase's changes reach any of
them. `plc_text_data` (STRING-tag current-value table) is unaffected -
it was never part of the growth problem this phase addresses (one row
per tag, overwritten each cycle, not a time series).

### PHASE STATUS: PASS

---

## Phase V1.3 — New-Factory Configuration & Security Readiness

**Completed:** 2026-08-21

### Objective

Review what a real deployment needs to confirm before treating this
system's numbers as production-trustworthy, close the two genuine
gaps found during that review (login-attempt protection, unmarked
synthetic documents), and produce a single checklist tying it all
together - without inventing real engineering values, and without
touching RBAC or any deterministic engine.

### 1. Area/System taxonomy - reviewed, not changed

Confirmed still exactly what the V1 roadmap review found: **all 16
areas and all 26 systems remain `inferred_from_simulation`** - zero
rows confirmed against a real factory layout yet. The existing
confirm-by-editing mechanism (Factory Configuration page,
🟢 confirmed / ⚪ inferred badges) already does everything needed here -
this is a site-data-entry task for whoever sets up a real factory, not
a missing feature. Documented as item 1 of the new setup checklist.

### 2. SIMULATION_TUNING review - reviewed, not changed

Enumerated every registry carrying a `SIMULATION_TUNING` provenance
tag: `engine/anomaly_targets.py`, `engine/health_targets.py`,
`engine/performance_targets.py`, `engine/maintenance_intelligence_
targets.py`, `engine/opportunity_targets.py`. Per explicit instruction,
**no real engineering values were invented or changed** - these are
reasonable engineering-judgment defaults for a dev-stage simulation,
already honestly labeled as such via the existing `USER_CONFIGURED`/
`ENGINEERING_RULE`/`MANUFACTURER_REFERENCE`/`STATISTICAL`/
`SIMULATION_TUNING` provenance vocabulary (unchanged). Documented as
item 3 of the new setup checklist, pointing at the exact registry
files rather than restating values that would go stale.

### 3. Energy tariff - reviewed, already sufficiently clear

`ui/pages/13_Factory_Configuration.py`'s tariff form already sets
`is_simulated=False` when a real tariff is entered, and
`SIMULATED_TARIFF_NOTICE` already renders a visible warning wherever a
still-simulated tariff is in effect. No code change needed here -
this requirement was already satisfied by existing work. Documented
as item 2 of the setup checklist for discoverability.

### 4. Synthetic equipment documents - now visibly marked (real gap, fixed)

Found a genuine gap: the 3 synthetic reference manuals (Eaton 9395
UPS, Donaldson Torit dust collector, IMA filling line) had NO visible
marking anywhere in the Documentation page - the existing "Source"
column only ever showed "Uploaded" vs "Reference manual", never real-
vs-synthetic. `ui/pages/8_Documentation.py` gained
`_is_synthetic_reference_manual()` - derived from the same "_REAL"
filename convention every one of the 25 genuinely-sourced manuals
already carries (no new database column, no schema change), verified
directly against all 28 real filenames in the live database. Synthetic
reference manuals now show a `⚠️ Synthetic placeholder` marker on both
the title and the Source column; the page's own caption explains the
convention. Site-uploaded documents (an engineer's own real photo/
file) are never mistakenly flagged, regardless of filename.

### 5. Login-attempt protection - new

`config/user_manager.py` gained two additive columns on `users`
(`failed_login_attempts`, `locked_until` - via the same idempotent
`ALTER TABLE ... IF NOT EXISTS`-style pattern every other migrator in
this project uses) and per-username lockout: 5 failed attempts locks
the account for 15 minutes (`MAX_FAILED_LOGIN_ATTEMPTS`/
`LOCKOUT_MINUTES`, both named constants). `authenticate()`'s return
contract (`dict | None`) is completely unchanged for any other caller;
a new `get_lockout_status()` read-only method lets `ui/auth.py`'s
login form show a distinct "temporarily locked" message instead of the
generic "incorrect username or password" one. A successful login or an
admin password reset both clear the lockout. Every lockout event is
written to the existing `audit_log` table - no new logging mechanism.

**Deliberately per-username only, not per-IP/global** - this is a
small internal-tool user base (an engineering department), not a
public-facing service; a global rate limiter was judged out of
proportion for V1 "sensible" protection. **RBAC (roles, permissions,
`ROLES` tuple) is completely untouched** - this is purely an
authentication-attempt counter alongside the existing password check.

### 6. New-factory setup checklist - new

`docs/NEW_FACTORY_SETUP_CHECKLIST.md` - the 7 items above (plus PLC
connection, user accounts, and backup destination) as a single,
concrete, page-by-page checklist for whoever brings up a real
deployment. No new code required to act on any item - every one is
done through an existing admin UI page. Referenced from `README.md`.

### Tests

35 new tests, all passing:
- `tests/test_user_manager_lockout.py` (12) - lockout does not trigger
  early, triggers exactly at the threshold, locks out even the correct
  password, records to the audit log, is cleared by an admin password
  reset, never touches role/active, and a static source check confirms
  `ui/auth.py` checks lockout status before attempting authentication
  (a live AppTest of the real login form was deliberately NOT used
  here - it would need 5 real failed logins against a real account in
  the live config.db to prove the message renders, permanently
  mutating that account's lockout counters; the isolated unit tests
  above already fully cover the actual behavior).
- `tests/test_documentation_synthetic_marking.py` (6, +6 subtests) -
  all 3 known-synthetic filenames flagged, all 3 spot-checked real
  filenames (including the one real manual NOT under `manuals/real/`)
  never flagged, site-uploaded documents never flagged regardless of
  filename, and a live `AppTest` confirms the real Documentation page
  loads cleanly and its caption mentions the synthetic-placeholder
  convention.

Full regression after these changes: see closeout report for the
exact current numbers.

### Files changed

New: `docs/NEW_FACTORY_SETUP_CHECKLIST.md`, `tests/test_user_manager_
lockout.py`, `tests/test_documentation_synthetic_marking.py`. Changed:
`config/user_manager.py` (additive schema + lockout logic), `ui/auth.py`
(lockout-aware login message), `ui/pages/8_Documentation.py` (synthetic
marking), `README.md` (checklist pointer). **No changes** to any
deterministic engine, Ask AI, database schema beyond the two additive
`users` columns, or RBAC.

### PHASE STATUS: PASS

---

## Phase V1.4 — Real PLC Connection & Cutover

**Completed:** 2026-08-21

### Objective

Prove SmartFactoryAI can operate correctly with real PLC data, without
redesigning any driver that already works, and stop to ask for real
PLC connection details rather than guessing them.

### 1. Architecture review - confirmed already sound

Read `plc/driver_factory.py`, `app/plc_logger.py`,
`config/plc_connection_manager.py`, `plc/tag_registry.py`, all four
protocol drivers (`plc/modbus_driver.py`, `plc/opcua_driver.py`,
`plc/s7_driver.py`, `plc/fins_driver.py`), `plc/simulator_driver.py`,
`plc/base_driver.py`, `ui/pages/10_PLC_Connectivity.py`, and the Data
Health staleness engine. Findings:

- **Environment separation already structurally sound.** Simulation
  and Actual are different SQLite files with their own independent
  `plc_connections` tables; the PLC Connectivity page's Connections
  section only renders at all when the Actual environment is selected
  (`ui/pages/10_PLC_Connectivity.py:340`), so a real protocol can never
  be activated while still pointed at the Simulation database. Switching
  environments requires a process restart (`config/environment.py`'s
  own docstring), so no running process can silently flip which
  database it writes to.
- **Driver failure signaling already safe.** Every real driver's
  `read_all()` catches per-tag exceptions and omits that tag rather
  than crashing the poll loop; a fully-down connection surfaces via
  `plc_logger.py`'s outer exception handling either way, and
  `RECONNECT_INTERVAL_SECONDS = 30` forces a reconnect attempt every
  30s regardless of failure signal (already documented in
  `app/plc_logger.py`'s own comments, added after a real OPC UA
  idle-timeout incident).
- **Staleness is already honestly surfaced.** Data Health marks a
  fixed-interval tag "Stale" once its logging interval is exceeded by
  5x (or 300s with no configured interval) - it never silently keeps
  showing an old value as current (`engine/data_health_engine.py:344-362`).

None of this needed redesigning, per the explicit instruction.

### 2. Real gap found and fixed: no environment indicator outside PLC Connectivity

Every page except PLC Connectivity, Equipment & Tag Configuration,
Energy Dashboard, and Production Context gave an engineer **zero
visual cue** whether they were looking at Simulation or Actual data -
Live Data, Ask AI, Event Records, Anomalies, etc. all rendered
identically regardless of environment. Once real PLC data starts
flowing, that is exactly the kind of silent-mixing risk the phase
objective warns against - not a database-level mixing risk (already
closed, see above), but a human one.

Fixed by extending `ui/auth.py`'s `render_sidebar_identity()` - already
the one function proven to render on every page (called once from
`Home.py`, which every page navigation re-executes) - with a persistent
sidebar badge: a quiet green "Simulation environment (demo/test data)"
caption, or a prominent red "🔴 ACTUAL environment - Real PLC data."
`st.error()` block. No new mechanism, no page-by-page changes.

### 3. End-to-end pipeline validated live

No real PLC hardware is available this session. Used the existing
local OPC UA test simulator (`/home/test/opcua-web-simulator`,
referenced in `CLAUDE.md`) as the closest available stand-in, exactly
as intended for this kind of test:

- Started the simulator's OPC UA server on port 4840 (it was not
  running at the start of this session).
- Temporarily set `config/active_environment.txt` to `actual` (a plain
  marker file only read at process start - confirmed via
  `config/environment.py`'s own docstring that this cannot affect the
  already-running Simulation-environment `plc_logger`/`streamlit`
  systemd services, which never restarted).
- Ran `python -m app.plc_logger` manually in the foreground for 25
  bounded seconds against the Actual environment's already-configured
  connection (an OPC UA connection named "test opc", pointing at that
  same local test simulator, left active from an earlier development
  session) and its 3 already-mapped test tags.
- Confirmed fresh rows landed in `database/actual/machine_data.db`'s
  `plc_data` table with live timestamps matching the test window -
  proving the full **driver_factory → OPC UA driver → tag_registry →
  historian** path works correctly against a real (test) server using
  entirely unmodified code.
- Restored `config/active_environment.txt` to `simulation` immediately
  afterward, and stopped the standalone OPC UA server process (left the
  simulator's own control UI on port 8502 running, its normal/intended
  state).

**This validates the mechanism, not genuine plant hardware** - the UI/
deterministic-engine/Ask AI legs of the path were not separately
re-validated against this test data, since they operate identically
regardless of source (already proven throughout every other phase
against the Simulation environment on the same schema/code path).

### 4. Existing stale test data found in the Actual environment - flagged, not touched

`database/actual/` was not empty, contradicting `CLAUDE.md`'s "genuinely
empty" description (now stale - flagged for correction). It already
contained ~10,100 historian rows and 3 placeholder tags (`tes55`,
`tes56`, `tes57`) from an earlier development test session against the
same OPC UA test simulator on 2026-08-12, plus 2 old `config.db` backup
files. This phase's own pipeline validation (item 3 above) added a
further ~15 rows to the same table. None of this is real plant data,
but none of it was deleted or altered beyond the validation reads/writes
already described - clearing or keeping it is a decision for whoever
runs the real cutover, documented as step 2 of the new procedure below.

### 5. New: safe real-PLC-connection procedure

`docs/REAL_PLC_CUTOVER_PROCEDURE.md` - a 10-step, safety-first
procedure for connecting the first real PLC/equipment: what connection
details to gather first, confirming the Actual environment, dealing
with the existing stale test data, testing the connection before
activating it, mapping one tag first, and verifying loss/recovery
behaviour honestly before expanding further. Referenced from `README.md`.

### Tests

3 new tests, all passing (`tests/test_sidebar_environment_badge.py`) -
the environment badge shows the quiet caption in Simulation, the
prominent error in Actual, and renders nothing at all when logged out.
Full regression: **1413 passed, 3 failed** (the same pre-existing,
unrelated `test_equipment_knowledge.py` typo-correction failures
carried since before this phase - not touched, out of scope).

### Files changed

New: `docs/REAL_PLC_CUTOVER_PROCEDURE.md`,
`tests/test_sidebar_environment_badge.py`. Changed: `ui/auth.py`
(sidebar environment badge), `README.md` (procedure doc pointer).
**No changes** to any PLC driver, `driver_factory.py`,
`plc_connection_manager.py`, `tag_registry.py`, historian schema, any
deterministic engine, or Ask AI.

### What still requires real, human-provided information

Per the explicit instruction to stop rather than guess:

1. **Protocol** - which of Modbus TCP / OPC UA / Siemens S7 / Omron
   FINS the real PLC speaks.
2. **Network reachability** - host/IP and port reachable from this VM
   (same subnet/VLAN or routed, firewall allowing the protocol's
   port) - an IT/network action, not something determinable from here.
3. **Protocol-specific parameters** - Modbus unit/slave ID; OPC UA
   endpoint URL plus whether it needs a username/password or trusted
   client certificate; S7 rack/slot; FINS PLC node/PC node.
4. **At least one real tag address** on that PLC, to map to a single
   SmartFactoryAI tag for the first end-to-end test.
5. **Which single piece of equipment to onboard first** - the
   procedure above deliberately starts with one tag, not the whole
   plant.
6. **A decision on the existing stale test data** in
   `database/actual/` (item 4 above) - clear it first, or proceed
   knowing it's there.

None of this can be guessed safely; all of it is either physical/
network information only the site can provide, or a scope decision
only the user can make.

### PHASE STATUS: PASS (mechanism validated via test simulator; genuine
plant hardware validation blocked on the real PLC information above)

---
