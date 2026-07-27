from __future__ import annotations

import json
import math
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_DATABASE = (
    PROJECT_ROOT
    / "database"
    / "config.db"
)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"


@dataclass(frozen=True)
class TagKnowledge:
    """
    One enabled PLC or machine tag loaded from config.db.
    """

    tag_id: int
    tag_name: str
    description: str
    driver: str
    address: str
    data_type: str
    unit: str
    equipment_names: tuple[str, ...] = ()
    equipment_display_names: tuple[str, ...] = ()
    equipment_descriptions: tuple[str, ...] = ()
    relationship_types: tuple[str, ...] = ()

    def semantic_text(self) -> str:
        """
        Build the descriptive text sent to the embedding model.

        The embedding model compares this text with the user's
        question.
        """
        parts: list[str] = [
            f"Tag name: {self.tag_name}",
        ]

        if self.description:
            parts.append(
                f"Measurement or status: {self.description}"
            )

        if self.equipment_display_names:
            parts.append(
                "Equipment: "
                + ", ".join(
                    self.equipment_display_names
                )
            )

        elif self.equipment_names:
            parts.append(
                "Equipment: "
                + ", ".join(
                    self.equipment_names
                )
            )

        if self.equipment_descriptions:
            parts.append(
                "Equipment description: "
                + ", ".join(
                    self.equipment_descriptions
                )
            )

        if self.unit:
            parts.append(
                f"Engineering unit: {self.unit}"
            )

        if self.data_type:
            parts.append(
                f"Data type: {self.data_type}"
            )

        if self.relationship_types:
            parts.append(
                "Equipment relationship: "
                + ", ".join(
                    self.relationship_types
                )
            )

        if self.address:
            parts.append(
                f"PLC address: {self.address}"
            )

        if self.driver:
            parts.append(
                f"Communication driver: {self.driver}"
            )

        return ". ".join(parts)


@dataclass(frozen=True)
class SemanticTagMatch:
    """
    One candidate returned by semantic tag matching.
    """

    tag: TagKnowledge
    similarity: float
    rank: int
    accepted: bool = False
    reason: str = ""

    @property
    def confidence_percent(self) -> float:
        return round(
            self.similarity * 100,
            1,
        )


@dataclass
class SemanticResolution:
    """
    Complete result returned by SemanticTagResolver.
    """

    question: str
    resolved: bool
    selected: SemanticTagMatch | None = None
    candidates: list[SemanticTagMatch] = field(
        default_factory=list
    )
    method: str = "semantic"
    message: str = ""

    @property
    def tag_name(self) -> str | None:
        if self.selected is None:
            return None

        return self.selected.tag.tag_name


class OllamaEmbeddingClient:
    """
    Small client for Ollama's local embedding API.

    No internet connection is required.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_EMBEDDING_MODEL,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def embed(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        if not texts:
            return []

        payload = json.dumps(
            {
                "model": self.model,
                "input": texts,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            url=f"{self.base_url}/api/embed",
            data=payload,
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw_response = response.read().decode(
                    "utf-8"
                )

        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode(
                "utf-8",
                errors="replace",
            )

            raise RuntimeError(
                "Ollama embedding request failed with "
                f"HTTP {exc.code}: {error_body}"
            ) from exc

        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Unable to connect to Ollama at "
                f"{self.base_url}. Confirm that Ollama is "
                "running."
            ) from exc

        except TimeoutError as exc:
            raise RuntimeError(
                "The Ollama embedding request timed out."
            ) from exc

        try:
            parsed = json.loads(
                raw_response
            )

        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Ollama returned invalid JSON."
            ) from exc

        embeddings = parsed.get(
            "embeddings"
        )

        if not isinstance(
            embeddings,
            list,
        ):
            raise RuntimeError(
                "Ollama response did not contain an "
                "'embeddings' list."
            )

        normalized_embeddings: list[
            list[float]
        ] = []

        for embedding in embeddings:
            if not isinstance(
                embedding,
                list,
            ):
                raise RuntimeError(
                    "Ollama returned an invalid embedding."
                )

            normalized_embeddings.append(
                [
                    float(value)
                    for value in embedding
                ]
            )

        if len(normalized_embeddings) != len(texts):
            raise RuntimeError(
                "Ollama returned a different number of "
                "embeddings than requested."
            )

        return normalized_embeddings

    def embed_one(
        self,
        text: str,
    ) -> list[float]:
        embeddings = self.embed(
            [text]
        )

        if not embeddings:
            raise RuntimeError(
                "Ollama returned no embedding."
            )

        return embeddings[0]


class SemanticTagResolver:
    """
    Resolves natural-language questions to configured tags.

    Resolution order:

        1. Exact tag-name match
        2. Exact description match
        3. Semantic embedding comparison

    Embeddings for configured tags are generated once and kept
    in memory. They can be rebuilt when tags are changed.
    """

    def __init__(
        self,
        database_path: str | Path | None = None,
        ollama_url: str = DEFAULT_OLLAMA_URL,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        auto_build_index: bool = True,
        automatic_accept_score: float = 0.80,
        conditional_accept_score: float = 0.65,
        minimum_score_gap: float = 0.08,
    ) -> None:
        self.database_path = Path(
            database_path
            or DEFAULT_CONFIG_DATABASE
        )

        self.embedding_client = OllamaEmbeddingClient(
            base_url=ollama_url,
            model=embedding_model,
        )

        self.automatic_accept_score = (
            automatic_accept_score
        )

        self.conditional_accept_score = (
            conditional_accept_score
        )

        self.minimum_score_gap = (
            minimum_score_gap
        )

        self._tags: list[TagKnowledge] = []
        self._embeddings: dict[
            int,
            list[float],
        ] = {}

        if auto_build_index:
            self.rebuild_index()

    def _connect(
        self,
    ) -> sqlite3.Connection:
        if not self.database_path.exists():
            raise FileNotFoundError(
                "Configuration database not found: "
                f"{self.database_path}"
            )

        connection = sqlite3.connect(
            str(self.database_path)
        )

        connection.row_factory = sqlite3.Row

        return connection

    @staticmethod
    def _clean_text(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        return str(value).strip()

    def _load_tags(
        self,
    ) -> list[TagKnowledge]:
        """
        Load enabled tags and their equipment relationships.

        A tag can be connected through:

        - tags.equipment_id
        - equipment_tags relationship table

        Both methods are supported.
        """
        query = """
            SELECT
                t.id AS tag_id,
                t.tag_name,
                t.description,
                t.driver,
                t.address,
                t.data_type,
                t.unit,

                e_direct.name
                    AS direct_equipment_name,

                e_direct.display_name
                    AS direct_equipment_display_name,

                e_direct.description
                    AS direct_equipment_description,

                e_related.name
                    AS related_equipment_name,

                e_related.display_name
                    AS related_equipment_display_name,

                e_related.description
                    AS related_equipment_description,

                et.relationship_type

            FROM tags AS t

            LEFT JOIN equipment AS e_direct
                ON e_direct.id = t.equipment_id

            LEFT JOIN equipment_tags AS et
                ON et.tag_id = t.id

            LEFT JOIN equipment AS e_related
                ON e_related.id = et.equipment_id

            WHERE t.enabled = 1

            ORDER BY
                t.id,
                et.display_order,
                et.id
        """

        grouped: dict[
            int,
            dict[str, Any],
        ] = {}

        with self._connect() as connection:
            rows = connection.execute(
                query
            ).fetchall()

        for row in rows:
            tag_id = int(
                row["tag_id"]
            )

            if tag_id not in grouped:
                grouped[tag_id] = {
                    "tag_id": tag_id,
                    "tag_name": self._clean_text(
                        row["tag_name"]
                    ),
                    "description": self._clean_text(
                        row["description"]
                    ),
                    "driver": self._clean_text(
                        row["driver"]
                    ),
                    "address": self._clean_text(
                        row["address"]
                    ),
                    "data_type": self._clean_text(
                        row["data_type"]
                    ),
                    "unit": self._clean_text(
                        row["unit"]
                    ),
                    "equipment_names": [],
                    "equipment_display_names": [],
                    "equipment_descriptions": [],
                    "relationship_types": [],
                }

            item = grouped[tag_id]

            direct_name = self._clean_text(
                row["direct_equipment_name"]
            )

            direct_display = self._clean_text(
                row[
                    "direct_equipment_display_name"
                ]
            )

            direct_description = self._clean_text(
                row[
                    "direct_equipment_description"
                ]
            )

            related_name = self._clean_text(
                row["related_equipment_name"]
            )

            related_display = self._clean_text(
                row[
                    "related_equipment_display_name"
                ]
            )

            related_description = self._clean_text(
                row[
                    "related_equipment_description"
                ]
            )

            relationship_type = self._clean_text(
                row["relationship_type"]
            )

            self._append_unique(
                item["equipment_names"],
                direct_name,
            )

            self._append_unique(
                item["equipment_names"],
                related_name,
            )

            self._append_unique(
                item["equipment_display_names"],
                direct_display,
            )

            self._append_unique(
                item["equipment_display_names"],
                related_display,
            )

            self._append_unique(
                item["equipment_descriptions"],
                direct_description,
            )

            self._append_unique(
                item["equipment_descriptions"],
                related_description,
            )

            self._append_unique(
                item["relationship_types"],
                relationship_type,
            )

        tags: list[TagKnowledge] = []

        for item in grouped.values():
            tags.append(
                TagKnowledge(
                    tag_id=item["tag_id"],
                    tag_name=item["tag_name"],
                    description=item["description"],
                    driver=item["driver"],
                    address=item["address"],
                    data_type=item["data_type"],
                    unit=item["unit"],
                    equipment_names=tuple(
                        item["equipment_names"]
                    ),
                    equipment_display_names=tuple(
                        item[
                            "equipment_display_names"
                        ]
                    ),
                    equipment_descriptions=tuple(
                        item[
                            "equipment_descriptions"
                        ]
                    ),
                    relationship_types=tuple(
                        item["relationship_types"]
                    ),
                )
            )

        return tags

    @staticmethod
    def _append_unique(
        values: list[str],
        value: str,
    ) -> None:
        if (
            value
            and value not in values
        ):
            values.append(
                value
            )

    def rebuild_index(
        self,
    ) -> int:
        """
        Reload tags from config.db and rebuild embeddings.

        Call this after adding or editing tags.
        """
        tags = self._load_tags()

        if not tags:
            self._tags = []
            self._embeddings = {}

            return 0

        semantic_texts = [
            tag.semantic_text()
            for tag in tags
        ]

        embeddings = self.embedding_client.embed(
            semantic_texts
        )

        self._tags = tags

        self._embeddings = {
            tag.tag_id: embedding
            for tag, embedding in zip(
                tags,
                embeddings,
                strict=True,
            )
        }

        return len(tags)

    @staticmethod
    def _normalize_for_exact_match(
        text: str,
    ) -> str:
        return "".join(
            character.lower()
            for character in text
            if character.isalnum()
        )

    def _find_exact_match(
        self,
        question: str,
    ) -> TagKnowledge | None:
        normalized_question = (
            self._normalize_for_exact_match(
                question
            )
        )

        if not normalized_question:
            return None

        for tag in self._tags:
            normalized_tag_name = (
                self._normalize_for_exact_match(
                    tag.tag_name
                )
            )

            if normalized_question == normalized_tag_name:
                return tag

        for tag in self._tags:
            normalized_description = (
                self._normalize_for_exact_match(
                    tag.description
                )
            )

            if (
                normalized_description
                and normalized_question
                == normalized_description
            ):
                return tag

        return None

    @staticmethod
    def cosine_similarity(
        first: list[float],
        second: list[float],
    ) -> float:
        if len(first) != len(second):
            raise ValueError(
                "Embedding dimensions do not match."
            )

        dot_product = sum(
            first_value * second_value
            for first_value, second_value in zip(
                first,
                second,
                strict=True,
            )
        )

        first_norm = math.sqrt(
            sum(
                value * value
                for value in first
            )
        )

        second_norm = math.sqrt(
            sum(
                value * value
                for value in second
            )
        )

        if (
            first_norm == 0.0
            or second_norm == 0.0
        ):
            return 0.0

        similarity = (
            dot_product
            / (first_norm * second_norm)
        )

        return max(
            -1.0,
            min(
                1.0,
                similarity,
            ),
        )

    def resolve(
        self,
        question: str,
        top_k: int = 5,
    ) -> SemanticResolution:
        """
        Resolve a question to the most likely configured tag.
        """
        clean_question = question.strip()

        if not clean_question:
            return SemanticResolution(
                question=question,
                resolved=False,
                message="The question is empty.",
            )

        if top_k < 1:
            raise ValueError(
                "top_k must be at least 1."
            )

        if not self._tags:
            return SemanticResolution(
                question=clean_question,
                resolved=False,
                message=(
                    "No enabled tags are available in "
                    "config.db."
                ),
            )

        exact_tag = self._find_exact_match(
            clean_question
        )

        if exact_tag is not None:
            exact_match = SemanticTagMatch(
                tag=exact_tag,
                similarity=1.0,
                rank=1,
                accepted=True,
                reason="Exact tag or description match.",
            )

            return SemanticResolution(
                question=clean_question,
                resolved=True,
                selected=exact_match,
                candidates=[
                    exact_match
                ],
                method="exact",
                message=(
                    f"Resolved exactly to "
                    f"{exact_tag.tag_name}."
                ),
            )

        question_embedding = (
            self.embedding_client.embed_one(
                clean_question
            )
        )

        scored: list[
            tuple[TagKnowledge, float]
        ] = []

        for tag in self._tags:
            tag_embedding = self._embeddings.get(
                tag.tag_id
            )

            if tag_embedding is None:
                continue

            similarity = self.cosine_similarity(
                question_embedding,
                tag_embedding,
            )

            scored.append(
                (
                    tag,
                    similarity,
                )
            )

        scored.sort(
            key=lambda item: item[1],
            reverse=True,
        )

        candidate_matches: list[
            SemanticTagMatch
        ] = []

        for rank, (
            tag,
            similarity,
        ) in enumerate(
            scored[:top_k],
            start=1,
        ):
            candidate_matches.append(
                SemanticTagMatch(
                    tag=tag,
                    similarity=similarity,
                    rank=rank,
                )
            )

        if not candidate_matches:
            return SemanticResolution(
                question=clean_question,
                resolved=False,
                message=(
                    "No semantic candidates were produced."
                ),
            )

        best = candidate_matches[0]

        second_score = (
            candidate_matches[1].similarity
            if len(candidate_matches) > 1
            else 0.0
        )

        score_gap = (
            best.similarity
            - second_score
        )

        accepted = False
        reason = ""

        if (
            best.similarity
            >= self.automatic_accept_score
        ):
            accepted = True
            reason = (
                "Similarity exceeded the automatic "
                "acceptance threshold."
            )

        elif (
            best.similarity
            >= self.conditional_accept_score
            and score_gap
            >= self.minimum_score_gap
        ):
            accepted = True
            reason = (
                "Similarity was acceptable and clearly "
                "higher than the second candidate."
            )

        elif (
            best.similarity
            < self.conditional_accept_score
        ):
            reason = (
                "The best similarity score was too low."
            )

        else:
            reason = (
                "The best candidate was too close to the "
                "second candidate."
            )

        updated_candidates: list[
            SemanticTagMatch
        ] = []

        for candidate in candidate_matches:
            is_selected = (
                candidate.rank == 1
                and accepted
            )

            updated_candidates.append(
                SemanticTagMatch(
                    tag=candidate.tag,
                    similarity=candidate.similarity,
                    rank=candidate.rank,
                    accepted=is_selected,
                    reason=(
                        reason
                        if candidate.rank == 1
                        else ""
                    ),
                )
            )

        selected = (
            updated_candidates[0]
            if accepted
            else None
        )

        if selected is not None:
            message = (
                f"Resolved to {selected.tag.tag_name} "
                f"with {selected.confidence_percent}% "
                f"semantic similarity."
            )

        else:
            message = (
                "No tag was accepted automatically. "
                f"Best candidate was {best.tag.tag_name} "
                f"with {best.confidence_percent}% "
                f"similarity. {reason}"
            )

        return SemanticResolution(
            question=clean_question,
            resolved=accepted,
            selected=selected,
            candidates=updated_candidates,
            method="semantic",
            message=message,
        )

    def get_tag(
        self,
        tag_name: str,
    ) -> TagKnowledge | None:
        normalized_target = (
            self._normalize_for_exact_match(
                tag_name
            )
        )

        for tag in self._tags:
            normalized_tag = (
                self._normalize_for_exact_match(
                    tag.tag_name
                )
            )

            if normalized_tag == normalized_target:
                return tag

        return None

    def list_tags(
        self,
    ) -> list[TagKnowledge]:
        return list(
            self._tags
        )


def format_semantic_resolution(
    resolution: SemanticResolution,
) -> str:
    """
    Format a semantic resolution for terminal testing.
    """
    lines = [
        f"Question: {resolution.question}",
        f"Resolved: {resolution.resolved}",
        f"Method: {resolution.method}",
        f"Message: {resolution.message}",
        "",
        "Candidates:",
    ]

    if not resolution.candidates:
        lines.append(
            "- No candidates"
        )

        return "\n".join(lines)

    for candidate in resolution.candidates:
        selected_marker = (
            " SELECTED"
            if candidate.accepted
            else ""
        )

        equipment = (
            ", ".join(
                candidate.tag.equipment_display_names
            )
            or ", ".join(
                candidate.tag.equipment_names
            )
            or "Not assigned"
        )

        lines.append(
            (
                f"{candidate.rank}. "
                f"{candidate.tag.tag_name} "
                f"- {candidate.confidence_percent}%"
                f"{selected_marker}"
            )
        )

        lines.append(
            (
                "   Description: "
                f"{candidate.tag.description or 'None'}"
            )
        )

        lines.append(
            f"   Equipment: {equipment}"
        )

        if candidate.tag.unit:
            lines.append(
                f"   Unit: {candidate.tag.unit}"
            )

        if candidate.reason:
            lines.append(
                f"   Reason: {candidate.reason}"
            )

    return "\n".join(lines)


def main() -> None:
    print(
        "Building semantic tag index..."
    )

    resolver = SemanticTagResolver()

    print(
        f"Loaded {len(resolver.list_tags())} enabled tags."
    )

    print(
        "Type a machine-data question."
    )

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

        try:
            result = resolver.resolve(
                question,
                top_k=5,
            )

            print()
            print(
                format_semantic_resolution(
                    result
                )
            )

        except Exception as exc:
            print(
                f"Semantic resolver error: {exc}"
            )


if __name__ == "__main__":
    main()
