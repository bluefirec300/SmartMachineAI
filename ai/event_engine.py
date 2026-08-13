import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai.equipment_knowledge import EquipmentKnowledge
from config.environment import get_config_db_path


EVENT_SEVERITIES = {
    "warning",
    "alarm",
}

NORMAL_CONDITIONS = {
    "within_limits",
    "not_evaluated",
    "invalid_value",
}


class EventEngine:
    """
    Converts abnormal RuleEngine results into standardized events.

    The Event Engine does not write to the database. It only detects
    and builds event dictionaries. Database storage will be added in
    the next milestone.
    """

    def __init__(
        self,
        equipment_knowledge: EquipmentKnowledge | None = None,
        database_path: str | Path | None = None,
    ):
        self.equipment_knowledge = (
            equipment_knowledge
            if equipment_knowledge is not None
            else EquipmentKnowledge()
        )

        self.tag_equipment_map = (
            self._build_tag_equipment_map()
        )

        # config/equipment_knowledge.json only ever described the
        # original ~20-tag hand-scripted demo, retired 2026-08-09 - so
        # for every tag in the current 313-tag P01/Phase2/3 dataset,
        # the map above has no entry and get_equipment_for_tag() was
        # silently falling back to the literal string "factory" for
        # every single live event. This live DB lookup takes priority
        # and covers the real dataset; the map above (and its
        # "factory" fallback) still exists underneath for
        # backward compatibility / injected test doubles.
        self.database_path = (
            Path(database_path) if database_path else get_config_db_path()
        )
        self._live_tag_equipment_map = (
            self._build_live_tag_equipment_map()
        )

    def _build_live_tag_equipment_map(self) -> dict[str, str]:
        if not self.database_path.exists():
            return {}

        connection = sqlite3.connect(self.database_path)

        try:
            rows = connection.execute(
                """
                SELECT tags.tag_name, equipment.display_name
                FROM tags
                LEFT JOIN equipment ON equipment.id = tags.equipment_id
                WHERE tags.enabled = 1 AND equipment.display_name IS NOT NULL
                """
            ).fetchall()

            return {row[0]: row[1] for row in rows}
        finally:
            connection.close()

    def _build_tag_equipment_map(
        self,
    ) -> dict[str, str]:
        """
        Build a lookup table:

            tag name -> equipment name

        Main and related equipment tags are both included.
        """
        mapping: dict[str, str] = {}

        equipment_definitions = (
            self.equipment_knowledge.get_all()
        )

        for equipment_name, equipment in (
            equipment_definitions.items()
        ):
            main_tags = equipment.get(
                "main_tags",
                [],
            )

            related_tags = equipment.get(
                "related_tags",
                [],
            )

            for tag_name in [
                *main_tags,
                *related_tags,
            ]:
                clean_tag_name = str(
                    tag_name
                ).strip()

                if not clean_tag_name:
                    continue

                mapping.setdefault(
                    clean_tag_name,
                    equipment_name,
                )

        return mapping

    def reload(self) -> None:
        """
        Reload equipment knowledge and rebuild tag relationships.
        """
        self.equipment_knowledge.reload()

        self.tag_equipment_map = (
            self._build_tag_equipment_map()
        )

        self._live_tag_equipment_map = (
            self._build_live_tag_equipment_map()
        )

    def get_equipment_for_tag(
        self,
        tag_name: str,
    ) -> str:
        """
        Return the equipment associated with a tag - the live
        database (config.db's tags/equipment tables, the real,
        current source of truth) first, then the legacy
        equipment_knowledge map (JSON file or an injected test
        double). Unknown tags are assigned to factory so historian
        data from external or older configurations can still create
        events.
        """
        if tag_name in self._live_tag_equipment_map:
            return self._live_tag_equipment_map[tag_name]

        return self.tag_equipment_map.get(
            tag_name,
            "factory",
        )

    @staticmethod
    def _current_timestamp() -> str:
        """
        Return an ISO-8601 UTC timestamp.
        """
        return datetime.now(
            timezone.utc
        ).isoformat(
            timespec="seconds"
        )

    @staticmethod
    def _summary_by_tag(
        summaries: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """
        Index trend summaries by tag name.
        """
        result: dict[str, dict[str, Any]] = {}

        for summary in summaries:
            tag_name = str(
                summary.get("tag", "")
            ).strip()

            if tag_name:
                result[tag_name] = summary

        return result

    @staticmethod
    def _is_significant(
        rule_result: dict[str, Any],
    ) -> bool:
        """
        Only warnings and alarms are significant events.
        """
        severity = str(
            rule_result.get(
                "severity",
                "",
            )
        ).strip().lower()

        condition = str(
            rule_result.get(
                "condition",
                "",
            )
        ).strip().lower()

        if severity not in EVENT_SEVERITIES:
            return False

        if condition in NORMAL_CONDITIONS:
            return False

        return True

    def build_event(
        self,
        rule_result: dict[str, Any],
        summary: dict[str, Any] | None = None,
        detected_at: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Convert one significant RuleEngine result into an event.

        Returns None when the rule result is normal or not evaluated.
        """
        if not self._is_significant(
            rule_result
        ):
            return None

        summary = summary or {}

        tag_name = str(
            rule_result.get(
                "tag",
                "",
            )
        ).strip()

        if not tag_name:
            return None

        severity = str(
            rule_result.get(
                "severity",
                "",
            )
        ).strip().lower()

        condition = str(
            rule_result.get(
                "condition",
                "",
            )
        ).strip().lower()

        message = str(
            rule_result.get(
                "message",
                "",
            )
        ).strip()

        updated = summary.get(
            "updated"
        )

        event_time = (
            str(updated).strip()
            if updated is not None
            and str(updated).strip()
            else detected_at
            or self._current_timestamp()
        )

        return {
            "event_time": event_time,
            "detected_at": (
                detected_at
                or self._current_timestamp()
            ),
            "equipment": (
                self.get_equipment_for_tag(
                    tag_name
                )
            ),
            "tag": tag_name,
            "severity": severity,
            "condition": condition,
            "value": summary.get(
                "current"
            ),
            "unit": str(
                summary.get(
                    "unit",
                    "",
                )
            ).strip(),
            "trend": summary.get(
                "trend"
            ),
            "message": message,
            "address": summary.get(
                "address"
            ),
            "samples": summary.get(
                "samples"
            ),
            "minimum": summary.get(
                "minimum"
            ),
            "maximum": summary.get(
                "maximum"
            ),
            "average": summary.get(
                "average"
            ),
            "change": summary.get(
                "change"
            ),
        }

    def detect(
        self,
        rule_results: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        detected_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Detect significant events from RuleEngine results.

        A matching trend summary is attached when available.
        """
        summaries_by_tag = self._summary_by_tag(
            summaries
        )

        events: list[dict[str, Any]] = []

        for rule_result in rule_results:
            tag_name = str(
                rule_result.get(
                    "tag",
                    "",
                )
            ).strip()

            summary = summaries_by_tag.get(
                tag_name
            )

            event = self.build_event(
                rule_result=rule_result,
                summary=summary,
                detected_at=detected_at,
            )

            if event is not None:
                events.append(event)

        return events


def format_events(
    events: list[dict[str, Any]],
) -> str:
    """
    Convert detected events into readable text.

    This will later be usable inside the AI prompt and diagnostic logs.
    """
    if not events:
        return "No significant events detected."

    lines: list[str] = []

    for event in events:
        equipment = str(
            event.get(
                "equipment",
                "factory",
            )
        )

        tag_name = str(
            event.get(
                "tag",
                "unknown",
            )
        )

        severity = str(
            event.get(
                "severity",
                "unknown",
            )
        ).upper()

        condition = str(
            event.get(
                "condition",
                "unknown",
            )
        ).replace(
            "_",
            " ",
        )

        value = event.get(
            "value"
        )

        unit = str(
            event.get(
                "unit",
                "",
            )
        ).strip()

        value_text = (
            f"{value} {unit}".strip()
            if value is not None
            else "N/A"
        )

        lines.append(
            f"[{severity}] "
            f"{equipment} / {tag_name}: "
            f"{condition} at {value_text}"
        )

    return "\n".join(lines)
