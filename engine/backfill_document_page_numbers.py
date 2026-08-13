from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from rag.document_store import ensure_schema, extract_pdf_pages, replace_document_chunks


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()


def migrate(database: Path, backup: bool = True) -> None:
    """
    Adds page-number tracking to every already-ingested manual, so
    root-cause answers can cite "(see page 42)" instead of just naming
    the manual - added for equipment_documents/document_chunks_fts
    after the fact, since page numbers weren't captured at all when
    these were first ingested (every page's text was flattened into
    one string before storage, discarding page boundaries).

    FTS5 virtual tables don't support ALTER TABLE ADD COLUMN
    (confirmed directly - "virtual tables may not be altered"), so
    this drops and recreates document_chunks_fts with the new schema,
    then re-extracts and re-chunks every PDF-backed document from its
    stored file_path (equipment_id/brand/model/title are untouched -
    only the chunk table is rebuilt, via replace_document_chunks(),
    which keeps the same document_id). Documents with no file_path, no
    file_type == "pdf", or a file_path that no longer exists on disk
    are skipped and reported, not treated as fatal.
    """
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(
            f"{database.stem}_before_page_number_backfill_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("DROP TABLE IF EXISTS document_chunks_fts")
        ensure_schema(connection)
        connection.commit()
        print("document_chunks_fts recreated with page_number column.")

        rows = connection.execute(
            """
            SELECT id, brand, model, title, file_path, file_type
            FROM equipment_documents
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()

    processed = 0
    skipped = []
    total_chunks = 0

    for row in rows:
        if row["file_type"] != "pdf" or not row["file_path"]:
            skipped.append((row["id"], row["title"], "not a PDF or no file_path"))
            continue

        full_path = PROJECT_ROOT / row["file_path"]

        if not full_path.exists():
            skipped.append((row["id"], row["title"], f"file not found: {full_path}"))
            continue

        try:
            pages = extract_pdf_pages(str(full_path))
        except Exception as error:
            skipped.append((row["id"], row["title"], f"extraction failed: {error}"))
            continue

        chunk_count = replace_document_chunks(
            document_id=row["id"],
            pages=pages,
            database_path=database,
        )
        total_chunks += chunk_count
        processed += 1
        print(f"  [{row['id']}] {row['title']}: {chunk_count} chunks, {len(pages)} pages")

    print()
    print(f"Re-chunked {processed} document(s), {total_chunks} chunks total.")

    if skipped:
        print(f"Skipped {len(skipped)} document(s):")
        for document_id, title, reason in skipped:
            print(f"  [{document_id}] {title}: {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill page numbers onto already-ingested equipment manuals.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
