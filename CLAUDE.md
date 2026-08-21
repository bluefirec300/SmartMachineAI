# SmartMachineAI - Project Context

## What this project is

An AI assistant that monitors factory equipment (cold room, compressor,
chiller, tank/pump, power consumption, and a much larger P01/P02 tag
dataset - see "Current data model" below) and answers engineer questions
like "why is the compressor pressure dropping" - explaining likely
causes, tracking history, and suggesting troubleshooting steps. This
Q&A logic is meant to eventually be embedded into a SCADA interface for
operators to query directly.

- **Current stage:** development, no live PLC yet. A simulator generates
  fake sensor data and alarm events for testing; real PLC drivers
  (Modbus/OPC UA/S7) exist and are wired up but not yet pointed at a
  real plant.
- **Hardware:** 4-core/16GB Ubuntu VM, **no GPU**. Local Ollama inference
  is CPU-only and slow (~0.6-0.8 tokens/sec) - a "why" root-cause answer
  takes ~5-10 minutes on `qwen2.5:7b`. Accepted deliberately for now; a
  GPU/cloud-API upgrade is planned once the pipeline design is fully
  validated, not before.
- **End users:** the factory's engineering department (not general
  operators) - questions can be reasonably technical.
- **NLP goal:** flexible, casual natural-language questions should
  resolve correctly without a large hardcoded alias/tag list, with real
  typo tolerance (not just for equipment names).
- **LLM backend:** switchable between OpenAI (GPT) and Ollama (Qwen) via
  `ai/providers/provider_factory.py`. Currently `qwen2.5:7b`. Qwen is the
  intended production choice for cost reasons; OpenAI is for dev/testing.
  **Do not remove this switchability.**
- **The person running this project** is a project engineer, not a
  professional programmer - comfortable with basic code but prefers
  step-by-step explanations and copy-paste-able commands over dense
  technical jargon.

## Architecture

Three "generations" of code exist from earlier ChatGPT-assisted
development. Only some of it is actually wired together:

**Live / working:**
- `app/plc_logger.py` -> reads simulator or real PLC -> writes to
  `plc_data` table in `database/machine_data.db`. Runs via the
  `plc_logger.service` systemd unit. Throttles writes to each tag's
  configured logging cadence (10s/1min/5min/change-only) rather than
  writing every tag every cycle. Auto-reconnects every 30s and on error;
  hot-swaps driver/tags within one interval when the active PLC
  connection changes in the UI (`_refresh_driver()`).
- `app/event_monitor.py` -> reads `plc_data` -> `RuleEngine` checks
  thresholds (from `database/config.db`) -> `EventEngine` builds event
  dicts -> `EventStore` writes to `machine_events`. Runs via the
  `event_monitor.service` systemd unit. **Don't manually launch a
  second copy** - `Restart=always` means systemd just respawns a killed
  process ~5s later, so a "stray" process is almost always this service.
- `engine/industrial_query_engine.py` (+ `app/query_cli_v2.py`) ->
  deterministic question -> tag/equipment/intent resolver. No LLM.
  Includes a designator-code fast path for exact instance codes (e.g.
  "CR01", "AC02") and plant-qualifier disambiguation (P01 vs P02) - see
  "NLP / query engine" below.
- `app/ask.py` -> orchestrator tying the deterministic engines to the
  LLM. See "`app/ask.py` pipeline" below for the full question-type
  list and data flow.
- `engine/tag_dataset_importer.py` - imports the two-plant master tag
  list (`config/master_tag_list.json`, 624 tags) into `config.db`,
  additive/idempotent. Both P01 and P02 are now fully enabled (625/625
  tags, 76 equipment instances).
- `simulator/tag_dataset_model.py` - generic value generator for the
  imported dataset. `plc/simulator_driver.py` still instantiates the
  legacy `simulator/factory_model.py` (`FactorySimulator`) alongside
  it, but that class produces no live output - the ~20-tag hand-scripted
  demo it drives was fully retired (deleted from `tags`/`equipment`,
  see "Current data model" below), so it's structurally still wired in
  but functionally inert. Each equipment instance has its own
  dormant->developing->faulted->recovering fault cycle so faults read as
  one correlated event. Start-count tags increment only on a genuine
  0->1 transition of their paired running-status tag.
- `engine/equipment_metadata_migrator.py` /
  `engine/seed_equipment_metadata.py` - `device_number`/`brand`/`model`/
  `service_interval_days`/`last_serviced_at` on `equipment`, seeded with
  illustrative (fabricated but real-brand) data.
- `rag/document_store.py` / `rag/retrieval.py` / `app/import_manual.py`
  - manufacturer-documentation storage/search (SQLite FTS5 keyword
  search, not embeddings), wired into `app/ask.py`'s `root_cause` path
  only. See "Equipment metadata & document lookup" below.

**Wired together via `app/ask.py`:**
- `ai/root_cause_engine.py` - deterministic root-cause analysis from
  `machine_events` history.
- `ai/prompt_builder.py` - builds a grounded LLM prompt from
  observations + rule evaluation + root-cause evidence + (root_cause
  only) retrieved documentation excerpts + (comparison only) a
  pre-computed verdict the LLM may only restate, never recompute.
- `ai/ai_provider.py` + `ai/providers/*` - the Qwen/GPT switch layer.

**Dead code cleanup - done (Phase V1.1, commit `15f211f`).** The
former `ai/ai_bridge.py` cluster (+ its `.backup*` files),
`ai/simulator/factory_simulator.py` (+ backup),
`ai/understanding/question_understanding.py`,
`engine/industrial_query_engine_before_driver_selection.py`, and the
stray `config/simulator_config.json`/`config/simulator_command.txt`
leftovers were all removed. `config/system.json` was kept and is now
a normal tracked file (not uncommitted WIP). `app/machine_query.py`
is the one item from that original list still present and still
unimported - safe to ignore/delete whenever it's next touched.

## Current data model

Both P01 and P02 of the two-plant tag dataset are fully enabled
(625/625 tags, 76 equipment instances - `config/master_tag_list.json`,
imported via `engine/tag_dataset_importer.py`). Thresholds are seeded
for the 88 threshold-eligible tags per equipment type
(`engine/seed_engineering_thresholds.py`, grounded where possible in
real standards). Cumulative totals (`*Hours`, `*Energy_kWh`, `*.Total`,
`*StartCount`) and control setpoints (`*.Setpoint`) are deliberately
excluded from Setpoints-page configurability - a fixed limit on a
number that only ever increases, or on a target rather than a measured
value, doesn't make sense. `simulator/tag_dataset_model.py`'s
`TAG_PROFILES` give each tag type its own realistic operating band
(not a shared generic per-unit range), so seeded thresholds match what
the simulator actually produces. The legacy ~20-tag hand-scripted demo
has been fully retired (deleted from `tags`/`equipment`).

**Landmine:** STRING-typed tags (e.g. `AlarmCode`) must never be
written to `plc_data` (a `REAL`-only column) - the historian skips them
entirely by design; the paired boolean Alarm tag + `machine_events`
already capture the fault. Each tag's historian save is wrapped in its
own try/except so one bad tag can never block the rest of a cycle.

## NLP / query engine - current capabilities

`engine/concept_extractor.py` + `engine/industrial_query_engine.py`
resolve a free-text question to a tag/equipment/intent without an LLM.
Supported intents: `current_data`, `root_cause`, `trend`, `threshold`,
`timeline`, `discovery` ("what's available to check"), `equipment_status`
("is everything ok", "how is AC01 doing", "show all cold room status"),
`chitchat_greeting`/`chitchat_thanks`, `comparison` ("compare power
today vs yesterday", or vs. any specific date), and a factory-wide
timeline fallback for time-based questions with no equipment named.
`discovery`, `equipment_status`, the factory-wide timeline fallback,
and chitchat all resolve instantly with no LLM call by default
(`INSTANT_ANSWERS_SKIP_LLM = True` in `app/ask.py`; can be flipped to
route them through the LLM for conversational phrasing at the cost of
a multi-minute wait - tried and reverted, kept as a flag).

Other resolved capabilities worth knowing about before touching this
code:
- **Designator-code fast path**: an explicit instance code (e.g. "AC01")
  wins outright over fuzzy/generic matches.
- **Plant-qualifier disambiguation**: "P01"/"p2"/"plant 2" is extracted
  generically (not hardcoded to 2 plants) and used to break ties when
  the same instance code exists in multiple plants.
- **`days_ago:N`**: handles "3 days ago", "two days before yesterday",
  "the day before yesterday", etc. as a specific calendar day, not just
  the fixed today/yesterday/last_7_days buckets.
- **5-turn rolling conversation history** (`AskEngine._history`): when
  the fast matcher fails on what looks like a bare follow-up ("how
  about yesterday?", "and the chiller?"), a short LLM call rewrites it
  into a standalone question using recent history before retrying the
  deterministic match. Fallback-only - never trusted as an answer
  itself, only ever rewrites the *question* text.
- **Numbered-menu clarification** doubles as a free-text counter-answer
  mechanism: any non-numeric reply to an ambiguity menu is treated as a
  fresh question rather than a menu selection.
- **`comparison` intent**: `database/database.py`'s `get_history_range()`
  + `app/ask.py`'s `_answer_comparison()` compute a real two-period
  comparison deterministically; the LLM is only allowed to restate the
  pre-computed verdict, never recompute or reverse it.

**Standing landmine - typo-correction false positives:** `_correct_word()`
(edit-distance-1-gated `SequenceMatcher` fuzzy correction) has
repeatedly mis-corrected short, legitimately-spelled real words into
an unrelated vocabulary term ("or"->"orp", "no"->"now", "mill"->"fill",
spelled-out numbers like "two"->"to", etc.) - each occurrence is
protected via `STOPWORDS` (for pure connector/number words) or
`VOCABULARY_WORDS`/`EQUIPMENT_NAME_WORDS` (for words that must still
survive into `equipment_terms`). **If a new phrase mysteriously fails
to match**, check `correct_spelling(question)` first - it may have
silently mangled a word before the intent classifier ever saw it. This
bug class has recurred five separate times; the edit-distance-1 guard
narrowed it substantially but did not eliminate it.

## Equipment metadata & document lookup (RAG)

`equipment` has `device_number`/`brand`/`model`/`service_interval_days`/
`last_serviced_at` (seeded with illustrative but real-brand data).
`equipment_documents` + `document_chunks_fts` (SQLite FTS5, not
embeddings - `nomic-embed-text` sits unused from an earlier abandoned
attempt) store manufacturer manuals, searched and injected into the
`root_cause` prompt only, and only when something is actually found.

25 of 28 equipment brand/models have real manufacturer documentation
(sourced from official manufacturer sites, verified via `pypdf` text
extraction). 3 remain synthetic after genuine sourcing attempts failed
(Eaton 9395, Donaldson Torit, IMA filling line). Real PDFs live in
`manuals/real/`, the 4 direct-in-`manuals/` files are the synthetic
placeholders plus `.gitkeep`; `equipment_documents.file_path` is
backfilled to point at the correct location for all 28 rows. Documents
are viewable in-browser (not just downloadable) via `ui/static ->
../manuals` + `.streamlit/config.toml`'s `enableStaticServing = true`.
**Known trade-off:** the static file server bypasses `ui/auth.py`'s
login gate entirely - anyone with a document's URL can view it without
logging in. Judged acceptable (equipment documentation, presumed
internal network) but worth reconsidering if that changes. The same
route (`ui/static -> ../manuals`) is also now used by the SCADA Floor
Plan's live snapshot writer (`manuals/_scada_live.json`) - explicitly
re-confirmed acceptable on the same reasoning (equipment status/power
data, not sensitive process control, presumed internal network) rather
than building a second, authenticated serving mechanism for one file.

**Known, accepted LLM limitation - do not keep chasing this with more/
better documents:** end-to-end root-cause answers against real
`qwen2.5:7b` have fabricated plausible-sounding, specific-seeming
details not present in the actual retrieved chunks, and falsely
attributed them to "the manufacturer"/"the documentation" - reproduced
against both synthetic and real (86-chunk Atlas Copco) source material.
This is a model grounding-fidelity limitation, not a retrieval-quality
or documentation-quality problem (retrieval itself has been spot-checked
as genuinely good). Revisit once on better hardware with a larger model.

## PLC Connectivity - current capabilities

`plc/driver_factory.py`'s `resolve_driver_name()` is the single source
of truth for which driver/tags are active (active `plc_connections` row,
else `settings.ini`'s legacy fallback) - both the tag query and the
actual driver object must use this, not read `settings.ini` directly,
or they can silently point at two different protocols.

- **OPC UA "Browse" picker** (`plc/opcua_driver.py`'s `browse_nodes()`,
  used from the Tag Mapping dialog): depth/count-capped, skips every
  namespace-0 node (OPC Foundation boilerplate, never real vendor tags
  per spec), shows each node's data type, and blocks saving a mapping
  with a confirmed type mismatch (shown ✅/⚠️/❔, not silently allowed).
- **Disconnect** actually disconnects the live process now (not just
  the DB flag) - `app/plc_logger.py`'s `_refresh_driver()` re-resolves
  the active driver every `RECONNECT_INTERVAL_SECONDS` (30s) and
  rebuilds the driver object if it's genuinely changed.
- **Auto-reconnect**: every 30s regardless of failure signal (catches
  e.g. an OPC UA idle-timeout drop with zero mapped tags, which
  produces no exception to react to), plus immediately on any
  exception from the main read loop.
- A separate Claude-Code-built OPC UA test server/simulator exists at
  `/home/test/opcua-web-simulator` (control UI :8502, OPC UA server
  :4840, endpoint `/opcua/simulator`) for testing this against a real
  server without real plant hardware.

## Web UI (`ui/`)

Streamlit app (`ui/Home.py`, `st.navigation`-based). Run via:
```bash
source venv/bin/activate
streamlit run ui/Home.py --server.headless true --server.port 8501
```
- `ui/data_access.py` - all SQLite reads/writes for the UI (reads
  whatever is currently **enabled** in `database/config.db`).
- **Live Data** - current value of every enabled tag, color-coded by
  `RuleEngine` severity, `@st.fragment(run_every=5)`-based auto-refresh.
- **Setpoints** - view/edit low/high warning/alarm limits, two-step
  confirm-before-write flow, audit log.
- **Service & Maintenance** - two tabs: one-off Service visits
  (`service_log` table, no schedule concept) and scheduled Maintenance
  (upcoming/overdue tracking from `next_due_at`/`service_interval_days`,
  done/ignore actions, history) as `st.dataframe` tables with row
  selection driving the action buttons.
- **Documentation** - manual/photo browser with in-browser View +
  Download per document (see RAG section above for the trade-off).
- **PLC Connectivity** (admin) - connection management, OPC UA Browse
  picker, disconnect/reconnect.
- **Equipment & Tag Configuration** (admin) - equipment/tag CRUD.
- **User Management** (admin) - accounts/roles (`config/user_manager.py`,
  PBKDF2-HMAC-SHA256 + random salt).
- **Simulator (Testing Only)** - manually trigger/force-recover a
  realistic, equipment-correlated fault via `simulator_fault_commands`
  (cross-process command table, since `plc_logger` owns the live
  simulator instance in a separate OS process). Hidden entirely outside
  the simulation environment.
- **SCADA Floor Plan** - imagined room layout (grounded in the real
  `P01.<AREA>.*` tag-naming convention, not a real factory drawing)
  showing every equipment instance's live severity plus a per-plant
  info board (power, utilities, plant health). **A flicker-free rewrite
  (background snapshot thread + client-side JS polling, replacing the
  flashing `st.fragment(run_every=...)` approach) and integration into
  the main app's sidebar (below Ask AI, retiring standalone port 8503)
  is in progress** - see
  `docs/superpowers/plans/2026-08-13-scada-floor-plan-live-view.md` for
  the design and implementation plan.

- **System Health** (Phase V2.1) - is SmartFactoryAI's own software
  running correctly, not the factory - see "System Health" below for
  the full design. Deliberately distinct from Equipment Health/Data
  Health, which are both about factory telemetry, not this app's own
  processes.

Auth: `ui/auth.py`'s `require_login()` gates every page from `Home.py`;
individual pages check `ui.auth.can_edit()`/`has_role()` for
write/admin-only controls. (This page list predates several later
phases - e.g. Energy Dashboard, Anomalies, Energy Opportunities,
Savings Verification, Equipment Health/Asset Performance/Data Health,
Factory Configuration, Production Context all exist and are not listed
above; not backfilled here, out of scope for this phase.)

## System Health (`ui/system_health_data.py`, `ui/pages/22_System_Health.py`)

Phase V2.1. Answers "is the SmartFactoryAI software itself running
correctly" for an engineer with no terminal/SSH access - all 13
systemd services (3 core: `plc_logger`, `event_monitor`, `streamlit`;
9 `*_worker` services; `production_simulator`), each showing running/
stopped state, uptime, restart count, last known activity, and a soft
recent-error scan.

- **No sudo required**: every call is a read-only `systemctl show`/
  `journalctl -u` query - confirmed these need no elevated privilege
  for the same user `streamlit.service` already runs as
  (`User=test`, group `adm`).
- **No invented health information**: every field is a real systemd
  property, a timestamp a service already writes to a table it owns as
  part of its real job, or one of `app/historian_maintenance_worker.py`'s
  existing marker files - never a new duplicate heartbeat mechanism.
  An unavailable signal shows "Unavailable", never a guess.
- **Landmine already fixed once**: a naive "does this log line contain
  the word 'error'" scan false-positived on every worker's own healthy
  `cycle: {..., 'errors': 0, ...}` summary line. `_is_error_line()`
  parses the worker's own reported count first and only falls back to
  a keyword scan for lines that aren't that shape (an actual exception/
  traceback/"X failed:" message). If a new worker's log format doesn't
  fit either shape, check this function before trusting its error count.
- Several workers only write to their output table when something
  worth recording actually happens (e.g. `opportunity_worker`'s
  `energy_opportunities.last_updated`, `energy_kpi_worker`'s daily
  summary) - a long gap since "last activity" is not automatically a
  failure for those; the page's "What that means" column says so per
  service rather than implying a uniform per-tick heartbeat that
  doesn't actually exist.

## `app/ask.py` pipeline

```
question -> IndustrialQueryEngine (tag/equipment/intent)
         -> [chitchat / discovery / equipment_status / factory-wide
            timeline / comparison: instant-by-default, deterministic]
         -> [ambiguous/unrecognized: LLM-assisted typo/follow-up
            correction retried once, else numbered menu]
         -> DatabaseManager.get_history / get_history_range
         -> trend_analyzer.analyse_tag
         -> RuleEngine.evaluate_summary
         -> RootCauseEngine (root_cause only)
         -> prompt_builder.build_prompt
         -> AIProvider.generate (Qwen or GPT phrases the final answer)
```

Key design choices to preserve:
- The LLM only ever **phrases** an answer from deterministic facts -
  never invents a value, and for `comparison` may not even recompute
  the verdict, only restate it.
- No AI provider available -> deterministic-only text answer, never a
  crash. Don't remove this fallback.
- Run standalone with `python -m app.ask`.

## Background services (systemd)

13 services under `/etc/systemd/system/` as of Phase V1.6, all
`Restart=always`, all need a manual restart to pick up any code change
(each imports its Python modules once at process start). Verify the
live count/health with `systemctl list-units --all | grep -iE
"worker|plc_logger|event_monitor|streamlit"` rather than trusting this
list's count to stay current.

**Three core services, not git-tracked** (unit files live only in
`/etc/systemd/system/`, not under `deploy/`):
```bash
sudo systemctl restart plc_logger.service event_monitor.service streamlit.service
```
- `event_monitor.service` - killing the process just makes it respawn
  ~5s later. Check `systemctl status <name>.service` before assuming a
  process is a stray leftover.
- `plc_logger.service` - `ExecStart=.../venv/bin/python -m app.plc_logger`,
  `WorkingDirectory=/home/test/SmartMachineAI`.
- `streamlit.service` - `streamlit run ui/Home.py --server.headless true
  --server.port 8501`, same `WorkingDirectory`.

**Nine `*_worker.service` background engines, git-tracked**
(`deploy/systemd/*.service`, one per deterministic engine - anomaly,
asset_performance, baseline, data_health_history, energy_kpi,
equipment_health, historian_maintenance, opportunity,
savings_verification):
```bash
sudo systemctl restart anomaly_worker.service asset_performance_worker.service \
  baseline_worker.service data_health_history_worker.service energy_kpi_worker.service \
  equipment_health_worker.service historian_maintenance_worker.service \
  opportunity_worker.service savings_verification_worker.service
```

**One more, not git-tracked, not yet moved under `deploy/`:**
`production_simulator.service` (`app/production_simulator.py`) -
generates simulated production batches for the production-normalized
energy metrics.

No `sudo`/TTY access from this session - ask the user to run restart
commands themselves (suggest the `! <command>` prefix).

## Known issues / standing constraints

1. **Database path convention**: every module expects
   `database/config.db`. If something throws "Run this first: python -m
   engine.metadata_migrator" or can't find `config.db`, check for a
   stray `config/config.db` first:
   ```bash
   cp config/config.db database/config.db
   python -m engine.metadata_migrator
   ```
2. `requirements.txt` has `openai>=1.0` and `pypdf>=4.0`. `cryptography`
   is installed in the venv (needed for encrypted PDFs) but not yet
   added to `requirements.txt`.
3. **When stripping the database before sharing/zipping**, delete
   `machine_data.db`, `machine_data.db-wal`, and `machine_data.db-shm`
   together - leaving the -wal/-shm files behind without the main .db
   causes confusing SQLite behavior.
4. `config/settings.ini` has `[AI] model = gpt-5.5` under
   `provider = ollama` - harmless (unused while on ollama) but
   confusing to read. Not cleaned up.
5. **Historian timestamps use `strftime("%Y-%m-%d %H:%M:%S")`, not
   `.isoformat()`** - matches `machine_events.event_time`'s format and
   ~980K legacy rows. Reverting this silently breaks same-day lookback
   windows (a plain string comparison, `' ' < 'T'` lexicographically).
   **Don't revert.**
6. Root-cause prompts can involve 20+ near-duplicate evidence lines and
   documentation excerpts - `_condense_evidence()` collapses to one
   line per distinct `(tag, condition)` pair, and the Ollama provider's
   `read_timeout` is 600s to accommodate this hardware.
7. Hardware is genuinely weak for this workload (4 cores, no GPU) -
   root-cause answers with a documentation excerpt can take ~10 minutes.
   Intentionally deferred until the pipeline design is fully validated.
8. Alarm/warning messages (`RuleEngine.evaluate_summary()`) cite the
   specific threshold breached (e.g. "high alarm limit: 108 °C"), not
   just the current value - both Event Records and the LLM prompt get
   this. **Not backfilled** to the ~11,000+ pre-existing `machine_events`
   rows (the threshold in effect *at the time* isn't necessarily today's
   value) - only new events from 2026-08-12 onward have it.

## Preferences for how to work on this project

- Explain changes in plain language, step by step.
- This person is not a professional developer - avoid unexplained
  jargon, prefer runnable commands over abstract descriptions.
- Don't remove the Qwen/GPT provider-switching architecture.
- Prefer wiring/reusing the existing deterministic engines over
  reinventing them - they're well-built, just need connecting.
- Before assuming a background process is a stray leftover, check
  `systemctl status <name>.service` and `/etc/systemd/system/` first.
- When the codebase's actual state (git status, running processes,
  files that do/don't exist) disagrees with this doc, trust the
  codebase and fix the doc - this doc has been stale before.
- For any large, multi-part feature, propose a phased plan and confirm
  scope/starting point before building, rather than guessing how much
  to build in one pass.
- Before declaring an LLM-grounding feature "working," actually check
  the model's specific claims against the real underlying data/
  evidence it was given - don't just check that the right context made
  it into the prompt.
- Commit only when explicitly asked, and stage specific files rather
  than `git add -A`/`.` - this repo periodically carries genuine
  unrelated uncommitted WIP (check `git status` fresh each time rather
  than trusting this doc's memory of what's currently uncommitted -
  see the note above about trusting the codebase over this doc) that
  must never get swept into an unrelated commit by accident.
