from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.db import get_connection, init_db
from app.project_deletion import delete_projects_and_storage
from app.storage import ensure_project_storage_dirs, project_storage_dir


class ProjectDeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "cqe.db"
        self.storage_root = self.root / "uploads"
        self.downloads_dir = self.root / "downloads"
        self.downloads_dir.mkdir()
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_project_with_related_data(self) -> int:
        with closing(get_connection(self.db_path)) as conn, conn:
            project_cursor = conn.execute(
                """
                INSERT INTO projects (
                    source_system,
                    name,
                    normalized_name,
                    state
                )
                VALUES ('Manual', 'Delete Me', 'delete me', 'Georgia')
                """
            )
            project_id = int(project_cursor.lastrowid)

            upload_cursor = conn.execute(
                """
                INSERT INTO uploads (
                    project_id,
                    source_system,
                    original_filename,
                    stored_filename,
                    stored_path,
                    file_type,
                    file_hash,
                    upload_status
                )
                VALUES (?, 'Manual', 'plans.pdf', 'plans.pdf', 'unused', 'pdf', 'hash-1', 'indexed')
                """,
                (project_id,),
            )
            upload_id = int(upload_cursor.lastrowid)

            filter_cursor = conn.execute(
                """
                INSERT INTO search_filters (name, category)
                VALUES ('Deletion Test Filter', 'Test')
                """
            )
            filter_id = int(filter_cursor.lastrowid)
            term_cursor = conn.execute(
                """
                INSERT INTO search_terms (filter_id, term)
                VALUES (?, 'test term')
                """,
                (filter_id,),
            )
            term_id = int(term_cursor.lastrowid)

            conn.execute(
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
                VALUES (?, ?, ?, ?, 1, 'test term', 'context')
                """,
                (project_id, upload_id, filter_id, term_id),
            )
            conn.execute(
                """
                INSERT INTO project_contacts (
                    project_id,
                    contact_type,
                    organization
                )
                VALUES (?, 'Architect', 'Example Architects')
                """,
                (project_id,),
            )

        return project_id

    def test_deletes_database_records_and_project_storage_only(self) -> None:
        project_id = self.create_project_with_related_data()
        project_dirs = ensure_project_storage_dirs(self.storage_root, project_id)
        for name, directory in project_dirs.items():
            if name != "base":
                (directory / f"{name}.txt").write_text(name, encoding="utf-8")

        download_file = self.downloads_dir / "source-download.zip"
        download_file.write_bytes(b"source")

        deleted_ids = delete_projects_and_storage(
            [project_id, project_id, 999999],
            db_path=self.db_path,
            storage_root=self.storage_root,
        )

        self.assertEqual(deleted_ids, [project_id])
        self.assertFalse(project_storage_dir(self.storage_root, project_id).exists())
        self.assertTrue(download_file.exists())

        with closing(get_connection(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM uploads WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM search_results WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM project_contacts WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
