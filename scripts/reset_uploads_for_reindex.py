import sys
from pathlib import Path

# Ensure local package imports work when running this file directly from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DB_PATH
from app.db import get_connection


def main():
    with get_connection(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE uploads
            SET upload_status = 'pending_index',
                updated_at = CURRENT_TIMESTAMP
            WHERE upload_status IN ('indexed', 'index_error')
            """
        )
        conn.commit()

    print("Uploads reset for re-indexing.")

if __name__ == "__main__":
    main()