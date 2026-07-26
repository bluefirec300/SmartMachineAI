import re

from ai.equipment_knowledge import EquipmentKnowledge

from typing import Any

from plc.tag_registry import TagRegistry

equipment_knowledge = EquipmentKnowledge()

FACTORY_WIDE_WORDS = {
    "factory",
    "everything",
    "all equipment",
    "all machines",
    "anything abnormal",
    "overall",
    "whole plant",
    "plant status",
}

TREND_WORDS = {
    # Existing
    "trend",
    "rising",
    "falling",
    "increasing",
    "decreasing",
    "history",
    "historical",
    "performance",
    "performing",
    "getting worse",
    "getting better",
    "over time",

    # Additional natural language
    "increase",
    "decrease",
    "increased",
    "decreased",
    "rise",
    "fall",
    "dropped",
    "dropping",
    "drop",
    "climbing",
    "climb",
    "declining",
    "decline",
    "improving",
    "improve",
    "changed",
    "change",
    "changing",
    "stable",
    "stabilised",
    "stabilized",
    "fluctuating",
    "fluctuation",
}



STATUS_WORDS = {
    "abnormal",
    "alarm",
    "alarms",
    "warning",
    "warnings",
    "problem",
    "problems",
    "fault",
    "faults",
    "healthy",
    "health",
    "condition",
    "status",
    "maintenance",
}


MEASUREMENT_ALIASES = {
    "pressure": {
        "pressure",
        "bar",
    },
    "temperature": {
        "temperature",
        "temp",
        "degree",
        "degrees",
    },
    "current": {
        "current",
        "amp",
        "amps",
        "ampere",
    },
    "running": {
        "running",
        "run",
        "operating",
        "started",
        "on",
        "stopped",
        "off",
    },
    "warning": {
        "warning",
        "warnings",
    },
    "alarm": {
        "alarm",
        "alarms",
        "fault",
        "faults",
    },
    "humidity": {
        "humidity",
        "rh",
    },
    "door": {
        "door",
        "opened",
        "closed",
    },
    "level": {
        "level",
        "full",
        "empty",
    },
    "energy": {
        "energy",
        "power",
        "kilowatt",
        "kw",
    },
}


def normalize_text(value: str) -> str:
    """
    Convert CamelCase and punctuation into searchable lowercase words.

    Example:
        CompressorPressure -> compressor pressure
        ColdRoomTemperature -> cold room temperature
    """
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = value.replace("_", " ")
    value = re.sub(r"[^a-zA-Z0-9]+", " ", value)

    return " ".join(value.lower().split())


def contains_phrase(text: str, phrase: str) -> bool:
    """
    Match complete words or phrases instead of arbitrary substrings.

    This prevents words such as 'air' matching inside unrelated words.
    """
    normalized_phrase = normalize_text(phrase)
    pattern = rf"\b{re.escape(normalized_phrase)}\b"

    return re.search(pattern, text) is not None


def detect_intent(question: str) -> str:
    text = normalize_text(question)

    if any(contains_phrase(text, word) for word in TREND_WORDS):
        return "trend"

    if any(contains_phrase(text, word) for word in STATUS_WORDS):
        return "status"

    return "current"


def detect_equipment(question: str) -> str:
    text = normalize_text(question)

    if any(
        contains_phrase(text, word)
        for word in FACTORY_WIDE_WORDS
    ):
        return "factory"

    knowledge_match = equipment_knowledge.find_equipment(
        question
    )

    if knowledge_match:
        return knowledge_match

    return "factory"





def detect_measurements(question: str) -> set[str]:
    text = normalize_text(question)
    detected = set()

    for measurement, aliases in MEASUREMENT_ALIASES.items():
        if any(contains_phrase(text, alias) for alias in aliases):
            detected.add(measurement)

    return detected


def build_tag_search_text(tag: dict[str, Any]) -> str:
    parts = [
        str(tag.get("name", "")),
        str(tag.get("description", "")),
        str(tag.get("unit", "")),
    ]

    return normalize_text(" ".join(parts))

def tag_matches_equipment(
    tag: dict[str, Any],
    equipment: str,
) -> bool:
    if equipment == "factory":
        return True

    search_text = build_tag_search_text(tag)
    aliases = equipment_knowledge.get_aliases(equipment)

    for alias in aliases:
        if contains_phrase(search_text, alias):
            return True

    return False


def tag_matches_measurements(
    tag: dict[str, Any],
    measurements: set[str],
) -> bool:
    if not measurements:
        return True

    search_text = build_tag_search_text(tag)

    for measurement in measurements:
        aliases = MEASUREMENT_ALIASES.get(measurement, set())

        if any(
            contains_phrase(search_text, alias)
            for alias in aliases
        ):
            return True

    return False



def get_enabled_tags() -> list[dict[str, Any]]:
    registry = TagRegistry()

    return [
        tag
        for tag in registry.get_all()
        if tag.get("enabled", True)
    ]


def select_tags(
    question: str,
    equipment: str,
    intent: str,
    measurements: set[str] | None = None,
) -> list[str] | None:

    registered_tags = get_enabled_tags()
    registered_names = {
        tag["name"]
        for tag in registered_tags
    }

    if measurements is None:
        measurements = detect_measurements(question)
        
    # For a named equipment system, use the engineering
    # relationships from equipment_knowledge.json.
    if equipment != "factory":
        knowledge_tags = equipment_knowledge.get_tags(
            equipment_name=equipment,
            include_related=True,
        )

        knowledge_tags = [
            tag_name
            for tag_name in knowledge_tags
            if tag_name in registered_names
        ]

        # A specific measurement question should return only
        # matching tags from the equipment knowledge group.
        if measurements:
            tags_by_name = {
                tag["name"]: tag
                for tag in registered_tags
            }

            matched_tags = [
                tag_name
                for tag_name in knowledge_tags
                if tag_matches_measurements(
                    tags_by_name[tag_name],
                    measurements,
                )
            ]

            if matched_tags:
                return matched_tags

        # Broad questions such as "How is the compressor?"
        # receive the full engineering context.
        return knowledge_tags

    # Factory-wide status questions focus on state and alarm tags.
    if intent == "status":
        status_measurements = {
            "alarm",
            "warning",
            "running",
            "door",
        }

        status_tags = [
            tag
            for tag in registered_tags
            if tag_matches_measurements(
                tag,
                status_measurements,
            )
        ]

        if status_tags:
            return [
                tag["name"]
                for tag in status_tags
            ]

    # None tells the database reader to retrieve all tags.
    return None








def route_question(question: str) -> dict[str, Any]:
    equipment = detect_equipment(question)
    intent = detect_intent(question)

    tags = select_tags(
        question=question,
        equipment=equipment,
        intent=intent,
    )

    if intent == "current":
        history_limit = 1
    elif intent == "trend":
        history_limit = 20
    else:
        history_limit = 10

    return {
        "equipment": equipment,
        "intent": intent,
        "tags": tags,
        "history_limit": history_limit,
    }
