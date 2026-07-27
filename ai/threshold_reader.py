import sqlite3
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "database"
    / "config.db"
)


THRESHOLD_LABELS = {
    "low_alarm": "Low alarm",
    "low_warning": "Low warning",
    "high_warning": "High warning",
    "high_alarm": "High alarm",
}


class ThresholdReader:
    """
    Reads engineering threshold configuration from config.db.

    This component reads configured setpoints. It does not evaluate
    live PLC values. Live evaluation remains the responsibility of
    RuleEngine.
    """

    def __init__(
        self,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
    ):
        self.database_path = Path(
            database_path
        ).resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.exists():
            raise FileNotFoundError(
                "Configuration database not found: "
                f"{self.database_path}"
            )

        connection = sqlite3.connect(
            self.database_path
        )

        connection.row_factory = sqlite3.Row

        return connection

    @staticmethod
    def _normalize_text(
        value: str,
    ) -> str:
        return "".join(
            character.lower()
            for character in str(value)
            if character.isalnum()
        )

    def get_all_thresholds(
        self,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    tag_name,
                    low_warning,
                    low_alarm,
                    high_warning,
                    high_alarm
                FROM thresholds
                ORDER BY tag_name
                """
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def get_threshold(
        self,
        tag_name: str,
    ) -> dict[str, Any] | None:
        normalized_requested = self._normalize_text(
            tag_name
        )

        for threshold in self.get_all_thresholds():
            normalized_tag = self._normalize_text(
                threshold["tag_name"]
            )

            if normalized_tag == normalized_requested:
                return threshold

        return None

    def find_thresholds(
        self,
        search_text: str,
    ) -> list[dict[str, Any]]:
        """
        Finds threshold rows using flexible text matching.

        Examples:
            water pressure -> WaterPressure
            tank level -> TankLevel
            compressor -> CompressorPressure,
                          CompressorTemperature,
                          CompressorCurrent
        """
        normalized_search = self._normalize_text(
            search_text
        )

        if not normalized_search:
            return []

        matches = []

        for threshold in self.get_all_thresholds():
            normalized_tag = self._normalize_text(
                threshold["tag_name"]
            )

            if (
                normalized_search in normalized_tag
                or normalized_tag in normalized_search
            ):
                matches.append(
                    threshold
                )

        return matches

    def find_by_words(
        self,
        words: list[str],
    ) -> list[dict[str, Any]]:
        """
        Matches rows containing all meaningful search words.
        """
        normalized_words = [
            self._normalize_text(word)
            for word in words
            if self._normalize_text(word)
        ]

        if not normalized_words:
            return []

        matches = []

        for threshold in self.get_all_thresholds():
            normalized_tag = self._normalize_text(
                threshold["tag_name"]
            )

            if all(
                word in normalized_tag
                for word in normalized_words
            ):
                matches.append(
                    threshold
                )

        return matches

    @staticmethod
    def get_configured_values(
        threshold: dict[str, Any],
    ) -> dict[str, float]:
        configured = {}

        for field_name in THRESHOLD_LABELS:
            value = threshold.get(
                field_name
            )

            if value is not None:
                configured[field_name] = float(
                    value
                )

        return configured


def format_threshold(
    threshold: dict[str, Any],
    unit: str = "",
) -> str:
    tag_name = str(
        threshold["tag_name"]
    )

    lines = [
        f"{tag_name} configured thresholds:"
    ]

    configured_count = 0

    for field_name, label in THRESHOLD_LABELS.items():
        value = threshold.get(
            field_name
        )

        if value is None:
            continue

        configured_count += 1

        value_text = f"{float(value):g}"

        if unit:
            value_text = (
                f"{value_text} {unit}"
            )

        lines.append(
            f"- {label}: {value_text}"
        )

    if configured_count == 0:
        lines.append(
            "- No warning or alarm thresholds configured."
        )

    return "\n".join(
        lines
    )


def format_thresholds(
    thresholds: list[dict[str, Any]],
    units: dict[str, str] | None = None,
) -> str:
    if not thresholds:
        return (
            "No matching engineering thresholds "
            "are configured."
        )

    units = units or {}

    sections = []

    for threshold in thresholds:
        tag_name = str(
            threshold["tag_name"]
        )

        sections.append(
            format_threshold(
                threshold,
                unit=units.get(
                    tag_name,
                    "",
                ),
            )
        )

    return "\n\n".join(
        sections
    )
