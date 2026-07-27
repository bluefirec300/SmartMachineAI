import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_DATABASE_PATH = (
    Path(__file__).resolve().parent.parent
    / "database"
    / "machine_data.db"
)


@dataclass(frozen=True)
class EvidenceItem:
    """
    One factual item used during root-cause analysis.
    """

    event_time: str
    equipment: str
    tag: str
    severity: str
    condition: str
    value: Any
    unit: str
    trend: str
    message: str

    def to_text(self) -> str:
        value_text = ""

        if self.value is not None:
            value_text = str(self.value)

            if self.unit:
                value_text += f" {self.unit}"

        details = [
            self.event_time,
            f"[{self.severity.upper()}]",
            self.equipment,
            self.tag,
            self.condition,
        ]

        if value_text:
            details.append(value_text)

        if self.trend:
            details.append(
                f"trend={self.trend}"
            )

        return " | ".join(
            item
            for item in details
            if item
        )


@dataclass(frozen=True)
class RootCauseCandidate:
    """
    A possible contributing condition.

    This is an engineering inference and not a confirmed
    mechanical or electrical diagnosis.
    """

    cause: str
    confidence: float
    reasoning: str
    supporting_tags: tuple[str, ...] = ()


@dataclass
class RootCauseResult:
    """
    Structured result returned by RootCauseEngine.
    """

    found: bool
    equipment: str = ""
    alarm_tag: str = ""
    alarm_condition: str = ""
    alarm_time: str = ""
    alarm_message: str = ""
    probable_causes: list[RootCauseCandidate] = field(
        default_factory=list
    )
    evidence: list[EvidenceItem] = field(
        default_factory=list
    )
    limitations: list[str] = field(
        default_factory=list
    )

    @property
    def confidence(self) -> float:
        if not self.probable_causes:
            return 0.0

        return max(
            candidate.confidence
            for candidate in self.probable_causes
        )


class RootCauseEngine:
    """
    Performs deterministic root-cause investigation using
    stored machine events.

    Processing:

        1. Find the latest matching alarm.
        2. Read events occurring before that alarm.
        3. Identify abnormal event sequences.
        4. Generate possible contributing causes.
        5. Return structured evidence and limitations.

    The engine does not confirm equipment failure. Its output
    is an engineering inference based only on recorded data.
    """

    def __init__(
        self,
        database_path: str | Path | None = None,
    ) -> None:
        self.database_path = Path(
            database_path
            or DEFAULT_DATABASE_PATH
        )

    def _connect(
        self,
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path)
        )

        connection.row_factory = sqlite3.Row

        return connection

    @staticmethod
    def _row_to_evidence(
        row: sqlite3.Row,
    ) -> EvidenceItem:
        return EvidenceItem(
            event_time=str(
                row["event_time"] or ""
            ),
            equipment=str(
                row["equipment"] or ""
            ),
            tag=str(
                row["tag"] or ""
            ),
            severity=str(
                row["severity"] or ""
            ),
            condition=str(
                row["condition"] or ""
            ),
            value=row["value"],
            unit=str(
                row["unit"] or ""
            ),
            trend=str(
                row["trend"] or ""
            ),
            message=str(
                row["message"] or ""
            ),
        )

    def _find_latest_alarm(
        self,
        equipment: str | None = None,
        tag: str | None = None,
    ) -> sqlite3.Row | None:
        conditions = [
            """
            LOWER(severity) IN (
                'alarm',
                'critical',
                'high'
            )
            """
        ]

        parameters: list[Any] = []

        if equipment:
            conditions.append(
                "LOWER(equipment) = LOWER(?)"
            )
            parameters.append(
                equipment
            )

        if tag:
            conditions.append(
                "LOWER(tag) = LOWER(?)"
            )
            parameters.append(
                tag
            )

        query = f"""
            SELECT
                id,
                event_time,
                detected_at,
                equipment,
                tag,
                severity,
                condition,
                value,
                unit,
                trend,
                message,
                address,
                samples,
                minimum,
                maximum,
                average,
                change,
                created_at
            FROM machine_events
            WHERE {" AND ".join(conditions)}
            ORDER BY
                event_time DESC,
                id DESC
            LIMIT 1
        """

        with self._connect() as connection:
            return connection.execute(
                query,
                parameters,
            ).fetchone()

    def _find_preceding_events(
        self,
        alarm: sqlite3.Row,
        limit: int,
        same_equipment_only: bool,
    ) -> list[sqlite3.Row]:
        conditions = [
            "event_time <= ?",
            "id != ?",
        ]

        parameters: list[Any] = [
            alarm["event_time"],
            alarm["id"],
        ]

        equipment = str(
            alarm["equipment"] or ""
        ).strip()

        if (
            same_equipment_only
            and equipment
        ):
            conditions.append(
                "LOWER(equipment) = LOWER(?)"
            )
            parameters.append(
                equipment
            )

        parameters.append(
            limit
        )

        query = f"""
            SELECT
                id,
                event_time,
                detected_at,
                equipment,
                tag,
                severity,
                condition,
                value,
                unit,
                trend,
                message,
                address,
                samples,
                minimum,
                maximum,
                average,
                change,
                created_at
            FROM machine_events
            WHERE {" AND ".join(conditions)}
            ORDER BY
                event_time DESC,
                id DESC
            LIMIT ?
        """

        with self._connect() as connection:
            rows = connection.execute(
                query,
                parameters,
            ).fetchall()

        return list(
            reversed(rows)
        )

    @staticmethod
    def _contains_any(
        text: str,
        terms: set[str],
    ) -> bool:
        normalized = text.lower()

        return any(
            term in normalized
            for term in terms
        )

    @staticmethod
    def _event_text(
        event: EvidenceItem,
    ) -> str:
        return " ".join(
            [
                event.tag,
                event.condition,
                event.trend,
                event.message,
            ]
        ).lower()

    @staticmethod
    def _unique_tags(
        events: list[EvidenceItem],
    ) -> tuple[str, ...]:
        tags: list[str] = []

        for event in events:
            if (
                event.tag
                and event.tag not in tags
            ):
                tags.append(
                    event.tag
                )

        return tuple(tags)

    def _build_candidates(
        self,
        alarm: EvidenceItem,
        preceding: list[EvidenceItem],
    ) -> list[RootCauseCandidate]:
        candidates: list[RootCauseCandidate] = []

        alarm_text = self._event_text(
            alarm
        )

        pressure_alarm = self._contains_any(
            alarm_text,
            {
                "pressure",
            },
        )

        temperature_alarm = self._contains_any(
            alarm_text,
            {
                "temperature",
                "temp",
                "overheat",
                "overheating",
            },
        )

        flow_alarm = self._contains_any(
            alarm_text,
            {
                "flow",
                "flowrate",
            },
        )

        level_alarm = self._contains_any(
            alarm_text,
            {
                "level",
                "tank",
            },
        )

        decreasing_flow_events = [
            event
            for event in preceding
            if self._contains_any(
                self._event_text(event),
                {
                    "flow",
                    "flowrate",
                },
            )
            and self._contains_any(
                self._event_text(event),
                {
                    "decreasing",
                    "falling",
                    "low",
                    "zero",
                    "stopped",
                },
            )
        ]

        stopped_equipment_events = [
            event
            for event in preceding
            if self._contains_any(
                self._event_text(event),
                {
                    "stopped",
                    "stop",
                    "off",
                    "not running",
                    "trip",
                    "tripped",
                    "fault",
                },
            )
        ]

        valve_events = [
            event
            for event in preceding
            if self._contains_any(
                self._event_text(event),
                {
                    "valve",
                },
            )
            and self._contains_any(
                self._event_text(event),
                {
                    "closed",
                    "closing",
                    "close",
                    "fault",
                    "not open",
                },
            )
        ]

        increasing_temperature_events = [
            event
            for event in preceding
            if self._contains_any(
                self._event_text(event),
                {
                    "temperature",
                    "temp",
                },
            )
            and self._contains_any(
                self._event_text(event),
                {
                    "increasing",
                    "rising",
                    "high",
                    "overheat",
                },
            )
        ]

        low_level_events = [
            event
            for event in preceding
            if self._contains_any(
                self._event_text(event),
                {
                    "level",
                    "tank",
                },
            )
            and self._contains_any(
                self._event_text(event),
                {
                    "low",
                    "decreasing",
                    "falling",
                    "empty",
                },
            )
        ]

        electrical_events = [
            event
            for event in preceding
            if self._contains_any(
                self._event_text(event),
                {
                    "current",
                    "voltage",
                    "overload",
                    "motor",
                    "drive",
                    "inverter",
                    "vfd",
                    "trip",
                    "fault",
                },
            )
        ]

        if (
            pressure_alarm
            and decreasing_flow_events
        ):
            candidates.append(
                RootCauseCandidate(
                    cause=(
                        "Possible downstream restriction "
                        "or reduced process flow"
                    ),
                    confidence=0.82,
                    reasoning=(
                        "Flow-related events were decreasing "
                        "or abnormal before the pressure alarm."
                    ),
                    supporting_tags=self._unique_tags(
                        decreasing_flow_events
                    ),
                )
            )

        if (
            pressure_alarm
            and valve_events
        ):
            candidates.append(
                RootCauseCandidate(
                    cause=(
                        "Possible closed or restricted valve"
                    ),
                    confidence=0.88,
                    reasoning=(
                        "A valve closing, closed-state, or "
                        "valve fault event occurred before "
                        "the pressure alarm."
                    ),
                    supporting_tags=self._unique_tags(
                        valve_events
                    ),
                )
            )

        if (
            flow_alarm
            and stopped_equipment_events
        ):
            candidates.append(
                RootCauseCandidate(
                    cause=(
                        "Possible pump, motor, or process "
                        "equipment stoppage"
                    ),
                    confidence=0.88,
                    reasoning=(
                        "A stopped, tripped, or faulted "
                        "equipment event occurred before "
                        "the flow alarm."
                    ),
                    supporting_tags=self._unique_tags(
                        stopped_equipment_events
                    ),
                )
            )

        if (
            temperature_alarm
            and stopped_equipment_events
        ):
            candidates.append(
                RootCauseCandidate(
                    cause=(
                        "Possible loss of cooling or "
                        "circulation"
                    ),
                    confidence=0.77,
                    reasoning=(
                        "Equipment stopped or tripped before "
                        "the temperature alarm, which may "
                        "have reduced cooling or circulation."
                    ),
                    supporting_tags=self._unique_tags(
                        stopped_equipment_events
                    ),
                )
            )

        if (
            temperature_alarm
            and increasing_temperature_events
        ):
            candidates.append(
                RootCauseCandidate(
                    cause=(
                        "Progressive thermal rise before alarm"
                    ),
                    confidence=0.80,
                    reasoning=(
                        "Temperature-related events showed "
                        "an increasing or high condition "
                        "before the alarm."
                    ),
                    supporting_tags=self._unique_tags(
                        increasing_temperature_events
                    ),
                )
            )

        if (
            level_alarm
            and decreasing_flow_events
        ):
            candidates.append(
                RootCauseCandidate(
                    cause=(
                        "Possible insufficient inlet flow"
                    ),
                    confidence=0.76,
                    reasoning=(
                        "Flow decreased before the level "
                        "alarm was detected."
                    ),
                    supporting_tags=self._unique_tags(
                        decreasing_flow_events
                    ),
                )
            )

        if (
            flow_alarm
            and low_level_events
        ):
            candidates.append(
                RootCauseCandidate(
                    cause=(
                        "Possible insufficient source level "
                        "or dry-running condition"
                    ),
                    confidence=0.78,
                    reasoning=(
                        "A low or decreasing source-level "
                        "condition occurred before the "
                        "flow alarm."
                    ),
                    supporting_tags=self._unique_tags(
                        low_level_events
                    ),
                )
            )

        if electrical_events:
            candidates.append(
                RootCauseCandidate(
                    cause=(
                        "Possible electrical drive or motor "
                        "condition"
                    ),
                    confidence=0.70,
                    reasoning=(
                        "Electrical, motor, VFD, overload, "
                        "trip, or fault events were recorded "
                        "before the alarm."
                    ),
                    supporting_tags=self._unique_tags(
                        electrical_events
                    ),
                )
            )

        if (
            not candidates
            and preceding
        ):
            abnormal_events = [
                event
                for event in preceding
                if event.severity.lower()
                in {
                    "warning",
                    "alarm",
                    "critical",
                    "high",
                }
            ]

            if abnormal_events:
                candidates.append(
                    RootCauseCandidate(
                        cause=(
                            "A preceding abnormal machine "
                            "condition may have contributed"
                        ),
                        confidence=0.55,
                        reasoning=(
                            "One or more abnormal events "
                            "occurred before the selected "
                            "alarm, but no specific causal "
                            "rule matched the sequence."
                        ),
                        supporting_tags=self._unique_tags(
                            abnormal_events
                        ),
                    )
                )

        return sorted(
            candidates,
            key=lambda item: item.confidence,
            reverse=True,
        )

    def analyze_latest_alarm(
        self,
        equipment: str | None = None,
        tag: str | None = None,
        preceding_event_limit: int = 20,
        same_equipment_only: bool = True,
    ) -> RootCauseResult:
        """
        Analyze the latest matching alarm.

        Args:
            equipment:
                Optional exact equipment filter.

            tag:
                Optional exact alarm-tag filter.

            preceding_event_limit:
                Maximum number of events to inspect before
                the alarm.

            same_equipment_only:
                When True, only preceding events belonging to
                the alarm equipment are examined.
        """
        if preceding_event_limit < 1:
            raise ValueError(
                "preceding_event_limit must be at least 1."
            )

        alarm_row = self._find_latest_alarm(
            equipment=equipment,
            tag=tag,
        )

        if alarm_row is None:
            return RootCauseResult(
                found=False,
                limitations=[
                    (
                        "No matching alarm was found in "
                        "machine_events."
                    )
                ],
            )

        alarm = self._row_to_evidence(
            alarm_row
        )

        preceding_rows = self._find_preceding_events(
            alarm=alarm_row,
            limit=preceding_event_limit,
            same_equipment_only=same_equipment_only,
        )

        preceding = [
            self._row_to_evidence(row)
            for row in preceding_rows
        ]

        candidates = self._build_candidates(
            alarm=alarm,
            preceding=preceding,
        )

        limitations = [
            (
                "The result is an engineering inference "
                "based on recorded events, not a confirmed "
                "failure diagnosis."
            ),
            (
                "PLC events may show sequence and timing, "
                "but they cannot directly confirm mechanical "
                "damage, blockage, wiring failure, or sensor "
                "accuracy."
            ),
        ]

        if not preceding:
            limitations.append(
                "No preceding events were available."
            )

        if not candidates:
            limitations.append(
                (
                    "No deterministic root-cause rule "
                    "matched the available event sequence."
                )
            )

        return RootCauseResult(
            found=True,
            equipment=alarm.equipment,
            alarm_tag=alarm.tag,
            alarm_condition=alarm.condition,
            alarm_time=alarm.event_time,
            alarm_message=alarm.message,
            probable_causes=candidates,
            evidence=[
                *preceding,
                alarm,
            ],
            limitations=limitations,
        )


def format_root_cause_result(
    result: RootCauseResult,
) -> str:
    """
    Convert a RootCauseResult into readable terminal text.
    """
    if not result.found:
        return (
            "No matching alarm was found for "
            "root-cause analysis."
        )

    lines = [
        "Root-cause analysis",
        "",
        f"Equipment: {result.equipment or 'Unknown'}",
        f"Alarm tag: {result.alarm_tag or 'Unknown'}",
        (
            "Alarm condition: "
            f"{result.alarm_condition or 'Unknown'}"
        ),
        f"Alarm time: {result.alarm_time or 'Unknown'}",
    ]

    if result.alarm_message:
        lines.append(
            f"Alarm message: {result.alarm_message}"
        )

    lines.extend(
        [
            "",
            "Probable contributing conditions:",
        ]
    )

    if result.probable_causes:
        for index, candidate in enumerate(
            result.probable_causes,
            start=1,
        ):
            confidence_percentage = round(
                candidate.confidence * 100
            )

            lines.append(
                (
                    f"{index}. {candidate.cause} "
                    f"({confidence_percentage}% confidence)"
                )
            )

            lines.append(
                f"   Reason: {candidate.reasoning}"
            )

            if candidate.supporting_tags:
                lines.append(
                    (
                        "   Supporting tags: "
                        + ", ".join(
                            candidate.supporting_tags
                        )
                    )
                )

    else:
        lines.append(
            (
                "No specific probable cause could be "
                "identified from the stored events."
            )
        )

    lines.extend(
        [
            "",
            "Event evidence:",
        ]
    )

    for evidence in result.evidence:
        lines.append(
            f"- {evidence.to_text()}"
        )

        if evidence.message:
            lines.append(
                f"  {evidence.message}"
            )

    lines.extend(
        [
            "",
            "Limitations:",
        ]
    )

    for limitation in result.limitations:
        lines.append(
            f"- {limitation}"
        )

    return "\n".join(
        lines
    )
