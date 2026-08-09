from __future__ import annotations

import json
import re
import sqlite3
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .concept_extractor import ConceptExtractor, normalize
from .models import Candidate, QueryResult, Tag


def tokens(value: object) -> set[str]:
    """Convert a value into normalized word tokens."""
    return set(normalize(value).split())


def overlap(left: object, right: object) -> float:
    """Return Jaccard token similarity between two values."""
    left_tokens = tokens(left)
    right_tokens = tokens(right)

    if not left_tokens or not right_tokens:
        return 0.0

    return len(left_tokens & right_tokens) / len(
        left_tokens | right_tokens
    )


DESIGNATOR_PATTERN = re.compile(r"^[a-z]{1,8}\d{1,3}$")


class IndustrialQueryEngine:
    """Deterministic industrial question-to-tag resolver."""

    REQUIRED = {
        "measurement",
        "location",
        "signal_type",
        "event_type",
        "threshold_type",
    }

    def __init__(
        self,
        database_path: str | Path = "database/config.db",
        system_config_path: str | Path = "config/system.json",
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.system_config_path = (
            Path(system_config_path).expanduser().resolve()
        )

        if not self.database_path.exists():
            raise FileNotFoundError(
                f"Configuration database not found: "
                f"{self.database_path}"
            )

        self.active_driver = self._load_active_driver()
        self.extractor = ConceptExtractor()
        self._check_schema()

    def _load_active_driver(self) -> str:
        """Read the active communications driver from system.json."""
        default_driver = "simulator"

        if not self.system_config_path.exists():
            return default_driver

        try:
            with self.system_config_path.open(
                "r",
                encoding="utf-8",
            ) as file:
                config: dict[str, Any] = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Unable to read system configuration "
                f"{self.system_config_path}: {exc}"
            ) from exc

        driver = normalize(config.get("active_driver", default_driver))

        if not driver:
            return default_driver

        return driver

    def _connect(self) -> sqlite3.Connection:
        """Open the configuration database."""
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _check_schema(self) -> None:
        """Confirm that metadata migration has been completed."""
        with self._connect() as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(tags)"
                )
            }

        if not self.REQUIRED.issubset(columns):
            raise RuntimeError(
                "Run this first: "
                "python -m engine.metadata_migrator"
            )

    def _load_equipment_aliases(
        self,
        connection: sqlite3.Connection,
    ) -> dict[int, list[str]]:
        """Load aliases indexed by equipment ID."""
        alias_rows = connection.execute(
            """
            SELECT equipment_id, alias
            FROM equipment_aliases
            ORDER BY equipment_id, alias
            """
        ).fetchall()

        aliases: dict[int, list[str]] = {}

        for row in alias_rows:
            equipment_id = int(row["equipment_id"])
            aliases.setdefault(equipment_id, []).append(
                str(row["alias"])
            )

        return aliases

    def _address_priority(self, row: sqlite3.Row) -> tuple[int, int]:
        """
        Rank address rows.

        Priority:
        1. Active driver address
        2. Tag's original/default driver
        3. Any enabled address
        """
        row_driver = normalize(row["address_driver"])
        tag_driver = normalize(row["tag_driver"])

        if row_driver == self.active_driver:
            return 0, int(row["address_id"] or 0)

        if row_driver and row_driver == tag_driver:
            return 1, int(row["address_id"] or 0)

        if row_driver:
            return 2, int(row["address_id"] or 0)

        return 3, int(row["address_id"] or 0)

    def _select_address(
        self,
        rows: list[sqlite3.Row],
    ) -> sqlite3.Row:
        """Select one communications address for a logical tag."""
        return sorted(rows, key=self._address_priority)[0]

    def _tags(self) -> list[Tag]:
        """
        Load each logical tag once.

        Multiple communications addresses may exist for a tag, but only
        the address matching the configured active driver is selected.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    t.id,
                    t.tag_name,
                    COALESCE(t.description, '') AS description,
                    COALESCE(t.driver, '') AS tag_driver,
                    COALESCE(t.address, '') AS tag_address,
                    COALESCE(t.data_type, '') AS data_type,
                    COALESCE(t.unit, '') AS unit,
                    t.equipment_id,
                    COALESCE(e.name, '') AS equipment_name,
                    COALESCE(
                        e.display_name,
                        ''
                    ) AS equipment_display_name,
                    COALESCE(t.measurement, '') AS measurement,
                    COALESCE(t.location, '') AS location,
                    COALESCE(t.signal_type, '') AS signal_type,
                    COALESCE(t.event_type, '') AS event_type,
                    COALESCE(
                        t.threshold_type,
                        ''
                    ) AS threshold_type,
                    ta.id AS address_id,
                    COALESCE(
                        ta.driver,
                        ''
                    ) AS address_driver,
                    COALESCE(
                        ta.address,
                        ''
                    ) AS address_value
                FROM tags t
                LEFT JOIN equipment e
                    ON e.id = t.equipment_id
                LEFT JOIN tag_addresses ta
                    ON ta.tag_id = t.id
                    AND ta.enabled = 1
                WHERE t.enabled = 1
                ORDER BY
                    t.tag_name,
                    ta.id
                """
            ).fetchall()

            equipment_aliases = self._load_equipment_aliases(
                connection
            )

        grouped_rows: dict[int, list[sqlite3.Row]] = {}

        for row in rows:
            grouped_rows.setdefault(
                int(row["id"]),
                [],
            ).append(row)

        tags: list[Tag] = []

        for tag_id, tag_rows in grouped_rows.items():
            selected_row = self._select_address(tag_rows)

            selected_driver = (
                selected_row["address_driver"]
                or selected_row["tag_driver"]
            )

            selected_address = (
                selected_row["address_value"]
                or selected_row["tag_address"]
            )

            equipment_id = selected_row["equipment_id"]

            if equipment_id is None:
                aliases: tuple[str, ...] = ()
            else:
                aliases = tuple(
                    equipment_aliases.get(
                        int(equipment_id),
                        [],
                    )
                )

            tags.append(
                Tag(
                    id=tag_id,
                    tag_name=selected_row["tag_name"],
                    description=selected_row["description"],
                    driver=selected_driver,
                    address=selected_address,
                    data_type=selected_row["data_type"],
                    unit=selected_row["unit"],
                    equipment_name=selected_row[
                        "equipment_name"
                    ],
                    equipment_display_name=selected_row[
                        "equipment_display_name"
                    ],
                    aliases=aliases,
                    measurement=selected_row["measurement"],
                    location=selected_row["location"],
                    signal_type=selected_row["signal_type"],
                    event_type=selected_row["event_type"],
                    threshold_type=selected_row[
                        "threshold_type"
                    ],
                )
            )

        return tags

    @staticmethod
    def _fuzzy_equipment_score(
        query_equipment: str,
        equipment_names: tuple[str, ...],
    ) -> tuple[float, str]:
        """
        Original equipment-name similarity scoring: best of token
        overlap or whole-string fuzzy (typo-tolerant) matching.
        """
        best_score = 0.0
        best_label = ""

        for equipment_name in equipment_names:
            if not equipment_name:
                continue

            score = max(
                overlap(query_equipment, equipment_name),
                SequenceMatcher(
                    None,
                    normalize(query_equipment),
                    normalize(equipment_name),
                ).ratio()
                * 0.75,
            )

            if score > best_score:
                best_score = score
                best_label = equipment_name

        return best_score, best_label

    def _equipment_score(
        self,
        terms: tuple[str, ...],
        tag: Tag,
    ) -> tuple[float, str]:
        """Calculate the equipment-name similarity score."""
        if not terms:
            return 0.0, ""

        query_equipment = " ".join(terms)

        equipment_names = (
            tag.equipment_name,
            tag.equipment_display_name,
            *tag.aliases,
        )

        designators = {
            term for term in terms if DESIGNATOR_PATTERN.match(term)
        }

        if not designators:
            return self._fuzzy_equipment_score(
                query_equipment,
                equipment_names,
            )

        # The question names a specific instance code (e.g. "cr01").
        # A tag whose equipment has that exact code as an alias is
        # almost certainly the one meant - resolve to it directly
        # rather than letting generic fuzzy word overlap (e.g. both
        # "Air Compressor" and "Cold Room ... Compressor Power"
        # sharing the word "compressor") outrank the correct instance.
        exact_designator_alias = next(
            (
                name
                for name in equipment_names
                if name and normalize(name) in designators
            ),
            None,
        )

        if exact_designator_alias:
            return 1.0, exact_designator_alias

        # This tag's equipment doesn't match the named instance code -
        # heavily discount whatever fuzzy score it would otherwise
        # get, so a wrong-equipment fuzzy match can't outrank the
        # candidate that actually has the named instance.
        fuzzy_score, _ = self._fuzzy_equipment_score(
            query_equipment,
            equipment_names,
        )

        return fuzzy_score * 0.3, ""

    def _rank(self, concepts: Any, tag: Tag) -> Candidate:
        """Score one configured tag against extracted concepts."""
        score = 0.0
        reasons: list[str] = []

        equipment_score, equipment_name = (
            self._equipment_score(
                concepts.equipment_terms,
                tag,
            )
        )

        score += equipment_score * 40

        if equipment_score:
            reasons.append(
                f"equipment={equipment_name}:"
                f"{equipment_score:.2f}"
            )

        if concepts.measurement:
            if (
                normalize(tag.measurement)
                == concepts.measurement
            ):
                score += 30
                reasons.append("measurement exact")
            elif concepts.measurement in normalize(
                f"{tag.tag_name} {tag.description}"
            ):
                score += 18
                reasons.append("measurement text")
            else:
                score -= 18
                reasons.append("measurement mismatch")

        if concepts.location:
            if normalize(tag.location) == concepts.location:
                score += 16
                reasons.append("location exact")
            elif concepts.location in normalize(
                f"{tag.tag_name} {tag.description}"
            ):
                score += 8
                reasons.append("location text")
            elif tag.location:
                score -= 6
                reasons.append("location mismatch")

        if concepts.event_type:
            if (
                normalize(tag.event_type)
                == concepts.event_type
            ):
                score += 14
                reasons.append("event exact")
            elif concepts.event_type in normalize(
                f"{tag.tag_name} {tag.description}"
            ):
                score += 7
                reasons.append("event text")
            else:
                score -= 8
                reasons.append("event mismatch")

        if concepts.threshold_type:
            if (
                normalize(tag.threshold_type)
                == concepts.threshold_type
            ):
                score += 10
                reasons.append("threshold exact")
            elif tag.threshold_type:
                score -= 5
                reasons.append("threshold mismatch")

        lexical_score = overlap(
            concepts.normalized,
            (
                f"{tag.tag_name} "
                f"{tag.description} "
                f"{tag.equipment_display_name}"
            ),
        )

        score += lexical_score * 10

        if lexical_score:
            reasons.append(f"lexical={lexical_score:.2f}")

        return Candidate(
            tag=tag,
            score=round(score, 3),
            reasons=tuple(reasons),
        )

    def query(
        self,
        question: str,
        limit: int = 5,
    ) -> QueryResult:
        """Resolve an operator question into a configured tag."""
        start = time.perf_counter()
        concepts = self.extractor.extract(question)

        ranked = sorted(
            (
                self._rank(concepts, tag)
                for tag in self._tags()
            ),
            key=lambda candidate: candidate.score,
            reverse=True,
        )[: max(1, limit)]

        top = ranked[0] if ranked else None

        second_score = (
            ranked[1].score
            if len(ranked) > 1
            else -999.0
        )

        gap = (
            top.score - second_score
            if top
            else 0.0
        )

        if not top or top.score < 18:
            status = "no_match"
            confidence = 0.30
            selected = None
            message = (
                "No configured tag matches the question "
                "reliably."
            )

        elif len(ranked) > 1 and gap < 7:
            status = "clarification_required"
            confidence = 0.60
            selected = None
            message = (
                "Similar candidates: "
                + ", ".join(
                    candidate.tag.tag_name
                    for candidate in ranked[:3]
                )
            )

        elif (
            concepts.measurement
            and top.tag.measurement
            and concepts.measurement
            != top.tag.measurement
        ):
            status = "clarification_required"
            confidence = 0.50
            selected = None
            message = (
                "The best candidate does not exactly match "
                "the requested measurement."
            )

        else:
            status = "resolved"
            confidence = min(
                0.99,
                0.45
                + max(top.score, 0) / 160
                + min(max(gap, 0), 30) / 150,
            )
            selected = top.tag
            message = (
                "A configured tag was resolved without an LLM. "
                f"Active driver: {self.active_driver}."
            )

        return QueryResult(
            question=concepts.question,
            status=status,
            intent=concepts.intent,
            time_expression=concepts.time_expression,
            selected_tag=(
                selected.tag_name
                if selected
                else ""
            ),
            equipment=(
                selected.equipment_name
                if selected
                else ""
            ),
            measurement=(
                selected.measurement
                if selected
                else concepts.measurement
            ),
            location=(
                selected.location
                if selected
                else concepts.location
            ),
            condition=concepts.condition,
            confidence=round(confidence, 3),
            message=message,
            candidates=ranked,
            concepts=concepts,
            elapsed_seconds=round(
                time.perf_counter() - start,
                6,
            ),
        )
