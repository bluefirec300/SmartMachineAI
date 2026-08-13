from __future__ import annotations

import argparse
from pathlib import Path

from rag.document_store import DEFAULT_DATABASE_PATH, extract_pdf_pages, store_document


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import a manufacturer manual/datasheet for one equipment "
            "brand and model, so root-cause answers can cite it."
        ),
    )

    parser.add_argument(
        "--brand",
        required=True,
        help='e.g. "Atlas Copco" - must match equipment.brand exactly.',
    )

    parser.add_argument(
        "--model",
        required=True,
        help='e.g. "GA30+" - must match equipment.model exactly.',
    )

    parser.add_argument(
        "--file",
        help="Path to a PDF file to extract text from.",
    )

    parser.add_argument(
        "--text",
        help="Raw text content, as an alternative to --file.",
    )

    parser.add_argument(
        "--title",
        default="",
        help="Optional human-readable title (defaults to the filename).",
    )

    parser.add_argument(
        "--database",
        default=str(DEFAULT_DATABASE_PATH),
    )

    args = parser.parse_args()

    if bool(args.file) == bool(args.text):
        parser.error("Provide exactly one of --file or --text.")

    pages = None

    if args.file:
        path = Path(args.file)

        if not path.exists():
            parser.error(f"File not found: {path}")

        if path.suffix.lower() == ".pdf":
            pages = extract_pdf_pages(str(path))
            text = ""
        else:
            text = path.read_text(encoding="utf-8")

        source_filename = path.name
        title = args.title or path.stem
    else:
        text = args.text
        source_filename = ""
        title = args.title

    if not (pages and any(p.strip() for p in pages)) and not text.strip():
        parser.error(
            "No extractable text was found - for a scanned/image-only "
            "PDF, this won't work without OCR, which isn't supported."
        )

    result = store_document(
        brand=args.brand,
        model=args.model,
        text=text,
        pages=pages,
        title=title,
        source_filename=source_filename,
        database_path=args.database,
    )

    print(
        f"Stored document {result['document_id']} for "
        f"{args.brand} {args.model}: {result['chunk_count']} chunks."
    )


if __name__ == "__main__":
    main()
