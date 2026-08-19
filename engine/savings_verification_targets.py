from __future__ import annotations

from dataclasses import dataclass

"""
Phase 11.1 - Savings Verification domain vocabulary. Schema/constants
only - no calculation logic, no automatic status progression (that's
Phase 11.3/11.4's job).

Architectural separation (per your Phase 11.1 direction):
  energy_opportunities -> savings_interventions -> savings_verification_results

  - savings_interventions answers "what action did the engineer take?"
    Its own `status` is a COARSE process-phase rollup, owned entirely
    by the intervention entity.
  - savings_verification_results answers "what did the verification
    engine conclude at a particular evaluation?" Its `result` is the
    fine-grained OUTCOME of exactly one evaluation attempt. The table
    is append-only in concept - a later phase's worker INSERTs a new
    row per evaluation, never UPDATEs a prior one, so financial
    evidence from an earlier evaluation is never destroyed by a later
    one (item: "future evaluations do not destroy previous financial
    evidence").

Non-overlapping vocabulary, deliberately: INTERVENTION_STATUS_VALUES
never contains a verification outcome (VERIFIED/REJECTED/INCONCLUSIVE
appear ONLY in VERIFICATION_RESULT_VALUES), and VERIFICATION_RESULT_VALUES
never contains a process-phase word (PLANNED/IMPLEMENTED appear ONLY in
INTERVENTION_STATUS_VALUES). COMPLETED is the intervention's terminal
state precisely because it's neutral - it says "the verification
process has concluded," not "it succeeded" - the actual outcome is only
ever found by reading the latest savings_verification_results row.
"""

# ---------------------------------------------------------------------------
# Intervention lifecycle (savings_interventions.status)
# ---------------------------------------------------------------------------

INTERVENTION_STATUS_PLANNED = "PLANNED"
INTERVENTION_STATUS_IMPLEMENTED = "IMPLEMENTED"
INTERVENTION_STATUS_VERIFICATION_PENDING = "VERIFICATION_PENDING"
INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS = "VERIFICATION_IN_PROGRESS"
INTERVENTION_STATUS_COMPLETED = "COMPLETED"

INTERVENTION_STATUS_VALUES = (
    INTERVENTION_STATUS_PLANNED,
    INTERVENTION_STATUS_IMPLEMENTED,
    INTERVENTION_STATUS_VERIFICATION_PENDING,
    INTERVENTION_STATUS_VERIFICATION_IN_PROGRESS,
    INTERVENTION_STATUS_COMPLETED,
)

# The only terminal intervention status - once reached, the partial
# unique index permits a NEW intervention to be recorded against the
# same opportunity (e.g. a second attempt after a REJECTED outcome, or
# a fresh recurrence after a VERIFIED one - see
# engine/savings_verification_migrator.py's index).
INTERVENTION_TERMINAL_STATUSES = (INTERVENTION_STATUS_COMPLETED,)

# ---------------------------------------------------------------------------
# Verification outcome (savings_verification_results.result) - one row
# per evaluation attempt, append-only.
# ---------------------------------------------------------------------------

VERIFICATION_RESULT_VERIFIED = "VERIFIED"
VERIFICATION_RESULT_REJECTED = "REJECTED"
VERIFICATION_RESULT_INCONCLUSIVE = "INCONCLUSIVE"

VERIFICATION_RESULT_VALUES = (
    VERIFICATION_RESULT_VERIFIED,
    VERIFICATION_RESULT_REJECTED,
    VERIFICATION_RESULT_INCONCLUSIVE,
)

# ---------------------------------------------------------------------------
# Evidence-quality tier (savings_verification_results.confidence) - a
# deterministic classification, never a numeric AI score. Defined here
# now so the column/vocabulary exists; the actual classification logic
# is Phase 11.3's job.
# ---------------------------------------------------------------------------

CONFIDENCE_STRONG = "STRONG"
CONFIDENCE_CONTEXT_MATCHED = "CONTEXT_MATCHED"
CONFIDENCE_LIMITED = "LIMITED"
CONFIDENCE_INSUFFICIENT = "INSUFFICIENT"

CONFIDENCE_VALUES = (
    CONFIDENCE_STRONG,
    CONFIDENCE_CONTEXT_MATCHED,
    CONFIDENCE_LIMITED,
    CONFIDENCE_INSUFFICIENT,
)

# ---------------------------------------------------------------------------
# Action-category registry - curated, extensible, mirrors
# engine/anomaly_targets.py / engine/opportunity_targets.py's plain-
# tuple-plus-dict pattern rather than a full rule dataclass, since
# Phase 11.1 has no rule-matching logic to attach to each category.
# ---------------------------------------------------------------------------

ACTION_CATEGORY_CONTROL_ADJUSTMENT = "CONTROL_ADJUSTMENT"
ACTION_CATEGORY_SETPOINT_CHANGE = "SETPOINT_CHANGE"
ACTION_CATEGORY_SCHEDULE_CHANGE = "SCHEDULE_CHANGE"
ACTION_CATEGORY_MAINTENANCE = "MAINTENANCE"
ACTION_CATEGORY_REPAIR = "REPAIR"
ACTION_CATEGORY_EQUIPMENT_REPLACEMENT = "EQUIPMENT_REPLACEMENT"
ACTION_CATEGORY_PROCESS_CHANGE = "PROCESS_CHANGE"
ACTION_CATEGORY_OPERATOR_PRACTICE = "OPERATOR_PRACTICE"
ACTION_CATEGORY_OTHER = "OTHER"

ACTION_CATEGORY_VALUES = (
    ACTION_CATEGORY_CONTROL_ADJUSTMENT,
    ACTION_CATEGORY_SETPOINT_CHANGE,
    ACTION_CATEGORY_SCHEDULE_CHANGE,
    ACTION_CATEGORY_MAINTENANCE,
    ACTION_CATEGORY_REPAIR,
    ACTION_CATEGORY_EQUIPMENT_REPLACEMENT,
    ACTION_CATEGORY_PROCESS_CHANGE,
    ACTION_CATEGORY_OPERATOR_PRACTICE,
    ACTION_CATEGORY_OTHER,
)

# Default stabilization period per category - a disclosed, tunable
# engineering judgment call (same honesty standard as every prior
# phase's thresholds - not derived from real commissioning data, none
# exists yet for this phase). An intervention's own stabilization_days
# column may override this default; this mapping is only ever a
# starting suggestion, applied by a later UI/engine phase, never
# written automatically by Phase 11.1 itself.
DEFAULT_STABILIZATION_DAYS = {
    ACTION_CATEGORY_CONTROL_ADJUSTMENT: 1,
    ACTION_CATEGORY_SETPOINT_CHANGE: 2,
    ACTION_CATEGORY_SCHEDULE_CHANGE: 3,
    ACTION_CATEGORY_MAINTENANCE: 3,
    ACTION_CATEGORY_REPAIR: 3,
    ACTION_CATEGORY_EQUIPMENT_REPLACEMENT: 7,
    ACTION_CATEGORY_PROCESS_CHANGE: 7,
    ACTION_CATEGORY_OPERATOR_PRACTICE: 3,
    ACTION_CATEGORY_OTHER: 3,
}


@dataclass(frozen=True)
class ActionCategory:
    code: str
    default_stabilization_days: int


def get_action_category(code: str) -> ActionCategory | None:
    if code not in ACTION_CATEGORY_VALUES:
        return None
    return ActionCategory(code=code, default_stabilization_days=DEFAULT_STABILIZATION_DAYS[code])
