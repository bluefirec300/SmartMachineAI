# New Factory Setup Checklist

What an engineer needs to configure before pointing SmartFactoryAI at a
real plant and treating its numbers as production-trustworthy. Every
item below exists because the simulation-era default is deliberately
labeled (never silently presented as real) - this checklist is the
list of "go find and confirm/replace those labels" work, not a list of
missing features.

**As of Phase V2.7, this checklist also exists in-app**: the
**New-Factory Setup Readiness** page (admin) shows live status for
every item below (confirmed/unconfirmed counts, simulated-vs-real
tariff, synthetic manual counts, etc.) read directly from the current
database/config state - no more manually running the `sqlite3` queries
below. This document remains the authoritative explanation of *why*
each item matters and exactly what to do about it; the in-app page is
a live dashboard on top of it, not a replacement for it.

Nothing here needs a code change - every item is done through the
existing admin UI pages. If an item is skipped, the system keeps
working and keeps being honest about it (a `⚪ inferred (simulation)`
badge, a `SIMULATION TARIFF` warning, etc.) - it just means that
specific number/label hasn't been confirmed against the real site yet.

## 1. Factory Area / System taxonomy

**Page:** Factory Configuration → Area/System tab.

Every area and system currently shows `⚪ inferred (simulation)` -
guessed from the simulator's own room grouping, not a confirmed real
factory layout. Go through each one, correct the name/description to
match the real site if needed, and save - saving automatically marks
it `🟢 confirmed`.

```bash
# Quick way to see exactly how many are still unconfirmed:
sqlite3 database/<environment>/config.db \
  "SELECT source, COUNT(*) FROM areas GROUP BY source; \
   SELECT source, COUNT(*) FROM systems GROUP BY source;"
```

## 2. Energy tariff

**Page:** Factory Configuration → Energy Tariff tab.

A brand-new scope (factory-wide, or any plant) auto-generates a
`SIMULATION TARIFF` placeholder (RM 0.50/kWh, flat) the first time it's
needed, so cost calculations always show *something* rather than
nothing - this is clearly marked with a `⚠️ SIMULATION TARIFF...`
warning wherever it's used. Enter the real tariff (simple flat rate,
or advanced mode with peak/off-peak/demand-charge fields) for each
scope that matters. The old placeholder is never overwritten - it's
closed out and preserved as history, so nothing about past
calculations changes retroactively.

**Without this step:** every cost figure across Energy Dashboard,
Energy Opportunities, and Savings Verification stays clearly labeled
"Simulated" - never presented as real money, but also not usable for
real financial decisions.

## 3. Equipment engineering thresholds and scoring weights

Every rule/weight/threshold that drives Anomaly Detection, Equipment
Health, Asset Performance, Maintenance Intelligence, and Energy
Opportunity is tagged with a `provenance` value - `SIMULATION_TUNING`
means "a reasonable engineering-judgment starting point for this
dev-stage simulation, not a manufacturer limit or site-validated
figure." **Do not treat these as ready-made real-plant limits without
review** - and this checklist deliberately does NOT propose new values
(that requires real site engineering knowledge this project doesn't
have).

Where the actual values live, if you want to review or eventually
override them:

| Domain | Registry file | What it defines |
|---|---|---|
| Anomaly detection | `engine/anomaly_targets.py` | Deviation thresholds, persistence windows, per-tag-type rules |
| Equipment Health | `engine/health_targets.py` | Factor weights, severity penalties, band cutoffs (0-100 score) |
| Asset Performance | `engine/performance_targets.py` | State-change cut points (Improving/Stable/Degrading) |
| Maintenance Intelligence | `engine/maintenance_intelligence_targets.py` | Priority band cutoffs |
| Energy Opportunities | `engine/opportunity_targets.py` | Which anomaly patterns qualify as an opportunity |

The provenance vocabulary (`USER_CONFIGURED` / `ENGINEERING_RULE` /
`MANUFACTURER_REFERENCE` / `STATISTICAL` / `SIMULATION_TUNING`) already
exists precisely so a value can be marked as upgraded once it's been
validated against real manufacturer specs or site history - changing
one is a deliberate engineering decision for whoever owns that
domain's registry file, not something to batch-edit blindly.

Setpoints (the low/high warning/alarm limits Live Data, SCADA, and
Event Records actually alarm against) are separate from the above and
already fully site-editable today via the **Setpoints** page - no
registry-file editing needed for those.

## 4. Equipment manufacturer documentation

**Page:** Documentation.

25 of 28 equipment brand/model combinations have real, sourced
manufacturer manuals. The remaining 3 (Eaton 9395 UPS, Donaldson Torit
dust collector, IMA filling line) are synthetic placeholders - real
sourcing was genuinely attempted and did not succeed. These are now
visibly marked `⚠️ Synthetic placeholder` on the Documentation page
itself (added this phase) so this is never mistaken for a real
manufacturer document, including inside Ask AI's root-cause answers
(which quote from whatever documentation exists). Replace with a real
manual via the Upload panel if one becomes available.

## 5. Real PLC connection

**Page:** PLC Connectivity (Admin).

Still simulator-only as of this writing - drivers for Modbus/OPC UA/S7/
FINS exist and are tested, but this system has never been pointed at
real plant hardware. Use the OPC UA "Browse" picker (or the equivalent
manual mapping for other protocols) to map real tags, confirm the
type-compatibility badges are all ✅, then switch the active connection.
Validate end-to-end (Live Data showing real values, an Event Record
firing from a real threshold crossing, an Ask AI question answered
from real history) before treating a plant as live.

## 6. User accounts

**Page:** User Management (Admin).

Default demo accounts (if any remain) should be deactivated or have
their passwords reset before go-live - do this from this page, not by
editing the database directly, so the change is captured in the audit
log. As of this phase, accounts also lock out automatically after 5
failed sign-in attempts (15-minute cooldown, or an admin password
reset clears it immediately) - no setup needed, already active.

## 7. Backup destination and retention

**Page:** none yet (config file) - see `FACTORY_AI_DEVELOPMENT_STATUS.md`'s
Phase V1.2 entry for the full mechanism.

Confirm `config/settings.ini`'s `[HISTORIAN_MAINTENANCE]` `backup_destination_dir`
points somewhere with real, durable storage for the target deployment
(not just this VM's default local `backups/` directory) before relying
on it for disaster recovery - a local-disk-only backup doesn't protect
against losing that disk.

## 8. Equipment/tag configuration completeness

**Page:** Factory Configuration / Equipment & Tag Configuration (Admin).

A deterministic, weighted completeness score (`engine/configuration_
completeness.py`) blending the factory profile, every plant, and every
classified equipment instance - not a field count, since engineering-
significant fields (e.g. rated power/pressure) are weighted above
purely administrative ones. Shown on the New-Factory Setup Readiness
page along with the highest-weight missing fields, ranked.

## What this checklist deliberately does NOT cover

Real production-system integration (Production Context is still fully
simulated batch data), a guided setup wizard UI (all of the above is
done through existing, separate admin pages today, not a single funnel),
and GPU/cloud-API hardware for faster Ask AI responses - all explicitly
deferred, see `FACTORY_AI_DEVELOPMENT_STATUS.md`'s V1 roadmap review for
the full Must/Recommended/V2 breakdown.
