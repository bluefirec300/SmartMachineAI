import json
import re
from typing import Any

from ai.ai_provider import AIProvider


class SemanticRouter:
    """
    Uses an AI provider to interpret natural-language questions.

    The AI may suggest routing values, but every result must pass
    validation against the allowed engineering values.
    """

    def __init__(
        self,
        ai: AIProvider,
        allowed_equipment: set[str],
        allowed_measurements: set[str],
        allowed_intents: set[str] | None = None,
    ):
        self.ai = ai
        self.allowed_equipment = {
            item.strip().lower()
            for item in allowed_equipment
            if item.strip()
        }
        self.allowed_measurements = {
            item.strip().lower()
            for item in allowed_measurements
            if item.strip()
        }
        self.allowed_intents = (
            allowed_intents
            or {
                "current",
                "status",
                "trend",
            }
        )

    def build_prompt(
        self,
        question: str,
    ) -> str:
        equipment_options = ", ".join(
            sorted(self.allowed_equipment)
        )
        measurement_options = ", ".join(
            sorted(self.allowed_measurements)
        )
        intent_options = ", ".join(
            sorted(self.allowed_intents)
        )

        return f"""
You are a language interpreter for an industrial
machine monitoring system.

Interpret the operator's question using only the
allowed values below.

Allowed equipment:
{equipment_options}

Allowed measurements:
{measurement_options}

Allowed intents:
{intent_options}

Intent meanings:
- current: asks for the latest value or condition
- status: asks about alarms, faults, warnings, or health
- trend: asks about changes or behaviour over time

Operator question:
{question}

Return valid JSON only in this exact structure:

{{
  "equipment": "one allowed equipment value",
  "measurements": ["zero or more allowed measurement values"],
  "intent": "one allowed intent value"
}}

Do not add explanations.
Do not invent equipment or measurements.
""".strip()

    @staticmethod
    def _extract_json(
        response: str,
    ) -> dict[str, Any] | None:
        if not response or not response.strip():
            return None

        cleaned = response.strip()

        cleaned = re.sub(
            r"^```(?:json)?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\s*```$",
            "",
            cleaned,
        )

        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start == -1 or end == -1 or end < start:
            return None

        try:
            result = json.loads(
                cleaned[start:end + 1]
            )
        except json.JSONDecodeError:
            return None

        if not isinstance(result, dict):
            return None

        return result

    def _validate_result(
        self,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        equipment = result.get("equipment")
        measurements = result.get("measurements", [])
        intent = result.get("intent")

        if not isinstance(equipment, str):
            return None

        if not isinstance(intent, str):
            return None

        equipment = equipment.strip().lower()
        intent = intent.strip().lower()

        if equipment not in self.allowed_equipment:
            return None

        if intent not in self.allowed_intents:
            return None

        if isinstance(measurements, str):
            measurements = [measurements]

        if not isinstance(measurements, list):
            return None

        validated_measurements = []

        for measurement in measurements:
            if not isinstance(measurement, str):
                return None

            normalized = measurement.strip().lower()

            if normalized not in self.allowed_measurements:
                return None

            if normalized not in validated_measurements:
                validated_measurements.append(normalized)

        return {
            "equipment": equipment,
            "measurements": validated_measurements,
            "intent": intent,
        }

    def interpret(
        self,
        question: str,
    ) -> dict[str, Any] | None:
        if not question or not question.strip():
            return None

        prompt = self.build_prompt(question)
        response = self.ai.generate(prompt)

        parsed = self._extract_json(response)

        if parsed is None:
            return None

        return self._validate_result(parsed)
