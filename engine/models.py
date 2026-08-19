from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Any

@dataclass(frozen=True)
class Concepts:
    question: str
    normalized: str
    intent: str = "current_data"
    equipment_terms: tuple[str, ...] = ()
    measurement: str = ""
    location: str = ""
    condition: str = ""
    event_type: str = ""
    threshold_type: str = ""
    time_expression: str = "latest"
    plant: str = ""
    # Set only for a two-period comparison question ("compare X for
    # today and yesterday", "X on Aug 10 vs Aug 12") - each is a
    # time_expression string using the same vocabulary as
    # time_expression above ("today"/"yesterday"/"days_ago:N") plus a
    # new "date:YYYY-MM-DD" form for an explicit calendar date. Both
    # empty unless intent == "comparison".
    compare_period_a: str = ""
    compare_period_b: str = ""
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True)
class Tag:
    id: int
    tag_name: str
    description: str
    driver: str
    address: str
    data_type: str
    unit: str
    equipment_name: str
    equipment_display_name: str
    aliases: tuple[str, ...]
    measurement: str
    location: str
    signal_type: str
    event_type: str
    threshold_type: str
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True)
class Candidate:
    tag: Tag
    score: float
    reasons: tuple[str, ...] = ()
    def to_dict(self) -> dict[str, Any]:
        return {"tag": self.tag.to_dict(), "score": self.score, "reasons": list(self.reasons)}

@dataclass
class EquipmentCandidate:
    """One equipment-level candidate from IndustrialQueryEngine.resolve_equipment() -
    Phase 15's equipment-only counterpart to Candidate (which is tag-level).
    equipment_score is the same 0.0-1.0 scale _equipment_score() already
    produces (never the *40-scaled tag-ranking score)."""
    equipment_name: str
    equipment_display_name: str
    instance_key: str
    plant: str
    equipment_score: float
    def to_dict(self) -> dict[str, Any]:
        return {
            "equipment_name": self.equipment_name,
            "equipment_display_name": self.equipment_display_name,
            "instance_key": self.instance_key,
            "plant": self.plant,
            "equipment_score": self.equipment_score,
        }

@dataclass
class EquipmentResolution:
    """Result of IndustrialQueryEngine.resolve_equipment() - mirrors
    QueryResult's status/candidates/confidence shape so callers can reuse
    the same "resolved / clarification_required / no_match" handling and
    the same numbered-menu rendering pattern, just at equipment
    granularity instead of tag granularity."""
    question: str
    status: str
    instance_key: str = ""
    equipment_name: str = ""
    equipment_display_name: str = ""
    plant: str = ""
    confidence: float = 0.0
    message: str = ""
    candidates: list[EquipmentCandidate] = field(default_factory=list)
    # True when the question named ANY equipment-like term at all (even
    # if resolution failed/was ambiguous) - lets a caller distinguish
    # "you named something I couldn't resolve" (surface a clarification)
    # from "you named nothing" (safe to fall back to prior session
    # context for a bare follow-up question). See app/ask.py's
    # AskEngine._answer_equipment_interpretation() - Phase 15's
    # "explicit new equipment always overrides previous context, never
    # silently reused" rule depends on this distinction.
    had_equipment_terms: bool = False
    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "instance_key": self.instance_key,
            "equipment_name": self.equipment_name,
            "equipment_display_name": self.equipment_display_name,
            "plant": self.plant,
            "confidence": self.confidence,
            "message": self.message,
            "candidates": [c.to_dict() for c in self.candidates],
            "had_equipment_terms": self.had_equipment_terms,
        }

@dataclass
class EquipmentComparisonResolution:
    """Phase 15 multi-entity comparison resolution - two INDEPENDENTLY
    resolved EquipmentResolution objects, never one resolver call re-run
    against the whole question twice (see
    IndustrialQueryEngine.resolve_equipment_pair())."""
    question: str
    status: str  # "resolved" | "clarification_required" | "no_match" | "could_not_split" | "duplicate_entity"
    entity_a: "EquipmentResolution | None" = None
    entity_b: "EquipmentResolution | None" = None
    message: str = ""

@dataclass
class QueryResult:
    question: str
    status: str
    intent: str
    time_expression: str
    selected_tag: str = ""
    equipment: str = ""
    measurement: str = ""
    location: str = ""
    condition: str = ""
    confidence: float = 0.0
    message: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    concepts: Concepts | None = None
    elapsed_seconds: float = 0.0
    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "intent": self.intent,
            "time_expression": self.time_expression,
            "selected_tag": self.selected_tag,
            "equipment": self.equipment,
            "measurement": self.measurement,
            "location": self.location,
            "condition": self.condition,
            "confidence": self.confidence,
            "message": self.message,
            "candidates": [c.to_dict() for c in self.candidates],
            "concepts": self.concepts.to_dict() if self.concepts else None,
            "elapsed_seconds": self.elapsed_seconds,
        }
