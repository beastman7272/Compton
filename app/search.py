from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config import DB_PATH
from app.db import get_connection, touch_upload
from app.pdf_text import extract_pdf_text
from app.models import SearchFilter, SearchTerm, SearchResult


DEFAULT_MATCH_LIMIT_PER_TERM = 4


@dataclass(slots=True)
class PageSearchMatch:
    filter_id: int
    term_id: int
    page_number: int
    matched_text: str
    context_text: str


def load_active_filters(conn: sqlite3.Connection) -> list[tuple[SearchFilter, list[SearchTerm]]]:
    """
    Load all active search filters and their terms.
    """
    filter_rows = conn.execute(
        """
        SELECT id, name, category, is_active
        FROM search_filters
        WHERE is_active = 1
        ORDER BY category, name
        """
    ).fetchall()

    filters: list[tuple[SearchFilter, list[SearchTerm]]] = []

    for row in filter_rows:
        search_filter = SearchFilter(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            is_active=bool(row["is_active"]),
        )

        term_rows = conn.execute(
            """
            SELECT id, filter_id, term
            FROM search_terms
            WHERE filter_id = ?
            ORDER BY term
            """,
            (search_filter.id,),
        ).fetchall()

        terms = [
            SearchTerm(
                id=term_row["id"],
                filter_id=term_row["filter_id"],
                term=term_row["term"],
            )
            for term_row in term_rows
        ]

        filters.append((search_filter, terms))

    return filters


def find_term_matches(
    text: str,
    term: str,
    context_chars: int = 250,
    max_matches: int = DEFAULT_MATCH_LIMIT_PER_TERM,
) -> list[tuple[str, str]]:
    """
    Find case-insensitive partial word/phrase matches.

    Returns:
        list of (matched_text, context_text)
    """
    if not text or not term:
        return []

    pattern = re.escape(term.strip())
    if not pattern:
        return []

    matches: list[tuple[str, str]] = []

    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        start, end = match.span()

        context_start = max(0, start - context_chars)
        context_end = min(len(text), end + context_chars)

        matched_text = text[start:end]
        context_text = clean_context(text[context_start:context_end])

        matches.append((matched_text, context_text))

        if len(matches) >= max_matches:
            break

    return matches


def clean_context(text: str) -> str:
    """
    Keep context readable for the Search Results page.
    """
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def search_pages(
    pages,
    filters: list[tuple[SearchFilter, list[SearchTerm]]],
    max_matches_per_term: int = DEFAULT_MATCH_LIMIT_PER_TERM,
) -> list[PageSearchMatch]:
    """
    Search extracted PDF pages using active filters/terms.

    The match limit applies per filter/term combination per upload, not globally.
    """
    results: list[PageSearchMatch] = []

    match_counts: dict[tuple[int, int], int] = {}

    for page in pages:
        page_text = page.text or ""

        if not page_text:
            continue

        for search_filter, terms in filters:
            for term in terms:
                key = (search_filter.id, term.id)
                current_count = match_counts.get(key, 0)

                if current_count >= max_matches_per_term:
                    continue

                remaining = max_matches_per_term - current_count

                matches = find_term_matches(
                    text=page_text,
                    term=term.term,
                    max_matches=remaining,
                )

                for matched_text, context_text in matches:
                    results.append(
                        PageSearchMatch(
                            filter_id=search_filter.id,
                            term_id=term.id,
                            page_number=page.page_number,
                            matched_text=matched_text,
                            context_text=context_text,
                        )
                    )

                match_counts[key] = current_count + len(matches)

    return results


def delete_existing_results_for_upload(
    conn: sqlite3.Connection,
    upload_id: int,
) -> None:
    """
    Clear prior search results before re-indexing an upload.
    """
    conn.execute(
        """
        DELETE FROM search_results
        WHERE upload_id = ?
        """,
        (upload_id,),
    )


def insert_search_results(
    conn: sqlite3.Connection,
    project_id: int,
    upload_id: int,
    matches: list[PageSearchMatch],
) -> list[int]:
    """
    Insert search result rows.
    """
    result_ids: list[int] = []

    for match in matches:
        cur = conn.execute(
            """
            INSERT INTO search_results (
                project_id,
                upload_id,
                filter_id,
                term_id,
                page_number,
                matched_text,
                context_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                upload_id,
                match.filter_id,
                match.term_id,
                match.page_number,
                match.matched_text,
                match.context_text,
            ),
        )

        result_ids.append(int(cur.lastrowid))

    return result_ids


def reindex_filter(
    conn: sqlite3.Connection,
    filter_id: int,
    max_matches_per_term: int = DEFAULT_MATCH_LIMIT_PER_TERM,
) -> list[int]:
    """Refresh one active filter without disturbing other filters' results."""
    row = conn.execute(
        "SELECT id, name, category, is_active FROM search_filters WHERE id = ? AND is_active = 1",
        (filter_id,),
    ).fetchone()
    if not row:
        return []

    search_filter = SearchFilter(
        id=row["id"],
        name=row["name"],
        category=row["category"],
        is_active=True,
    )
    terms = [
        SearchTerm(id=term["id"], filter_id=filter_id, term=term["term"])
        for term in conn.execute(
            "SELECT id, term FROM search_terms WHERE filter_id = ? ORDER BY term",
            (filter_id,),
        ).fetchall()
    ]
    result_ids: list[int] = []

    uploads = conn.execute(
        "SELECT id, project_id, stored_path FROM uploads WHERE LOWER(file_type) = 'pdf'"
    ).fetchall()
    for upload in uploads:
        try:
            pages = extract_pdf_text(upload["stored_path"], upload["id"]).pages
        except Exception as exc:
            print(f"Skipping upload {upload['id']} while re-indexing filter {filter_id}: {exc}")
            continue

        conn.execute(
            "DELETE FROM search_results WHERE upload_id = ? AND filter_id = ?",
            (upload["id"], filter_id),
        )
        result_ids.extend(
            insert_search_results(
                conn,
                upload["project_id"],
                upload["id"],
                search_pages(pages, [(search_filter, terms)], max_matches_per_term),
            )
        )

    return result_ids


def mark_upload_status(
    conn: sqlite3.Connection,
    upload_id: int,
    status: str,
) -> None:
    conn.execute(
        """
        UPDATE uploads
        SET upload_status = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, upload_id),
    )


def update_upload_page_count(
    conn: sqlite3.Connection,
    upload_id: int,
    page_count: int,
) -> None:
    conn.execute(
        """
        UPDATE uploads
        SET page_count = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (page_count, upload_id),
    )


def index_upload(
    conn: sqlite3.Connection,
    upload_id: int,
    max_matches_per_term: int = DEFAULT_MATCH_LIMIT_PER_TERM,
) -> list[int]:
    """
    Extract text from one upload PDF and insert search_results rows.
    """
    upload = conn.execute(
        """
        SELECT id, project_id, stored_path
        FROM uploads
        WHERE id = ?
        """,
        (upload_id,),
    ).fetchone()

    if not upload:
        raise RuntimeError(f"Upload not found: {upload_id}")

    pdf_path = Path(upload["stored_path"])

    if not pdf_path.exists():
        raise FileNotFoundError(f"Stored PDF not found: {pdf_path}")

    try:
        filters = load_active_filters(conn)

        delete_existing_results_for_upload(conn, upload_id)

        text_result = extract_pdf_text(
            pdf_path=pdf_path,
            upload_id=upload_id,
        )

        update_upload_page_count(
            conn=conn,
            upload_id=upload_id,
            page_count=text_result.page_count,
        )

        matches = search_pages(
            pages=text_result.pages,
            filters=filters,
            max_matches_per_term=max_matches_per_term,
        )

        result_ids = insert_search_results(
            conn=conn,
            project_id=upload["project_id"],
            upload_id=upload_id,
            matches=matches,
        )

        mark_upload_status(conn, upload_id, "indexed")
        touch_upload(conn, upload_id)

        return result_ids

    except Exception:
        mark_upload_status(conn, upload_id, "index_error")
        touch_upload(conn, upload_id)
        raise


def index_pending_uploads(
    db_path: Path | str = DB_PATH,
    max_uploads: int | None = None,
    max_matches_per_term: int = DEFAULT_MATCH_LIMIT_PER_TERM,
) -> None:
    """
    Index uploads with upload_status = pending_index.

    This can be called after imports or later as a background-style batch.
    """
    with get_connection(db_path) as conn:
        query = """
            SELECT id
            FROM uploads
            WHERE upload_status = 'pending_index'
            ORDER BY created_at
        """

        params: tuple = ()

        if max_uploads is not None:
            query += " LIMIT ?"
            params = (max_uploads,)

        rows = conn.execute(query, params).fetchall()

        if not rows:
            print("No pending uploads to index.")
            return

        print(f"Found {len(rows)} pending upload(s) to index.")

        for row in rows:
            upload_id = row["id"]
            print(f"Indexing upload {upload_id}...")

            try:
                result_ids = index_upload(
                    conn=conn,
                    upload_id=upload_id,
                    max_matches_per_term=max_matches_per_term,
                )
                conn.commit()
                print(f"  Indexed upload {upload_id}; search results: {len(result_ids)}")
            except Exception as exc:
                conn.commit()
                print(f"  ERROR indexing upload {upload_id}: {exc}")


def index_upload_ids(
    upload_ids: list[int],
    db_path: Path | str = DB_PATH,
    max_matches_per_term: int = DEFAULT_MATCH_LIMIT_PER_TERM,
) -> None:
    """
    Index a specific list of upload IDs.
    Useful immediately after an import result returns uploaded_file_ids.
    """
    if not upload_ids:
        return

    with get_connection(db_path) as conn:
        for upload_id in upload_ids:
            try:
                result_ids = index_upload(
                    conn=conn,
                    upload_id=upload_id,
                    max_matches_per_term=max_matches_per_term,
                )
                conn.commit()
                print(f"Indexed upload {upload_id}; search results: {len(result_ids)}")
            except Exception as exc:
                conn.commit()
                print(f"ERROR indexing upload {upload_id}: {exc}")


if __name__ == "__main__":
    index_pending_uploads()
