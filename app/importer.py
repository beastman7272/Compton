from __future__ import annotations

import re
import shutil
import sqlite3
from contextlib import closing
from difflib import SequenceMatcher
from pathlib import Path

from app.config import DB_PATH
from app.db import get_connection, init_db, touch_project
from app.models import ImportItem, ImportResult, ProjectMatch, StoredPdf
from app.storage import (
    DEFAULT_STORAGE_ROOT,
    ensure_project_storage_dirs,
    file_sha256,
    prepare_import_files,
    safe_name,
    unique_path,
)


DEFAULT_DB_PATH = DB_PATH


def normalize_project_name(name: str) -> str:
    """
    Normalize project names for deduping.
    """
    name = (name or "").lower().strip()
    name = re.sub(r"\*:.*$", "", name)
    name = re.sub(r"\b(addendum|revision|rev|rebid|bid invite|project)\b", " ", name)
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize_text(value: str) -> str:
    value = (value or "").lower().strip()
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def similarity(a: str, b: str) -> float:
    a_norm = normalize_project_name(a)
    b_norm = normalize_project_name(b)

    if not a_norm or not b_norm:
        return 0.0

    if a_norm == b_norm:
        return 1.0

    if a_norm in b_norm or b_norm in a_norm:
        return 0.92

    return SequenceMatcher(None, a_norm, b_norm).ratio()


def clean_blank(value: str | None) -> str:
    return (value or "").strip()


def find_existing_project(
    conn: sqlite3.Connection,
    item: ImportItem,
) -> ProjectMatch | None:
    """
    Deduping priority:

    1. ConstructConnect source_project_id exact match.
    2. Normalized project name + state/city.
    3. High-confidence fuzzy project-name match.
    """

    source_project_id = clean_blank(item.source_project_id)

    if source_project_id:
        row = conn.execute(
            """
            SELECT id
            FROM projects
            WHERE source_project_id = ?
              AND source_system = ?
            LIMIT 1
            """,
            (source_project_id, item.source_system),
        ).fetchone()

        if row:
            return ProjectMatch(
                project_id=row["id"],
                confidence=1.0,
                reason="Matched by source project ID",
            )

    normalized_name = normalize_project_name(item.project_name)
    state = clean_blank(item.state)
    city = clean_blank(item.city)

    if normalized_name:
        row = conn.execute(
            """
            SELECT id
            FROM projects
            WHERE normalized_name = ?
              AND COALESCE(state, '') = COALESCE(?, '')
              AND COALESCE(city, '') = COALESCE(?, '')
            LIMIT 1
            """,
            (normalized_name, state, city),
        ).fetchone()

        if row:
            return ProjectMatch(
                project_id=row["id"],
                confidence=0.98,
                reason="Matched by normalized name + city/state",
            )

    candidate_rows = conn.execute(
        """
        SELECT id, name, city, state
        FROM projects
        WHERE normalized_name IS NOT NULL
        """
    ).fetchall()

    best_match: ProjectMatch | None = None

    for row in candidate_rows:
        score = similarity(item.project_name, row["name"])

        same_state = bool(state and normalize_text(state) == normalize_text(row["state"]))
        same_city = bool(city and normalize_text(city) == normalize_text(row["city"]))

        if same_state:
            score += 0.04
        if same_city:
            score += 0.04

        score = min(score, 1.0)

        if score >= 0.88 and (best_match is None or score > best_match.confidence):
            best_match = ProjectMatch(
                project_id=row["id"],
                confidence=score,
                reason="Matched by fuzzy project name/location",
            )

    return best_match


def create_project(
    conn: sqlite3.Connection,
    item: ImportItem,
) -> int:
    """
    Create a new project with default status Needs Review.
    """

    cur = conn.execute(
        """
        INSERT INTO projects (
            source_system,
            source_project_id,
            source_sheet_id,
            source_tab,
            source_row,
            name,
            normalized_name,
            address_raw,
            city,
            state,
            county,
            bid_date,
            budget,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Needs Review')
        """,
        (
            item.source_system,
            clean_blank(item.source_project_id),
            item.source_sheet_id,
            item.source_tab,
            item.source_row,
            clean_blank(item.project_name),
            normalize_project_name(item.project_name),
            clean_blank(item.address_raw),
            clean_blank(item.city),
            clean_blank(item.state),
            clean_blank(item.county),
            clean_blank(item.bid_date),
            clean_blank(item.budget),
        ),
    )

    return int(cur.lastrowid)


def update_project_fill_blanks(
    conn: sqlite3.Connection,
    project_id: int,
    item: ImportItem,
) -> None:
    """
    For existing projects, only fill blank metadata fields.

    Do not overwrite user-edited status or existing project details.
    """

    existing = conn.execute(
        """
        SELECT *
        FROM projects
        WHERE id = ?
        """,
        (project_id,),
    ).fetchone()

    if not existing:
        raise RuntimeError(f"Project ID not found: {project_id}")

    updates: dict[str, str | int] = {}

    field_map = {
        "source_project_id": clean_blank(item.source_project_id),
        "source_sheet_id": item.source_sheet_id,
        "source_tab": item.source_tab,
        "source_row": item.source_row,
        "address_raw": clean_blank(item.address_raw),
        "city": clean_blank(item.city),
        "state": clean_blank(item.state),
        "county": clean_blank(item.county),
        "bid_date": clean_blank(item.bid_date),
        "budget": clean_blank(item.budget),
    }

    for field, incoming_value in field_map.items():
        existing_value = existing[field]

        if incoming_value and not clean_blank(str(existing_value or "")):
            updates[field] = incoming_value

    if not updates:
        touch_project(conn, project_id)
        return

    assignments = ", ".join(f"{field} = ?" for field in updates)
    values = list(updates.values())

    conn.execute(
        f"""
        UPDATE projects
        SET {assignments},
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        values + [project_id],
    )


def get_or_create_project(
    conn: sqlite3.Connection,
    item: ImportItem,
) -> tuple[int, bool, ProjectMatch | None]:
    """
    Return:
        project_id, created_project, match
    """

    match = find_existing_project(conn, item)

    if match:
        update_project_fill_blanks(conn, match.project_id, item)
        return match.project_id, False, match

    project_id = create_project(conn, item)
    return project_id, True, None


def upload_exists(
    conn: sqlite3.Connection,
    project_id: int,
    file_hash: str,
) -> bool:
    row = conn.execute(
        """
        SELECT id
        FROM uploads
        WHERE project_id = ?
          AND file_hash = ?
        LIMIT 1
        """,
        (project_id, file_hash),
    ).fetchone()

    return row is not None


def create_upload_record(
    conn: sqlite3.Connection,
    project_id: int,
    item: ImportItem,
    stored_pdf: StoredPdf,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO uploads (
            project_id,
            source_system,
            original_filename,
            stored_filename,
            stored_path,
            file_type,
            file_hash,
            page_count,
            upload_status
        )
        VALUES (?, ?, ?, ?, ?, 'pdf', ?, ?, 'pending_index')
        """,
        (
            project_id,
            item.source_system,
            stored_pdf.original_filename,
            stored_pdf.stored_filename,
            str(stored_pdf.stored_path),
            stored_pdf.file_hash,
            stored_pdf.page_count,
        ),
    )

    return int(cur.lastrowid)


def import_manual_attachment(
    item: ImportItem,
    attachment_file: Path,
    db_path: Path = DEFAULT_DB_PATH,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
) -> ImportResult:
    """
    Store a non-PDF manual upload without sending it through PDF indexing.

    Exact duplicates are skipped by the same project + SHA-256 rule used for
    indexed PDFs. Files with the same name but different contents are retained
    with a numbered stored filename.
    """
    attachment_file = Path(attachment_file)

    if not attachment_file.exists():
        return ImportResult(
            status="file_not_found",
            message=f"Attachment not found: {attachment_file}",
        )

    init_db(db_path)

    try:
        file_hash = file_sha256(attachment_file)

        with closing(get_connection(db_path)) as conn, conn:
            project_id, created_project, match = get_or_create_project(conn, item)

            if upload_exists(conn, project_id, file_hash):
                return ImportResult(
                    status="duplicate_file",
                    message="Attachment already exists; duplicate skipped",
                    project_id=project_id,
                    created_project=created_project,
                    matched_existing_project=match is not None,
                    skipped_files=[attachment_file.name],
                )

            dirs = ensure_project_storage_dirs(storage_root, project_id)
            stored_path = unique_path(
                dirs["attachments"] / safe_name(attachment_file.name, max_length=180)
            )
            shutil.copy2(attachment_file, stored_path)

            suffix = attachment_file.suffix.lower().lstrip(".")
            cursor = conn.execute(
                """
                INSERT INTO uploads (
                    project_id,
                    source_system,
                    original_filename,
                    stored_filename,
                    stored_path,
                    file_type,
                    file_hash,
                    page_count,
                    upload_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'not_indexed')
                """,
                (
                    project_id,
                    item.source_system,
                    attachment_file.name,
                    stored_path.name,
                    str(stored_path),
                    suffix or "file",
                    file_hash,
                ),
            )
            upload_id = int(cursor.lastrowid)
            touch_project(conn, project_id)

            return ImportResult(
                status="imported" if created_project else "updated",
                message=(
                    "Created project and saved 1 attachment"
                    if created_project
                    else "Updated existing project with 1 attachment"
                ),
                project_id=project_id,
                created_project=created_project,
                matched_existing_project=match is not None,
                uploaded_file_ids=[upload_id],
            )
    except Exception as exc:
        return ImportResult(
            status="error",
            message=str(exc),
            errors=[str(exc)],
        )


def import_project_from_source(
    item: ImportItem,
    matched_file: Path,
    db_path: Path = DEFAULT_DB_PATH,
    storage_root: Path = DEFAULT_STORAGE_ROOT,
    dry_run: bool = False,
) -> ImportResult:
    """
    Import one project from a normalized Sheet row and one matched downloaded file.

    Responsibilities:
    - Find or create project.
    - Copy original download into app storage.
    - Extract/copy PDFs into project document storage.
    - Create upload records.
    - Skip duplicate files using project_id + SHA-256 hash.

    Not responsible for:
    - Reading Google Sheets.
    - Finding the matching file in Downloads.
    - Updating Google Sheets status.
    - Running indexing/search.
    """

    matched_file = Path(matched_file)

    if not matched_file.exists():
        return ImportResult(
            status="file_not_found",
            message=f"Matched file not found: {matched_file}",
        )

    if dry_run:
        return ImportResult(
            status="imported",
            message=f"Dry run OK for {item.project_name} using {matched_file.name}",
        )

    init_db(db_path)

    try:
        with get_connection(db_path) as conn:
            project_id, created_project, match = get_or_create_project(conn, item)

            _, stored_pdfs = prepare_import_files(
                item=item,
                matched_file=matched_file,
                storage_root=storage_root,
                project_id=project_id,
            )

            uploaded_file_ids: list[int] = []
            skipped_files: list[str] = []

            for stored_pdf in stored_pdfs:
                if upload_exists(conn, project_id, stored_pdf.file_hash):
                    skipped_files.append(stored_pdf.stored_filename)
                    continue

                upload_id = create_upload_record(
                    conn=conn,
                    project_id=project_id,
                    item=item,
                    stored_pdf=stored_pdf,
                )
                uploaded_file_ids.append(upload_id)

            touch_project(conn, project_id)

            if uploaded_file_ids:
                status = "imported" if created_project else "updated"
                message = (
                    f"Created project and imported {len(uploaded_file_ids)} upload(s)"
                    if created_project
                    else f"Updated existing project with {len(uploaded_file_ids)} upload(s)"
                )
            elif skipped_files:
                status = "duplicate_file"
                message = "No new uploads created; all files were duplicates"
            else:
                status = "skipped"
                message = "No upload records created"

            return ImportResult(
                status=status,
                message=message,
                project_id=project_id,
                created_project=created_project,
                matched_existing_project=match is not None,
                uploaded_file_ids=uploaded_file_ids,
                skipped_files=skipped_files,
            )

    except Exception as exc:
        return ImportResult(
            status="error",
            message=str(exc),
            errors=[str(exc)],
        )