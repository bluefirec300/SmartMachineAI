from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any

import requests

from ai.knowledge_engine import (
    FactoryKnowledgeEngine,
)
from ai.llm_question_parser import (
    LLMQuestionParser,
    LLMQuestionParserError,
    ParsedQuestion,
)


class KnowledgeAwareQuestionParser(
    LLMQuestionParser
):
    """
    Question parser that uses the factory configuration as context.

    The LLM is responsible only for understanding language.

    Configured equipment and tags remain controlled by config.db.
    Final PLC tag selection remains the responsibility of the
    semantic tag resolver.
    """

    BASE_SYSTEM_PROMPT = """
You are an industrial automation question parser for one configured
factory.

Return JSON only.

Do not answer the question.
Do not explain.
Do not generate SQL or Python.
Do not invent PLC tags.
Do not invent equipment.
Do not guess missing information.

Use only the supplied factory configuration.

Rules:

1. equipment must be one configured equipment name.
2. Return empty string when equipment is unknown.
3. Return empty string for any field not stated or clearly implied.
4. Alarm means measurement="alarm".
5. Warning means measurement="warning".
6. Running state means measurement="running".
7. Do not convert an alarm question into temperature or pressure.
8. Use condition="high" only when high is explicitly mentioned.
9. Use condition="low" only when low is explicitly mentioned.
10. tag_hint must be natural language, not a PLC tag name.

Allowed intents:

current_data:
Latest value or present state.

timeline:
Past alarms, warnings, faults, trips or events.

root_cause:
Why something happened.

threshold:
Alarm limit, warning limit, setpoint or threshold.

trend:
How a value changed over time.

general:
Cannot classify reliably.

Fields:

equipment:
One configured equipment name or empty string.

measurement:
Requested quantity or state such as pressure, temperature, level,
flow, current, power, energy, running, alarm, warning, humidity,
door or position.

location:
Explicit qualifier such as return, supply, discharge, suction,
inlet, outlet, motor or room.

condition:
Explicit state such as high, low, hot, cold, running, stopped,
open, closed, increasing, decreasing or abnormal.

time_expression:
Normalize explicit time references:
latest, now, today, yesterday, last_hour, last_30_minutes,
last_7_days or recent.

For current_data without explicit time, use latest.
For other intents, do not invent a time.

tag_hint:
A short description containing the relevant equipment,
location, measurement and threshold condition.

confidence:
Number from 0.0 to 1.0.
Lower confidence when requested data is not represented in the
factory configuration.
""".strip()

    def __init__(
        self,
        knowledge_engine: FactoryKnowledgeEngine | None = None,
        model: str = "qwen2.5:3b",
        ollama_url: str = "http://127.0.0.1:11434",
        timeout_seconds: int = 180,
        keep_alive: str = "30m",
        num_ctx: int = 4096,
        num_predict: int = 160,
    ) -> None:
        super().__init__(
            model=model,
            ollama_url=ollama_url,
            timeout_seconds=timeout_seconds,
            keep_alive=keep_alive,
        )

        self.knowledge_engine = (
            knowledge_engine
            or FactoryKnowledgeEngine()
        )

        self.num_ctx = max(
            2048,
            int(num_ctx),
        )

        self.num_predict = max(
            80,
            int(num_predict),
        )

        self.factory_context = (
            self._build_compact_factory_context()
        )

        self.SYSTEM_PROMPT = (
            self._build_system_prompt()
        )

    @staticmethod
    def _clean_value(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        return str(value).strip()

    @staticmethod
    def _unique_values(
        values: list[str],
    ) -> list[str]:
        output: list[str] = []

        for value in values:
            clean_value = str(
                value or ""
            ).strip()

            if (
                clean_value
                and clean_value not in output
            ):
                output.append(
                    clean_value
                )

        return output

    def _build_compact_factory_context(
        self,
    ) -> str:
        """
        Build a smaller context than the full knowledge-engine report.

        PLC addresses, drivers and data types are unnecessary for
        language parsing, so they are excluded.
        """

        knowledge = (
            self.knowledge_engine.get_knowledge()
        )

        lines = [
            "FACTORY CONFIGURATION",
        ]

        for equipment in knowledge.equipment:
            names = self._unique_values(
                [
                    equipment.equipment_name,
                    *sorted(
                        equipment.alternative_names
                    ),
                ]
            )

            lines.append(
                "EQUIPMENT: "
                + equipment.equipment_name
            )

            if len(names) > 1:
                lines.append(
                    "NAMES: "
                    + ", ".join(
                        names
                    )
                )

            tag_lines: list[str] = []

            for tag in equipment.tags:
                parts = [
                    tag.tag_name,
                ]

                if tag.description:
                    parts.append(
                        tag.description
                    )

                useful_concepts = [
                    concept
                    for concept in tag.concepts
                    if concept not in {
                        "factory",
                        "system",
                        "machine",
                        "data",
                        "value",
                        "tag",
                    }
                ]

                if useful_concepts:
                    parts.append(
                        "concepts="
                        + ",".join(
                            useful_concepts
                        )
                    )

                if tag.unit:
                    parts.append(
                        "unit="
                        + tag.unit
                    )

                tag_lines.append(
                    " | ".join(
                        parts
                    )
                )

            for tag_line in tag_lines:
                lines.append(
                    "TAG: "
                    + tag_line
                )

        if knowledge.unassigned_tags:
            lines.append(
                "UNASSIGNED TAGS"
            )

            for tag in knowledge.unassigned_tags:
                parts = [
                    tag.tag_name,
                ]

                if tag.description:
                    parts.append(
                        tag.description
                    )

                if tag.concepts:
                    parts.append(
                        "concepts="
                        + ",".join(
                            tag.concepts
                        )
                    )

                lines.append(
                    "TAG: "
                    + " | ".join(
                        parts
                    )
                )

        return "\n".join(
            lines
        )

    def _build_system_prompt(
        self,
    ) -> str:
        return (
            self.BASE_SYSTEM_PROMPT
            + "\n\n"
            + self.factory_context
        )

    def rebuild_factory_context(
        self,
    ) -> None:
        """
        Reload config.db knowledge after equipment or tag changes.
        """

        self.knowledge_engine.rebuild()

        self.factory_context = (
            self._build_compact_factory_context()
        )

        self.SYSTEM_PROMPT = (
            self._build_system_prompt()
        )

    def knowledge_summary(
        self,
    ) -> dict[str, int]:
        return self.knowledge_engine.summary()

    def prompt_statistics(
        self,
    ) -> dict[str, int]:
        return {
            "system_prompt_characters": len(
                self.SYSTEM_PROMPT
            ),
            "factory_context_characters": len(
                self.factory_context
            ),
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
        }

    @staticmethod
    def _display_to_identifier(
        value: str,
    ) -> str:
        return (
            LLMQuestionParser._normalize_identifier(
                value
            )
        )

    def _resolve_configured_equipment(
        self,
        value: str,
    ) -> str:
        clean_value = self._clean_value(
            value
        )

        if not clean_value:
            return ""

        equipment = (
            self.knowledge_engine.find_equipment(
                clean_value
            )
        )

        if equipment is None:
            equipment = (
                self.knowledge_engine.find_equipment(
                    clean_value.replace(
                        "_",
                        " ",
                    )
                )
            )

        if equipment is None:
            return ""

        return self._display_to_identifier(
            equipment.equipment_name
        )

    def _get_equipment_tags(
        self,
        equipment_identifier: str,
    ) -> list[Any]:
        if not equipment_identifier:
            return []

        return (
            self.knowledge_engine.get_tags_for_equipment(
                equipment_identifier.replace(
                    "_",
                    " ",
                )
            )
        )

    def _measurement_is_configured(
        self,
        equipment_identifier: str,
        measurement: str,
    ) -> bool:
        clean_measurement = (
            self._clean_value(
                measurement
            ).lower()
        )

        if not clean_measurement:
            return True

        if equipment_identifier:
            tags = self._get_equipment_tags(
                equipment_identifier
            )

            for tag in tags:
                searchable_values = [
                    tag.tag_name,
                    tag.description,
                    tag.unit,
                    *tag.concepts,
                ]

                searchable_text = " ".join(
                    searchable_values
                ).lower()

                if (
                    clean_measurement
                    in searchable_text
                ):
                    return True

            return False

        matches = (
            self.knowledge_engine.find_tags_by_concepts(
                concepts=[
                    clean_measurement
                ],
            )
        )

        return bool(
            matches
        )

    def _build_validated_tag_hint(
        self,
        parsed: ParsedQuestion,
        equipment: str,
    ) -> str:
        parts: list[str] = []

        values = [
            equipment.replace(
                "_",
                " ",
            ),
            parsed.location.replace(
                "_",
                " ",
            ),
            parsed.measurement.replace(
                "_",
                " ",
            ),
        ]

        for value in values:
            clean_value = value.strip()

            if (
                clean_value
                and clean_value not in parts
            ):
                parts.append(
                    clean_value
                )

        if parsed.intent == "threshold":
            if (
                parsed.condition
                and parsed.condition not in parts
            ):
                parts.append(
                    parsed.condition
                )

            parts.append(
                "threshold"
            )

        elif (
            parsed.condition
            and parsed.condition not in {
                "hot",
                "cold",
            }
            and parsed.condition not in parts
        ):
            parts.append(
                parsed.condition
            )

        if parts:
            return " ".join(
                parts
            )

        return parsed.tag_hint.strip().lower()

    def _validate_result(
        self,
        question: str,
        parsed: dict[str, Any],
        raw_response: str,
        elapsed_seconds: float,
        load_seconds: float,
        generation_seconds: float,
    ) -> ParsedQuestion:
        base_result = super()._validate_result(
            question=question,
            parsed=parsed,
            raw_response=raw_response,
            elapsed_seconds=elapsed_seconds,
            load_seconds=load_seconds,
            generation_seconds=generation_seconds,
        )

        configured_equipment = (
            self._resolve_configured_equipment(
                base_result.equipment
            )
        )

        confidence = (
            base_result.confidence
        )

        if (
            base_result.equipment
            and not configured_equipment
        ):
            confidence = min(
                confidence,
                0.55,
            )

        if not self._measurement_is_configured(
            equipment_identifier=(
                configured_equipment
            ),
            measurement=(
                base_result.measurement
            ),
        ):
            confidence = min(
                confidence,
                0.60,
            )

        tag_hint = (
            self._build_validated_tag_hint(
                parsed=base_result,
                equipment=configured_equipment,
            )
        )

        return replace(
            base_result,
            equipment=configured_equipment,
            tag_hint=tag_hint,
            confidence=round(
                confidence,
                3,
            ),
        )

    def parse(
        self,
        question: str,
    ) -> ParsedQuestion:
        """
        Parse one question using the larger knowledge-aware context.
        """

        clean_question = self._clean_text(
            question
        )

        if not clean_question:
            raise ValueError(
                "Question cannot be empty."
            )

        endpoint = (
            f"{self.ollama_url}/api/chat"
        )

        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "format": self.RESPONSE_SCHEMA,
            "messages": [
                {
                    "role": "system",
                    "content": self.SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": clean_question,
                },
            ],
            "options": {
                "temperature": 0,
                "seed": 7,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
                "top_k": 10,
                "top_p": 0.5,
            },
        }

        request_start = time.perf_counter()

        try:
            response = self.session.post(
                endpoint,
                json=payload,
                timeout=self.timeout_seconds,
            )

        except requests.ConnectionError as exc:
            raise LLMQuestionParserError(
                "Cannot connect to Ollama at "
                f"{self.ollama_url}. "
                "Check that Ollama is running."
            ) from exc

        except requests.Timeout as exc:
            raise LLMQuestionParserError(
                "The knowledge-aware parser request timed out "
                f"after {self.timeout_seconds} seconds. "
                f"Context size is {self.num_ctx} tokens."
            ) from exc

        except requests.RequestException as exc:
            raise LLMQuestionParserError(
                f"Ollama request failed: {exc}"
            ) from exc

        elapsed_seconds = round(
            time.perf_counter()
            - request_start,
            3,
        )

        if response.status_code != 200:
            error_message = response.text

            try:
                response_error = response.json()

                if isinstance(
                    response_error,
                    dict,
                ):
                    error_message = str(
                        response_error.get(
                            "error",
                            error_message,
                        )
                    )

            except ValueError:
                pass

            raise LLMQuestionParserError(
                "Ollama returned HTTP "
                f"{response.status_code}: "
                f"{error_message}"
            )

        try:
            response_data = response.json()

        except ValueError as exc:
            raise LLMQuestionParserError(
                "Ollama returned invalid HTTP JSON."
            ) from exc

        raw_content = (
            self._extract_message_content(
                response_data
            )
        )

        parsed_content = self._load_json(
            raw_content
        )

        load_seconds = (
            self._nanoseconds_to_seconds(
                response_data.get(
                    "load_duration"
                )
            )
        )

        generation_seconds = (
            self._nanoseconds_to_seconds(
                response_data.get(
                    "eval_duration"
                )
            )
        )

        return self._validate_result(
            question=clean_question,
            parsed=parsed_content,
            raw_response=raw_content,
            elapsed_seconds=elapsed_seconds,
            load_seconds=load_seconds,
            generation_seconds=generation_seconds,
        )

    def warm_up(
        self,
    ) -> float:
        """
        Load Qwen into memory without sending the full factory context.

        This avoids wasting time processing the complete knowledge
        prompt during startup.
        """

        endpoint = (
            f"{self.ollama_url}/api/chat"
        )

        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Reply with only the word OK."
                    ),
                },
            ],
            "options": {
                "temperature": 0,
                "num_ctx": 512,
                "num_predict": 4,
            },
        }

        start_time = time.perf_counter()

        try:
            response = self.session.post(
                endpoint,
                json=payload,
                timeout=60,
            )

        except requests.ConnectionError as exc:
            raise LLMQuestionParserError(
                "Cannot connect to Ollama at "
                f"{self.ollama_url}."
            ) from exc

        except requests.Timeout as exc:
            raise LLMQuestionParserError(
                "The lightweight Ollama warm-up timed out."
            ) from exc

        except requests.RequestException as exc:
            raise LLMQuestionParserError(
                f"Ollama warm-up failed: {exc}"
            ) from exc

        if response.status_code != 200:
            raise LLMQuestionParserError(
                "Ollama warm-up returned HTTP "
                f"{response.status_code}: "
                f"{response.text}"
            )

        return round(
            time.perf_counter()
            - start_time,
            3,
        )


def format_parsed_question(
    parsed: ParsedQuestion,
) -> str:
    lines = [
        f"Question: {parsed.question}",
        f"Intent: {parsed.intent}",
        (
            "Equipment: "
            f"{parsed.equipment or 'None'}"
        ),
        (
            "Measurement: "
            f"{parsed.measurement or 'None'}"
        ),
        (
            "Location: "
            f"{parsed.location or 'None'}"
        ),
        (
            "Condition: "
            f"{parsed.condition or 'None'}"
        ),
        (
            "Time expression: "
            f"{parsed.time_expression or 'None'}"
        ),
        (
            "Tag hint: "
            f"{parsed.tag_hint or 'None'}"
        ),
        f"Confidence: {parsed.confidence:.1%}",
        (
            "Elapsed time: "
            f"{parsed.elapsed_seconds:.3f} seconds"
        ),
        (
            "Model load time: "
            f"{parsed.load_seconds:.3f} seconds"
        ),
        (
            "Generation time: "
            f"{parsed.generation_seconds:.3f} seconds"
        ),
    ]

    return "\n".join(
        lines
    )


def main() -> None:
    print(
        "SmartMachineAI Knowledge-Aware Question Parser V2"
    )

    print(
        "Building factory knowledge..."
    )

    try:
        parser = (
            KnowledgeAwareQuestionParser()
        )

    except Exception as exc:
        print(
            f"Startup error: {exc}"
        )
        return

    print()
    print(
        "Knowledge summary:"
    )

    print(
        json.dumps(
            parser.knowledge_summary(),
            indent=2,
        )
    )

    print()
    print(
        "Prompt statistics:"
    )

    print(
        json.dumps(
            parser.prompt_statistics(),
            indent=2,
        )
    )

    print()
    print(
        "Loading Qwen with lightweight warm-up..."
    )

    try:
        warm_up_seconds = (
            parser.warm_up()
        )

    except Exception as exc:
        print(
            f"Warm-up error: {exc}"
        )
        return

    print(
        f"Qwen ready in "
        f"{warm_up_seconds:.3f} seconds."
    )

    print(
        "The first full factory question may take longer."
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
            result = parser.parse(
                question
            )

            print()
            print(
                format_parsed_question(
                    result
                )
            )

            print()
            print(
                "JSON:"
            )

            print(
                json.dumps(
                    result.to_dict(),
                    indent=2,
                )
            )

        except Exception as exc:
            print(
                f"Parser error: {exc}"
            )


if __name__ == "__main__":
    main()
