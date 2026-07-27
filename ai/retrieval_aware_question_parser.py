from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from typing import Any

import requests

from ai.llm_question_parser import (
    LLMQuestionParser,
    LLMQuestionParserError,
    ParsedQuestion,
)
from ai.semantic_tag_resolver import (
    SemanticTagResolver,
)


class RetrievalAwareQuestionParser(
    LLMQuestionParser
):
    """
    Retrieves a small list of relevant configured PLC tags before
    asking the LLM to understand the operator's question.

    Processing flow:

        Question
            ↓
        Semantic retrieval
            ↓
        Small candidate context
            ↓
        Qwen structured parsing
            ↓
        Deterministic validation

    The LLM is never allowed to create a PLC tag.
    """

    SYSTEM_PROMPT = """
You are an industrial automation question parser.

Return JSON only.

Do not answer the operator's question.
Do not explain your reasoning.
Do not generate SQL.
Do not generate Python.
Do not invent equipment.
Do not invent measurements.
Do not invent PLC tags.
Do not guess missing information.

A short list of configured candidate tags will be provided.

Use those candidates only as factory context.

IMPORTANT

The measurement field must contain a generic physical quantity or
state, not a PLC tag name.

Correct measurement examples:

pressure
temperature
level
flow
current
power
energy
humidity
alarm
warning
fault
running
door
position

Incorrect measurement examples:

CompressorPressure
ChillerReturnTemp
TankLowAlarm
TransferPumpRunning

Rules:

1. equipment:
   Return the configured equipment name associated with the best
   matching candidate.

2. measurement:
   Return only the generic quantity or state.

3. location:
   Return an explicit qualifier such as:
   discharge, suction, return, supply, inlet, outlet, room or motor.

4. condition:
   Return only an explicitly stated condition such as:
   high, low, hot, cold, running, stopped, open, closed,
   increasing, decreasing or abnormal.

5. Missing fields:
   Return an empty string.
   Never guess a missing condition, location or measurement.

6. Alarm questions:
   Use measurement="alarm".
   Do not automatically convert an alarm question into temperature,
   pressure or level.

7. Threshold questions:
   Use intent="threshold".
   Keep high or low only when explicitly requested.

8. tag_hint:
   Return the exact candidate tag name that best matches the question.
   Never create a new tag name.

Allowed intents:

current_data
timeline
root_cause
threshold
trend
general

Time expression rules:

Current value without explicit time:
latest

Explicit supported values:
latest
now
today
yesterday
last_hour
last_30_minutes
last_7_days
recent

For non-current questions, do not invent a time expression.
""".strip()

    MEASUREMENT_WORDS = (
        "temperature",
        "pressure",
        "humidity",
        "current",
        "voltage",
        "power",
        "energy",
        "level",
        "flow",
        "speed",
        "frequency",
        "weight",
        "alarm",
        "warning",
        "fault",
        "trip",
        "running",
        "status",
        "door",
        "position",
    )

    MEASUREMENT_ALIASES = {
        "temp": "temperature",
        "amps": "current",
        "amp": "current",
        "flowrate": "flow",
        "flow_rate": "flow",
        "run": "running",
        "state": "status",
        "trip": "fault",
    }

    def __init__(
        self,
        resolver: SemanticTagResolver | None = None,
        model: str = "qwen2.5:3b",
        ollama_url: str = "http://127.0.0.1:11434",
        timeout_seconds: int = 120,
        keep_alive: str = "30m",
        retrieval_top_k: int = 6,
        num_ctx: int = 2048,
        num_predict: int = 140,
    ) -> None:
        super().__init__(
            model=model,
            ollama_url=ollama_url,
            timeout_seconds=timeout_seconds,
            keep_alive=keep_alive,
        )

        self.resolver = (
            resolver
            or SemanticTagResolver()
        )

        self.retrieval_top_k = max(
            3,
            int(retrieval_top_k),
        )

        self.num_ctx = max(
            1024,
            int(num_ctx),
        )

        self.num_predict = max(
            80,
            int(num_predict),
        )

    @staticmethod
    def _normalize_text(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        text = str(value).strip().lower()

        text = re.sub(
            r"([a-z])([A-Z])",
            r"\1 \2",
            str(value).strip(),
        ).lower()

        text = text.replace(
            "_",
            " ",
        )

        text = re.sub(
            r"[^a-z0-9]+",
            " ",
            text,
        )

        return re.sub(
            r"\s+",
            " ",
            text,
        ).strip()

    @staticmethod
    def _normalize_identifier(
        value: str,
    ) -> str:
        clean_value = re.sub(
            r"[^a-zA-Z0-9]+",
            "_",
            str(value or "").strip(),
        )

        clean_value = re.sub(
            r"_+",
            "_",
            clean_value,
        ).strip("_")

        return clean_value.lower()

    @staticmethod
    def _unique(
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

    def _get_candidates(
        self,
        question: str,
    ) -> list[Any]:
        resolution = self.resolver.resolve(
            question=question,
            top_k=self.retrieval_top_k,
        )

        return list(
            resolution.candidates
        )

    def _candidate_tag_name(
        self,
        candidate: Any,
    ) -> str:
        return str(
            candidate.tag.tag_name
        ).strip()

    def _candidate_equipment_names(
        self,
        candidate: Any,
    ) -> list[str]:
        tag = candidate.tag

        display_names = list(
            getattr(
                tag,
                "equipment_display_names",
                [],
            )
            or []
        )

        internal_names = list(
            getattr(
                tag,
                "equipment_names",
                [],
            )
            or []
        )

        return self._unique(
            display_names
            + internal_names
        )

    def _candidate_concepts(
        self,
        candidate: Any,
    ) -> list[str]:
        concepts = getattr(
            candidate.tag,
            "concepts",
            [],
        )

        return self._unique(
            list(
                concepts or []
            )
        )

    def _build_candidate_context(
        self,
        candidates: list[Any],
    ) -> str:
        lines = [
            "CONFIGURED CANDIDATE TAGS",
        ]

        for index, candidate in enumerate(
            candidates,
            start=1,
        ):
            tag = candidate.tag

            tag_name = self._candidate_tag_name(
                candidate
            )

            equipment_names = (
                self._candidate_equipment_names(
                    candidate
                )
            )

            description = str(
                getattr(
                    tag,
                    "description",
                    "",
                )
                or ""
            ).strip()

            concepts = (
                self._candidate_concepts(
                    candidate
                )
            )

            unit = str(
                getattr(
                    tag,
                    "unit",
                    "",
                )
                or ""
            ).strip()

            fields = [
                f"{index}. tag={tag_name}",
            ]

            if equipment_names:
                fields.append(
                    "equipment="
                    + ", ".join(
                        equipment_names
                    )
                )

            if description:
                fields.append(
                    "description="
                    + description
                )

            if concepts:
                fields.append(
                    "concepts="
                    + ", ".join(
                        concepts
                    )
                )

            if unit:
                fields.append(
                    "unit="
                    + unit
                )

            fields.append(
                "similarity="
                f"{candidate.similarity:.3f}"
            )

            lines.append(
                " | ".join(
                    fields
                )
            )

        return "\n".join(
            lines
        )

    def _valid_tag_names(
        self,
        candidates: list[Any],
    ) -> list[str]:
        return [
            self._candidate_tag_name(
                candidate
            )
            for candidate in candidates
        ]

    def _match_candidate_tag(
        self,
        requested_tag: str,
        candidates: list[Any],
    ) -> Any | None:
        requested_normalized = (
            self._normalize_text(
                requested_tag
            )
        )

        if requested_normalized:
            for candidate in candidates:
                candidate_name = (
                    self._candidate_tag_name(
                        candidate
                    )
                )

                if (
                    self._normalize_text(
                        candidate_name
                    )
                    == requested_normalized
                ):
                    return candidate

        if candidates:
            return candidates[0]

        return None

    def _canonical_measurement(
        self,
        question: str,
        parsed_measurement: str,
        selected_candidate: Any | None,
    ) -> str:
        question_text = self._normalize_text(
            question
        )

        measurement_text = self._normalize_text(
            parsed_measurement
        )

        combined_text = (
            question_text
            + " "
            + measurement_text
        )

        for alias, canonical in (
            self.MEASUREMENT_ALIASES.items()
        ):
            alias_pattern = (
                r"\b"
                + re.escape(
                    alias.replace(
                        "_",
                        " ",
                    )
                )
                + r"\b"
            )

            if re.search(
                alias_pattern,
                combined_text,
            ):
                return canonical

        for measurement in (
            self.MEASUREMENT_WORDS
        ):
            if re.search(
                r"\b"
                + re.escape(
                    measurement
                )
                + r"\b",
                combined_text,
            ):
                if measurement == "trip":
                    return "fault"

                return measurement

        if selected_candidate is not None:
            tag = selected_candidate.tag

            candidate_text = " ".join(
                [
                    str(
                        getattr(
                            tag,
                            "tag_name",
                            "",
                        )
                        or ""
                    ),
                    str(
                        getattr(
                            tag,
                            "description",
                            "",
                        )
                        or ""
                    ),
                    " ".join(
                        self._candidate_concepts(
                            selected_candidate
                        )
                    ),
                ]
            )

            candidate_text = (
                self._normalize_text(
                    candidate_text
                )
            )

            for measurement in (
                self.MEASUREMENT_WORDS
            ):
                if re.search(
                    r"\b"
                    + re.escape(
                        measurement
                    )
                    + r"\b",
                    candidate_text,
                ):
                    if measurement == "trip":
                        return "fault"

                    return measurement

        return ""

    def _configured_equipment(
        self,
        parsed_equipment: str,
        selected_candidate: Any | None,
    ) -> str:
        if selected_candidate is None:
            return ""

        equipment_names = (
            self._candidate_equipment_names(
                selected_candidate
            )
        )

        if not equipment_names:
            return ""

        parsed_text = self._normalize_text(
            parsed_equipment
        )

        if parsed_text:
            for name in equipment_names:
                normalized_name = (
                    self._normalize_text(
                        name
                    )
                )

                if (
                    parsed_text == normalized_name
                    or parsed_text in normalized_name
                    or normalized_name in parsed_text
                ):
                    return self._normalize_identifier(
                        name
                    )

        return self._normalize_identifier(
            equipment_names[0]
        )

    def _validate_retrieval_result(
        self,
        result: ParsedQuestion,
        candidates: list[Any],
    ) -> ParsedQuestion:
        selected_candidate = (
            self._match_candidate_tag(
                requested_tag=result.tag_hint,
                candidates=candidates,
            )
        )

        if selected_candidate is None:
            return replace(
                result,
                equipment="",
                measurement="",
                tag_hint="",
                confidence=min(
                    result.confidence,
                    0.3,
                ),
            )

        selected_tag_name = (
            self._candidate_tag_name(
                selected_candidate
            )
        )

        equipment = (
            self._configured_equipment(
                parsed_equipment=result.equipment,
                selected_candidate=selected_candidate,
            )
        )

        measurement = (
            self._canonical_measurement(
                question=result.question,
                parsed_measurement=result.measurement,
                selected_candidate=selected_candidate,
            )
        )

        confidence = result.confidence

        selected_similarity = float(
            getattr(
                selected_candidate,
                "similarity",
                0.0,
            )
            or 0.0
        )

        if selected_similarity < 0.55:
            confidence = min(
                confidence,
                0.60,
            )

        if not measurement:
            confidence = min(
                confidence,
                0.50,
            )

        return replace(
            result,
            equipment=equipment,
            measurement=measurement,
            tag_hint=selected_tag_name,
            confidence=round(
                confidence,
                3,
            ),
        )

    def parse(
        self,
        question: str,
    ) -> ParsedQuestion:
        clean_question = self._clean_text(
            question
        )

        if not clean_question:
            raise ValueError(
                "Question cannot be empty."
            )

        retrieval_start = (
            time.perf_counter()
        )

        candidates = self._get_candidates(
            clean_question
        )

        retrieval_seconds = round(
            time.perf_counter()
            - retrieval_start,
            3,
        )

        if not candidates:
            raise LLMQuestionParserError(
                "No configured tag candidates were found."
            )

        candidate_context = (
            self._build_candidate_context(
                candidates
            )
        )

        user_prompt = (
            "OPERATOR QUESTION\n"
            + clean_question
            + "\n\n"
            + candidate_context
            + "\n\n"
            + "Return the structured JSON now."
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
                    "content": user_prompt,
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

        request_start = (
            time.perf_counter()
        )

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
                "The retrieval-aware parser timed out "
                f"after {self.timeout_seconds} seconds."
            ) from exc

        except requests.RequestException as exc:
            raise LLMQuestionParserError(
                f"Ollama request failed: {exc}"
            ) from exc

        llm_seconds = round(
            time.perf_counter()
            - request_start,
            3,
        )

        if response.status_code != 200:
            error_message = response.text

            try:
                error_data = response.json()

                if isinstance(
                    error_data,
                    dict,
                ):
                    error_message = str(
                        error_data.get(
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

        base_result = self._validate_result(
            question=clean_question,
            parsed=parsed_content,
            raw_response=raw_content,
            elapsed_seconds=round(
                retrieval_seconds
                + llm_seconds,
                3,
            ),
            load_seconds=load_seconds,
            generation_seconds=(
                generation_seconds
            ),
        )

        return self._validate_retrieval_result(
            result=base_result,
            candidates=candidates,
        )

    def warm_up(
        self,
    ) -> float:
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
                "The lightweight Qwen warm-up timed out."
            ) from exc

        except requests.RequestException as exc:
            raise LLMQuestionParserError(
                f"Qwen warm-up failed: {exc}"
            ) from exc

        if response.status_code != 200:
            raise LLMQuestionParserError(
                "Qwen warm-up returned HTTP "
                f"{response.status_code}: "
                f"{response.text}"
            )

        return round(
            time.perf_counter()
            - start_time,
            3,
        )

    def prompt_statistics(
        self,
        question: str,
    ) -> dict[str, Any]:
        candidates = self._get_candidates(
            question
        )

        candidate_context = (
            self._build_candidate_context(
                candidates
            )
        )

        return {
            "system_prompt_characters": len(
                self.SYSTEM_PROMPT
            ),
            "candidate_context_characters": len(
                candidate_context
            ),
            "candidate_count": len(
                candidates
            ),
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
            "candidate_tags": (
                self._valid_tag_names(
                    candidates
                )
            ),
        }


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
            "Selected tag: "
            f"{parsed.tag_hint or 'None'}"
        ),
        (
            "Confidence: "
            f"{parsed.confidence:.1%}"
        ),
        (
            "Total time: "
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
        "SmartMachineAI Retrieval-Aware Question Parser"
    )

    print(
        "Loading semantic resolver..."
    )

    try:
        parser = (
            RetrievalAwareQuestionParser()
        )

    except Exception as exc:
        print(
            f"Startup error: {exc}"
        )
        return

    print(
        "Loading Qwen..."
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

    test_question = (
        "What is the compressor discharge pressure?"
    )

    print()
    print(
        "Initial retrieval statistics:"
    )

    try:
        print(
            json.dumps(
                parser.prompt_statistics(
                    test_question
                ),
                indent=2,
            )
        )

    except Exception as exc:
        print(
            f"Retrieval test error: {exc}"
        )

    print()
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
