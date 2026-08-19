from __future__ import annotations

"""
Phase 12.2 - Equipment Health PERSISTENCE policy constants. Deliberately
separate from engine/health_targets.py (Phase 12.1's accepted scoring
registry, which this phase must not modify) - everything here governs
WHEN a calculated result gets written to history, never HOW the score
itself is computed. Centralized here so no threshold is scattered
through worker/orchestration code (mirrors item 7's instruction).
"""

# A health_score difference at or above this many points, between the
# current calculation and the latest persisted snapshot for the same
# equipment, counts as a material change worth a new history row.
# Reused as the trend-direction threshold too (engine/health_history.py)
# - the same "is this a real change" judgment applies to both questions.
SCORE_CHANGE_THRESHOLD = 3.0

# Even with zero material change, persist a heartbeat snapshot at least
# this often - preserves trend continuity ("assessment continued
# running and remained healthy") without spamming history for 34
# pieces of equipment that mostly don't change cycle-to-cycle.
HEARTBEAT_MAX_INTERVAL_HOURS = 24.0

# "Material factor composition change" (item 7): the SET of factor_ids
# currently in "penalized" status differs from the latest persisted
# snapshot's penalized set - a new finding appeared or an existing one
# fully resolved, independent of whether the score moved past
# SCORE_CHANGE_THRESHOLD yet.
