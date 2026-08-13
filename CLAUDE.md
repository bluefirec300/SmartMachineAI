# SmartMachineAI - Project Context

## What this project is

An AI assistant that monitors factory equipment (cold room, compressor,
chiller, tank/pump, power consumption) and answers engineer questions
like "why is the compressor pressure dropping" - explaining likely
causes, tracking history, and suggesting troubleshooting steps.

- **Current stage:** development, no live PLC yet. Runs on a 4-core/
  16GB Ubuntu VM with **no GPU** (confirmed via `lspci`/`nvidia-smi` -
  only a virtual VMware display adapter is present). Local Ollama
  inference is therefore CPU-only and slow (~0.6-0.8 tokens/sec) - a
  "why" root-cause answer takes ~5-10 minutes on `qwen2.5:7b`. This is
  being accepted deliberately for now; a GPU (or cloud API) upgrade is
  planned once the pipeline design itself is fully validated, not
  before. A simulator generates fake sensor data and alarm events for
  testing.
- **End users:** the factory's engineering department (not general
  operators) - so questions can be reasonably technical.
- **NLP goal:** flexible, casual natural-language questions should
  resolve correctly without a large hardcoded alias/tag list. Now
  includes real typo tolerance (see "NLP / query engine" below), not
  just for equipment names.
- **LLM backend:** switchable between OpenAI (GPT) and Ollama (Qwen)
  via `ai/providers/provider_factory.py`. Currently `qwen2.5:7b`,
  chosen deliberately over `qwen2.5:3b` for more reliable grounding
  while the design is still being validated. Qwen is the intended
  production choice for cost reasons; OpenAI is for dev/testing. Do
  not remove this switchability.
- **End goal:** this Q&A logic gets embedded into a SCADA interface
  for operators to query directly.
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
  `plc_logger.service` systemd unit (fixed and confirmed running as
  of 2026-08-11, see "Background services" below - it used to run as
  a manually-launched, detached process instead). Now throttles
  writes to each tag's configured
  logging cadence (10s/1min/5min/change-only) instead of writing every
  tag every 2s - see "P01 Phase 1 tag migration" below for why.
- `app/event_monitor.py` -> reads `plc_data` -> `RuleEngine` checks
  thresholds (from `database/config.db`) -> `EventEngine` builds
  event dicts -> `EventStore` writes to `machine_events` table. Runs
  via the `event_monitor.service` systemd unit - see "Background
  services" below. **Don't manually launch a second copy** - this bit
  us once already this session (see that section for the full story).
- `engine/industrial_query_engine.py` (+ `app/query_cli_v2.py`) ->
  deterministic question -> tag/equipment/intent resolver. No LLM.
  Now has a designator-code fast path for exact instance codes (e.g.
  "CR01", "AC02") - see "NLP / query engine" below.
- `app/ask.py` -> the orchestrator that ties the deterministic
  engines to the LLM. Handles nine question types now (was five):
  `current_data`, `root_cause`, `trend`, `threshold`, `timeline`,
  `discovery` ("what's available to check"), `equipment_status`
  ("is everything ok", "how is AC01 doing", "is anything broken" - see
  "Common-sense NLP" below), `chitchat_greeting`/`chitchat_thanks`
  ("hi", "thanks" - canned instant replies, see same section), and a
  factory-wide timeline fallback for time-based questions with no
  specific equipment named. Also has stateful numbered-menu
  clarification, which doubles as a general free-text counter-answer
  mechanism (see below). Run it with `python -m app.ask`.
- `engine/tag_dataset_importer.py` - imports the two-plant master tag
  list (`config/master_tag_list.json`, 624 tags) into `config.db`,
  additive/idempotent, safe to re-run with different
  `--enable-plant`/`--enable-phase` filters to phase in more of the
  dataset later. See "P01 Phase 1 tag migration" below.
- `simulator/tag_dataset_model.py` - generic value generator for the
  imported dataset, kept fully separate from `simulator/factory_model.py`
  (the original ~20-tag hand-scripted demo, untouched) so nothing
  about the tested original behavior could be disturbed. Each
  equipment instance gets its own dormant->developing->faulted->
  recovering fault cycle so faults read as one correlated event, not
  independent per-tag noise.
- `engine/equipment_metadata_migrator.py` /
  `engine/seed_equipment_metadata.py` - added `device_number`/`brand`/
  `model`/`service_interval_days`/`last_serviced_at` columns to
  `equipment` and seeded all rows with illustrative (fabricated but
  real-brand) data. See "Equipment metadata & document lookup" below.
- `rag/document_store.py` / `rag/retrieval.py` / `app/import_manual.py`
  - manufacturer-documentation storage and search (SQLite FTS5
  keyword search, not embeddings), wired into `app/ask.py`'s
  `root_cause` path only. See "Equipment metadata & document lookup"
  below.

**Wired together via `app/ask.py`:**
- `ai/root_cause_engine.py` - deterministic root-cause analysis from
  `machine_events` history. Returns structured evidence + probable
  causes.
- `ai/prompt_builder.py` - builds a grounded LLM prompt from
  observations + rule evaluation + root-cause evidence + (for
  `root_cause` only) retrieved documentation excerpts. The
  "possible causes" structure is conditional on `root_cause` intent -
  every other intent gets an "answer using only the confirmed
  observations, don't speculate" instruction instead.
- `ai/ai_provider.py` + `ai/providers/*` - the Qwen/GPT switch layer.

**Confirmed dead code (still not imported anywhere, still safe to
ignore/delete later, still not cleaned up):**
- `ai/ai_bridge.py` + `ai/ai_bridge.py.backup` +
  `ai/ai_bridge.py.backup_before_root_cause`
- `ai/simulator/factory_simulator.py` +
  `ai/simulator/factory_simulator_backup.py` (the *live* simulator is
  `simulator/factory_model.py`, used via `plc/simulator_driver.py`)
- `app/machine_query.py` (earliest prototype, primitive keyword match)
- `ai/understanding/question_understanding.py` (byte-identical
  duplicate of `ai/question_understanding.py`)

**Separate uncommitted WIP - deliberately NOT touched, continued, or
merged with any of this session's work:**
- `engine/industrial_query_engine.py`'s uncommitted "driver
  selection" feature (reads `config/system.json`'s `active_driver` to
  prefer simulator vs. real-PLC tag addresses) - my designator-code
  fix this session was layered on top of this same file/diff since
  there was no way to separate them cleanly (all one uncommitted diff
  against the last commit); committed together deliberately, per
  explicit confirmation, as commit `25062de`.
- `ai/ai_bridge.py` has uncommitted edits wiring in `RootCauseEngine`
  despite the file still being dead/unimported code.
- Related untracked leftovers from that same thread:
  `engine/industrial_query_engine_before_driver_selection.py`,
  `config/system.json`, `config/simulator_config.json`,
  `config/simulator_command.txt`.
- When picking this back up: decide deliberately whether to finish or
  discard it.

## P01 Phase 1 tag migration

User provided a much larger, more realistic two-plant tag dataset
(`Factory_AI_Two_Plant_Simulation.ipynb`, 624 tags, P01+P02, 3
phases) to replace the toy-scale ~20-tag demo. Migrated in phases,
starting with P01 Phase 1 only (162 tags, 76 equipment instances) -
chosen because its equipment (Air Compressor, Chiller, Chilled Water
Pump, Cold Room, Water Supply Pump, electrical incomers) is a more
granular version of what was already modeled, not a scope jump.

- All 624 tag definitions are imported (via
  `engine/tag_dataset_importer.py`, source data in
  `config/master_tag_list.json`), but only the 162 P01 Phase 1 tags
  are `enabled=1`. Re-run the importer with different
  `--enable-plant`/`--enable-phase` to phase in more later - it's
  additive, never disables something already enabled.
- `engine/metadata_migrator.py`'s existing `infer()` auto-derives the
  concept fields (measurement/location/signal_type/event_type/
  threshold_type) from tag name + description - reused as-is, no need
  to hand-classify 162 tags.
- Thresholds are **not** configured for the new tags (no source data
  for them, and fabricating specific alarm/warning setpoints for real-
  sounding equipment felt like the wrong kind of guess to make) - they
  correctly show as `not_evaluated` rather than silently missing.
- `plc_logger.py` had a serious active bug found while testing this:
  5,729 crashes over 8 hours (~one every 5 seconds). Cause: STRING-
  typed tags (`AlarmCode`) can't fit `plc_data.value` (a `REAL`
  column); the crash happened before per-tag bookkeeping updated, so
  it retried forever, and because it aborted mid-cycle, every tag
  alphabetically after the first STRING tag never got a chance to log
  that cycle. **Fixed:** STRING tags are now skipped from the
  historian entirely (the paired boolean Alarm tag + `machine_events`
  already capture the fault), and each tag's save is wrapped in its
  own try/except so one bad tag can never again cascade into blocking
  the rest of the cycle.

**Legacy ~20-tag demo retired (2026-08-09).** Now that the P01 dataset
covers the same equipment in more detail, the original hand-scripted
demo tags (`CompressorTemperature`, `CompressorCurrent`,
`ChillerSupplyTemp`, `ColdRoomTemperature`, `TankLevel`, etc. - 20
tags total, all with `equipment_id IS NULL`, i.e. never linked to a
proper `equipment` row) were deleted from `tags` in `database/config.db`
(backup taken automatically: `database/config_before_legacy_tag_cleanup_*.db`).
This cascade-deleted 11 of the 12 `thresholds` rows that belonged to
them. **`CompressorPressure` was deliberately kept** - it's the one
tag from that original set that *does* have a proper equipment link
(`Air Compressor System`) and a configured threshold, so it stays live
and answers normally. `simulator/factory_model.py` (`FactorySimulator`)
was **not** touched - it still internally computes all 21 original
values including `CompressorPressure`, but since `app/plc_logger.py`
only logs tags present in the enabled config registry, the 20 deleted
tags simply stop being read/logged - no simulator code change needed
or safe to make (removing `FactorySimulator` entirely would have also
killed `CompressorPressure`). `plc_logger` was restarted to pick this
up (it caches its tag list once at startup) - confirmed via its log
output: 163 tags now (was 183), none of the deleted tag names appear.
`event_monitor.service` (systemd, `Restart=always`) was deliberately
**left running** rather than killed/restarted - see "Background
services" below for why manually touching it is risky - its in-memory
`RuleEngine` still holds the 11 removed thresholds until its next
natural restart, but this is harmless: `plc_data` no longer receives
new rows for those tag names, so there's nothing left for it to
evaluate against them.

## NLP / query engine fixes (found via testing at the larger tag scale)

The 162-tag P01 dataset exposed real gaps in `engine/
industrial_query_engine.py` and `engine/concept_extractor.py` that the
original ~20-tag demo never surfaced, since equipment used to be
unambiguous almost by default:

1. **Designator-code matching.** `_equipment_score`'s `SequenceMatcher`
   fuzzy path could let a generic shared word (e.g. "compressor")
   outrank an exact instance code (e.g. "CR01" vs. Air Compressor
   AC01/02/03). Fixed: when a question names an explicit instance code
   (pattern `[a-z]{1,8}\d{1,3}`), an exact alias match for it is now
   required and wins outright; every other query path is byte-
   identical to before (zero regression risk for anything without a
   code in it).
2. **Missing stopwords.** `"why"`, `"trend"`, `"limit"`, `"trip"`
   (later also `"there"`, `"anything"`, `"happen"`, `"wrong"`, `"any"`)
   weren't in `STOPWORDS`, so they leaked into equipment-matching and
   diluted otherwise-exact scores. Harmless with ~20 tags (nothing
   competed); became consequential once real competing tags existed
   to lose ambiguous matches to. All five original flagship questions
   ("what is/why is/trend of/alarm limit/when did the compressor
   pressure...") broke and were re-fixed by this.
3. **Spelling correction.** Added a lightweight typo-correction pass
   (`correct_spelling`/`VOCABULARY_WORDS` in `concept_extractor.py`)
   that runs before any matching - separate from the equipment-name
   fuzzy matching, which only helps identify *which* tag, not
   recognize intent/vocabulary words like "wat is availble" ->
   "what is available". Skips anything with a digit (tag codes must
   never get "corrected"), uses a length-scaled similarity threshold
   (short words need a lower bar for the same one-letter edit).
4. **Genuine remaining ambiguity, not a bug:** "what is the cold room
   temperature" (no instance named) now correctly asks for
   clarification, since there really are two cold rooms (CR01/CR02).
   This is the *intended* effect of a bigger, more realistic dataset.

All fixes verified against the full 95-test regression suite (`python
-m unittest discover tests`) plus a manual sweep of every flagship
question, every time a change was made - this is the safety net that
made touching this code (originally protected by "Milestone 22 - typo
tolerant equipment matching") reasonable to do at all.

## New question types

- **`discovery`** ("what is available for me to check", "list
  equipment/component/tag", also "help") - answered instantly,
  deterministically, no LLM call: groups the enabled tags by equipment
  (numbered instances like Air Compressor 1/2/3 collapse into one
  line) plus example question phrasings.
- **Factory-wide timeline fallback** ("is there anything happen
  yesterday") - when a `timeline` question doesn't resolve to one
  specific tag, queries `machine_events` across everything in the
  requested time window (yesterday/today/last 7 days/last hour/last
  30 min, default last 24h) instead of failing with "no match". Also
  instant/deterministic.
- **Numbered-menu clarification.** When a question is ambiguous or
  unrecognized, `AskEngine` now presents the top candidates as a
  numbered list and remembers them (`self._pending`); the next input
  can just be a number to select one, which then runs through the
  normal answer pipeline. Out-of-range numbers get a retry prompt and
  keep the menu alive; any non-numeric input abandons the stale menu
  and is treated as a fresh question. Deliberately designed as a
  generic `{"kind": "select_candidate", ...}` dict (not narrowly typed
  to candidate-selection) so a future `{"kind": "confirm_write", ...}`
  - e.g. confirming a threshold change before applying it - can reuse
  the same mechanism without a redesign. The write capability itself
  is **not built** - deliberately out of scope, has real safety
  considerations (validation, who's allowed, audit trail) that
  deserve their own design pass. **Confirmed working as a general
  "counter-answer" mechanism, not just numbered-menu selection**: any
  non-numeric reply (e.g. "actually I meant the chiller") already
  abandons the stale menu and runs as a fresh question through the
  normal pipeline - verified directly this session, no code change
  needed. See "Common-sense NLP" below for the follow-on work that
  used this as its foundation.
- **`equipment_status`** ("is everything ok", "how is AC01 doing", "is
  the chiller ok", "any problems with the mixer", "is anything
  broken", "any warnings", "show me alarms") - see "Common-sense NLP +
  equipment_status intent" below.
- **`chitchat_greeting`/`chitchat_thanks`** ("hi", "thanks") - canned
  instant replies for small talk, whole-message exact match only so a
  real question is never swallowed. See "Common-sense NLP" below.

## Equipment metadata & document lookup (RAG)

User wants device info (brand/model/device number) and eventually
manufacturer-documentation lookup during root-cause answers ("dig the
manufacturer-recommended fix out of the manual").

**Schema** (`engine/equipment_metadata_migrator.py`): `equipment`
gained `device_number`, `brand`, `model`, `service_interval_days`,
`last_serviced_at` - nullable, safe additive migration.

**Seed data** (`engine/seed_equipment_metadata.py`): all equipment
rows filled with illustrative (fabricated, but real-brand) data -
sequential `EQ-0001`+ device numbers, one brand/model per equipment
*type* (a real factory standardizes per class, not per unit),
maintenance intervals varying sensibly by class. Idempotent - only
fills genuinely empty rows.

**Document storage/retrieval** (`rag/document_store.py`,
`rag/retrieval.py`, `app/import_manual.py`): `equipment_documents`
(metadata, keyed by brand+model so one manual covers every matching
instance) + `document_chunks_fts` (SQLite FTS5 virtual table).
Deliberately keyword search, not embeddings - `nomic-embed-text` sits
unused in Ollama from an earlier abandoned attempt; FTS5 needs no new
heavy dependencies and is easier to reason about. Wired into
`app/ask.py`'s `root_cause` path only (not threshold/other intents,
deliberately scoped for v1): resolves tag -> equipment -> brand/model
-> searches -> adds excerpts to the prompt **only** when something is
actually found, never an empty "documentation" header.

**Real documentation ingested:** 25 of 28 equipment brand/models now
have real manufacturer documentation (found via web search, verified
genuine via `pypdf` text extraction before ingesting, sourced from
official manufacturer sites - Daikin, Grundfos, Bitzer, Vaisala,
Honeywell, Schneider Electric, Cummins, ABB, Xylem, SMC, Endress+Hauser,
Sensus, and others), replacing the initial synthetic/AI-generated test
content. Stored PDFs live in `manuals/` (gitignored - proprietary
third-party documents, not project source; the actual ingested/
chunked text lives in `database/config.db`, also gitignored).
3 remain synthetic after genuine attempts failed: **Eaton 9395** (site
blocked/timed out from this environment), **Donaldson Torit**
(downloads repeatedly arrived corrupted), **IMA filling line** (no
accessible manual found online at all). A few others are close-family
matches rather than exact SKU matches (Atlas Copco GA30 vs "GA30+",
Cummins "HGLAA" genset vs "C1100D5", Carrier 39CP vs "39M") - same
manufacturer, closely related product, noted honestly in each
document's title field.

**Verified finding - retrieval works, citation fidelity doesn't (yet),
and this is a model limitation, not a documentation one:**
Retrieval quality spot-checked as genuinely good across the real
documents (specific fault codes, troubleshooting sections, alarm/trip
logic all surfaced correctly for on-topic queries). But when tested
end-to-end against real `qwen2.5:7b`, the LLM fabricated a plausible-
sounding, specific-seeming detail ("oil separator, max pressure
difference of 1 bar") **not present anywhere in the actual retrieved
chunks** (checked directly), and explicitly (falsely) attributed it to
"the manufacturer" and "the documentation." This happened identically
against both the earlier synthetic test excerpt and the real 86-chunk
Atlas Copco manual - ruling out "the fake docs weren't good enough" as
the explanation. **Don't keep chasing this with more/better source
documents or more prompt tweaking** - it tracks the same pattern
already observed comparing `qwen2.5:3b` vs `7b` grounding quality;
revisit with a larger model once on better hardware.

## Common-sense NLP + equipment_status intent (2026-08-11)

User said they'd be away until the next day and gave open latitude to
propose improvements, with two concrete asks: (1) make the Ollama
Q&A "more language understand friendly" / accept more common-sense
questions, and (2) confirm the numbered-menu clarification already
supports a free-text counter-answer when none of the options fit -
see the "Confirmed working" note under "New question types" above for
that second part (already built, just verified).

**Vocabulary expansion** (`engine/concept_extractor.py`): added
measurement aliases for every Phase 2/3 signal type not yet covered
(pH, turbidity, conductivity, dissolved oxygen, ORP, dew point,
vibration, battery/state-of-charge, runtime), plus casual phrasing
("how hot", "how full", "how fast") and more `LOCATIONS`/`CONDITIONS`/
`EVENTS`/`STOPWORDS` entries surfaced by testing at that vocabulary's
scale. Three interconnected bugs found and fixed along the way:
- "pH" gets mangled to "p h" by `normalize()`'s camelCase splitter
  (lowercase-then-uppercase boundary) - fixed by adding "p h" as an
  explicit alias.
- Canonical concept keys must never contain underscores -
  `normalize()` also replaces `_` with a space, so an underscored key
  like `dissolved_oxygen` can never equal itself after normalization
  and silently never matches. Fixed by renaming to single-word keys
  (`dissolvedoxygen`, `dewpoint`), matching the existing convention
  every other key already followed.
- `engine/metadata_migrator.py`'s `migrate()` only fills *empty*
  columns, never overwrites a non-empty one - so the first (buggy)
  migrate() run's wrong values had to be corrected with a direct SQL
  UPDATE, re-running migrate() alone wasn't enough.

**New `equipment_status` intent** - broad health-check questions
("is everything ok", "how is AC01 doing", "any problems with the
mixer") that don't name a specific measurement. Detected in
`ConceptExtractor.extract()` via two end-shape checks (a question
starting with "is "/"how is "/"how are " and ending in
ok/okay/fine/healthy/good/well, or containing "how everything"/"any
problem(s) with"/"any issue(s) with"), deliberately placed after the
existing five intents so it can never steal a real
current_data/trend/threshold/timeline/root_cause question.

Handled in `app/ask.py` (new `_evaluate_status()` +
`_format_equipment_status()`/`_format_factory_status()`), instant and
deterministic like `discovery`/the timeline fallback - **no LLM
call**: one `DatabaseManager.get_latest_all()` query for every
enabled tag's latest value, evaluated per-tag through the existing
`RuleEngine.evaluate_summary()`, grouped by severity.
- If the question names a specific equipment confidently enough
  (`_equipment_score() >= EQUIPMENT_STATUS_SCORE_FLOOR`, same 0.4
  floor already used by the factory-total-power fallback), the answer
  covers just that instance and **names which equipment it picked**
  in the first line - if the guess is wrong (e.g. ambiguous between
  CHL01/CHL02), the operator can just say what they meant, reusing
  the counter-answer mechanism above rather than a separate
  equipment-picker menu (deliberate scope decision).
- Otherwise (score below the floor, e.g. "is everything ok" or "any
  problems with the mixer" when no equipment called "mixer" exists),
  the answer covers the whole factory, mirroring the existing
  factory-wide timeline fallback's shape.
- Deliberately honest about missing thresholds rather than reporting
  false confidence: a tag with `condition == "not_evaluated"` (no
  threshold configured) is called out separately, never silently
  folded into "normal".

Verified: full 95-test regression suite (same 5 pre-existing
unrelated failures, no new ones), a manual sweep of ~20 questions
(flagship + new common-sense phrasings) against
`database/simulation/config.db`, and end-to-end `AskEngine.ask()`
calls confirming both the specific-equipment and factory-wide paths
against real simulator data (correctly surfaced a real MCC Room
humidity/temperature warning pair in the factory-wide case).

Restarted `plc_logger`/`event_monitor`/`streamlit` (user confirmed) -
all three picked up the environment switch and this NLP work; live
`plc_logger` journal confirmed logging real cycles again against the
313-tag simulation dataset.

**Follow-on round, same day: chitchat + broader "what's currently
wrong" phrasing.** A wider casual-phrasing sweep against the now-live
services turned up two more real gaps:
- **Small talk fell through to a confusing tag-candidate menu.** "hi",
  "hello", "thanks", "what can you do" etc. had no home, so they hit
  the generic "I couldn't confidently match that" fallback - a bad
  first impression for a chat interface. Fixed with a new
  `chitchat_greeting`/`chitchat_thanks` intent pair
  (`GREETING_PHRASES`/`THANKS_PHRASES` in `concept_extractor.py`),
  checked as a **whole-message exact match only** (not `contains()`),
  so a real question that happens to start with "hi" ("hi, why is the
  compressor tripping") is never swallowed - verified directly.
  Answered via a canned `CHITCHAT_RESPONSES` dict in `app/ask.py`,
  same instant/no-LLM pattern as everything else. "what can you do"/
  "what do you do" were also added as `discovery` triggers (reuses the
  existing equipment listing, no new response needed).
- **"is anything broken"/"any warnings"/"show me alarms" (no time
  word) weren't recognized.** These read as "what's currently wrong"
  - closer to a live `RuleEngine` status check than a
  `machine_events` history query, so they were added to the
  `equipment_status` broad-check phrase list rather than `timeline`'s
  (which already owns the *with-a-time-word* forms: "any alarms right
  now", "is anything wrong right now", "is there anything happen
  yesterday" - deliberately left alone, already correct). One bug
  found doing this: `correct_spelling()` mangles "faulty" down to
  "fault" (already in the vocabulary via `EVENTS`'s "fault" alias),
  so the phrase had to be checked in its post-correction form
  (`"anything fault"`, not `"anything faulty"`) - same class of bug as
  the earlier "problems"->"problem" one, now the second time this
  exact gotcha has bitten a phrase-list addition here.

Verified again: full 95-test suite (same 5 baseline failures), a
combined sweep of every flagship + common-sense + chitchat question
end-to-end through `AskEngine.ask()` against the live simulation
data (which now has a real in-progress CHL02 fault - 5 alarms/3
warnings - making "is anything broken" a genuinely meaningful,
non-trivial check rather than a "0 results" test).

## P02 enabled + instant answers routed through real Ollama (2026-08-11)

Two follow-on requests after noticing "list all equipment" only showed
P01: (1) enable P02 too, and (2) make the "instant, no-LLM" intents
(`discovery`, `equipment_status`, the factory-wide timeline fallback,
`chitchat_greeting`/`chitchat_thanks`) genuinely go through Ollama
instead of returning scripted text, explicitly accepting the much
slower response time since this is still a dev/test platform, not the
eventual SCADA-facing production use.

**Regression suite baseline changed from 5 to 12 failures because of
this - found 2026-08-12, several hours after the fact.** Every
`CLAUDE.md` entry earlier in this doc that says "same 5 baseline
failures" was accurate *at the time it was written* - all still refer
to `test_equipment_knowledge.py`/`test_hybrid_router.py`/
`test_pipeline.py`, still all genuinely dead code (`ai/router.py`/
`ai/equipment_knowledge.py`/`ai/hybrid_router.py` - confirmed zero
imports anywhere in `app/`/`ui/`/`engine/`, or the `ai/*` modules
`app/ask.py` actually uses). What changed: `ai/equipment_knowledge.py`
tries loading equipment from the *live, environment-resolved*
`config.db` before ever falling back to its static JSON file - so
enabling P02 (312 more tags/aliases) changed what this unused
matching algorithm returns, and 7 more of its already-fragile alias-
matching tests started failing (chiller/cold-room aliases, not just
compressor). **12 is now the correct baseline to expect** - don't
"fix" these (matches this doc's standing policy on this dead code all
along), but don't mistake 12 for a new problem either. Run `python -m
unittest discover tests 2>&1 | grep -E "^(FAIL|ERROR)"` (not just the
final count) if verifying this baseline again later - a count alone
can silently drift like this without being noticed, exactly what
happened here.

**P02 enabled.** `python -m engine.tag_dataset_importer --enable-plant
P02` against `database/simulation/config.db` (backup taken first:
`database/simulation/config_before_p02_enable_*.db`) - purely
additive per the importer's existing design, P01 untouched. Now 625
of 625 tags enabled (was 313), 76 equipment instances (was 38).
`engine/metadata_migrator.py`'s concept inference had already been
run against the full 624-tag import earlier (P02 tags already had
`measurement`/`location`/etc. filled in even while disabled), so no
re-run was needed there. `engine/seed_engineering_thresholds.py` *was*
re-run (idempotent, safe) to backfill thresholds for the newly-
eligible P02 tags via the existing `canonical_key()`-based profile
matching (already "P02-ready" by design, per the "Realistic per-tag
simulation" section above) - thresholds went from 187 to 374
configured, same 6 pre-existing no-profile gaps as before (`*.
ValvePosition`, `*.ContainerSize`, `*.TargetWeight` - genuinely not
threshold-worthy signals, not a regression).

**Instant answers can optionally be LLM-phrased - tried, then
reverted.** New `INSTANT_ANSWERS_SKIP_LLM` flag in `app/ask.py` gates
a new `_phrase_with_llm(question, deterministic_text)` method: when
`False`, the already-computed, already-correct deterministic text for
these four intents is wrapped in a prompt asking Ollama to restate it
conversationally ("here are the confirmed facts, do not add/remove/
invent anything, just rephrase naturally") and run through the same
`AIProvider.generate()` used by every other intent, falling back to
the verbatim deterministic text on any AI error (same fallback
discipline as `_answer_for_tag`). Briefly set to `False` and verified
working end-to-end against real `qwen2.5:7b` (~90s for a chitchat
greeting, correctly phrased, no hallucinated facts) - then reverted
back to `True` (the original/current behavior) on reflection: forcing
a multi-minute wait onto an answer that was already fully correct
didn't actually add anything. Kept as a flag rather than removed, in
case this is worth revisiting differently later. `current_data`/
`root_cause`/`trend`/`threshold`/timeline-for-one-tag were never
affected either way - those already always go through real Ollama for
phrasing, regardless of this flag.

Verified (both states tested): full 95-test suite (same 5 baseline
failures throughout), a mocked-provider sweep confirming all four
intents correctly route through `_phrase_with_llm` with the
deterministic facts intact verbatim inside the prompt when the flag
is `False`, and - after reverting to `True` - a mocked-provider sweep
confirming zero LLM calls are made for these four intents and the
instant deterministic text is returned unchanged.

## Plant-qualifier disambiguation + LLM-assisted typo-correction
fallback (2026-08-11)

Two more gaps found testing broad status questions against the
now-live services, both fixed the same day P02 was enabled.

**Bug: broad "has alarm" phrasing wasn't recognized, and a much
bigger, systemic vocabulary bug was hiding behind it.** "is any
equipment has alarm now?" and "tell me if anything has alarm or
warning now" both fell through to the generic tag-candidate menu
instead of `equipment_status`. Two separate causes:
1. That exact phrase shape ("alarm"/"warning" + generic "any"/
   "anything"/"equipment" framing) wasn't covered by the fixed
   `contains()` phrase list - fixed by computing `event_type` early
   (same pattern as `measurement`, see the comment above it in
   `concept_extractor.py`) and adding a broader condition: EVENTS word
   present + generic framing word present -> `equipment_status`. A
   specific request like "what is the AC01 alarm code" has neither
   framing word, so it's untouched and still resolves as
   `current_data`.
2. The second question's "or" was silently getting typo-corrected to
   "orp" (a chemistry measurement alias added earlier this session) -
   `_correct_word()`'s short-word similarity threshold is loose enough
   that "or" scores a 0.8 ratio against "orp", well past the 0.72 bar
   for a 2-letter word. **A broader scan turned up 10 more real
   words with the same problem** ("and"->"an", "no"->"now"/"not",
   "then"->"the", "than"->"thank", "over"->"very", "here"->"where",
   "such"->"much", "own"->"on", "even"->"been", "ever"->"very") - and
   because short-word ties are broken by set-iteration order, which
   set of collisions actually manifests can vary between separate
   Python process runs (`PYTHONHASHSEED`), not just between which
   words happen to be in a given question. Fixed by adding a broad,
   defensive batch of common English connector/preposition/conjunction
   words to `STOPWORDS` (protected from correction via the
   `word in VOCABULARY_WORDS` early-return `_correct_word()` already
   had) rather than only patching back whichever word the next real
   question happens to break.

**Found while fixing the above: P02 broke the designator-code fast
path system-wide.** P01 and P02 reuse identical equipment numbering
(both have AC01/AC02/AC03, CR01/CR02, ...), so almost every precise,
instance-coded question ("what is the AC01 pressure", "trend of CR01
temperature") became ambiguous between plants the moment P02 was
enabled - a much bigger regression than it first looked, since the
designator-code fast path (see "NLP / query engine fixes" above) was
specifically built to make exactly these questions resolve instantly.
User's explicit direction: teach it to understand plant qualifiers,
generically - not hardcoded to exactly 2 plants, since a future
P03/P04 shouldn't need code changes here.

- New `plant` field on `Concepts` (`engine/models.py`) and
  `extract_plant()`/`PLANT_PATTERN` in `concept_extractor.py` - matches
  "P01"/"p2"/"plant 2"/"plant02" as a bounded token/phrase (never
  "pressure", never an equipment designator like "ac01"), canonicalized
  to zero-padded `"p01"`/`"p02"`/... form. Stripped from
  `equipment_terms` the same way every other matched concept alias
  already is.
- New `_break_plant_ties()` in `industrial_query_engine.py`, called
  right before `query()`'s existing ambiguity check: when the top-
  scoring candidates are tied ONLY because of a same-instance-code
  match on more than one plant (verified via a fresh
  `_equipment_score() == 1.0` check per candidate, not just comparing
  final blended scores - those can differ by a point or two from
  incidental lexical-overlap noise even for a genuine plant-duplicate
  tie, e.g. a P02 tag's own name literally containing "p02" nudges its
  score up slightly), it drops the non-preferred plant's duplicate(s)
  from consideration entirely rather than re-scoring or reordering
  anything. Prefers an explicitly-named plant (`concepts.plant`),
  otherwise the lexicographically-first plant among the tied
  candidates (`min(plants)` - "p01" before "p02" before "p03", ...,
  scales to any count without change). A real difference between two
  different pieces of equipment is left completely untouched, still
  correctly ambiguous.
- Deliberately narrow and conservative: only ever removes exact-
  designator-match duplicates, never touches the generic fuzzy-match
  path (so "what is the compressor pressure" - no instance code, 6-way
  tie among real different instances - correctly still asks for
  clarification, same as the CR01/CR02-without-a-code precedent
  already documented above).

**New: LLM-assisted typo-correction, but only as a fallback.** User
proposed letting Ollama fix typos before routing, specifically noting
that a short "just give me the corrected question back" response
should be far cheaper on this hardware than a full answer (generation
time is bound by *output* length at ~0.6-0.8 tokens/sec, and a
corrected question is a handful of words vs. a paragraph). Scoped
per explicit confirmation to fallback-only (not run on every
question): `app/ask.py`'s `ask()` was refactored so the whole
intent-dispatch chain (chitchat/discovery/equipment_status/timeline-
fallback/power-fallback/resolved-tag) now lives in a new
`_resolve_and_answer(result, question)` helper returning `None`
instead of building the candidate menu directly when nothing
resolves. `ask()` calls it once; if it's `None`, `_try_correct_question()`
sends just the original question to Ollama with a short, output-only-
the-question prompt, and the corrected text is re-run through
`_resolve_and_answer()` exactly once before falling back to the
numbered menu (using whichever result's candidates are better).
Never trusted as an answer itself - only ever used to retry matching
through the same deterministic engine, so it can't introduce an
ungrounded claim.

Verified (mocked-provider sweep distinguishing correction vs.
phrasing prompts, since a naive mock returning the same string for
every call was initially misleading): already-resolving questions
make zero LLM calls (unaffected, exactly as fast as before);
genuinely ambiguous questions make exactly one correction attempt,
and when the correction changes nothing (no real typo), fall back to
the original, uncorrupted candidate menu. Full 95-test suite clean
throughout (same 5 baseline failures) across every step of this
round - the STOPWORDS fix, the event_type/equipment_status broadening,
and the plant tie-break change all verified independently before
being combined.

## "Yesterday" dropped from alarm questions + one-question follow-up
memory (2026-08-11)

User asked "is there any alarm or warning yesterday" (correctly
answered, but with *current* status, not yesterday's history - the
word "yesterday" was silently ignored) then "how about in the pass
few days?" (landed on a nonsense equipment-name menu) - and asked,
reasonably, why this doesn't "talk like a real AI" the way this
session's own chat does.

**Bug: event_type + time_expression combinations picked the wrong
intent.** "is there any alarm or warning yesterday" has event_type=
"alarm" and time_expression="yesterday", both correctly extracted -
but the equipment_status broadening added earlier this session (event
word + generic framing word -> equipment_status) fired first, since
`time_expression` was computed *after* the intent-classification
chain, too late to matter. Fixed the same way `measurement`/
`event_type` were fixed earlier: moved `time_expression` computation
early too, and added a condition to the *existing* `timeline` branch
(checked earlier in the chain, so it wins): an EVENTS word paired with
a genuine **past** time range routes to `timeline`, not
`equipment_status`. New `HISTORICAL_TIME_EXPRESSIONS` set
deliberately excludes "now"/"latest" - "what is the current alarm
status" must stay a live-state question, not get pulled into a
machine_events history query just because "current" is technically a
TIMES alias.

**The deeper question: no conversation memory at all, unlike this
chat.** `AskEngine.ask()` re-parses every question from scratch, with
zero awareness of what was just discussed - fundamentally different
from an LLM conversation (this one included) where every prior
message is automatically in view. Given explicit confirmation to
build a scoped fix (not general conversation history - a single-turn,
fallback-only rescue): `AskEngine` now tracks `self._last_question`
(the previous *resolved* question - skips chitchat, so a stray "thanks"
in between doesn't erase real topic context), and `_try_correct_question()`
(the typo-correction fallback added earlier this session) now also
receives it as optional context. When the fast matcher fails on a
question that reads as a bare follow-up ("how about the past few
days?", "and the chiller?" - no topic of its own), the same short
Ollama call that used to only fix typos now also rewrites it into a
standalone question using the previous one as context, before
retrying the deterministic match. Same safety property as the typo
fix: the LLM only ever rewrites the *question* text, never invents an
answer.

Deliberately narrow, and explicitly explained to the user as such:
this is not real conversational memory - it only rescues the specific
"fast matcher already failed, and the question looks like it's
referencing the previous one" case, not open-ended multi-turn
reasoning. A follow-up that happens to accidentally match some
unrelated tag on its own won't trigger this rescue path.

Verified: full 95-test suite (same 5 baseline failures) after each of
the two fixes independently and combined; a mocked-provider sweep
confirming the follow-up correctly resolves ("how about in the pass
few days?" after an alarm/warning question now returns a real
last-7-days events summary, not the tag-name menu); a real end-to-end
run against live `qwen2.5:7b` reproducing the user's exact two-turn
conversation.

**Follow-on, same day: single-question memory extended to a 5-turn
rolling history.** User asked, directly, why this can't "talk like a
real AI" the way this session's own chat does - a fair question,
answered honestly rather than just patched: an LLM conversation (this
one included) works by re-sending the *entire* transcript as context
on every turn, which is fundamentally different from how `AskEngine`
is built (a deterministic engine resolves facts; the LLM is only ever
shown already-verified facts to phrase, specifically so nothing in an
answer is invented). Explained that closing that gap for real is
possible without abandoning that grounding guarantee - give the
fallback-only correction step a short rolling history instead of just
one prior question - and confirmed scope before building, including
the user's forward-looking ask about migrating to a real device later
with unbounded history.

- `AskEngine.__init__`'s `self._last_question: str` became
  `self._history: list[str]`, capped to `HISTORY_TURNS_KEPT` (5) via
  a new `_remember()` helper. Deliberately shaped as a plain list from
  the start specifically so raising/removing the cap, storing answers
  alongside questions, or using it for more than just the fallback
  path later are all small, contained changes - not a redesign - for
  exactly the "make this a real running history once it's on real
  hardware" migration the user asked about.
- `_try_correct_question()` now takes `history: list[str]` instead of
  a single `previous_question: str`, rendered into the prompt as a
  numbered "recent conversation so far" list. Still fallback-only
  (never runs on a question that already resolves) and still only
  ever rewrites the *question* text, never the answer.

Verified: full 95-test suite (same 5 baseline failures); a 3-turn
mocked-provider sweep confirming a question referencing *two* turns
back ("what about the day before that?", after a "past few days"
turn that itself followed a "yesterday" turn) correctly resolves
using the accumulated history, not just the immediately-prior turn.

**The first real end-to-end test against live `qwen2.5:7b` failed -
caught precisely because it was checked against the real model
instead of trusting the mock.** "how about in the pass few days?"
(after "is there any alarm or warning yesterday") landed back on the
tag-name candidate menu. Traced by capturing the model's actual
output: it only fixed the "pass"->"past" typo and otherwise ignored
the "use history to rewrite as standalone" half of the instruction -
`_try_correct_question()`'s original single-paragraph prompt gave the
model too much latitude to take the easy path (typo-fix only) instead
of doing the harder "recognize this has no subject of its own, carry
one over" reasoning.

**Fixed by restructuring the history-aware prompt as an explicit
step-by-step instruction** (see the docstring/prompt in
`_try_correct_question()`): Step 1 forces an explicit check - does
the new message name its own subject at all? - before Step 2 allows a
context-carrying rewrite, rather than leaving that judgment implicit
in one paragraph. Re-tested the exact same failing case in isolation
first (confirmed the new prompt alone produces "How about any alarms
or warnings in the past few days?" - correctly carrying over
"alarms"/"warnings"), *then* wired it into the real code and
re-verified: full 95-test suite (same 5 baseline failures) plus a
real 3-turn `qwen2.5:7b` conversation reproducing the exact
previously-failing sequence - all three turns now correct, including
the topic-switch turn (chiller) that must NOT get hijacked by the
alarm/warning history.

**Don't revert to the simpler single-paragraph prompt without
re-verifying against the real model** - it's the kind of failure a
mocked test won't catch (the mock happily "worked" with the broken
version), which is exactly why this was checked live before being
declared done, consistent with this project's established discipline
("check the model's specific claims against the real underlying
data/evidence" - see "Equipment metadata & document lookup" above for
the first time this same discipline caught a real gap).

## Proactive common-sense sweep + systemic typo-correction fix
(2026-08-11)

After the user set the hardware question aside and asked to "go
further of the project," continued the same test-and-fix loop that
found every real bug earlier this session, but proactively this time
(not waiting for another screenshot): swept ~25 realistic casual
phrasings across every intent. Found and fixed several real gaps in
`concept_extractor.py`:
- `root_cause`: "why does" wasn't a trigger (only "why did"/"why
  is"), and "what's wrong with"/"what's causing" weren't recognized
  at all.
- `threshold`: "what triggers an alarm", "at what point does it
  alarm", "safe range"/"normal range" weren't recognized - these
  were falling through to `current_data` and hitting the tag-name
  menu instead of a real limit answer.
- `equipment_status`: short "everything good?"/"all good on the
  factory floor?"/"any equipment down"/"is anything offline" weren't
  recognized. The existing end-anchor check only matched a fixed
  "ok/okay/fine/healthy/good/well" set and only after "is " - widened
  the prefix set (also "any "/"anything "/"all "/"everything ") and
  the state-word set (also "smoothly"/"up"/"online"/"down"/"offline"/
  "broken" - positive and negative words both included on purpose,
  since this only has to recognize the *shape* of a status question,
  not judge the answer). Added a handful of contains()-based phrases
  ("all good", "everything ok", ...) for cases where the state word
  isn't the literal last word ("all good on the factory floor?").

**Found the same recurring bug class two more times while wiring
these in** ("whats" always corrects to "what", breaking any trigger
phrase literally containing "whats"; "range" was getting
mis-corrected to "strange") - both patched immediately (STOPWORDS
addition for "range"; rewrote the root_cause phrases in their actual
post-correction form). But given this is now the *fourth* distinct
discovery of "a real English word coincidentally resembles something
in the vocabulary and gets silently rewritten" in one session (`or`/
`orp`, `down`/`own`, `idle`/`side`, `range`/`strange`, and more),
whack-a-mole patching stopped being the right level to fix this at.

**Systemic fix: added an edit-distance guard to `_correct_word()`.**
A genuine typo is almost always a single character away from the
intended word ("wat"/"what", "availble"/"available", "pass"/"past"
are all edit-distance 1). `SequenceMatcher.ratio()` alone doesn't
guarantee that - it can also rate two different, both-correctly-
spelled real words as "close enough" ("range"/"strange" clears the
ratio bar at 0.83 but is edit-distance 2). New `_edit_distance()`
(plain Levenshtein) now gates every correction: `ratio` must clear
the existing threshold *and* the edit distance must be `<=1`, or the
word is left alone. This is a generic fix for the whole bug class
going forward, not one more reactive word added to STOPWORDS -
though the STOPWORDS entries already added stay (harmless, and still
load-bearing for a couple of edit-distance-1 cases like `or`/`orp`
that the new guard alone wouldn't catch).

Verified carefully, since this touches the correction path used by
literally every question: full 95-test suite (same 5 baseline
failures); every documented legitimate correction from this session
re-tested directly against `_correct_word()` ("wat"->"what",
"wy"->"why", "availble"->"available", "pass"->"past",
"problems"->"problem", "faulty"->"fault" - all still correct); every
previously-found false positive re-tested to confirm it's still
inert ("or", "and", "no", "then", "than", "over", "here", "such",
"own", "even", "ever", "down", "idle", "range" - all still
unchanged); a combined sweep of every flagship/common-sense question
tested anywhere in this whole session's work, run together in one
pass, all still classifying correctly.

Also confirmed "comprssor"->"compressor"-style equipment-*name*
typo tolerance is a completely separate mechanism
(`industrial_query_engine.py`'s own fuzzy equipment matching, per the
existing comment on `VOCABULARY_WORDS`) and was never affected by
`_correct_word()` at all - re-verified end-to-end
(`engine.query("what is the comprssor pressure")`) after initially
mis-testing this against the wrong function.

## Three user-requested fixes: event_monitor's stale-database bug,
in-browser document viewing, logical start counters (2026-08-11)

**1. Event Records "factory" bug - user said it was still happening,
turned out to be a much bigger bug than a display issue.** Investigated
and found `event_monitor.service` had literally never been updated to
use `config/environment.py`'s environment-aware paths when that system
was introduced - `app/event_monitor.py` had its own hardcoded
`database/machine_data.db`/`database/config.db` constants (the
pre-environment-abstraction paths), unlike `plc_logger.py`/`app/ask.py`/
`ui/data_access.py`, which all correctly resolve through
`get_config_db_path()`/`get_machine_db_path()`. Confirmed via the
service's own journal: "events stored this cycle: 0" for 20+
consecutive minutes, and `database/simulation/machine_data.db`'s
`machine_events` table hadn't received a single new row since
"2026-08-10 18:18:16" - a full day of this session's work - while a
*separate*, orphaned `database/machine_data.db` (the legacy path)
kept quietly growing instead. The historical "factory" rows the user
saw were pre-existing legacy rows from before the equipment-mapping
fix that never got diluted/replaced, because no fresh, correctly-
labeled events had been added in over a day.

Fixed: `app/event_monitor.py` now imports `get_config_db_path`/
`get_machine_db_path` the same way `plc_logger.py` does - one-line
fix once found. Also backfilled all 3,249 stale "factory" rows in
`database/simulation/machine_data.db` in place, using the exact same
tag->equipment join `EventEngine` uses live, after taking a backup
(`database/simulation/machine_data_before_factory_backfill_*.db`) -
zero rows left unmapped, verified event_monitor's own regression
suite unaffected. **Needs the usual service restart to actually start
writing fresh events again** - until then, event_monitor is still
running against the wrong database even with the code fixed.

**2. Documents now viewable in-browser (new tab), not just
downloadable.** New `ui/static` -> `../manuals` symlink (untracked,
not meant to be committed - a deploy-time artifact, recreate if the
repo is ever cloned fresh) plus `.streamlit/config.toml`
(`enableStaticServing = true`, also untracked/new) makes every stored
document reachable at a plain URL through Streamlit's own static file
server. `ui/pages/8_Documentation.py` now renders a "👁 View" link
next to each "⬇" download button - a plain `<a target="_blank">`, not
a Streamlit widget, so it opens PDFs/images in the browser's native
viewer without a script rerun. `_static_view_url()` derives the URL
from the document's existing PROJECT_ROOT-relative `file_path` (just
swaps the "manuals/" prefix for "/app/static/", URL-encoding the
rest) - no migration needed, every existing document (uploaded or
pre-ingested) works immediately.

Verified two ways before trusting the documented behavior: (1)
`streamlit.testing.v1.AppTest` confirms the page still renders with
no exception with a simulated logged-in session: (2) spun up a
genuinely separate, throwaway `streamlit run` process on port 8599
(not the real service) and `curl`'d a real manual's static URL
directly - HTTP 200, correct `application/pdf` content-type, exact
byte-for-byte match with the file on disk - specifically because
research surfaced real historical uncertainty about whether Streamlit's
static server follows symlinks in every version, and this needed to be
confirmed against this exact installed version (1.61.1) rather than
assumed from documentation alone. Process cleaned up after.

**Known trade-off, disclosed rather than silently accepted:** the
static file server is a separate code path from this app's `ui/auth.py`
`require_login()` gate - anyone with a document's URL can view it
without logging in, bypassing RBAC entirely for that one file. Judged
acceptable for now (equipment documentation, not sensitive process
data, and the app is presumably on an internal factory network), but
worth remembering if document sensitivity or network exposure changes
later.

**3. Simulator start counters now track their own running-status BOOL
instead of being decoupled from it.** User asked for BOOL tags to
toggle "logically" ("cold room door open", "compressor run count").
Investigation found DoorStatus-style generic status bits *already*
toggle randomly and reasonably (confirmed directly against real
historian data - `P01.COLDROOM.CR01.DoorStatus` flipped several times
over a few hours of real logged history) - that part was already
working, just not obviously so without checking the data. But
"StartCount"/"...Starts" tags (24 INT tags total) were a genuine gap:
they incremented on fault-onset or independent random noise, with zero
actual relationship to the paired running-status BOOL every single one
of them has (`RunStatus`/`CompressorStatus`/`JockeyPumpStatus`,
confirmed for all 22 `StartCount` tags plus `JockeyPumpStarts`) - so a
"start count" bore no real relationship to how many times the
equipment had actually started.

Fixed in `simulator/tag_dataset_model.py`: new
`self._running_tag_by_instance` map (built once, at construction) plus
a restructured `update_values()` that now runs REAL/BOOL tags in one
pass before INT/STRING tags in a second, so `_update_int()` can safely
compare a sibling running-tag's before/after value for the *same*
cycle regardless of which tag name happens to sort first
alphabetically - previously everything ran in one single pass in
`tag_name` order. `_update_int()` now increments a start counter only
on a genuine 0->1 transition of its paired running tag; every other
INT tag (production `GoodCount`/`RejectCount`, or a hypothetical
start-style counter with no paired running tag) keeps the original
fault-linked/random-noise behavior unchanged.

Verified against a throwaway copy of `database/simulation/config.db`
(never the live one): an unseeded run initially looked like a 9-vs-8
mismatch between transitions and increments over 3,000 cycles - traced
to an off-by-one bug in the *test script itself* (couldn't detect an
increment landing on the very first cycle), not the implementation;
re-verified with a corrected counting method and separately with a
seeded, reproducible 6,000-cycle run - both showed a perfect 1:1 match
between running-tag transitions and StartCount increments, zero
mismatches. Also confirmed `GoodCount` still increments via the old
logic (unaffected) and a running-status BOOL still spends the
overwhelming majority of cycles "on" (~100% over a 2,000-cycle sample,
as expected). Full 95-test suite clean throughout (same 5 baseline
failures). **Needs `plc_logger.service` restarted** to pick this up -
it's the process holding the live `TagDatasetSimulator` instance.

## Documentation "View" button was actually a pre-existing data gap,
not a bug in the new feature (2026-08-11)

User reported no View/Download button appeared for manuals/images at
all. Investigation found `file_path` was **NULL for all 28 rows** in
`equipment_documents` - meaning the download button (which existed
before today's View feature) had *always* been silently broken for
every pre-ingested manual too, just never noticed/reported before now.

Root cause: the real manuals were ingested into two different places
that were never reconciled. `manuals/` (repo root) holds an earlier
set of files named `Brand_Model_Type.pdf` - 3 of these are the
deliberately-synthetic placeholders documented above (Eaton, Donaldson,
IMA - genuine sourcing failed for these), but most of the rest are
**leftover synthetic ReportLab-generated placeholders** from before the
real manuals were found (confirmed by extracting one: literally
"ReportLab Generated PDF document (opensource)", ~6-7KB). The actual
real manuals - some over 10MB - live in a separate `manuals/real/`
subdirectory, named after their original download filename (matching
the `source_filename` DB column, e.g. `Bitzer_KB-100-2_manual_REAL.pdf`)
- `file_path` was simply never populated to point at either location.

**First fix attempt was wrong and caught before being applied** - fuzzy
brand/model matching against `manuals/` root alone confidently (score
1.0) matched 24 of 28 rows to the *synthetic placeholder* files instead
of the real ones, because both share the same brand name in their
filename. Caught by checking file sizes/content before trusting the
match, which led to finding `manuals/real/`. Correct fix: exact
`source_filename` lookup against `manuals/<name>` first, then
`manuals/real/<name>` - both locations are legitimate (4 documents,
including the 3 intentional synthetic ones, correctly live directly in
`manuals/`; the other 24 real ones live in `manuals/real/`, including 2
Grundfos rows - CR15-2 and CR32-2 - that deliberately share one manual).
All 28 resolved with exact-filename confidence, zero guessed.

Backed up first (`database/simulation/config_before_document_filepath
_backfill_*.db`), then backfilled `file_path` (and `file_type='pdf'`,
also previously NULL for all 28) directly via `UPDATE
equipment_documents`. Verified against the real *running* service (not
just a throwaway test this time, since the code path itself was already
proven separately): `curl`'d the Bitzer manual's live static URL through
the actual `streamlit.service` on port 8501 - HTTP 200, correct
`application/pdf` content-type, and the byte count matched the real
11.67MB file on disk exactly. **No service restart needed for this
one** - `ui/pages/8_Documentation.py` queries `equipment_documents`
fresh on every page load, so the fix is live immediately; just refresh
the Documentation page in the browser.

## "N days ago" follow-ups were silently collapsing to "yesterday"
(2026-08-11)

User chained "is there any alarm yesterday" -> "how about one day
before" -> "how about 2 day before" and got the *identical* "yesterday"
answer all three times. Traced with a real end-to-end test: the LLM
follow-up-rewrite step (added earlier this session) was actually
working perfectly - it correctly expanded "how about one day before"
into "How about there was any alarm one day before yesterday?" and
"how about 2 day before" into "...two days before yesterday?". The bug
was entirely downstream: `TIMES` is a fixed set of named buckets
(yesterday/today/last_7_days/...), matched by plain substring
`contains()` - so "one day before **yesterday**" matches the bare
"yesterday" bucket via substring containment and the "one day before"
qualifier in front is silently discarded. There was no way to express
an arbitrary single day N days back at all.

Fixed with a genuinely new mechanism rather than another fixed bucket:
new `extract_days_ago()`/`DAYS_AGO_PATTERN`/`DAYS_BEFORE_YESTERDAY_
PATTERN` in `concept_extractor.py` produce a dynamic `"days_ago:N"`
time_expression (not a fixed enum value), overriding the plain
"yesterday" bucket match when a more specific phrase is present.
Handles both `"N days ago"`/`"N days before today"` and `"N days
before yesterday"` (which is N+1 days back, since yesterday is already
1 day back) plus the bare idiom "day before yesterday" (=2 days back,
no number needed), in both digit (`"3"`) and spelled-out (`"three"`)
form. `app/ask.py`'s `_factory_timeline_range()` now handles
`"days_ago:N"` by computing that single specific calendar day's
midnight-to-midnight window - scales to any N, no fixed-bucket
enum to keep extending as new phrasings turn up.

**Found the exact same recurring typo-correction bug class a fifth
time while building this - now systematically, not word by word.**
Scanned all ten spelled-out number words up front instead of waiting
to be bitten by each one individually: **8 of 10 were vulnerable**
("one"->"on", "two"->"to", "four"->"for", "five"->"give", "seven"->
"even", "eight"->"weight", "nine"->"fine", "ten"->"then" - only
"three"/"six" happened to survive). This is exactly why "two days
before yesterday" initially still computed the wrong answer
(`days_ago:2` instead of `3`) even after the extraction logic itself
was correct - "two" had already become "to" before the regex ever
saw it, so no number was captured and it defaulted to N=1. Fixed by
adding the complete set of number words to `STOPWORDS` defensively,
matching the standing policy from the connector-word batch earlier
this session: protect the whole class up front once a pattern like
this is found, not one word at a time as each is discovered live.

Verified: full 95-test suite (same 5 baseline failures); every
phrasing variant re-tested directly against `ConceptExtractor` after
the number-word fix (correctly produces `days_ago:2`/`days_ago:3` for
both digit and word forms, digit form was never affected); a real
end-to-end 3-turn run against live `qwen2.5:7b` reproducing the user's
exact original sequence.

## Threshold checkbox alignment + alarm/warning messages now cite the
actual limit breached (2026-08-12)

**1. Threshold-monitoring checkbox row was jagged.** On both the "new
tag" and "edit tag" forms in `ui/pages/11_Equipment_and_Tag_
Configuration.py`, the four Low Alarm/Low Warning/High Warning/High
Alarm checkboxes+inputs sat in `st.columns(4)` - four equal, fairly
narrow columns. "High Warning" (12 characters, one more than the
others) wrapped to two lines in that width while its siblings didn't,
pushing that one column's number_input down and making the row look
misaligned. Fixed by switching to two rows of `st.columns(2)` instead
of one row of `st.columns(4)` - wider columns give every label room
to stay on one line. Same fix applied to both forms (identical
duplicated code in each). Verified via `AppTest` (renders with no
exception) - no live browser available to visually confirm the wrap
is gone, so worth a look next time the page is open.

**2. Alarm/warning messages didn't say what threshold was actually
breached.** `RuleEngine.evaluate_summary()`'s messages (e.g.
"AC02.Pressure is critically high at 115 bar.") only ever stated the
*current* value, never the configured limit that made it abnormal -
true for all four conditions (low_alarm/high_alarm/low_warning/
high_warning), and this exact message is what both Event Records
displays *and* what `app/ask.py` feeds the LLM as a "confirmed fact"
for phrasing - so both got the improvement, not just Event Records.
Now every message includes the specific limit breached, e.g. "...is
critically high at 110.26 °C (high alarm limit: 108 °C)." - reuses
the existing `_value_text()` helper for the threshold number too, so
units stay consistent. Verified against real live data for all three
of alarm/high-warning/low-warning at once (low_alarm mirrors
high_alarm's code exactly, so wasn't separately re-verified against a
live example - same pattern, same confidence). Full 95-test suite
clean (same 5 baseline failures) - no test asserts the old exact
message text, confirmed before changing it.

**Deliberately not backfilled to old events** - the ~11,000+ existing
rows in `machine_events` keep their old plain messages. Recomputing
"what threshold was breached" for historical rows would need whatever
threshold was configured *at the time*, not today's - and several
thresholds were re-seeded today (P02 enable), so a naive backfill
using current values could misrepresent old events. Only new events
from here on get the enriched message.

## OPC UA "Browse" picker for Tag Mapping (2026-08-12)

User testing OPC UA connectivity asked for a way to pick a tag address
from the live server's own address space instead of typing the exact
NodeId by hand. Confirmed scope first (per-tag "Browse" button opening
a searchable list, not a full expandable tree browser - simpler to
build, adequate for a moderate-sized address space).

**`plc/opcua_driver.py`'s new `browse_nodes(max_depth=6, max_nodes=
500)`** walks the server from the `Objects` node using `asyncua`'s
`get_children_descriptions()` (one Browse-service round trip per
level, returning name/class/NodeId together - meaningfully fewer round
trips than reading each attribute separately per child), recursing
through Object nodes and collecting every Variable node found as
`(display_path, node_id_string)`. Depth/count-capped so a very large
server's address space can't hang the page - an incomplete result
isn't an error, just "the first N found." Exact `asyncua` sync API
(`get_children_descriptions`, `.NodeId`/`.DisplayName`/`.NodeClass_`
on `ReferenceDescription`, `ExpandedNodeId.to_string()` producing the
same `"ns=X;i=Y"` format the driver already expects) verified directly
against the installed `asyncua==1.1.8` via introspection before
writing any code, not assumed from memory/docs.

**UI** (`ui/pages/10_PLC_Connectivity.py`): a "🔍 Browse" button next to
each tag's address box (OPC UA connections only) opens an
`st.dialog` that connects, browses once (cached in session per
connection, with a manual "Refresh" to re-browse), and shows a
type-to-filter searchable list; picking a node fills that tag's
address box. Had to restructure the OPC UA branch of Tag Mapping to
NOT use `st.form` (unlike the unchanged FINS/Modbus branch) - a
form-batched `st.button` can't trigger a dialog immediately, only the
form's own submit button reacts right away. Each address box's own
`key` already persists its value across reruns, so a manual "Save
mapping" button reading straight from `st.session_state` stands in
for the form's batching instead - functionally equivalent, just not
form-wrapped.

**Verified what could be verified without a real OPC UA server**:
`browse_nodes()`'s recursion/depth-cap/count-cap/NodeId-formatting all
directly unit-tested against a mocked node tree built from real
`asyncua.ua` types (not a bare `MagicMock` guess) - correct nested
paths, correct truncation at both caps, confirmed via call-count
assertions. Page compiles, imports cleanly, and renders via `AppTest`
with no exception. **Not verified**: the actual live "connect to a
real server and browse" flow end-to-end - no real OPC UA server
available in this environment. Worth the user's own test against
their real/test server before trusting this fully; if `asyncua`'s
`get_children_descriptions()` behaves differently against some real
server's specific implementation quirks than the mocked test assumes,
that would only surface there.

## OPC UA browse bug: real-server boilerplate was burying real tags
(2026-08-12)

User is testing the OPC UA connection against a separate project,
`/home/test/opcua-web-simulator` (also Claude-Code-built, a
Streamlit-based OPC UA server simulator - control UI on port 8502,
actual OPC UA server on port 4840, endpoint path `/opcua/simulator`,
3 test tags `test1`/`test2`/`test3`, anonymous auth allowed). Reported
the connection "not working" together with SmartMachineAI.

**Diagnosed by reproducing every step of the real connection flow
directly, not guessing:** raw `asyncua` connect, the app's own
`_test_connection()` logic, and a plain tag `read()` all worked
correctly against the real running simulator - the actual live
`plc_connections` row does use `opc.tcp://0.0.0.0:4840/...` as its
host, which is technically a *bind* address, not something a client
should connect to - but on this VM (server and client on the same
host) it happens to work via loopback. **Worth using `localhost` or
`127.0.0.1` there instead for portability** (would matter if the
simulator ever moves to a separate machine/container) even though
it's not currently broken.

**The real bug: the OPC UA "Browse" feature (built earlier this
session, never tested against a real server until now) returned 232
nodes for a server with exactly 3 real tags.** 229 of them were
standard OPC Foundation server-metadata nodes
(`ServerCapabilities`/`PublishSubscribe`/diagnostics/... - identical
boilerplate every OPC UA server exposes), burying the 3 actual tags
at the very end of the list and eating most of the `max_nodes` budget
on nothing. This is exactly the gap flagged when the feature was
built ("not verified: the actual live connect-and-browse flow... if a
real server's implementation quirks differ from the mocked test,
that's where it would show") - found the moment it was tested for
real.

Fixed in `plc/opcua_driver.py`'s `_browse_recursive()`: skip any node
whose own `NodeId.NamespaceIndex == 0` entirely (neither included in
results nor recursed into) - by OPC UA spec, namespace 0 is reserved
for the OPC Foundation's own standard information model, never actual
vendor/user tags, which always live in a non-zero namespace by spec.
Re-verified against the real simulator: 232 nodes -> exactly the 3
real tags, correctly formatted (`ns=2;s=test1` etc., matching the
simulator's own documented NodeId convention). Also added a
mock-based regression test specifically for this filter (a fake
ns=0 Object + ns=0 Variable + a real ns=2 Variable in one tree -
confirms only the real one survives), on top of the existing
depth/count-cap mocked tests, so this can't silently regress. Full
95-test suite still clean at the (now-12) baseline.

## Three more OPC UA Tag Mapping improvements (2026-08-12)

Continued feedback from testing against `opcua-web-simulator`.

**1. No way to disconnect an active connection.** The UI only ever
offered "Set Active" (hidden once already active) - once set, there
was no path back to "no connection active" short of deleting the
connection outright. Added `PLCConnectionManager.deactivate()`
(mirrors `set_active()`'s audit-logging pattern, just clears the flag
without setting anything else) and a "🔌 Disconnect" button that
replaces "Set Active" once a connection is already active. Verified
against a throwaway copy of the real `database/actual/config.db` (not
the live one) - active connection correctly cleared.

**2. Browse results now show each node's data type.** `browse_nodes()`
now returns `(display_path, node_id_string, data_type_name)` triples
instead of pairs - reads each Variable's type via asyncua's
`read_data_type_as_variant_type().name` (one extra round trip per
variable; acceptable since the namespace-0 filter from the previous
round already keeps the Variable count small). Verified against the
real simulator: `test1`/`test2`/`test3` correctly report
`Boolean`/`Int16`/`Int32`, matching their configured types exactly.

**3. Type mismatches are shown but blocked, not silently allowed.**
New `OPCUA_COMPATIBLE_VARIANT_TYPES` in
`ui/pages/10_PLC_Connectivity.py` maps each of SmartMachineAI's own
tag data types (REAL/INT/BOOL/STRING) to the OPC UA VariantType names
that actually fit (e.g. INT accepts SByte through UInt64; REAL
accepts Float/Double). The browse dialog now takes the target tag's
configured data type as a parameter, marks every option ✅/⚠️/❔
(match / mismatch / type couldn't be read), and disables "Use this
node" for a confirmed mismatch (with a message pointing at *why*,
not just a dead button) - an unreadable type is a warning, not a
block, since it's genuinely unknown rather than confirmed wrong.
Verified the full compatibility matrix (all 4 SmartMachineAI types x
all 3 real simulator tags) against the live server - every match/
mismatch verdict came out correct.

Full 95-test suite clean (same 12-failure baseline, see above). Page
still renders with no exception via `AppTest`.

## Why "connected clients" never showed live - two compounding causes
(2026-08-12)

User reported the simulator's "Connected clients" panel never showed
anything live. Investigated by checking what's actually true right
now rather than guessing: `plc_logger.service`'s own journal showed
"0 of 0 tags logged this cycle" continuously, and zero real TCP
connections to port 4840 existed - so the honest answer was "nothing
is actually connecting," not a display bug in either project.

**Cause 1 (not a bug, a data gap):** `database/actual/config.db`'s
`tag_addresses` table is completely empty - no OPC UA mapping has ever
been saved. The `actual` environment's only 2 tags are both `REAL`
type, and the simulator's only 3 tags are `Boolean`/`Int16`/`Int32` -
none `Float`/`Double` - so there was never a type-compatible node to
pick, meaning last round's type-matching guard (deliberately) never
let a save through. Not something to "fix" - the guard is working
correctly; the user needs either a `Float`/`Double` tag added on the
simulator side, or a `BOOL`/`INT` tag created on the SmartMachineAI
side to actually test end-to-end.

**Cause 2 (a real, separate bug, found while checking cause 1
wasn't the whole story):** even with a valid mapping saved,
`app/plc_logger.py` would still have found zero tags to poll.
`driver = create_driver()` correctly builds the driver for whatever
PLC connection is actually active (OPC UA, in this case), but the very
next line queried `registry.get_enabled_for_driver(cfg.driver)` -
`cfg.driver` reads `config/settings.ini`'s legacy `[DATA_SOURCE]
driver` value directly, which is `simulator` and **never updates when
a connection is activated on the PLC Connectivity page**. So the
driver *object* and the tag *query* could silently point at two
different protocols - `tag_addresses` keys every mapping by the exact
driver string, so this always found zero tags for a real, correctly
saved OPC UA (or Modbus/FINS/S7) mapping, regardless of the UI saying
otherwise.

Fixed with a new `resolve_driver_name()` in `plc/driver_factory.py`
that mirrors `create_driver()`'s exact resolution logic (active
connection's protocol, else settings.ini's driver) without
constructing the driver - `app/plc_logger.py` now uses this for both
the tag query and its own startup log line (previously printed the
same stale "Driver: simulator" regardless of what was actually
active). Verified: `resolve_driver_name()` returns `"opcua"` matching
the real `OPCUADriver` instance `create_driver()` builds; a throwaway
copy of the real `database/actual/config.db` with one manually-
inserted valid `tag_addresses` row confirms the full chain now finds
the tag correctly (previously would have returned empty even with
that row present, since the old code queried for `"simulator"`
instead). Full 95-test suite clean (same 12-failure baseline).

## The full "connected clients shows 0" story + auto-reconnect
(2026-08-12)

Follow-up to the previous entry - user clarified they meant the
simulator's live count still read 0 even after connecting. Traced
precisely using the simulator's own log file (`data/logs/server.log`)
rather than guessing further: `plc_logger` genuinely **did** connect
successfully at startup (`New connection from ('127.0.0.1', 32896)`
at 12:07:38) - but with zero tags mapped (the same root cause as the
previous entry), nothing ever generated read traffic, and the OPC UA
session **idle-timed-out and was dropped by the server ~45 seconds
later** (`Lost connection ... 12:08:23`). "0 connected clients" was
completely accurate - the connection really was gone.

**The deeper finding: nothing in `app/plc_logger.py` could ever have
noticed or recovered from that drop.** Two independent reasons:
1. Zero tags means `driver.read_all([])` never attempts a read at
   all - no traffic, no exception, no signal of any kind.
2. Even *with* tags configured, each driver's `read_all()` catches and
   prints per-tag read errors internally rather than raising them
   (deliberate - see the STRING/AlarmCode crash-loop fix earlier in
   this doc for why one bad tag must never block the rest of a
   cycle) - so a dead connection wouldn't propagate as an exception to
   `main()`'s own error handling either.

**Added real auto-reconnect**, not just a fix for this one scenario -
user asked for it explicitly once this was found. New
`_reconnect()` helper (disconnect, ignoring any error since the old
connection may already be dead, then connect again) called two ways:
(1) proactively every `RECONNECT_INTERVAL_SECONDS` (30s) regardless of
whether anything has failed - this is what actually fixes the zero-
tags/idle-timeout case, since it's the only signal-independent path;
(2) immediately whenever the main loop's existing catch-all
`except Exception` fires, for faster recovery on a failure that *does*
propagate (e.g. the whole `read_all()` call throwing, not just one
tag's read within it).

Verified three ways: (1) a mocked-driver unit test confirming
`_reconnect()` calls disconnect-then-connect, and still connects even
if disconnect() itself raises; (2) a real end-to-end run against the
live simulator with a shortened 3s interval (for a fast test) doing
the exact zero-tags idle scenario - confirmed via the simulator's own
log that the old connection was dropped and a new one opened right on
schedule; (3) full 95-test suite clean (same 12-failure baseline).

## "Disconnect" in the UI didn't actually disconnect the live process
(2026-08-12)

Follow-up bug from the auto-reconnect feature directly above, found by
the user actually testing it: clicking Disconnect (or Set Active to a
different connection) on the PLC Connectivity page only ever updated
the `plc_connections.is_active` database flag. `app/plc_logger.py`'s
`main()` resolves `driver`/`driver_name`/`tags` exactly **once**, at
process startup, and the periodic auto-reconnect added in the entry
above just called `_reconnect()` on that same already-built `driver`
object every 30s - so a deactivated connection was kept alive
indefinitely, not dropped. Confirmed precisely before fixing anything:
DB read back `None` for the active connection, but `ss -tnp` still
showed an ESTABLISHED TCP connection from the running `plc_logger.service`
process (PID confirmed older than the Disconnect click via
`systemctl show -p ExecMainStartTimestamp`) to the simulator's port
4840 - visible on the simulator side as a client that "won't
disconnect" no matter what's done in SmartMachineAI's own UI.

**Fix:** new `_refresh_driver(driver, driver_name, registry)` in
`app/plc_logger.py`, replacing both call sites that used to call
`_reconnect()` directly (the periodic 30s refresh and the
`except Exception` handler). It re-resolves `resolve_driver_name()`
every time: if the result is unchanged, it falls back to the same
lightweight disconnect/reconnect refresh as before (preserving the
idle-timeout recovery from the entry above); if it's genuinely
different (a different connection activated, or deactivated back to
`settings.ini`'s legacy fallback driver), it disconnects the old
driver, builds a fresh one via `create_driver()`, connects it, and
re-queries `registry.get_enabled_for_driver()` for the new driver
name - `main()`'s loop then swaps its local `driver`/`driver_name`/
`tags` to the new values. So a Disconnect or Set-Active change made in
the UI now takes effect automatically within one
`RECONNECT_INTERVAL_SECONDS` window (30s), no manual restart needed.

Verified three ways: (1) a mocked-`create_driver`/`resolve_driver_name`
unit test covering both branches (name unchanged -> same driver object,
lightweight refresh only; name changed -> new driver built, tags
re-queried, old driver's `disconnect()` called even when it raises);
(2) a real end-to-end run against the live simulator with a shortened
3s interval - activated the real `test opc` OPC UA connection,
confirmed a genuine session connect in the simulator's log, then
deactivated it and confirmed from the process's own log output
(`Active connection changed (opcua -> simulator) - switching driver.`)
and from `ss -tnp` that the OPC UA TCP connection actually disappeared,
with the process correctly falling back to the `simulator` driver (0
tags, since none are mapped for it); (3) full 95-test suite clean
(same 12-failure baseline, see "Regression testing discipline" note
elsewhere in this doc).

**Not yet live** - this only changed `app/plc_logger.py`, and
`plc_logger.service` only imports it once at process start, so the
currently-running service (still holding its stale connection from
before this fix) needs a restart to pick this up:
```bash
sudo systemctl restart plc_logger.service
```

## OPC UA simulator: "Set Value" only persisted, never pushed to the
live server (2026-08-12)

User reported that in `opcua-web-simulator`'s Tag Configuration page,
changing a tag's "Set Value" did nothing until "Save & apply tags"
was clicked - contradicting that section's own caption ("Applies
immediately on change, no Save button"), which was accurate for an
earlier round of this feature but had quietly stopped being true.

Root cause: `_render_set_value_row()`'s instant-update path only ever
called `config_store.update_tag(tag_id, initial_value=...)` - which
persists to SQLite (used to build node values at the *next* server
start or `apply_tags()` rebuild) but never touches the already-running
server's live OPC UA node. The "Live values & manual control" panel
below it (pre-existing, unchanged) already had the correct mechanism
for this (`ServerController.write_value()`, which writes straight to
the live node and updates `_live_values`) - the Set Value section just
never called it.

**Fix:** new `_apply_set_value(tag, value)` helper in
`views/tag_configuration.py` - persists via `config_store.update_tag()`
as before, then also calls `ServerController.instance().write_value()`
when the server is running, so the change is visible immediately on
the live server, not just the next restart. Falls back to persist-only
(silently) when there's no live node yet (server stopped, or the tag
disabled) - same as before, just no longer the *only* path when the
server is up.

Verified against a real in-process server (throwaway copy of
`data/config.db`, separate test port so the live simulator was never
touched): called the actual `_apply_set_value` function (extracted
from the page's AST so the real code under test runs, not a
reimplementation) against a Boolean and an Int16 tag - both the live
node value (read back through the real server, not a cache) and the
persisted value updated instantly, no Save click. Also confirmed the
stopped-server case still degrades gracefully to persist-only, no
crash. The real running simulator (not a systemd service - see
"Background services" below) was relaunched afterward (this page
script's own code, so a normal Streamlit rerun likely would have
picked it up anyway, but relaunching removed any doubt) and confirmed
still serving real data over OPC UA post-relaunch.

## `app/ask.py` follow-up correction: a bare time-only follow-up with
no keyword to latch onto silently failed (2026-08-12)

User reported: "anything happen today" correctly returned today's
factory-wide alarm/warning timeline, but the immediate follow-up "how
about yesterday" fell through to the generic tag-candidate menu
(Generator GEN01 tags) instead of yesterday's timeline - the exact
failure mode the follow-up-rewrite fallback (see "'Yesterday' dropped
from alarm questions" and "'N days ago' follow-ups" sections above)
was built to rescue, but didn't this time.

**Root cause, found by direct evidence gathering (not guessing):**
querying `IndustrialQueryEngine` directly showed "how about yesterday"
classifies as `current_data` (not `timeline`) - a bare "yesterday"
alone has no EVENTS-type word (no "alarm"/"warning"/"happen"/etc.) for
the intent classifier to latch onto, unlike every previously-fixed
case in this doc, which always had one either in the question itself
or after the correction step ran. That's fine *in principle* - it's
exactly the case `_try_correct_question()`'s history-aware rewrite is
supposed to handle. But calling it directly showed **the rewrite
itself was the bug**: it correctly detected "how about yesterday" as a
subject-less follow-up (the existing Step 1/2 check worked), but
instead of carrying over the actual prior topic it invented an
unrelated, plausible-sounding one - "How about yesterday's equipment
operation?", when the prior turn ("anything happen today") was about
alarm/warning events, not "operation" at all. Confirmed deterministic/
reproducible against the real model (not a one-off sampling fluke) -
the original prompt said "carry over the missing subject" but never
asked the model to first identify *what that subject actually is*,
so with no explicit keyword in the recent history to anchor to, it
paraphrased into a guess instead.

**Fix:** added an explicit Step 1 to the history-aware prompt in
`_try_correct_question()`: "what topic was the most recent relevant
earlier message actually asking about, in a few words" - reasoning
only, not part of the output - *before* the existing "does the newest
message name its own subject" / "rewrite using that topic" steps.
Verified in isolation first (per this project's established
discipline for this exact prompt, see the entry below it): 2/2 runs
against real `qwen2.5:7b` correctly produced "anything happened
yesterday" / "Did anything happen yesterday?", both of which re-
resolve as a real `timeline` match against the deterministic engine.
Re-verified the two previously-documented cases didn't regress: "how
about in the pass few days?" after "is there any alarm yesterday"
still correctly carries "alarm" forward; "and the chiller?" (the
topic-switch case that must not get hijacked by alarm history) still
resolves the same way as before - turns out both the old *and* new
prompt phrasing blend "alarm" into that one's rewrite, which was
already true before this change too, and doesn't break the chiller
clarification menu either way.

Verified end-to-end against the real live engine and `qwen2.5:7b`,
reproducing the user's exact reported sequence: "anything happen
today" -> today's timeline (unchanged), then "how about yesterday" ->
now correctly returns yesterday's factory-wide timeline instead of the
candidate menu.

**Regression baseline note:** re-running the full suite during this
fix showed 5 failures, not the "12" last recorded after P02 was
enabled (same test files - `test_equipment_knowledge.py`/
`test_hybrid_router.py`/`test_pipeline.py`, still all genuinely dead
code, confirmed unrelated to this fix). Noting the drift honestly per
this doc's own standing instruction ("a count alone can silently
drift like this without being noticed") rather than silently updating
the number without comment - not investigated further since it's
dead/unimported code, same standing policy as every other mention of
this baseline in this doc.

## Two more gaps found via live testing: "show me all X status" and
plant-filtered discovery (2026-08-12)

Same testing session, two more pasted transcripts, same treat-it-as-
a-bug-report pattern as the entry above.

**Bug A: "show me all compressor status" returned an unrelated "2
days ago" alarm/warning timeline.** Traced with direct evidence, not
guessed: querying `IndustrialQueryEngine` directly showed this
question classifies as `current_data` (measurement="status", matching
per-instance `LoadStatus`/`UnloadStatus` tags) with `status:
clarification_required` - genuinely ambiguous across AC01/02/03, no
time reference anywhere in the deterministic result. So "2 days ago"
had to be coming from `_try_correct_question()`'s LLM fallback -
confirmed by calling it directly, which reproduced a bad rewrite, but
**a different bad rewrite each time** ("What is the status of all
compressors today?" with one plausible history, "Show me the status
of all compressors." with none) - neither matched the user's literal
"2 days ago" artifact. This ruled out "fix the prompt again" as the
right move (the entry above already fixed one real prompt gap this
session, but this isn't that - it's the model's fundamental
unreliability on an open-ended rewrite task, the same class of
limitation already documented in "Equipment metadata & document
lookup" above). The real, fixable root cause is one layer up: this is
a perfectly natural, common phrasing that should never have needed
LLM rescue at all.

Root cause: `ConceptExtractor.extract()`'s broader `equipment_status`
branch is gated on `not measurement` - correct for most cases (a
named measurement usually means "give me that one specific value"),
but wrong here, since "status" itself matched `MEASUREMENTS` (a real
per-tag concept like `RunStatus`), so the gate silently excluded
every "status" question from ever reaching `equipment_status`,
regardless of how broadly it was framed. Fixed with a new, narrowly-
scoped `elif`: `measurement == "status" and discovery_words &
{"all", "every", "overall"}` - i.e. only when "status" is paired with
an explicit broadening word, not every question that happens to
contain "status". Verified: "show me all compressor status"/"what is
the status of all compressors" now correctly classify as
`equipment_status` (resolves instantly, deterministically, no LLM -
so the "2 days ago"-style hallucination risk is structurally
eliminated for this phrasing, not just less likely); "what is the
AC01 status" and "show me the compressor status" (no broadening word)
correctly stay `current_data`, unaffected. Also confirmed the
resulting single-instance pick (it lands on AC01 (P01) specifically,
not a "which compressor" menu) isn't new behavior introduced by this
fix - "is the compressor ok" (an already-existing, already-verified
equipment_status trigger) does the exact same thing, per this
intent's documented design ("if the guess is wrong, just say what you
meant" - see "New question types" above).

**Bug B: "show me P01 equipment" returned all 76 equipment instances
across both P01 and P02, ignoring the named plant.** Confirmed via
direct evidence that extraction itself was fine
(`concepts.plant == 'p01'`, correctly canonicalized) - the gap was
purely that `app/ask.py`'s `discovery` handling never consulted it.
Fixed: `_list_available_equipment()` now takes an optional `plant`
parameter and filters equipment by `e.name LIKE '<plant>\_%' ESCAPE
'\'` (the underscore escaped since it's a LIKE wildcard) - the same
`p01_`/`p02_` name-prefix convention `_equipment_type_key()` and
`industrial_query_engine.py`'s `_break_plant_ties()` already rely on,
so this introduces no new convention. `_resolve_and_answer()`'s
`discovery` branch now passes `result.concepts.plant or None`
through. `_format_available_equipment()` also now appends "in P01"/
"in P02" to the header when a plant filter was applied, so the answer
is honest about the scope it's showing. Verified: "show me P01
equipment" now returns exactly 38 equipment / 313 tags, all
genuinely P01 (checked for the literal `(P02)` substring, not just
`"P02"` - a naive check briefly false-flagged on tag names like
"CHWP02"/"WSP02" that happen to contain "P02" as a substring, caught
before trusting it); "show me all equipment" (no plant named) still
returns the full unfiltered 76/625 listing, confirming no regression
for the common case.

Full 95-test suite clean for both fixes (same 5-failure baseline, see
the regression baseline note above).

## Proactive equipment_status sweep, per explicit request (2026-08-12)

User reported "show all cold room status" returned a factory-wide
summary that never mentioned Cold Room at all, then explicitly asked
to stop testing gaps like this one-by-one and instead take the time
to think through the space of similar questions proactively. Treated
as a mandate for a broader sweep, not just the one reported case -
mirrors the "Proactive common-sense sweep" entry earlier this doc.

**Bug 1 (the reported one) - equipment names that collide with the
generic LOCATIONS/CONDITIONS vocabulary lose to empty equipment_terms.**
Root-caused with direct evidence: `ConceptExtractor.extract()` strips
every word that matched a location/condition/measurement alias from
`equipment_terms` (so they don't pollute equipment-name fuzzy
matching) - correct for a real modifier word ("discharge pressure"),
wrong when the "modifier" word IS the equipment's own name. "Cold
Room" collided on two fronts at once: "cold" is a CONDITIONS alias
(`"too cold"/"freezing"`) and "room" is a LOCATIONS alias - both
words of "cold room" got stripped, leaving `equipment_terms == ()`
and nothing for `_equipment_score()` to match against. Confirmed
systemic, not a one-off: "tank", "header", "incomer", "breaker",
"motor" all hit the exact same empty-equipment_terms failure (each is
both a real equipment/component name and a bare LOCATIONS entry).

Fixed in `app/ask.py`'s `equipment_status` handling: new
`_equipment_category_key()` (like the existing `_equipment_type_key()`
used for discovery grouping, but also strips the plant prefix, so a
mixed CR01(P01)/CR02(P01)/CR01(P02) candidate set still collapses to
one category) plus a `category_confident` fallback - when
`equipment_terms` is *completely* empty, `status ==
"clarification_required"`, and every one of the top candidates shares
one equipment category, that's treated as confident enough (the
engine's own top-candidate ranking already got this right via
location/condition scoring - the gap was only in how `app/ask.py`
independently re-derived its own confidence signal from
`equipment_terms` alone, throwing that away).

**Caught by testing broadly before trusting it - the first version of
this fix was wrong.** An initial, looser version fired whenever every
top candidate shared one category, regardless of whether
`equipment_terms` had SOME content or none. That produced two new,
confidently WRONG answers: "show all air header status"
(`equipment_terms=('air',)`, a real but weak match) resolved to Air
Compressor instead of Compressed Air Header, and "show all MCC room
status" (`equipment_terms=('mcc',)`) resolved to Cold Room instead of
MCC Room - both cases where a non-empty-but-weak equipment_terms match
should have been trusted (and left correctly below the confidence
floor) rather than overridden by the new fallback. Narrowed the gate
to require equipment_terms be *completely* empty - the precise
signature of the actual bug being fixed, not a general "trust the
candidates" override. Re-verified: both false-positive cases are safe
again (factory-wide, no wrong claim), the original cold-room/tank
cases still resolve correctly.

**Bug 2, found via the proactive sweep - six more equipment-name words
were silently mangled by typo-correction, one non-deterministically.**
Systematically ran every significant word from every equipment display
name (not just "cold room") through `correct_spelling()`, 6 fresh-
process repeats each (to also catch the `PYTHONHASHSEED`-dependent
tie-breaking already documented for other words in this doc). Found
"mill" (Bead Mill) silently corrected to "will" or "fill" depending on
the process's hash seed - the same recurring bug class as
"or"->"orp"/the connector-word batch/the number-word batch, just
hitting an equipment-name word instead of a common English one this
time (confirmed live: "show all bead mill status" non-deterministically
answered about Filling Machine instead of Bead Mill, traced to "mill"
getting corrected to "fill" some process runs but not others). Also
found five more, all deterministically wrong every run: "area"->"are",
"dust"->"just", "filling"->"falling", "fire"->"fine"/"five" (this one
also hash-seed-dependent), "ups"->"up".

Fixed with a new `EQUIPMENT_NAME_WORDS = {"area", "dust", "filling",
"fire", "mill", "ups"}` added to `VOCABULARY_WORDS` in
`concept_extractor.py` (protects from `_correct_word()`'s correction
pass) - deliberately NOT added to `STOPWORDS`, since these words must
still survive into `equipment_terms` to identify their equipment
(unlike a true stopword, which is deliberately stripped). Re-verified
all six stable across repeated fresh-process runs after the fix, and
re-swept every equipment word in the system once more to confirm no
others remain vulnerable.

**Verification: full sweep of "show all X status" across every real
equipment category in the system** (not just the ones already spot-
checked), comparing against what each SHOULD resolve to. Combined
result of both fixes: `cold_room`, `tank`, `bead_mill`,
`fire_water_system`, `ahu`, `air_compressor`, `area_monitoring`,
`chilled_water_pump`, `chiller`, `dust_collector`, `generator`,
`mixer`, `solvent_transfer` all now correctly resolve to their real
equipment (several of these - bead_mill, fire_water_system - were
fixed by Bug 2 alone, not Bug 1). `compressor`/`is the compressor ok`
(the already-existing, already-working cases) confirmed unaffected.

**Two pre-existing, separate issues found but deliberately NOT fixed
in this pass** - flagged for the user rather than bundled in, since
both touch `_equipment_score()`'s core fuzzy-matching weights, used by
literally every question type across the whole system (current_data,
trend, threshold, timeline, root_cause - not just equipment_status), a
much bigger blast radius than either fix above:
1. **"show all pump status"** ranks Main Incomer/Air Compressor tags
   above real Water Supply Pump/Chilled Water Pump tags, despite
   `_equipment_score()` alone correctly favoring the real pump
   equipment (0.25-0.26 vs 0.17-0.19) - traced to
   `tag.measurement` classifying `RunStatus`/`CompressorStatus`/
   `JockeyPumpStatus` tags as `"running"` (a real, deliberate, more
   specific classification) while `BreakerStatus`/`LoadStatus`/
   `DefrostStatus`/etc. classify as generic `"status"` - so a
   generic "status" question's "measurement exact" scoring bonus
   (+30) only applies to the *wrong* equipment for a pump, while real
   pump tags only get the weaker "measurement text" bonus (+18), a
   12-point gap that outweighs the equipment-name advantage.
2. **"show all water meter status"/"show all RO system status"** both
   resolve to Fire Water System instead - generic overlapping words
   ("water", "system") appear to accumulate a stronger lexical/fuzzy
   score against "Fire Water System" than the actually-intended
   equipment.

Both are real gaps, but neither is the vocabulary-collision or typo-
correction bug class fixed above - they're about how `_rank()`/
`_equipment_score()` weighs measurement-exactness and lexical overlap
against equipment-name specificity, and deserve their own dedicated,
carefully-verified pass (per this project's standing preference: "for
any large, multi-part feature, propose a phased plan and confirm
scope/starting point before building") rather than a fix bolted onto
this one. Given this project's own principle honestly applied: my
`category_confident` fallback correctly does NOT paper over either of
these (`equipment_terms` is non-empty for both, so the new fallback
never fires) - they still safely default to factory-wide rather than
confidently naming the wrong equipment.

Full 95-test suite clean throughout (same 5-failure baseline).

## Background services (systemd - not obvious from reading the code
alone)

Two systemd **system** services exist under `/etc/systemd/system/`,
separate from anything git-tracked:
- **`event_monitor.service`** - enabled and active, `Restart=always` /
  `RestartSec=5`. Killing the process just makes systemd respawn it
  ~5 seconds later - **this happened twice this session**: what
  looked like an "orphaned leftover" `event_monitor` process was
  actually this service running since well before the session
  started; killing it, and killing the process that respawned in its
  place, just produced two more respawns. Current state: the manually-
  launched duplicate `event_monitor` was stopped instead, leaving
  this systemd service as the sole event monitor (no `sudo` access was
  available in this session to disable the service itself). To take
  manual control instead, from a real interactive terminal (not a
  Claude Code `!` command - `sudo` needs a real TTY):
  ```bash
  sudo systemctl stop event_monitor.service
  sudo systemctl disable event_monitor.service
  ```
- **`plc_logger.service`** - **fixed and confirmed running as of
  2026-08-11.** Previously crash-looped ("Error: timed out" on
  2026-07-25, most likely a real PLC/FINS connection that wasn't
  available) and separately had a stale `ExecStart` (ran `python
  plc_logger.py` from `WorkingDirectory=.../app` with no `sys.path`
  fixup, so it hit `ModuleNotFoundError: No module named 'config'`
  immediately). The corrected unit (`ExecStart=.../venv/bin/python -m
  app.plc_logger`, `WorkingDirectory=/home/test/SmartMachineAI`, same
  pattern `event_monitor.service` already used) was handed to the
  user as copy-paste `sudo` commands in an earlier session; confirmed
  via `systemctl status`/`systemctl show` this session that it's now
  installed and running that corrected command.
- **`streamlit.service`** - **also confirmed created and running as of
  2026-08-11** (`streamlit run ui/Home.py --server.headless true
  --server.port 8501`, same `WorkingDirectory`). Didn't exist before
  this unit was added - the UI had only ever been run manually.
- **All three services (`plc_logger`, `event_monitor`, `streamlit`)
  need a restart to pick up any code change**, since each only
  imports its Python modules once at process start. Found this
  session: all three happened to start at the exact same timestamp
  (a coordinated deploy/boot), and `config/active_environment.txt`
  was switched to `"simulation"` about 10 minutes *after* that -
  meaning all three are currently still bound to whatever was active
  before the switch (confirmed via `plc_logger`'s own journal logging
  "0 of 0 tags logged this cycle"), exactly the situation the PLC
  Connectivity page's own "restart to pick it up" warning banner
  already describes. **Restart all three to pick up both the
  environment switch and this session's `app/ask.py`/
  `engine/concept_extractor.py` changes:**
  ```bash
  sudo systemctl restart plc_logger.service event_monitor.service streamlit.service
  ```

## Known issues found during review

1. **Database path mismatch (fixed once, may recur if re-migrating
   a project copy):** every module expects `database/config.db`, but
   the actual configured data sometimes ends up at
   `config/config.db`. If the engine throws a "Run this first: python
   -m engine.metadata_migrator" or can't find config.db, check this
   first:
   ```bash
   cp config/config.db database/config.db
   python -m engine.metadata_migrator
   ```
2. **`requirements.txt`** now has `openai>=1.0` (pre-existing
   uncommitted addition, predates this session) and `pypdf>=4.0`
   (added this session, for manual/datasheet text extraction -
   committed). `cryptography` is also installed in the venv
   (uncommitted, needed for encrypted PDFs like Bitzer's official
   docs) - not yet added to requirements.txt.
3. **When stripping the database before sharing/zipping the project,
   delete all three files together:** `machine_data.db`,
   `machine_data.db-wal`, `machine_data.db-shm`. Leaving the -wal/-shm
   files behind without the main .db file can cause confusing SQLite
   behavior.
4. `config/settings.ini` has `[AI] model = gpt-5.5` sitting under
   `provider = ollama` - harmless (unused while on ollama) but
   confusing to read. Still not cleaned up.
5. **FIXED - historian timestamp format mismatch.**
   `database/database.py`'s `save_tag`/`get_history`/`cleanup` used
   to write timestamps with `.isoformat()` (a "T" separator), but the
   ~980K existing rows in `plc_data` used a space separator. Since the
   time-range filter is a plain string comparison, `' ' < 'T'`
   lexicographically, so same-day lookback windows silently matched
   almost nothing. Fixed to `strftime("%Y-%m-%d %H:%M:%S")`
   everywhere, matching the legacy format (and matching
   `machine_events.event_time`, used by the factory-wide timeline
   fallback). **Don't revert this to `.isoformat()`.**
6. **FIXED - root-cause prompts timing out on `qwen2.5:7b`.**
   `RootCauseEngine` can return 20+ near-duplicate evidence lines.
   Fixed: `app/ask.py`'s `_condense_evidence()` collapses to one line
   per distinct `(tag, condition)` pair, and
   `ai/providers/ollama_provider.py`'s `read_timeout` was raised from
   300s to 600s.
7. **Hardware is weaker than earlier docs claimed** - 4 cores, not 8,
   and no GPU at all. Root-cause answers with a documentation excerpt
   attached can now take up to ~10 minutes. Intentionally deferred
   until the pipeline design is fully validated; see hardware-upgrade
   discussion (budget/mid-range GPU vs. Jetson tiers) from earlier
   this session if picking that decision back up.
8. **FIXED - `plc_logger` crash loop on STRING-typed tags.** See "P01
   Phase 1 tag migration" above.
9. **FIXED - equipment-matching false ties at the larger tag scale.**
   See "NLP / query engine fixes" above.

## `app/ask.py` status (fully verified)

```
question -> IndustrialQueryEngine (tag/equipment/intent)
         -> [discovery / factory-wide-timeline: instant, deterministic,
            no LLM - see "New question types" above]
         -> [ambiguous/unrecognized: numbered menu, remembers
            selection for the next reply - see "New question types"]
         -> DatabaseManager.get_history (raw historian rows)
         -> trend_analyzer.analyse_tag (current value, trend, min/max/avg)
         -> RuleEngine.evaluate_summary (inside/outside engineering limits?)
         -> RootCauseEngine (root_cause only - condensed machine_events
            evidence + retrieved manufacturer-documentation excerpts,
            when available for that equipment's brand/model)
         -> prompt_builder.build_prompt (assembles one grounded prompt;
            "possible causes" section only appears for root_cause)
         -> AIProvider.generate (Qwen or GPT phrases the final answer)
```

Key design choices to preserve:
- The LLM is only used to **phrase** the answer. Every fact comes from
  deterministic code. Discovery and factory-wide-timeline answers skip
  the LLM entirely (structured listings don't need paraphrasing, and
  it means instant responses instead of minutes-long waits).
- If no AI provider is available, falls back to a deterministic-only
  text answer instead of crashing. Don't remove this fallback.
- Run it with `python -m app.ask`.

All seven intents verified against real local Qwen, including the two
gaps a prior version of this doc had flagged as "not yet fixed"
(`threshold` now surfaces real configured limit values; `timeline` now
queries real `machine_events` history and no longer invents ungrounded
causes when it has nothing to report):

| Intent | Result |
|---|---|
| `current_data` | Correct, grounded, ~1.5-2 min |
| `root_cause` | Correct, grounded, stays within supplied evidence, ~5-10 min (longer with a documentation excerpt attached) |
| `trend` | Correct, grounded, ~2.5 min |
| `threshold` | Correct - now cites the actual configured limit, e.g. "low alarm is 4.5 bar" |
| `timeline` | Correct - now cites real alarm timestamps/values from `machine_events`, no more inventing causes with nothing to report |
| `discovery` | Correct, instant (no LLM) |
| factory-wide timeline fallback | Correct, instant (no LLM) |

Known, accepted limitation (not a bug to keep chasing right now):
root-cause answers can cite a plausible-sounding fabricated detail
alongside genuinely-grounded ones, even with real manufacturer
documentation supplied - see "Equipment metadata & document lookup"
above.

## Web UI (`ui/`, new this session)

A local Streamlit app, run with:

```bash
source venv/bin/activate
streamlit run ui/Home.py --server.headless true --server.port 8501
```

Then open `http://localhost:8501` (or the VM's LAN/external IP shown in
the terminal on the same port) in a browser. This satisfies the "make
it a local web interface" request directly - Streamlit's dev server
*is* a local web server, nothing extra was needed for that part.

Structure:
- `ui/Home.py` - landing page / nav description.
- `ui/data_access.py` - all SQLite reads/writes for the UI live here
  (not scattered across pages). Always reads whatever is currently
  **enabled** in `database/config.db`, so enabling more of the
  P01/P02/Phase 2/3 tag dataset (`engine/tag_dataset_importer.py`)
  makes it show up with no UI code changes.
- `ui/pages/1_Live_Data.py` - current value of every enabled tag,
  grouped by equipment, colour-coded by `RuleEngine` severity
  (alarm/warning/normal), auto-refreshing every 5s by default (can be
  turned off with a checkbox - it reruns the whole script on a timer,
  which is the correct/intended behaviour for a live dashboard, not a
  bug, even though it looks like an infinite loop to automated test
  tooling).
- `ui/pages/2_Setpoints.py` - view/edit the low/high warning/alarm
  limits per tag (backed by the existing `thresholds` table via
  `config/configuration_manager.py`'s `ConfigurationManager.set_threshold()`
  and `get_thresholds()` - already had audit logging built in, nothing
  new needed there). Only 12 tags currently have configured thresholds
  (the original hand-built factory tags) - the 162 enabled P01 tags
  don't have engineering limits defined yet. Edits go through a
  two-step review/confirm flow before writing (per the explicit
  earlier request: "AI will need to ask confirmation from user that
  'Are you sure to proceed the change?'"), and an audit-log expander
  shows recent changes.
- `ui/pages/3_Maintenance.py` - three sections: (1) upcoming/overdue
  schedule for all 84 equipment rows, computed from
  `equipment.next_due_at` if explicitly set, else
  `last_serviced_at + service_interval_days`, colour-coded
  overdue/due-soon/ok/not-scheduled; (2) a log-new-work form (matches
  the "I changed compressor oil, next oil change in 6 months" example
  - equipment picked from a dropdown, so the numbered-menu
  disambiguation built for the natural-language chat flow isn't
  needed here, a dropdown is unambiguous by construction); (3) history
  of past entries, filterable by equipment. New table
  `maintenance_log` (equipment_id, category, description,
  parts_replaced, performed_by, performed_at, next_due_at, created_at)
  and a new `equipment.next_due_at` column, added via
  `engine/maintenance_migrator.py` (same idempotent-migration pattern
  as `engine/equipment_metadata_migrator.py` - already run against
  `database/config.db`, takes an automatic timestamped backup first).
  The five categories (confirmed with the user): Preventive
  Maintenance, Repair, Replacement, Inspection, Calibration.

Build order was explicitly confirmed with the user as Live Data ->
Setpoints -> Maintenance; all three are now built.

**Tested this session (not yet clicked through in a real browser by a
human):**
- All four routes (`/`, `/Live_Data`, `/Setpoints`, `/Maintenance`)
  verified serving HTTP 200 from a running `streamlit run` process.
- Every page's actual data logic (not just imports) exercised directly
  in plain Python against the real `database/config.db` /
  `machine_data.db` - zero errors across 183 enabled tags, 84
  equipment rows.
- Found and fixed a real bug this way: Live Data's "Value" column
  mixed floats and string placeholders ("-", "(not tracked here)"),
  which broke Streamlit's Arrow serialization (silently fell back with
  a warning rather than crashing, but still wrong) - fixed by always
  formatting the column as a string.
- Setpoints' and Maintenance's write paths (`set_threshold`,
  `add_maintenance_entry`) were tested against a throwaway **copy** of
  `config.db`, not the live database, to confirm inserts/updates/audit
  logging work without touching real data.
- `requirements.txt` now includes `streamlit>=1.61` (a fresh venv
  needs it - the CPU-only VM's pip install took roughly an hour due to
  pandas/pyarrow being unavoidable hard dependencies of Streamlit,
  confirmed via PyPI's metadata; there's no lighter-weight install
  path).

## Realistic per-tag simulation + engineering thresholds (2026-08-09)

Two follow-on requests after the Web UI: (1) remove the very last
legacy-demo remnant, and (2) fill in engineering thresholds for the
P01 dataset "logically."

**Legacy demo fully retired.** `CompressorPressure` (the one tag kept
during the earlier cleanup, since it was the only legacy tag with a
proper `equipment` link) and its equipment row `Air Compressor System`
(id 1) were deleted - it predates the P01 naming convention and is
superseded by the real `P01.UTILITY.AC01/02/03` tags. Backup taken
first (`database/config_before_compressor_system_removal_*.db`).
`plc_logger` restarted to drop it from the active tag list (163 -> 162
enabled tags).

**Setpoints page refined again.** Beyond excluding BOOL (done earlier),
also excludes:
- Cumulative totals - any tag whose `unit` is in
  `simulator.tag_dataset_model.MONOTONIC_UNITS` (`kWh`, `h`, `m³`).
  This covers `*Hours` runtime counters *and* `*Energy_kWh`/
  `*.Total` running totals (e.g. the Building Water Meter's `Total`
  and the Power Incomer's `Energy_kWh` - confirmed these already
  exist as the "accumulate number" that was asked about, they just
  correctly don't get a threshold, for the same reason as Hours: a
  fixed limit on a number that only ever increases would trip once
  and stay tripped forever).
- `*StartCount` - an INT counter that behaves the same way in this
  simulator (only ever increments), same problem as the totals above.
- `*.Setpoint` - a control target (e.g. "cold room setpoint"), not a
  measured process variable.

All of the above stay visible on Live Data (including `RunningHours`,
kept for display purposes as explicitly requested), just aren't
offered as configurable setpoints. 88 tags remain genuinely
threshold-eligible.

**Simulator recalibrated from generic-per-unit to realistic-per-tag
ranges.** `simulator/tag_dataset_model.py`'s original `UNIT_PROFILES`
gave every tag sharing a unit the same operating band (e.g. *every*
`°C` tag - cold room, chiller, compressor discharge, MCC room -
oscillated around the same 15-45°C range). Harmless while no
thresholds existed, but it would have made realistic thresholds
useless (a cold room threshold set near a real 2-8°C range would
show permanent alarm against a simulator baseline of ~30°C). Added
`TAG_PROFILES` (keyed by `canonical_key(tag_name)`, which strips the
plant prefix and instance number - e.g.
`P01.UTILITY.AC01.OutletTemp` -> `UTILITY.AC.OutletTemp` - so AC01/
AC02/AC03 share one profile, and it's already P02-ready for whenever
that phase is enabled) covering all 88 threshold-eligible tags with
real per-equipment-type bands, falling back to the old `UNIT_PROFILES`
for anything not listed (safe/additive). Confirmed after restarting
`plc_logger`: all 116 currently-logged REAL tags evaluate as `normal`
- realistic values, no permanent false alarms.

**Thresholds filled** via new `engine/seed_engineering_thresholds.py`
(same idempotent-migration spirit as the other `engine/*` scripts,
takes an automatic backup first - `database/config_before_threshold_seed_*.db`).
`THRESHOLD_PROFILES` mirrors the simulator's `TAG_PROFILES` structure
so the "normal" band and the warning/alarm limits are always
consistent with each other. Grounded where possible in real standards
(e.g. ISO 10816-3 vibration zone boundaries: 4.5/7.1 mm/s) and in the
brand/model info already seeded (e.g. Atlas Copco GA30+ realistic
discharge temp/pressure, refined from the old generic-demo numbers
now that the actual model is known). Most signals only get a
`high_warning`/`high_alarm` (a motor running at low power/current
isn't a fault); pressure, level, and flow signals typically get both
low and high (running dry or blocked are both real fault conditions).
Applied to 88 of 88 eligible tags, zero gaps.

**Follow-up (same day): compressed-air flow totalizer added, DewPoint
threshold removed.** The air compressor header (`AIRHDR01`) had an
instantaneous `Flow` (Nm³/h) but no cumulative total, unlike the
Water Meter (`Flow` + `Total`) and every energy signal
(`Energy_kWh`). Added `P01.UTILITY.AIRHDR01.FlowTotal` (REAL, unit
`Nm³`) - required inserting both a `tags` row *and* a `tag_addresses`
row (address resolution goes through `tag_addresses`, not the
`tags.address` column - easy to miss, caused the tag to silently not
show up in `plc_logger` on the first attempt until this was found).
`Nm³` added to `simulator.tag_dataset_model.MONOTONIC_UNITS` so it
accumulates like the other totals. Verified logging end-to-end after
restarting `plc_logger` (163 tags now). Separately, the `DewPoint`
threshold (`high_warning`/`high_alarm`) was removed per explicit
request - all four parameters cleared via
`ConfigurationManager.set_threshold(..., value=None)`, and the entry
deleted from `engine/seed_engineering_thresholds.py`'s
`THRESHOLD_PROFILES` so a future re-run of that script won't
resurrect it. The tag itself is still simulated and still visible
everywhere, it just no longer alarms - confirmed via `RuleEngine`:
`not_evaluated`.

**Maintenance page split into Service & Maintenance (2026-08-09).**
`ui/pages/3_Maintenance.py` renamed to
`ui/pages/3_Service_and_Maintenance.py`, restructured into two
`st.tabs`:

- **Service** - one-off service visits, no schedule/next-due concept
  at all (deliberately - a service record doesn't drive the upcoming/
  overdue view the way a maintenance entry does). New `service_log`
  table (`engine/service_migrator.py`, same idempotent/backed-up
  pattern as the other migrators): equipment_id, `person_in_charge`,
  description, performed_at, created_at. `person_in_charge` is a
  select box (`PERSON_IN_CHARGE_OPTIONS` in the page file) - explicitly
  a **placeholder list**, since a real user-management feature is
  planned but out of scope here; swapping it for a real list later
  needs no other changes. Creating a record uses the same
  `st.dialog` popup pattern as the Maintenance "mark as done" flow.
  Still bumps `equipment.last_serviced_at` (a service visit counts as
  "serviced" for schedule purposes) via `ui/data_access.py`'s new
  `add_service_entry()`/`get_service_history()`.
- **Maintenance** - unchanged behavior (upcoming/overdue tracking,
  done/ignore, log-work form, history), but the upcoming/overdue list
  is now a real `st.dataframe` table instead of bulleted text lines.
  Since a plain table can't hold per-row buttons, it uses
  `st.dataframe(..., on_select="rerun", selection_mode="single-row")`
  - clicking a row selects it, and "Mark as Done"/"Ignore" buttons
  appear below the table for that row's equipment (only when the
  selected row's status is `OVERDUE`, matching the original scope of
  that feature). Maintenance History was also converted to a table for
  the same readability reason, even though not explicitly requested -
  worth flagging in case that wasn't wanted.

## Simulator Control - temporary dev tool (2026-08-09)

User wanted a way to manually trigger a realistic fault on demand (so
root-cause questions can be tested against a known event instead of
waiting for the simulator's random ~30-60min-per-instance timing), with
correlated, equipment-appropriate behavior (e.g. "compressor pressure
drops while power/current rises") rather than an arbitrary jump.
Explicitly framed as **temporary** - a dev/test tool, not part of the
operator-facing app.

**Reused the existing fault engine instead of building a new one.**
`simulator/tag_dataset_model.py`'s `_InstanceFaultState` (dormant ->
developing -> faulted -> recovering) already produces exactly this:
every REAL tag on an instance is pushed in a direction matching what
it physically measures (`FAULT_DIRECTION_DOWN = {"pressure", "flow",
"level"}` go down, everything else goes up), so each equipment type's
fault signature falls out naturally from which tags it has - no
per-scenario scripting needed. Added `force_start()`/`force_recover()`
to that class to let it be triggered manually instead of only randomly.

**Cross-process trigger, since `plc_logger` runs as its own separate
OS process** holding the live `TagDatasetSimulator` instance -
Streamlit can't call into it directly. New `simulator_fault_commands`
table in `config.db` (self-provisioning `CREATE TABLE IF NOT EXISTS`,
not a full migrator script, since this is explicitly temporary):
Streamlit inserts a row (`ui/data_access.py`'s
`send_simulator_command()`), `TagDatasetSimulator.update_values()`
polls for unprocessed rows once per cycle
(`_apply_pending_commands()`) and applies them, writing back a
`result` string (`started` / `skipped (already <phase>)` /
`recovering`) so the UI shows what happened. Verified end-to-end
against the live system: command picked up within ~1 cycle, correlated
fault visible in real `plc_data` within seconds, correctly evaluated
as `alarm` by `RuleEngine` a minute later, force-recovered cleanly
afterward.

**Found and fixed a real bug in the existing engine while testing
this** (not something this feature introduced, just newly exposed by
watching a full fault cycle play out for the first time): a sustained
fault's `fault_push` compounds every cycle with nothing capping it, so
letting one run its full ~10-30 cycle `faulted` phase drifted values to
physically implausible extremes (compressor discharge temp past
200°C, pressure hitting exactly 0). Fixed in `_update_real()` by
clamping the trend to a full band-width beyond the profile's normal
range (still a clear, sustained alarm-worthy excursion, just not
absurd) - **applied after clamping, not before**, so a "maxed out"
plateau still jitters with noise like real sensor data instead of
reading bit-for-bit identical for dozens of consecutive cycles.

New page `ui/pages/6_Simulator_Control.py`: equipment-instance picker,
Trigger Fault / Force Recover buttons, a live status view for just
that equipment's tags (reusing the same `RuleEngine`/color-coding
pattern as Live Data), and a recent-commands log. Clearly labeled
"temporary developer tool" in the page itself and in `ui/Home.py`.

## SCADA Floor Plan - experimental prototype (2026-08-12)

User asked to try a SCADA-style floor plan visualization using the
existing equipment, explicitly undecided about whether to keep it,
giving full creative latitude ("use your own imagination"). Built as
`ui/pages/12_SCADA_Floor_Plan.py`, deliberately **not** wired into
`ui/Home.py`'s navigation yet - run standalone
(`streamlit run ui/pages/12_SCADA_Floor_Plan.py --server.port 8503`)
so it can be evaluated with zero risk to the real app; trivial to wire
in later if kept, trivial to delete (one file) if not.

No real factory floor plan exists to copy - the 8-room layout
(Utilities Yard, Electrical Room, Cold Storage, Production Floor, Tank
Farm, Water Treatment Plant, HVAC & Monitoring, Fire Safety) is
imagined, but grounded in the tag-naming convention's own area codes
(`P01.UTILITY.*`, `P01.COLDROOM.*`, `P01.PROD.*`, ...) rather than
arbitrary - a `_category_key()` groups each `p01_<category>_<instance>`
equipment name into a category (same convention as `app/ask.py`'s
`_equipment_category_key()`), and each category is assigned to one
room. Anything not explicitly mapped falls into an auto-added "Other"
zone rather than silently disappearing. Equipment boxes are color-
coded via the same `RuleEngine.evaluate_summary()` severity logic Live
Data uses (red/amber/green/gray), plus a decorative emoji icon per
category (`CATEGORY_ICON`) and a blueprint-style grid background -
purely cosmetic, not derived from any real drawing.

**First version had two real usability problems, both fixed same
session after direct feedback:**
1. Equipment was clickable via `<a href="?equipment=...">` links
   directly on the SVG - a real browser navigation, so Streamlit
   reran the *entire* script on every click (visible full-page
   reload). Fixed by moving equipment selection to a plain
   `st.selectbox` and wrapping the whole live section (floor plan +
   selector + detail panel + the new info board) in one
   `@st.fragment` - a widget interaction or the fragment's own
   `run_every` timer only reruns the fragment, not the page, which
   also directly fixes the "auto-refresh makes it blink" complaint
   (the old `time.sleep()+st.rerun()` pattern reran, and visibly
   reloaded, the whole script every cycle).
2. Font/alignment issues in the custom HTML/SVG - fixed with
   `dominant-baseline="middle"` for SVG text (proper centering instead
   of a manual pixel-offset guess) and CSS Grid with a fixed
   `grid-template-columns` for the tag-detail rows (guaranteed
   column alignment regardless of content length, replacing an
   earlier flexbox layout).

Selection persistence across refreshes required care: the selectbox's
options are the *stable equipment name* (not the rendered, emoji-
prefixed label) with `format_func` handling display - options are
also re-sorted worst-severity-first on every refresh, and if the
selected *label string* had been the actual option value, a severity
change between refreshes would change the label and silently reset
the selection. Using the stable name as the value avoids this.

**Main Information Board** (right-hand column, per follow-up request
"a main information board... accumulate current consumption for
today, current ampere... you decide"): reads straight from each
plant's own Main Incomer tags (`P0X.ELEC.MAIN.*` - the plant's single
point of total incoming power) - today's accumulated energy (computed
as the delta of the monotonic `Energy_kWh` counter between midnight
and now via `DatabaseManager.get_history()`, not the raw running-total
reading itself), instantaneous power draw, per-phase current/voltage,
power factor, frequency - plus a small "Utilities" section (compressed
air header pressure/flow, water supply header pressure/tank level) and
a "Plant Health" rollup (equipment normal count, alarm/warning counts)
reusing the same per-equipment severity already computed for the floor
plan, so it doesn't re-derive anything. Lives in the same fragment, so
it refreshes on the same cadence as everything else.

Verified: `python3 -m py_compile`; `streamlit.testing.v1.AppTest` runs
clean with zero exceptions (works now that the auto-refresh mechanism
is `st.fragment(run_every=...)` instead of a blocking `time.sleep()`,
which previously hung AppTest's script-run - noted as a real
constraint, not assumed); the core data-gathering/rendering functions
(`_category_key`, `_equipment_status`, `_build_floor_plan_svg`,
`_todays_energy_kwh`, `_render_info_board`) exercised directly against
the real database (all 38 P01 + 38 P02 equipment instances correctly
categorized with zero left uncovered, generated SVG well-formed,
today's-energy-delta and live electrical readings sane against real
data - e.g. ~20-21 kWh accumulated since midnight, ~26 kW instantaneous
draw, 0.94 power factor); a real standalone instance launched on port
8503 and curl-verified serving HTTP 200 with no server-side errors in
its log. **Not verified**: the actual visual rendering in a browser -
no browser tool available in this session, so the alignment/graphics
fixes are only as good as the CSS/SVG reasoning behind them, not an
eyes-on confirmation. Worth the user's own look before trusting the
visual polish fully.

**Round three, after a real screenshot: text overflow, still-flashing
refresh, equipment not clickable, P02 missing from the info board
(2026-08-12).** First real look at the rendered page (not just logic
verification) surfaced genuine problems the earlier "not verified -
visual rendering" caveat above had flagged as a real risk:

1. **Equipment labels overflowed their icon boxes**, e.g. "Chilled
   Water Pump CHWP01" rendered in a 100px-wide box at 11px font -
   massively overflowing into neighboring icons, reading as garbled/
   overlapping text in the screenshot. Root cause: SVG `<text>`
   doesn't wrap or truncate on its own, and full display names were
   never going to fit fixed-width boxes at any font size that also
   fits a "code" reading.
2. **The whole screen still visibly flashed every 5s**, despite the
   `@st.fragment` fix from the round above. Root cause, reasoned
   through (no visual access to confirm directly, but consistent with
   documented Streamlit behavior): `st.fragment` does stop a literal
   full-page *navigation*, but `st.markdown(unsafe_allow_html=True)`
   content is not part of Streamlit's diffed widget tree - any change
   to the string wholesale-replaces that DOM node. The floor plan's
   entire visual area (SVG with every equipment icon baked into one
   huge string) was inside the fragment and regenerated every cycle -
   technically fragment-scoped, but since it covered almost the whole
   visible viewport, replacing it every 5s was visually
   indistinguishable from a full page reload.
3. **Equipment stopped being clickable at all** - the round-two
   redesign replaced the (real-navigation-causing) SVG click links
   with a single `st.selectbox`, which technically "worked" but wasn't
   what anyone actually wanted from a floor plan: clicking directly on
   equipment.
4. Info board only ever showed the plant currently selected for the
   floor plan - explicit ask to show P02 alongside P01, not gated
   behind the plant toggle.

**Fix, all in `ui/pages/12_SCADA_Floor_Plan.py`:** equipment are now
real `st.button` widgets (label = status dot + category icon + short
instance code, e.g. "🟢💨 AC01" - full name moved to the button's
native `help=` tooltip), grouped into bordered `st.container`s per
room. Native widget reruns are far smoother than replacing a big HTML
blob, and buttons are genuinely clickable again - `st.session_state`
tracks the selection so it survives both fragment reruns and full page
reruns. The SVG is now **purely decorative** (room outlines, floor
texture, connectors - no equipment, no live data) and is rendered
*outside* the fragment entirely, cached via `@st.cache_data` - it
structurally cannot cause the flash anymore, since it only redraws
when the plant selector changes (a deliberate action), never on the
periodic timer. Refresh interval also raised from 5s to 12s
(`REFRESH_SECONDS`) per explicit "delay is ok, just don't make it look
like it's refreshing." `_render_plant_board()` (renamed from
`_render_info_board()`) is now called twice - once for P01, once for
P02 - stacked in the right column with a divider, independent of
whichever plant's floor plan is currently displayed.

Verified: `python3 -m py_compile`; `AppTest` clean with zero exceptions
(38 buttons rendered for P01, matching its 38 equipment instances);
**simulated an actual button click through `AppTest`**
(`at.button[0].click().run()`) and confirmed the detail panel updates
to the correct equipment (`st.subheader` shows "Air Compressor AC01
(P01)") - this is the first round that verified the *click
interaction itself* works, not just that the page renders; confirmed
both P01 and P02 have zero uncovered equipment categories; full 95-
test suite clean (same 5-failure baseline); real standalone instance
on port 8503 relaunched and confirmed healthy (HTTP 200, no server
errors) with the current file's timestamp.

**Also fixed the identical root cause on `ui/pages/2_Live_Data.py`**
("also fix for the previous web that created earlier" - interpreted as
this page, since it's the one other place in the actual app with the
exact same `time.sleep()+st.rerun()` full-page-reload pattern, and the
one most likely to be what "the previous web" refers to given how much
this session used it). Wrapped the whole table (data load, equipment/
status filters, styled dataframe) in one `@st.fragment(run_every=5 if
auto_refresh else None)`, replacing the old sleep-then-rerun tail.
Verified: `py_compile`; `AppTest` clean with zero exceptions, correct
row count ("625 of 625 tags shown"); full 95-test suite still clean.
**This page IS wired into `Home.py`'s live navigation** (unlike the
still-standalone SCADA prototype), so `streamlit.service` needs a
restart to pick this up.

## Round four - the earlier "buttons fix the flash" explanation was
wrong, confirmed against Streamlit's own source (2026-08-12)

User reported, after round three above, that the decorative building-
icon SVG was pointless ("not clickable, remove it") and - more
importantly - **the screen was still flashing every 5s** despite
switching equipment to native `st.button` widgets specifically to fix
that. This meant the round-three theory ("native widgets update more
smoothly than replacing raw HTML") was wrong, or at least insufficient
- worth checking properly rather than layering on another guess.

Read `venv/.../streamlit/runtime/fragment.py`'s own docstring directly
instead of continuing to theorize: *"When Streamlit element commands
are called directly in a fragment, the elements are cleared and
redrawn on each fragment rerun, just like all elements are redrawn on
each app rerun."* This applies to **everything** inside a fragment -
native widgets included, not just raw HTML/SVG. The round-three fix
never addressed the actual mechanism: putting almost the entire page's
visible content (8 zone containers, dozens of buttons, the info board)
inside one `run_every` fragment means ALL of it gets torn down and
rebuilt every cycle, regardless of widget type - which, since it's
nearly the whole viewport, is always going to read as "the screen
flashes," exactly as reported both before and after the button change.

**This is a genuine Streamlit architectural constraint for this
amount of content in one auto-rerunning fragment, not a bug fixable
by swapping widget types.** A true flicker-free "only the changed
values update" live view would need a custom JavaScript component
(the page pushing individual value updates over its own WebSocket/JS
bridge instead of relying on Streamlit's built-in rerun mechanism) -
a substantially bigger undertaking than this prototype's current
scope, not attempted here without discussing it first.

**What was actually done:** removed the decorative SVG entirely (dead
weight - confirmed non-interactive and, per direct feedback, added no
value) along with its now-unused helpers (`_zone_center`,
`_build_static_floor_svg`, `CONNECTORS`, `CANVAS_WIDTH`,
`CANVAS_HEIGHT`). Raised `REFRESH_SECONDS` from 12 to 30 - the
practical mitigation given explicit confirmation that delay is
acceptable: infrequent enough that the redraw isn't disruptive, rather
than continuing to chase a flicker-free effect that isn't achievable
here without a much bigger change. The file's own header comment now
documents this reasoning (and the three prior attempts) directly, so
the next session doesn't retry the same "swap widget types" idea
without re-discovering why it doesn't address the root cause.

Verified: `python3 -m py_compile`; `AppTest` clean with zero
exceptions after the removal; **re-simulated a button click through
`AppTest`** to confirm the interaction still works post-edit
(`at.button[5].click().run()` correctly showed "Compressed Air Header
AIRHDR01 (P01)" in the subheader); full 95-test suite clean (same
5-failure baseline); standalone instance on port 8503 relaunched and
confirmed healthy.

## Round five - info board content restored + compacted, button colors
back (2026-08-12)

The flashing discussion above was explicitly set aside for later
("we will discuss about this flashing thing later") - this round is
two unrelated, smaller follow-ups from the same feedback pass.

**Info board had lost detail during the round-four merge.** When
`_render_info_board()` was renamed to `_render_plant_board()` and
started being called twice (P01 + P02) instead of once for whichever
plant was selected, it got trimmed down along the way - voltage
readings and the entire "Utilities" section (compressed air/water
header pressure) quietly disappeared, not a deliberate simplification.
User asked for them back, plus a request to make the whole thing more
space-efficient now that it covers two full plants.

Restored voltage (L1-2/L2-3/L3-1) and the utilities section, and added
a bit more to Plant Health (not-evaluated/no-data counts, not just
normal/alarm/warning) - genuinely more information than before, not
just restoring what was removed. Fits it by switching from the
original one-stat-per-card layout (11px label + 18px bold value + an
optional subtext line, ~8px padding, one card per line) to dense
single-line rows (`_compact_row()`: label left / value right, 11.5px/
12.5px, 2px padding, one border line) with small section headings
(`_board_heading()`) - roughly 3x the information density in the same
column width.

**Equipment button colors restored** (per a separate mid-turn request
- "I like the earlier design that when it has warning or alarm it will
change the equipment button colour", from before equipment became
`st.button` widgets in round three). `st.button` has no parameter for
a custom background color - the documented, version-stable workaround
is wrapping each button in its own `st.container(key=...)`, which
Streamlit gives a `st-key-<key>` CSS class, then targeting that class
directly with a plain `<style>` block (`_button_color_css()`, one
combined block for all 38 buttons rather than one tag per button).
`SEVERITY_TEXT` added alongside the existing `SEVERITY_COLOR` so each
background gets a text color actually readable against it (white on
the red alarm background, dark text on the lighter amber/green/gray
backgrounds). The status-dot emoji prefix was dropped from button
labels since the background color now carries that signal directly,
matching the original SVG-icon version's design intent.

Verified: `python3 -m py_compile`; `AppTest` clean with zero
exceptions, 38 buttons still present; **re-simulated a button click**
post-change to confirm interaction still works
(`at.button[3].click().run()` correctly updated the subheader); the
CSS-generation and info-board functions exercised directly against
real data (confirmed the real live alarm on Water Treatment System
SYS01 (P01) produced a correctly-matching `.st-key-...` alarm-red CSS
rule; both `_render_plant_board("p01", ...)` and
`_render_plant_board("p02", ...)` render with no exception); full
95-test suite clean (same 5-failure baseline); standalone instance on
port 8503 relaunched and confirmed healthy.

## Preferences for how to work on this project

- Explain changes in plain language, step by step.
- This person is not a professional developer - avoid unexplained
  jargon, prefer runnable commands over abstract descriptions.
- Don't remove the Qwen/GPT provider-switching architecture.
- Prefer wiring/reusing the existing deterministic engines over
  reinventing them - they're well-built, just needed connecting. This
  extended to reusing `engine/metadata_migrator.py`'s `infer()` for
  the P01 tags and `EventStore.get_recent_events()` (just adding
  optional time-range params) for the factory-wide timeline fallback,
  rather than writing new equivalents.
- Before assuming a background process is a stray leftover, check
  `systemctl status <name>.service` and `/etc/systemd/system/` first
  - this actually caused a repeated mistake this session (see
  "Background services" above), not just a hypothetical risk.
- When the codebase's actual state (git status, running processes,
  files that do/don't exist) disagrees with this doc, trust the
  codebase and fix the doc - this doc has been stale before.
- For any large, multi-part feature (the P01 migration, the document
  lookup feature), propose a phased plan and confirm scope/starting
  point before building, rather than guessing how much to build in one
  pass. This worked well every time it was done this session.
- Before declaring an LLM-grounding feature "working," actually check
  the model's specific claims against the real underlying data/
  evidence it was given - don't just check that the right context made
  it into the prompt. This is how the citation-fidelity gap above was
  caught rather than papered over, twice (once against synthetic docs,
  once against real ones).
- Commit only when explicitly asked, and stage specific files rather
  than `git add -A`/`.` - this repo has genuine unrelated pre-existing
  uncommitted WIP (see "Separate uncommitted WIP" above) that must
  never get swept into an unrelated commit by accident.
