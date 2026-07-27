from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

import requests


VALID_INTENTS = {
    "current_data",
    "timeline",
    "root_cause",
    "threshold",
    "trend",
    "general",
}


@dataclass(frozen=True)
class ParsedQuestion:
    question: str
    intent: str
    equipment: str
    measurement: str
    location: str
    condition: str
    time_expression: str
    tag_hint: str
    confidence: float
    elapsed_seconds: float
    load_seconds: float
    generation_seconds: float
    raw_response: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LLMQuestionParserError(RuntimeError):
    pass


class LLMQuestionParser:
    """
    Uses a local Ollama model only for natural-language understanding.

    It does not answer questions, access the PLC, generate SQL,
    or invent PLC tag names.
    """

    RESPONSE_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
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
            "equipment": {
                "type": "string",
            },
            "measurement": {
                "type": "string",
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
            "tag_hint": {
                "type": "string",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
        "required": [
            "intent",
            "equipment",
            "measurement",
            "location",
            "condition",
            "time_expression",
            "tag_hint",
            "confidence",
        ],
        "additionalProperties": False,
    }

    SYSTEM_PROMPT = """
You are an industrial automation question parser.

Return JSON only.
Do not answer the question.
Do not explain.
Do not generate SQL or Python.
Do not invent PLC tag names.

Allowed intents:

current_data:
Latest value or present equipment state.

timeline:
Past alarms, warnings, trips, faults, events, or event history.

root_cause:
Why something happened or what caused an abnormal condition.

threshold:
Alarm limit, warning limit, setpoint, or configured threshold.

trend:
How a value changed over time.

general:
Cannot reliably classify.

Fields:

equipment:
Equipment name in lowercase snake_case.
Return empty string when unknown.

measurement:
Physical quantity or state, such as pressure, temperature,
level, flow, current, power, energy, running, alarm, warning,
humidity, or door.
Return empty string when unknown.

location:
Location or qualifier, such as return, supply, discharge,
suction, inlet, outlet, motor, or room.
Return empty string when absent.

condition:
State such as high, low, hot, cold, running, stopped,
open, closed, increasing, decreasing, or abnormal.
Return empty string when absent.

time_expression:
Use normalized values such as latest, now, today, yesterday,
last_hour, last_30_minutes, last_7_days, or recent.
For current_data without an explicit time, use latest.

tag_hint:
A short natural-language description for semantic tag search.
Do not write a PLC tag name.

confidence:
A number from 0.0 to 1.0.
Lower it when the meaning is unclear.
""".strip()

    def __init__(
        self,
        model: str = "qwen2.5:3b",
        ollama_url: str = "http://127.0.0.1:11434",
        timeout_seconds: int = 60,
        keep_alive: str = "30m",
    ) -> None:
        self.model = model
        self.ollama_url = ollama_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.keep_alive = keep_alive

        self.session = requests.Session()

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""

        return str(value).strip()

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        text = LLMQuestionParser._clean_text(value).lower()

        text = text.replace("-", "_")
        text = text.replace(" ", "_")

        while "__" in text:
            text = text.replace("__", "_")

        return text.strip("_")

    @staticmethod
    def _normalize_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = 0.0

        return round(
            max(
                0.0,
                min(1.0, confidence),
            ),
            3,
        )

    @staticmethod
    def _nanoseconds_to_seconds(value: Any) -> float:
        try:
            nanoseconds = int(value)
        except (TypeError, ValueError):
            return 0.0

        return round(
            nanoseconds / 1_000_000_000,
            3,
        )

    @staticmethod
    def _extract_message_content(
        response_data: dict[str, Any],
    ) -> str:
        message = response_data.get("message")

        if not isinstance(message, dict):
            raise LLMQuestionParserError(
                "Ollama response did not contain a valid message."
            )

        content = message.get("content")

        if not isinstance(content, str) or not content.strip():
            raise LLMQuestionParserError(
                "Ollama returned an empty parser response."
            )

        return content.strip()

    @staticmethod
    def _load_json(content: str) -> dict[str, Any]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMQuestionParserError(
                "The language model did not return valid JSON. "
                f"Response: {content}"
            ) from exc

        if not isinstance(parsed, dict):
            raise LLMQuestionParserError(
                "The language model response must be a JSON object."
            )

        return parsed

    def _validate_result(
        self,
        question: str,
        parsed: dict[str, Any],
        raw_response: str,
        elapsed_seconds: float,
        load_seconds: float,
        generation_seconds: float,
    ) -> ParsedQuestion:
        intent = self._normalize_identifier(
            parsed.get("intent")
        )

        if intent not in VALID_INTENTS:
            intent = "general"

        equipment = self._normalize_identifier(
            parsed.get("equipment")
        )

        measurement = self._normalize_identifier(
            parsed.get("measurement")
        )

        location = self._normalize_identifier(
            parsed.get("location")
        )

        condition = self._normalize_identifier(
            parsed.get("condition")
        )

        time_expression = self._normalize_identifier(
            parsed.get("time_expression")
        )

        tag_hint = self._clean_text(
            parsed.get("tag_hint")
        ).lower()

        confidence = self._normalize_confidence(
            parsed.get("confidence")
        )

        if (
            intent == "current_data"
            and not time_expression
        ):
            time_expression = "latest"

        if not tag_hint:
            hint_parts = [
                equipment.replace("_", " "),
                location.replace("_", " "),
                measurement.replace("_", " "),
                condition.replace("_", " "),
            ]

            tag_hint = " ".join(
                part
                for part in hint_parts
                if part
            ).strip()

        if not equipment and not measurement:
            confidence = min(
                confidence,
                0.4,
            )

        return ParsedQuestion(
            question=question,
            intent=intent,
            equipment=equipment,
            measurement=measurement,
            location=location,
            condition=condition,
            time_expression=time_expression,
            tag_hint=tag_hint,
            confidence=confidence,
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
                "num_ctx": 2048,
                "num_predict": 160,
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
                f"{self.ollama_url}. Check that Ollama is running."
            ) from exc

        except requests.Timeout as exc:
            raise LLMQuestionParserError(
                "The Ollama parser request timed out."
            ) from exc

        except requests.RequestException as exc:
            raise LLMQuestionParserError(
                f"Ollama request failed: {exc}"
            ) from exc

        elapsed_seconds = round(
            time.perf_counter() - request_start,
            3,
        )

        if response.status_code != 200:
            error_message = response.text

            try:
                error_data = response.json()

                if isinstance(error_data, dict):
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
                f"{response.status_code}: {error_message}"
            )

        try:
            response_data = response.json()
        except ValueError as exc:
            raise LLMQuestionParserError(
                "Ollama returned an invalid HTTP JSON response."
            ) from exc

        raw_content = self._extract_message_content(
            response_data
        )

        parsed_content = self._load_json(
            raw_content
        )

        load_seconds = self._nanoseconds_to_seconds(
            response_data.get("load_duration")
        )

        generation_seconds = self._nanoseconds_to_seconds(
            response_data.get("eval_duration")
        )

        return self._validate_result(
            question=clean_question,
            parsed=parsed_content,
            raw_response=raw_content,
            elapsed_seconds=elapsed_seconds,
            load_seconds=load_seconds,
            generation_seconds=generation_seconds,
        )

    def warm_up(self) -> float:
        """
        Loads the model into memory before the first real question.
        """

        start_time = time.perf_counter()

        self.parse(
            "What is the compressor pressure?"
        )

        return round(
            time.perf_counter() - start_time,
            3,
        )


def format_parsed_question(
    result: ParsedQuestion,
) -> str:
    lines = [
        f"Question: {result.question}",
        f"Intent: {result.intent}",
        f"Equipment: {result.equipment or 'None'}",
        f"Measurement: {result.measurement or 'None'}",
        f"Location: {result.location or 'None'}",
        f"Condition: {result.condition or 'None'}",
        (
            "Time expression: "
            f"{result.time_expression or 'None'}"
        ),
        f"Tag hint: {result.tag_hint or 'None'}",
        f"Confidence: {result.confidence:.3f}",
        f"Total time: {result.elapsed_seconds:.3f} seconds",
        f"Model load time: {result.load_seconds:.3f} seconds",
        (
            "Generation time: "
            f"{result.generation_seconds:.3f} seconds"
        ),
    ]

    return "\n".join(lines)


def main() -> None:
    print(
        "SmartMachineAI LLM Question Parser"
    )
    print(
        "Model: qwen2.5:3b"
    )
    print(
        "Keeping model loaded for 30 minutes."
    )

    parser = LLMQuestionParser()

    print()
    print(
        "Loading AI model..."
    )

    try:
        warm_up_time = parser.warm_up()

        print(
            f"Model ready in {warm_up_time:.3f} seconds."
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
                "Structured data:"
            )

            output_data = result.to_dict()
            output_data.pop(
                "raw_response",
                None,
            )

            print(
                json.dumps(
                    output_data,
                    indent=2,
                )
            )

        except Exception as exc:
            print(
                f"Parser error: {exc}"
            )


if __name__ == "__main__":
    main()
