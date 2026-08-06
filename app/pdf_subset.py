from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Iterable

from app import config
from app.db import get_connection


MAX_SELECTED_PAGES = 500


def build_selected_pages_pdf(
    selections: Iterable[tuple[int, int]],
    db_path: Path | str,
) -> bytes:
    """
    Return one PDF containing the selected upload pages in the supplied order.

    All uploads must be PDFs from the same project. Page numbers are 1-based.
    """
    normalized_selections: list[tuple[int, int]] = []
    seen_selections: set[tuple[int, int]] = set()

    for upload_id, page_number in selections:
        if (
            isinstance(upload_id, bool)
            or isinstance(page_number, bool)
            or not isinstance(upload_id, int)
            or not isinstance(page_number, int)
            or upload_id <= 0
            or page_number <= 0
        ):
            raise ValueError("Each selected page must have a valid upload and page number.")

        selection = (upload_id, page_number)
        if selection not in seen_selections:
            normalized_selections.append(selection)
            seen_selections.add(selection)

    if not normalized_selections:
        raise ValueError("Select at least one page to download.")

    if len(normalized_selections) > MAX_SELECTED_PAGES:
        raise ValueError(
            f"Select no more than {MAX_SELECTED_PAGES} pages per download."
        )

    upload_ids = list(dict.fromkeys(
        upload_id for upload_id, _ in normalized_selections
    ))
    placeholders = ", ".join("?" for _ in upload_ids)

    with closing(get_connection(db_path)) as conn:
        upload_rows = conn.execute(
            f"""
            SELECT id, project_id, stored_path, file_type
            FROM uploads
            WHERE id IN ({placeholders})
            """,
            upload_ids,
        ).fetchall()

    uploads_by_id = {int(row["id"]): row for row in upload_rows}

    if len(uploads_by_id) != len(upload_ids):
        raise ValueError("One or more selected documents no longer exist.")

    project_ids = {int(row["project_id"]) for row in upload_rows}
    if len(project_ids) != 1:
        raise ValueError("Selected pages must belong to the same project.")

    if any(row["file_type"].lower() != "pdf" for row in upload_rows):
        raise ValueError("Only PDF pages can be included in this download.")

    try:
        import fitz
    except ImportError as exc:
        raise ImportError(
            "Missing dependency: PyMuPDF. Install it with: pip install pymupdf"
        ) from exc

    source_documents: dict[int, object] = {}
    output_document = fitz.open()

    try:
        for upload_id, page_number in normalized_selections:
            source_document = source_documents.get(upload_id)

            if source_document is None:
                upload = uploads_by_id[upload_id]
                file_path = config.resolve_project_path(upload["stored_path"])

                if not file_path.exists():
                    raise FileNotFoundError(
                        f"Selected PDF not found: {file_path.name}"
                    )

                source_document = fitz.open(file_path)
                source_documents[upload_id] = source_document

            if page_number > source_document.page_count:
                raise ValueError(
                    f"Page {page_number} is outside the selected document."
                )

            zero_based_page = page_number - 1
            output_document.insert_pdf(
                source_document,
                from_page=zero_based_page,
                to_page=zero_based_page,
            )

        return output_document.tobytes(garbage=4, deflate=True)
    finally:
        output_document.close()
        for source_document in source_documents.values():
            source_document.close()
