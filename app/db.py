from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_DB_PATH = Path("data") / "cqe.db"


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """
    Open a SQLite connection and apply app-level defaults.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """
    Create all V1 database tables if they do not already exist.
    """
    with get_connection(db_path) as conn:
        create_tables(conn)
        create_indexes(conn)


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            source_system TEXT NOT NULL,
            source_project_id TEXT,
            source_sheet_id TEXT,
            source_tab TEXT,
            source_row INTEGER,

            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,

            address_raw TEXT,
            city TEXT,
            state TEXT,
            county TEXT,

            bid_date TEXT,
            budget TEXT,

            status TEXT NOT NULL DEFAULT 'Needs Review',

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            project_id INTEGER NOT NULL,
            source_system TEXT NOT NULL,

            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,

            file_type TEXT NOT NULL DEFAULT 'pdf',
            file_hash TEXT NOT NULL,
            page_count INTEGER,

            upload_status TEXT NOT NULL DEFAULT 'pending_index',

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (project_id)
                REFERENCES projects(id)
                ON DELETE CASCADE,

            UNIQUE(project_id, file_hash)
        );

        CREATE TABLE IF NOT EXISTS search_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS search_terms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            filter_id INTEGER NOT NULL,
            term TEXT NOT NULL,

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (filter_id)
                REFERENCES search_filters(id)
                ON DELETE CASCADE,

            UNIQUE(filter_id, term)
        );

        CREATE TABLE IF NOT EXISTS search_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            project_id INTEGER NOT NULL,
            upload_id INTEGER NOT NULL,
            filter_id INTEGER NOT NULL,
            term_id INTEGER NOT NULL,

            page_number INTEGER NOT NULL,
            matched_text TEXT NOT NULL,
            context_text TEXT,

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (project_id)
                REFERENCES projects(id)
                ON DELETE CASCADE,

            FOREIGN KEY (upload_id)
                REFERENCES uploads(id)
                ON DELETE CASCADE,

            FOREIGN KEY (filter_id)
                REFERENCES search_filters(id)
                ON DELETE CASCADE,

            FOREIGN KEY (term_id)
                REFERENCES search_terms(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS project_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            project_id INTEGER NOT NULL,

            contact_type TEXT NOT NULL,
            organization TEXT,
            contact_name TEXT,
            email TEXT,
            phone TEXT,

            source_upload_id INTEGER,
            source_page_number INTEGER,
            confidence REAL,

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (project_id)
                REFERENCES projects(id)
                ON DELETE CASCADE,

            FOREIGN KEY (source_upload_id)
                REFERENCES uploads(id)
                ON DELETE SET NULL
        );
        """
    )


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_projects_source
            ON projects(source_system, source_project_id);

        CREATE INDEX IF NOT EXISTS idx_projects_normalized_name
            ON projects(normalized_name);

        CREATE INDEX IF NOT EXISTS idx_projects_location
            ON projects(state, city);

        CREATE INDEX IF NOT EXISTS idx_projects_status
            ON projects(status);

        CREATE INDEX IF NOT EXISTS idx_uploads_project
            ON uploads(project_id);

        CREATE INDEX IF NOT EXISTS idx_uploads_hash
            ON uploads(file_hash);

        CREATE INDEX IF NOT EXISTS idx_search_terms_filter
            ON search_terms(filter_id);

        CREATE INDEX IF NOT EXISTS idx_search_results_project
            ON search_results(project_id);

        CREATE INDEX IF NOT EXISTS idx_search_results_upload
            ON search_results(upload_id);

        CREATE INDEX IF NOT EXISTS idx_search_results_filter_term
            ON search_results(filter_id, term_id);

        CREATE INDEX IF NOT EXISTS idx_project_contacts_project
            ON project_contacts(project_id);
        """
    )


def execute(
    conn: sqlite3.Connection,
    sql: str,
    params: Iterable | dict = (),
) -> sqlite3.Cursor:
    return conn.execute(sql, params)


def fetch_one(
    conn: sqlite3.Connection,
    sql: str,
    params: Iterable | dict = (),
) -> Optional[sqlite3.Row]:
    return conn.execute(sql, params).fetchone()


def fetch_all(
    conn: sqlite3.Connection,
    sql: str,
    params: Iterable | dict = (),
) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def touch_project(conn: sqlite3.Connection, project_id: int) -> None:
    conn.execute(
        """
        UPDATE projects
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (project_id,),
    )


def touch_upload(conn: sqlite3.Connection, upload_id: int) -> None:
    conn.execute(
        """
        UPDATE uploads
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (upload_id,),
    )


def reset_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """
    Development helper only.
    Deletes the SQLite database and recreates it.
    """
    db_path = Path(db_path)

    if db_path.exists():
        db_path.unlink()

    init_db(db_path)


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at: {DEFAULT_DB_PATH}")