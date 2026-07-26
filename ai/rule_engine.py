import json
from pathlib import Path
from typing import Any

from ai.trend_analyzer import format_number
from plc.tag_registry import TagRegistry


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "engineering_rules.json"


class RuleEngine:
    def __init__(
        self,
        rules_path: Path | str = DEFAULT_RULES_PATH,
    ):
        self.rules_path = Path(rules_path).resolve()
        self.rules = self._load_rules()

        registry = TagRegistry()

        self.tag_metadata = {
            tag["name"]: tag
            for tag in registry.get_all()
        }

    def _load_rules(self) -> dict[str, dict[str, float]]:
        if not self.rules_path.exists():
            raise FileNotFoundError(
                f"Engineering rules file not found: {self.rules_path}"
            )

        with self.rules_path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError(
                "Engineering rules must contain a JSON object."
            )

        return data

    def _unit(self, tag_name: str) -> str:
        metadata = self.tag_metadata.get(tag_name, {})
        return str(metadata.get("unit", "")).strip()

    @staticmethod
    def _value_text(
        value: float,
        unit: str,
    ) -> str:
        formatted = format_number(value)
        return f"{formatted} {unit}".strip()

    def evaluate_summary(
        self,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        tag_name = summary["tag"]
        current = summary.get("current")

        result = {
            "tag": tag_name,
            "severity": "normal",
            "condition": "within_limits",
            "message": "",
        }

        rules = self.rules.get(tag_name)

        if not rules or current is None:
            result["condition"] = "not_evaluated"
            result["message"] = (
                f"No engineering threshold is configured for {tag_name}."
            )
            return result

        try:
            value = float(current)
        except (TypeError, ValueError):
            result["condition"] = "invalid_value"
            result["message"] = (
                f"{tag_name} cannot be evaluated because its value "
                f"is not numeric."
            )
            return result

        unit = self._unit(tag_name)
        value_text = self._value_text(value, unit)

        low_alarm = rules.get("low_alarm")
        low_warning = rules.get("low_warning")
        high_warning = rules.get("high_warning")
        high_alarm = rules.get("high_alarm")

        if low_alarm is not None and value <= low_alarm:
            result.update(
                {
                    "severity": "alarm",
                    "condition": "low_alarm",
                    "message": (
                        f"{tag_name} is critically low at {value_text}."
                    ),
                }
            )

        elif high_alarm is not None and value >= high_alarm:
            result.update(
                {
                    "severity": "alarm",
                    "condition": "high_alarm",
                    "message": (
                        f"{tag_name} is critically high at {value_text}."
                    ),
                }
            )

        elif low_warning is not None and value <= low_warning:
            result.update(
                {
                    "severity": "warning",
                    "condition": "low_warning",
                    "message": (
                        f"{tag_name} is below its normal range "
                        f"at {value_text}."
                    ),
                }
            )

        elif high_warning is not None and value >= high_warning:
            result.update(
                {
                    "severity": "warning",
                    "condition": "high_warning",
                    "message": (
                        f"{tag_name} is above its normal range "
                        f"at {value_text}."
                    ),
                }
            )

        else:
            result["message"] = (
                f"{tag_name} is within its configured operating limits "
                f"at {value_text}."
            )

        return result

    def evaluate(
        self,
        summaries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            self.evaluate_summary(summary)
            for summary in summaries
        ]


def format_rule_results(
    results: list[dict[str, Any]],
    include_normal: bool = False,
) -> str:
    selected = []

    for result in results:
        if (
            include_normal
            or result["severity"] in {"warning", "alarm"}
        ):
            selected.append(
                f"- [{result['severity'].upper()}] "
                f"{result['message']}"
            )

    if not selected:
        return "- No configured engineering limits are currently exceeded."

    return "\n".join(selected)
