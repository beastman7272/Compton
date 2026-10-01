import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DB_PATH


def print_row(row):
    for key in row.keys():
        print(f"{key}: {row[key]}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Fill address fields on one project.",
    )
    parser.add_argument("project_name", help="Exact project name")
    parser.add_argument("--id", type=int, help="Project id, when the name matches more than one row")
    parser.add_argument("--address")
    parser.add_argument("--city")
    parser.add_argument("--state")
    parser.add_argument("--county")
    args = parser.parse_args()

    updates = {
        "address_raw": args.address,
        "city": args.city,
        "state": args.state,
        "county": args.county,
    }
    updates = {field: value for field, value in updates.items() if value is not None}

    if not updates:
        print('Add at least one of: --address "..." --city "..." --state "..." --county "..."')
        raise SystemExit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.id is not None:
        rows = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND name = ?",
            (args.id, args.project_name),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM projects WHERE name = ?",
            (args.project_name,),
        ).fetchall()

    if not rows:
        print(f"No project named {args.project_name}")
        raise SystemExit(1)

    if len(rows) > 1:
        print("More than one project has that name. Run again with --id.")
        for row in rows:
            print(f"id: {row['id']}  name: {row['name']}")
        raise SystemExit(1)

    project_id = rows[0]["id"]
    assignments = ", ".join(f"{field} = ?" for field in updates)
    conn.execute(
        f"""
        UPDATE projects
        SET {assignments}, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (*updates.values(), project_id),
    )
    conn.commit()

    saved = conn.execute(
        "SELECT * FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    print("Saved:")
    print_row(saved)


if __name__ == "__main__":
    main()
