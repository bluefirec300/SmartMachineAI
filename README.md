# SmartFactoryAI

An AI assistant that monitors factory equipment (cold room, compressor,
chiller, tank/pump, power consumption, and a larger two-plant P01/P02
tag dataset - 625 tags, 76 equipment instances) and answers engineer
questions like *"why is the compressor pressure dropping"* - explaining
likely causes, tracking history, and suggesting troubleshooting steps.
Meant to eventually be embedded into a SCADA interface for operators to
query directly.

**Current stage:** development, simulator-driven. Real PLC drivers
(Modbus/OPC UA/S7/FINS) are built and wired up but have not yet been
pointed at real plant hardware - see `FACTORY_AI_DEVELOPMENT_STATUS.md`
for the phase-by-phase build history and current gaps toward a first
production deployment.

*(The project's own code, package names, database files, and git
history keep the original `SmartMachineAI` name throughout - only the
user-facing product name shown in the web UI was changed.)*

## Architecture

```
Deterministic engines (energy, baseline, anomaly, opportunity,
savings verification, equipment health, maintenance intelligence,
asset performance, data health)
        |
        v
Structured context (ai/context_builder.py)
        |
        v
LLM interpretation (Ollama/qwen2.5:7b locally, or OpenAI)  -- phrasing only
        |
        v
Grounding guard (ai/grounding_guard.py)  -- rejects any AI claim
        |                                    the deterministic data
        v                                    doesn't support
Engineer (Streamlit UI, or CLI)
```

The LLM never computes a value or invents a fact - every number an
engineer sees comes from a deterministic engine first; the AI's only
job is to phrase it conversationally, and a grounding check discards
any AI wording that isn't supported by the underlying data (falling
back to the plain deterministic text instead).

## Major features

- **Ask AI** - natural-language Q&A over live and historical factory
  data, with root-cause analysis, trend/threshold lookups, plant
  comparisons, and manufacturer-documentation-grounded answers.
- **SCADA Floor Plan** - live, flicker-free visual plant map.
- **Energy Dashboard** - demand, energy, cost, and utility-performance
  KPIs per plant, with a P01 vs P02 comparison.
- **Anomaly Detection / Energy Opportunities / Savings Verification** -
  a full pipeline from "this is statistically unusual" through
  "worth investigating" to "did the fix actually work", entirely
  evidence-driven, never a forced or fabricated verdict.
- **Equipment Health / Asset Performance / Data Health** - three
  distinct, deliberately-separate deterministic scores: mechanical
  condition, performance-vs-own-history, and telemetry trustworthiness.
- **Service & Maintenance, Setpoints, Documentation, Factory/Equipment/
  Tag Configuration, User Management, PLC Connectivity** - the
  day-to-day admin and engineering surface.

## Project structure

```text
SmartMachineAI/
├── ai/          AI provider abstraction, context building, grounding guard,
│                interpretation layer (Phase 15+); a small amount of confirmed
│                dead/superseded prototype code still lives here too - see
│                CLAUDE.md's "Confirmed dead code" note before extending it
├── app/         Entry-point scripts - the actual running processes
│                (ask.py, plc_logger.py, event_monitor.py, the *_worker.py
│                background workers)
├── config/      Application configuration, environment switching, user manager
├── database/    SQLite access; simulation/ and actual/ are separate,
│                switchable environments (see config/environment.py)
├── deploy/      systemd unit files for the background workers
├── engine/      Deterministic engines - one module family per domain
│                (energy KPI, baseline, anomaly, opportunity, savings
│                verification, health, performance, data health, etc.)
├── plc/         PLC drivers (Modbus/OPC UA/S7/FINS) and driver_factory.py
├── rag/         Manufacturer-documentation storage/search (SQLite FTS5)
├── simulator/   The live simulator (tag_dataset_model.py, via
│                plc/simulator_driver.py) - generates realistic fault-
│                correlated sensor data for development/testing
├── tests/       ~1360 automated tests
└── ui/          The Streamlit application (Home.py + pages/)
```

## Running it

```bash
# one-time setup
cd ~/SmartMachineAI
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# the web app
streamlit run ui/Home.py --server.headless true --server.port 8501
```

In a real deployment, `plc_logger`, `event_monitor`, `streamlit`, and
the background `*_worker` services all run continuously under systemd
(`deploy/systemd/*.service`, plus `plc_logger.service`,
`event_monitor.service`, and `streamlit.service` - not yet moved into
`deploy/`). All are `Restart=always` and need a manual restart after
any code change:

```bash
sudo systemctl restart plc_logger.service event_monitor.service streamlit.service
```

Ask AI can also be run standalone from the CLI: `python -m app.ask`.

## Database setup

Config and historian databases live under `database/simulation/` and
`database/actual/` - which one is active is controlled by
`config/active_environment.txt` (never hardcode a path directly; use
`config/environment.py`'s `get_config_db_path()`/`get_machine_db_path()`).
A fresh `config.db` needs `engine/tag_dataset_importer.py` run against
`config/master_tag_list.json` to populate equipment/tags, plus the
various `engine/*_migrator.py` scripts (additive, idempotent) to add
each phase's schema.

## More detail

- `CLAUDE.md` - the authoritative, actively-maintained architecture
  and conventions reference for anyone (human or AI) working on this
  codebase.
- `FACTORY_AI_DEVELOPMENT_STATUS.md` - the full phase-by-phase build
  history, what's tested, what's still a known gap, and the current
  roadmap toward a first production (V1) deployment.
- `docs/NEW_FACTORY_SETUP_CHECKLIST.md` - what an engineer needs to
  configure (area/system taxonomy, tariff, engineering thresholds,
  real PLC connection, user accounts) before treating a deployment as
  production-ready.
- `docs/REAL_PLC_CUTOVER_PROCEDURE.md` - the step-by-step, safety-first
  procedure for connecting the very first real PLC/equipment, from
  connection details needed through testing loss/recovery behaviour.
