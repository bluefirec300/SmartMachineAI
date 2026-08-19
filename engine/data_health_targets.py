from __future__ import annotations

"""
Phase 16.1 - Data Health engine registry. Mirrors the established
*_targets.py convention (health_targets.py, maintenance_intelligence_targets.py,
etc.): every weight/threshold is a named, documented constant here, never
scattered magic numbers inside the engine module.

Data Health is a DISTINCT concept from Equipment Health (engine/health_targets.py):
Equipment Health asks "does the EVIDENCE suggest this equipment needs
engineering attention?" Data Health asks "can the TELEMETRY feeding that
evidence (and every other Phase 6-15 analytic) actually be trusted right
now?" A pump can be mechanically fine while its sensor feed is stale, and
a pump's data can be perfectly fresh/valid while the pump itself is in
poor condition. Nothing in this module reads or writes Equipment Health
data, and nothing in health_engine.py reads this module.

No LLM anywhere in this module. No database writes. No persistence -
Phase 16.1 is calculate-on-demand only, mirroring Phase 13's own
architecture decision for the identical reason (cheap to recompute from
already-indexed evidence).
"""

DATA_HEALTH_MODEL_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Required-tag resolution (item D). Reuses engine.baseline_targets'
# EQUIPMENT_TYPE_PROFILES.raw_targets - the existing, curated registry of
# which concrete tags actually feed Phase 8-14's analytics pipeline - as
# the definition of "telemetry required for this equipment." Deliberately
# NOT engine.health_targets.HEALTH_FACTOR_REGISTRY: that registry is a
# narrower, Health-Score-specific curation built ON TOP of baseline
# targets (and explicitly excludes some equipment/family combinations,
# e.g. air_compressor's CONDITION family, per its own documented
# exclusions) - baseline_targets.py's raw_targets is the more complete
# and more directly appropriate source for "what telemetry does the
# analytics pipeline as a whole depend on." Derived targets (tag_name is
# None - e.g. "cop", "flow_per_kw") are excluded from the required-tag
# list itself (there is no single concrete tag to check); their own raw
# inputs are already separately covered by other raw_targets entries for
# the same equipment type, so nothing is silently unchecked.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Freshness (item I). A tag is STALE if the time since its last logged
# sample exceeds its OWN configured logging_interval_seconds times this
# multiplier - generous relative to cadence, the same principle already
# used by engine.energy_kpi_engine.STALE_READING_SECONDS ("generous
# relative to Power_kW's 10s log cadence"), just generalized to whatever
# cadence each individual tag is actually configured for (10s/30s/60s/
# 300s all coexist live) instead of one universal constant.
# ---------------------------------------------------------------------------

FRESHNESS_INTERVAL_MULTIPLIER = 5.0

# Fallback threshold ONLY for the small number of legacy tags with
# neither logging_interval_seconds NOR log_on_change set (the "log every
# poll" fallback tier documented in app/plc_logger.py) - these have no
# configured cadence to multiply, so a fixed, documented fallback applies
# instead. Chosen to match the plc_logger.py scan_interval default order
# of magnitude with the same "generous" principle.
FRESHNESS_FALLBACK_SECONDS = 300.0

# log_on_change tags (item I): absence of a new row does NOT mean
# communication failure - a legitimately unchanged value produces no new
# row by design. Phase 16.1 cannot deterministically distinguish "still
# fine, just unchanged" from "source went quiet" for one isolated
# log_on_change tag using only that tag's own timestamp, so its freshness
# is reported as INDETERMINATE rather than guessed - and scored as a
# fixed neutral credit (neither rewarded nor punished) in the Freshness
# component, so it can never inflate NOR deflate Data Confidence.
FRESHNESS_INDETERMINATE_SCORE = 50.0

# ---------------------------------------------------------------------------
# Continuity (item J). Bounded lookback only - no full historian scans.
# log_on_change tags are exempt from gap logic entirely (a fixed-period
# gap check is meaningless for a tag that only logs on real change).
# ---------------------------------------------------------------------------

CONTINUITY_LOOKBACK_HOURS = 4
CONTINUITY_GAP_INTERVAL_MULTIPLIER = 5.0

# ---------------------------------------------------------------------------
# Timestamp health (item K).
# ---------------------------------------------------------------------------

FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 5.0

# ---------------------------------------------------------------------------
# Frozen/stuck detection (item G) - ADVISORY ONLY in Phase 16.1. Never
# feeds into any of the four confidence components. Eligibility (checked
# in engine/data_health_engine.py, defensively, not merely inherited from
# which tags happen to be passed in):
#   - log_on_change must be 0/false (a fixed-interval, continuously-
#     logged tag - a log_on_change tag holding one value IS the tag
#     correctly doing its job, never "stuck").
#   - data_type must be a real numeric/continuous type, never
#     BOOL/BOOLEAN/STRING (a discrete status/state tag legitimately
#     holds one value for long periods; that is not sensor evidence of
#     anything).
#   - Requires FROZEN_MIN_CONSECUTIVE_SAMPLES identical consecutive
#     values spanning at least FROZEN_MIN_WINDOW_INTERVAL_MULTIPLIER x
#     the tag's own logging_interval_seconds (a real elapsed duration,
#     not just "a few samples that happen to be close together").
#   - Requires CORROBORATION: at least one other required tag on the SAME
#     equipment instance must be independently FRESH at the same time -
#     otherwise the whole equipment/source has gone quiet together, which
#     is a freshness/continuity/interruption story, never a "this one
#     sensor is stuck" diagnosis for any individual tag in that group.
# These thresholds are NOT yet empirically validated against real
# multi-day/multi-week factory history - deliberately kept advisory and
# excluded from the score until they can be tuned (per explicit
# direction), not because the detection logic itself is incomplete.
# ---------------------------------------------------------------------------

FROZEN_MIN_CONSECUTIVE_SAMPLES = 6
FROZEN_MIN_WINDOW_INTERVAL_MULTIPLIER = 10.0
FROZEN_INELIGIBLE_DATA_TYPES = ("BOOL", "BOOLEAN", "STRING")

# ---------------------------------------------------------------------------
# Data Confidence formula (item C) - four independently-evidenced
# components (item B: NEVER derived from the single mutually-exclusive
# per-tag display state). Weights sum to 1.0.
# ---------------------------------------------------------------------------

COMPONENT_WEIGHT_FRESHNESS = 0.35
COMPONENT_WEIGHT_AVAILABILITY = 0.25
COMPONENT_WEIGHT_VALIDITY = 0.25
COMPONENT_WEIGHT_CONTINUITY = 0.15

COMPONENT_WEIGHTS: dict[str, float] = {
    "freshness": COMPONENT_WEIGHT_FRESHNESS,
    "availability": COMPONENT_WEIGHT_AVAILABILITY,
    "validity": COMPONENT_WEIGHT_VALIDITY,
    "continuity": COMPONENT_WEIGHT_CONTINUITY,
}

STATUS_GOOD = "GOOD"
STATUS_DEGRADED = "DEGRADED"
STATUS_POOR = "POOR"
STATUS_UNAVAILABLE = "UNAVAILABLE"

CONFIDENCE_STATUS_VALUES = (STATUS_GOOD, STATUS_DEGRADED, STATUS_POOR, STATUS_UNAVAILABLE)

CONFIDENCE_GOOD_MIN = 85.0
CONFIDENCE_DEGRADED_MIN = 60.0
# Below CONFIDENCE_DEGRADED_MIN (and score is not None) -> POOR.
# score is None (never a fabricated 0) -> UNAVAILABLE.


def confidence_status_for_score(score: float | None) -> str:
    if score is None:
        return STATUS_UNAVAILABLE
    if not (0.0 <= score <= 100.0):
        raise ValueError(f"score {score!r} outside the defined 0-100 range")
    if score >= CONFIDENCE_GOOD_MIN:
        return STATUS_GOOD
    if score >= CONFIDENCE_DEGRADED_MIN:
        return STATUS_DEGRADED
    return STATUS_POOR


# ---------------------------------------------------------------------------
# Per-tag EVIDENCE vocabularies (item B) - independent dimensions, never
# collapsed into each other.
# ---------------------------------------------------------------------------

FRESHNESS_FRESH = "FRESH"
FRESHNESS_STALE = "STALE"
FRESHNESS_INDETERMINATE = "INDETERMINATE"
# No freshness value at all (tag has never produced data) is represented
# as None, not a fourth vocabulary member - availability is a separate
# dimension and already carries that fact.

VALIDITY_VALID = "VALID"
VALIDITY_INVALID = "INVALID"
# Like freshness, "no value to validate" is None, not a vocabulary member.

# ---------------------------------------------------------------------------
# Per-tag DISPLAY state (item L) - a single, mutually-exclusive,
# human-readable summary computed AFTER the independent dimensions above
# are known, priority-ordered worst-first. Used for presentation/summary
# lists only - NEVER fed back into the four component calculations
# (item B's core requirement).
# ---------------------------------------------------------------------------

TAG_DISPLAY_MISSING = "MISSING"
TAG_DISPLAY_INVALID = "INVALID"
TAG_DISPLAY_STALE = "STALE"
TAG_DISPLAY_FROZEN_CANDIDATE = "FROZEN_CANDIDATE"
TAG_DISPLAY_FRESH = "FRESH"
TAG_DISPLAY_INDETERMINATE = "INDETERMINATE"
