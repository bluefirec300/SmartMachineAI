from typing import Any

from ai.trend_analyzer import format_number
from plc.tag_registry import TagRegistry


class ObservationEngine:
    def __init__(self):
        registry = TagRegistry()

        self.tag_metadata = {
            tag["name"]: tag
            for tag in registry.get_all()
        }

    @staticmethod
    def _is_active(value: Any) -> bool:
        if isinstance(value, bool):
            return value

        if isinstance(value, (int, float)):
            return value != 0

        text = str(value).strip().lower()

        return text in {
            "1",
            "true",
            "on",
            "active",
            "running",
            "open",
            "yes",
        }

    @staticmethod
    def _friendly_name(tag_name: str) -> str:
        replacements = {
            "CompressorPressure": "Compressor discharge pressure",
            "CompressorTemperature": "Compressor temperature",
            "CompressorCurrent": "Compressor motor current",
            "CompressorRunning": "Compressor",
            "CompressorWarning": "Compressor warning",
            "CompressorAlarm": "Compressor alarm",
            "ChillerSupplyTemp": "Chiller supply temperature",
            "ChillerReturnTemp": "Chiller return temperature",
            "ChillerRunning": "Chiller",
            "ChillerAlarm": "Chiller alarm",
            "ColdRoomTemperature": "Cold room temperature",
            "ColdRoomHumidity": "Cold room humidity",
            "ColdRoomDoor": "Cold room door",
            "ColdRoomAlarm": "Cold room alarm",
            "TankLevel": "Tank level",
            "TransferPumpRunning": "Transfer pump",
            "TransferPumpCurrent": "Transfer pump current",
            "TankLowAlarm": "Tank low-level alarm",
            "FactoryEnergyKW": "Factory electrical load",
            "WaterPressure": "Factory water pressure",
            "AirPressure": "Factory compressed-air pressure",
        }

        return replacements.get(tag_name, tag_name)

    def _format_boolean(
        self,
        tag_name: str,
        value: Any,
    ) -> str:
        active = self._is_active(value)

        if tag_name.endswith("Running"):
            equipment = self._friendly_name(tag_name)

            if active:
                return f"{equipment} is running."

            return f"{equipment} is stopped."

        if tag_name.endswith("Alarm"):
            alarm_name = self._friendly_name(tag_name)

            if active:
                return f"{alarm_name} is active."

            return f"No {alarm_name.lower()} is active."

        if tag_name.endswith("Warning"):
            warning_name = self._friendly_name(tag_name)

            if active:
                return f"{warning_name} is active."

            return f"No {warning_name.lower()} is active."

        if tag_name.endswith("Door"):
            door_name = self._friendly_name(tag_name)

            if active:
                return f"{door_name} is open."

            return f"{door_name} is closed."

        name = self._friendly_name(tag_name)
        state = "ON" if active else "OFF"

        return f"{name} is {state}."

    def _format_analogue(
        self,
        summary: dict[str, Any],
        intent: str,
    ) -> list[str]:
        tag_name = summary["tag"]
        name = self._friendly_name(tag_name)
        current = format_number(summary["current"])
        unit = summary.get("unit", "")

        value_text = f"{current} {unit}".strip()

        observations = [
            f"{name} is {value_text}."
        ]

        if intent == "current":
            return observations

        trend = summary.get("trend")

        if trend:
            observations.append(
                f"{name} trend is {trend}."
            )

        change = summary.get("change")

        if change is not None:
            change_text = format_number(change)
            change_with_unit = f"{change_text} {unit}".strip()

            observations.append(
                f"{name} changed by {change_with_unit} "
                f"over the analysed samples."
            )

        minimum = summary.get("minimum")
        maximum = summary.get("maximum")
        average = summary.get("average")

        if (
            minimum is not None
            and maximum is not None
            and average is not None
        ):
            minimum_text = (
                f"{format_number(minimum)} {unit}"
            ).strip()

            maximum_text = (
                f"{format_number(maximum)} {unit}"
            ).strip()

            average_text = (
                f"{format_number(average)} {unit}"
            ).strip()

            observations.append(
                f"Analysed range for {name.lower()}: "
                f"minimum {minimum_text}, "
                f"maximum {maximum_text}, "
                f"average {average_text}."
            )

        return observations

    def build(
        self,
        summaries: list[dict[str, Any]],
        intent: str,
    ) -> list[str]:
        observations = []

        for summary in summaries:
            tag_name = summary["tag"]

            metadata = self.tag_metadata.get(
                tag_name,
                {},
            )

            data_type = str(
                metadata.get("data_type", "")
            ).upper()

            if data_type == "BOOL":
                observations.append(
                    self._format_boolean(
                        tag_name=tag_name,
                        value=summary["current"],
                    )
                )
            else:
                observations.extend(
                    self._format_analogue(
                        summary=summary,
                        intent=intent,
                    )
                )

        return observations


def format_observations(
    observations: list[str],
) -> str:
    if not observations:
        return "No matching machine observations are available."

    return "\n".join(
        f"- {observation}"
        for observation in observations
    )
