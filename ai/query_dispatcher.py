import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class QueryType(str, Enum):
    CURRENT_DATA = "current_data"
    THRESHOLD = "threshold"
    TIMELINE = "timeline"
    ROOT_CAUSE = "root_cause"
    GENERAL = "general"


@dataclass(frozen=True)
class DispatchResult:
    query_type: QueryType
    reason: str
    confidence: float


THRESHOLD_TERMS = {
    "threshold",
    "thresholds",
    "setpoint",
    "setpoints",
    "limit",
    "limits",
    "configured",
    "configuration",
    "setting",
    "settings",
}


CURRENT_PHRASES = {
    "current",
    "currently",
    "right now",
    "at the moment",
    "latest value",
    "live value",
    "actual value",
    "present value",
}


TIMELINE_PHRASES = {
    "what happened",
    "recent event",
    "recent events",
    "latest event",
    "latest events",
    "last event",
    "last events",
    "previous event",
    "previous events",
    "last alarm",
    "latest alarm",
    "recent alarm",
    "recent alarms",
    "previous alarm",
    "previous alarms",
    "last warning",
    "latest warning",
    "recent warning",
    "recent warnings",
    "previous warning",
    "previous warnings",
    "alarm history",
    "warning history",
    "event history",
    "event timeline",
    "show events",
    "show alarms",
    "show warnings",
}


ROOT_CAUSE_PHRASES = {
    "why did",
    "why was",
    "why is",
    "why are",
    "what caused",
    "possible cause",
    "possible causes",
    "probable cause",
    "probable causes",
    "root cause",
    "root causes",
    "reason for",
    "reason why",
    "led to",
    "leading to",
    "sequence leading",
    "caused the",
    "cause of",
    "causes of",
}


CONTEXT_PHRASES = {
    "what happened before",
    "what happened after",
    "before the latest",
    "before the last",
    "before the previous",
    "after the latest",
    "after the last",
    "after the previous",
    "around the latest",
    "around the last",
    "around the previous",
}


MEASUREMENT_WORDS = {
    "pressure",
    "temperature",
    "level",
    "flow",
    "flowrate",
    "current",
    "voltage",
    "speed",
    "frequency",
    "status",
    "state",
    "running",
    "stopped",
    "value",
    "reading",
    "readings",
    "humidity",
    "power",
    "energy",
    "torque",
    "position",
    "weight",
    "count",
    "counter",
}


CURRENT_QUESTION_PREFIXES = {
    "what is",
    "what are",
    "show",
    "read",
    "tell me",
    "give me",
    "display",
    "check",
}


class QueryDispatcher:
    """
    Classifies user questions before database retrieval or AI generation.

    The dispatcher does not answer questions. It selects the appropriate
    processing path for current data, thresholds, event history,
    root-cause analysis, or general AI.
    """

    @staticmethod
    def normalize_text(value: str) -> str:
        text = str(value).lower().strip()

        text = re.sub(
            r"[^a-z0-9_\s]",
            " ",
            text,
        )

        text = re.sub(
            r"\s+",
            " ",
            text,
        )

        return text.strip()

    @staticmethod
    def tokenize(value: str) -> set[str]:
        normalized = QueryDispatcher.normalize_text(
            value
        )

        if not normalized:
            return set()

        return set(
            re.findall(
                r"[a-z0-9_]+",
                normalized,
            )
        )

    @staticmethod
    def _contains_any_phrase(
        text: str,
        phrases: set[str],
    ) -> bool:
        return any(
            phrase in text
            for phrase in phrases
        )

    @staticmethod
    def _starts_with_any(
        text: str,
        prefixes: set[str],
    ) -> bool:
        return any(
            text == prefix
            or text.startswith(
                prefix + " "
            )
            for prefix in prefixes
        )

    def _is_threshold_question(
        self,
        normalized: str,
    ) -> bool:
        words = self.tokenize(
            normalized
        )

        if words.intersection(
            THRESHOLD_TERMS
        ):
            return True

        has_alarm_or_warning = bool(
            words.intersection(
                {
                    "alarm",
                    "alarms",
                    "warning",
                    "warnings",
                }
            )
        )

        configuration_language = self._contains_any_phrase(
            normalized,
            {
                "set at",
                "configured at",
                "configured value",
                "configured values",
                "alarm value",
                "warning value",
                "alarm setting",
                "warning setting",
                "high alarm",
                "low alarm",
                "high warning",
                "low warning",
                "what is the high",
                "what is the low",
                "what are the high",
                "what are the low",
            },
        )

        return (
            has_alarm_or_warning
            and configuration_language
        )

    def _is_root_cause_question(
        self,
        normalized: str,
    ) -> bool:
        return self._contains_any_phrase(
            normalized,
            ROOT_CAUSE_PHRASES,
        )

    def _is_timeline_question(
        self,
        normalized: str,
    ) -> bool:
        if self._contains_any_phrase(
            normalized,
            TIMELINE_PHRASES,
        ):
            return True

        if self._contains_any_phrase(
            normalized,
            CONTEXT_PHRASES,
        ):
            return True

        words = self.tokenize(
            normalized
        )

        event_words = {
            "event",
            "events",
            "alarm",
            "alarms",
            "warning",
            "warnings",
            "history",
            "timeline",
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

        return bool(
            words.intersection(
                event_words
            )
            and words.intersection(
                time_words
            )
        )

    def _route_identifies_current_data(
        self,
        route: dict[str, Any] | None,
    ) -> bool:
        if not route:
            return False

        tags = route.get(
            "tags",
            [],
        )

        intent = str(
            route.get(
                "intent",
                "",
            )
        ).lower().strip()

        if tags and intent == "current":
            return True

        return False

    def _is_current_data_question(
        self,
        normalized: str,
        route: dict[str, Any] | None,
    ) -> bool:
        if self._route_identifies_current_data(
            route
        ):
            return True

        if self._contains_any_phrase(
            normalized,
            CURRENT_PHRASES,
        ):
            return True

        words = self.tokenize(
            normalized
        )

        has_measurement_word = bool(
            words.intersection(
                MEASUREMENT_WORDS
            )
        )

        starts_like_current_question = (
            self._starts_with_any(
                normalized,
                CURRENT_QUESTION_PREFIXES,
            )
        )

        if (
            has_measurement_word
            and starts_like_current_question
        ):
            return True

        if has_measurement_word:
            short_measurement_question = (
                len(words) <= 4
            )

            if short_measurement_question:
                return True

        return False

    def dispatch(
        self,
        question: str,
        route: dict[str, Any] | None = None,
    ) -> DispatchResult:
        normalized = self.normalize_text(
            question
        )

        if not normalized:
            return DispatchResult(
                query_type=QueryType.GENERAL,
                reason="The question is empty.",
                confidence=0.0,
            )

        if self._is_root_cause_question(
            normalized
        ):
            return DispatchResult(
                query_type=QueryType.ROOT_CAUSE,
                reason=(
                    "The question asks for a cause, reason, "
                    "or sequence leading to an event."
                ),
                confidence=0.95,
            )

        if self._is_threshold_question(
            normalized
        ):
            return DispatchResult(
                query_type=QueryType.THRESHOLD,
                reason=(
                    "The question asks about configured warning, "
                    "alarm, threshold, or setpoint values."
                ),
                confidence=0.95,
            )

        if self._is_timeline_question(
            normalized
        ):
            return DispatchResult(
                query_type=QueryType.TIMELINE,
                reason=(
                    "The question asks about stored events "
                    "or historical event context."
                ),
                confidence=0.90,
            )

        if self._is_current_data_question(
            normalized,
            route,
        ):
            return DispatchResult(
                query_type=QueryType.CURRENT_DATA,
                reason=(
                    "The question asks for a current measurement "
                    "or equipment state."
                ),
                confidence=0.85,
            )

        return DispatchResult(
            query_type=QueryType.GENERAL,
            reason=(
                "No deterministic engineering query type "
                "was identified."
            ),
            confidence=0.50,
        )
