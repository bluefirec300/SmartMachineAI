import json
import math
from pathlib import Path
from typing import Any

from ai.trend_analyzer import format_number
from config.configuration_manager import ConfigurationManager
from config.configuration_service import get_configuration
from config.environment import get_config_db_path
from plc.tag_registry import TagRegistry


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_RULES_PATH = (
    PROJECT_ROOT
    / "config"
    / "engineering_rules.json"
)

DEFAULT_DATABASE_PATH = get_config_db_path()


class RuleEngine:
    """
    Evaluates tag summaries against engineering thresholds.

    Primary source:
        database/config.db

    Temporary fallback:
        config/engineering_rules.json
    """

    def __init__(
        self,
        rules_path: Path | str = DEFAULT_RULES_PATH,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
        allow_json_fallback: bool = True,
    ):
        self.rules_path = Path(rules_path).resolve()
        self.database_path = Path(database_path).resolve()
        self.allow_json_fallback = allow_json_fallback

        self.source = ""
        self.rules = self._load_rules()

        registry = TagRegistry(
            database_path=self.database_path,
            allow_json_fallback=allow_json_fallback,
        )

        self.tag_metadata = {
            tag["name"]: tag
            for tag in registry.get_all()
        }

    def _load_rules(
        self,
    ) -> dict[str, dict[str, float]]:
        database_error: Exception | None = None

        try:
            rules = self._load_from_database()

            if rules:
                self.source = "database"
                return rules

        except Exception as error:
            database_error = error

        if self.allow_json_fallback:
            rules = self._load_from_json()
            self.source = "json"
            return rules

        if database_error is not None:
            raise RuntimeError(
                "Unable to load engineering thresholds "
                "from configuration database."
            ) from database_error

        raise RuntimeError(
            "Configuration database contains no engineering "
            "thresholds and JSON fallback is disabled."
        )

    def _load_from_database(
        self,
    ) -> dict[str, dict[str, float]]:
        if not self.database_path.exists():
            raise FileNotFoundError(
                f"Configuration database not found: "
                f"{self.database_path}"
            )

        default_database_path = DEFAULT_DATABASE_PATH.resolve()

        if self.database_path == default_database_path:
            config = get_configuration()
        else:
            config = ConfigurationManager(
                database_path=self.database_path
            )

        threshold_rows = config.get_thresholds()

        rules: dict[str, dict[str, float]] = {}

        threshold_names = (
            "low_warning",
            "low_alarm",
            "high_warning",
            "high_alarm",
        )

        for row in threshold_rows:
            tag_name = str(row["tag_name"])
            tag_rules: dict[str, float] = {}

            for threshold_name in threshold_names:
                value = row.get(threshold_name)

                if value is not None:
                    tag_rules[threshold_name] = float(value)

            if tag_rules:
                rules[tag_name] = tag_rules

        return rules

    def _load_from_json(
        self,
    ) -> dict[str, dict[str, float]]:
        if not self.rules_path.exists():
            raise FileNotFoundError(
                f"Engineering rules file not found: "
                f"{self.rules_path}"
            )

        with self.rules_path.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError(
                "Engineering rules must contain a JSON object."
            )

        return data

    def reload(self) -> None:
        self.rules = self._load_rules()

    def get_source(self) -> str:
        return self.source

    def _unit(self, tag_name: str) -> str:
        metadata = self.tag_metadata.get(tag_name, {})

        return str(
            metadata.get("unit", "")
        ).strip()

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
                f"No engineering threshold is configured "
                f"for {tag_name}."
            )

            return result

        try:
            value = float(current)

        except (TypeError, ValueError):
            result["condition"] = "invalid_value"
            result["message"] = (
                f"{tag_name} cannot be evaluated because "
                f"its value is not numeric."
            )

            return result

        # Phase 16.1 correctness fix: float(current) does NOT raise for
        # NaN/+-inf, so without this explicit check a NaN/infinite
        # reading fell through to the alarm/warning comparisons below -
        # every comparison against NaN is False, and every comparison
        # against +-inf is trivially True/False depending on direction -
        # so a NaN reading was previously silently reported as
        # "within_limits"/"normal" instead of "invalid_value". This is
        # the same objective-invalidity check the new Data Health engine
        # (engine/data_health_engine.py) uses, kept in sync deliberately.
        if math.isnan(value) or math.isinf(value):
            result["condition"] = "invalid_value"
            result["message"] = (
                f"{tag_name} cannot be evaluated because "
                f"its value ({value}) is not a valid finite number."
            )

            return result

        unit = self._unit(tag_name)
        value_text = self._value_text(value, unit)

        low_alarm = rules.get("low_alarm")
        low_warning = rules.get("low_warning")
        high_warning = rules.get("high_warning")
        high_alarm = rules.get("high_alarm")

        if (
            low_alarm is not None
            and value <= low_alarm
        ):
            result.update(
                {
                    "severity": "alarm",
                    "condition": "low_alarm",
                    "message": (
                        f"{tag_name} is critically low at {value_text} "
                        f"(low alarm limit: {self._value_text(low_alarm, unit)})."
                    ),
                }
            )

        elif (
            high_alarm is not None
            and value >= high_alarm
        ):
            result.update(
                {
                    "severity": "alarm",
                    "condition": "high_alarm",
                    "message": (
                        f"{tag_name} is critically high at {value_text} "
                        f"(high alarm limit: {self._value_text(high_alarm, unit)})."
                    ),
                }
            )

        elif (
            low_warning is not None
            and value <= low_warning
        ):
            result.update(
                {
                    "severity": "warning",
                    "condition": "low_warning",
                    "message": (
                        f"{tag_name} is below its normal range at {value_text} "
                        f"(low warning limit: {self._value_text(low_warning, unit)})."
                    ),
                }
            )

        elif (
            high_warning is not None
            and value >= high_warning
        ):
            result.update(
                {
                    "severity": "warning",
                    "condition": "high_warning",
                    "message": (
                        f"{tag_name} is above its normal range at {value_text} "
                        f"(high warning limit: {self._value_text(high_warning, unit)})."
                    ),
                }
            )

        else:
            result["message"] = (
                f"{tag_name} is within its configured "
                f"operating limits at {value_text}."
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
            or result["severity"] in {
                "warning",
                "alarm",
            }
        ):
            selected.append(
                f"- [{result['severity'].upper()}] "
                f"{result['message']}"
            )

    if not selected:
        return (
            "- No configured engineering limits "
            "are currently exceeded."
        )

    return "\n".join(selected)
