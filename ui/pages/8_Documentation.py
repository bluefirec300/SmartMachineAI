from __future__ import annotations

import html
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.configuration_manager import ConfigurationManager
from rag.document_store import delete_document, extract_pdf_pages, list_documents, store_document
from ui import auth
from ui.data_access import CONFIG_DATABASE_PATH, get_equipment_list


MANUALS_DIR = PROJECT_ROOT / "manuals"
UPLOAD_DIR = MANUALS_DIR / "uploaded"

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_EXTENSIONS = PDF_EXTENSIONS | IMAGE_EXTENSIONS

# ui/static is a symlink to manuals/ (see the repo - not committed,
# created once at deploy time), served by Streamlit's own static
# file server (enableStaticServing=true in .streamlit/config.toml).
# Every document's file_path in the DB is already PROJECT_ROOT-
# relative ("manuals/uploaded/xyz.pdf" or "manuals/some_manual.pdf"),
# so the browser-viewable URL is just that path with the "manuals/"
# prefix swapped for "/app/static/" - no separate copy/sync step
# needed, existing and future documents both work automatically.
STATIC_URL_PREFIX = "manuals/"


def _static_view_url(file_path: str) -> str | None:
    """
    Browser-viewable URL for a stored document, or None if it isn't
    reachable through the static mount (defensive - every current
    file_path starts with "manuals/", but a future storage location
    change shouldn't crash this page, just hide the View link).
    """
    if not file_path.startswith(STATIC_URL_PREFIX):
        return None

    relative_path = file_path[len(STATIC_URL_PREFIX):]

    return "/app/static/" + quote(relative_path)

current_user = auth.current_user()
can_edit = auth.can_edit("admin", "engineer")

st.title("📚 Documentation")
st.caption(
    "Manuals/datasheets/photos mapped to equipment. PDF and image files only. "
    "Every upload, delete, and download is recorded in the audit log below."
)

if not can_edit:
    st.info("View/download only - only Engineer and Administrator accounts can upload or delete documents.")


def _audit(action: str, entity_name: str, details: str) -> None:
    ConfigurationManager(database_path=CONFIG_DATABASE_PATH).write_audit_log(
        username=current_user["username"],
        action=action,
        entity_type="equipment_document",
        entity_name=entity_name,
        details=details,
    )


equipment_list = get_equipment_list()
equipment_options = {eq["display_name"]: eq for eq in equipment_list}

if not equipment_options:
    st.info(
        "No equipment configured yet - an Administrator needs to add some on the "
        "Equipment & Tag Configuration page first."
    )
    st.stop()

selected_name = st.selectbox("Equipment", sorted(equipment_options))
selected_equipment = equipment_options[selected_name]

st.divider()

if can_edit:
    st.subheader("Upload")

    uploaded_files = st.file_uploader(
        "Add one or more files for this equipment",
        type=sorted(ext.lstrip(".") for ext in ALLOWED_EXTENSIONS),
        accept_multiple_files=True,
        key=f"uploader_{selected_equipment['id']}",
    )

    if uploaded_files and st.button("Save uploaded file(s)", type="primary"):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        brand = selected_equipment["brand"] or selected_equipment["display_name"]
        model = selected_equipment["model"] or "unspecified"

        saved_count = 0

        for uploaded_file in uploaded_files:
            # Path(...).name strips any directory components a crafted
            # filename might carry (e.g. "../../etc/passwd") - browsers
            # normally only ever send a bare filename for a file input,
            # but that's a client-side convention, not something this
            # server-side code should rely on unverified.
            safe_name = Path(uploaded_file.name).name

            if not safe_name or safe_name in {".", ".."}:
                st.error(f"Skipped '{uploaded_file.name}' - invalid filename.")
                continue

            suffix = Path(safe_name).suffix.lower()

            if suffix not in ALLOWED_EXTENSIONS:
                st.error(f"Skipped '{uploaded_file.name}' - only PDF and image files are supported.")
                continue

            file_bytes = uploaded_file.getvalue()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S%f")
            stored_filename = f"{selected_equipment['name']}_{timestamp}_{safe_name}"
            stored_path = (UPLOAD_DIR / stored_filename).resolve()

            if UPLOAD_DIR.resolve() not in stored_path.parents:
                st.error(f"Skipped '{uploaded_file.name}' - invalid path.")
                continue
            stored_path.write_bytes(file_bytes)

            pages = None

            if suffix in PDF_EXTENSIONS:
                file_type = "pdf"
                try:
                    from io import BytesIO

                    pages = extract_pdf_pages(BytesIO(file_bytes))
                except Exception:
                    pages = None
            else:
                file_type = "image"

            result = store_document(
                brand=brand,
                model=model,
                pages=pages,
                title=Path(uploaded_file.name).stem,
                source_filename=uploaded_file.name,
                equipment_id=selected_equipment["id"],
                file_path=str(stored_path.relative_to(PROJECT_ROOT)),
                file_type=file_type,
                uploaded_by=current_user["username"],
                database_path=CONFIG_DATABASE_PATH,
            )

            _audit(
                "upload_document",
                uploaded_file.name,
                f"equipment={selected_equipment['display_name']}, document_id={result['document_id']}, "
                f"chunks={result['chunk_count']}",
            )
            saved_count += 1

        if saved_count:
            st.success(f"Saved {saved_count} file(s) for {selected_equipment['display_name']}.")
            st.rerun()

    st.divider()

st.subheader(f"Documents - {selected_equipment['display_name']}")

own_docs = list_documents(equipment_id=selected_equipment["id"], database_path=CONFIG_DATABASE_PATH)

# Pre-ingested manufacturer manuals aren't tied to one equipment_id
# (one manual covers every instance of that brand/model) - shown here
# too so engineers see everything available for this equipment, but
# not deletable from this page (see the "Reference" badge / disabled
# delete below) to avoid one equipment's page accidentally deleting a
# manual shared by other instances of the same brand/model.
shared_docs = []
if selected_equipment["brand"] and selected_equipment["model"]:
    all_matching = list_documents(database_path=CONFIG_DATABASE_PATH)
    shared_docs = [
        doc
        for doc in all_matching
        if doc["equipment_id"] is None
        and doc["brand"] == selected_equipment["brand"]
        and doc["model"] == selected_equipment["model"]
    ]

all_docs = own_docs + shared_docs

if not all_docs:
    st.caption("No documents for this equipment yet.")
else:
    docs_df = pd.DataFrame(
        [
            {
                "Title": doc["title"] or doc["source_filename"] or "(untitled)",
                "Type": (doc["file_type"] or "-").upper(),
                "Source": "Uploaded" if doc["equipment_id"] is not None else "Reference manual",
                "Uploaded": doc["uploaded_at"],
                "By": doc["uploaded_by"] or "-",
            }
            for doc in all_docs
        ]
    )
    st.dataframe(docs_df, hide_index=True, width="stretch", height=300)

    for doc in all_docs:
        label = doc["title"] or doc["source_filename"] or f"Document {doc['id']}"
        file_path = doc["file_path"]

        view_col, download_col, delete_col = st.columns([3, 3, 1])

        if file_path and (PROJECT_ROOT / file_path).exists():
            with view_col:
                view_url = _static_view_url(file_path)

                if view_url:
                    # A plain anchor, not a Streamlit widget - opens in
                    # a new browser tab (target="_blank") without a
                    # script rerun, so PDFs/images render using the
                    # browser's own built-in viewer. Not audit-logged
                    # like the download button below: a client-side-
                    # only link click never reaches Streamlit's server,
                    # so there's no on_click hook to log it from - a
                    # read-only view is also a lower-stakes action than
                    # a download or delete, so this was an acceptable
                    # trade rather than adding a JS workaround for it.
                    # label comes from a document's title/source_filename
                    # (an uploader-controlled value, even if restricted to
                    # Engineer/Admin roles) - html.escape()'d before going
                    # into raw HTML so it can't break out of the anchor
                    # tag or inject a script. view_url is already
                    # percent-encoded by _static_view_url(), escaped here
                    # too for defense in depth against the same class of
                    # injection.
                    st.markdown(
                        f'<a href="{html.escape(view_url, quote=True)}" target="_blank" rel="noopener">'
                        f"👁 View {html.escape(label)}</a>",
                        unsafe_allow_html=True,
                    )

            with download_col:
                file_bytes = (PROJECT_ROOT / file_path).read_bytes()

                st.download_button(
                    f"⬇ {label}",
                    data=file_bytes,
                    file_name=doc["source_filename"] or Path(file_path).name,
                    key=f"download_{doc['id']}",
                    on_click=_audit,
                    args=("download_document", doc["source_filename"] or label, f"document_id={doc['id']}"),
                )
        else:
            with view_col:
                st.caption(f"{label} (file not stored locally - reference-only entry)")

        with delete_col:
            deletable = can_edit and doc["equipment_id"] is not None

            if st.button("🗑 Delete", key=f"delete_{doc['id']}", disabled=not deletable):
                deleted = delete_document(doc["id"], database_path=CONFIG_DATABASE_PATH)

                if deleted and deleted["file_path"]:
                    deleted_path = PROJECT_ROOT / deleted["file_path"]
                    if deleted_path.exists():
                        deleted_path.unlink()

                _audit("delete_document", label, f"document_id={doc['id']}")
                st.success(f"Deleted {label}.")
                st.rerun()

st.divider()

with st.expander("Recent changes (audit log)"):
    audit_rows = ConfigurationManager(database_path=CONFIG_DATABASE_PATH).get_audit_log(limit=50)
    doc_audit_rows = [row for row in audit_rows if row["entity_type"] == "equipment_document"]

    if not doc_audit_rows:
        st.caption("No document activity recorded yet.")
    else:
        for row in doc_audit_rows:
            st.write(
                f"- {row['timestamp']} — **{row['action']}** `{row['entity_name']}` "
                f"({row['details']}) (by {row['username']})"
            )
