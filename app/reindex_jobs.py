from __future__ import annotations

import threading
import time
from contextlib import closing
from pathlib import Path

from app.db import get_connection
from app.search import reindex_filter_upload


_worker_lock = threading.Lock()
_workers: dict[str, threading.Thread] = {}


def cancel_filter_jobs(conn, filter_id: int) -> None:
    conn.execute(
        """UPDATE search_reindex_jobs
        SET status = 'superseded', completed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE filter_id = ? AND status IN ('queued', 'running')""",
        (filter_id,),
    )


def enqueue_filter_reindex(conn, filter_id: int) -> int:
    cancel_filter_jobs(conn, filter_id)
    totals = conn.execute(
        """SELECT COUNT(*) AS total, COALESCE(MAX(id), 0) AS max_id
        FROM uploads WHERE LOWER(file_type) = 'pdf'"""
    ).fetchone()
    cursor = conn.execute(
        """INSERT INTO search_reindex_jobs
        (filter_id, max_upload_id, total_uploads) VALUES (?, ?, ?)""",
        (filter_id, totals["max_id"], totals["total"]),
    )
    return int(cursor.lastrowid)


def retry_reindex_job(conn, job_id: int) -> int | None:
    job = conn.execute(
        "SELECT filter_id FROM search_reindex_jobs WHERE id = ? AND status = 'failed'",
        (job_id,),
    ).fetchone()
    if not job:
        return None
    conn.execute(
        """UPDATE search_reindex_jobs SET acknowledged_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (job_id,),
    )
    return enqueue_filter_reindex(conn, job["filter_id"])


def process_queued_jobs(db_path: Path | str) -> None:
    while True:
        with closing(get_connection(db_path)) as conn, conn:
            job = conn.execute(
                "SELECT * FROM search_reindex_jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if not job:
                return
            conn.execute(
                """UPDATE search_reindex_jobs SET status = 'running',
                started_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (job["id"],),
            )
        try:
            _process_job(db_path, job["id"])
        except Exception as exc:
            with closing(get_connection(db_path)) as conn, conn:
                conn.execute(
                    """UPDATE search_reindex_jobs SET status = 'failed', error_text = ?,
                    completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'running'""",
                    (str(exc)[:2000], job["id"]),
                )


def _process_job(db_path: Path | str, job_id: int) -> None:
    while True:
        with closing(get_connection(db_path)) as conn:
            job = conn.execute(
                "SELECT * FROM search_reindex_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not job or job["status"] != "running":
                return
            upload = conn.execute(
                """SELECT id FROM uploads WHERE LOWER(file_type) = 'pdf'
                AND id > ? AND id <= ? ORDER BY id LIMIT 1""",
                (job["last_upload_id"], job["max_upload_id"]),
            ).fetchone()

        if not upload:
            status = "failed" if job["failed_uploads"] else "completed"
            with closing(get_connection(db_path)) as conn, conn:
                conn.execute(
                    """UPDATE search_reindex_jobs SET status = ?,
                    completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'running'""",
                    (status, job_id),
                )
            return

        try:
            with closing(get_connection(db_path)) as conn, conn:
                reindex_filter_upload(conn, job["filter_id"], upload["id"])
                conn.execute(
                    """UPDATE search_reindex_jobs SET last_upload_id = ?,
                    processed_uploads = processed_uploads + 1,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'running'""",
                    (upload["id"], job_id),
                )
        except Exception as exc:
            error = f"Upload {upload['id']}: {exc}"
            with closing(get_connection(db_path)) as conn, conn:
                conn.execute(
                    """UPDATE search_reindex_jobs SET last_upload_id = ?,
                    processed_uploads = processed_uploads + 1,
                    failed_uploads = failed_uploads + 1,
                    error_text = SUBSTR(COALESCE(error_text || CHAR(10), '') || ?, 1, 2000),
                    updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'running'""",
                    (upload["id"], error, job_id),
                )


def start_reindex_worker(db_path: Path | str) -> None:
    key = str(Path(db_path).resolve())
    with _worker_lock:
        if key in _workers and _workers[key].is_alive():
            return
        with closing(get_connection(db_path)) as conn, conn:
            conn.execute(
                """UPDATE search_reindex_jobs SET status = 'queued',
                updated_at = CURRENT_TIMESTAMP WHERE status = 'running'"""
            )
        worker = threading.Thread(
            target=_worker_loop,
            args=(db_path,),
            name="search-reindex-worker",
            daemon=True,
        )
        _workers[key] = worker
        worker.start()


def _worker_loop(db_path: Path | str) -> None:
    while True:
        try:
            process_queued_jobs(db_path)
        except Exception as exc:
            print(f"Search re-index worker error: {exc}", flush=True)
        time.sleep(1)
