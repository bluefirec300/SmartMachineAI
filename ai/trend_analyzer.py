from statistics import mean
from typing import Any

from plc.tag_registry import TagRegistry


DIGITAL_DATA_TYPES = {
    "BOOL",
    "BOOLEAN",
    "BIT",
}


_registry: TagRegistry | None = None
_metadata_cache: dict[str, dict[str, Any]] | None = None


def _get_registry() -> TagRegistry:
    """
    Lazily initialize the shared tag registry.

    Lazy initialization keeps this module easy to import in tests and
    avoids loading configuration until trend analysis is actually used.
    """
    global _registry

    if _registry is None:
        _registry = TagRegistry()

    return _registry


def _get_metadata() -> dict[str, dict[str, Any]]:
    """
    Return tag metadata indexed by logical tag name.
    """
    global _metadata_cache

    if _metadata_cache is None:
        registry = _get_registry()

        _metadata_cache = {
            tag["name"]: tag
            for tag in registry.get_all()
        }

    return _metadata_cache


def reload_metadata() -> None:
    """
    Clear cached metadata.

    Call this after changing tag configuration while the process is
    already running.
    """
    global _registry
    global _metadata_cache

    _registry = None
    _metadata_cache = None


def get_tag_metadata(
    tag_name: str,
) -> dict[str, Any]:
    """
    Get configuration metadata for one logical tag.

    Unknown historian tags are allowed and return an empty dictionary,
    ensuring old or external data can still be analysed.
    """
    return _get_metadata().get(
        tag_name,
        {},
    )


def get_tag_unit(
    tag_name: str,
) -> str:
    metadata = get_tag_metadata(tag_name)

    unit = metadata.get("unit")

    if unit is None:
        return ""

    return str(unit).strip()


def is_digital_tag(
    tag_name: str,
) -> bool:
    metadata = get_tag_metadata(tag_name)

    data_type = str(
        metadata.get("data_type", "")
    ).strip().upper()

    return data_type in DIGITAL_DATA_TYPES


def format_number(value):
    if value is None:
        return "N/A"

    return f"{value:.2f}".rstrip("0").rstrip(".")


def digital_state(value):
    try:
        return "ON" if float(value) != 0 else "OFF"

    except (TypeError, ValueError):
        return str(value)


def calculate_analogue_trend(values):
    if len(values) < 2:
        return "No trend history"

    first_value = values[0]
    latest_value = values[-1]
    change = latest_value - first_value

    average_magnitude = max(
        abs(mean(values)),
        1.0,
    )

    stable_threshold = average_magnitude * 0.01
    rapid_threshold = average_magnitude * 0.10

    if abs(change) <= stable_threshold:
        return "Stable"

    if change >= rapid_threshold:
        return "Rising rapidly"

    if change > stable_threshold:
        return "Rising"

    if change <= -rapid_threshold:
        return "Falling rapidly"

    return "Falling"


def calculate_digital_trend(values):
    if len(values) < 2:
        return "No trend history"

    transitions = 0

    for previous, current in zip(
        values,
        values[1:],
    ):
        previous_state = float(previous) != 0
        current_state = float(current) != 0

        if previous_state != current_state:
            transitions += 1

    if transitions == 0:
        return "No state change"

    return f"{transitions} state transition(s)"


def analyse_tag(tag, rows):
    if not rows:
        raise ValueError(
            f"Cannot analyse empty history for tag: {tag}"
        )

    latest_row = rows[-1]
    unit = get_tag_unit(tag)

    numeric_values = []

    for row in rows:
        try:
            numeric_values.append(
                float(row["value"])
            )

        except (TypeError, ValueError):
            pass

    if is_digital_tag(tag):
        return {
            "tag": tag,
            "address": latest_row["address"],
            "updated": latest_row["time"],
            "current": digital_state(
                latest_row["value"]
            ),
            "unit": "",
            "samples": len(rows),
            "trend": calculate_digital_trend(
                numeric_values
            ),
            "average": None,
            "minimum": None,
            "maximum": None,
            "change": None,
        }

    if not numeric_values:
        return {
            "tag": tag,
            "address": latest_row["address"],
            "updated": latest_row["time"],
            "current": latest_row["value"],
            "unit": unit,
            "samples": len(rows),
            "trend": "Unavailable",
            "average": None,
            "minimum": None,
            "maximum": None,
            "change": None,
        }

    return {
        "tag": tag,
        "address": latest_row["address"],
        "updated": latest_row["time"],
        "current": numeric_values[-1],
        "unit": unit,
        "samples": len(numeric_values),
        "trend": calculate_analogue_trend(
            numeric_values
        ),
        "average": mean(numeric_values),
        "minimum": min(numeric_values),
        "maximum": max(numeric_values),
        "change": (
            numeric_values[-1]
            - numeric_values[0]
        ),
    }


def analyse_history(history):
    summaries = []

    for tag in sorted(history):
        rows = history[tag]

        if not rows:
            continue

        summaries.append(
            analyse_tag(
                tag,
                rows,
            )
        )

    return summaries
