from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from ai.llm_question_parser import (
    LLMQuestionParser,
    ParsedQuestion,
)


@dataclass(frozen=True)
class AIRoute:
    """
    Route produced from the AI language parser.

    At this stage, tags are intentionally empty because the semantic
    tag resolver will select real configured tags in the next step.
    """

    question: str
    query_type: str
    equipment: str
    intent: str
    measurements: list[str]
    location: str
    condition: str
    time_expression: str
    tag_hint: str
    tags: list[str]
    history_limit: int
    confidence: float
    route_source: str
    parser_time_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_legacy_route(self) -> dict[str, Any]:
        """
        Produces the basic route structure currently expected by
        existing SmartMachineAI modules.
        """

        return {
            "equipment": self.equipment,
            "intent": self.intent,
            "measurements": self.measurements,
            "tags": self.tags or None,
            "history_limit": self.history_limit,
            "query_type": self.query_type,
            "location": self.location,
            "condition": self.condition,
            "time_expression": self.time_expression,
            "tag_hint": self.tag_hint,
            "confidence": self.confidence,
            "route_source": self.route_source,
        }


class LLMRouteAdapter:
    """
    Converts ParsedQuestion into a route suitable for the existing
    SmartMachineAI processing pipeline.

    The LLM understands language.

    This adapter converts that understanding into deterministic route
    fields such as intent and history limit.

    It does not select PLC tags and does not access the database.
    """

    QUERY_TYPE_TO_ROUTE_INTENT = {
        "current_data": "current",
        "timeline": "status",
        "root_cause": "status",
        "threshold": "current",
        "trend": "trend",
        "general": "current",
    }

    def __init__(
        self,
        parser: LLMQuestionParser | None = None,
    ) -> None:
        self.parser = parser or LLMQuestionParser()

    @staticmethod
    def _history_limit(
        parsed: ParsedQuestion,
    ) -> int:
        """
        Selects how many historical records per tag should be read.

        These values can later be replaced by exact timestamp filtering.
        """

        if parsed.intent == "current_data":
            return 1

        if parsed.intent == "threshold":
            return 1

        if parsed.intent == "trend":
            return 100

        if parsed.intent == "timeline":
            return 100

        if parsed.intent == "root_cause":
            return 200

        return 10

    @staticmethod
    def _equipment_name(
        parsed: ParsedQuestion,
    ) -> str:
        if parsed.equipment:
            return parsed.equipment

        return "factory"

    @staticmethod
    def _measurements(
        parsed: ParsedQuestion,
    ) -> list[str]:
        measurements: list[str] = []

        if parsed.measurement:
            measurements.append(
                parsed.measurement
            )

        return measurements

    def build_from_parsed(
        self,
        parsed: ParsedQuestion,
    ) -> AIRoute:
        route_intent = self.QUERY_TYPE_TO_ROUTE_INTENT.get(
            parsed.intent,
            "current",
        )

        return AIRoute(
            question=parsed.question,
            query_type=parsed.intent,
            equipment=self._equipment_name(
                parsed
            ),
            intent=route_intent,
            measurements=self._measurements(
                parsed
            ),
            location=parsed.location,
            condition=parsed.condition,
            time_expression=parsed.time_expression,
            tag_hint=parsed.tag_hint,
            tags=[],
            history_limit=self._history_limit(
                parsed
            ),
            confidence=parsed.confidence,
            route_source="llm_parser",
            parser_time_seconds=parsed.elapsed_seconds,
        )

    def route(
        self,
        question: str,
    ) -> AIRoute:
        parsed = self.parser.parse(
            question
        )

        return self.build_from_parsed(
            parsed
        )

    def warm_up(self) -> float:
        return self.parser.warm_up()


def format_route(
    route: AIRoute,
) -> str:
    measurements = (
        ", ".join(route.measurements)
        if route.measurements
        else "None"
    )

    tags = (
        ", ".join(route.tags)
        if route.tags
        else "Not resolved yet"
    )

    lines = [
        f"Question: {route.question}",
        f"Query type: {route.query_type}",
        f"Equipment: {route.equipment}",
        f"Legacy intent: {route.intent}",
        f"Measurements: {measurements}",
        f"Location: {route.location or 'None'}",
        f"Condition: {route.condition or 'None'}",
        (
            "Time expression: "
            f"{route.time_expression or 'None'}"
        ),
        f"Tag hint: {route.tag_hint or 'None'}",
        f"Tags: {tags}",
        f"History limit: {route.history_limit}",
        f"Confidence: {route.confidence:.3f}",
        f"Route source: {route.route_source}",
        (
            "Parser time: "
            f"{route.parser_time_seconds:.3f} seconds"
        ),
    ]

    return "\n".join(lines)


def main() -> None:
    print(
        "SmartMachineAI LLM Route Adapter"
    )
    print(
        "Model: qwen2.5:3b"
    )
    print(
        "Tags are not resolved in this test."
    )
    print()

    adapter = LLMRouteAdapter()

    print(
        "Loading AI model..."
    )

    try:
        warm_up_seconds = adapter.warm_up()

        print(
            f"Model ready in "
            f"{warm_up_seconds:.3f} seconds."
        )

    except Exception as exc:
        print(
            f"Warm-up error: {exc}"
        )
        return

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
            route = adapter.route(
                question
            )

            print()
            print(
                format_route(
                    route
                )
            )

            print()
            print(
                "Legacy route:"
            )

            print(
                json.dumps(
                    route.to_legacy_route(),
                    indent=2,
                )
            )

        except Exception as exc:
            print(
                f"Route error: {exc}"
            )


if __name__ == "__main__":
    main()
