import sys
from pathlib import Path

# Ensure local package imports work when running this file directly from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import get_connection

DB_PATH = PROJECT_ROOT / "data" / "cqe.db"

def show_table(conn, title, sql):
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)

    rows = conn.execute(sql).fetchall()

    if not rows:
        print("(no rows)")
        return

    for row in rows:
        print(dict(row))

def main():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return

    with get_connection(DB_PATH) as conn:
        show_table(
            conn,
            "Projects",
            """
            SELECT id, name, status, source_system, source_project_id, city, state, bid_date
            FROM projects
            ORDER BY id DESC
            LIMIT 20
            """
        )

        show_table(
            conn,
            "Uploads",
            """
            SELECT id, project_id, stored_filename, stored_path, upload_status, page_count
            FROM uploads
            ORDER BY id DESC
            LIMIT 20
            """
        )

        show_table(
            conn,
            "Search Results",
            """
            SELECT *
            FROM search_results
            LIMIT 20
            """
        )

        # show_table(
        #     conn,
        #     "Search Filters",
        #     """
        #     SELECT id, name, category, is_active, created_at
        #     FROM search_filters
        #     ORDER BY id
        #     """
        # )

        # show_table(
        #     conn,
        #     "Search Terms",
        #     """
        #     SELECT
        #         st.id,
        #         st.filter_id,
        #         sf.name AS filter_name,
        #         st.term
        #     FROM search_terms st
        #     JOIN search_filters sf ON sf.id = st.filter_id
        #     ORDER BY sf.name, st.term
        #     """
        # )

        show_table(
            conn,
            "Project Contacts",
            """
            SELECT
                pc.id,
                pc.project_id,
                p.name AS project_name,
                pc.contact_type,
                pc.organization,
                pc.contact_name,
                pc.email,
                pc.phone,
                pc.source_page_number,
                pc.confidence
            FROM project_contacts pc
            JOIN projects p ON p.id = pc.project_id
            ORDER BY pc.project_id, pc.contact_type
            """
        )

if __name__ == "__main__":
    main()