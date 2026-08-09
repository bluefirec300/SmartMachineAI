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
from database.database import DatabaseManager
from engine.industrial_query_engine import IndustrialQueryEngine


PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"
MACHINE_DATABASE_PATH = PROJECT_ROOT / "database" / "machine_data.db"

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
    Convert a time_expression (from ConceptExtractor.TIMES) into an
    actual (start, end, label) window for querying machine_events.

    Uses the same space-separated timestamp format as database.py -
    machine_events.event_time is written in that format, and a
    mismatched separator would silently break the comparison the
    same way it did for plc_data earlier this session.
    """
    now = datetime.now()

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


def _equipment_type_key(equipment_name):
    """
    Group numbered equipment instances by type.

    e.g. "p01_air_compressor_ac01" -> "p01_air_compressor", so "Air
    Compressor 1/2/3" collapse into one summary line instead of
    three, the same way an operator would think about it.
    """
    match = re.match(r"^(p\d+_.+)_[a-z]+\d+$", equipment_name)
    return match.group(1) if match else equipment_name


def _format_available_equipment(rows):
    """
    Render a deterministic "what can I ask about" summary.

    Not routed through the LLM - this is a structured listing
    straight from configuration, not something that benefits from
    (or should risk) paraphrasing.
    """
    if not rows:
        return "No equipment is currently configured/enabled."

    groups: dict[str, list] = {}

    for row in rows:
        key = _equipment_type_key(row["name"])
        groups.setdefault(key, []).append(row)

    total_equipment = len(rows)
    total_tags = sum(row["tag_count"] for row in rows)

    lines = [
        f"This system currently monitors {total_equipment} pieces of "
        f"equipment ({total_tags} tags total):",
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

    def _list_available_equipment(self) -> str:
        connection = sqlite3.connect(self.config_database_path)
        connection.row_factory = sqlite3.Row

        try:
            rows = connection.execute(
                """
                SELECT e.name, e.display_name, COUNT(t.id) AS tag_count
                FROM equipment e
                JOIN tags t ON t.equipment_id = e.id AND t.enabled = 1
                GROUP BY e.id
                HAVING tag_count > 0
                ORDER BY e.display_name
                """
            ).fetchall()
        finally:
            connection.close()

        return _format_available_equipment(rows)

    def _factory_wide_timeline(self, time_expression: str) -> str:
        start, end, label = _factory_timeline_range(time_expression)

        events = self.event_store.get_recent_events(
            limit=FACTORY_WIDE_EVENTS_LIMIT,
            start_time=start,
            end_time=end,
        )

        return _format_factory_wide_events(events, label)

    def ask(self, question: str) -> str:
        """Answer one operator question and return the final text."""
        result = self.query_engine.query(question)

        if result.intent == "discovery":
            return self._list_available_equipment()

        if result.intent == "timeline" and (
            result.status != "resolved" or not result.selected_tag
        ):
            return self._factory_wide_timeline(result.time_expression)

        if result.status != "resolved" or not result.selected_tag:
            return result.message

        history = self.database.get_history(
            tag=result.selected_tag,
            hours=self._history_hours(result.intent),
            limit=DEFAULT_HISTORY_LIMIT,
        )

        if not history:
            return (
                f"'{result.selected_tag}' was resolved, but no "
                "historian data has been recorded for it yet."
            )

        summary = analyse_tag(result.selected_tag, history)
        rule_result = self.rule_engine.evaluate_summary(summary)

        machine_context = format_machine_context(
            [summary],
            result.intent,
        )

        rule_context = format_rule_results(
            [rule_result],
            include_normal=True,
        )

        if result.intent == "root_cause":
            root_cause_result = (
                self.root_cause_engine.analyze_latest_alarm(
                    equipment=result.equipment or None,
                    tag=result.selected_tag or None,
                )
            )

            root_cause_result.evidence = _condense_evidence(
                root_cause_result.evidence
            )

            machine_context = (
                f"{machine_context}\n\n"
                f"{format_root_cause_result(root_cause_result)}"
            )

        if result.intent == "threshold":
            configured_thresholds = self.rule_engine.rules.get(
                result.selected_tag
            )

            rule_context = (
                f"{rule_context}\n\n"
                f"{_format_configured_thresholds(result.selected_tag, configured_thresholds, summary['unit'])}"
            )

        if result.intent == "timeline":
            recent_events = self.event_store.get_recent_events(
                tag=result.selected_tag,
                limit=RECENT_EVENTS_LIMIT,
            )

            machine_context = (
                f"{machine_context}\n\n"
                f"{_format_event_history(result.selected_tag, recent_events)}"
            )

        route = {
            "equipment": result.equipment or "Unknown",
            "intent": result.intent,
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
