import sys
from pathlib import Path

# Ensure local package imports work when running this file directly from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DB_PATH
from app.db import get_connection

FILTER_ID = 1

TERMS_TO_ADD = [
    "Barbed Wire", 
    "Razor Wire", 
    "Fortress", 
    "security fence", 
    "Ameristar", 
    "Ornamental", 
    "V 2", 
    "anti climb", 
    "crash barrier", 
    "anti-climb", 
    "anticlimb", 
    "Stephens Pipe", 
    "Chain Link"
]

def main():
    with get_connection(DB_PATH) as conn:
        existing_rows = conn.execute(
            """
            SELECT term
            FROM search_terms
            WHERE filter_id = ?
            """,
            (FILTER_ID,),
        ).fetchall()

        existing_terms = {row["term"].strip().lower() for row in existing_rows}

        added = 0
        skipped = 0

        for term in TERMS_TO_ADD:
            clean_term = term.strip()

            if not clean_term:
                continue

            if clean_term.lower() in existing_terms:
                print(f"Skipped existing term: {clean_term}")
                skipped += 1
                continue

            conn.execute(
                """
                INSERT INTO search_terms (filter_id, term)
                VALUES (?, ?)
                """,
                (FILTER_ID, clean_term),
            )

            print(f"Added term: {clean_term}")
            added += 1

        conn.commit()

    print()
    print(f"Done. Added: {added}; skipped: {skipped}")

if __name__ == "__main__":
    main()