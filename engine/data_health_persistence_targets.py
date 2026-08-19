from __future__ import annotations

"""
Phase 16.5 - Data Health snapshot PERSISTENCE policy constants.
Deliberately separate from engine/data_health_targets.py (Phase 16.1's
accepted scoring registry, which this phase must not modify) - this
module governs WHEN a calculated result gets written to history, never
HOW the score itself is computed. Mirrors
engine/health_persistence_targets.py's own split exactly.

No retention/pruning policy exists in this phase (intentional current
design decision, not an oversight - the Phase 16.5 architecture
checkpoint's own storage-growth estimate put projected volume at
roughly 12,000-36,000 rows/year, two-plus orders of magnitude below
anything that would make pruning worth the added complexity today;
equipment_health_snapshots itself, the closest precedent, has never
needed retention logic either).
"""

TICK_INTERVAL_SECONDS = 300.0  # 5 min - see app/data_health_history_worker.py's own docstring for the full justification

# A confidence_score difference at or above this many points, between
# the current evaluation and the latest persisted snapshot, counts as a
# material change worth a new history row. Mirrors
# engine/health_persistence_targets.py's SCORE_CHANGE_THRESHOLD exactly
# (same "is this a real change" judgment, same numeric value, for
# consistency across the two persisted-history systems).
CONFIDENCE_SCORE_CHANGE_THRESHOLD = 3.0

# A component score (Freshness/Availability/Validity/Continuity)
# difference at or above this many points counts as material. Chosen
# deliberately ABOVE any possible floating-point/rounding noise (every
# component score is already rounded to 1 decimal place by Phase 16.1)
# but comfortably BELOW the smallest real change a single tag flipping
# state can produce - each component score moves in increments of
# 100/required_tag_count points (e.g. 20pt for a 5-tag equipment, 33pt
# for a 3-tag one), so a 5.0pt threshold only filters genuine noise,
# never a real single-tag change.
COMPONENT_SCORE_CHANGE_THRESHOLD = 5.0

# Even with zero material change, persist a heartbeat snapshot at least
# this often - preserves status-duration continuity ("Data Health
# continued evaluating and remained GOOD") without spamming history for
# 34 pieces of equipment that mostly don't change cycle-to-cycle.
# Mirrors engine/health_persistence_targets.py's
# HEARTBEAT_MAX_INTERVAL_HOURS exactly.
HEARTBEAT_MAX_INTERVAL_HOURS = 24.0

# Integer count fields where ANY difference (not a threshold) is
# material - these are already small, discrete, directly-meaningful
# units (a missing/stale/invalid tag either is or isn't), so unlike the
# continuous score fields above, no noise-filtering threshold applies.
COUNT_CHANGE_FIELDS: tuple[str, ...] = (
    "missing_tag_count", "stale_tag_count", "invalid_tag_count", "gap_count",
    "timestamp_issue_count", "indeterminate_freshness_count", "frozen_candidate_count",
)

# The issue-type -> persisted count-column mapping recurring_issues()
# uses for its "inactive -> active = new occurrence" counting (item 15).
# Kept here, not hardcoded in engine/data_health_history.py, so the
# vocabulary stays in one place alongside the other persistence-layer
# constants.
RECURRING_ISSUE_FIELDS: dict[str, str] = {
    "MISSING": "missing_tag_count",
    "STALE": "stale_tag_count",
    "INVALID": "invalid_tag_count",
    "CONTINUITY_GAP": "gap_count",
    "TIMESTAMP_ISSUE": "timestamp_issue_count",
    "INDETERMINATE_CHANGE_ONLY": "indeterminate_freshness_count",
    "FROZEN_CANDIDATE": "frozen_candidate_count",
}
