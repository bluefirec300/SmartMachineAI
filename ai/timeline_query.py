from typing import Any

from ai.event_timeline import (
    EventTimeline,
    format_event_context,
    format_event_timeline,
)


TIMELINE_PHRASES = (
    "what happened",
    "recent event",
    "recent events",
    "latest event",
    "latest events",
    "event history",
    "event timeline",
    "alarm history",
    "warning history",
    "recent alarm",
    "recent alarms",
    "latest alarm",
    "latest alarms",
    "last alarm",
    "last warning",
    "previous alarm",
    "previous warning",
    "before the alarm",
    "before the latest",
    "before the last",
    "after the alarm",
    "after the latest",
    "after the last",
)


def normalize_text(
    value: str,
) -> str:
    return " ".join(
        str(value).lower().split()
    )


def is_timeline_question(
    question: str,
) -> bool:
    normalized = normalize_text(
        question
    )

    if any(
        phrase in normalized
        for phrase in TIMELINE_PHRASES
    ):
        return True

    event_words = {
        "event",
        "events",
        "alarm",
        "alarms",
        "warning",
        "warnings",
        "timeline",
        "history",
    }

    time_words = {
        "recent",
        "recently",
        "latest",
        "last",
        "previous",
        "before",
        "after",
        "happened",
    }

    question_words = set(
        normalized.split()
    )

    return bool(
        question_words.intersection(
            event_words
        )
        and question_words.intersection(
            time_words
        )
    )


def requested_severity(
    question: str,
) -> str | None:
    normalized = normalize_text(
        question
    )

    if (
        "alarm" in normalized
        or "alarms" in normalized
    ):
        return "alarm"

    if (
        "warning" in normalized
        or "warnings" in normalized
    ):
        return "warning"

    return None


def requested_event_limit(
    question: str,
    default_limit: int = 10,
) -> int:
    normalized = normalize_text(
        question
    )

    words = normalized.split()

    word_numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "twenty": 20,
        "fifty": 50,
    }

    for word in words:
        if word.isdigit():
            return max(
                1,
                min(
                    int(word),
                    100,
                ),
            )

        if word in word_numbers:
            return word_numbers[word]

    if any(
        phrase in normalized
        for phrase in (
            "latest event",
            "latest alarm",
            "latest warning",
            "last event",
            "last alarm",
            "last warning",
            "previous event",
            "previous alarm",
            "previous warning",
        )
    ):
        return 1

    return max(
        1,
        min(
            int(default_limit),
            100,
        ),
    )


def requests_event_context(
    question: str,
) -> bool:
    normalized = normalize_text(
        question
    )

    return any(
        phrase in normalized
        for phrase in (
            "what happened before",
            "what happened after",
            "before the latest",
            "before the last",
            "before the alarm",
            "before the warning",
            "after the latest",
            "after the last",
            "after the alarm",
            "after the warning",
            "around the latest",
            "around the last",
            "around the alarm",
            "around the warning",
        )
    )


def requested_context_counts(
    question: str,
) -> tuple[int, int]:
    normalized = normalize_text(
        question
    )

    before_count = 5
    after_count = 5

    if (
        "before" in normalized
        and "after" not in normalized
        and "around" not in normalized
    ):
        after_count = 0

    elif (
        "after" in normalized
        and "before" not in normalized
        and "around" not in normalized
    ):
        before_count = 0

    return (
        before_count,
        after_count,
    )


def route_equipment(
    route: dict[str, Any],
) -> str | None:
    equipment = str(
        route.get(
            "equipment",
            "",
        )
    ).strip()

    if not equipment:
        return None

    if equipment.lower() in {
        "factory",
        "all",
        "general",
        "unknown",
    }:
        return None

    return equipment


def route_tag(
    route: dict[str, Any],
) -> str | None:
    tags = route.get(
        "tags",
        [],
    )

    if not isinstance(
        tags,
        list,
    ):
        return None

    valid_tags = [
        str(tag).strip()
        for tag in tags
        if str(tag).strip()
    ]

    if len(valid_tags) != 1:
        return None

    return valid_tags[0]


class TimelineQuestionHandler:
    """
    Answers historical machine-event questions from machine_events.

    This handler does not use the LLM to invent event history.
    It retrieves stored events directly from EventTimeline.
    """

    def __init__(
        self,
        timeline: EventTimeline | None = None,
    ):
        self.timeline = (
            timeline
            if timeline is not None
            else EventTimeline()
        )

    def _get_latest_matching_event(
        self,
        equipment: str | None,
        tag: str | None,
        severity: str | None,
    ) -> dict[str, Any] | None:
        return self.timeline.get_latest_event(
            equipment=equipment,
            tag=tag,
            severity=severity,
        )

    def answer(
        self,
        question: str,
        route: dict[str, Any],
    ) -> str:
        equipment = route_equipment(
            route
        )

        tag = route_tag(
            route
        )

        severity = requested_severity(
            question
        )

        if requests_event_context(
            question
        ):
            latest_event = (
                self._get_latest_matching_event(
                    equipment=equipment,
                    tag=tag,
                    severity=severity,
                )
            )

            if latest_event is None:
                return (
                    "No matching machine event was found."
                )

            before_count, after_count = (
                requested_context_counts(
                    question
                )
            )

            context = (
                self.timeline.get_context_around_event(
                    event_id=int(
                        latest_event["id"]
                    ),
                    before_count=before_count,
                    after_count=after_count,
                )
            )

            return format_event_context(
                context
            )

        limit = requested_event_limit(
            question
        )

        events = self.timeline.get_recent_events(
            limit=limit,
            equipment=equipment,
            tag=tag,
            severity=severity,
        )

        return format_event_timeline(
            events
        )

