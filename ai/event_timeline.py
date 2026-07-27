import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "database"
    / "machine_data.db"
)


class EventTimeline:
    """
    Reads machine events and builds a chronological timeline.

    The timeline uses the existing machine_events table created
    by EventStore.
    """

    def __init__(
        self,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
    ):
        self.database_path = Path(database_path).resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.exists():
            raise FileNotFoundError(
                "Machine database not found: "
                f"{self.database_path}"
            )

        connection = sqlite3.connect(
            self.database_path
        )

        connection.row_factory = sqlite3.Row

        return connection

    @staticmethod
    def _parse_datetime(
        value: str,
    ) -> datetime | None:
        if not value:
            return None

        try:
            return datetime.fromisoformat(
                str(value).replace(
                    "Z",
                    "+00:00",
                )
            )

        except ValueError:
            return None

    def get_recent_events(
        self,
        limit: int = 20,
        equipment: str | None = None,
        tag: str | None = None,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Returns recent machine events, newest first.
        """
        safe_limit = max(
            1,
            min(
                int(limit),
                500,
            ),
        )

        conditions = []
        parameters: list[Any] = []

        if equipment:
            conditions.append(
                "LOWER(equipment) = LOWER(?)"
            )
            parameters.append(
                equipment.strip()
            )

        if tag:
            conditions.append(
                "LOWER(tag) = LOWER(?)"
            )
            parameters.append(
                tag.strip()
            )

        if severity:
            conditions.append(
                "LOWER(severity) = LOWER(?)"
            )
            parameters.append(
                severity.strip()
            )

        where_clause = ""

        if conditions:
            where_clause = (
                "WHERE "
                + " AND ".join(conditions)
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
            {where_clause}
            ORDER BY
                event_time DESC,
                id DESC
            LIMIT ?
        """

        parameters.append(
            safe_limit
        )

        with self._connect() as connection:
            rows = connection.execute(
                query,
                parameters,
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def get_events_between(
        self,
        start_time: str,
        end_time: str,
        equipment: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """
        Returns events between two ISO-formatted timestamps.

        Results are oldest first so they form a timeline.
        """
        safe_limit = max(
            1,
            min(
                int(limit),
                5000,
            ),
        )

        conditions = [
            "event_time >= ?",
            "event_time <= ?",
        ]

        parameters: list[Any] = [
            start_time,
            end_time,
        ]

        if equipment:
            conditions.append(
                "LOWER(equipment) = LOWER(?)"
            )
            parameters.append(
                equipment.strip()
            )

        parameters.append(
            safe_limit
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
                event_time ASC,
                id ASC
            LIMIT ?
        """

        with self._connect() as connection:
            rows = connection.execute(
                query,
                parameters,
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def get_context_around_event(
        self,
        event_id: int,
        before_count: int = 5,
        after_count: int = 5,
    ) -> dict[str, Any]:
        """
        Returns one target event together with surrounding events.
        """
        before_count = max(
            0,
            min(
                int(before_count),
                100,
            ),
        )

        after_count = max(
            0,
            min(
                int(after_count),
                100,
            ),
        )

        with self._connect() as connection:
            target_row = connection.execute(
                """
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
                WHERE id = ?
                """,
                (
                    int(event_id),
                ),
            ).fetchone()

            if target_row is None:
                return {
                    "target": None,
                    "before": [],
                    "after": [],
                }

            target = dict(
                target_row
            )

            before_rows = connection.execute(
                """
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
                WHERE
                    event_time < ?
                    OR (
                        event_time = ?
                        AND id < ?
                    )
                ORDER BY
                    event_time DESC,
                    id DESC
                LIMIT ?
                """,
                (
                    target["event_time"],
                    target["event_time"],
                    target["id"],
                    before_count,
                ),
            ).fetchall()

            after_rows = connection.execute(
                """
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
                WHERE
                    event_time > ?
                    OR (
                        event_time = ?
                        AND id > ?
                    )
                ORDER BY
                    event_time ASC,
                    id ASC
                LIMIT ?
                """,
                (
                    target["event_time"],
                    target["event_time"],
                    target["id"],
                    after_count,
                ),
            ).fetchall()

        before = [
            dict(row)
            for row in reversed(
                before_rows
            )
        ]

        after = [
            dict(row)
            for row in after_rows
        ]

        return {
            "target": target,
            "before": before,
            "after": after,
        }

    def get_latest_event(
        self,
        equipment: str | None = None,
        tag: str | None = None,
        severity: str | None = None,
    ) -> dict[str, Any] | None:
        events = self.get_recent_events(
            limit=1,
            equipment=equipment,
            tag=tag,
            severity=severity,
        )

        if not events:
            return None

        return events[0]


def format_event_time(
    event_time: str,
) -> str:
    try:
        parsed = datetime.fromisoformat(
            str(event_time).replace(
                "Z",
                "+00:00",
            )
        )

        return parsed.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    except ValueError:
        return str(
            event_time
        )


def format_timeline_event(
    event: dict[str, Any],
) -> str:
    event_time = format_event_time(
        str(
            event.get(
                "event_time",
                "",
            )
        )
    )

    equipment = str(
        event.get(
            "equipment",
            "",
        )
    ).strip()

    tag = str(
        event.get(
            "tag",
            "",
        )
    ).strip()

    severity = str(
        event.get(
            "severity",
            "unknown",
        )
    ).upper()

    condition = str(
        event.get(
            "condition",
            "",
        )
    ).replace(
        "_",
        " ",
    ).strip()

    value = event.get(
        "value"
    )

    unit = str(
        event.get(
            "unit",
            "",
        )
    ).strip()

    message = str(
        event.get(
            "message",
            "",
        )
    ).strip()

    title_parts = []

    if equipment:
        title_parts.append(
            equipment
        )

    if tag:
        title_parts.append(
            tag
        )

    title = " - ".join(
        title_parts
    )

    if not title:
        title = "Machine event"

    value_text = ""

    if value is not None:
        try:
            value_text = (
                f"{float(value):g}"
            )

        except (TypeError, ValueError):
            value_text = str(
                value
            )

        if unit:
            value_text = (
                f"{value_text} {unit}"
            )

    details = []

    if condition:
        details.append(
            condition
        )

    if value_text:
        details.append(
            value_text
        )

    detail_text = ""

    if details:
        detail_text = (
            " | "
            + " | ".join(details)
        )

    line = (
        f"{event_time} "
        f"[{severity}] "
        f"{title}"
        f"{detail_text}"
    )

    if message:
        line += (
            f"\n  {message}"
        )

    return line


def format_event_timeline(
    events: list[dict[str, Any]],
    newest_first: bool = False,
) -> str:
    if not events:
        return (
            "No machine events were found "
            "for the requested period."
        )

    selected_events = list(
        events
    )

    if not newest_first:
        selected_events.sort(
            key=lambda event: (
                str(
                    event.get(
                        "event_time",
                        "",
                    )
                ),
                int(
                    event.get(
                        "id",
                        0,
                    )
                ),
            )
        )

    return "\n\n".join(
        format_timeline_event(
            event
        )
        for event in selected_events
    )


def format_event_context(
    context: dict[str, Any],
) -> str:
    target = context.get(
        "target"
    )

    if target is None:
        return (
            "The requested machine event "
            "was not found."
        )

    sections = []

    before = context.get(
        "before",
        [],
    )

    if before:
        sections.append(
            "Events before:\n"
            + format_event_timeline(
                before
            )
        )

    sections.append(
        "Target event:\n"
        + format_timeline_event(
            target
        )
    )

    after = context.get(
        "after",
        [],
    )

    if after:
        sections.append(
            "Events after:\n"
            + format_event_timeline(
                after
            )
        )

    return "\n\n".join(
        sections
    )
