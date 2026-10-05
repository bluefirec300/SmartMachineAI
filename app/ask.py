from __future__ import annotations

import argparse
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ai.ai_provider import AIProvider
from ai.context_builder import (
    build_comparison_ai_context,
    build_equipment_ai_context,
    build_factory_ai_context,
)
from ai.event_store import EventStore
from ai.grounding_guard import check_grounding
from ai.interpretation_intent import classify_interpretation_intent
from ai.interpretation_prompt_builder import (
    build_deterministic_fallback,
    build_interpretation_prompt,
)
from ai.prompt_builder import build_prompt, format_machine_context
from ai.root_cause_engine import RootCauseEngine, format_root_cause_result
from ai.rule_engine import RuleEngine, format_rule_results
from ai.trend_analyzer import analyse_tag, format_number
from config.environment import get_config_db_path, get_machine_db_path
from database.database import DatabaseManager
from engine.concept_extractor import extract_comparison_periods
from engine.industrial_query_engine import IndustrialQueryEngine
from rag.retrieval import search_chunks


@dataclass
class AskResult:
    """
    Phase 15 - additive structured result contract. AskEngine.ask()
    keeps returning a plain string unchanged (existing callers - the
    CLI REPL, tests - are unaffected). AskEngine.ask_structured() is the
    NEW entry point the UI uses, so it can reliably show resolved
    equipment / View Evidence / source timestamps without re-parsing a
    plain string.
    """

    answer: str
    intent: str
    resolved_entities: list[dict[str, Any]] = field(default_factory=list)
    structured_context: dict[str, Any] | None = None
    provider: str | None = None
    # Phase V2.4 - which Ollama model actually produced this answer
    # (e.g. "qwen2.5:3b" in Fast mode, "qwen2.5:7b" in Thorough mode) -
    # None whenever provider is also None (no AI phrasing happened at all).
    model: str | None = None
    # "grounded" | "violation_detected" | "not_checked" | "not_applicable"
    grounding_status: str = "not_applicable"
    fallback_used: bool = False


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
# listing that doesn't need paraphrasing). current_data/root_cause/
# trend/threshold/timeline-for-one-tag/comparison were never affected
# either way - those already always go through real Ollama regardless
# of this flag.
#
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

# Same fallback pattern as the power tag above, for "what is the
# factory/total water consumption" - the Building Water Meter is the
# single point that measures total incoming water for the whole plant,
# same role as the Main Incomer for power. Unlike power (instantaneous,
# answered via the normal current-value path), the underlying tag here
# is a monotonic running total - see _answer_factory_water_consumption()
# for why that needs its own dedicated handling instead of just
# reusing _answer_for_tag(). No equipment-score floor constant here
# (unlike the power fallback above) - see the dispatch site in
# _resolve_and_answer() for why that check isn't used for this one.
FACTORY_WATER_METER_TAG = "P01.WATER.MTR01.Total"
FACTORY_WATER_METER_EQUIPMENT = "Building Water Meter MTR01 (P01)"

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

# Explicit "that's not what I wanted, ask the real AI" follow-up
# phrases - see AskEngine._maybe_escalate_to_llm(). Deliberately a
# short, literal-substring list (same conservative approach as
# _correct_word()'s guard lists) rather than a broad keyword match, so
# a genuine new question that happens to mention "AI"/"claude"/"ollama"
# in passing isn't misread as a rejection of the previous answer.
ESCALATION_PHRASES = (
    "not what i want",
    "not what i meant",
    "none of these",
    "none of those",
    "thats not it",
    "that's not it",
    "ask the ai",
    "ask ai directly",
    "use the ai directly",
    "ask claude",
    "ask ollama",
)

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


def _format_factory_wide_events(events, label, plant: str | None = None):
    """
    Render a factory-wide (not tag-specific) event summary - or, when
    `plant` is given, one scoped to that plant only.

    Used when a timeline question doesn't name a specific tag/
    equipment ("is there anything happen yesterday") - the normal
    tag resolver has nothing to resolve to, so this queries
    machine_events across everything (or one plant) instead of
    failing outright.
    """
    scope = f"in {plant.upper()} " if plant else ""

    if not events:
        return f"No alarm or warning events were recorded {scope}{label}."

    lines = [
        f"Alarm/warning events {scope}{label} "
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
        # Each blank line before a header/summary line below is
        # required, not cosmetic - _format_status_line() renders each
        # reading as a Markdown bullet ("- tag (value): message"), and
        # a plain (non-bulleted) line immediately following a bullet
        # with no blank line between them gets treated as a lazy
        # continuation of that SAME bullet by the Markdown renderer,
        # not a new line - confirmed live: without these blank lines,
        # the next section's header/equipment name was silently
        # swallowed onto the end of the previous bullet's text.
        if alarms:
            lines.append(f"IN ALARM ({len(alarms)}):")
            lines.extend(_format_status_line(s) for s in alarms)
        if warnings:
            lines.append("")
            lines.append(f"IN WARNING ({len(warnings)}):")
            lines.extend(_format_status_line(s) for s in warnings)
        if normal:
            lines.append("")
            lines.append(f"{len(normal)} other reading(s) normal.")

    if not_evaluated:
        lines.append("")
        lines.append(
            f"({len(not_evaluated)} reading(s) have no configured "
            "threshold, so aren't included above.)"
        )

    if no_data:
        lines.append("")
        lines.append(
            f"({len(no_data)} tag(s) have no historian data recorded yet.)"
        )

    return "\n".join(lines)


def _format_factory_status(statuses, plant: str | None = None):
    """
    Render a deterministic, no-LLM status summary - factory-wide by
    default, or scoped to one plant when `plant` is given (e.g. "p01")
    - used when an "equipment_status" question doesn't name a specific
    instance confidently enough (see EQUIPMENT_STATUS_SCORE_FLOOR in
    ask()), mirroring the factory-wide timeline fallback above.
    """
    evaluated = [s for s in statuses if s["value"] is not None]

    alarms = [s for s in evaluated if s["severity"] == "alarm"]
    warnings = [s for s in evaluated if s["severity"] == "warning"]

    scope = plant.upper() if plant else "the factory"

    if not alarms and not warnings:
        return (
            f"Everything looks normal across {scope} - all "
            f"{len(evaluated)} monitored reading(s) with a configured "
            "threshold are within limits."
        )

    header_scope = plant.upper() if plant else "Factory-wide"

    lines = [
        f"{header_scope} status - {len(alarms)} in alarm, "
        f"{len(warnings)} in warning (out of {len(evaluated)} monitored "
        "reading(s) with a configured threshold):",
        "",
    ]

    by_equipment: dict[str, list] = {}

    for status in alarms + warnings:
        by_equipment.setdefault(status["equipment"], []).append(status)

    # The blank line appended before each equipment header (except the
    # first, which already has one from the intro block above) is
    # required, not cosmetic - _format_status_line() renders each
    # reading as a Markdown bullet ("- tag (value): message"), and a
    # plain (non-bulleted) header line immediately following a bullet
    # with no blank line between them gets treated as a lazy
    # continuation of that SAME bullet by the Markdown renderer, not a
    # new line - confirmed live: without this, the NEXT equipment's
    # name was silently swallowed onto the end of the PREVIOUS
    # equipment's last bullet, garbling which reading belonged to which
    # equipment.
    for index, (equipment_display_name, equipment_statuses) in enumerate(sorted(by_equipment.items())):
        if index > 0:
            lines.append("")
        lines.append(f"{equipment_display_name}:")
        lines.extend(
            _format_status_line(s)
            for s in sorted(
                equipment_statuses, key=lambda s: s["severity"] != "alarm"
            )
        )

    return "\n".join(lines)


def _format_candidate_menu(candidates, status, lead_sentence=None):
    """
    Render a numbered "which one did you mean" menu.

    status distinguishes "found several good matches, pick one"
    (clarification_required) from "nothing matched confidently, here
    are the closest guesses" (no_match) - the wording is honest about
    which situation the operator is in.

    lead_sentence, when given, replaces the plain templated header
    below with an LLM-phrased counter-question (see
    AskEngine._phrase_clarifying_question()) - the numbered list itself
    is always built the exact same deterministic way either way, so a
    missing/failed AI call just falls back to the plain header, never
    to a broken or empty menu.
    """
    if lead_sentence:
        header = lead_sentence
    elif status == "clarification_required":
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
    Render retrieved manufacturer-documentation excerpts as plain
    facts - no LLM-only instructions here. This becomes part of
    machine_context, which _deterministic_answer() shows verbatim to
    the operator whenever the AI call is unavailable/fails, so it must
    only ever contain real, confirmed facts - see
    ai.prompt_builder.build_prompt()'s root_cause branch for the
    citation-discipline instructions that used to live here (moved out
    2026-08-15 after they leaked into a user-visible fallback answer:
    an AI-provider timeout caused this exact "IMPORTANT: your
    Recommended checks must..." instruction text to be shown directly
    to the operator instead of ever reaching a model).

    Each excerpt is labeled with its page number when one is known
    (chunks stored before page tracking existed, or from the raw
    --text CLI import path, have page_number=None and are labeled
    "page unknown" instead).
    """
    lines = [f"Manufacturer documentation ({brand} {model}):"]

    for chunk in chunks:
        page = chunk.get("page_number")
        page_label = f"page {page}" if page else "page unknown"
        lines.append(f"- ({chunk['source']}, {page_label}) {chunk['chunk_text']}")

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
        model_override: str | None = None,
        provider_name: str | None = None,
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

        self._provider_name = provider_name
        self.ai_provider, self.ai_error = self._load_ai_provider(model_override, provider_name)

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

        # What the previous ask() call actually answered with, so a
        # follow-up like "that's not what I wanted" can redo the same
        # turn through the LLM instead of just being treated as a
        # brand new question. "kind" is one of:
        #   "menu"     - an ambiguity menu was shown; "candidates" holds
        #                the exact list offered, so an escalation can
        #                only ever synthesize across tags the user
        #                already saw, never a new/invented one.
        #   "instant"  - a deterministic answer that skipped the LLM
        #                per INSTANT_ANSWERS_SKIP_LLM (equipment_status/
        #                discovery/chitchat/factory-wide timeline);
        #                "deterministic_text" is the exact same text
        #                _phrase_with_llm() would otherwise just rephrase.
        #   "llm_answer" - the last answer already came from the LLM;
        #                nothing to escalate.
        # None until the first ask() call completes.
        self._last_turn: dict | None = None

    @staticmethod
    def _load_ai_provider(
        model_override: str | None = None,
        provider_name: str | None = None,
    ) -> tuple[AIProvider | None, str]:
        try:
            return AIProvider(provider_name=provider_name, model_override=model_override), ""
        except Exception as error:
            return None, str(error)

    def set_model_override(self, model_override: str | None) -> None:
        """
        Phase V2.4 - Ask AI Fast/Thorough mode. Rebuilds ONLY
        self.ai_provider/self.ai_error, in place - deliberately not
        "just construct a new AskEngine", which would also silently
        reset self._pending (an in-progress clarification menu) and
        self._history (recent-question context for follow-up
        rewriting). Switching modes mid-conversation should change
        which model answers the NEXT question, nothing else.

        provider_name is left at whatever it already was (None unless
        set_provider() below was previously called) - this method only
        ever changes the model within the current provider.
        """
        self.ai_provider, self.ai_error = self._load_ai_provider(
            model_override, self._provider_name
        )

    def set_provider(
        self,
        provider_name: str | None,
        model_override: str | None = None,
    ) -> None:
        """
        (testing) - Ask AI provider selector. Same in-place-rebuild
        pattern as set_model_override() above (only self.ai_provider/
        self.ai_error change - self._pending/self._history untouched).
        provider_name=None restores whatever settings.ini/env already
        configure (the pre-existing default, "ollama" unless changed
        there) - this method is purely additive, never required.
        """
        self._provider_name = provider_name
        self.ai_provider, self.ai_error = self._load_ai_provider(
            model_override, provider_name
        )

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

    def _status_tag_rows(self, equipment_name: str | None, plant: str | None = None):
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
                        AND (? IS NULL OR e.name LIKE ? || '\\_%' ESCAPE '\\')
                    ORDER BY e.display_name, tags.tag_name
                    """,
                    (plant, plant),
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

    def _evaluate_status(
        self, equipment_name: str | None, plant: str | None = None
    ) -> list[dict]:
        """
        Build one evaluated status dict per enabled tag (optionally
        scoped to one equipment instance, or - when equipment_name is
        None - to one plant), reusing RuleEngine exactly the same way
        _answer_for_tag() does for individual tags - just against the
        single latest reading per tag (DatabaseManager.get_latest_all(),
        one query total) rather than a full history window, since a
        status check only needs "is this currently fine", not a trend.
        plant is ignored when equipment_name is given - a resolved
        equipment instance already implies its own plant.
        """
        rows = self._status_tag_rows(equipment_name, plant if equipment_name is None else None)
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

    def _phrase_with_llm(
        self, question: str, deterministic_text: str, force: bool = False
    ) -> str:
        """
        Optionally re-phrase an already-correct, deterministic answer
        through the LLM instead of returning it verbatim - see
        INSTANT_ANSWERS_SKIP_LLM above. The LLM is only ever asked to
        restate the given facts conversationally, never to add to
        them, so this can't introduce ungrounded claims the way
        phrasing raw sensor data could - if it fails or is
        unavailable, the verbatim deterministic text is still a
        correct answer on its own.

        force=True bypasses only the INSTANT_ANSWERS_SKIP_LLM check
        (never the "no provider available" one) - used by
        _maybe_escalate_to_llm() when the user explicitly asked for
        the AI's phrasing on a turn that would otherwise have skipped
        it. Not recorded into self._last_turn, so the original
        question/answer this was escalating stays the thing a further
        escalation attempt would redo.
        """
        if not force:
            self._last_turn = {
                "kind": "instant",
                "question": question,
                "deterministic_text": deterministic_text,
            }

        if (INSTANT_ANSWERS_SKIP_LLM and not force) or self.ai_provider is None:
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

    def _factory_wide_timeline(self, time_expression: str, plant: str | None = None) -> str:
        start, end, label = _factory_timeline_range(time_expression)

        events = self.event_store.get_recent_events(
            limit=FACTORY_WIDE_EVENTS_LIMIT,
            start_time=start,
            end_time=end,
            tag_prefix=f"{plant.upper()}." if plant else None,
        )

        return _format_factory_wide_events(events, label, plant=plant)

    def _answer_factory_water_consumption(self, question: str) -> str:
        """
        "What is the factory water consumption today" - a dedicated,
        deterministic answer rather than routing FACTORY_WATER_METER_TAG
        through the normal _answer_for_tag() current-value path used
        for the power fallback. That tag is a monotonic running total
        (it only ever counts up), so its raw current reading answers
        "how much has this meter ever recorded," not "how much today" -
        the exact pitfall already found and fixed for the SCADA Floor
        Plan's info board (see
        ui.scada_floor_plan_data._todays_accumulated_total's docstring
        for the full story, including the counter-reset case). Reuses
        that same reset-safe accumulation helper instead of duplicating
        the logic here.
        """
        self._last_turn = {"kind": "llm_answer"}
        from ui.scada_floor_plan_data import _todays_accumulated_total

        today_m3 = _todays_accumulated_total(self.database, FACTORY_WATER_METER_TAG)

        if today_m3 is None:
            deterministic_text = (
                f"No water consumption has been logged yet today for {FACTORY_WATER_METER_EQUIPMENT}."
            )
        else:
            deterministic_text = (
                f"{FACTORY_WATER_METER_EQUIPMENT} has used {format_number(today_m3)} m³ "
                "of water so far today."
            )

        if self.ai_provider is None:
            return deterministic_text

        prompt = (
            "You are SmartMachineAI, a factory monitoring assistant. "
            f'A user asked: "{question}"\n\n'
            "Here is the exact, already-verified answer to relay to them. "
            "This number has already been computed correctly - do not "
            f"recompute it, second-guess it, or change it:\n\n{deterministic_text}\n\n"
            "Rephrase this as a natural, conversational reply. Keep every "
            "fact, name, and number exactly as given above."
        )

        try:
            return self.ai_provider.generate(prompt)
        except Exception:
            return deterministic_text

    def _answer_for_tag(
        self,
        tag_name: str,
        equipment: str,
        intent: str,
        question: str,
    ) -> str:
        self._last_turn = {"kind": "llm_answer"}

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

        has_documentation = False

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
                    has_documentation = True
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
            has_documentation=has_documentation,
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
        self._last_turn = {"kind": "llm_answer"}

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

            plant = result.concepts.plant or None
            return self._phrase_with_llm(
                question,
                _format_factory_status(
                    self._evaluate_status(None, plant=plant), plant=plant
                ),
            )

        if result.intent == "timeline" and (
            result.status != "resolved" or not result.selected_tag
        ):
            return self._phrase_with_llm(
                question,
                self._factory_wide_timeline(
                    result.time_expression, plant=result.concepts.plant or None
                ),
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

        # "What is the factory/total water consumption (today)" - same
        # fallback shape as the power block above, but intentionally
        # narrower: only current_data (the exact question this exists
        # for). trend/threshold/comparison against a running-total tag
        # don't have an obviously well-defined meaning yet the way they
        # do for instantaneous power, so they're left unhandled here
        # rather than guessed at - a genuinely ambiguous water question
        # of those kinds still falls through to the normal candidate
        # menu below, same as before this existed.
        #
        # Deliberately NOT gated on an equipment_score floor the way
        # the power block above is - found live that it backfires here:
        # once "water"/"consumption" are stripped, the only leftover
        # equipment_term is often "factory", which scores a spuriously
        # high match (1.0) against the Main Incomer tags because their
        # own stored description text is literally "Factory active
        # power"/"Factory cumulative energy" - an accidental word
        # collision specific to this dataset's descriptions, unrelated
        # to water at all. Since there's only one Building Water Meter
        # per plant, and this measurement only fires here when the
        # deterministic matcher couldn't already resolve a specific
        # real tag on its own (status != "resolved" below), always
        # answering with the factory-wide meter is safe - mirrors how
        # the power fallback above also unconditionally defaults to
        # P01's Main Incomer once its own (more reliable, for power)
        # floor check passes.
        if (
            result.intent == "current_data"
            and result.concepts
            and result.concepts.measurement == "waterconsumption"
            and (result.status != "resolved" or not result.selected_tag)
        ):
            return self._answer_factory_water_consumption(question)

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

    def _phrase_clarifying_question(self, question: str, candidates, status: str) -> str | None:
        """
        Ask the LLM for a short, natural counter-question when the
        deterministic matcher (even after _try_correct_question()'s
        retry) still couldn't confidently resolve a question - replaces
        _format_candidate_menu()'s plain templated header ("I found
        more than one possible match...") with something that reads
        like a real clarifying question, per explicit request: "mix
        mode" - understood questions stay instant/deterministic
        (INSTANT_ANSWERS_SKIP_LLM above), but a genuinely unresolved
        one should get real LLM help, not just a bare numbered list.

        Deliberately narrow, same discipline as _try_correct_question():
        the LLM is only ever asked to phrase ONE framing sentence around
        a candidate list that deterministic code already built - it is
        explicitly told not to list the options itself and not to
        invent any option beyond what's given, so it can't introduce an
        equipment/tag that doesn't actually exist. The numbered list
        underneath is always the same deterministic
        _format_candidate_menu() output regardless of whether this call
        succeeds, and the numbered-reply mechanism in ask() is
        completely unaffected either way - a number still selects a
        candidate, and any non-numeric reply still abandons the menu
        and is treated as a fresh question, so the operator can answer
        "with or without the numbered answer" as requested.
        """
        if self.ai_provider is None:
            return None

        equipment_labels = sorted(
            {
                candidate.tag.equipment_display_name or candidate.tag.equipment_name or "unknown equipment"
                for candidate in candidates
            }
        )
        options_text = ", ".join(equipment_labels)

        if status == "clarification_required":
            situation = "found more than one real match and needs the operator to say which one they meant"
        else:
            situation = (
                "could not confidently match the question to any configured tag, "
                "and is about to show the closest guesses it found"
            )

        prompt = (
            "A factory-monitoring assistant just tried to answer this operator "
            f'question: "{question}"\n\n'
            f"It {situation}. The possible options are: {options_text}.\n\n"
            "Write ONE short, friendly sentence asking the operator to clarify "
            "which one they meant, or to describe their question a bit more "
            "specifically. Do not list the options yourself - they will be shown "
            "separately as a numbered list right after your sentence - and do not "
            "invent or mention any option that isn't in that list. Output ONLY "
            "that one sentence, nothing else - no explanation, no quotes, no "
            "preamble."
        )

        try:
            raw = self.ai_provider.generate(prompt).strip()
        except Exception:
            return None

        sentence = raw.splitlines()[0].strip().strip('"').strip("'") if raw else ""

        return sentence or None

    def _remember(self, question: str) -> None:
        """Append a resolved question to self._history, capped to the last HISTORY_TURNS_KEPT."""
        self._history.append(question)
        self._history = self._history[-HISTORY_TURNS_KEPT:]

    # -----------------------------------------------------------------
    # Phase 15 - AI Interpretation Layer. Everything below is additive:
    # it never touches the tag-level pipeline above, and only runs when
    # ai.interpretation_intent.classify_interpretation_intent() has
    # already identified a domain question (see ask_structured()).
    # -----------------------------------------------------------------

    def _render_interpretation_answer(
        self, context: dict[str, Any], intent: str, question: str, resolved_entities: list[dict[str, Any]],
        on_progress: Callable[[str], None] | None = None,
    ) -> AskResult:
        """
        Shared tail for every Phase 15 answer path: build the prompt,
        call the provider (never more than once per question), run the
        deterministic grounding guard, and fall back to the plain
        rendered context whenever the provider is unavailable/fails OR
        the guard finds an unsupported authoritative claim. The
        deterministic backend remains the source of truth either way.

        Phase 18 - `on_progress`, if given, is called with a small
        stage string ("generating_ai" right before the one provider
        call this method ever makes, "validating" right after it
        returns and before the grounding check) so a caller (e.g. the
        Ask AI page) can show an honest status. Entirely optional and
        UI-agnostic - this file never imports Streamlit. "validating"
        is only ever emitted here, never for the older ask()/
        IndustrialQueryEngine pipeline, because that pipeline never
        calls check_grounding() at all - emitting it there would be
        dishonest, not just imprecise.
        """
        # Phase 16.3 - deterministic UNAVAILABLE short-circuit. When the
        # question materially depends on current telemetry AND Data
        # Confidence is UNAVAILABLE for this equipment, skip the AI
        # provider call entirely (never fabricate a current-condition
        # assessment, and never spend a multi-minute LLM call on a
        # question this deterministic check already knows cannot be
        # reliably answered - item P). Naturally scoped to single-
        # equipment contexts only: build_comparison_ai_context() and
        # build_factory_ai_context() never attach a top-level
        # context["data_health"] (each COMPARISON entity carries its own,
        # nested, handled per-entity; FACTORY_SUMMARY never resolves a
        # single equipment's telemetry at all).
        data_health = context.get("data_health")
        requires_confidence = context.get("request", {}).get("requires_telemetry_confidence", False)

        if (
            data_health is not None
            and requires_confidence
            and data_health.get("confidence_status") == "UNAVAILABLE"
        ):
            reason = (
                "Data Confidence is UNAVAILABLE for this equipment, so a reliable "
                "current-condition assessment cannot be made. "
                + ("; ".join(data_health.get("reasons", [])) or "No deterministic reason was recorded.")
            )
            return AskResult(
                answer=build_deterministic_fallback(context, intent, reason),
                intent=intent,
                resolved_entities=resolved_entities,
                structured_context=context,
                provider=None,
                model=None,
                grounding_status="not_applicable",
                fallback_used=True,
            )

        provider_name = self.ai_provider.provider if self.ai_provider is not None else None
        model_name = self.ai_provider.model if self.ai_provider is not None else None

        if self.ai_provider is None:
            return AskResult(
                answer=build_deterministic_fallback(context, intent, self.ai_error),
                intent=intent,
                resolved_entities=resolved_entities,
                structured_context=context,
                provider=None,
                model=None,
                grounding_status="not_applicable",
                fallback_used=True,
            )

        prompt = build_interpretation_prompt(context, intent, question)

        if on_progress is not None:
            on_progress("generating_ai")

        try:
            answer = self.ai_provider.generate(prompt)
        except Exception as error:
            return AskResult(
                answer=build_deterministic_fallback(context, intent, str(error)),
                intent=intent,
                resolved_entities=resolved_entities,
                structured_context=context,
                provider=provider_name,
                model=model_name,
                grounding_status="not_applicable",
                fallback_used=True,
            )

        if on_progress is not None:
            on_progress("validating")

        grounding = check_grounding(answer, context, intent)

        if not grounding.checked:
            return AskResult(
                answer=answer,
                intent=intent,
                resolved_entities=resolved_entities,
                structured_context=context,
                provider=provider_name,
                model=model_name,
                grounding_status="not_checked",
                fallback_used=False,
            )

        if not grounding.grounded:
            # Do NOT pass the unsupported claim through silently - prefer
            # the deterministic fallback (the approved correction).
            reason = "an unsupported claim was detected and removed: " + "; ".join(grounding.violations)
            return AskResult(
                answer=build_deterministic_fallback(context, intent, reason),
                intent=intent,
                resolved_entities=resolved_entities,
                structured_context=context,
                provider=provider_name,
                model=model_name,
                grounding_status="violation_detected",
                fallback_used=True,
            )

        return AskResult(
            answer=answer,
            intent=intent,
            resolved_entities=resolved_entities,
            structured_context=context,
            provider=provider_name,
            model=model_name,
            grounding_status="grounded",
            fallback_used=False,
        )

    def _format_equipment_clarification(self, resolution) -> str:
        """Numbered menu for an ambiguous/no-match Phase 15 equipment
        resolution - deliberately separate from _format_candidate_menu()
        above (that one renders TAG candidates; this renders EQUIPMENT
        candidates), but the same plain, deterministic style."""
        if resolution.status == "clarification_required":
            header = resolution.message
        else:
            header = "I couldn't confidently match that to any configured equipment."

        lines = [header, ""]

        seen: set[str] = set()
        index = 0

        for candidate in resolution.candidates:
            if candidate.instance_key in seen:
                continue
            seen.add(candidate.instance_key)
            index += 1
            lines.append(f"{index}. {candidate.equipment_display_name} ({candidate.instance_key})")

            if index >= 5:
                break

        lines.extend(["", "Could you name the specific equipment (including plant, if it exists in more than one)?"])

        return "\n".join(lines)

    def _answer_equipment_interpretation(
        self, question: str, intent: str, context_equipment: str | None,
        on_progress: Callable[[str], None] | None = None,
    ) -> AskResult:
        resolution = self.query_engine.resolve_equipment(question)

        if resolution.status == "resolved":
            instance_key = resolution.instance_key
        elif resolution.had_equipment_terms:
            # The question named something equipment-like but it could
            # not be confidently/uniquely resolved - surface that
            # directly. Never silently fall back to a DIFFERENT
            # equipment from a prior turn just because this one is
            # ambiguous (Correction 1/3: an explicit new equipment
            # reference always overrides previous context, even when
            # that new reference itself needs clarifying).
            return AskResult(
                answer=self._format_equipment_clarification(resolution),
                intent=intent,
                resolved_entities=[c.to_dict() for c in resolution.candidates[:5]],
                grounding_status="not_applicable",
            )
        elif context_equipment:
            # A bare follow-up naming no equipment at all - reuse the
            # CALLER-supplied, session-scoped equipment context
            # (Correction 1: never a shared/global AskEngine attribute -
            # the caller owns this value, e.g. in st.session_state).
            instance_key = context_equipment
        else:
            return AskResult(
                answer=self._format_equipment_clarification(resolution),
                intent=intent,
                resolved_entities=[c.to_dict() for c in resolution.candidates[:5]],
                grounding_status="not_applicable",
            )

        context = build_equipment_ai_context(
            self.config_database_path, self.database.db_path, instance_key, intent, question=question,
        )

        return self._render_interpretation_answer(
            context, intent, question, resolved_entities=[{"instance_key": instance_key}],
            on_progress=on_progress,
        )

    def _answer_comparison_interpretation(
        self, question: str, on_progress: Callable[[str], None] | None = None,
    ) -> AskResult:
        """
        classify_interpretation_intent() routes ANY question containing
        a bare "compare"/"vs"/"versus" trigger word here, regardless of
        whether it's actually naming two DIFFERENT pieces of equipment
        ("compare AC01 vs AC02") or ONE piece of equipment across two
        TIME PERIODS ("compare today's pressure to 3 days ago") - the
        classifier only phrase-matches, it has no concept of periods.
        resolve_equipment_pair() below is built only for the former
        shape (found live: "how does P01 compressed air header
        pressure today compare to 3 days ago" produced two unrelated
        "which equipment did you mean" menus, because it was trying to
        find a SECOND equipment in text that only ever named one).

        extract_comparison_periods() only returns two non-empty
        periods when a real comparison trigger word AND two distinct
        time expressions are both present - a precise enough signal to
        safely hand a genuine period comparison to the existing,
        already-correct single-equipment/two-period pipeline (the
        legacy ask() -> _answer_comparison(), which computes both
        periods' real values deterministically and never lets the LLM
        recompute the verdict) instead of the equipment-pair one below.
        A question naming no time period at all ("compare AC01 vs
        AC02") is unaffected and still reaches resolve_equipment_pair()
        exactly as before.
        """
        period_a, _, period_b, _ = extract_comparison_periods(question)

        if period_a and period_b:
            answer = self.ask(question)
            return AskResult(
                answer=answer,
                intent="",
                resolved_entities=[],
                structured_context=None,
                provider=self.ai_provider.provider if self.ai_provider is not None else None,
                model=self.ai_provider.model if self.ai_provider is not None else None,
                grounding_status="not_applicable",
                fallback_used=self.ai_provider is None,
            )

        pair = self.query_engine.resolve_equipment_pair(question)

        if pair.status != "resolved":
            lines = [pair.message, ""]

            for label, entity in (("First equipment", pair.entity_a), ("Second equipment", pair.entity_b)):
                if entity is not None and entity.status != "resolved":
                    lines.append(f"{label}: {self._format_equipment_clarification(entity)}")

            return AskResult(
                answer="\n".join(lines),
                intent="COMPARISON",
                resolved_entities=[],
                grounding_status="not_applicable",
            )

        context = build_comparison_ai_context(
            self.config_database_path, self.database.db_path,
            pair.entity_a.instance_key, pair.entity_b.instance_key,
            question=question,
        )

        return self._render_interpretation_answer(
            context, "COMPARISON", question,
            resolved_entities=[
                {"instance_key": pair.entity_a.instance_key},
                {"instance_key": pair.entity_b.instance_key},
            ],
            on_progress=on_progress,
        )

    def _answer_factory_summary(
        self, question: str, on_progress: Callable[[str], None] | None = None,
    ) -> AskResult:
        context = build_factory_ai_context(
            self.config_database_path, self.database.db_path, plant_code=None, question=question,
        )

        return self._render_interpretation_answer(
            context, "FACTORY_SUMMARY", question, resolved_entities=[], on_progress=on_progress,
        )

    def ask_structured(
        self, question: str, context_equipment: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> AskResult:
        """
        Phase 15 entry point. `context_equipment`, when given, is the
        CALLER's own session-scoped "last discussed equipment"
        instance_key (e.g. read from st.session_state) - AskEngine
        itself never stores this across calls, so one browser session's
        follow-up context can never leak into another's (Correction 1).

        Falls back to the existing ask()/string pipeline UNCHANGED for
        every question that isn't a recognized Phase 15 domain question
        (Route A/C - see ai/interpretation_intent.py's module docstring).

        Phase 18 - `on_progress`, if given, is called with a small
        stage string as the request moves through it. Entirely
        optional: every existing caller (the CLI, tests) that doesn't
        pass it keeps working unchanged. "preparing_context" covers
        intent classification/entity resolution/context building here
        - all deterministic and, per this project's own measurements,
        well under a second for almost every question, so a caller
        polling on any reasonable interval will often never render it
        at all (expected, not a bug). The old ask()/IndustrialQueryEngine
        fallback path (`intent is None`) is treated as one opaque
        "generating_ai" span rather than instrumented internally - it
        has no exposed grounding step to report a "validating" stage
        for, and it is the path used by the slowest real questions
        (root_cause), so labeling its whole span "generating_ai" is
        honest for the dominant case without touching ask()'s internals.
        """
        if on_progress is not None:
            on_progress("preparing_context")

        intent = classify_interpretation_intent(question)

        if intent is None:
            if on_progress is not None:
                on_progress("generating_ai")

            answer = self.ask(question)
            return AskResult(
                answer=answer,
                intent="",
                resolved_entities=[],
                structured_context=None,
                provider=self.ai_provider.provider if self.ai_provider is not None else None,
                model=self.ai_provider.model if self.ai_provider is not None else None,
                grounding_status="not_applicable",
                fallback_used=self.ai_provider is None,
            )

        if intent == "FACTORY_SUMMARY":
            return self._answer_factory_summary(question, on_progress=on_progress)

        if intent == "COMPARISON":
            return self._answer_comparison_interpretation(question, on_progress=on_progress)

        return self._answer_equipment_interpretation(question, intent, context_equipment, on_progress=on_progress)

    def _maybe_escalate_to_llm(self, stripped_question: str) -> str | None:
        """
        Detect an explicit "that's not what I wanted, ask the real AI"
        follow-up (ESCALATION_PHRASES) and redo the previous turn
        through the LLM instead of matching it as a brand new
        question. Returns None (meaning: not an escalation, continue
        normal question handling) whenever there's no prior turn to
        escalate or the text doesn't contain one of the explicit
        phrases.

        The LLM is still only ever handed facts already computed
        deterministically (the same candidate list or the same
        deterministic answer text already shown) - escalating can't
        introduce an invented tag or value any more than the normal
        answer paths can.
        """
        normalized = stripped_question.lower()

        if not self._last_turn or not any(
            phrase in normalized for phrase in ESCALATION_PHRASES
        ):
            return None

        last_turn = self._last_turn

        if self.ai_provider is None:
            return (
                "AI phrasing is unavailable "
                f"({self.ai_error}) - there's no AI provider configured "
                "to escalate to right now."
            )

        if last_turn["kind"] == "llm_answer":
            return (
                "That answer was already generated by the AI - "
                "there's nothing further to escalate."
            )

        if last_turn["kind"] == "instant":
            return self._phrase_with_llm(
                last_turn["question"], last_turn["deterministic_text"], force=True
            )

        if last_turn["kind"] == "menu":
            return self._answer_menu_escalation(
                last_turn["question"], last_turn["candidates"], last_turn["intent"]
            )

        return None

    def _answer_menu_escalation(self, question: str, candidates: list, intent: str) -> str:
        """
        "None of these" follow-up to an ambiguity menu - ask the LLM to
        synthesize/compare across the SAME candidate tags already
        offered, using each one's real current data, rather than
        re-running the deterministic matcher (which already failed to
        pick one) or letting the LLM answer free-form. Mirrors
        _answer_for_tag()'s single-tag prompt assembly, just across
        the whole candidate list at once.
        """
        self._last_turn = {"kind": "llm_answer"}

        summaries = []
        rule_results = []

        for candidate in candidates:
            tag = candidate.tag
            history = self.database.get_history(
                tag=tag.tag_name,
                hours=self._history_hours(intent),
                limit=DEFAULT_HISTORY_LIMIT,
            )

            if not history:
                continue

            summary = analyse_tag(tag.tag_name, history)
            summaries.append(summary)
            rule_results.append(self.rule_engine.evaluate_summary(summary))

        if not summaries:
            return (
                "None of the tags from that menu have any historian "
                "data recorded yet, so there's nothing for the AI to "
                "work from either."
            )

        machine_context = format_machine_context(summaries, intent or "current_data")
        rule_context = format_rule_results(rule_results, include_normal=True)

        route = {
            "equipment": "one of several possible matches",
            "intent": intent or "current_data",
        }

        prompt = build_prompt(
            question=question,
            machine_context=machine_context,
            rule_context=rule_context,
            route=route,
        )

        if self.ai_provider is None:
            return self._deterministic_answer(machine_context, rule_context, self.ai_error)

        try:
            return self.ai_provider.generate(prompt)
        except Exception as error:
            return self._deterministic_answer(machine_context, rule_context, str(error))

    def ask(self, question: str) -> str:
        """Answer one operator question and return the final text."""
        stripped_question = question.strip()

        escalation = self._maybe_escalate_to_llm(stripped_question)

        if escalation is not None:
            return escalation

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
        self._last_turn = {
            "kind": "menu",
            "question": result.question,
            "intent": result.intent,
            "candidates": candidates,
        }

        lead_sentence = self._phrase_clarifying_question(question, candidates, result.status)

        return _format_candidate_menu(candidates, result.status, lead_sentence=lead_sentence)


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
