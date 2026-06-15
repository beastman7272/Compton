from __future__ import annotations

import argparse
import sys
from pathlib import Path
from textwrap import shorten

# Ensure local package imports work when running this file directly from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import get_connection


DB_PATH = Path("data") / "cqe.db"


def clean(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def truncate(value, width: int = 120) -> str:
    value = clean(value).replace("\n", " ")
    return shorten(value, width=width, placeholder="...")


def print_divider(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def show_dashboard_view(conn, limit_projects: int = 20) -> None:
    """
    Dashboard-style grouped project view.

    Shows each project once, with related search matches underneath.
    """
    print_divider("DASHBOARD-STYLE GROUPED VIEW")

    projects = conn.execute(
        """
        SELECT
            p.id,
            p.name,
            p.state,
            p.city,
            p.status,
            p.updated_at,
            COUNT(DISTINCT sr.id) AS match_count,
            COUNT(DISTINCT u.id) AS upload_count
        FROM projects p
        LEFT JOIN uploads u ON u.project_id = p.id
        LEFT JOIN search_results sr ON sr.project_id = p.id
        GROUP BY p.id
        ORDER BY p.updated_at DESC, p.id DESC
        LIMIT ?
        """,
        (limit_projects,),
    ).fetchall()

    if not projects:
        print("(no projects)")
        return

    for project in projects:
        location = ", ".join(
            part for part in [clean(project["city"]), clean(project["state"])]
            if part
        )

        print()
        print(
            f"[Project {project['id']}] {project['name']} "
            f"| {location or 'No location'} "
            f"| Status: {project['status']} "
            f"| Uploads: {project['upload_count']} "
            f"| Matches: {project['match_count']}"
        )

        matches = conn.execute(
            """
            SELECT
                sf.name AS filter_name,
                sf.category,
                st.term,
                u.stored_filename,
                sr.page_number,
                sr.matched_text,
                sr.context_text
            FROM search_results sr
            JOIN search_filters sf ON sf.id = sr.filter_id
            JOIN search_terms st ON st.id = sr.term_id
            JOIN uploads u ON u.id = sr.upload_id
            WHERE sr.project_id = ?
            ORDER BY sf.category, sf.name, st.term, sr.page_number
            LIMIT 12
            """,
            (project["id"],),
        ).fetchall()

        if not matches:
            print("  (no search matches)")
            continue

        for match in matches:
            print(
                f"  - {match['category']} / {match['filter_name']} "
                f"| term: {match['term']} "
                f"| page {match['page_number']} "
                f"| upload: {match['stored_filename']}"
            )
            print(f"    context: {truncate(match['context_text'], 180)}")


def show_search_results_view(conn, limit_results: int = 50) -> None:
    """
    Search Results-style flat view.

    Mirrors the planned Search Results table:
    Updated, State, Project, Search Filter, Search Term, Upload, Page, Status, Category.
    """
    print_divider("SEARCH RESULTS-STYLE FLAT VIEW")

    rows = conn.execute(
        """
        SELECT
            p.updated_at AS updated,
            p.state,
            p.name AS project_name,
            sf.name AS search_filter,
            st.term AS search_term,
            u.stored_filename AS upload,
            sr.page_number AS page,
            p.status,
            sf.category,
            sr.context_text
        FROM search_results sr
        JOIN projects p ON p.id = sr.project_id
        JOIN uploads u ON u.id = sr.upload_id
        JOIN search_filters sf ON sf.id = sr.filter_id
        JOIN search_terms st ON st.id = sr.term_id
        ORDER BY p.updated_at DESC, p.state, p.name, sf.category, sf.name, st.term, sr.page_number
        LIMIT ?
        """,
        (limit_results,),
    ).fetchall()

    if not rows:
        print("(no search results)")
        return

    for idx, row in enumerate(rows, start=1):
        print()
        print(f"{idx}. Updated: {row['updated']}")
        print(f"   State: {clean(row['state']) or '--'}")
        print(f"   Project: {row['project_name']}")
        print(f"   Search Filter: {row['search_filter']}")
        print(f"   Search Term: {row['search_term']}")
        print(f"   Upload: {row['upload']}")
        print(f"   Page: {row['page']}")
        print(f"   Status: {row['status']}")
        print(f"   Category: {row['category']}")
        print(f"   Context: {truncate(row['context_text'], 240)}")


def show_status_summary(conn) -> None:
    print_divider("STATUS SUMMARY")

    rows = conn.execute(
        """
        SELECT
            status,
            COUNT(*) AS count
        FROM projects
        GROUP BY status
        ORDER BY status
        """
    ).fetchall()

    if not rows:
        print("(no statuses)")
        return

    for row in rows:
        print(f"{row['status']}: {row['count']}")


def show_filter_summary(conn) -> None:
    print_divider("SEARCH FILTER SUMMARY")

    rows = conn.execute(
        """
        SELECT
            sf.id,
            sf.name,
            sf.category,
            sf.is_active,
            COUNT(st.id) AS term_count,
            COUNT(sr.id) AS result_count
        FROM search_filters sf
        LEFT JOIN search_terms st ON st.filter_id = sf.id
        LEFT JOIN search_results sr ON sr.filter_id = sf.id
        GROUP BY sf.id
        ORDER BY sf.category, sf.name
        """
    ).fetchall()

    if not rows:
        print("(no search filters)")
        return

    for row in rows:
        active = "active" if row["is_active"] else "inactive"
        print(
            f"[{row['id']}] {row['category']} / {row['name']} "
            f"| {active} | terms: {row['term_count']} | results: {row['result_count']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print Dashboard/Search Results-style views from the local CQE SQLite database."
    )

    parser.add_argument(
        "--db-path",
        default=str(DB_PATH),
        help="SQLite database path.",
    )

    parser.add_argument(
        "--projects",
        type=int,
        default=20,
        help="Number of projects to show in the dashboard-style view.",
    )

    parser.add_argument(
        "--results",
        type=int,
        default=50,
        help="Number of search result rows to show.",
    )

    parser.add_argument(
        "--only",
        choices=["all", "dashboard", "results", "summary", "filters"],
        default="all",
        help="Limit output to one section.",
    )

    args = parser.parse_args()
    db_path = Path(args.db_path)

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    with get_connection(db_path) as conn:
        if args.only in {"all", "summary"}:
            show_status_summary(conn)

        if args.only in {"all", "filters"}:
            show_filter_summary(conn)

        if args.only in {"all", "dashboard"}:
            show_dashboard_view(conn, limit_projects=args.projects)

        if args.only in {"all", "results"}:
            show_search_results_view(conn, limit_results=args.results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())