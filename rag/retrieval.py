from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from rag.document_store import DEFAULT_DATABASE_PATH


DEFAULT_RESULT_LIMIT = 3
MIN_QUERY_WORD_LENGTH = 3


def _build_fts_query(text: str) -> str:
    """
    Turn free text into a safe FTS5 MATCH query.

    Extracts plain words only (drops FTS5 special characters like
    quotes/parens/hyphens that could otherwise break the query or
    behave unexpectedly) and OR's them together, since a short manual
    excerpt is unlikely to contain every search term but ranking
    (bm25) still surfaces the best-matching chunks first.
    """
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    words = [word for word in words if len(word) >= MIN_QUERY_WORD_LENGTH]

    if not words:
        return ""

    unique_words = list(dict.fromkeys(words))

    return " OR ".join(f'"{word}"' for word in unique_words)


def search_chunks(
    brand: str,
    model: str,
    query_text: str,
    limit: int = DEFAULT_RESULT_LIMIT,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> list[dict[str, Any]]:
    """
    Search stored documentation for one brand/model, ranked by
    relevance to query_text.

    Returns an empty list (never raises) whenever there's nothing
    useful to return - no schema yet, no documents for this brand/
    model, or an empty/unusable query - so callers can unconditionally
    skip the documentation section rather than handling a dozen edge
    cases themselves.
    """
    if not brand.strip() or not model.strip():
        return []

    fts_query = _build_fts_query(query_text)

    if not fts_query:
        return []

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    try:
        table_exists = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'document_chunks_fts'
            """
        ).fetchone()

        if not table_exists:
            return []

        document_rows = connection.execute(
            """
            SELECT id, title, source_filename
            FROM equipment_documents
            WHERE brand = ? AND model = ?
            """,
            (brand.strip(), model.strip()),
        ).fetchall()

        if not document_rows:
            return []

        documents_by_id = {row["id"]: row for row in document_rows}
        placeholders = ",".join("?" for _ in document_rows)

        rows = connection.execute(
            f"""
            SELECT document_id, page_number, chunk_text
            FROM document_chunks_fts
            WHERE document_chunks_fts MATCH ?
              AND document_id IN ({placeholders})
            ORDER BY bm25(document_chunks_fts)
            LIMIT ?
            """,
            (fts_query, *documents_by_id.keys(), limit),
        ).fetchall()

    finally:
        connection.close()

    results = []

    for row in rows:
        document = documents_by_id[row["document_id"]]

        source = (
            document["title"]
            or document["source_filename"]
            or f"{brand} {model} documentation"
        )

        results.append(
            {
                "chunk_text": row["chunk_text"],
                "source": source,
                # None for chunks stored before page tracking existed,
                # or from the raw --text CLI path (no page concept) -
                # app/ask.py only ever tells the model to cite a page
                # number when one is actually present, never invents one.
                "page_number": row["page_number"],
            }
        )

    return results
