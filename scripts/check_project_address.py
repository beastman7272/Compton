import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DB_PATH

if len(sys.argv) != 2:
    print('Usage: python scripts/check_project_address.py "Project Name"')
    raise SystemExit(1)

project_name = sys.argv[1]

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT * FROM projects WHERE name = ?",
    (project_name,),
).fetchall()

if not rows:
    print(f"No project named {project_name}")
else:
    for row in rows:
        for key in row.keys():
            print(f"{key}: {row[key]}")
        print()
