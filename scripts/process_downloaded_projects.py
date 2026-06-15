#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_import import run_import, print_summary  # noqa: E402


DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "cqe.db"
DEFAULT_STORAGE_ROOT = PROJECT_ROOT / "data" / "uploads"
DEFAULT_DOWNLOADS_DIR = Path.home() / "Downloads"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Process downloaded BuildingConnected and ConstructConnect files "
            "into the local CQE app."
        )
    )

    parser.add_argument(
        "--source",
        choices=["both", "bc", "cc"],
        default="both",
        help="Which downloaded project source to process. Default: both.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be processed without writing files, database records, or Sheet statuses.",
    )

    parser.add_argument(
        "--no-sheet-update",
        action="store_true",
        help="Do not update Google Sheet import status columns.",
    )

    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path.",
    )

    parser.add_argument(
        "--storage-root",
        default=str(DEFAULT_STORAGE_ROOT),
        help="Local upload storage root.",
    )

    parser.add_argument(
        "--downloads-dir",
        default=str(DEFAULT_DOWNLOADS_DIR),
        help="Folder where downloaded BC/CC files are located.",
    )

    args = parser.parse_args()

    print()
    print("=" * 80)
    print("PROCESS DOWNLOADED PROJECTS")
    print("=" * 80)
    print(f"Source:       {args.source}")
    print(f"Downloads:    {args.downloads_dir}")
    print(f"Database:     {args.db_path}")
    print(f"Storage:      {args.storage_root}")
    print(f"Dry run:      {args.dry_run}")
    print(f"Sheet update: {not args.no_sheet_update}")
    print("Contacts:     always")
    print("=" * 80)

    summary = run_import(
        source=args.source,
        db_path=Path(args.db_path),
        storage_root=Path(args.storage_root),
        downloads_dir=Path(args.downloads_dir),
        dry_run=args.dry_run,
        update_sheets=not args.no_sheet_update,
    )

    print_summary(summary)

    if summary.errors:
        print()
        print("Completed with errors. Review the messages above.")
        return 1

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())