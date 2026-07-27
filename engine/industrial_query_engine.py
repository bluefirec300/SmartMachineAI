from __future__ import annotations
import sqlite3, time
from difflib import SequenceMatcher
from pathlib import Path
from .concept_extractor import ConceptExtractor, normalize
from .models import Tag, Candidate, QueryResult

def tokens(value: object) -> set[str]:
    return set(normalize(value).split())

def overlap(left: object, right: object) -> float:
    a, b = tokens(left), tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0

class IndustrialQueryEngine:
    REQUIRED = {"measurement","location","signal_type","event_type","threshold_type"}

    def __init__(self, database_path: str | Path = "database/config.db") -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        if not self.database_path.exists():
            raise FileNotFoundError(f"Configuration database not found: {self.database_path}")
        self.extractor = ConceptExtractor()
        self._check_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.database_path)
        con.row_factory = sqlite3.Row
        return con

    def _check_schema(self) -> None:
        with self._connect() as con:
            columns = {row["name"] for row in con.execute("PRAGMA table_info(tags)")}
        if not self.REQUIRED.issubset(columns):
            raise RuntimeError("Run this first: python -m engine.metadata_migrator")

    def _tags(self) -> list[Tag]:
        with self._connect() as con:
            rows = con.execute("""
                SELECT t.id, t.tag_name, COALESCE(t.description,'') description,
                       COALESCE(ta.driver,t.driver,'') driver,
                       COALESCE(ta.address,t.address,'') address,
                       COALESCE(t.data_type,'') data_type,
                       COALESCE(t.unit,'') unit,
                       COALESCE(e.name,'') equipment_name,
                       COALESCE(e.display_name,'') equipment_display_name,
                       COALESCE(t.measurement,'') measurement,
                       COALESCE(t.location,'') location,
                       COALESCE(t.signal_type,'') signal_type,
                       COALESCE(t.event_type,'') event_type,
                       COALESCE(t.threshold_type,'') threshold_type
                FROM tags t
                LEFT JOIN equipment e ON e.id=t.equipment_id
                LEFT JOIN tag_addresses ta ON ta.tag_id=t.id AND ta.enabled=1
                WHERE t.enabled=1 ORDER BY t.tag_name
            """).fetchall()
            alias_rows = con.execute("""
                SELECT equipment_id, alias FROM equipment_aliases
                ORDER BY equipment_id, alias
            """).fetchall()

        aliases: dict[int, list[str]] = {}
        for row in alias_rows:
            aliases.setdefault(int(row["equipment_id"]), []).append(row["alias"])

        return [
            Tag(
                id=row["id"], tag_name=row["tag_name"], description=row["description"],
                driver=row["driver"], address=row["address"], data_type=row["data_type"],
                unit=row["unit"], equipment_name=row["equipment_name"],
                equipment_display_name=row["equipment_display_name"],
                aliases=tuple(aliases.get(row["id"], [])),
                measurement=row["measurement"], location=row["location"],
                signal_type=row["signal_type"], event_type=row["event_type"],
                threshold_type=row["threshold_type"],
            )
            for row in rows
        ]

    def _equipment_score(self, terms: tuple[str, ...], tag: Tag) -> tuple[float, str]:
        if not terms:
            return 0.0, ""
        q = " ".join(terms)
        best, label = 0.0, ""
        for name in (tag.equipment_name, tag.equipment_display_name, *tag.aliases):
            if not name:
                continue
            score = max(
                overlap(q, name),
                SequenceMatcher(None, normalize(q), normalize(name)).ratio() * 0.75,
            )
            if score > best:
                best, label = score, name
        return best, label

    def _rank(self, concepts, tag: Tag) -> Candidate:
        score, reasons = 0.0, []
        eq_score, eq_name = self._equipment_score(concepts.equipment_terms, tag)
        score += eq_score * 40
        if eq_score:
            reasons.append(f"equipment={eq_name}:{eq_score:.2f}")

        if concepts.measurement:
            if normalize(tag.measurement) == concepts.measurement:
                score += 30; reasons.append("measurement exact")
            elif concepts.measurement in normalize(f"{tag.tag_name} {tag.description}"):
                score += 18; reasons.append("measurement text")
            else:
                score -= 18; reasons.append("measurement mismatch")

        if concepts.location:
            if normalize(tag.location) == concepts.location:
                score += 16; reasons.append("location exact")
            elif concepts.location in normalize(f"{tag.tag_name} {tag.description}"):
                score += 8; reasons.append("location text")
            elif tag.location:
                score -= 6; reasons.append("location mismatch")

        if concepts.event_type:
            if normalize(tag.event_type) == concepts.event_type:
                score += 14; reasons.append("event exact")
            elif concepts.event_type in normalize(f"{tag.tag_name} {tag.description}"):
                score += 7; reasons.append("event text")
            else:
                score -= 8; reasons.append("event mismatch")

        if concepts.threshold_type:
            if normalize(tag.threshold_type) == concepts.threshold_type:
                score += 10; reasons.append("threshold exact")
            elif tag.threshold_type:
                score -= 5; reasons.append("threshold mismatch")

        lex = overlap(concepts.normalized, f"{tag.tag_name} {tag.description} {tag.equipment_display_name}")
        score += lex * 10
        if lex:
            reasons.append(f"lexical={lex:.2f}")
        return Candidate(tag, round(score, 3), tuple(reasons))

    def query(self, question: str, limit: int = 5) -> QueryResult:
        start = time.perf_counter()
        concepts = self.extractor.extract(question)
        ranked = sorted(
            (self._rank(concepts, tag) for tag in self._tags()),
            key=lambda c: c.score,
            reverse=True,
        )[:max(1, limit)]

        top = ranked[0] if ranked else None
        second = ranked[1].score if len(ranked) > 1 else -999.0
        gap = top.score - second if top else 0.0

        if not top or top.score < 18:
            status, confidence = "no_match", 0.30
            message = "No configured tag matches the question reliably."
            selected = None
        elif len(ranked) > 1 and gap < 7:
            status, confidence = "clarification_required", 0.60
            selected = None
            message = "Similar candidates: " + ", ".join(c.tag.tag_name for c in ranked[:3])
        elif concepts.measurement and top.tag.measurement and concepts.measurement != top.tag.measurement:
            status, confidence = "clarification_required", 0.50
            selected = None
            message = "The best candidate does not exactly match the requested measurement."
        else:
            status = "resolved"
            confidence = min(0.99, 0.45 + max(top.score, 0) / 160 + min(max(gap, 0), 30) / 150)
            selected = top.tag
            message = "A configured tag was resolved without an LLM."

        return QueryResult(
            question=concepts.question,
            status=status,
            intent=concepts.intent,
            time_expression=concepts.time_expression,
            selected_tag=selected.tag_name if selected else "",
            equipment=selected.equipment_name if selected else "",
            measurement=selected.measurement if selected else concepts.measurement,
            location=selected.location if selected else concepts.location,
            condition=concepts.condition,
            confidence=round(confidence, 3),
            message=message,
            candidates=ranked,
            concepts=concepts,
            elapsed_seconds=round(time.perf_counter() - start, 6),
        )
