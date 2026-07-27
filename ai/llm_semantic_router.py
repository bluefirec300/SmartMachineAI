from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from ai.llm_route_adapter import (
    AIRoute,
    LLMRouteAdapter,
)
from ai.semantic_tag_resolver import (
    SemanticResolution,
    SemanticTagMatch,
    SemanticTagResolver,
)


@dataclass(frozen=True)
class ResolvedAIRoute:
    """
    Final route produced by combining:

    1. LLM language understanding
    2. Semantic tag matching
    3. Equipment validation

    Only real tags loaded from config.db can be selected.
    """

    question: str
    query_type: str
    equipment: str
    intent: str
    measurements: list[str]
    location: str
    condition: str
    time_expression: str
    tag_hint: str

    tags: list[str]
    primary_tag: str | None

    history_limit: int
    parser_confidence: float
    semantic_similarity: float
    combined_confidence: float

    resolved: bool
    needs_clarification: bool
    route_source: str
    message: str

    candidates: list[dict[str, Any]]
    parser_time_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_legacy_route(self) -> dict[str, Any]:
        """
        Route structure suitable for the existing SmartMachineAI
        pipeline.
        """

        return {
            "equipment": self.equipment,
            "intent": self.intent,
            "measurements": self.measurements,
            "tags": self.tags or None,
            "primary_tag": self.primary_tag,
            "history_limit": self.history_limit,
            "query_type": self.query_type,
            "location": self.location,
            "condition": self.condition,
            "time_expression": self.time_expression,
            "tag_hint": self.tag_hint,
            "confidence": self.combined_confidence,
            "resolved": self.resolved,
            "needs_clarification": self.needs_clarification,
            "route_source": self.route_source,
            "message": self.message,
        }


class LLMSemanticRouter:
    """
    Combines the local Qwen question parser with the existing
    SemanticTagResolver.

    The LLM understands language but never selects an arbitrary PLC tag.

    SemanticTagResolver can only select tags that exist in config.db.

    Equipment validation prevents a tag belonging to another machine
    from being accepted accidentally.
    """

    def __init__(
        self,
        route_adapter: LLMRouteAdapter | None = None,
        semantic_resolver: SemanticTagResolver | None = None,
        semantic_top_k: int = 10,
        minimum_parser_confidence: float = 0.55,
        minimum_semantic_similarity: float = 0.60,
        clarification_score_gap: float = 0.04,
    ) -> None:
        self.route_adapter = (
            route_adapter
            or LLMRouteAdapter()
        )

        self.semantic_resolver = (
            semantic_resolver
            or SemanticTagResolver()
        )

        self.semantic_top_k = max(
            2,
            semantic_top_k,
        )

        self.minimum_parser_confidence = (
            minimum_parser_confidence
        )

        self.minimum_semantic_similarity = (
            minimum_semantic_similarity
        )

        self.clarification_score_gap = (
            clarification_score_gap
        )

    @staticmethod
    def _normalize(value: str) -> str:
        text = str(
            value or ""
        ).strip().lower()

        for character in (
            " ",
            "-",
            "_",
            "/",
            "\\",
            ".",
            ",",
            "(",
            ")",
        ):
            text = text.replace(
                character,
                "",
            )

        return text

    def _candidate_matches_equipment(
        self,
        candidate: SemanticTagMatch,
        equipment: str,
    ) -> bool:
        """
        Confirm that the semantic candidate is connected to the
        equipment identified by the LLM.

        Equipment validation is skipped when the parser could not
        identify specific equipment.
        """

        normalized_equipment = self._normalize(
            equipment
        )

        if not normalized_equipment:
            return True

        if normalized_equipment in {
            "factory",
            "system",
            "plant",
            "machine",
        }:
            return True

        equipment_values = [
            *candidate.tag.equipment_names,
            *candidate.tag.equipment_display_names,
        ]

        if not equipment_values:
            return False

        for equipment_value in equipment_values:
            normalized_candidate = self._normalize(
                equipment_value
            )

            if not normalized_candidate:
                continue

            if (
                normalized_equipment
                == normalized_candidate
            ):
                return True

            if (
                normalized_equipment
                in normalized_candidate
            ):
                return True

            if (
                normalized_candidate
                in normalized_equipment
            ):
                return True

        return False

    def _equipment_candidates(
        self,
        semantic_result: SemanticResolution,
        equipment: str,
    ) -> list[SemanticTagMatch]:
        """
        Return semantic candidates associated with the selected
        equipment.
        """

        matches: list[SemanticTagMatch] = []

        for candidate in semantic_result.candidates:
            if self._candidate_matches_equipment(
                candidate,
                equipment,
            ):
                matches.append(
                    candidate
                )

        return matches

    @staticmethod
    def _candidate_to_dict(
        candidate: SemanticTagMatch,
        equipment_match: bool,
    ) -> dict[str, Any]:
        equipment_names = list(
            candidate.tag.equipment_names
        )

        equipment_display_names = list(
            candidate.tag.equipment_display_names
        )

        return {
            "rank": candidate.rank,
            "tag_name": candidate.tag.tag_name,
            "description": candidate.tag.description,
            "unit": candidate.tag.unit,
            "address": candidate.tag.address,
            "driver": candidate.tag.driver,
            "similarity": round(
                candidate.similarity,
                4,
            ),
            "confidence_percent": (
                candidate.confidence_percent
            ),
            "equipment_names": equipment_names,
            "equipment_display_names": (
                equipment_display_names
            ),
            "equipment_match": equipment_match,
        }

    @staticmethod
    def _combined_confidence(
        parser_confidence: float,
        semantic_similarity: float,
    ) -> float:
        """
        Combine language-understanding confidence and semantic-match
        confidence.

        Parser confidence receives 40% weight.
        Semantic similarity receives 60% weight.
        """

        result = (
            parser_confidence * 0.40
            + semantic_similarity * 0.60
        )

        return round(
            max(
                0.0,
                min(
                    1.0,
                    result,
                ),
            ),
            3,
        )

    def _select_candidate(
        self,
        route: AIRoute,
        semantic_result: SemanticResolution,
    ) -> tuple[
        SemanticTagMatch | None,
        bool,
        str,
    ]:
        """
        Select a final tag after applying equipment validation.

        Returns:

        selected_candidate
        needs_clarification
        message
        """

        equipment_candidates = (
            self._equipment_candidates(
                semantic_result,
                route.equipment,
            )
        )

        if not equipment_candidates:
            return (
                None,
                True,
                (
                    "Semantic candidates were found, but none were "
                    f"assigned to equipment '{route.equipment}'."
                ),
            )

        best = equipment_candidates[0]

        second = (
            equipment_candidates[1]
            if len(equipment_candidates) > 1
            else None
        )

        score_gap = (
            best.similarity
            - second.similarity
            if second is not None
            else best.similarity
        )

        if (
            best.similarity
            < self.minimum_semantic_similarity
        ):
            return (
                None,
                True,
                (
                    f"Best tag candidate was "
                    f"{best.tag.tag_name}, but its semantic "
                    f"similarity was only "
                    f"{best.confidence_percent}%."
                ),
            )

        if (
            second is not None
            and score_gap
            < self.clarification_score_gap
        ):
            return (
                None,
                True,
                (
                    "The two best tag candidates were too similar: "
                    f"{best.tag.tag_name} "
                    f"({best.confidence_percent}%) and "
                    f"{second.tag.tag_name} "
                    f"({second.confidence_percent}%)."
                ),
            )

        return (
            best,
            False,
            (
                f"Resolved to configured tag "
                f"{best.tag.tag_name} with "
                f"{best.confidence_percent}% semantic similarity."
            ),
        )

    def resolve_route(
        self,
        route: AIRoute,
    ) -> ResolvedAIRoute:
        """
        Resolve an existing AI route to a real configured PLC tag.
        """

        search_text = (
            route.tag_hint.strip()
            or route.question.strip()
        )

        semantic_result = (
            self.semantic_resolver.resolve(
                search_text,
                top_k=self.semantic_top_k,
            )
        )

        selected, needs_clarification, message = (
            self._select_candidate(
                route,
                semantic_result,
            )
        )

        parser_confidence_low = (
            route.confidence
            < self.minimum_parser_confidence
        )

        if parser_confidence_low:
            selected = None
            needs_clarification = True

            message = (
                "The AI parser confidence was too low "
                f"({route.confidence:.1%}). "
                "The question needs clarification."
            )

        primary_tag = (
            selected.tag.tag_name
            if selected is not None
            else None
        )

        tags = (
            [primary_tag]
            if primary_tag
            else []
        )

        semantic_similarity = (
            selected.similarity
            if selected is not None
            else (
                semantic_result.candidates[0].similarity
                if semantic_result.candidates
                else 0.0
            )
        )

        combined_confidence = (
            self._combined_confidence(
                route.confidence,
                semantic_similarity,
            )
        )

        candidate_output: list[
            dict[str, Any]
        ] = []

        for candidate in semantic_result.candidates:
            equipment_match = (
                self._candidate_matches_equipment(
                    candidate,
                    route.equipment,
                )
            )

            candidate_output.append(
                self._candidate_to_dict(
                    candidate,
                    equipment_match,
                )
            )

        resolved = (
            selected is not None
            and not needs_clarification
        )

        return ResolvedAIRoute(
            question=route.question,
            query_type=route.query_type,
            equipment=route.equipment,
            intent=route.intent,
            measurements=route.measurements,
            location=route.location,
            condition=route.condition,
            time_expression=route.time_expression,
            tag_hint=route.tag_hint,
            tags=tags,
            primary_tag=primary_tag,
            history_limit=route.history_limit,
            parser_confidence=route.confidence,
            semantic_similarity=round(
                semantic_similarity,
                4,
            ),
            combined_confidence=combined_confidence,
            resolved=resolved,
            needs_clarification=needs_clarification,
            route_source="llm_semantic_router",
            message=message,
            candidates=candidate_output,
            parser_time_seconds=(
                route.parser_time_seconds
            ),
        )

    def route(
        self,
        question: str,
    ) -> ResolvedAIRoute:
        """
        Complete process:

        Natural-language question
            -> LLM route
            -> semantic tag resolution
            -> equipment validation
            -> final route
        """

        ai_route = self.route_adapter.route(
            question
        )

        return self.resolve_route(
            ai_route
        )

    def warm_up(self) -> float:
        """
        Load the Qwen parser model into memory.
        """

        return self.route_adapter.warm_up()


def format_resolved_route(
    result: ResolvedAIRoute,
) -> str:
    measurements = (
        ", ".join(
            result.measurements
        )
        if result.measurements
        else "None"
    )

    tags = (
        ", ".join(
            result.tags
        )
        if result.tags
        else "Not resolved"
    )

    lines = [
        f"Question: {result.question}",
        f"Query type: {result.query_type}",
        f"Equipment: {result.equipment}",
        f"Legacy intent: {result.intent}",
        f"Measurements: {measurements}",
        f"Location: {result.location or 'None'}",
        f"Condition: {result.condition or 'None'}",
        (
            "Time expression: "
            f"{result.time_expression or 'None'}"
        ),
        f"Tag hint: {result.tag_hint or 'None'}",
        f"Primary tag: {result.primary_tag or 'None'}",
        f"Tags: {tags}",
        f"History limit: {result.history_limit}",
        (
            "Parser confidence: "
            f"{result.parser_confidence:.1%}"
        ),
        (
            "Semantic similarity: "
            f"{result.semantic_similarity:.1%}"
        ),
        (
            "Combined confidence: "
            f"{result.combined_confidence:.1%}"
        ),
        f"Resolved: {result.resolved}",
        (
            "Needs clarification: "
            f"{result.needs_clarification}"
        ),
        f"Message: {result.message}",
        f"Route source: {result.route_source}",
        (
            "Parser time: "
            f"{result.parser_time_seconds:.3f} seconds"
        ),
        "",
        "Candidates:",
    ]

    if not result.candidates:
        lines.append(
            "- No candidates"
        )

        return "\n".join(
            lines
        )

    for candidate in result.candidates:
        equipment_marker = (
            "equipment match"
            if candidate["equipment_match"]
            else "different equipment"
        )

        lines.append(
            (
                f"{candidate['rank']}. "
                f"{candidate['tag_name']} "
                f"- {candidate['confidence_percent']}% "
                f"({equipment_marker})"
            )
        )

        if candidate["description"]:
            lines.append(
                "   Description: "
                f"{candidate['description']}"
            )

        equipment_names = (
            candidate["equipment_display_names"]
            or candidate["equipment_names"]
        )

        if equipment_names:
            lines.append(
                "   Equipment: "
                + ", ".join(
                    equipment_names
                )
            )

        if candidate["unit"]:
            lines.append(
                f"   Unit: {candidate['unit']}"
            )

    return "\n".join(
        lines
    )


def main() -> None:
    print(
        "SmartMachineAI LLM Semantic Router"
    )
    print(
        "Building semantic tag index..."
    )

    try:
        router = LLMSemanticRouter()

    except Exception as exc:
        print(
            f"Startup error: {exc}"
        )
        return

    print(
        "Semantic tag index ready."
    )
    print(
        "Loading Qwen parser..."
    )

    try:
        warm_up_seconds = router.warm_up()

        print(
            f"Qwen ready in "
            f"{warm_up_seconds:.3f} seconds."
        )

    except Exception as exc:
        print(
            f"Warm-up error: {exc}"
        )
        return

    print(
        "Type 'exit' to stop."
    )

    while True:
        try:
            question = input(
                "\nQuestion: "
            ).strip()

        except (
            EOFError,
            KeyboardInterrupt,
        ):
            print()
            break

        if question.lower() in {
            "exit",
            "quit",
        }:
            break

        if not question:
            continue

        try:
            result = router.route(
                question
            )

            print()
            print(
                format_resolved_route(
                    result
                )
            )

            print()
            print(
                "Legacy route:"
            )

            print(
                json.dumps(
                    result.to_legacy_route(),
                    indent=2,
                )
            )

        except Exception as exc:
            print(
                f"Route error: {exc}"
            )


if __name__ == "__main__":
    main()
