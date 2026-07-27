import sqlite3
from typing import Any

import requests

from ai.ai_provider import AIProvider
from ai.database_reader import (
    DB_PATH,
    get_machine_history,
)
from ai.event_engine import EventEngine
from ai.event_store import EventStore
from ai.observation_engine import (
    ObservationEngine,
    format_observations,
)
from ai.prompt_builder import build_prompt
from ai.query_dispatcher import (
    QueryDispatcher,
    QueryType,
)
from ai.router import route_question
from ai.rule_engine import (
    RuleEngine,
    format_rule_results,
)
from ai.threshold_reader import ThresholdReader
from ai.timeline_query import TimelineQuestionHandler
from ai.trend_analyzer import analyse_history


class SmartMachineBridge:
    """
    Main SmartMachineAI question orchestrator.
    """

    def __init__(self) -> None:
        self.dispatcher = QueryDispatcher()
        self.observation_engine = ObservationEngine()
        self.rule_engine = RuleEngine()
        self.event_engine = EventEngine()
        self.event_store = EventStore()
        self.threshold_reader = ThresholdReader()
        self.timeline_handler = TimelineQuestionHandler()
        self.ai = AIProvider()

    @staticmethod
    def _display_route(
        route: dict[str, Any],
        query_type: QueryType,
    ) -> None:
        equipment = str(
            route.get(
                "equipment",
                "factory",
            )
        )

        intent = str(
            route.get(
                "intent",
                "current",
            )
        )

        history_limit = route.get(
            "history_limit",
            1,
        )

        print(
            f"[Query: {query_type.value} | "
            f"Equipment: {equipment} | "
            f"Intent: {intent} | "
            f"History: {history_limit}]"
        )

    @staticmethod
    def _format_number(
        value: Any,
    ) -> str:
        if value is None:
            return "not configured"

        try:
            numeric_value = float(value)

        except (
            TypeError,
            ValueError,
        ):
            return str(value)

        if numeric_value.is_integer():
            return str(
                int(numeric_value)
            )

        return (
            f"{numeric_value:.6f}"
            .rstrip("0")
            .rstrip(".")
        )

    @classmethod
    def _format_threshold_record(
        cls,
        threshold: dict[str, Any],
    ) -> str:
        tag_name = str(
            threshold.get(
                "tag_name",
                threshold.get(
                    "tag",
                    "Unknown tag",
                ),
            )
        )

        unit = str(
            threshold.get(
                "unit",
                "",
            )
            or ""
        ).strip()

        def value_text(
            field_name: str,
        ) -> str:
            value = cls._format_number(
                threshold.get(
                    field_name
                )
            )

            if (
                value != "not configured"
                and unit
            ):
                return f"{value} {unit}"

            return value

        lines = [
            f"{tag_name}:",
            (
                "  Low alarm: "
                f"{value_text('low_alarm')}"
            ),
            (
                "  Low warning: "
                f"{value_text('low_warning')}"
            ),
            (
                "  High warning: "
                f"{value_text('high_warning')}"
            ),
            (
                "  High alarm: "
                f"{value_text('high_alarm')}"
            ),
        ]

        return "\n".join(
            lines
        )

    @classmethod
    def _format_threshold_records(
        cls,
        thresholds: list[dict[str, Any]],
    ) -> str:
        if not thresholds:
            return (
                "No matching configured "
                "threshold was found."
            )

        return "\n\n".join(
            cls._format_threshold_record(
                threshold
            )
            for threshold in thresholds
        )

    def _store_detected_events(
        self,
        rule_results: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
    ) -> int:
        events = self.event_engine.detect(
            rule_results,
            summaries,
        )

        stored_count = 0

        for event in events:
            inserted = (
                self.event_store.insert_event(
                    event
                )
            )

            if inserted:
                stored_count += 1

        return stored_count

    def _build_machine_analysis(
        self,
        route: dict[str, Any],
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        str,
        str,
    ]:
        tags = route.get(
            "tags",
            None,
        )

        history_limit = int(
            route.get(
                "history_limit",
                1,
            )
        )

        history = get_machine_history(
            tags=tags,
            limit=history_limit,
        )

        summaries = analyse_history(
            history
        )

        rule_results = (
            self.rule_engine.evaluate(
                summaries
            )
        )

        self._store_detected_events(
            rule_results=rule_results,
            summaries=summaries,
        )

        rule_context = format_rule_results(
            rule_results,
            include_normal=False,
        )

        observations = (
            self.observation_engine.build(
                summaries=summaries,
                intent=str(
                    route.get(
                        "intent",
                        "current",
                    )
                ),
            )
        )

        machine_context = format_observations(
            observations
        )

        return (
            summaries,
            rule_results,
            machine_context,
            rule_context,
        )

    def _stream_ai_answer(
        self,
        prompt: str,
    ) -> None:
        print(
            "\nAI: ",
            end="",
            flush=True,
        )

        for text in self.ai.stream(
            prompt
        ):
            print(
                text,
                end="",
                flush=True,
            )

        print()

    def _answer_current_data(
        self,
        question: str,
        route: dict[str, Any],
    ) -> None:
        (
            summaries,
            _rule_results,
            machine_context,
            rule_context,
        ) = self._build_machine_analysis(
            route
        )

        if not summaries:
            print(
                "\nAI: No matching historian "
                "data was found."
            )
            return

        prompt = build_prompt(
            question=question,
            machine_context=machine_context,
            rule_context=rule_context,
            route=route,
        )

        self._stream_ai_answer(
            prompt
        )

    @staticmethod
    def _normalize_threshold_results(
        result: Any,
    ) -> list[dict[str, Any]]:
        if isinstance(
            result,
            dict,
        ):
            return [
                result
            ]

        if isinstance(
            result,
            list,
        ):
            return [
                item
                for item in result
                if isinstance(
                    item,
                    dict,
                )
            ]

        return []

    def _call_threshold_method(
        self,
        method_name: str,
        *arguments: Any,
    ) -> list[dict[str, Any]]:
        method = getattr(
            self.threshold_reader,
            method_name,
            None,
        )

        if not callable(
            method
        ):
            return []

        try:
            result = method(
                *arguments
            )

        except (
            TypeError,
            KeyError,
            ValueError,
        ):
            return []

        return (
            self._normalize_threshold_results(
                result
            )
        )

    def _find_thresholds(
        self,
        question: str,
        route: dict[str, Any],
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []

        question_methods = (
            "find_thresholds",
            "find_by_words",
            "search",
        )

        for method_name in question_methods:
            matches.extend(
                self._call_threshold_method(
                    method_name,
                    question,
                )
            )

            if matches:
                break

        tags = route.get(
            "tags",
            [],
        )

        if not isinstance(
            tags,
            list,
        ):
            tags = []

        tag_methods = (
            "get_threshold",
            "get_exact_threshold",
            "find_threshold",
        )

        for tag in tags:
            for method_name in tag_methods:
                tag_matches = (
                    self._call_threshold_method(
                        method_name,
                        str(tag),
                    )
                )

                if tag_matches:
                    matches.extend(
                        tag_matches
                    )
                    break

        unique_matches: list[
            dict[str, Any]
        ] = []

        seen: set[str] = set()

        for match in matches:
            tag_name = str(
                match.get(
                    "tag_name",
                    match.get(
                        "tag",
                        "",
                    ),
                )
            ).strip()

            key = tag_name.lower()

            if key and key in seen:
                continue

            if key:
                seen.add(
                    key
                )

            unique_matches.append(
                match
            )

        return unique_matches

    def _answer_threshold(
        self,
        question: str,
        route: dict[str, Any],
    ) -> None:
        thresholds = self._find_thresholds(
            question=question,
            route=route,
        )

        answer = (
            self._format_threshold_records(
                thresholds
            )
        )

        print(
            f"\nAI: {answer}"
        )

    def _answer_timeline(
        self,
        question: str,
        route: dict[str, Any],
    ) -> None:
        answer = (
            self.timeline_handler.answer(
                question=question,
                route=route,
            )
        )

        print(
            f"\nAI: {answer}"
        )

    def _answer_root_cause(
        self,
        question: str,
        route: dict[str, Any],
    ) -> None:
        context = (
            self.timeline_handler.answer(
                question=(
                    "What happened before "
                    "the latest alarm?"
                ),
                route=route,
            )
        )

        print(
            "\nAI: The dedicated root-cause "
            "engine has not been implemented yet."
        )

        print(
            "The factual event sequence "
            "currently available is:\n"
        )

        print(
            context
        )

    def _answer_general(
        self,
        question: str,
    ) -> None:
        prompt = (
            "You are MMG Smart Machine AI, "
            "an industrial automation assistant.\n\n"
            "Answer the user's general engineering "
            "question. Do not claim to have read "
            "live PLC data unless machine data was "
            "provided.\n\n"
            f"User question:\n{question}"
        )

        self._stream_ai_answer(
            prompt
        )

    def answer(
        self,
        question: str,
    ) -> None:
        route = route_question(
            question
        )

        dispatch = self.dispatcher.dispatch(
            question=question,
            route=route,
        )

        self._display_route(
            route=route,
            query_type=dispatch.query_type,
        )

        if (
            dispatch.query_type
            == QueryType.CURRENT_DATA
        ):
            self._answer_current_data(
                question=question,
                route=route,
            )
            return

        if (
            dispatch.query_type
            == QueryType.THRESHOLD
        ):
            self._answer_threshold(
                question=question,
                route=route,
            )
            return

        if (
            dispatch.query_type
            == QueryType.TIMELINE
        ):
            self._answer_timeline(
                question=question,
                route=route,
            )
            return

        if (
            dispatch.query_type
            == QueryType.ROOT_CAUSE
        ):
            self._answer_root_cause(
                question=question,
                route=route,
            )
            return

        self._answer_general(
            question
        )


def main() -> None:
    print(
        "MMG Smart Machine AI"
    )

    print(
        f"Database: {DB_PATH}"
    )

    print(
        "Question routing: enabled"
    )

    print(
        "Query dispatcher: enabled"
    )

    print(
        "Threshold queries: enabled"
    )

    print(
        "Event timeline: enabled"
    )

    print(
        "Event detection: enabled"
    )

    print(
        "Event storage: enabled"
    )

    try:
        bridge = SmartMachineBridge()

    except Exception as error:
        print(
            "Unable to initialize "
            f"Smart Machine AI: {error}"
        )
        return

    print(
        f"AI Provider: {bridge.ai.provider}"
    )

    print(
        f"AI Model: {bridge.ai.model}"
    )

    print(
        "Type 'exit' to quit."
    )

    while True:
        try:
            question = input(
                "\nYou: "
            ).strip()

        except KeyboardInterrupt:
            print(
                "\nAssistant stopped."
            )
            break

        except EOFError:
            print(
                "\nAssistant stopped."
            )
            break

        if question.lower() in {
            "exit",
            "quit",
        }:
            print(
                "Assistant stopped."
            )
            break

        if not question:
            continue

        try:
            bridge.answer(
                question
            )

        except sqlite3.Error as error:
            print(
                f"\nDatabase error: {error}"
            )

        except requests.Timeout:
            print(
                "\nAI provider timed out."
            )

        except requests.RequestException as error:
            print(
                "\nAI provider connection error: "
                f"{error}"
            )

        except KeyError as error:
            print(
                f"\nMissing data field: {error}"
            )

        except ValueError as error:
            print(
                f"\nData processing error: {error}"
            )

        except Exception as error:
            print(
                f"\nUnexpected error: {error}"
            )


if __name__ == "__main__":
    main()
