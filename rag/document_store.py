from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"

DEFAULT_CHUNK_MAX_CHARS = 800


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS equipment_documents
        (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,
            title TEXT,
            source_filename TEXT,
            uploaded_at TEXT NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_equipment_documents_brand_model
        ON equipment_documents(brand, model)
        """
    )

    # FTS5 holds the searchable content directly (no separate content
    # table) - simplest design that still gets fast, ranked (bm25())
    # keyword search with no extra dependencies.
    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5
        (
            document_id UNINDEXED,
            chunk_index UNINDEXED,
            chunk_text
        )
        """
    )


def chunk_text(
    text: str,
    max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
) -> list[str]:
    """
    Split text into paragraph-respecting chunks of roughly max_chars.

    Paragraphs are merged up to the limit rather than split
    arbitrarily, so a chunk reads as a coherent excerpt rather than a
    fixed-size slice cut mid-sentence. A single paragraph longer than
    max_chars is hard-split as a fallback.
    """
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]

    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""

            for start in range(0, len(paragraph), max_chars):
                chunks.append(paragraph[start:start + max_chars])

            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph

        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)

    return chunks


def store_document(
    brand: str,
    model: str,
    text: str,
    title: str = "",
    source_filename: str = "",
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
) -> dict[str, Any]:
    """
    Store one document's text, chunked and indexed for search.

    Returns {"document_id": ..., "chunk_count": ...}.
    """
    if not brand.strip() or not model.strip():
        raise ValueError("brand and model are required.")

    if not text.strip():
        raise ValueError("Document text cannot be empty.")

    chunks = chunk_text(text, max_chars=max_chars)

    connection = sqlite3.connect(database_path)

    try:
        ensure_schema(connection)

        cursor = connection.execute(
            """
            INSERT INTO equipment_documents
            (brand, model, title, source_filename, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                brand.strip(),
                model.strip(),
                title.strip() or None,
                source_filename.strip() or None,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

        document_id = cursor.lastrowid

        for index, chunk in enumerate(chunks):
            connection.execute(
                """
                INSERT INTO document_chunks_fts
                (document_id, chunk_index, chunk_text)
                VALUES (?, ?, ?)
                """,
                (document_id, index, chunk),
            )

        connection.commit()

    finally:
        connection.close()

    return {"document_id": document_id, "chunk_count": len(chunks)}
