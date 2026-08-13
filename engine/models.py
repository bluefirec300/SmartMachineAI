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
