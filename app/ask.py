from __future__ import annotations

import argparse
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

    def ask(self, question: str) -> str:
        """Answer one operator question and return the final text."""
        result = self.query_engine.query(question)

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
