from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.db import get_connection
from app.importer import import_manual_attachment
from app.models import ImportItem


class ManualAttachmentImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "app.db"
        self.storage_root = self.root / "uploads"
        self.source_path = self.root / "scope-notes.docx"
        self.item = ImportItem(
            source_system="Manual",
            source_sheet_id="",
            source_tab="",
            source_row=0,
            project_name="Library Renovation",
            city="Atlanta",
            state="Georgia",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_non_pdf_is_saved_as_unindexed_attachment(self) -> None:
        self.source_path.write_bytes(b"first attachment")

        result = import_manual_attachment(
            item=self.item,
            attachment_file=self.source_path,
            db_path=self.db_path,
            storage_root=self.storage_root,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.uploaded_file_ids), 1)

        with closing(get_connection(self.db_path)) as conn:
            upload = conn.execute("SELECT * FROM uploads").fetchone()

        self.assertEqual(upload["file_type"], "docx")
        self.assertEqual(upload["upload_status"], "not_indexed")
        self.assertEqual(upload["original_filename"], "scope-notes.docx")
        stored_path = Path(upload["stored_path"])
        self.assertEqual(stored_path.read_bytes(), b"first attachment")
        self.assertEqual(stored_path.parent.name, "attachments")

    def test_exact_duplicate_is_skipped_but_changed_file_is_retained(self) -> None:
        self.source_path.write_bytes(b"version one")
        first = import_manual_attachment(
            item=self.item,
            attachment_file=self.source_path,
            db_path=self.db_path,
            storage_root=self.storage_root,
        )
        duplicate = import_manual_attachment(
            item=self.item,
            attachment_file=self.source_path,
            db_path=self.db_path,
            storage_root=self.storage_root,
        )

        self.source_path.write_bytes(b"version two")
        changed = import_manual_attachment(
            item=self.item,
            attachment_file=self.source_path,
            db_path=self.db_path,
            storage_root=self.storage_root,
        )

        self.assertTrue(first.ok)
        self.assertEqual(duplicate.status, "duplicate_file")
        self.assertEqual(duplicate.skipped_files, ["scope-notes.docx"])
        self.assertTrue(changed.ok)

        with closing(get_connection(self.db_path)) as conn:
            uploads = conn.execute(
                "SELECT stored_filename FROM uploads ORDER BY id"
            ).fetchall()

        self.assertEqual(
            [row["stored_filename"] for row in uploads],
            ["scope-notes.docx", "scope-notes_002.docx"],
        )


if __name__ == "__main__":
    unittest.main()
