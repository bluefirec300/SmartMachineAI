from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ai.query_dispatcher import (
    DispatchResult,
    QueryDispatcher,
    QueryType,
)
from ai.router import route_question
from ai.semantic_tag_resolver import (
    SemanticResolution,
    SemanticTagResolver,
)


@dataclass(frozen=True)
class TimeContext:
    """
    Time information extracted from a user's question.

    start_time and end_time remain None when the question does not
    contain a supported time expression.
    """

    expression: str = "latest"
    start_time: datetime | None = None
    end_time: datetime | None = None
    history_limit: int = 1


@dataclass
class QuestionUnderstanding:
    """
    Structured interpretation of one user question.

    Other SmartMachineAI modules should use this object instead of
    independently parsing the original question.
    """

    original_question: str
    intent: QueryType
    equipment: str
    primary_tag: str | None
    related_tags: list[str] = field(default_factory=list)
    measurements: list[str] = field(default_factory=list)
    time_context: TimeContext = field(default_factory=TimeContext)
    confidence: float = 0.0
    dispatcher_reason: str = ""
    tag_resolution_method: str = ""
    route_source: str = ""
    semantic_candidates: list[str] = field(default_factory=list)

    @property
    def all_tags(self) -> list[str]:
        """
        Return primary and related tags without duplicates.
        """
        tags: list[str] = []

        if self.primary_tag:
            tags.append(self.primary_tag)

        for tag in self.related_tags:
            if tag and tag not in tags:
                tags.append(tag)

        return tags

    def to_route(self) -> dict[str, Any]:
        """
        Convert the understanding object into the route format already
        used by SmartMachineBridge.

        This allows gradual migration without immediately rewriting all
        existing handlers.
        """
        return {
            "equipment": self.equipment,
            "measurements": list(self.measurements),
            "intent": self._legacy_intent(),
            "tags": self.all_tags or None,
            "history_limit": self.time_context.history_limit,
            "route_source": self.route_source,
            "primary_tag": self.primary_tag,
            "related_tags": list(self.related_tags),
            "time_expression": self.time_context.expression,
            "time_start": self.time_context.start_time,
            "time_end": self.time_context.end_time,
            "understanding_confidence": self.confidence,
        }

    def _legacy_intent(self) -> str:
        """
        Convert QueryType into the intent names expected by the old
        routing and historian code.
        """
        if self.intent == QueryType.CURRENT_DATA:
            return "current"

        if self.intent == QueryType.TIMELINE:
            return "timeline"

        if self.intent == QueryType.ROOT_CAUSE:
            return "root_cause"

        if self.intent == QueryType.THRESHOLD:
            return "threshold"

        return "general"


class TimeContextParser:
    """
    Deterministic parser for common machine-history time expressions.

    This parser intentionally handles only clear expressions. It does
    not invent a time range when the user's wording is unclear.
    """

    @staticmethod
    def normalize_text(value: str) -> str:
        text = str(value).lower().strip()

        text = re.sub(
            r"[^a-z0-9_\s]",
            " ",
            text,
        )

        text = re.sub(
            r"\s+",
            " ",
            text,
        )

        return text.strip()

    @staticmethod
    def _start_of_day(value: datetime) -> datetime:
        return value.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    @staticmethod
    def _end_of_day(value: datetime) -> datetime:
        return value.replace(
            hour=23,
            minute=59,
            second=59,
            microsecond=999999,
        )

    def parse(
        self,
        question: str,
        query_type: QueryType,
        now: datetime | None = None,
    ) -> TimeContext:
        current_time = now or datetime.now()
        text = self.normalize_text(question)

        today_start = self._start_of_day(
            current_time
        )

        if "day before yesterday" in text:
            target_day = (
                current_time
                - timedelta(days=2)
            )

            return TimeContext(
                expression="day before yesterday",
                start_time=self._start_of_day(
                    target_day
                ),
                end_time=self._end_of_day(
                    target_day
                ),
                history_limit=500,
            )

        if "yesterday" in text:
            target_day = (
                current_time
                - timedelta(days=1)
            )

            return TimeContext(
                expression="yesterday",
                start_time=self._start_of_day(
                    target_day
                ),
                end_time=self._end_of_day(
                    target_day
                ),
                history_limit=500,
            )

        if "today" in text:
            return TimeContext(
                expression="today",
                start_time=today_start,
                end_time=current_time,
                history_limit=500,
            )

        if (
            "last hour" in text
            or "past hour" in text
        ):
            return TimeContext(
                expression="last hour",
                start_time=(
                    current_time
                    - timedelta(hours=1)
                ),
                end_time=current_time,
                history_limit=500,
            )

        hour_match = re.search(
            r"\b(?:last|past)\s+"
            r"(\d+)\s+hours?\b",
            text,
        )

        if hour_match:
            hours = max(
                1,
                int(hour_match.group(1)),
            )

            return TimeContext(
                expression=f"last {hours} hours",
                start_time=(
                    current_time
                    - timedelta(hours=hours)
                ),
                end_time=current_time,
                history_limit=1000,
            )

        minute_match = re.search(
            r"\b(?:last|past)\s+"
            r"(\d+)\s+minutes?\b",
            text,
        )

        if minute_match:
            minutes = max(
                1,
                int(minute_match.group(1)),
            )

            return TimeContext(
                expression=f"last {minutes} minutes",
                start_time=(
                    current_time
                    - timedelta(minutes=minutes)
                ),
                end_time=current_time,
                history_limit=1000,
            )

        day_match = re.search(
            r"\b(?:last|past)\s+"
            r"(\d+)\s+days?\b",
            text,
        )

        if day_match:
            days = max(
                1,
                int(day_match.group(1)),
            )

            return TimeContext(
                expression=f"last {days} days",
                start_time=(
                    current_time
                    - timedelta(days=days)
                ),
                end_time=current_time,
                history_limit=2000,
            )

        if (
            "last week" in text
            or "past week" in text
        ):
            return TimeContext(
                expression="last week",
                start_time=(
                    current_time
                    - timedelta(days=7)
                ),
                end_time=current_time,
                history_limit=3000,
            )

        if (
            "recent" in text
            or "latest event" in text
            or "last event" in text
        ):
            return TimeContext(
                expression="recent",
                history_limit=50,
            )

        if query_type == QueryType.CURRENT_DATA:
            return TimeContext(
                expression="latest",
                history_limit=1,
            )

        if query_type == QueryType.THRESHOLD:
            return TimeContext(
                expression="configuration",
                history_limit=1,
            )

        if query_type == QueryType.TIMELINE:
            return TimeContext(
                expression="recent",
                history_limit=50,
            )

        if query_type == QueryType.ROOT_CAUSE:
            return TimeContext(
                expression="recent context",
                history_limit=100,
            )

        return TimeContext(
            expression="not specified",
            history_limit=10,
        )


class QuestionUnderstandingEngine:
    """
    Central language-understanding component for SmartMachineAI.

    Processing order:

        1. QueryDispatcher detects the question type.
        2. Existing deterministic router detects equipment and tags.
        3. SemanticTagResolver improves or supplies the primary tag.
        4. TimeContextParser extracts a supported time range.
        5. One QuestionUnderstanding object is returned.

    Existing router results are preserved when they are confident.
    Semantic resolution is mainly used when no precise tag was selected.
    """

    def __init__(
        self,
        dispatcher: QueryDispatcher | None = None,
        semantic_resolver: SemanticTagResolver | None = None,
        time_parser: TimeContextParser | None = None,
    ) -> None:
        self.dispatcher = (
            dispatcher
            or QueryDispatcher()
        )

        self.semantic_resolver = (
            semantic_resolver
            or SemanticTagResolver()
        )

        self.time_parser = (
            time_parser
            or TimeContextParser()
        )

    @staticmethod
    def _clean_string(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        return str(value).strip()

    @classmethod
    def _clean_tags(
        cls,
        values: Any,
    ) -> list[str]:
        if values is None:
            return []

        if isinstance(
            values,
            str,
        ):
            values = [
                values
            ]

        if not isinstance(
            values,
            (
                list,
                tuple,
                set,
            ),
        ):
            return []

        tags: list[str] = []

        for value in values:
            tag = cls._clean_string(
                value
            )

            if (
                tag
                and tag not in tags
            ):
                tags.append(tag)

        return tags

    @staticmethod
    def _candidate_names(
        semantic_result: SemanticResolution,
    ) -> list[str]:
        names: list[str] = []

        for candidate in semantic_result.candidates:
            tag_name = (
                candidate.tag.tag_name
            )

            if tag_name not in names:
                names.append(tag_name)

        return names

    @staticmethod
    def _combine_confidence(
        dispatch_result: DispatchResult,
        semantic_result: SemanticResolution | None,
        deterministic_tags: list[str],
    ) -> float:
        dispatch_confidence = max(
            0.0,
            min(
                1.0,
                float(
                    dispatch_result.confidence
                ),
            ),
        )

        if semantic_result is None:
            if deterministic_tags:
                return round(
                    min(
                        1.0,
                        dispatch_confidence + 0.10,
                    ),
                    3,
                )

            return round(
                dispatch_confidence,
                3,
            )

        if semantic_result.selected is not None:
            semantic_confidence = max(
                0.0,
                min(
                    1.0,
                    semantic_result.selected.similarity,
                ),
            )

            combined = (
                dispatch_confidence * 0.45
                + semantic_confidence * 0.55
            )

            if deterministic_tags:
                combined += 0.05

            return round(
                min(
                    1.0,
                    combined,
                ),
                3,
            )

        if deterministic_tags:
            return round(
                min(
                    1.0,
                    dispatch_confidence + 0.05,
                ),
                3,
            )

        return round(
            dispatch_confidence * 0.75,
            3,
        )

    def understand(
        self,
        question: str,
    ) -> QuestionUnderstanding:
        clean_question = str(
            question
        ).strip()

        if not clean_question:
            raise ValueError(
                "Question cannot be empty."
            )

        dispatch_result = (
            self.dispatcher.dispatch(
                clean_question
            )
        )

        existing_route = route_question(
            clean_question
        )

        equipment = self._clean_string(
            existing_route.get(
                "equipment",
                "factory",
            )
        ) or "factory"

        measurements = self._clean_tags(
            existing_route.get(
                "measurements"
            )
        )

        deterministic_tags = self._clean_tags(
            existing_route.get(
                "tags"
            )
        )

        route_source = self._clean_string(
            existing_route.get(
                "route_source",
                "rules",
            )
        ) or "rules"

        semantic_result: (
            SemanticResolution | None
        ) = None

        try:
            semantic_result = (
                self.semantic_resolver.resolve(
                    clean_question,
                    top_k=5,
                )
            )

        except Exception as exc:
            print(
                "[QuestionUnderstanding] "
                "Semantic resolver unavailable: "
                f"{exc}"
            )

        primary_tag: str | None = None
        related_tags: list[str] = []
        tag_resolution_method = "none"

        if deterministic_tags:
            primary_tag = (
                deterministic_tags[0]
            )

            related_tags = (
                deterministic_tags[1:]
            )

            tag_resolution_method = (
                "deterministic"
            )

            if (
                semantic_result is not None
                and semantic_result.resolved
                and semantic_result.tag_name
            ):
                semantic_tag = (
                    semantic_result.tag_name
                )

                if semantic_tag in deterministic_tags:
                    primary_tag = semantic_tag

                    related_tags = [
                        tag
                        for tag in deterministic_tags
                        if tag != semantic_tag
                    ]

                    tag_resolution_method = (
                        "deterministic_confirmed_semantically"
                    )

        elif (
            semantic_result is not None
            and semantic_result.resolved
            and semantic_result.tag_name
        ):
            primary_tag = (
                semantic_result.tag_name
            )

            tag_resolution_method = (
                semantic_result.method
            )

            route_source = (
                "semantic_tag_resolver"
            )

            selected_tag = (
                semantic_result.selected.tag
                if semantic_result.selected
                else None
            )

            if (
                selected_tag is not None
                and selected_tag.equipment_names
                and equipment == "factory"
            ):
                equipment = (
                    selected_tag.equipment_names[0]
                )

        time_context = (
            self.time_parser.parse(
                question=clean_question,
                query_type=(
                    dispatch_result.query_type
                ),
            )
        )

        confidence = (
            self._combine_confidence(
                dispatch_result=dispatch_result,
                semantic_result=semantic_result,
                deterministic_tags=deterministic_tags,
            )
        )

        semantic_candidates: list[str] = []

        if semantic_result is not None:
            semantic_candidates = (
                self._candidate_names(
                    semantic_result
                )
            )

        return QuestionUnderstanding(
            original_question=clean_question,
            intent=dispatch_result.query_type,
            equipment=equipment,
            primary_tag=primary_tag,
            related_tags=related_tags,
            measurements=measurements,
            time_context=time_context,
            confidence=confidence,
            dispatcher_reason=(
                dispatch_result.reason
            ),
            tag_resolution_method=(
                tag_resolution_method
            ),
            route_source=route_source,
            semantic_candidates=(
                semantic_candidates
            ),
        )


def format_question_understanding(
    understanding: QuestionUnderstanding,
) -> str:
    """
    Format an understanding result for terminal testing.
    """
    primary_tag = (
        understanding.primary_tag
        or "None"
    )

    related_tags = (
        ", ".join(
            understanding.related_tags
        )
        or "None"
    )

    measurements = (
        ", ".join(
            understanding.measurements
        )
        or "None"
    )

    candidates = (
        ", ".join(
            understanding.semantic_candidates
        )
        or "None"
    )

    start_time = (
        understanding.time_context.start_time.isoformat(
            sep=" ",
            timespec="seconds",
        )
        if understanding.time_context.start_time
        else "None"
    )

    end_time = (
        understanding.time_context.end_time.isoformat(
            sep=" ",
            timespec="seconds",
        )
        if understanding.time_context.end_time
        else "None"
    )

    lines = [
        (
            "Question: "
            f"{understanding.original_question}"
        ),
        (
            "Intent: "
            f"{understanding.intent.value}"
        ),
        (
            "Equipment: "
            f"{understanding.equipment}"
        ),
        (
            "Primary tag: "
            f"{primary_tag}"
        ),
        (
            "Related tags: "
            f"{related_tags}"
        ),
        (
            "Measurements: "
            f"{measurements}"
        ),
        (
            "Time expression: "
            f"{understanding.time_context.expression}"
        ),
        (
            "Time start: "
            f"{start_time}"
        ),
        (
            "Time end: "
            f"{end_time}"
        ),
        (
            "History limit: "
            f"{understanding.time_context.history_limit}"
        ),
        (
            "Confidence: "
            f"{understanding.confidence:.3f}"
        ),
        (
            "Dispatcher reason: "
            f"{understanding.dispatcher_reason}"
        ),
        (
            "Tag resolution: "
            f"{understanding.tag_resolution_method}"
        ),
        (
            "Route source: "
            f"{understanding.route_source}"
        ),
        (
            "Semantic candidates: "
            f"{candidates}"
        ),
    ]

    return "\n".join(
        lines
    )


def main() -> None:
    print(
        "Building SmartMachineAI "
        "question-understanding engine..."
    )

    engine = QuestionUnderstandingEngine()

    print(
        "Question-understanding engine ready."
    )

    print(
        "Type 'exit' to stop."
    )

    while True:
        try:
            question = input(
                "\nQuestion: "
            ).strip()

        except (
            EOFError,
            KeyboardInterrupt,
        ):
            print()
            break

        if question.lower() in {
            "exit",
            "quit",
        }:
            break

        if not question:
            continue

        try:
            understanding = (
                engine.understand(
                    question
                )
            )

            print()
            print(
                format_question_understanding(
                    understanding
                )
            )

            print()
            print(
                "Compatible route:"
            )

            print(
                understanding.to_route()
            )

        except Exception as exc:
            print(
                "Question-understanding error: "
                f"{exc}"
            )


if __name__ == "__main__":
    main()
