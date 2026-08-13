from __future__ import annotations
from config.environment import get_config_db_path

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

DEFAULT_CHUNK_MAX_CHARS = 800


def extract_pdf_pages(source) -> list[str]:
    """
    Per-page extracted text, in page order (index 0 = page 1) - the
    shared extraction path for app/import_manual.py (a file path) and
    ui/pages/8_Documentation.py (uploaded bytes), so both feed
    store_document() identically and page numbers mean the same thing
    regardless of which path a document came in through. `source` is
    anything pypdf.PdfReader accepts directly (a file path string/Path,
    or a BytesIO for in-memory upload bytes).

    Previously each caller flattened every page into one string with
    "\\n\\n".join(pages) before this module ever saw it, which is why
    no page number was ever recoverable for a stored chunk - fixed by
    keeping pages separate all the way through chunking.
    """
    from pypdf import PdfReader

    reader = PdfReader(source)

    return [page.extract_text() or "" for page in reader.pages]


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

    # Additive columns for the Documentation page (engineer-driven
    # upload/delete, mapped directly to one equipment_id) - the
    # original brand/model-only rows (auto-ingested manufacturer
    # manuals) keep working unchanged with these left NULL. brand/model
    # stay populated for engineer uploads too (auto-filled from the
    # chosen equipment's own brand/model), so the existing
    # rag/retrieval.py search path needs no changes to also find them.
    # Plain sqlite3.connect() here (no row_factory guaranteed by the
    # caller) - index PRAGMA table_info's tuple form (name is column 1)
    # rather than assuming Row access works.
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(equipment_documents)").fetchall()
    }

    for column, ddl_type in (
        ("equipment_id", "INTEGER"),
        ("file_path", "TEXT"),
        ("file_type", "TEXT"),
        ("uploaded_by", "TEXT"),
    ):
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE equipment_documents ADD COLUMN {column} {ddl_type}"
            )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_equipment_documents_equipment_id
        ON equipment_documents(equipment_id)
        """
    )

    # FTS5 holds the searchable content directly (no separate content
    # table) - simplest design that still gets fast, ranked (bm25())
    # keyword search with no extra dependencies. page_number is which
    # page of the source PDF this chunk came from (1-indexed), NULL for
    # chunks stored from raw --text (no page concept) - lets a root-
    # cause answer cite "page 42" instead of just naming the manual,
    # so an engineer can verify a claim in seconds instead of trusting
    # it blindly (see CLAUDE.md's "Equipment metadata & document
    # lookup" citation-fidelity finding - this is the mitigation: the
    # LLM is only ever asked to relay a page number it was actually
    # given alongside the excerpt, never to invent one).
    #
    # FTS5 virtual tables don't support ALTER TABLE ADD COLUMN
    # (confirmed directly - "virtual tables may not be altered"), so
    # this table's shape is fixed at creation. Existing installs get
    # migrated by engine/backfill_document_page_numbers.py, which drops
    # and recreates this table and re-ingests every document from its
    # stored file_path - not handled here, since "IF NOT EXISTS" can't
    # retroactively add a column to a table that already exists.
    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5
        (
            document_id UNINDEXED,
            chunk_index UNINDEXED,
            page_number UNINDEXED,
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


def _chunk_with_pages(
    pages: list[str] | None,
    text: str,
    max_chars: int,
) -> list[tuple[int | None, str]]:
    """
    (page_number, chunk_text) pairs - page_number is 1-indexed and
    always corresponds to a real PDF page when pages is given (each
    page is chunked separately, so a chunk can never straddle a page
    boundary and end up mislabeled). Falls back to the old flat-text
    behavior (page_number=None for every chunk) when only `text` is
    given - the raw --text CLI path has no page concept at all, so
    there is deliberately nothing to invent here either.
    """
    if pages is not None:
        result: list[tuple[int | None, str]] = []
        for page_number, page_text in enumerate(pages, start=1):
            if not page_text.strip():
                continue
            for chunk in chunk_text(page_text, max_chars=max_chars):
                result.append((page_number, chunk))
        return result

    if not text.strip():
        return []

    return [(None, chunk) for chunk in chunk_text(text, max_chars=max_chars)]


def store_document(
    brand: str,
    model: str,
    text: str = "",
    pages: list[str] | None = None,
    title: str = "",
    source_filename: str = "",
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
    equipment_id: int | None = None,
    file_path: str = "",
    file_type: str = "",
    uploaded_by: str = "",
) -> dict[str, Any]:
    """
    Store one document, chunked and indexed for search if it has
    extractable text.

    pages, when given (a PDF's per-page extracted text, see
    extract_pdf_pages()), takes precedence over text and lets every
    stored chunk carry the real page number it came from - preferred
    for anything PDF-derived. text is the older flat-string path,
    still used for the CLI's --text option (pasted text has no page
    concept) - chunks stored that way get page_number=None, which
    rag/retrieval.py and app/ask.py both already treat as "don't cite
    a page" rather than inventing one.

    Both text and pages may be empty/absent (e.g. an image file with
    nothing to OCR) - the equipment_documents row is still created (so
    it shows up in listings and can be downloaded/deleted), it just
    gets zero search chunks, since there's nothing to search.

    Returns {"document_id": ..., "chunk_count": ...}.
    """
    if not brand.strip() or not model.strip():
        raise ValueError("brand and model are required.")

    chunks = _chunk_with_pages(pages, text, max_chars)

    connection = sqlite3.connect(database_path)

    try:
        ensure_schema(connection)

        cursor = connection.execute(
            """
            INSERT INTO equipment_documents
            (brand, model, title, source_filename, uploaded_at,
             equipment_id, file_path, file_type, uploaded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                brand.strip(),
                model.strip(),
                title.strip() or None,
                source_filename.strip() or None,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                equipment_id,
                file_path.strip() or None,
                file_type.strip() or None,
                uploaded_by.strip() or None,
            ),
        )

        document_id = cursor.lastrowid

        for index, (page_number, chunk) in enumerate(chunks):
            connection.execute(
                """
                INSERT INTO document_chunks_fts
                (document_id, chunk_index, page_number, chunk_text)
                VALUES (?, ?, ?, ?)
                """,
                (document_id, index, page_number, chunk),
            )

        connection.commit()

    finally:
        connection.close()

    return {"document_id": document_id, "chunk_count": len(chunks)}


def replace_document_chunks(
    document_id: int,
    text: str = "",
    pages: list[str] | None = None,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
) -> int:
    """
    Re-chunks an already-stored document in place - deletes its
    existing document_chunks_fts rows and inserts fresh ones, without
    touching its equipment_documents row (brand/model/equipment_id/
    file_path all stay exactly as they were). Used by
    engine/backfill_document_page_numbers.py to add page numbers to
    documents that were ingested before page tracking existed, without
    re-doing the equipment-matching step that already succeeded the
    first time. Returns the new chunk count.
    """
    chunks = _chunk_with_pages(pages, text, max_chars)

    connection = sqlite3.connect(database_path)

    try:
        ensure_schema(connection)
        connection.execute(
            "DELETE FROM document_chunks_fts WHERE document_id = ?", (document_id,)
        )

        for index, (page_number, chunk) in enumerate(chunks):
            connection.execute(
                """
                INSERT INTO document_chunks_fts
                (document_id, chunk_index, page_number, chunk_text)
                VALUES (?, ?, ?, ?)
                """,
                (document_id, index, page_number, chunk),
            )

        connection.commit()
    finally:
        connection.close()

    return len(chunks)


def list_documents(
    equipment_id: int | None = None,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> list[dict[str, Any]]:
    """
    Documents for one equipment_id, or every document if equipment_id
    is None. Used by the Documentation page's listing/management view.
    """
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    try:
        ensure_schema(connection)

        if equipment_id is not None:
            rows = connection.execute(
                """
                SELECT * FROM equipment_documents
                WHERE equipment_id = ?
                ORDER BY uploaded_at DESC
                """,
                (equipment_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM equipment_documents ORDER BY uploaded_at DESC"
            ).fetchall()

        return [dict(row) for row in rows]
    finally:
        connection.close()


def get_document(
    document_id: int,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict[str, Any] | None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    try:
        ensure_schema(connection)

        row = connection.execute(
            "SELECT * FROM equipment_documents WHERE id = ?",
            (document_id,),
        ).fetchone()

        return dict(row) if row else None
    finally:
        connection.close()


def delete_document(
    document_id: int,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> dict[str, Any] | None:
    """
    Removes a document's row and its search chunks. Returns the
    deleted row's data (so the caller can also remove the physical
    file from disk) or None if it didn't exist.
    """
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    try:
        ensure_schema(connection)

        row = connection.execute(
            "SELECT * FROM equipment_documents WHERE id = ?",
            (document_id,),
        ).fetchone()

        if row is None:
            return None

        connection.execute(
            "DELETE FROM document_chunks_fts WHERE document_id = ?", (document_id,)
        )
        connection.execute("DELETE FROM equipment_documents WHERE id = ?", (document_id,))
        connection.commit()

        return dict(row)
    finally:
        connection.close()
