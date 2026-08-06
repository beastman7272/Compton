from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import fitz

from app.db import get_connection, init_db
from app.pdf_subset import build_selected_pages_pdf


class SelectedPagesPdfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "cqe.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_project(self, name: str) -> int:
        with closing(get_connection(self.db_path)) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO projects (
                    source_system,
                    name,
                    normalized_name
                )
                VALUES ('Manual', ?, ?)
                """,
                (name, name.lower()),
            )
            return int(cursor.lastrowid)

    def create_pdf_upload(
        self,
        project_id: int,
        filename: str,
        page_labels: list[str],
    ) -> int:
        pdf_path = self.root / filename
        document = fitz.open()

        try:
            for label in page_labels:
                page = document.new_page()
                page.insert_text((72, 72), label)
            document.save(pdf_path)
        finally:
            document.close()

        with closing(get_connection(self.db_path)) as conn, conn:
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
                    upload_status
                )
                VALUES (?, 'Manual', ?, ?, ?, 'pdf', ?, 'indexed')
                """,
                (
                    project_id,
                    filename,
                    filename,
                    str(pdf_path),
                    f"hash-{project_id}-{filename}",
                ),
            )
            return int(cursor.lastrowid)

    def test_combines_selected_pages_in_order_and_removes_duplicates(self) -> None:
        project_id = self.create_project("Library Renovation")
        plans_id = self.create_pdf_upload(
            project_id,
            "plans.pdf",
            ["Plans page 1", "Plans page 2", "Plans page 3"],
        )
        specs_id = self.create_pdf_upload(
            project_id,
            "specs.pdf",
            ["Specs page 1", "Specs page 2"],
        )

        pdf_bytes = build_selected_pages_pdf(
            [
                (plans_id, 2),
                (specs_id, 1),
                (plans_id, 2),
            ],
            db_path=self.db_path,
        )

        with fitz.open(stream=pdf_bytes, filetype="pdf") as result:
            self.assertEqual(result.page_count, 2)
            self.assertIn("Plans page 2", result[0].get_text())
            self.assertIn("Specs page 1", result[1].get_text())

    def test_rejects_pages_from_different_projects(self) -> None:
        first_project_id = self.create_project("First Project")
        second_project_id = self.create_project("Second Project")
        first_upload_id = self.create_pdf_upload(
            first_project_id,
            "first.pdf",
            ["First"],
        )
        second_upload_id = self.create_pdf_upload(
            second_project_id,
            "second.pdf",
            ["Second"],
        )

        with self.assertRaisesRegex(
            ValueError,
            "same project",
        ):
            build_selected_pages_pdf(
                [(first_upload_id, 1), (second_upload_id, 1)],
                db_path=self.db_path,
            )

    def test_rejects_page_outside_document(self) -> None:
        project_id = self.create_project("Page Bounds")
        upload_id = self.create_pdf_upload(
            project_id,
            "bounds.pdf",
            ["Only page"],
        )

        with self.assertRaisesRegex(
            ValueError,
            "outside",
        ):
            build_selected_pages_pdf(
                [(upload_id, 2)],
                db_path=self.db_path,
            )


if __name__ == "__main__":
    unittest.main()
