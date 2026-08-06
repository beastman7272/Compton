from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Iterable

from app.config import DB_PATH, STORAGE_ROOT
from app.db import get_connection
from app.storage import delete_project_storage


def delete_projects_and_storage(
    project_ids: Iterable[int],
    db_path: Path | str = DB_PATH,
    storage_root: Path = STORAGE_ROOT,
) -> list[int]:
    """
    Delete existing projects from SQLite, then remove their app-owned files.

    Source files in the downloads directory are intentionally not touched.
    Returns the project IDs that existed and were deleted.
    """
    unique_ids = list(dict.fromkeys(project_ids))

    if not unique_ids:
        return []

    placeholders = ", ".join("?" for _ in unique_ids)

    with closing(get_connection(db_path)) as conn, conn:
        existing_rows = conn.execute(
            f"""
            SELECT id
            FROM projects
            WHERE id IN ({placeholders})
            """,
            unique_ids,
        ).fetchall()
        existing_ids = [int(row["id"]) for row in existing_rows]

        if not existing_ids:
            return []

        existing_placeholders = ", ".join("?" for _ in existing_ids)

        conn.execute(
            f"""
            DELETE FROM project_contacts
            WHERE project_id IN ({existing_placeholders})
            """,
            existing_ids,
        )
        conn.execute(
            f"""
            DELETE FROM search_results
            WHERE project_id IN ({existing_placeholders})
            """,
            existing_ids,
        )
        conn.execute(
            f"""
            DELETE FROM uploads
            WHERE project_id IN ({existing_placeholders})
            """,
            existing_ids,
        )
        conn.execute(
            f"""
            DELETE FROM projects
            WHERE id IN ({existing_placeholders})
            """,
            existing_ids,
        )

    for project_id in existing_ids:
        delete_project_storage(storage_root, project_id)

    return existing_ids
