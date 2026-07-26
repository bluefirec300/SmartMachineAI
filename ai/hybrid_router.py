from typing import Any

from ai.router import (
    FACTORY_WIDE_WORDS,
    contains_phrase,
    detect_equipment,
    detect_intent,
    detect_measurements,
    normalize_text,
    select_tags,
)
from ai.semantic_router import SemanticRouter


class HybridRouter:
    """
    Uses deterministic routing first and the semantic router only
    when the rule-based equipment result appears ambiguous.
    """

    def __init__(
        self,
        semantic_router: SemanticRouter,
    ):
        self.semantic_router = semantic_router

    @staticmethod
    def _explicit_factory_question(
        question: str,
    ) -> bool:
        text = normalize_text(question)

        return any(
            contains_phrase(text, phrase)
            for phrase in FACTORY_WIDE_WORDS
        )

    @staticmethod
    def _history_limit(
        intent: str,
    ) -> int:
        if intent == "current":
            return 1

        if intent == "trend":
            return 20

        return 10

    def route(
        self,
        question: str,
    ) -> dict[str, Any]:
        rule_equipment = detect_equipment(question)
        rule_intent = detect_intent(question)
        rule_measurements = detect_measurements(question)

        use_rule_result = (
            rule_equipment != "factory"
            or self._explicit_factory_question(question)
        )

        if use_rule_result:
            tags = select_tags(
                question=question,
                equipment=rule_equipment,
                intent=rule_intent,
                measurements=rule_measurements,
            )

            return {
                "equipment": rule_equipment,
                "measurements": sorted(rule_measurements),
                "intent": rule_intent,
                "tags": tags,
                "history_limit": self._history_limit(
                    rule_intent
                ),
                "route_source": "rules",
            }

        semantic_result = self.semantic_router.interpret(
            question
        )

        if semantic_result is None:
            tags = select_tags(
                question=question,
                equipment=rule_equipment,
                intent=rule_intent,
                measurements=rule_measurements,
            )

            return {
                "equipment": rule_equipment,
                "measurements": sorted(rule_measurements),
                "intent": rule_intent,
                "tags": tags,
                "history_limit": self._history_limit(
                    rule_intent
                ),
                "route_source": "rules_fallback",
            }

        semantic_measurements = set(
            semantic_result["measurements"]
        )
        semantic_equipment = semantic_result["equipment"]
        semantic_intent = semantic_result["intent"]

        tags = select_tags(
            question=question,
            equipment=semantic_equipment,
            intent=semantic_intent,
            measurements=semantic_measurements,
        )

        return {
            "equipment": semantic_equipment,
            "measurements": sorted(
                semantic_measurements
            ),
            "intent": semantic_intent,
            "tags": tags,
            "history_limit": self._history_limit(
                semantic_intent
            ),
            "route_source": "semantic",
        }
