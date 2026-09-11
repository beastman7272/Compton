from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import fitz

import app_web
from app.db import get_connection, init_db
from app.reindex_jobs import process_queued_jobs
from app.search import index_upload


class SearchFilterEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.pdf_path = Path(self.temp_dir.name) / "plans.pdf"
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "Fabral R Panel Hefti-Rib Bilco Fortress")
            document.save(self.pdf_path)

        init_db(self.db_path)
        with closing(get_connection(self.db_path)) as conn, conn:
            self.project_id = conn.execute(
                "INSERT INTO projects (source_system, name, normalized_name) VALUES ('Manual', 'Station', 'station')"
            ).lastrowid
            self.upload_id = conn.execute(
                """INSERT INTO uploads (project_id, source_system, original_filename,
                stored_filename, stored_path, file_type, file_hash, upload_status)
                VALUES (?, 'Manual', 'plans.pdf', 'plans.pdf', ?, 'pdf', 'hash', 'indexed')""",
                (self.project_id, str(self.pdf_path)),
            ).lastrowid
            self.fabral_id = self.add_filter(conn, "Fabral", "Roofing", ["Fabral", "R Panel"])
            self.fencing_id = self.add_filter(conn, "Fencing", "Sitework", ["Fortress"])
            self.bilco_id = self.add_filter(conn, "Bilco", "Openings", ["Bilco"])
            index_upload(conn, self.upload_id)

        self.original_db_path = app_web.DB_PATH
        app_web.DB_PATH = self.db_path
        self.client = app_web.create_app().test_client()

    def tearDown(self) -> None:
        app_web.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def add_filter(conn, name: str, category: str, terms: list[str]) -> int:
        filter_id = conn.execute(
            "INSERT INTO search_filters (name, category) VALUES (?, ?)",
            (name, category),
        ).lastrowid
        conn.executemany(
            "INSERT INTO search_terms (filter_id, term) VALUES (?, ?)",
            [(filter_id, term) for term in terms],
        )
        return int(filter_id)

    def test_term_edit_saves_before_background_reindex(self) -> None:
        with closing(get_connection(self.db_path)) as conn:
            original_term_id = conn.execute(
                "SELECT id FROM search_terms WHERE filter_id = ? AND term = 'Fabral'",
                (self.fabral_id,),
            ).fetchone()["id"]

        response = self.client.post(
            f"/search-filters/{self.fabral_id}/edit",
            data={
                "name": "Fabral",
                "category": "Roofing",
                "terms": "Fabral\nHefti-Rib",
                "is_active": "1",
            },
        )
        self.assertEqual(response.status_code, 302)

        with closing(get_connection(self.db_path)) as conn:
            terms = conn.execute(
                "SELECT id, term FROM search_terms WHERE filter_id = ? ORDER BY term",
                (self.fabral_id,),
            ).fetchall()
            job = conn.execute(
                "SELECT id, status FROM search_reindex_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()

        self.assertEqual([row["term"] for row in terms], ["Fabral", "Hefti-Rib"])
        self.assertEqual(terms[0]["id"], original_term_id)
        self.assertEqual(job["status"], "queued")
        self.assertIn(
            "Filter updated. Search Results are being refreshed.",
            self.client.get("/search-filters").get_data(as_text=True),
        )

        process_queued_jobs(self.db_path)
        with closing(get_connection(self.db_path)) as conn:
            matches = conn.execute(
                """SELECT sf.name, st.term FROM search_results sr
                JOIN search_filters sf ON sf.id = sr.filter_id
                JOIN search_terms st ON st.id = sr.term_id
                ORDER BY sf.name, st.term"""
            ).fetchall()
            status = conn.execute(
                "SELECT status FROM search_reindex_jobs WHERE id = ?",
                (job["id"],),
            ).fetchone()["status"]

        self.assertEqual(status, "completed")
        self.assertEqual(
            {(row["name"], row["term"]) for row in matches},
            {("Bilco", "Bilco"), ("Fabral", "Fabral"), ("Fabral", "Hefti-Rib"), ("Fencing", "Fortress")},
        )
        self.assertIn(
            "Search Results Updated.",
            self.client.get("/search-filters").get_data(as_text=True),
        )

    def test_failed_reindex_shows_retry_action(self) -> None:
        response = self.client.post(
            f"/search-filters/{self.fabral_id}/edit",
            data={
                "name": "Fabral",
                "category": "Roofing",
                "terms": "Fabral\nR Panel\nHefti-Rib",
                "is_active": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.pdf_path.unlink()
        process_queued_jobs(self.db_path)

        html = self.client.get("/search-filters").get_data(as_text=True)
        self.assertIn("Search Results could not be updated.", html)
        self.assertIn(">Retry</button>", html)

        with closing(get_connection(self.db_path)) as conn:
            failed_job_id = conn.execute(
                "SELECT id FROM search_reindex_jobs WHERE status = 'failed'"
            ).fetchone()["id"]
        response = self.client.post(f"/search-reindex-jobs/{failed_job_id}/retry")
        self.assertEqual(response.status_code, 302)
        with closing(get_connection(self.db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM search_reindex_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()["status"]
        self.assertEqual(status, "queued")

    def test_deactivation_keeps_terms_and_existing_matches(self) -> None:
        with closing(get_connection(self.db_path)) as conn:
            term_id = conn.execute(
                "SELECT id FROM search_terms WHERE filter_id = ?",
                (self.fencing_id,),
            ).fetchone()["id"]

        response = self.client.post(
            f"/search-filters/{self.fencing_id}/edit",
            data={"name": "Fencing", "category": "Sitework", "terms": "Fortress"},
        )
        self.assertEqual(response.status_code, 302)

        with closing(get_connection(self.db_path)) as conn:
            search_filter = conn.execute(
                "SELECT is_active FROM search_filters WHERE id = ?",
                (self.fencing_id,),
            ).fetchone()
            result = conn.execute(
                """SELECT st.id FROM search_results sr
                JOIN search_terms st ON st.id = sr.term_id
                WHERE sr.filter_id = ?""",
                (self.fencing_id,),
            ).fetchone()

        self.assertFalse(search_filter["is_active"])
        self.assertEqual(result["id"], term_id)
        html = self.client.get("/search-results?status=all").get_data(as_text=True)
        self.assertNotIn("Fortress", html)


if __name__ == "__main__":
    unittest.main()
