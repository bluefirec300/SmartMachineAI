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


class CandidateSelectionParser(
    LLMQuestionParser
):
    """
    Retrieval-first parser.

    Processing:

        Operator question
            ↓
        SemanticTagResolver retrieves candidates
            ↓
        Qwen selects one candidate number
            ↓
        Equipment, measurement and tag metadata come from config.db

    Qwen never creates a PLC tag name.
    """

    SELECTION_SCHEMA = {
        "type": "object",
        "properties": {
            "selected_candidate": {
                "type": "integer",
                "minimum": 0,
                "maximum": 6,
            },
            "intent": {
                "type": "string",
                "enum": [
                    "current_data",
                    "timeline",
                    "root_cause",
                    "threshold",
                    "trend",
                    "general",
                ],
            },
            "location": {
                "type": "string",
            },
            "condition": {
                "type": "string",
            },
            "time_expression": {
                "type": "string",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
        "required": [
            "selected_candidate",
            "intent",
            "location",
            "condition",
            "time_expression",
            "confidence",
        ],
        "additionalProperties": False,
    }

    SYSTEM_PROMPT = """
You select the best configured industrial PLC tag candidate for an
operator question.

Return JSON only.

Do not answer the question.
Do not explain.
Do not create a PLC tag.
Do not copy the candidate description.
Do not return equipment or measurement names.
Return only the requested JSON fields.

Candidate selection:

- selected_candidate is the candidate number.
- Use 0 when none of the candidates represents the question.
- Consider equipment, measurement, location and operating state.
- Prefer an exact measurement match over a general similarity score.
- Pressure must not select current.
- Current must not select pressure.
- Temperature must not select pressure.
- Alarm questions should select an alarm tag.
- Warning questions should select a warning tag.
- Running or stopped questions should select a running/status tag.
- High threshold questions should prefer a high alarm or high limit.
- Low threshold questions should prefer a low alarm or low limit.

Intent values:

current_data:
The latest value or current status.

timeline:
Historical alarms, events, faults, warnings or trips.

root_cause:
Why an event or abnormal condition happened.

threshold:
Alarm limit, warning limit, setpoint or threshold.

trend:
How a measurement changed over time.

general:
The intent cannot be classified reliably.

Location:

Return only an explicitly stated location such as:
discharge, suction, return, supply, inlet, outlet, room or motor.

Otherwise return an empty string.

Condition:

Return only an explicitly stated condition such as:
high, low, hot, cold, running, stopped, open, closed, increasing,
decreasing or abnormal.

Otherwise return an empty string.

Time expression:

For current_data without an explicit time, return latest.

Supported explicit values:
latest
now
today
yesterday
last_hour
last_30_minutes
last_7_days
recent

For non-current questions without an explicit time, return an empty
string.

Confidence:

Return a value from 0.0 to 1.0.
Lower confidence when two candidates are similarly suitable.
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
        "running",
        "status",
        "door",
        "position",
    )

    MEASUREMENT_ALIASES = {
        "temp": "temperature",
        "flowrate": "flow",
        "flow rate": "flow",
        "amps": "current",
        "amp": "current",
        "trip": "fault",
        "run": "running",
    }

    def __init__(
        self,
        resolver: SemanticTagResolver | None = None,
        model: str = "qwen2.5:3b",
        ollama_url: str = "http://127.0.0.1:11434",
        timeout_seconds: int = 120,
        keep_alive: str = "30m",
        retrieval_top_k: int = 6,
        num_ctx: int = 1536,
        num_predict: int = 100,
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
            min(
                6,
                int(retrieval_top_k),
            ),
        )

        self.num_ctx = max(
            1024,
            int(num_ctx),
        )

        self.num_predict = max(
            60,
            int(num_predict),
        )

    @staticmethod
    def _clean_string(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        return str(value).strip()

    @staticmethod
    def _normalize_text(
        value: Any,
    ) -> str:
        raw_value = str(
            value or ""
        ).strip()

        raw_value = re.sub(
            r"([a-z0-9])([A-Z])",
            r"\1 \2",
            raw_value,
        )

        raw_value = raw_value.replace(
            "_",
            " ",
        )

        raw_value = raw_value.lower()

        raw_value = re.sub(
            r"[^a-z0-9]+",
            " ",
            raw_value,
        )

        return re.sub(
            r"\s+",
            " ",
            raw_value,
        ).strip()

    @staticmethod
    def _normalize_identifier(
        value: Any,
    ) -> str:
        text = str(
            value or ""
        ).strip()

        text = re.sub(
            r"([a-z0-9])([A-Z])",
            r"\1_\2",
            text,
        )

        text = re.sub(
            r"[^a-zA-Z0-9]+",
            "_",
            text,
        )

        text = re.sub(
            r"_+",
            "_",
            text,
        ).strip("_")

        return text.lower()

    @staticmethod
    def _unique_strings(
        values: list[Any],
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

    def _retrieve_candidates(
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

    @staticmethod
    def _candidate_tag_name(
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

        return self._unique_strings(
            display_names
            + internal_names
        )

    def _candidate_concepts(
        self,
        candidate: Any,
    ) -> list[str]:
        concepts = list(
            getattr(
                candidate.tag,
                "concepts",
                [],
            )
            or []
        )

        return self._unique_strings(
            concepts
        )

    def _candidate_description(
        self,
        candidate: Any,
    ) -> str:
        return str(
            getattr(
                candidate.tag,
                "description",
                "",
            )
            or ""
        ).strip()

    def _candidate_unit(
        self,
        candidate: Any,
    ) -> str:
        return str(
            getattr(
                candidate.tag,
                "unit",
                "",
            )
            or ""
        ).strip()

    def _build_candidate_context(
        self,
        candidates: list[Any],
    ) -> str:
        """
        Build a concise candidate list.

        Similarity values are deliberately excluded because Qwen should
        judge the operator wording rather than simply copying the
        embedding ranking.
        """

        lines = [
            "CANDIDATES",
        ]

        for index, candidate in enumerate(
            candidates,
            start=1,
        ):
            tag_name = (
                self._candidate_tag_name(
                    candidate
                )
            )

            equipment_names = (
                self._candidate_equipment_names(
                    candidate
                )
            )

            description = (
                self._candidate_description(
                    candidate
                )
            )

            concepts = (
                self._candidate_concepts(
                    candidate
                )
            )

            unit = self._candidate_unit(
                candidate
            )

            canonical_equipment = ""

            if equipment_names:
                canonical_equipment = (
                    equipment_names[0]
                )

            line_parts = [
                f"{index}",
                f"tag {tag_name}",
            ]

            if canonical_equipment:
                line_parts.append(
                    "equipment "
                    + canonical_equipment
                )

            if description:
                line_parts.append(
                    "meaning "
                    + description
                )

            if concepts:
                line_parts.append(
                    "concepts "
                    + ", ".join(
                        concepts[:6]
                    )
                )

            if unit:
                line_parts.append(
                    "unit "
                    + unit
                )

            lines.append(
                " | ".join(
                    line_parts
                )
            )

        return "\n".join(
            lines
        )

    def _request_selection(
        self,
        question: str,
        candidates: list[Any],
    ) -> tuple[
        dict[str, Any],
        str,
        float,
        float,
        float,
    ]:
        candidate_context = (
            self._build_candidate_context(
                candidates
            )
        )

        user_prompt = (
            "QUESTION\n"
            + question
            + "\n\n"
            + candidate_context
        )

        endpoint = (
            f"{self.ollama_url}/api/chat"
        )

        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "format": self.SELECTION_SCHEMA,
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
                "The candidate-selection request timed out "
                f"after {self.timeout_seconds} seconds."
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
                "Ollama returned an invalid HTTP response."
            ) from exc

        raw_content = (
            self._extract_message_content(
                response_data
            )
        )

        try:
            parsed_content = json.loads(
                raw_content
            )

        except json.JSONDecodeError as exc:
            raise LLMQuestionParserError(
                "The language model did not return valid JSON. "
                f"Response: {raw_content}"
            ) from exc

        if not isinstance(
            parsed_content,
            dict,
        ):
            raise LLMQuestionParserError(
                "The language model JSON response was not an object."
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

        return (
            parsed_content,
            raw_content,
            elapsed_seconds,
            load_seconds,
            generation_seconds,
        )

    @staticmethod
    def _safe_integer(
        value: Any,
        default: int = 0,
    ) -> int:
        try:
            return int(
                value
            )
        except (
            TypeError,
            ValueError,
        ):
            return default

    @staticmethod
    def _safe_confidence(
        value: Any,
    ) -> float:
        try:
            confidence = float(
                value
            )
        except (
            TypeError,
            ValueError,
        ):
            return 0.5

        return max(
            0.0,
            min(
                1.0,
                confidence,
            ),
        )

    def _derive_measurement(
        self,
        question: str,
        candidate: Any,
    ) -> str:
        """
        Derive the generic measurement from the operator wording and
        selected configured tag metadata.
        """

        question_text = (
            self._normalize_text(
                question
            )
        )

        for alias, canonical in (
            self.MEASUREMENT_ALIASES.items()
        ):
            if re.search(
                r"\b"
                + re.escape(
                    alias
                )
                + r"\b",
                question_text,
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
                question_text,
            ):
                return measurement

        candidate_text = self._normalize_text(
            " ".join(
                [
                    self._candidate_tag_name(
                        candidate
                    ),
                    self._candidate_description(
                        candidate
                    ),
                    " ".join(
                        self._candidate_concepts(
                            candidate
                        )
                    ),
                ]
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
                return measurement

        return ""

    def _derive_equipment(
        self,
        candidate: Any,
    ) -> str:
        equipment_names = (
            self._candidate_equipment_names(
                candidate
            )
        )

        if not equipment_names:
            return ""

        return self._normalize_identifier(
            equipment_names[0]
        )

    def _build_result(
        self,
        question: str,
        candidates: list[Any],
        selection: dict[str, Any],
        raw_response: str,
        elapsed_seconds: float,
        load_seconds: float,
        generation_seconds: float,
    ) -> ParsedQuestion:
        selected_number = (
            self._safe_integer(
                selection.get(
                    "selected_candidate"
                ),
                default=0,
            )
        )

        intent = self._clean_string(
            selection.get(
                "intent"
            )
        ).lower()

        valid_intents = {
            "current_data",
            "timeline",
            "root_cause",
            "threshold",
            "trend",
            "general",
        }

        if intent not in valid_intents:
            intent = "general"

        location = (
            self._normalize_identifier(
                selection.get(
                    "location"
                )
            )
        )

        condition = (
            self._normalize_identifier(
                selection.get(
                    "condition"
                )
            )
        )

        time_expression = (
            self._normalize_identifier(
                selection.get(
                    "time_expression"
                )
            )
        )

        confidence = (
            self._safe_confidence(
                selection.get(
                    "confidence"
                )
            )
        )

        if (
            selected_number < 1
            or selected_number > len(
                candidates
            )
        ):
            return ParsedQuestion(
                question=question,
                intent=intent,
                equipment="",
                measurement="",
                location=location,
                condition=condition,
                time_expression=time_expression,
                tag_hint="",
                confidence=min(
                    confidence,
                    0.3,
                ),
                elapsed_seconds=elapsed_seconds,
                load_seconds=load_seconds,
                generation_seconds=generation_seconds,
                raw_response=raw_response,
            )

        selected_candidate = candidates[
            selected_number - 1
        ]

        tag_name = (
            self._candidate_tag_name(
                selected_candidate
            )
        )

        equipment = self._derive_equipment(
            selected_candidate
        )

        measurement = (
            self._derive_measurement(
                question=question,
                candidate=selected_candidate,
            )
        )

        similarity = float(
            getattr(
                selected_candidate,
                "similarity",
                0.0,
            )
            or 0.0
        )

        if similarity < 0.50:
            confidence = min(
                confidence,
                0.55,
            )

        if not measurement:
            confidence = min(
                confidence,
                0.50,
            )

        return ParsedQuestion(
            question=question,
            intent=intent,
            equipment=equipment,
            measurement=measurement,
            location=location,
            condition=condition,
            time_expression=time_expression,
            tag_hint=tag_name,
            confidence=round(
                confidence,
                3,
            ),
            elapsed_seconds=elapsed_seconds,
            load_seconds=load_seconds,
            generation_seconds=generation_seconds,
            raw_response=raw_response,
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

        total_start = time.perf_counter()

        candidates = (
            self._retrieve_candidates(
                clean_question
            )
        )

        if not candidates:
            raise LLMQuestionParserError(
                "No configured tag candidates were found."
            )

        (
            selection,
            raw_response,
            _llm_elapsed,
            load_seconds,
            generation_seconds,
        ) = self._request_selection(
            question=clean_question,
            candidates=candidates,
        )

        total_elapsed = round(
            time.perf_counter()
            - total_start,
            3,
        )

        return self._build_result(
            question=clean_question,
            candidates=candidates,
            selection=selection,
            raw_response=raw_response,
            elapsed_seconds=total_elapsed,
            load_seconds=load_seconds,
            generation_seconds=generation_seconds,
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
                        "Reply with only OK."
                    ),
                },
            ],
            "options": {
                "temperature": 0,
                "num_ctx": 256,
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
                "The Qwen warm-up timed out."
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

    def retrieval_preview(
        self,
        question: str,
    ) -> dict[str, Any]:
        candidates = (
            self._retrieve_candidates(
                question
            )
        )

        return {
            "system_prompt_characters": len(
                self.SYSTEM_PROMPT
            ),
            "candidate_context_characters": len(
                self._build_candidate_context(
                    candidates
                )
            ),
            "candidate_count": len(
                candidates
            ),
            "candidate_tags": [
                self._candidate_tag_name(
                    candidate
                )
                for candidate in candidates
            ],
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
        }


def format_result(
    result: ParsedQuestion,
) -> str:
    lines = [
        f"Question: {result.question}",
        f"Intent: {result.intent}",
        (
            "Equipment: "
            f"{result.equipment or 'None'}"
        ),
        (
            "Measurement: "
            f"{result.measurement or 'None'}"
        ),
        (
            "Location: "
            f"{result.location or 'None'}"
        ),
        (
            "Condition: "
            f"{result.condition or 'None'}"
        ),
        (
            "Time expression: "
            f"{result.time_expression or 'None'}"
        ),
        (
            "Selected tag: "
            f"{result.tag_hint or 'None'}"
        ),
        (
            "Confidence: "
            f"{result.confidence:.1%}"
        ),
        (
            "Total time: "
            f"{result.elapsed_seconds:.3f} seconds"
        ),
        (
            "Model load time: "
            f"{result.load_seconds:.3f} seconds"
        ),
        (
            "Generation time: "
            f"{result.generation_seconds:.3f} seconds"
        ),
    ]

    return "\n".join(
        lines
    )


def main() -> None:
    print(
        "SmartMachineAI Candidate Selection Parser V3"
    )

    print(
        "Loading semantic resolver..."
    )

    try:
        parser = CandidateSelectionParser()

    except Exception as exc:
        print(
            f"Startup error: {exc}"
        )
        return

    print(
        "Loading Qwen..."
    )

    try:
        warm_up_seconds = parser.warm_up()

    except Exception as exc:
        print(
            f"Warm-up error: {exc}"
        )
        return

    print(
        f"Qwen ready in "
        f"{warm_up_seconds:.3f} seconds."
    )

    preview_question = (
        "What is the compressor discharge pressure?"
    )

    print()
    print(
        "Retrieval preview:"
    )

    try:
        preview = parser.retrieval_preview(
            preview_question
        )

        print(
            json.dumps(
                preview,
                indent=2,
            )
        )

    except Exception as exc:
        print(
            f"Retrieval preview error: {exc}"
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
                format_result(
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

