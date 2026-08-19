from __future__ import annotations

import json
import re
import sqlite3
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from config.environment import get_config_db_path

from .concept_extractor import ConceptExtractor, normalize
from .models import (
    Candidate,
    EquipmentCandidate,
    EquipmentComparisonResolution,
    EquipmentResolution,
    QueryResult,
    Tag,
)


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

PLANT_SEGMENT_PATTERN = re.compile(r"^p\d{1,2}$")


def _plant_of(tag_name: str) -> str:
    """
    e.g. "P01.UTILITY.AC01.Pressure" -> "p01". Returns "" for a tag
    name that doesn't start with a plant-style segment (every tag in
    the current dataset does, but this must never crash on one that
    doesn't - it just means that tag opts out of plant-tie-breaking).
    """
    first_segment = tag_name.split(".", 1)[0].lower()

    return first_segment if PLANT_SEGMENT_PATTERN.match(first_segment) else ""


# Phase 15 - equipment-only resolution thresholds. Deliberately SEPARATE
# constants from query()'s own tag-ranking thresholds (top.score < 18,
# gap < 7) - those operate on the *40-scaled combined tag score;
# equipment_score here is the raw 0.0-1.0 fuzzy/designator score, so the
# floors/gaps need their own, smaller-scale numbers. Chosen to mirror the
# existing EQUIPMENT_STATUS_SCORE_FLOOR / FACTORY_TOTAL_POWER_EQUIPMENT_SCORE_FLOOR
# convention already used in app/ask.py (both 0.4).
EQUIPMENT_RESOLUTION_SCORE_FLOOR = 0.4
EQUIPMENT_RESOLUTION_GAP_THRESHOLD = 0.15

# Ordered, most-unambiguous-first. Tried in this order against a
# comparison question with a leading "compare"/"comparing" word already
# stripped - the first connector that produces two non-empty segments
# wins. "vs"/"versus" are tried before "and"/"or" because they can ONLY
# ever mean a comparison connector, whereas "and"/"or" could in principle
# appear inside a longer equipment description (rare in this dataset's
# short designator-code style names, but "vs"/"versus" are strictly safer
# so they get first refusal).
_COMPARISON_CONNECTOR_PATTERNS = [
    re.compile(r"\s+(?:vs\.?|versus)\s+", re.IGNORECASE),
    re.compile(r"\s+against\s+", re.IGNORECASE),
    re.compile(r"\s+with\s+", re.IGNORECASE),
    re.compile(r"\s+and\s+", re.IGNORECASE),
    re.compile(r"\s+or\s+", re.IGNORECASE),
    re.compile(r"\s+to\s+", re.IGNORECASE),
]

_COMPARISON_LEADING_WORD_PATTERN = re.compile(
    r"^\s*(?:compare|comparing|which\s+is\s+better,?)\s*[:,]?\s*",
    re.IGNORECASE,
)


def split_comparison_entities(question: str) -> tuple[str, str] | None:
    """
    Deterministic, ORDERED split of an equipment-comparison question into
    two independent equipment-referring segments - e.g. "Compare P01
    WSP01 and P02 WSP01" -> ("P01 WSP01", "P02 WSP01").

    Deliberately NOT "call resolve_equipment() on the full question
    twice" - that would resolve BOTH mentions against the same combined
    text and either collapse to one equipment or pick an arbitrary one
    twice, never genuinely resolving "entity A" and "entity B"
    independently (see IndustrialQueryEngine.resolve_equipment_pair()).

    Returns None if no known connector splits the question into two
    non-empty segments - callers should treat that as "could not
    determine two comparison entities" and fall back to a clarification
    response rather than guessing.
    """
    body = _COMPARISON_LEADING_WORD_PATTERN.sub("", question).strip()

    if not body:
        return None

    for pattern in _COMPARISON_CONNECTOR_PATTERNS:
        parts = pattern.split(body, maxsplit=1)

        if len(parts) == 2:
            left, right = parts[0].strip(" ?.,"), parts[1].strip(" ?.,")

            if left and right:
                return left, right

    return None


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
        database_path: str | Path = get_config_db_path(),
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

    def _break_plant_ties(
        self,
        ranked: list[Candidate],
        concepts: Any,
    ) -> list[Candidate]:
        """
        Resolve a top-score tie that exists ONLY because the same
        equipment instance code (e.g. "AC01") exists on more than one
        plant - a real, common situation once more than one plant
        shares the same numbering convention (see the designator-exact
        -match branch of _equipment_score(), which scores every
        same-coded instance identically regardless of plant).

        Prefers whichever plant the question named explicitly
        (concepts.plant, e.g. "P02 AC01 pressure"), otherwise the
        lexicographically-first plant among the candidates ("p01"
        before "p02" before "p03", ...) - not hardcoded to any
        specific number of plants, so this scales to however many
        exist without changes here.

        Deliberately narrow: only ever DROPS other-plant duplicates
        that are true exact-designator-code matches, never re-scores
        or reorders anything else. Candidates are compared within the
        same score window (`< 7`) that query()'s own ambiguity check
        uses below, rather than requiring a near-exact score match -
        two same-code, different-plant tags rarely land on the
        *identical* final score (e.g. incidental lexical token overlap
        with a tag's own "p02..." name already nudges one candidate a
        point or two above the other), even though they represent the
        same underlying ambiguity. A genuine difference between two
        different pieces of equipment is left completely untouched,
        still surfaced as a real clarification-required ambiguity.
        """
        if len(ranked) < 2:
            return ranked

        top_score = ranked[0].score
        near_top = [
            candidate for candidate in ranked if top_score - candidate.score < 7
        ]

        if len(near_top) < 2:
            return ranked

        exact_designator_matches = [
            candidate
            for candidate in near_top
            if self._equipment_score(
                concepts.equipment_terms, candidate.tag
            )[0]
            == 1.0
        ]

        if len(exact_designator_matches) < 2:
            return ranked

        plants = {
            _plant_of(candidate.tag.tag_name)
            for candidate in exact_designator_matches
        }

        if len(plants) < 2 or "" in plants:
            # Not a plant-only tie - either every matching candidate is
            # already on the same plant, or one of them has no
            # recognizable plant prefix at all. Leave it alone.
            return ranked

        preferred_plant = (
            concepts.plant if concepts.plant in plants else min(plants)
        )
        preferred = [
            candidate
            for candidate in exact_designator_matches
            if _plant_of(candidate.tag.tag_name) == preferred_plant
        ]

        if len(preferred) != 1:
            # Still ambiguous even after picking a plant (e.g. two
            # different tags tied on the same preferred plant) - don't
            # guess further.
            return ranked

        dropped = {
            id(candidate)
            for candidate in exact_designator_matches
            if candidate is not preferred[0]
        }

        return [
            candidate for candidate in ranked if id(candidate) not in dropped
        ]

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
        )

        ranked = self._break_plant_ties(ranked, concepts)[
            : max(1, limit)
        ]

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

    def _equipment_plant_tie(
        self,
        candidates: list[Candidate],
        concepts: Any,
    ) -> tuple[list[Candidate], bool]:
        """
        Phase 15 equipment-only plant-tie resolution - deliberately
        DIFFERENT from _break_plant_ties() above.

        _break_plant_ties() silently defaults to the lexicographically-
        first plant when the question named no plant qualifier at all -
        a reasonable trade-off for a single TAG VALUE question, where
        picking P01's reading as the default display is low-stakes and
        instantly correctable ("no, I meant P02"). For an EQUIPMENT-LEVEL
        domain question ("why is WSP01 unhealthy"), silently guessing the
        plant would silently answer about the wrong physical asset -
        Phase 15's approved correction requires this to surface as a
        genuine ambiguity instead.

        Returns (candidates, was_ambiguous_multiplant) - the second
        value is True only when a real multi-plant tie exists AND no
        explicit plant qualifier in the question resolved it. Reuses
        _equipment_score()'s exact designator-match detection (score ==
        1.0) so the two tie-break paths never disagree about what counts
        as "the same instance code."
        """
        if len(candidates) < 2:
            return candidates, False

        top_score = candidates[0].score
        near_top = [
            c for c in candidates
            if top_score - c.score < EQUIPMENT_RESOLUTION_GAP_THRESHOLD
        ]

        if len(near_top) < 2:
            return candidates, False

        exact_designator_matches = [
            c for c in near_top
            if self._equipment_score(concepts.equipment_terms, c.tag)[0] == 1.0
        ]

        if len(exact_designator_matches) < 2:
            return candidates, False

        plants = {_plant_of(c.tag.tag_name) for c in exact_designator_matches}

        if len(plants) < 2 or "" in plants:
            return candidates, False

        if concepts.plant and concepts.plant in plants:
            preferred = [
                c for c in exact_designator_matches
                if _plant_of(c.tag.tag_name) == concepts.plant
            ]

            if len(preferred) == 1:
                dropped = {
                    id(c) for c in exact_designator_matches if c is not preferred[0]
                }
                return (
                    [c for c in candidates if id(c) not in dropped],
                    False,
                )

        # No explicit plant qualifier named (or it didn't uniquely
        # resolve the tie) - a genuine multi-plant ambiguity. Never
        # silently pick one; the caller returns clarification_required.
        return candidates, True

    def resolve_equipment(
        self,
        question: str,
        limit: int = 5,
    ) -> EquipmentResolution:
        """
        Phase 15 - resolve a question to a canonical EQUIPMENT identity
        (instance_key), not a specific tag. Reuses the existing tag-level
        scoring (_equipment_score(), same designator fast path and fuzzy
        matching every tag-level question already relies on) rather than
        reimplementing equipment matching - a wrapper around already-
        tested logic, not a parallel resolver.

        _equipment_score() depends only on a tag's
        equipment_name/equipment_display_name/aliases, which are
        identical for every tag belonging to the same equipment, so each
        equipment needs to be scored exactly once (via one representative
        tag) rather than once per tag.
        """
        concepts = self.extractor.extract(question)
        tags = self._tags()

        representative_tag_by_equipment: dict[str, Tag] = {}

        for tag in tags:
            if tag.equipment_name:
                representative_tag_by_equipment.setdefault(
                    tag.equipment_name, tag
                )

        scored = [
            Candidate(
                tag=rep_tag,
                score=self._equipment_score(
                    concepts.equipment_terms, rep_tag
                )[0],
                reasons=(),
            )
            for rep_tag in representative_tag_by_equipment.values()
        ]

        ranked = sorted(scored, key=lambda c: c.score, reverse=True)
        ranked, multiplant_ambiguous = self._equipment_plant_tie(
            ranked, concepts
        )
        ranked = ranked[: max(1, limit)]

        top = ranked[0] if ranked else None
        second_score = ranked[1].score if len(ranked) > 1 else -999.0
        gap = (top.score - second_score) if top else 0.0

        candidates = [
            EquipmentCandidate(
                equipment_name=c.tag.equipment_name,
                equipment_display_name=c.tag.equipment_display_name,
                instance_key=_instance_key_of(c.tag.tag_name),
                plant=_plant_of(c.tag.tag_name),
                equipment_score=c.score,
            )
            for c in ranked
        ]

        # Deliberately NOT bool(concepts.equipment_terms) - the standing
        # "leftover generic word" landmine (CLAUDE.md: _correct_word()/
        # equipment_terms can retain an ordinary English word like
        # "improved" that was never meant to name equipment, e.g. "has
        # it improved since yesterday" -> equipment_terms=('improved',)
        # - which would make a bare follow-up look like it named real
        # equipment). A genuine reference in this dataset's convention
        # is either an exact designator code (DESIGNATOR_PATTERN, e.g.
        # "wsp01") or something that scored a strong, not merely
        # coincidental, fuzzy match - 0.9+ is well above what stray
        # word overlap produces in practice (verified: "improved"
        # against every real equipment name tops out around 0.31).
        had_equipment_terms = any(
            DESIGNATOR_PATTERN.match(term) for term in concepts.equipment_terms
        ) or (top is not None and top.score >= 0.9)

        if not top or top.score < EQUIPMENT_RESOLUTION_SCORE_FLOOR:
            return EquipmentResolution(
                question=question,
                status="no_match",
                confidence=0.30,
                message="No configured equipment matches the question reliably.",
                candidates=candidates,
                had_equipment_terms=had_equipment_terms,
            )

        if multiplant_ambiguous:
            return EquipmentResolution(
                question=question,
                status="clarification_required",
                confidence=0.55,
                message=(
                    "This equipment code exists in more than one plant - "
                    "please specify which plant (e.g. P01 or P02)."
                ),
                candidates=candidates,
                had_equipment_terms=had_equipment_terms,
            )

        if len(ranked) > 1 and gap < EQUIPMENT_RESOLUTION_GAP_THRESHOLD:
            return EquipmentResolution(
                question=question,
                status="clarification_required",
                confidence=0.55,
                message=(
                    "More than one piece of equipment matches similarly "
                    "well - please be more specific."
                ),
                candidates=candidates,
                had_equipment_terms=had_equipment_terms,
            )

        return EquipmentResolution(
            question=question,
            status="resolved",
            instance_key=_instance_key_of(top.tag.tag_name),
            equipment_name=top.tag.equipment_name,
            equipment_display_name=top.tag.equipment_display_name,
            plant=_plant_of(top.tag.tag_name),
            confidence=min(0.99, 0.45 + max(top.score, 0) * 0.5 + min(max(gap, 0), 0.3)),
            message="Equipment resolved without an LLM.",
            had_equipment_terms=had_equipment_terms,
            candidates=candidates,
        )

    def resolve_equipment_pair(
        self,
        question: str,
    ) -> EquipmentComparisonResolution:
        """
        Phase 15 - ordered, independent multi-entity resolution for a
        comparison question ("Compare P01 WSP01 and P02 WSP01"). Splits
        the question into two segments via split_comparison_entities()
        and resolves EACH independently through resolve_equipment() -
        deliberately never resolve_equipment() called once against the
        whole question (that would not distinguish "entity A" from
        "entity B" at all).
        """
        split = split_comparison_entities(question)

        if split is None:
            return EquipmentComparisonResolution(
                question=question,
                status="could_not_split",
                message=(
                    "I couldn't tell which two pieces of equipment you "
                    "want to compare - try phrasing it like "
                    '"compare X and Y".'
                ),
            )

        segment_a, segment_b = split
        entity_a = self.resolve_equipment(segment_a)
        entity_b = self.resolve_equipment(segment_b)

        if (
            entity_a.status == "resolved" and entity_b.status == "resolved"
            and entity_a.instance_key == entity_b.instance_key
        ):
            # Phase 17.2c - a genuine gap found during audit: both
            # segments independently resolving to the SAME equipment
            # (e.g. "compare WSP01 with WSP01") used to silently return
            # status="resolved", producing a comparison of one equipment
            # against itself. Entity A/B must remain distinct canonical
            # objects (the approved Phase 17.2c boundary) - this is
            # never a resolution failure of either side individually
            # (both are legitimately "resolved"), so it gets its own
            # status rather than being folded into "no_match"/
            # "clarification_required".
            status = "duplicate_entity"
            message = (
                "Both sides of the comparison resolved to the same equipment "
                f"({entity_a.equipment_display_name or entity_a.instance_key}) - "
                "please name two different pieces of equipment to compare."
            )
        elif entity_a.status == "resolved" and entity_b.status == "resolved":
            status = "resolved"
            message = "Both pieces of equipment were resolved without an LLM."
        elif entity_a.status == "no_match" or entity_b.status == "no_match":
            status = "no_match"
            message = "At least one side of the comparison did not match any configured equipment."
        else:
            status = "clarification_required"
            message = "At least one side of the comparison is ambiguous."

        return EquipmentComparisonResolution(
            question=question,
            status=status,
            entity_a=entity_a,
            entity_b=entity_b,
            message=message,
        )


def _instance_key_of(tag_name: str) -> str:
    """
    "P01.UTILITY.AC01.Pressure" -> "P01.UTILITY.AC01" - the canonical
    Phase 8-14 instance_key, derived directly from a resolved tag's own
    name. Every tag in this dataset is named "{instance_key}.{signal
    name}" (confirmed across Phase 6-14's own tag-naming convention), so
    a plain rsplit is sufficient and avoids introducing a new dependency
    from engine/ on simulator/ just for this string transform (mirrors
    simulator.plant_context.instance_key()'s identical logic, kept as an
    independent copy rather than a cross-package import - see
    ai/context_builder.py for where the bridge to simulator/-owned
    concepts, like production context, is made instead).
    """
    parts = tag_name.split(".")
    return ".".join(parts[:-1]) if len(parts) >= 2 else tag_name
