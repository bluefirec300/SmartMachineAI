from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ai.semantic_tag_resolver import (
    SemanticTagResolver,
    TagKnowledge,
)


@dataclass(frozen=True)
class KnowledgeTag:
    """
    LLM-safe description of one real configured tag.

    This object contains descriptive information only. The LLM may
    understand these values, but the final tag must still be validated
    against config.db.
    """

    tag_name: str
    description: str
    unit: str
    data_type: str
    driver: str
    address: str
    equipment: tuple[str, ...]
    concepts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EquipmentKnowledge:
    """
    All configured tags associated with one equipment group.
    """

    equipment_name: str
    alternative_names: set[str] = field(
        default_factory=set
    )
    descriptions: set[str] = field(
        default_factory=set
    )
    tags: list[KnowledgeTag] = field(
        default_factory=list
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "equipment_name": self.equipment_name,
            "alternative_names": sorted(
                self.alternative_names
            ),
            "descriptions": sorted(
                self.descriptions
            ),
            "tags": [
                tag.to_dict()
                for tag in self.tags
            ],
        }


@dataclass(frozen=True)
class FactoryKnowledge:
    """
    Complete factory knowledge generated from config.db.
    """

    equipment: tuple[EquipmentKnowledge, ...]
    unassigned_tags: tuple[KnowledgeTag, ...]
    available_tag_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "equipment": [
                item.to_dict()
                for item in self.equipment
            ],
            "unassigned_tags": [
                tag.to_dict()
                for tag in self.unassigned_tags
            ],
            "available_tag_names": list(
                self.available_tag_names
            ),
        }


class FactoryKnowledgeEngine:
    """
    Builds an LLM-readable description of the configured factory.

    Source of truth:
        config.db

    The knowledge engine does not create or rename tags. It only
    describes enabled tags already loaded by SemanticTagResolver.
    """

    GENERIC_WORDS = {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "current",
        "data",
        "for",
        "from",
        "in",
        "is",
        "latest",
        "machine",
        "measurement",
        "of",
        "on",
        "or",
        "reading",
        "status",
        "system",
        "tag",
        "the",
        "to",
        "value",
        "with",
    }

    UNIT_CONCEPTS = {
        "degc": "temperature",
        "°c": "temperature",
        "celsius": "temperature",
        "bar": "pressure",
        "kpa": "pressure",
        "mpa": "pressure",
        "psi": "pressure",
        "%": "percentage",
        "percent": "percentage",
        "a": "current",
        "amp": "current",
        "amps": "current",
        "ampere": "current",
        "v": "voltage",
        "volt": "voltage",
        "volts": "voltage",
        "hz": "frequency",
        "rpm": "speed",
        "l/min": "flow",
        "lpm": "flow",
        "m3/h": "flow",
        "m³/h": "flow",
        "kw": "power",
        "kwh": "energy",
    }

    def __init__(
        self,
        semantic_resolver: SemanticTagResolver | None = None,
    ) -> None:
        self.semantic_resolver = (
            semantic_resolver
            or SemanticTagResolver()
        )

        self._knowledge: FactoryKnowledge | None = None

    @staticmethod
    def _clean_text(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        return str(value).strip()

    @staticmethod
    def _split_camel_case(
        value: str,
    ) -> str:
        text = re.sub(
            r"([a-z0-9])([A-Z])",
            r"\1 \2",
            value,
        )

        text = re.sub(
            r"([A-Z]+)([A-Z][a-z])",
            r"\1 \2",
            text,
        )

        return text

    @classmethod
    def _tokenize(
        cls,
        value: str,
    ) -> list[str]:
        clean_value = cls._split_camel_case(
            cls._clean_text(value)
        ).lower()

        tokens = re.findall(
            r"[a-z0-9%°³]+(?:/[a-z0-9]+)?",
            clean_value,
        )

        return [
            token
            for token in tokens
            if token
            and token not in cls.GENERIC_WORDS
        ]

    @staticmethod
    def _append_unique(
        output: list[str],
        value: str,
    ) -> None:
        clean_value = value.strip()

        if clean_value and clean_value not in output:
            output.append(
                clean_value
            )

    def _extract_concepts(
        self,
        tag: TagKnowledge,
    ) -> tuple[str, ...]:
        """
        Extract descriptive concepts from existing tag metadata.

        This is not a manually maintained alias table. Concepts are
        generated from the configured tag name, description and unit.
        """

        concepts: list[str] = []

        source_values = [
            tag.tag_name,
            tag.description,
        ]

        for source_value in source_values:
            for token in self._tokenize(
                source_value
            ):
                self._append_unique(
                    concepts,
                    token,
                )

        normalized_unit = (
            tag.unit.strip().lower()
        )

        unit_concept = self.UNIT_CONCEPTS.get(
            normalized_unit
        )

        if unit_concept:
            self._append_unique(
                concepts,
                unit_concept,
            )

        description_text = (
            tag.description.lower()
        )

        known_concepts = (
            "alarm",
            "warning",
            "running",
            "stopped",
            "temperature",
            "pressure",
            "current",
            "voltage",
            "frequency",
            "speed",
            "level",
            "flow",
            "humidity",
            "door",
            "position",
            "count",
            "weight",
            "power",
            "energy",
            "setpoint",
            "threshold",
            "high",
            "low",
            "supply",
            "return",
            "discharge",
            "suction",
            "inlet",
            "outlet",
        )

        combined_text = " ".join(
            [
                self._split_camel_case(
                    tag.tag_name
                ).lower(),
                description_text,
            ]
        )

        for concept in known_concepts:
            if re.search(
                rf"\b{re.escape(concept)}\b",
                combined_text,
            ):
                self._append_unique(
                    concepts,
                    concept,
                )

        return tuple(
            concepts
        )

    def _equipment_values(
        self,
        tag: TagKnowledge,
    ) -> tuple[str, ...]:
        values: list[str] = []

        for value in (
            *tag.equipment_display_names,
            *tag.equipment_names,
        ):
            self._append_unique(
                values,
                value,
            )

        return tuple(
            values
        )

    def _make_knowledge_tag(
        self,
        tag: TagKnowledge,
    ) -> KnowledgeTag:
        return KnowledgeTag(
            tag_name=tag.tag_name,
            description=tag.description,
            unit=tag.unit,
            data_type=tag.data_type,
            driver=tag.driver,
            address=tag.address,
            equipment=self._equipment_values(
                tag
            ),
            concepts=self._extract_concepts(
                tag
            ),
        )

    def rebuild(self) -> FactoryKnowledge:
        """
        Rebuild knowledge from the current enabled tags.
        """

        grouped: dict[
            str,
            EquipmentKnowledge,
        ] = {}

        unassigned_tags: list[
            KnowledgeTag
        ] = []

        all_tag_names: list[str] = []

        for tag in self.semantic_resolver.list_tags():
            knowledge_tag = (
                self._make_knowledge_tag(
                    tag
                )
            )

            self._append_unique(
                all_tag_names,
                tag.tag_name,
            )

            display_names = list(
                tag.equipment_display_names
            )

            internal_names = list(
                tag.equipment_names
            )

            equipment_descriptions = list(
                tag.equipment_descriptions
            )

            primary_name = (
                display_names[0]
                if display_names
                else (
                    internal_names[0]
                    if internal_names
                    else ""
                )
            )

            if not primary_name:
                unassigned_tags.append(
                    knowledge_tag
                )
                continue

            normalized_key = (
                primary_name.strip().lower()
            )

            if normalized_key not in grouped:
                grouped[normalized_key] = (
                    EquipmentKnowledge(
                        equipment_name=primary_name,
                    )
                )

            equipment_item = grouped[
                normalized_key
            ]

            equipment_item.alternative_names.update(
                name
                for name in (
                    *display_names,
                    *internal_names,
                )
                if name.strip()
                and name.strip() != primary_name
            )

            equipment_item.descriptions.update(
                description
                for description in equipment_descriptions
                if description.strip()
            )

            equipment_item.tags.append(
                knowledge_tag
            )

        equipment_items = sorted(
            grouped.values(),
            key=lambda item: (
                item.equipment_name.lower()
            ),
        )

        for equipment_item in equipment_items:
            equipment_item.tags.sort(
                key=lambda item: (
                    item.tag_name.lower()
                )
            )

        self._knowledge = FactoryKnowledge(
            equipment=tuple(
                equipment_items
            ),
            unassigned_tags=tuple(
                sorted(
                    unassigned_tags,
                    key=lambda item: (
                        item.tag_name.lower()
                    ),
                )
            ),
            available_tag_names=tuple(
                sorted(
                    all_tag_names,
                    key=str.lower,
                )
            ),
        )

        return self._knowledge

    def get_knowledge(
        self,
    ) -> FactoryKnowledge:
        if self._knowledge is None:
            return self.rebuild()

        return self._knowledge

    def build_llm_context(
        self,
        include_addresses: bool = False,
        include_drivers: bool = False,
    ) -> str:
        """
        Build compact factory context for the question parser.

        PLC addresses and communication drivers are excluded by default
        because they are not normally required for language parsing.
        """

        knowledge = self.get_knowledge()

        lines = [
            "FACTORY CONFIGURATION",
            "",
            (
                "Use only the equipment, measurements, states and "
                "tags listed below."
            ),
            (
                "Do not invent equipment names, measurements, "
                "locations, conditions or tags."
            ),
            (
                "When information is not stated by the user, return "
                "an empty value."
            ),
            "",
        ]

        for equipment in knowledge.equipment:
            lines.append(
                f"EQUIPMENT: {equipment.equipment_name}"
            )

            if equipment.alternative_names:
                lines.append(
                    "Alternative configured names: "
                    + ", ".join(
                        sorted(
                            equipment.alternative_names
                        )
                    )
                )

            if equipment.descriptions:
                lines.append(
                    "Description: "
                    + "; ".join(
                        sorted(
                            equipment.descriptions
                        )
                    )
                )

            lines.append(
                "Available configured tags:"
            )

            for tag in equipment.tags:
                tag_parts = [
                    f"- {tag.tag_name}",
                ]

                if tag.description:
                    tag_parts.append(
                        f"description={tag.description}"
                    )

                if tag.concepts:
                    tag_parts.append(
                        "concepts="
                        + ", ".join(
                            tag.concepts
                        )
                    )

                if tag.unit:
                    tag_parts.append(
                        f"unit={tag.unit}"
                    )

                if tag.data_type:
                    tag_parts.append(
                        f"data_type={tag.data_type}"
                    )

                if include_addresses and tag.address:
                    tag_parts.append(
                        f"address={tag.address}"
                    )

                if include_drivers and tag.driver:
                    tag_parts.append(
                        f"driver={tag.driver}"
                    )

                lines.append(
                    " | ".join(
                        tag_parts
                    )
                )

            lines.append(
                ""
            )

        if knowledge.unassigned_tags:
            lines.append(
                "UNASSIGNED CONFIGURED TAGS"
            )

            for tag in knowledge.unassigned_tags:
                tag_parts = [
                    f"- {tag.tag_name}",
                ]

                if tag.description:
                    tag_parts.append(
                        f"description={tag.description}"
                    )

                if tag.concepts:
                    tag_parts.append(
                        "concepts="
                        + ", ".join(
                            tag.concepts
                        )
                    )

                if tag.unit:
                    tag_parts.append(
                        f"unit={tag.unit}"
                    )

                lines.append(
                    " | ".join(
                        tag_parts
                    )
                )

        return "\n".join(
            lines
        ).strip()

    def find_equipment(
        self,
        name: str,
    ) -> EquipmentKnowledge | None:
        """
        Find configured equipment by its configured name.

        This method performs normalized comparison only. It does not
        use manually maintained aliases.
        """

        normalized_target = self._normalize_name(
            name
        )

        if not normalized_target:
            return None

        for equipment in self.get_knowledge().equipment:
            candidate_names = [
                equipment.equipment_name,
                *equipment.alternative_names,
            ]

            for candidate_name in candidate_names:
                normalized_candidate = (
                    self._normalize_name(
                        candidate_name
                    )
                )

                if normalized_target == normalized_candidate:
                    return equipment

                if (
                    normalized_target in normalized_candidate
                    or normalized_candidate
                    in normalized_target
                ):
                    return equipment

        return None

    @staticmethod
    def _normalize_name(
        value: str,
    ) -> str:
        return re.sub(
            r"[^a-z0-9]+",
            "",
            str(
                value or ""
            ).lower(),
        )

    def get_tags_for_equipment(
        self,
        equipment_name: str,
    ) -> list[KnowledgeTag]:
        equipment = self.find_equipment(
            equipment_name
        )

        if equipment is None:
            return []

        return list(
            equipment.tags
        )

    def find_tags_by_concepts(
        self,
        concepts: list[str],
        equipment_name: str = "",
    ) -> list[KnowledgeTag]:
        """
        Deterministically narrow tags using concepts extracted by the
        LLM.

        Every non-empty requested concept must appear in the tag's
        configured concepts, name or description.
        """

        clean_concepts = [
            concept.strip().lower()
            for concept in concepts
            if concept.strip()
        ]

        if equipment_name:
            candidates = (
                self.get_tags_for_equipment(
                    equipment_name
                )
            )
        else:
            knowledge = self.get_knowledge()

            candidates = [
                tag
                for equipment in knowledge.equipment
                for tag in equipment.tags
            ]

            candidates.extend(
                knowledge.unassigned_tags
            )

        if not clean_concepts:
            return candidates

        matching_tags: list[
            KnowledgeTag
        ] = []

        for tag in candidates:
            searchable_text = " ".join(
                [
                    self._split_camel_case(
                        tag.tag_name
                    ),
                    tag.description,
                    " ".join(
                        tag.concepts
                    ),
                    tag.unit,
                ]
            ).lower()

            if all(
                re.search(
                    rf"\b{re.escape(concept)}\b",
                    searchable_text,
                )
                for concept in clean_concepts
            ):
                matching_tags.append(
                    tag
                )

        return matching_tags

    def summary(
        self,
    ) -> dict[str, int]:
        knowledge = self.get_knowledge()

        assigned_tag_count = sum(
            len(
                equipment.tags
            )
            for equipment in knowledge.equipment
        )

        return {
            "equipment_count": len(
                knowledge.equipment
            ),
            "assigned_tag_count": (
                assigned_tag_count
            ),
            "unassigned_tag_count": len(
                knowledge.unassigned_tags
            ),
            "total_tag_count": len(
                knowledge.available_tag_names
            ),
        }


def main() -> None:
    print(
        "Building SmartMachineAI factory knowledge..."
    )

    try:
        engine = FactoryKnowledgeEngine()
        summary = engine.summary()

    except Exception as exc:
        print(
            f"Startup error: {exc}"
        )
        return

    print()
    print(
        "Knowledge summary:"
    )

    print(
        json.dumps(
            summary,
            indent=2,
        )
    )

    print()
    print(
        engine.build_llm_context()
    )

    print()
    print(
        "Concept filtering tests:"
    )

    tests = [
        (
            "Chiller System",
            [
                "temperature",
                "return",
            ],
        ),
        (
            "Air Compressor System",
            [
                "pressure",
            ],
        ),
        (
            "Cold Room",
            [
                "alarm",
            ],
        ),
        (
            "Storage and Transfer System",
            [
                "level",
                "high",
            ],
        ),
    ]

    for equipment_name, concepts in tests:
        matches = engine.find_tags_by_concepts(
            concepts=concepts,
            equipment_name=equipment_name,
        )

        print()
        print(
            f"Equipment: {equipment_name}"
        )
        print(
            "Concepts: "
            + ", ".join(
                concepts
            )
        )

        if not matches:
            print(
                "Matches: None"
            )
            continue

        print(
            "Matches: "
            + ", ".join(
                tag.tag_name
                for tag in matches
            )
        )


if __name__ == "__main__":
    main()

