```text
Project audit date:     2026-08-15
Project root:           /home/test/SmartMachineAI
Application status:     Development (no live PLC connected; simulator-driven)
Database:               SQLite (two separate files: config.db + machine_data.db, per environment)
Simulation status:      Active (systemd-managed, generating data continuously)
AI status:              Active (local Ollama, qwen2.5:7b) - LLM used for phrasing only, never for facts
```

This report was produced by direct inspection of the repository (file tree, source code, live SQLite schemas and row counts) on the date above. No code, database, or configuration was modified to produce it.

---

# 1. PROJECT STRUCTURE

```text
SmartMachineAI/
├── app/                    Entry-point scripts (the actual running processes)
│   ├── ask.py                 Ask AI orchestrator - the core Q&A pipeline
│   ├── plc_logger.py           Reads PLC/simulator tags -> writes plc_data historian
│   ├── event_monitor.py        Reads plc_data -> evaluates thresholds -> writes machine_events
│   ├── import_manual.py        CLI to ingest a manufacturer PDF into the RAG store
│   ├── query_cli_v2.py         CLI wrapper around the deterministic query engine
│   ├── machine_query.py        DEAD - earliest prototype, not imported anywhere
│   └── main.py                 Not part of the live app (see note below)
│
├── ai/                      AI/NLP logic - a mix of live and dead code (see Section 14)
│   ├── ask.py-adjacent live modules: ai_provider.py, prompt_builder.py, rule_engine.py,
│   │   root_cause_engine.py, trend_analyzer.py, event_engine.py, event_store.py
│   ├── providers/              Provider abstraction: ollama_provider.py, openai_provider.py,
│   │                           provider_factory.py, base_provider.py
│   └── (17 other files)        DEAD CODE - earlier prototype generations, zero live imports
│                               (router.py, hybrid_router.py, semantic_router.py,
│                               equipment_knowledge.py, question_understanding.py,
│                               knowledge_engine.py, query_dispatcher.py, timeline_query.py,
│                               threshold_reader.py, event_timeline.py, and others -
│                               see Section 22 "Confirmed dead code" for the full list)
│
├── engine/                  Deterministic NLP + data-model migration scripts
│   ├── concept_extractor.py    Vocabulary/intent extraction (no LLM) - the real NLP engine
│   ├── industrial_query_engine.py  Question -> tag/equipment resolver (no LLM)
│   ├── models.py                Concepts/QueryResult dataclasses
│   ├── tag_dataset_importer.py  Imports the 625-tag master list into config.db
│   ├── metadata_migrator.py     Fills tags.measurement/location/signal_type/etc.
│   ├── equipment_metadata_migrator.py  Adds brand/model/device_number columns to equipment
│   ├── seed_equipment_metadata.py      Seeds illustrative brand/model data
│   ├── seed_engineering_thresholds.py  Seeds low/high warning/alarm limits
│   ├── maintenance_migrator.py  Adds maintenance_log table + equipment.next_due_at
│   ├── service_migrator.py      Adds service_log table
│   ├── seed_users.py            Seeds demo user accounts
│   └── backfill_document_page_numbers.py  Re-chunks PDFs with page-number tracking
│
├── database/                 Database layer
│   ├── database.py              DatabaseManager - all plc_data/machine_events access
│   ├── initialize_config_db.py  Base schema creation for config.db
│   ├── initialize_actual_environment.py  Builds an empty "actual" environment
│   ├── simulation/              config.db + machine_data.db (the live dev database)
│   └── actual/                  config.db + machine_data.db (empty, for a real PLC deployment)
│
├── simulator/                 Fake sensor data generation
│   ├── tag_dataset_model.py     Live simulator - drives all 625 imported tags
│   └── factory_model.py         Legacy ~20-tag hand-built demo model (superseded, still
│                                imported by dead ai/simulator/factory_simulator.py only)
│
├── plc/                       Real PLC driver layer (not currently connected to real hardware)
│   ├── driver_factory.py        Picks the active driver (simulator/Modbus/OPC UA/S7/FINS)
│   ├── simulator_driver.py      Wraps simulator/tag_dataset_model.py as a "driver"
│   ├── modbus_driver.py, opcua_driver.py, s7_driver.py, fins_driver.py  Real protocol drivers
│   └── tag_registry.py          Loads tags/tag_addresses for whichever driver is active
│
├── rag/                        Manufacturer-documentation retrieval
│   ├── document_store.py        Schema + storage for equipment_documents/document_chunks_fts
│   └── retrieval.py              FTS5 keyword search (no embeddings/vector DB)
│
├── config/                     Configuration, environment, connection, and user management
│   ├── environment.py            simulation/actual environment switch
│   ├── configuration_manager.py  Threshold/audit-log read-write layer used by the UI
│   ├── plc_connection_manager.py PLC connection CRUD (used by PLC Connectivity page)
│   ├── user_manager.py           User accounts, PBKDF2-HMAC-SHA256 password hashing
│   ├── config_manager.py, configuration_importer.py, configuration_service.py,
│   │   equipment_knowledge_importer.py   Older config-loading utilities, partially superseded
│   └── settings.ini              Legacy driver/AI-provider config (fallback only)
│
├── ui/                          Streamlit web application
│   ├── Home.py                   Entry point - auth gate + sidebar navigation
│   ├── auth.py                   Login/session/role logic
│   ├── data_access.py            Shared SQLite read/write helpers for the UI
│   ├── scada_floor_plan_data.py  Data/logic for the SCADA Floor Plan page
│   ├── scada_snapshot_writer.py  Background thread writing live SCADA JSON snapshot
│   └── pages/                    11 pages - see Section 3
│
├── manuals/                     Equipment manuals/datasheets (gitignored, 96MB, 51 PDFs)
├── docs/superpowers/             Design specs and implementation plans from prior sessions
├── tests/                        14 test files, 110 tests total (95 real, 15 exercise dead code)
└── .streamlit/config.toml        Streamlit server config (static file serving, session TTL)
```

**Note on `app/main.py`:** exists in the tree but is not the live entry point - the actual running processes are `app/plc_logger.py`, `app/event_monitor.py`, and `streamlit run ui/Home.py`, each its own systemd service. `app/main.py` was not found to be referenced by any of them.

---

# 2. CURRENT APPLICATION ARCHITECTURE

- **Frontend:** Streamlit (Python-only, server-rendered, no separate JS framework) - `ui/Home.py` + 11 pages under `ui/pages/`. One page (`12_SCADA_Floor_Plan.py`) additionally embeds a hand-written vanilla-JS component via `st.components.v1.html()` for a flicker-free live view; everything else is plain Streamlit widgets.
- **Backend:** Plain Python, no web framework (no Flask/FastAPI/Django) - Streamlit *is* the web server. Business logic lives in `app/`, `ai/`, `engine/`, `config/`, `database/`, `plc/`, `rag/` as importable modules, not as HTTP API endpoints.
- **Database:** SQLite, split into two files per environment: `config.db` (equipment, tags, thresholds, users, documents, audit log - low write volume) and `machine_data.db` (`plc_data`/`plc_text_data`/`machine_events` - high write volume, currently 6.87M rows in `plc_data`). No PostgreSQL/MySQL/cloud DB anywhere.
- **AI/LLM:** Local Ollama (`qwen2.5:7b` currently configured) via a provider abstraction that also supports OpenAI as a swap-in alternative (`ai/providers/provider_factory.py`). No other LLM backends. The LLM is used **only to phrase already-verified facts** - it never has direct database access and is not asked to compute anything itself except for two narrow, non-factual fallback tasks: fixing typos/rewriting an unresolved question, and phrasing a clarifying question when nothing resolves.
- **Simulation engine:** `simulator/tag_dataset_model.py`, driven by `app/plc_logger.py` (see Section 8).
- **Authentication:** Custom, in-house - `ui/auth.py` + `config/user_manager.py`. Session state via Streamlit's `st.session_state`, PBKDF2-HMAC-SHA256 password hashing with per-user random salt, no external identity provider, no MFA.
- **API architecture:** None - there is no REST/GraphQL API. All interaction is either through the Streamlit UI or direct Python function calls (`AskEngine`, `IndustrialQueryEngine`, etc.) from CLI scripts.
- **Background services:** three systemd units - `plc_logger.service`, `event_monitor.service`, `streamlit.service` - each a long-running Python process, independent of user sessions.
- **External libraries/services:** Ollama (local, self-hosted), optional OpenAI API (unused by default), `pypdf` (PDF text extraction), `pymodbus`/`asyncua`/`python-snap7` (real PLC protocol libraries, not currently connected to real hardware), Streamlit itself. No cloud services, no message queue, no cache layer (Redis etc.), no task scheduler beyond systemd + in-process polling loops.
- **Component communication:** Everything communicates through SQLite files on local disk and direct Python imports - there is no message bus or HTTP call between `plc_logger`, `event_monitor`, and the Streamlit UI. They are decoupled only in the sense that each independently reads/writes the same SQLite files.

```text
Browser
   |
Streamlit UI (ui/Home.py + ui/pages/*)
   |
   +-- ui/data_access.py --------------> config.db (SQLite)  <-- app/plc_logger.py writes plc_data
   |                                     machine_data.db      <-- app/event_monitor.py writes machine_events
   |
   +-- app/ask.py (AskEngine) ---> engine/industrial_query_engine.py (deterministic resolver)
   |         |                          |
   |         |                          +-- config.db (tags/equipment/thresholds)
   |         |
   |         +--> ai/rule_engine.py, ai/root_cause_engine.py, ai/trend_analyzer.py
   |         |         (deterministic fact computation, no LLM)
   |         |
   |         +--> rag/retrieval.py --> config.db (document_chunks_fts, FTS5 keyword search)
   |         |
   |         +--> ai/prompt_builder.py --> ai/ai_provider.py --> Ollama (localhost:11434)
   |                                                              (phrasing only)
   |
   +-- ui/pages/12_SCADA_Floor_Plan.py --> ui/scada_snapshot_writer.py (background thread)
                                            --> manuals/_scada_live.json --> served via
                                                Streamlit's static file route, polled by
                                                embedded JS every 2s (bypasses Streamlit rerun)

Separate, independent processes (systemd), all reading/writing the same SQLite files:
  plc_logger.service   -> simulator/tag_dataset_model.py (or a real plc/*_driver.py) -> plc_data
  event_monitor.service -> plc_data -> ai/rule_engine.py -> ai/event_engine.py -> machine_events
```

---

# 3. CURRENT WEB PAGES

| Page / Route | Purpose | Main functions | Data source | Status |
|---|---|---|---|---|
| Home (`ui/Home.py`) | Landing page, role-aware nav description | Static text, links out to other pages | None (static) | Complete |
| Ask AI (`1_Ask_AI.py`) | Natural-language Q&A | Question input, background-threaded answer polling, conversation history | `AskEngine` (config.db + machine_data.db + Ollama) | Complete |
| Live Data (`2_Live_Data.py`) | Current value of every enabled tag | Grouped by equipment, severity-colored, auto-refresh (5s, `st.fragment`) | `plc_data`/`plc_text_data` (latest) | Complete |
| Setpoints (`3_Setpoints.py`) | View/edit alarm & warning limits | Two-step confirm-before-write, audit log expander | `thresholds` table | Complete |
| Service and Maintenance (`4_Service_and_Maintenance.py`) | Maintenance scheduling + one-off service visits | Upcoming/overdue table, log-work form, service history | `maintenance_log`, `service_log`, `equipment.next_due_at` | Complete |
| Event Records (`5_Event_Records.py`) | Alarm/warning history browser | Filter by severity/equipment/tag/time range | `machine_events` | Complete |
| Simulator (Testing Only) (`6_Simulator_(testing_only).py`) | Manually trigger/recover a simulated fault | Instance picker, force-fault/force-recover buttons | `simulator_fault_commands` (cross-process command table) | Complete, dev-only (hidden outside "simulation" environment) |
| Documentation (`8_Documentation.py`) | Manufacturer manual/photo library | Upload (PDF/image), view/download/delete, per-equipment attachment counts | `equipment_documents` + filesystem (`manuals/`) | Complete |
| User Management (`9_User_Management.py`) | Account administration | Admin-only; create/edit users, roles | `users` table | Complete |
| PLC Connectivity (`10_PLC_Connectivity.py`) | Configure real PLC connections | Connection CRUD, OPC UA node browser, tag-address mapping, auto-reconnect status | `plc_connections`, `tag_addresses` | Complete |
| Equipment and Tag Configuration (`11_Equipment_and_Tag_Configuration.py`) | Equipment/tag CRUD | Admin-only; add/edit equipment and tags | `equipment`, `tags` | Complete |
| SCADA Floor Plan (`12_SCADA_Floor_Plan.py`) | Live plant-wide visual overview | Room-grouped equipment status grid, per-equipment detail panel, per-plant info board (electrical/water/utilities/plant health), flicker-free 2s polling | `ui.scada_snapshot_writer` JSON snapshot (built from `plc_data`/`config.db`) | Complete |

**Not present:** no dedicated "Dashboard", "Equipment" (list/detail beyond the admin CRUD page), "Energy", or "Reports" page exists as such. The closest equivalents are Live Data (raw tag values) and the SCADA Floor Plan's per-plant info board (electrical/water summary figures only, not a full energy dashboard).

---

# 4. CURRENT DASHBOARD

There is no single page called "Dashboard." The two pages that come closest are **Live Data** and the **SCADA Floor Plan**'s info board.

**Live Data page:**
| Item | Data type |
|---|---|
| Per-tag current value table, grouped by equipment | Real (from `plc_data`/`plc_text_data`, simulator-sourced) |
| Severity color-coding (alarm/warning/normal) | Calculated (`RuleEngine.evaluate_summary()` against `thresholds`) |
| Equipment/status filter dropdowns | Real (drawn from `config.db`) |

**SCADA Floor Plan info board (per plant):**
| Item | Data type |
|---|---|
| Today's electrical consumption (kWh), power draw, current, voltage, PF/freq | Real (from `plc_data`, delta-of-monotonic-counter calculated for "today's consumption") |
| Today's water consumption (m³), current flow | Real (same calculation pattern as electrical) |
| Compressed air header pressure/flow, water supply header pressure/tank level | Real |
| Plant Health (normal/alarm/warning/no-threshold/no-data counts) | Calculated (aggregated from per-tag `RuleEngine` results) |
| Room-grouped equipment status grid | Real + Calculated (live severity per equipment) |

**Not present on any page:** KPI cards in the conventional sense (single big numbers with trend arrows), charts/graphs of any kind (no line/bar charts anywhere in the UI - Live Data and Event Records are plain tables), production information, AI-generated summaries embedded in a dashboard, or any static/hard-coded display data.

---

# 5. DATABASE

Two SQLite files per environment (`simulation` and `actual`), selected by `config/environment.py`. Row counts below are from the `simulation` environment (the one currently active and populated).

## `config.db` (low-frequency, configuration + reference data)

| Table | Purpose | Important columns | Relationships | Rows | Written by | Read by |
|---|---|---|---|---|---|---|
| `equipment` | Equipment master list | `name`, `display_name`, `brand`, `model`, `device_number`, `service_interval_days`, `last_serviced_at`, `next_due_at` | `tags.equipment_id`, `equipment_documents.equipment_id`, `maintenance_log.equipment_id`, `service_log.equipment_id` | 83 | Equipment & Tag Config page, migrators | Almost every page/module |
| `tags` | Tag/point master list | `tag_name`, `equipment_id`, `data_type`, `unit`, `measurement`, `location`, `signal_type`, `event_type`, `threshold_type`, `logging_interval_seconds` | `equipment.id`, `tag_addresses.tag_id`, `thresholds.tag_name` | 625 | Tag importer, Equipment & Tag Config page | `plc_logger`, `event_monitor`, `AskEngine`, all UI pages |
| `tag_addresses` | Per-driver PLC address for each tag | `tag_id`, `driver`, `address`, `enabled` | `tags.id` | 625 | Tag importer, PLC Connectivity page | `plc/tag_registry.py` |
| `thresholds` | Alarm/warning limits | `tag_name`, `low_warning`, `low_alarm`, `high_warning`, `high_alarm` | `tags.tag_name` (not a formal FK) | 374 | Setpoints page, threshold seeder | `RuleEngine` |
| `equipment_aliases` | Alternate names for fuzzy NLP matching | `equipment_id`, `alias` | `equipment.id` | 259 | Metadata migrator | `IndustrialQueryEngine` |
| `equipment_documents` | Manual/manufacturer-doc metadata | `brand`, `model`, `title`, `file_path`, `equipment_id` (nullable - shared docs) | `equipment.id` (nullable) | 30 | Documentation page, import CLI | `rag/retrieval.py`, Documentation page |
| `document_chunks_fts` (+ 4 auxiliary FTS5 tables) | Full-text search index over document chunks | `document_id`, `chunk_index`, `page_number`, `chunk_text` | `equipment_documents.id` | 5,941 chunks | Document ingestion, backfill script | `rag/retrieval.search_chunks()` |
| `maintenance_log` | Preventive/corrective maintenance history | `equipment_id`, `category`, `description`, `parts_replaced`, `performed_by`, `performed_at`, `next_due_at` | `equipment.id` | 6 | Service & Maintenance page | Same page |
| `service_log` | One-off service visit history | `equipment_id`, `person_in_charge`, `description`, `performed_at` | `equipment.id` | 1 | Service & Maintenance page | Same page |
| `users` | Login accounts | `username`, `password_hash`, `password_salt`, `role`, `active` | none | 6 | User Management page, seed script | `ui/auth.py` |
| `plc_connections` | Real PLC connection configs | `protocol`, `host`, `port`, `username`, `password` (plaintext - see Section 25), `is_active` | none | 1 | PLC Connectivity page | `plc/driver_factory.py` |
| `audit_log` | Change history for thresholds/documents/etc. | `username`, `action`, `entity_type`, `entity_name`, `old_value`, `new_value` | none (soft references) | 1,818 | `ConfigurationManager.write_audit_log()` | Setpoints/Documentation/PLC pages |
| `simulator_fault_commands` | Cross-process command queue for the dev fault-trigger tool | `instance_key`, `action`, `processed_at`, `result` | none | 7 | Simulator (Testing Only) page | `TagDatasetSimulator` (in `plc_logger`) |
| `equipment_tags`, `ai_settings`, `communication` | Legacy/placeholder tables | - | - | 0 (all empty) | Never written | Never read (confirmed no live code references) |

## `machine_data.db` (high-frequency historian)

| Table | Purpose | Important columns | Rows | Written by | Read by |
|---|---|---|---|---|---|
| `plc_data` | Historian - every logged tag reading (REAL values only) | `time`, `tag`, `address`, `value` | 6,865,462 | `plc_logger.service` | `AskEngine`, Live Data, SCADA snapshot writer, trend analysis |
| `plc_text_data` | Latest STRING-typed tag readings (e.g. AlarmCode) | `tag`, `value`, `time` | 50 | `plc_logger.service` | Same consumers, for STRING tags only |
| `machine_events` | Alarm/warning event log | `event_time`, `equipment`, `tag`, `severity`, `condition`, `value`, `message` | 36,196 | `event_monitor.service` | Event Records page, `RootCauseEngine`, factory-wide timeline answers |

**Notable data-hygiene finding:** 7 of the 83 `equipment` rows (`chiller`, `cold_room`, `tank`, `pump`, `energy`, `water`, `air` - no plant prefix, unlike the other 76) have **zero associated tags**. These appear to be orphaned leftovers from an earlier equipment structure (predating the P01/P02 tag-dataset migration) that were never cleaned up when their tags were removed/reassigned. They still carry brand/model metadata and are linked to some `equipment_documents` rows, so deleting them is not risk-free without checking document impact first - flagged here, not acted on.

---

# 6. EQUIPMENT MODEL

| Field | Status |
|---|---|
| Equipment ID | Already implemented (`equipment.id`, auto-increment) |
| Plant | Partially implemented - not a column; encoded as a `p01_`/`p02_` prefix in `equipment.name` |
| Building | Not implemented |
| Area | Partially implemented - encoded in tag naming convention (`P01.UTILITY.*`, `P01.COLDROOM.*`) and the SCADA Floor Plan's room-grouping logic, but not a queryable database column |
| System | Not implemented as a distinct concept from Area |
| Equipment Type | Partially implemented - derivable from `equipment.name`'s middle segment (e.g. `air_compressor`), not a stored column |
| Manufacturer | Already implemented (`equipment.brand`) |
| Model | Already implemented (`equipment.model`) |
| Serial Number | Not implemented |
| Rated Power | Not implemented |
| Rated Voltage | Not implemented |
| Rated Current | Not implemented |
| Rated Flow | Not implemented |
| Rated Pressure | Not implemented |
| Capacity | Not implemented |
| Installation Date | Not implemented |
| Criticality | Not implemented |
| Maintenance Interval | Already implemented (`equipment.service_interval_days`) |
| Last Maintenance | Already implemented (`equipment.last_serviced_at`) |
| Next Maintenance | Already implemented (`equipment.next_due_at`) |
| Equipment Status | Partially implemented - only as a *calculated, live* value (worst severity across its tags), not a stored status field |

---

# 7. PLANT / BUILDING STRUCTURE

| Concept | Status |
|---|---|
| Factory | Implicit (the whole system = one factory); no explicit "factory" entity/table |
| Plant | Implemented via naming convention only - `p01_`/`p02_` prefix on `equipment.name` and `P01.`/`P02.` prefix on `tags.tag_name`. No `plants` table. |
| Building | Not implemented |
| Area | Implemented via naming convention (tag segment, e.g. `UTILITY`, `COLDROOM`, `PROD`) and a hardcoded room-layout dict in `ui/scada_floor_plan_data.py` (`ZONES`) - not a database concept |
| System | Not implemented as distinct from Area |
| Equipment | Implemented (`equipment` table) |
| Tag | Implemented (`tags` table) |

**Plant 1 and Plant 2 confirmed to exist:** yes - 38 equipment instances and 313 tags each (625 tags total, 76 equipment instances across both plants, plus the 7 orphaned legacy rows noted in Section 5).

**Adding Plant 3+ later:** the tag-import/simulation/query layers were explicitly built to scale this way - `engine/tag_dataset_importer.py` takes a `--enable-plant` filter, the NLP query engine's plant-qualifier disambiguation (`extract_plant()`/`_break_plant_ties()`) is pattern-based (`p\d+`/`plant \d+`), not hardcoded to exactly two, and the simulator's tag profiles are keyed by a plant-stripped canonical key so they apply uniformly to any plant. **However**, several UI/reporting spots ARE currently hardcoded to exactly "p01"/"p02": the SCADA Floor Plan's plant toggle (two buttons, `p01`/`p02` literal), the factory-wide power/water-consumption fallback answers in `app/ask.py` (`FACTORY_TOTAL_POWER_TAG`/`FACTORY_WATER_METER_TAG`, both hardcoded to `P01.*`), and the SCADA info board (renders exactly two boards). Adding a Plant 3 would need new tag master-list entries plus targeted updates to those specific hardcoded spots - not a full architectural rework, but not fully "just works" either.

---

# 8. CURRENT SIMULATION ENGINE

**Main files:** `simulator/tag_dataset_model.py` (`TagDatasetSimulator`, live) and `simulator/factory_model.py` (`FactorySimulator`, legacy ~20-tag hand-built model, no longer wired into `plc_logger` but still importable and still used by dead code in `ai/simulator/`).

**Generation frequency:** `app/plc_logger.py` polls the active driver every `scan_interval` seconds (`config/settings.ini`, currently 2s), then writes to the historian per-tag according to each tag's own configured logging cadence (10s / 1min / 5min / change-only) rather than logging every tag every cycle.

**Where data is stored:** `plc_data`/`plc_text_data` in `machine_data.db` (see Section 5).

**Equipment simulated:** all 76 P01/P02 equipment instances (see Section 7) - Air Compressors (AC01-03), Compressed Air Header, Chillers (CHL01-02), Chilled Water Pumps (CHWP01-03), Cold Rooms (CR01-02), Water Supply Pumps (WSP01-02), Water Treatment System, Building Water Meter, Bead Mill, High-Speed Disperser, Mixer, Filling Machine, Dust Collector, Tanks (TK01-04), Solvent Transfer, Main Incomer/electrical, AHU, MCC Room, Area Monitoring, Weather Node, Fire Water System, Generator, UPS, Transformer - per plant.

**Example tags per equipment (Air Compressor AC01):**
```text
Pressure, DischargeTemp, Current, Power_kW, RunStatus, LoadStatus,
UnloadStatus, AlarmCode, StartCount, RunningHours
```

**Normal operating behavior:** each tag has a per-type profile (`TAG_PROFILES`, keyed by a plant-stripped canonical tag name) defining a realistic normal operating band, with random noise applied within that band each cycle.

**Randomization:** yes - Gaussian-ish noise within each tag's configured band every cycle.

**Fault simulation:** yes, per-equipment-instance (`_InstanceFaultState`) - a 4-phase cycle (dormant -> developing -> faulted -> recovering) that pushes every REAL tag on that instance in a physically-appropriate direction (pressure/flow/level tags fall during a fault, everything else rises), so a fault reads as one correlated event across multiple tags rather than independent per-tag noise. Faults trigger randomly over time, or can be manually forced/recovered via the Simulator (Testing Only) page.

**Deterioration simulation:** not implemented as a distinct concept - only the fault-cycle model above exists; there is no gradual, long-term equipment-degradation trend independent of a fault event.

**Correlations between variables:** yes, but scoped to *within one equipment instance during a fault* (e.g. compressor pressure drops while its current/power rises, driven by the same fault-push mechanism). There is **no cross-equipment correlation** (e.g. one pump's fault doesn't affect a downstream tank's level) and **no production-affects-energy correlation** and **no weather-affects-energy correlation** - confirmed by direct code inspection, no such logic exists anywhere in the simulator.

**Do Plant 1 and Plant 2 behave differently:** no - both plants share the exact same `TAG_PROFILES` (canonical-key-based, plant-agnostic), so P01 and P02 equipment of the same type behave identically except for independent random fault timing per instance.

**Monotonic counters** (`Energy_kWh`, running-hours, water meter totals, etc.) only ever increase within a simulator process's lifetime - a process restart re-seeds them from a lower baseline rather than resuming (a known, already-worked-around quirk - see Section 25).

---

# 9. DATA SOURCE ARCHITECTURE

The application does **not** track data provenance as a first-class concept. There is no `source` column anywhere distinguishing real-PLC vs. simulated vs. user-entered vs. calculated vs. AI-generated vs. manufacturer-reference data. Provenance is implicit, by table/column:

| Data kind | How it's actually distinguished |
|---|---|
| Real PLC data vs. simulated | Not distinguished at the data level - both flow through the identical `plc_data`/`plc_text_data` schema via whichever driver (`plc/driver_factory.py`) is currently active (`simulator`/`modbus`/`opcua`/`s7`/`fins`). The *active environment* (`config/active_environment.txt`, currently `simulation`) is the only signal, and it's a deployment-level switch, not a per-row tag. |
| User-entered data | Distinguished only by which table it lands in (`thresholds`, `maintenance_log`, `service_log`, `equipment_documents`) - these tables are never written by the simulator/PLC path. |
| Calculated data | Never persisted as such - trend/average/min/max/severity are computed on-the-fly at query time (`ai/trend_analyzer.py`, `ai/rule_engine.py`) and not written back to any table. |
| AI-generated data | Never persisted - the LLM's output is only ever returned as a response string, never written to the database. |
| Manufacturer/reference data | `equipment_documents`/`document_chunks_fts` (manuals) and `equipment.brand`/`model`/`device_number` (seeded, illustrative-but-real-brand data - explicitly documented as fabricated device numbers paired with genuine brand/model names). |

**Explicit conclusion:** no formal data-source/provenance architecture exists. This is a genuine gap if "real vs. simulated vs. calculated" needs to be surfaced to an end user or audited later.

---

# 10. ENERGY MONITORING

| Item | Status |
|---|---|
| kW | Implemented (`Power_kW` tags, e.g. Main Incomer, pumps) |
| kWh | Implemented (`Energy_kWh` monotonic counter tags) |
| Voltage | Implemented (`Voltage_L1L2`/`L2L3`/`L3L1`) |
| Current | Implemented (`Current_L1`/`L2`/`L3`) |
| Power Factor | Implemented (`PF` tag) |
| kVA | Not implemented |
| kvar | Not implemented |
| Maximum Demand | Not implemented |
| Energy Cost | Not implemented |
| Energy Tariff | Not implemented |
| Peak / Off-Peak | Not implemented |
| Energy by Plant | Implemented (SCADA info board, one per plant - but only current-day total, not historical/comparable) |
| Energy by Equipment | Partial - individual equipment `Power_kW` tags exist and are queryable via Ask AI, but there's no dedicated per-equipment energy report/rollup |
| Energy by System | Not implemented |
| Energy Intensity | Not implemented |
| kWh/tonne | Not implemented |
| Cost/tonne | Not implemented |
| Baseline | Not implemented |
| Energy anomaly | Not implemented |
| Potential savings | Not implemented |
| Verified savings | Not implemented |

**What genuinely works today:** live instantaneous electrical readings (power/voltage/current/PF/frequency) and a reset-safe "today's consumption so far" figure, both per-plant, visible on the SCADA Floor Plan info board and answerable via Ask AI ("what is the factory water/power consumption today" - power and water both have a dedicated deterministic fallback path; there is currently no equivalent for a "factory-wide kWh cost" question since cost/tariff data doesn't exist).

---

# 11. PRODUCTION DATA

| Item | Status |
|---|---|
| Product | Partial - `ProductCode` tag exists per production equipment (Filling Machine, Disperser, Mill, Mixer) |
| Product category | Not implemented |
| Batch | Partial - `BatchNumber` tag exists (STRING type, simulator-generated) |
| Recipe | Not implemented |
| Production quantity | Partial - `GoodCount`/`RejectCount` INT tags exist on the Filling Machine |
| Production rate | Not implemented (no rate calculation, only cumulative counts) |
| Good quantity | Implemented as a raw tag (`GoodCount`) |
| Reject quantity | Implemented as a raw tag (`RejectCount`) |
| Start time / End time | Not implemented (no batch/run record with start/end timestamps) |
| Line | Not implemented as a distinct concept |
| Plant | Implicit via tag prefix, same as everywhere else |
| Shift | Not implemented |
| Downtime | Not implemented (no downtime tracking/reason-coding) |

**Can energy consumption currently be correlated with production?** No. This would require joining `Power_kW`/`Energy_kWh` history against `GoodCount`/`BatchNumber` history by timestamp, and no code anywhere does this - confirmed by direct inspection of the simulator (no production-affects-energy logic) and the query/analytics layer (no such join exists).

---

# 12. MAINTENANCE

| Item | Status |
|---|---|
| Maintenance history | Implemented (`maintenance_log` table, Service & Maintenance page) |
| Preventive maintenance | Implemented (category field on `maintenance_log`, plus schedule computed from `service_interval_days`) |
| Corrective maintenance | Implemented (same table, different category value) |
| Maintenance schedule | Implemented (upcoming/overdue view, computed from `next_due_at` or `last_serviced_at + service_interval_days`) |
| Last maintenance | Implemented (`equipment.last_serviced_at`) |
| Next maintenance | Implemented (`equipment.next_due_at`) |
| Technician | Implemented as `performed_by` (free text, not linked to the `users` table) |
| Parts replaced | Implemented (`maintenance_log.parts_replaced`, free text) |
| Problem | Partial - only a generic `description` field, no structured problem/cause/action breakdown |
| Cause | Not implemented as a distinct field |
| Action | Not implemented as a distinct field (folded into `description`) |
| Before/after readings | Not implemented |
| Downtime | Not implemented |
| Maintenance cost | Not implemented |

A separate one-off "Service" log (`service_log` table) also exists for visits that don't fit the scheduled-maintenance model (no next-due concept).

---

# 13. ALARM / FAULT SYSTEM

- **Generation:** `event_monitor.service` polls `plc_data` continuously, evaluates each enabled tag against `thresholds` via `RuleEngine.evaluate_summary()`, and `EventEngine` builds an event dict for anything outside normal limits.
- **Storage:** `machine_events` table in `machine_data.db` (36,196 rows currently).
- **Severity levels:** four - `alarm`, `warning`, `normal`, `not_evaluated` (no threshold configured) - plus a UI-only `no_data` state for tags never logged.
- **Acknowledgement:** **not implemented** - no code path exists to acknowledge/clear an alarm; events are a pure append-only log, always re-evaluated live from current values.
- **Alarm history:** implemented (Event Records page - filterable by severity/equipment/tag/time; also queried by Ask AI's `timeline` intent and `RootCauseEngine`).
- **Equipment trips:** not modeled as a distinct concept from a threshold-breach alarm.
- **Simulated faults:** yes, see Section 8.
- **Pre-fault sensor history retained:** yes, implicitly - `plc_data` retains all historian readings regardless of alarm state (subject to `retention_days` in settings, default 90), so trend analysis around a fault is possible via the existing `analyse_tag()`/history query functions. There is no dedicated "pre-fault snapshot" feature, just the general historian.

---

# 14. ASK AI SYSTEM

```text
User Question
      |
      v
IndustrialQueryEngine.query() (engine/industrial_query_engine.py)
  - ConceptExtractor (engine/concept_extractor.py): typo-correction,
    intent classification (10 intents - see below), equipment/measurement/
    time-expression extraction, plant-qualifier disambiguation
  - Deterministic tag/equipment resolution (fuzzy matching, designator-code
    fast path, no LLM)
      |
      v
Resolved? --NO--> one LLM call to fix typos/rewrite an unclear follow-up
      |            question (using up to 5 turns of recent history),
      |            then retried through the resolver above once
      |
      v (still unresolved)
      Numbered candidate menu, with an LLM-phrased clarifying question
      as the lead sentence (deterministic list underneath either way)
      |
      v (resolved)
AskEngine._answer_for_tag() / _answer_comparison() / _evaluate_status() / etc.
  - DatabaseManager.get_history() (raw plc_data rows)
  - ai/trend_analyzer.py (current value, trend, min/max/avg)
  - ai/rule_engine.py (inside/outside configured limits?)
  - ai/root_cause_engine.py (root_cause only - evidence from machine_events)
  - rag/retrieval.py (root_cause only - manufacturer doc excerpts, if a
    brand/model match exists)
      |
      v
ai/prompt_builder.py builds ONE grounded prompt (facts + strict
"don't invent/don't contradict" instructions)
      |
      v
ai/ai_provider.py -> Ollama (qwen2.5:7b) phrases the final answer
(falls back to the raw deterministic text, unphrased, if Ollama is
unavailable or times out)
      |
      v
Response
```

- **LLM used:** Ollama, local, `qwen2.5:7b` (configurable; OpenAI GPT is a supported alternative via the same provider abstraction, currently unused).
- **Local or cloud:** Local only, by current configuration.
- **How prompts are generated:** `ai/prompt_builder.build_prompt()` - a single templated prompt combining the question, deterministic "confirmed observations," threshold evaluation, and (for `root_cause`) root-cause evidence + documentation excerpts, with intent-specific instructions (e.g. root_cause gets a "confirmed observations / possible causes / recommended checks" structure; every other intent gets an explicit "don't speculate" instruction).
- **Factory data included:** current value, historical trend (min/max/avg/change over a configurable lookback), and threshold evaluation for the resolved tag(s) - never raw unfiltered database access.
- **How much historical data:** governed by `_history_hours(intent)` (varies by intent, e.g. longer lookback for `trend` than `current_data`) and a row limit (`DEFAULT_HISTORY_LIMIT`), not unbounded.
- **Equipment manuals included:** yes, for `root_cause` questions only, when the resolved equipment's brand/model has ingested documentation (see Section 15).
- **RAG exists:** yes - SQLite FTS5 keyword search, not a vector database/embeddings.
- **Maintenance history included:** no - `maintenance_log`/`service_log` are not queried by `AskEngine` at all currently.
- **Alarms included:** yes, for `root_cause` (via `RootCauseEngine`) and `timeline` (via `EventStore`) intents.
- **Calculated KPIs included:** only the basic trend statistics (avg/min/max/change) computed by `ai/trend_analyzer.py` - no higher-level KPIs (efficiency, health score, etc.) exist to include, since none are calculated anywhere (see Section 16).
- **Direct database access for the AI itself:** no - the LLM never sees a connection string or is asked to write/interpret SQL; all data access happens in deterministic Python before the prompt is built.
- **AI router:** the *live* routing is entirely deterministic (`ConceptExtractor` + `IndustrialQueryEngine`) - there is no LLM-based intent router in the live path. Multiple LLM-based router prototypes exist in `ai/` (`llm_semantic_router.py`, `semantic_router.py`, `hybrid_router.py`, `llm_route_adapter.py`) but are confirmed dead code (zero imports outside `ai/` and their own tests).
- **Different question types use different logic:** yes - 10 distinct intents (`current_data`, `trend`, `threshold`, `root_cause`, `timeline`, `comparison`, `discovery`, `equipment_status`, `chitchat_greeting`, `chitchat_thanks`), each with its own answer-formatting path in `app/ask.py`.
- **Responses cached:** no caching of LLM responses anywhere.
- **Why responses may be slow:** CPU-only inference (no GPU on this VM) at `qwen2.5:7b` - roughly 0.6-0.8 tokens/sec, so a multi-paragraph root-cause answer can take 5-10 minutes; simple current-value answers are faster (shorter expected output) but still 1-3 minutes. A 600-second read-timeout is configured for the Ollama HTTP call, and answers fall back to unphrased deterministic text if that's exceeded (observed directly during this audit's own testing).

**Key AI-related files:**
- `app/ask.py` - the orchestrator (all intent-specific answer logic lives here)
- `engine/concept_extractor.py` / `engine/industrial_query_engine.py` - the real (deterministic) NLP
- `ai/prompt_builder.py` - prompt assembly
- `ai/ai_provider.py` + `ai/providers/*` - the LLM backend switch
- `ai/rule_engine.py`, `ai/root_cause_engine.py`, `ai/trend_analyzer.py`, `ai/event_engine.py`, `ai/event_store.py` - deterministic fact computation
- `rag/retrieval.py`, `rag/document_store.py` - documentation search

---

# 15. EQUIPMENT DOCUMENTATION / RAG

| Item | Status |
|---|---|
| Equipment PDFs | Implemented - 51 PDFs on disk (`manuals/`, 96MB), 30 rows in `equipment_documents` |
| Manufacturer specifications | Implemented (real manufacturer manuals for 28 of 30 documents; a few are close-family matches or synthetic placeholders where a genuine document couldn't be sourced) |
| Installation manuals | Implemented (many of the ingested PDFs are full installation/operating manuals) |
| Troubleshooting guides | Partial - covered only incidentally, where the source manual itself includes a troubleshooting section |
| Maintenance instructions | Partial, same basis as above |
| Vector database | Not implemented |
| Embeddings | Not implemented (an unused `nomic-embed-text` Ollama model is present from an earlier abandoned attempt, per prior session notes, but nothing in the live code path uses it) |
| RAG search | Implemented - SQLite FTS5 keyword search (`document_chunks_fts`, 5,941 chunks, page-number tracked) |
| Document indexing | Implemented - chunked and indexed at ingestion time (`app/import_manual.py` / the Documentation page's upload flow) |

**How Ask AI accesses this:** only for the `root_cause` intent. `AskEngine._answer_for_tag()` looks up the resolved equipment's `brand`/`model`, calls `rag/retrieval.search_chunks()` (FTS5 query built from the tag name + alarm condition/message), and if any chunks are found, appends them to the prompt with page-number citations. No other intent (current_data, trend, threshold, etc.) consults documentation at all.

**Known, previously-verified limitation (not fixed, documented for awareness):** the LLM has been observed fabricating a plausible-sounding but non-existent specific detail even when given real, correctly-retrieved documentation excerpts, and attributing it to "the manufacturer." Retrieval itself was verified accurate; this is a model-grounding-fidelity limitation, not a retrieval-quality one.

---

# 16. CURRENT CALCULATIONS

Backend/Python calculations that exist today (LLM-computed values excluded):

```text
Average, Minimum, Maximum, Change (ai/trend_analyzer.py - analyse_tag())
Trend direction (rising/falling/stable, same module)
Today's accumulated consumption from a monotonic counter, reset-aware
  (ui/scada_floor_plan_data.py - _todays_accumulated_total())
Threshold evaluation / severity classification (ai/rule_engine.py)
Two-period comparison delta and percentage (app/ask.py - _format_comparison_context())
Equipment-category grouping / zone assignment (ui/scada_floor_plan_data.py)
```

**Not implemented anywhere in backend code:** COP, efficiency, specific energy, kWh/tonne, equipment health score, anomaly score, baseline. These do not exist even as stubs.

---

# 17. EXISTING ANALYTICS

| Item | Status |
|---|---|
| Trend analysis | Implemented (`ai/trend_analyzer.py` - direction + min/max/avg/change over a lookback window) |
| Baseline comparison | Not implemented |
| Anomaly detection | Not implemented |
| Predictive maintenance | Not implemented (maintenance scheduling is purely interval-based, not condition/trend-based) |
| Equipment health score | Not implemented |
| Energy opportunity detection | Not implemented |
| Root cause analysis | Implemented - deterministic evidence gathering (`ai/root_cause_engine.py`, pulls recent related `machine_events`) + LLM phrasing under strict "don't invent a cause" instructions |
| Plant comparison | Partial - Ask AI's `comparison` intent supports comparing one tag across two time periods, but not comparing Plant 1 vs Plant 2 directly for the same metric |
| Equipment comparison | Not implemented |
| Efficiency calculation | Not implemented |

---

# 18. USER-CONFIGURABLE INFORMATION

**Configurable through the UI:**
```text
Alarm/warning limits (Setpoints page)
Equipment records - name, brand, model, device number, service interval
  (Equipment & Tag Configuration page, admin only)
Tags - name, address, data type, unit, logging cadence
  (same page)
PLC connections - protocol, host, credentials, active connection
  (PLC Connectivity page, admin only)
User accounts and roles (User Management page, admin only)
Maintenance schedule entries / log-new-work (Service & Maintenance page)
Documents - upload/delete manuals per equipment (Documentation page)
```

**Requires editing files/config manually (no UI):**
```text
AI provider/model selection (config/settings.ini's [AI]/[OLLAMA] sections,
  or the AI_PROVIDER/OLLAMA_MODEL/OPENAI_MODEL environment variables -
  the `ai_settings` table exists in the schema but has 0 rows and is not
  read by any live code)
Active environment (simulation vs actual) - config/active_environment.txt
Which plants/phases are enabled in the tag dataset - CLI flag on
  engine/tag_dataset_importer.py
Scan interval / retention days - config/settings.ini
Energy tariff, production targets, operating schedule - do not exist at
  all, configurable or otherwise
```

---

# 19. AUTHENTICATION AND USER ROLES

- **Login implementation:** custom Streamlit form (`ui/auth._login_form()`), gates the entire app via `require_login()` called once at the top of `ui/Home.py`.
- **User database:** `users` table in `config.db` (6 accounts currently).
- **Password handling:** PBKDF2-HMAC-SHA256 with a random per-user salt (`config/user_manager.py`) - not plaintext, not a fast/unsalted hash.
- **Sessions:** Streamlit's own `st.session_state`, keyed under a single `auth_user` key; no separate session table, no JWT/cookie-based session beyond what Streamlit itself manages. `disconnectedSessionTTL` is set to 0 (a same-day configuration change), meaning a dropped connection (including a browser refresh) always requires signing in again rather than silently resuming a stale session.
- **Existing roles:** three - `admin`, `engineer`, `operator`.
- **Permissions:** role-gated per page via `ui.auth.can_edit(*roles)` (view-vs-edit split, e.g. operators can view Setpoints but not change them) and `ui.auth.require_role(*roles)` (hard page-level gate, e.g. User Management is admin-only).
- **Administrator functions:** User Management, PLC Connectivity, Equipment & Tag Configuration - all three pages are entirely hidden from non-admin roles in the sidebar navigation, not just access-controlled after the fact.

(No passwords, hashes, or secrets are reproduced in this report.)

---

# 20. DATA HEALTH

| Item | Status |
|---|---|
| PLC connection status | Partial - the PLC Connectivity page shows the currently active connection/driver; `app/plc_logger.py` has auto-reconnect logic (retries every 30s, immediate retry on error), but there is no persistent "connection health" indicator surfaced in the general UI |
| Database connection | Not explicitly monitored - failures would surface as an unhandled exception, not a health indicator |
| Sensor/tag status | Partial - `no_data` severity state exists (a tag with zero historian rows shows as such everywhere it's displayed), but there's no dedicated "data quality" dashboard |
| Tag quality (good/bad/uncertain, OPC UA-style) | Not implemented - only a binary "has data or not" |
| Stale values | Not implemented - a value logged hours ago is displayed identically to one logged seconds ago (no staleness threshold/warning), **except** on the SCADA Floor Plan's live snapshot specifically, which does track a ~15-second staleness window for its background writer |
| Missing values | Partial - the `no_data` severity state again covers "never logged," but a tag that stops updating mid-session isn't specifically flagged as "missing" vs. "just not alarming" |
| Simulator status | Partial - visible only via the Simulator (Testing Only) page's recent-commands log and the underlying systemd service status (not a UI indicator) |
| AI status | Implemented - the Ask AI page shows the currently configured provider/model, and reports plainly when AI phrasing is unavailable (falls back to deterministic text with the reason shown) |
| Last data update | Partial - shown per-tag ("Updated" column on Live Data/SCADA detail panel) but not as a system-wide "last successful poll" indicator |

---

# 21. REPORTING

**Nothing in this category is implemented.** No PDF report generation, no CSV export, no Excel export, and no scheduled (daily/weekly/monthly) summary of any kind exists anywhere in the codebase - confirmed by a direct search for common export/reporting library usage (`reportlab`, `openpyxl`, `xlsxwriter`, `to_csv`, `to_excel`) across `ui/` and `app/`, which returned no matches. The only "download" functionality anywhere is the Documentation page's file download button, which serves an already-stored manual/photo as-is - not a generated report.

---

# DO NOT DUPLICATE

| Feature | Files responsible | Database tables | Current status |
|---|---|---|---|
| Deterministic NLP question resolution | `engine/concept_extractor.py`, `engine/industrial_query_engine.py` | `tags`, `equipment`, `equipment_aliases` | Complete, mature, extensively tested this session against dozens of phrasings |
| Ask AI orchestration (facts-then-LLM-phrasing) | `app/ask.py`, `ai/prompt_builder.py`, `ai/ai_provider.py` | (reads config.db + machine_data.db) | Complete, 10 intents, deliberate anti-hallucination design |
| Threshold-based alarm/warning evaluation | `ai/rule_engine.py` | `thresholds` | Complete, includes threshold-value citation in messages |
| Event/alarm history logging | `app/event_monitor.py`, `ai/event_engine.py`, `ai/event_store.py` | `machine_events` | Complete |
| Historian read/write | `database/database.py`, `app/plc_logger.py` | `plc_data`, `plc_text_data` | Complete, per-tag cadence throttling already solved |
| Equipment/tag configuration + PLC driver abstraction | `plc/driver_factory.py` + protocol drivers, `config/plc_connection_manager.py` | `tag_addresses`, `plc_connections` | Complete for Modbus/OPC UA/S7/FINS, simulator |
| Manufacturer documentation RAG | `rag/document_store.py`, `rag/retrieval.py` | `equipment_documents`, `document_chunks_fts` | Complete (FTS5 keyword search) |
| Realistic multi-equipment simulator with correlated faults | `simulator/tag_dataset_model.py` | (writes to `plc_data` via `plc_logger`) | Complete |
| Authentication + RBAC | `ui/auth.py`, `config/user_manager.py` | `users` | Complete (3 roles) |
| Maintenance scheduling | `ui/pages/4_Service_and_Maintenance.py`, `engine/maintenance_migrator.py` | `maintenance_log`, `service_log`, `equipment.next_due_at` | Complete for the interval-based model it targets |
| SCADA-style live floor plan | `ui/pages/12_SCADA_Floor_Plan.py`, `ui/scada_floor_plan_data.py`, `ui/scada_snapshot_writer.py` | (reads config.db + machine_data.db) | Complete, flicker-free, theme-aware |

---

# EXTEND EXISTING

**Feature: Energy monitoring**
Existing: live kW/kWh/V/A/PF per plant, reset-safe "today's consumption," Ask AI can answer factory-wide power/water questions.
Missing: tariff, cost, maximum demand, kVA/kvar, baseline, anomaly detection, energy-by-equipment rollups, kWh/tonne.
Recommendation: extend `ui/scada_floor_plan_data.py`'s board-building functions and `app/ask.py`'s factory-total fallback pattern (already proven for power and water) rather than building a parallel energy module.

**Feature: Equipment model**
Existing: `equipment` table with brand/model/device number/maintenance interval; plant/area encoded in naming convention.
Missing: explicit plant/building/area/system columns, serial number, ratings (power/voltage/current/flow/pressure), capacity, installation date, criticality.
Recommendation: add columns to the existing `equipment` table via a new additive migrator (matching the established pattern in `engine/equipment_metadata_migrator.py`), rather than a new equipment table.

**Feature: Maintenance**
Existing: interval-based scheduling, history log, technician (free text), parts replaced.
Missing: structured problem/cause/action fields, downtime, cost, before/after readings, linking `performed_by` to real user accounts.
Recommendation: extend `maintenance_log`'s schema, not a new table.

**Feature: Production data**
Existing: raw per-equipment tags (`GoodCount`, `RejectCount`, `BatchNumber`, `ProductCode`).
Missing: any structured batch/run record (start/end time, line, shift), and any energy-production correlation.
Recommendation: a new `production_runs` table keyed by equipment + time range, populated by watching the existing tags for batch-boundary transitions (similar pattern to the SCADA writer's polling design) - this is closer to "build" than "extend," since there's no structural table to extend, only raw tags to build on top of.

**Feature: Data health**
Existing: `no_data`/`not_evaluated` severity states, AI-provider status indicator, PLC auto-reconnect.
Missing: system-wide staleness thresholds, a unified data-health view, tag quality beyond binary has-data/no-data.
Recommendation: extend `ai/rule_engine.py`'s severity model (a natural place, since it already computes per-tag status everywhere) rather than a parallel health-check system.

---

# NOT YET IMPLEMENTED

```text
Energy tariff / cost / maximum demand / kVA / kvar
Energy baseline comparison and anomaly detection
kWh/tonne, cost/tonne, energy intensity, specific energy
Energy savings tracking (potential or verified)
Equipment ratings (power/voltage/current/flow/pressure/capacity)
Serial number, installation date, criticality on equipment
Explicit Plant/Building/Area/System as database entities (currently naming-convention-only)
Structured production run/batch tracking (start/end, line, shift, downtime)
Energy-vs-production correlation
Weather-vs-energy correlation
Predictive maintenance (condition/trend-based, as opposed to interval-based)
Equipment health scoring
Alarm acknowledgement workflow
Plant-vs-plant / equipment-vs-equipment comparison analytics
PDF/CSV/Excel report generation, scheduled summaries
Vector-embedding-based RAG (current RAG is keyword/FTS5-based)
Data provenance tracking (real vs. simulated vs. calculated vs. AI-generated)
System-wide data-staleness/health dashboard
```

---

# TECHNICAL DEBT / RISKS

- **Extensive dead code in `ai/`:** roughly 18 of 25 files in `ai/` (excluding `providers/`) are not imported by any live code path - multiple superseded router/parser/knowledge-engine prototypes from earlier development generations. Zero functional risk (nothing calls them), but real cognitive/maintenance overhead for anyone reading the codebase fresh, and they inflate the regression-test-failure baseline (12-15 of the ~110 tests exist only to exercise this dead code and some are documented as already-broken/never-fixed).
- **Orphaned equipment rows:** 7 of 83 `equipment` rows have zero associated tags (see Section 5) - likely safe leftovers, but not verified safe to delete without checking `equipment_documents` links first.
- **Empty/unused config tables:** `ai_settings`, `communication`, `equipment_tags` all have 0 rows and no live code writes to them - either dead schema or an intended-but-never-built feature (AI settings UI, most likely, given the table's column names).
- **PLC connection passwords stored in plaintext:** `plc_connections.password` is a plain TEXT column (unlike user account passwords, which are correctly hashed) - a real credential-exposure risk if this database file is ever copied/shared.
- **Unauthenticated static file route:** `/app/static/...` (Streamlit's static file server) serves both equipment manuals *and* the live SCADA snapshot JSON with no login check at all - accepted as a risk specifically because the app is assumed to run on an internal network only; would need addressing before any external exposure.
- **Blocking AI calls:** `AskEngine.ask()` makes a synchronous HTTP call to Ollama with up to a 600-second timeout; a genuinely slow/stuck model call blocks that user's request for up to 10 minutes (the Ask AI page works around this with a background-threaded polling pattern, but the underlying call itself is still a long blocking operation).
- **No database indexes verified beyond SQLite's implicit rowid/primary-key indexes** - `plc_data` (6.87M rows) is queried by `tag` + time range frequently; whether a compound index exists on `(tag, time)` was not confirmed in this audit and is worth checking given the table's size.
- **Two parallel, incompatible "driver selection" implementations** exist in `engine/industrial_query_engine.py` vs. the untracked `engine/industrial_query_engine_before_driver_selection.py` - documented in prior session notes as deliberately unresolved WIP, not currently causing a problem but a merge conflict waiting to happen if picked up carelessly.
- **`ai/ai_bridge.py` has uncommitted, unfinished edits** wiring in `RootCauseEngine` despite being dead/unimported code - same category of risk as above.
- **Hardcoded plant assumption in factory-wide fallbacks:** `FACTORY_TOTAL_POWER_TAG`/`FACTORY_WATER_METER_TAG` in `app/ask.py` are hardcoded to `P01.*` - a factory-wide question always answers from Plant 1's main incomer/water meter specifically, not a true sum across all plants. Not a bug given the current 2-plant setup and existing precedent, but worth knowing before assuming "factory-wide" means "all plants combined."
- **`config/settings.ini` has a stale/confusing value:** `[AI] model = gpt-5.5` sits under `provider = ollama` - harmless (unused while on Ollama) but misleading to read.
- **No automated data-provenance labeling** (see Section 9) - if real PLC data and simulated data are ever mixed in the same historian file during a transition period, there is currently no way to distinguish rows after the fact.
- **`config/` has four overlapping configuration-loading modules** (`config_manager.py`, `configuration_manager.py`, `configuration_service.py`, `configuration_importer.py`) with similar-sounding names and only partially overlapping responsibilities - worth understanding which is actually live before extending any of them (the live UI path uses `configuration_manager.py`'s `ConfigurationManager`, confirmed via `app/ask.py`/UI page imports; the others' live-usage status was not individually re-verified in this pass).
- **No rate-limiting on the login form** - unlimited password attempts are possible.

---

# RECOMMENDED DEVELOPMENT ORDER

**Priority 1 — Foundation**
- Add explicit Plant/Area columns to `equipment` (currently naming-convention-only) if the roadmap needs them queryable rather than parsed.
- Add equipment ratings (power/voltage/current/flow/pressure/capacity), serial number, installation date, criticality columns.
- Resolve/clean the dead `ai/` code and the two orphaned WIP files, to reduce confusion for future development.
- Decide the fate of the 7 orphaned equipment rows.

**Priority 2 — Energy Intelligence**
- Add tariff/cost configuration and wire it into the existing per-plant kWh calculation.
- Add maximum demand tracking.
- Build energy-by-equipment rollups on top of the existing `Power_kW` tags.
- Add baseline + simple anomaly detection (even a static/statistical baseline would be new capability here).

**Priority 3 — Equipment Intelligence**
- Structured production run/batch tracking, built on the existing raw production tags.
- Equipment health scoring (a genuinely new calculation layer).
- Energy-vs-production correlation, once both sides have queryable structured data.

**Priority 4 — AI Integration**
- Extend Ask AI to consult maintenance/service history (not currently included in any prompt).
- Consider whether the newer Ask AI capabilities (comparison, factory-wide fallbacks) should extend to a true multi-plant "factory-wide" sum rather than Plant 1 only.
- Revisit citation-fidelity/grounding limitation once on better hardware (documented, not something to chase further on current hardware).

**Priority 5 — Professional UI / Reporting**
- PDF/CSV/Excel export (nothing exists today).
- Scheduled summary reports.
- A true KPI dashboard (charts/graphs - currently zero exist anywhere in the UI).
- Alarm acknowledgement workflow.

---

# 27. FINAL FEATURE MATRIX

| Feature | Current Status | Existing Location | Recommended Action |
|---|---|---|---|
| Factory profile | NOT IMPLEMENTED | - | Build |
| Plant configuration | BASIC (naming convention only) | `equipment.name`/`tags.tag_name` prefix | Extend |
| Energy tariff | NOT IMPLEMENTED | - | Build |
| Energy cost | NOT IMPLEMENTED | - | Build |
| Production | BASIC (raw tags only) | `tags` (GoodCount/RejectCount/BatchNumber) | Extend/Build |
| Equipment master | PARTIAL | `equipment` table | Extend |
| Maintenance | COMPLETE (for interval-based model) | `maintenance_log`, `service_log` | Extend |
| Engineering notes/thresholds | COMPLETE | `thresholds`, Setpoints page | Keep |
| Baselines | NOT IMPLEMENTED | - | Build |
| Anomaly detection | NOT IMPLEMENTED | - | Build |
| Energy opportunities | NOT IMPLEMENTED | - | Build |
| Savings tracking | NOT IMPLEMENTED | - | Build |
| Equipment health | NOT IMPLEMENTED | - | Build |
| Data health | PARTIAL | `ai/rule_engine.py` severity states, Ask AI status indicator | Extend |
| Ask AI | COMPLETE | `app/ask.py` + supporting `ai/`/`engine/` modules | Keep |
| RAG/manuals | COMPLETE (keyword-based) | `rag/`, `equipment_documents` | Keep |
| Reports | NOT IMPLEMENTED | - | Build |

---

# FILES MOST IMPORTANT FOR FUTURE DEVELOPMENT

1. `app/ask.py` - the entire Ask AI orchestration logic; almost any new question type or data source lives here.
2. `engine/concept_extractor.py` - the NLP vocabulary/intent classifier; extending what Ask AI can *understand* starts here.
3. `engine/industrial_query_engine.py` - the deterministic tag/equipment resolver that concept_extractor feeds into.
4. `ai/prompt_builder.py` - controls exactly what the LLM is told and how strictly it's constrained; touch carefully.
5. `ai/rule_engine.py` - the single source of truth for "is this value normal/warning/alarm," used by nearly every page.
6. `database/database.py` - all historian reads/writes; any new time-series data type goes through here.
7. `simulator/tag_dataset_model.py` - the live data generator; new equipment/tags/correlations start here.
8. `engine/tag_dataset_importer.py` - how new equipment/tags/plants get registered into `config.db`.
9. `equipment` table (`config.db`) - the equipment master; any equipment-model extension changes this table's shape.
10. `ui/scada_floor_plan_data.py` - the current home of energy/water rollup calculations; likely the place to extend for more energy KPIs.
11. `ui/data_access.py` - shared UI data-access helpers; where new UI-facing queries should live for consistency.
12. `rag/retrieval.py` - the documentation search entry point; relevant if RAG is ever upgraded to embeddings.
13. `config/environment.py` - the simulation/actual environment switch; relevant to any real-PLC rollout.
14. `plc/driver_factory.py` - the PLC protocol abstraction; relevant once real hardware is connected.
15. `config/user_manager.py` - authentication/roles; relevant to any new permission or user-facing feature.
16. `ai/root_cause_engine.py` - deterministic evidence-gathering for root-cause questions; the place to extend if maintenance history should factor into diagnosis.
17. `engine/models.py` - the `Concepts`/`QueryResult` dataclasses that define the NLP pipeline's data shape end-to-end.
18. `CLAUDE.md` (project root) - the existing, detailed running history of design decisions, gotchas, and standing constraints - read before making architectural changes.
