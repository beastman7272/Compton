from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import app_web
from app.db import get_connection, init_db


class ProjectsSearchFlagTests(unittest.TestCase):
    def test_projects_page_flags_projects_without_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.db"
            init_db(db_path)
            with closing(get_connection(db_path)) as conn, conn:
                matched = conn.execute(
                    "INSERT INTO projects (source_system, name, normalized_name) VALUES ('Manual', 'Matched', 'matched')"
                ).lastrowid
                conn.execute(
                    "INSERT INTO projects (source_system, name, normalized_name) VALUES ('Manual', 'Unmatched', 'unmatched')"
                )
                upload = conn.execute(
                    """INSERT INTO uploads (project_id, source_system, original_filename, stored_filename,
                    stored_path, file_type, file_hash) VALUES (?, 'Manual', 'a.pdf', 'a.pdf', 'a.pdf', 'pdf', 'hash')""",
                    (matched,),
                ).lastrowid
                search_filter = conn.execute(
                    "INSERT INTO search_filters (name, category) VALUES ('Filter', 'Test')"
                ).lastrowid
                term = conn.execute(
                    "INSERT INTO search_terms (filter_id, term) VALUES (?, 'term')",
                    (search_filter,),
                ).lastrowid
                conn.execute(
                    """INSERT INTO search_results (project_id, upload_id, filter_id, term_id,
                    page_number, matched_text) VALUES (?, ?, ?, ?, 1, 'term')""",
                    (matched, upload, search_filter, term),
                )

            original_db_path = app_web.DB_PATH
            try:
                app_web.DB_PATH = db_path
                response = app_web.create_app().test_client().get("/projects")
            finally:
                app_web.DB_PATH = original_db_path

            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Matches Found", html)
            self.assertIn("No Matches", html)


if __name__ == "__main__":
    unittest.main()
