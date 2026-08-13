from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from ai.ai_provider import AIProvider
from ai.event_store import EventStore
from ai.prompt_builder import build_prompt, format_machine_context
from ai.root_cause_engine import RootCauseEngine, format_root_cause_result
from ai.rule_engine import RuleEngine, format_rule_results
from ai.trend_analyzer import analyse_tag, format_number
from config.environment import get_config_db_path, get_machine_db_path
from database.database import DatabaseManager
from engine.industrial_query_engine import IndustrialQueryEngine
from rag.retrieval import search_chunks


PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DATABASE_PATH = get_config_db_path()
MACHINE_DATABASE_PATH = get_machine_db_path()

# "current_data" questions only need the latest sample. Every other
# intent (trend, threshold, timeline, root_cause) needs enough history
# to describe a trend, so they get a wider window.
HISTORY_HOURS_BY_INTENT = {
    "current_data": 1,
}

DEFAULT_HISTORY_HOURS = 24
DEFAULT_HISTORY_LIMIT = 2000
RECENT_EVENTS_LIMIT = 10

THRESHOLD_LABELS = (
    "low_alarm",
    "low_warning",
    "high_warning",
    "high_alarm",
)

FACTORY_WIDE_EVENTS_LIMIT = 30

# Canned, instant, no-LLM replies for small talk that isn't really a
# factory question - see GREETING_PHRASES/THANKS_PHRASES in
# concept_extractor.py for exactly which whole-message inputs trigger
# these (a real question that happens to start with "hi" never does).
# When True, the "instant" intents below (discovery, equipment_status,
# chitchat, factory-wide timeline fallback) skip the LLM entirely and
# answer straight from deterministic code - the production/SCADA-
# facing default (instant, no multi-minute wait for a structured
# listing that doesn't need paraphrasing). Briefly flipped to False to
# test genuinely routing these through Ollama too (confirmed working:
# ~90s for a chitchat greeting, correctly phrased, no hallucinated
# facts) - reverted back to True on reflection, since forcing a
# multi-minute wait onto answers that were already fully correct
# didn't actually buy anything. current_data/root_cause/trend/
# threshold/timeline-for-one-tag were never affected either way -
# those already always go through real Ollama.
INSTANT_ANSWERS_SKIP_LLM = True

CHITCHAT_RESPONSES = {
    "chitchat_greeting": (
        "Hi! Ask me about any equipment, e.g. \"what is the compressor "
        "pressure\" or \"is everything ok\" - say \"help\" any time to "
        "see what's available."
    ),
    "chitchat_thanks": "You're welcome!",
}

# Main Incomer P01 measures total incoming power for the whole plant
# ("Factory active power" is its actual tag description) - the correct
# single answer for a "what's the factory/total/plant power" question
# that names no specific equipment. See the equipment_score floor
# check in ask() for how "no equipment named" is distinguished from
# "named equipment, ambiguous which instance" (e.g. "compressor
# power" - genuinely ambiguous among AC01/02/03, should keep the
# normal clarification menu, not get redirected here).
FACTORY_TOTAL_POWER_TAG = "P01.ELEC.MAIN.Power_kW"
FACTORY_TOTAL_POWER_EQUIPMENT = "Main Incomer MAIN (P01)"
FACTORY_TOTAL_POWER_EQUIPMENT_SCORE_FLOOR = 0.4

# Same "how confident is the equipment match" floor as the power
# fallback above, reused for "equipment_status" questions ("is
# everything ok", "how is AC01 doing") - below this score, no specific
# equipment was really named, so the answer covers the whole factory
# instead of guessing one instance.
EQUIPMENT_STATUS_SCORE_FLOOR = 0.4

# How many recent questions AskEngine._history keeps for
# _try_correct_question()'s follow-up-rewriting context. Raise this
# (or drop the cap entirely for a truly unbounded history) as the only
# change needed to extend how far back a follow-up can reference -
# see the comment on self._history in __init__.
HISTORY_TURNS_KEPT = 5

TIME_RANGE_LABELS = {
    "yesterday": "yesterday",
    "today": "today",
    "last_7_days": "in the last 7 days",
    "last_hour": "in the last hour",
    "last_30_minutes": "in the last 30 minutes",
}

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _factory_timeline_range(time_expression):
    """
    Convert a time_expression (from ConceptExtractor.TIMES, or the
    dynamic "days_ago:N"/"date:YYYY-MM-DD" forms) into an actual
    (start, end, label) window - originally built for querying
    machine_events, also reused by _answer_comparison() below for
    querying plc_data over a specific calendar day.

    Uses the same space-separated timestamp format as database.py -
    machine_events.event_time/plc_data.time are both written in that
    format, and a mismatched separator would silently break the
    comparison the same way it did for plc_data earlier this session.
    """
    now = datetime.now()

    # "days_ago:N" - a single specific calendar day, N days before
    # today (see engine.concept_extractor.extract_days_ago()). Not a
    # fixed TIME_RANGE_LABELS entry since N is unbounded - the label
    # is built inline instead of falling through to the lookup below.
    if time_expression.startswith("days_ago:"):
        n = int(time_expression.split(":", 1)[1])
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start_of_today - timedelta(days=n)
        end = start_of_today - timedelta(days=n - 1) - timedelta(seconds=1)
        label = "yesterday" if n == 1 else f"{n} days ago"

        return start.strftime(TIMESTAMP_FORMAT), end.strftime(TIMESTAMP_FORMAT), label

    # "date:YYYY-MM-DD" - an explicit calendar day named in the
    # question (see engine.concept_extractor.extract_comparison_periods()),
    # e.g. "compare pressure on Aug 10 vs Aug 12". A named day in the
    # future is left as a genuine empty window rather than special-
    # cased - the caller (_answer_comparison()) already handles "no
    # data for that period" honestly instead of guessing.
    if time_expression.startswith("date:"):
        day = datetime.strptime(time_expression.split(":", 1)[1], "%Y-%m-%d")
        start = day
        end = day + timedelta(days=1) - timedelta(seconds=1)

        return start.strftime(TIMESTAMP_FORMAT), end.strftime(TIMESTAMP_FORMAT), day.strftime("%Y-%m-%d")

    if time_expression == "yesterday":
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start_of_today - timedelta(days=1)
        end = start_of_today - timedelta(seconds=1)
    elif time_expression == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif time_expression == "last_7_days":
        start = now - timedelta(days=7)
        end = now
    elif time_expression == "last_hour":
        start = now - timedelta(hours=1)
        end = now
    elif time_expression == "last_30_minutes":
        start = now - timedelta(minutes=30)
        end = now
    else:
        start = now - timedelta(hours=24)
        end = now

    label = TIME_RANGE_LABELS.get(time_expression, "in the last 24 hours")

    return start.strftime(TIMESTAMP_FORMAT), end.strftime(TIMESTAMP_FORMAT), label


def _format_factory_wide_events(events, label):
    """
    Render a factory-wide (not tag-specific) event summary.

    Used when a timeline question doesn't name a specific tag/
    equipment ("is there anything happen yesterday") - the normal
    tag resolver has nothing to resolve to, so this queries
    machine_events across everything instead of failing outright.
    """
    if not events:
        return f"No alarm or warning events were recorded {label}."

    lines = [
        f"Alarm/warning events {label} "
        f"({len(events)} shown, most recent first):"
    ]

    for event in events:
        value_text = ""

        if event.get("value") is not None:
            value_text = f" {format_number(event['value'])}"

            if event.get("unit"):
                value_text += f" {event['unit']}"

        lines.append(
            f"- {event['event_time']} | {event['equipment']} | "
            f"{event['tag']} | [{event['severity'].upper()}] "
            f"{event['condition']}{value_text}"
        )

    return "\n".join(lines)


def _format_comparison_context(tag_name, label_a, summary_a, label_b, summary_b):
    """
    Render the two periods' real stats plus a deterministically
    computed verdict (which is higher/lower, by how much) for
    AskEngine._answer_comparison() to hand the LLM as already-settled
    facts. The verdict is computed here in plain Python arithmetic,
    never left for the LLM to work out - see _answer_comparison()'s
    docstring for the exact bug ("28.2 is higher than 29.38") this is
    guarding against.
    """
    unit = summary_a.get("unit") or summary_b.get("unit") or ""
    unit_suffix = f" {unit}" if unit else ""

    def _period_line(label, summary):
        return (
            f"{label}: average {format_number(summary['average'])}{unit_suffix} "
            f"(minimum {format_number(summary['minimum'])}{unit_suffix}, "
            f"maximum {format_number(summary['maximum'])}{unit_suffix}, "
            f"{summary['samples']} sample(s))"
        )

    lines = [
        f"Comparison for {tag_name}:",
        "",
        _period_line(label_a, summary_a),
        _period_line(label_b, summary_b),
        "",
    ]

    avg_a = summary_a.get("average")
    avg_b = summary_b.get("average")

    if avg_a is None or avg_b is None:
        lines.append(
            "CONFIRMED FACT: at least one period has no numeric "
            "average available (non-numeric or digital tag) - do not "
            "state which period is higher or lower."
        )
    else:
        difference = avg_a - avg_b
        percent = (
            f" ({format_number(abs(difference) / abs(avg_b) * 100)}%)"
            if avg_b
            else ""
        )

        if abs(difference) < 1e-9:
            lines.append(
                f"CONFIRMED FACT: {label_a}'s average and {label_b}'s "
                "average are essentially equal."
            )
        elif difference > 0:
            lines.append(
                f"CONFIRMED FACT: {label_a}'s average "
                f"({format_number(avg_a)}{unit_suffix}) is HIGHER than "
                f"{label_b}'s average ({format_number(avg_b)}{unit_suffix}) "
                f"by {format_number(abs(difference))}{unit_suffix}{percent}. "
                "Do not state the opposite."
            )
        else:
            lines.append(
                f"CONFIRMED FACT: {label_a}'s average "
                f"({format_number(avg_a)}{unit_suffix}) is LOWER than "
                f"{label_b}'s average ({format_number(avg_b)}{unit_suffix}) "
                f"by {format_number(abs(difference))}{unit_suffix}{percent}. "
                "Do not state the opposite."
            )

    return "\n".join(lines)


def _format_status_line(status):
    unit_suffix = f" {status['unit']}" if status.get("unit") else ""
    value_text = (
        f" ({format_number(status['value'])}{unit_suffix})"
        if status.get("value") is not None
        else ""
    )
    return f"- {status['tag']}{value_text}: {status['message']}"


def _format_equipment_status(equipment_display_name, statuses):
    """
    Render a deterministic, no-LLM status summary for one resolved
    equipment instance - used for "equipment_status" questions that
    matched a specific instance confidently enough (see
    EQUIPMENT_STATUS_SCORE_FLOOR in ask()).

    Deliberately names the equipment it picked in the first line, so
    if the guess was wrong (e.g. ambiguous between CHL01/CHL02), the
    operator can just say what they actually meant - the existing
    free-text-correction handling in ask() already treats anything
    other than a bare menu number as a fresh question.
    """
    evaluated = [s for s in statuses if s["value"] is not None]
    no_data = [s for s in statuses if s["value"] is None]

    alarms = [s for s in evaluated if s["severity"] == "alarm"]
    warnings = [s for s in evaluated if s["severity"] == "warning"]
    not_evaluated = [s for s in evaluated if s["condition"] == "not_evaluated"]
    normal = [
        s
        for s in evaluated
        if s["severity"] == "normal" and s["condition"] != "not_evaluated"
    ]

    lines = [f"Here's the status for {equipment_display_name}:", ""]

    if not alarms and not warnings:
        if normal:
            lines.append(
                f"Everything looks normal - all {len(normal)} monitored "
                "reading(s) with a configured threshold are within limits."
            )
        else:
            lines.append(
                "No engineering thresholds are configured yet for this "
                "equipment's tags, so nothing can be flagged as abnormal."
            )
    else:
        if alarms:
            lines.append(f"IN ALARM ({len(alarms)}):")
            lines.extend(_format_status_line(s) for s in alarms)
        if warnings:
            lines.append(f"IN WARNING ({len(warnings)}):")
            lines.extend(_format_status_line(s) for s in warnings)
        if normal:
            lines.append(f"{len(normal)} other reading(s) normal.")

    if not_evaluated:
        lines.append(
            f"({len(not_evaluated)} reading(s) have no configured "
            "threshold, so aren't included above.)"
        )

    if no_data:
        lines.append(
            f"({len(no_data)} tag(s) have no historian data recorded yet.)"
        )

    return "\n".join(lines)


def _format_factory_status(statuses):
    """
    Render a deterministic, no-LLM factory-wide status summary - used
    when an "equipment_status" question doesn't name a specific
    instance confidently enough (see EQUIPMENT_STATUS_SCORE_FLOOR in
    ask()), mirroring the factory-wide timeline fallback above.
    """
    evaluated = [s for s in statuses if s["value"] is not None]

    alarms = [s for s in evaluated if s["severity"] == "alarm"]
    warnings = [s for s in evaluated if s["severity"] == "warning"]

    if not alarms and not warnings:
        return (
            f"Everything looks normal across the factory - all "
            f"{len(evaluated)} monitored reading(s) with a configured "
            "threshold are within limits."
        )

    lines = [
        f"Factory-wide status - {len(alarms)} in alarm, "
        f"{len(warnings)} in warning (out of {len(evaluated)} monitored "
        "reading(s) with a configured threshold):",
        "",
    ]

    by_equipment: dict[str, list] = {}

    for status in alarms + warnings:
        by_equipment.setdefault(status["equipment"], []).append(status)

    for equipment_display_name, equipment_statuses in sorted(by_equipment.items()):
        lines.append(f"{equipment_display_name}:")
        lines.extend(
            _format_status_line(s)
            for s in sorted(
                equipment_statuses, key=lambda s: s["severity"] != "alarm"
            )
        )

    return "\n".join(lines)


def _format_candidate_menu(candidates, status):
    """
    Render a numbered "which one did you mean" menu.

    status distinguishes "found several good matches, pick one"
    (clarification_required) from "nothing matched confidently, here
    are the closest guesses" (no_match) - the wording is honest about
    which situation the operator is in.
    """
    if status == "clarification_required":
        header = "I found more than one possible match. Which one did you mean?"
    else:
        header = (
            "I couldn't confidently match that to a configured tag. "
            "Closest matches I found:"
        )

    lines = [header, ""]

    for index, candidate in enumerate(candidates, start=1):
        tag = candidate.tag
        equipment_label = tag.equipment_display_name or tag.equipment_name or "Unknown equipment"
        lines.append(f"{index}. {tag.tag_name} ({equipment_label})")

    lines.extend(["", "Reply with a number, or ask a new question."])

    return "\n".join(lines)


def _equipment_type_key(equipment_name):
    """
    Group numbered equipment instances by type.

    e.g. "p01_air_compressor_ac01" -> "p01_air_compressor", so "Air
    Compressor 1/2/3" collapse into one summary line instead of
    three, the same way an operator would think about it.
    """
    match = re.match(r"^(p\d+_.+)_[a-z]+\d+$", equipment_name)
    return match.group(1) if match else equipment_name


def _equipment_category_key(equipment_name):
    """
    Same idea as _equipment_type_key(), but also strips the plant
    prefix - "p01_cold_room_cr01"/"p02_cold_room_cr02" -> both
    "cold_room". Used only by the equipment_status confidence check
    below, where "every candidate is the same equipment category,
    across plants" is the right granularity (a mixed CR01(P01)/
    CR02(P01)/CR01(P02) candidate set should still count as
    confidently about Cold Room, even though it's genuinely ambiguous
    *which* instance) - unlike _equipment_type_key(), which
    deliberately keeps plants separate for the "list what's
    available" grouping in discovery answers.
    """
    match = re.match(r"^p\d+_(.+)_[a-z]+\d+$", equipment_name)
    return match.group(1) if match else _equipment_type_key(equipment_name)


def _format_available_equipment(rows, plant=None):
    """
    Render a deterministic "what can I ask about" summary.

    Not routed through the LLM - this is a structured listing
    straight from configuration, not something that benefits from
    (or should risk) paraphrasing. plant, when given, is the already-
    canonicalized "p01"/"p02"/... form the caller already filtered
    rows by - only used here to make the header honest about the
    filter that's already been applied, not to filter again.
    """
    plant_suffix = f" in {plant.upper()}" if plant else ""

    if not rows:
        return f"No equipment is currently configured/enabled{plant_suffix}."

    groups: dict[str, list] = {}

    for row in rows:
        key = _equipment_type_key(row["name"])
        groups.setdefault(key, []).append(row)

    total_equipment = len(rows)
    total_tags = sum(row["tag_count"] for row in rows)

    lines = [
        f"This system currently monitors {total_equipment} pieces of "
        f"equipment ({total_tags} tags total){plant_suffix}:",
        "",
    ]

    for _, group_rows in sorted(groups.items()):
        if len(group_rows) == 1:
            row = group_rows[0]
            lines.append(f"- {row['display_name']} ({row['tag_count']} tags)")
        else:
            names = ", ".join(row["display_name"] for row in group_rows)
            lines.append(
                f"- {len(group_rows)}x similar units "
                f"(~{group_rows[0]['tag_count']} tags each): {names}"
            )

    lines.extend(
        [
            "",
            "You can ask things like:",
            '- "what is the compressor pressure" (current value)',
            '- "what is the trend of the compressor pressure" (trend)',
            '- "what is the compressor pressure alarm limit" (threshold)',
            '- "why is the compressor pressure dropping" (root cause)',
            '- "when did the compressor pressure alarm trip" (history for one tag)',
            '- "is there anything happen yesterday" (factory-wide summary)',
        ]
    )

    return "\n".join(lines)


def _condense_evidence(evidence):
    """
    Collapse repeated (tag, condition) occurrences down to their most
    recent reading.

    A developing fault can log the same warning/alarm many times as a
    value keeps ticking past the same threshold (e.g. ten separate
    "AirPressure low_warning" rows a few seconds apart). The LLM only
    needs the causal sequence - one line per distinct condition - not
    every intermediate tick, and trimming this keeps the prompt small
    enough to answer within the AI provider's timeout.
    """
    latest_by_key = {}

    for item in evidence:
        latest_by_key[(item.tag, item.condition)] = item

    return sorted(
        latest_by_key.values(),
        key=lambda item: item.event_time,
    )


def _format_configured_thresholds(tag, rules, unit):
    """
    Render the actual configured warning/alarm limits for a tag.

    RuleEngine.evaluate_summary() only reports whether the current
    value passes or fails those limits, not the limit values
    themselves - a "what is the alarm limit" question needs the raw
    numbers, which this pulls straight from RuleEngine.rules.
    """
    if not rules:
        return f"No engineering thresholds are configured for {tag}."

    unit_suffix = f" {unit}" if unit else ""

    parts = [
        f"{label.replace('_', ' ')} {format_number(rules[label])}{unit_suffix}"
        for label in THRESHOLD_LABELS
        if label in rules
    ]

    return f"Configured thresholds for {tag}: " + ", ".join(parts) + "."


def _format_event_history(tag, events):
    """
    Render recorded alarm/warning history for a tag, oldest first.

    Used for "timeline" questions ("when did X happen"), which need
    actual machine_events records rather than the plc_data trend
    summary used by current/trend/threshold questions.
    """
    if not events:
        return f"No recorded alarm/warning history is available for {tag}."

    lines = [f"Recent alarm/warning history for {tag}:"]

    for event in reversed(events):
        value_text = ""

        if event.get("value") is not None:
            value_text = f" {format_number(event['value'])}"

            if event.get("unit"):
                value_text += f" {event['unit']}"

        lines.append(
            f"- {event['event_time']} [{event['severity'].upper()}] "
            f"{event['condition']}{value_text}"
        )

    return "\n".join(lines)


def _format_documentation_excerpts(chunks, brand, model):
    """
    Render retrieved manufacturer-documentation excerpts.

    Only called with non-empty chunks - the caller skips this
    section entirely when nothing was found, so the LLM never sees a
    "documentation" header with nothing behind it.

    Each excerpt is labeled with its page number when one is known
    (chunks stored before page tracking existed, or from the raw
    --text CLI import path, have page_number=None and are labeled
    "page unknown" instead) - the instruction below only ever asks the
    model to *relay* a page number that's already right there in the
    excerpt it's citing, never to work one out or guess, the same
    grounding discipline as every other fact in this prompt.
    """
    lines = [f"Manufacturer documentation ({brand} {model}):"]

    for chunk in chunks:
        page = chunk.get("page_number")
        page_label = f"page {page}" if page else "page unknown"
        lines.append(f"- ({chunk['source']}, {page_label}) {chunk['chunk_text']}")

    lines.append(
        "IMPORTANT: your \"Recommended checks\" must name the specific "
        "components, part names, or procedures mentioned above (not "
        "generic advice) wherever the documentation above covers the "
        "current situation. When you use an excerpt with a known page "
        "number, cite it in parentheses right after that point, e.g. "
        "\"(see page 42)\" - only cite a page number that was actually "
        "given to you above, never a number you work out or guess "
        "yourself, and never cite a page for an excerpt marked "
        "\"page unknown\"."
    )

    return "\n".join(lines)


class AskEngine:
    """
    Orchestrates one operator question end-to-end.

        question -> IndustrialQueryEngine (tag/equipment/intent)
                 -> DatabaseManager.get_history (raw historian rows)
                 -> trend_analyzer.analyse_tag (current value, trend,
                    min/max/avg)
                 -> RuleEngine.evaluate_summary (inside/outside
                    engineering limits?)
                 -> RootCauseEngine (only for root_cause questions -
                    pulls deterministic evidence from machine_events
                    alarm history)
                 -> prompt_builder.build_prompt (assembles one
                    grounded prompt)
                 -> AIProvider.generate (Qwen or GPT phrases the
                    final answer)

    The LLM is only used to phrase the final answer. Every fact in
    the prompt comes from the deterministic engines above. If no AI
    provider is available (Qwen not running, no API key), a
    deterministic-only answer is returned instead of raising.
    """

    def __init__(
        self,
        config_database_path: str | Path = CONFIG_DATABASE_PATH,
        machine_database_path: str | Path = MACHINE_DATABASE_PATH,
    ) -> None:
        self.config_database_path = Path(config_database_path)

        self.query_engine = IndustrialQueryEngine(
            database_path=config_database_path,
        )

        self.database = DatabaseManager(
            db_path=machine_database_path,
        )

        self.rule_engine = RuleEngine(
            database_path=config_database_path,
        )

        self.root_cause_engine = RootCauseEngine(
            database_path=machine_database_path,
        )

        self.event_store = EventStore(
            database_path=machine_database_path,
        )

        self.ai_provider, self.ai_error = self._load_ai_provider()

        # General pending-action state for the current conversation.
        # Today this only holds a "select one of these candidates"
        # menu, but the shape (a dict keyed by "kind") is meant to
        # also carry a future "confirm this write" kind - e.g.
        # confirming a threshold change - without a redesign later.
        self._pending: dict | None = None

        # Recent real (non-chitchat) questions, oldest first, used as
        # context for _try_correct_question() so a follow-up like "how
        # about the past few days?" - or one referring further back,
        # like "what about the day before that" - can be rewritten
        # into a standalone question before matching. See ask() and
        # _try_correct_question() below.
        #
        # Deliberately a plain list of question strings, capped to the
        # last HISTORY_TURNS_KEPT - not full conversational memory
        # (answers aren't stored, and it's only ever consulted in the
        # fallback path when the fast matcher already failed). Kept
        # this shape specifically so it's a small, contained change
        # later rather than a redesign: dropping the cap for an
        # unbounded running history, storing answers alongside
        # questions, or feeding it into every question (not just the
        # fallback) all extend this same list without touching how
        # it's built or trimmed.
        self._history: list[str] = []

    @staticmethod
    def _load_ai_provider() -> tuple[AIProvider | None, str]:
        try:
            return AIProvider(), ""
        except Exception as error:
            return None, str(error)

    @staticmethod
    def _history_hours(intent: str) -> int:
        return HISTORY_HOURS_BY_INTENT.get(
            intent,
            DEFAULT_HISTORY_HOURS,
        )

    @staticmethod
    def _deterministic_answer(
        machine_context: str,
        rule_context: str,
        reason: str,
    ) -> str:
        header = "AI phrasing is unavailable"

        if reason:
            header += f" ({reason})"

        header += " - showing the deterministic result instead."

        return "\n".join(
            [
                header,
                "",
                "Confirmed engineering observations:",
                machine_context,
                "",
                "Engineering limit evaluation:",
                rule_context,
            ]
        )

    def _list_available_equipment(self, plant: str | None = None) -> str:
        """
        plant, when given, is the already-canonicalized "p01"/"p02"/...
        form ConceptExtractor.extract_plant() produces - equipment
        names for the imported P01/P02 dataset are always prefixed
        that way (e.g. "p01_air_compressor_ac01"), same prefix
        _equipment_type_key() and industrial_query_engine.py's
        _break_plant_ties() already rely on, so a plain LIKE prefix
        match is consistent with how "plant" is used everywhere else -
        no new convention introduced. The handful of legacy/dead
        equipment rows with no plant prefix at all (e.g. "chiller",
        "cold_room" - pre-P01 remnants, already 0 enabled tags each)
        are naturally excluded by this filter same as by the existing
        tag_count > 0 filter, not a special case.
        """
        connection = sqlite3.connect(self.config_database_path)
        connection.row_factory = sqlite3.Row

        try:
            rows = connection.execute(
                """
                SELECT e.name, e.display_name, COUNT(t.id) AS tag_count
                FROM equipment e
                JOIN tags t ON t.equipment_id = e.id AND t.enabled = 1
                WHERE (? IS NULL OR e.name LIKE ? || '\\_%' ESCAPE '\\')
                GROUP BY e.id
                HAVING tag_count > 0
                ORDER BY e.display_name
                """,
                (plant, plant),
            ).fetchall()
        finally:
            connection.close()

        return _format_available_equipment(rows, plant=plant)

    def _lookup_brand_model(self, equipment_name: str) -> tuple[str, str] | None:
        if not equipment_name:
            return None

        connection = sqlite3.connect(self.config_database_path)

        try:
            row = connection.execute(
                "SELECT brand, model FROM equipment WHERE name = ?",
                (equipment_name,),
            ).fetchone()
        finally:
            connection.close()

        if not row or not row[0] or not row[1]:
            return None

        return row[0], row[1]

    def _status_tag_rows(self, equipment_name: str | None):
        connection = sqlite3.connect(self.config_database_path)
        connection.row_factory = sqlite3.Row

        try:
            if equipment_name is None:
                return connection.execute(
                    """
                    SELECT tags.tag_name, tags.unit, e.display_name AS equipment_display_name
                    FROM tags
                    JOIN equipment AS e ON e.id = tags.equipment_id
                    WHERE tags.enabled = 1
                    ORDER BY e.display_name, tags.tag_name
                    """
                ).fetchall()

            return connection.execute(
                """
                SELECT tags.tag_name, tags.unit, e.display_name AS equipment_display_name
                FROM tags
                JOIN equipment AS e ON e.id = tags.equipment_id
                WHERE tags.enabled = 1 AND e.name = ?
                ORDER BY tags.tag_name
                """,
                (equipment_name,),
            ).fetchall()
        finally:
            connection.close()

    def _evaluate_status(self, equipment_name: str | None) -> list[dict]:
        """
        Build one evaluated status dict per enabled tag (optionally
        scoped to one equipment instance), reusing RuleEngine exactly
        the same way _answer_for_tag() does for individual tags -
        just against the single latest reading per tag
        (DatabaseManager.get_latest_all(), one query total) rather
        than a full history window, since a status check only needs
        "is this currently fine", not a trend.
        """
        rows = self._status_tag_rows(equipment_name)
        latest = self.database.get_latest_all()

        statuses = []

        for row in rows:
            latest_row = latest.get(row["tag_name"])
            value = latest_row["value"] if latest_row else None

            rule_result = self.rule_engine.evaluate_summary(
                {"tag": row["tag_name"], "current": value}
            )

            statuses.append(
                {
                    "tag": row["tag_name"],
                    "equipment": row["equipment_display_name"],
                    "unit": row["unit"],
                    "value": value,
                    **rule_result,
                }
            )

        return statuses

    def _phrase_with_llm(self, question: str, deterministic_text: str) -> str:
        """
        Optionally re-phrase an already-correct, deterministic answer
        through the LLM instead of returning it verbatim - see
        INSTANT_ANSWERS_SKIP_LLM above. The LLM is only ever asked to
        restate the given facts conversationally, never to add to
        them, so this can't introduce ungrounded claims the way
        phrasing raw sensor data could - if it fails or is
        unavailable, the verbatim deterministic text is still a
        correct answer on its own.
        """
        if INSTANT_ANSWERS_SKIP_LLM or self.ai_provider is None:
            return deterministic_text

        prompt = (
            "You are SmartMachineAI, a factory monitoring assistant. "
            f'A user asked: "{question}"\n\n'
            "Here is the exact, already-verified answer to relay to "
            "them. Every fact below is confirmed correct - do not "
            "add, remove, invent, or change any names, numbers, or "
            f"values:\n\n{deterministic_text}\n\n"
            "Rephrase this as a natural, conversational reply. Keep "
            "every fact, name, and number exactly as given above."
        )

        try:
            return self.ai_provider.generate(prompt)
        except Exception:
            return deterministic_text

    def _factory_wide_timeline(self, time_expression: str) -> str:
        start, end, label = _factory_timeline_range(time_expression)

        events = self.event_store.get_recent_events(
            limit=FACTORY_WIDE_EVENTS_LIMIT,
            start_time=start,
            end_time=end,
        )

        return _format_factory_wide_events(events, label)

    def _answer_for_tag(
        self,
        tag_name: str,
        equipment: str,
        intent: str,
        question: str,
    ) -> str:
        history = self.database.get_history(
            tag=tag_name,
            hours=self._history_hours(intent),
            limit=DEFAULT_HISTORY_LIMIT,
        )

        if not history:
            return (
                f"'{tag_name}' was resolved, but no "
                "historian data has been recorded for it yet."
            )

        summary = analyse_tag(tag_name, history)
        rule_result = self.rule_engine.evaluate_summary(summary)

        machine_context = format_machine_context(
            [summary],
            intent,
        )

        rule_context = format_rule_results(
            [rule_result],
            include_normal=True,
        )

        if intent == "root_cause":
            root_cause_result = (
                self.root_cause_engine.analyze_latest_alarm(
                    equipment=equipment or None,
                    tag=tag_name or None,
                )
            )

            root_cause_result.evidence = _condense_evidence(
                root_cause_result.evidence
            )

            machine_context = (
                f"{machine_context}\n\n"
                f"{format_root_cause_result(root_cause_result)}"
            )

            brand_model = self._lookup_brand_model(equipment)

            if brand_model:
                brand, model = brand_model

                documentation_query = " ".join(
                    part
                    for part in (
                        tag_name,
                        root_cause_result.alarm_condition,
                        root_cause_result.alarm_message,
                    )
                    if part
                )

                chunks = search_chunks(
                    brand,
                    model,
                    documentation_query,
                    database_path=self.config_database_path,
                )

                if chunks:
                    machine_context = (
                        f"{machine_context}\n\n"
                        f"{_format_documentation_excerpts(chunks, brand, model)}"
                    )

        if intent == "threshold":
            configured_thresholds = self.rule_engine.rules.get(tag_name)

            rule_context = (
                f"{rule_context}\n\n"
                f"{_format_configured_thresholds(tag_name, configured_thresholds, summary['unit'])}"
            )

        if intent == "timeline":
            recent_events = self.event_store.get_recent_events(
                tag=tag_name,
                limit=RECENT_EVENTS_LIMIT,
            )

            machine_context = (
                f"{machine_context}\n\n"
                f"{_format_event_history(tag_name, recent_events)}"
            )

        route = {
            "equipment": equipment or "Unknown",
            "intent": intent,
        }

        prompt = build_prompt(
            question=question,
            machine_context=machine_context,
            rule_context=rule_context,
            route=route,
        )

        if self.ai_provider is None:
            return self._deterministic_answer(
                machine_context,
                rule_context,
                self.ai_error,
            )

        try:
            return self.ai_provider.generate(prompt)
        except Exception as error:
            return self._deterministic_answer(
                machine_context,
                rule_context,
                str(error),
            )

    def _answer_comparison(
        self,
        tag_name: str,
        equipment: str,
        question: str,
        period_a: str,
        period_b: str,
    ) -> str:
        """
        Answer a "compare X for period A vs period B" question (see
        engine.concept_extractor.extract_comparison_periods()).

        Deliberately different from _answer_for_tag(): that method
        only ever pulls ONE window of history, so it has no way to
        answer a two-period question honestly - a real gap found live
        ("compare power consumption for today and yesterday" was
        answered anyway, with the LLM inventing a "yesterday" figure
        that was never actually computed and getting the comparison
        backwards). This method computes BOTH periods' real averages
        deterministically, computes which one is actually higher/lower
        in Python (never left to the LLM to work out), and refuses
        with an honest "I don't have that data" instead of guessing
        when either period has no historian data at all.
        """
        start_a, end_a, label_a = _factory_timeline_range(period_a)
        start_b, end_b, label_b = _factory_timeline_range(period_b)

        history_a = self.database.get_history_range(tag_name, start_a, end_a)
        history_b = self.database.get_history_range(tag_name, start_b, end_b)

        missing = [
            label for label, history in ((label_a, history_a), (label_b, history_b))
            if not history
        ]

        if missing:
            return (
                f"I don't have historian data for '{tag_name}' for "
                f"{' or '.join(missing)}, so I can't make that "
                "comparison honestly. Could you pick a different date, "
                "or ask about a period with recorded data?"
            )

        summary_a = analyse_tag(tag_name, history_a)
        summary_b = analyse_tag(tag_name, history_b)

        machine_context = _format_comparison_context(
            tag_name, label_a, summary_a, label_b, summary_b
        )

        route = {"equipment": equipment or "Unknown", "intent": "comparison"}

        prompt = build_prompt(
            question=question,
            machine_context=machine_context,
            rule_context="Not applicable to a period-to-period comparison.",
            route=route,
        )

        if self.ai_provider is None:
            return machine_context

        try:
            return self.ai_provider.generate(prompt)
        except Exception:
            return machine_context

    def _resolve_and_answer(self, result, question: str) -> str | None:
        """
        Turn one query_engine result into a final answer - or None if
        it's unresolved/ambiguous, letting the caller in ask() decide
        whether to retry with an LLM-corrected question
        (_try_correct_question()) or fall back to the numbered
        candidate menu. Every branch here is unchanged from before
        this was split out of ask() - the only new behavior is that
        "no confident match" returns None instead of building the
        menu directly.
        """
        if result.intent in CHITCHAT_RESPONSES:
            return self._phrase_with_llm(question, CHITCHAT_RESPONSES[result.intent])

        if result.intent == "discovery":
            return self._phrase_with_llm(
                question, self._list_available_equipment(result.concepts.plant or None)
            )

        if result.intent == "equipment_status":
            top_candidate = result.candidates[0] if result.candidates else None
            equipment_score = 0.0

            if top_candidate:
                equipment_score, _ = self.query_engine._equipment_score(
                    result.concepts.equipment_terms, top_candidate.tag
                )

            # Fallback confidence signal for when equipment_terms comes
            # back COMPLETELY EMPTY even though a specific equipment WAS
            # clearly named - e.g. "cold room", "tank": each is a real
            # equipment type name that ALSO happens to be a generic
            # LOCATIONS/CONDITIONS vocabulary word ("room"/"cold",
            # "tank"), so the word(s) get stripped as concept modifiers
            # instead of surviving as the equipment identifier, leaving
            # nothing at all for _equipment_score to match against
            # (found live: "show all cold room status" returned a
            # factory-wide summary that never mentioned Cold Room at
            # all). The engine's own top-candidate ranking already
            # accounts for this correctly in that specific case
            # (location="room" + condition="cold" score highly against
            # real Cold Room tags) - every candidate collapsing to the
            # same equipment category is that same signal, just not
            # visible from equipment_score alone.
            #
            # Deliberately gated on equipment_terms being empty, not
            # just "equipment_score is low" - tried a broader version
            # of this check first and it backfired: "show all air
            # header status" (equipment_terms=('air',), a real but
            # weak/partial match) and "show all MCC room status"
            # (equipment_terms=('mcc',)) both have every top-5
            # candidate share one category too, but the WRONG one (Air
            # Compressor, Cold Room respectively) - a non-empty-but-
            # weak equipment_terms match is a real signal that should
            # still be trusted over this fallback, not overridden by
            # it. Only a completely empty equipment_terms means "the
            # normal mechanism had literally nothing to work with",
            # which is the actual bug this exists to rescue - found by
            # testing this broader version against every equipment
            # type in the system before trusting it, not assumed.
            category_confident = (
                not result.concepts.equipment_terms
                and result.status == "clarification_required"
                and result.candidates
                and len({
                    _equipment_category_key(c.tag.equipment_name)
                    for c in result.candidates
                })
                == 1
            )

            if (
                equipment_score >= EQUIPMENT_STATUS_SCORE_FLOOR or category_confident
            ) and top_candidate:
                tag = top_candidate.tag

                return self._phrase_with_llm(
                    question,
                    _format_equipment_status(
                        tag.equipment_display_name,
                        self._evaluate_status(tag.equipment_name),
                    ),
                )

            return self._phrase_with_llm(
                question, _format_factory_status(self._evaluate_status(None))
            )

        if result.intent == "timeline" and (
            result.status != "resolved" or not result.selected_tag
        ):
            return self._phrase_with_llm(
                question, self._factory_wide_timeline(result.time_expression)
            )

        if (
            result.intent in ("current_data", "trend", "threshold", "comparison")
            and result.concepts
            and result.concepts.measurement == "power"
            and (result.status != "resolved" or not result.selected_tag)
        ):
            top_candidate = result.candidates[0] if result.candidates else None
            equipment_score = 0.0

            if top_candidate:
                equipment_score, _ = self.query_engine._equipment_score(
                    result.concepts.equipment_terms, top_candidate.tag
                )

            if equipment_score < FACTORY_TOTAL_POWER_EQUIPMENT_SCORE_FLOOR:
                if result.intent == "comparison":
                    return self._answer_comparison(
                        FACTORY_TOTAL_POWER_TAG,
                        FACTORY_TOTAL_POWER_EQUIPMENT,
                        question,
                        result.concepts.compare_period_a,
                        result.concepts.compare_period_b,
                    )
                return self._answer_for_tag(
                    FACTORY_TOTAL_POWER_TAG,
                    FACTORY_TOTAL_POWER_EQUIPMENT,
                    result.intent,
                    question,
                )

        if result.status != "resolved" or not result.selected_tag:
            return None

        if result.intent == "comparison":
            return self._answer_comparison(
                result.selected_tag,
                result.equipment,
                question,
                result.concepts.compare_period_a,
                result.concepts.compare_period_b,
            )

        return self._answer_for_tag(
            result.selected_tag,
            result.equipment,
            result.intent,
            question,
        )

    def _try_correct_question(
        self, question: str, history: list[str] | None = None
    ) -> str | None:
        """
        Ask the LLM to fix typos/unclear phrasing - or resolve a
        follow-up reference to recent questions - in a question the
        deterministic matcher couldn't confidently resolve on its own.
        Used only as a fallback in ask(), never run on a question that
        already resolves fine. The corrected text is only ever used to
        retry matching against the same deterministic engine, never
        treated as an answer itself, so this can't introduce an
        ungrounded claim the way asking the LLM to just answer
        directly could.

        This system has no general conversation memory - each question
        is normally resolved in complete isolation, unlike a real LLM
        chat that sees the whole conversation. Passing recent questions
        (see HISTORY_TURNS_KEPT/self._history) as optional context here
        lets a short, otherwise-meaningless follow-up ("how about the
        past few days?", "and the chiller?", or one referencing further
        back like "what about the day before that") get rewritten into
        a standalone question before matching - a narrow, fallback-only
        rescue, not real conversational memory.

        Deliberately a short, output-only-the-question prompt: since
        generation time on this hardware is bound by output length
        (~0.6-0.8 tokens/sec), asking for a handful of corrected words
        back is far cheaper than the multi-paragraph answers this same
        model takes minutes for elsewhere in this app.

        The history branch below is a step-by-step prompt, not a
        single loose instruction - verified necessary against real
        qwen2.5:7b, not just assumed: the first (single-instruction)
        version reliably ignored the "use history" half of the
        instruction and just fixed typos, e.g. "how about in the pass
        few days?" only became "How about in the past few days?" -
        still missing "alarms"/"warnings" from the prior turn, so it
        still failed to match. Forcing an explicit "does it name its
        own subject?" check first, before allowing a rewrite, reliably
        produced a correct carry-over in testing.

        **Second real gap, found later:** even with that fix, a bare
        follow-up with no explicit keyword to latch onto - "how about
        yesterday" after "anything happen today" - still failed. The
        model correctly detected it as a subject-less follow-up (Step
        1/2 fired), but instead of carrying over the actual prior
        topic it invented an unrelated, plausible-sounding one
        ("How about yesterday's equipment operation?", when the prior
        turn was about alarm/warning events, not "operation" at all) -
        confirmed reproducible/deterministic against the real model,
        not a one-off sampling fluke. The original instruction just
        said "carry over the missing subject", without ever asking the
        model to first identify *what that subject actually is* -
        adding an explicit "what was message N actually asking about,
        in a few words" reasoning step before the rewrite step fixed
        it (verified: 2/2 runs correctly produced "anything happened
        yesterday"/"Did anything happen yesterday?", both of which
        re-resolve as a real `timeline` match). Also re-verified this
        didn't regress the earlier "past few days" case (still
        correctly carries "alarm" forward) or the topic-switch "and
        the chiller?" case (still resolves the same way as before -
        both old and new phrasings happen to blend in "alarm" for that
        one, which was already true before this change too, and
        doesn't break the chiller clarification menu either way).
        Don't revert to a plain "carry over the missing subject"
        instruction without re-verifying against the real model, the
        same way this was found.
        """
        if self.ai_provider is None:
            return None

        if history:
            numbered = "\n".join(
                f"{i}. {q}" for i, q in enumerate(history, start=1)
            )
            prompt = (
                "A user is asking a factory-monitoring assistant a "
                "series of questions. Here is the recent conversation, "
                f"oldest first:\n{numbered}\n\n"
                f'The user\'s newest message is: "{question}"\n\n'
                "Step 1: In a few words, what topic was the most "
                "recent relevant earlier message actually asking about "
                "(e.g. \"alarm/warning events\", \"compressor "
                "pressure\", \"chiller status\")? This is just for "
                "your own reasoning - don't include it in the output.\n"
                "Step 2: Does the newest message already name its own "
                "subject (an equipment name, a measurement, an "
                "alarm/warning, a specific topic)? Words like \"how "
                "about\", \"and\", \"what about\" alone do NOT count "
                "as a subject.\n"
                "Step 3: If it does NOT name its own subject, rewrite "
                "it as one complete standalone question that combines "
                "the topic from Step 1 with the newest message's own "
                "new details (like a different time range). If it DOES "
                "already name its own subject, just fix any typos or "
                "unclear phrasing.\n\n"
                "Output ONLY the final question text, nothing else - "
                "no explanation, no quotes, no step labels, no "
                "reasoning."
            )
        else:
            prompt = (
                "A user asked a factory-monitoring assistant this "
                f'question, but it could not be understood: "{question}"\n\n'
                "If it contains typos, unclear phrasing, or shorthand, "
                "rewrite it as a single clear, well-spelled question "
                "with the same meaning. Output ONLY the corrected "
                "question, nothing else - no explanation, no quotes, "
                "no preamble."
            )

        try:
            raw = self.ai_provider.generate(prompt).strip()
        except Exception:
            return None

        corrected = raw.splitlines()[0].strip().strip('"').strip("'") if raw else ""

        if not corrected or corrected.lower() == question.strip().lower():
            return None

        return corrected

    def _remember(self, question: str) -> None:
        """Append a resolved question to self._history, capped to the last HISTORY_TURNS_KEPT."""
        self._history.append(question)
        self._history = self._history[-HISTORY_TURNS_KEPT:]

    def ask(self, question: str) -> str:
        """Answer one operator question and return the final text."""
        stripped_question = question.strip()

        if self._pending and self._pending["kind"] == "select_candidate":
            if stripped_question.isdigit():
                candidates = self._pending["candidates"]
                index = int(stripped_question)

                if 1 <= index <= len(candidates):
                    tag = candidates[index - 1].tag
                    intent = self._pending["intent"]
                    original_question = self._pending["question"]
                    self._pending = None
                    self._remember(original_question)

                    return self._answer_for_tag(
                        tag.tag_name,
                        tag.equipment_name,
                        intent,
                        original_question,
                    )

                return (
                    f"Please reply with a number between 1 and "
                    f"{len(candidates)}, or ask a new question."
                )

            # Anything other than a bare number abandons the pending
            # menu - treat it as a fresh question instead.
            self._pending = None

        # Captured before this turn appends to it below, so it always
        # reflects the questions from up to HISTORY_TURNS_KEPT turns
        # ago - see _try_correct_question() for how it's used.
        recent_history = list(self._history)

        result = self.query_engine.query(question)
        answer = self._resolve_and_answer(result, question)

        if answer is not None:
            if result.intent not in CHITCHAT_RESPONSES:
                self._remember(question)

            return answer

        # Unresolved/ambiguous - try one LLM-assisted typo/follow-up
        # correction (using recent history as context, so a short
        # reference like "how about the past few days?" - or one
        # further back, like "what about the day before that" - can be
        # rewritten into a standalone question) before falling back to
        # the numbered candidate menu. Only reached here, so every
        # question that already resolves confidently is completely
        # unaffected and stays exactly as fast as before.
        corrected = self._try_correct_question(question, recent_history)

        if corrected:
            retry_result = self.query_engine.query(corrected)
            retry_answer = self._resolve_and_answer(retry_result, question)

            if retry_answer is not None:
                if retry_result.intent not in CHITCHAT_RESPONSES:
                    self._remember(corrected)

                return retry_answer

            # Corrected text still didn't resolve - its candidates are
            # usually a strict improvement over the original garbled
            # attempt's, so show those instead.
            result = retry_result

        candidates = result.candidates[:5]

        if not candidates:
            return result.message

        self._pending = {
            "kind": "select_candidate",
            "candidates": candidates,
            "intent": result.intent,
            "question": result.question,
        }

        return _format_candidate_menu(candidates, result.status)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ask SmartMachineAI a question about the factory.",
    )

    parser.add_argument(
        "--config-database",
        default=str(CONFIG_DATABASE_PATH),
    )

    parser.add_argument(
        "--machine-database",
        default=str(MACHINE_DATABASE_PATH),
    )

    args = parser.parse_args()

    try:
        engine = AskEngine(
            config_database_path=args.config_database,
            machine_database_path=args.machine_database,
        )
    except Exception as error:
        print(f"Unable to start SmartMachineAI: {error}")
        return

    if engine.ai_provider is not None:
        print(f"AI provider: {engine.ai_provider.provider} ({engine.ai_provider.model})")
    else:
        print(f"AI provider: unavailable ({engine.ai_error})")

    print("Type 'exit' to stop.")

    while True:
        try:
            question = input("\nQuestion: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if question.lower() in {"exit", "quit"}:
            break

        if not question:
            continue

        try:
            answer = engine.ask(question)
        except Exception as error:
            print(f"Error answering question: {error}")
            continue

        print(f"\n{answer}")


if __name__ == "__main__":
    main()
