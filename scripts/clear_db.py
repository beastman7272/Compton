import argparse
import sys
from pathlib import Path

# Ensure local package imports work when running this file directly from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import DEFAULT_DB_PATH, get_connection

DB_PATH = PROJECT_ROOT / DEFAULT_DB_PATH

TABLES_TO_CLEAR = [
    "search_results",
    "project_contacts",
    "uploads",
    "search_terms",
    "search_filters",
    "projects",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete all rows from the SQLite database while preserving the schema.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"Path to the SQLite database. Defaults to {DB_PATH}",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm that all database contents should be deleted.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = args.db

    if not args.yes:
        print("Refusing to clear the database without --yes.")
        print(f"Target database: {db_path}")
        return 1

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    with get_connection(db_path) as conn:
        existing_tables = {
            row["name"]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        }

        cleared_tables = []
        for table_name in TABLES_TO_CLEAR:
            if table_name not in existing_tables:
                continue

            conn.execute(f"DELETE FROM {table_name}")
            cleared_tables.append(table_name)

        if cleared_tables:
            placeholders = ", ".join("?" for _ in cleared_tables)
            conn.execute(
                f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
                cleared_tables,
            )

        conn.commit()

    print(f"Cleared database contents from: {db_path}")
    if cleared_tables:
        print("Tables cleared: " + ", ".join(cleared_tables))
    else:
        print("No matching app tables were found to clear.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
