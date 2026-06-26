#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# Ensure local package imports work when running this file directly from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.importer import import_project_from_source
from app.models import ImportItem, ImportResult
from app.search import index_upload_ids
from app.contact_extraction import extract_contacts_for_upload_ids
from app import config
from app.google_runtime import build_sheets_service

from dotenv import load_dotenv

ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE)


def env_value(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


# ── Paths ────────────────────────────────────────────────────────────────

DEFAULT_DB_PATH = config.DB_PATH
DEFAULT_STORAGE_ROOT = config.STORAGE_ROOT
DOWNLOADS = config.DOWNLOADS_DIR


# ── Google Sheets config ─────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# BuildingConnected / Bid Board sheet
BC_SHEET_ID = env_value("BID_BOARD_SHEET_ID", "14PMQx_SiNkSX2gLWtfjsIEpovmADJpv1f53bhICQKfY")
BC_TAB_NAME = env_value("BID_BOARD_TAB_NAME", "Sheet1")
BC_STATUS_COL = "G"

# ConstructConnect sheet
CC_SHEET_ID = env_value("CONSTRUCTCONNECT_SHEET_ID", "1vqEd71BGHNMDJdBcymM4cgGzEQhlXsib3sFXY9Qlt7U")
CC_TAB_NAME = env_value("CONSTRUCTCONNECT_TAB_NAME", "Fortress")
CC_DOWNLOAD_STATUS_COL_INDEX = 12  # L
CC_IMPORT_STATUS_COL = "M"         # CQE/import status

MANUFACTURER_DAYS = {
    "Citadel": {"monday"},
    "Fortress": {"tuesday", "sunday"},
    "Fabral": {"wednesday", "saturday"},
    "Metal-Era": {"thursday"},
}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


MATCH_THRESHOLD = 0.78

STATE_NAMES_BY_ABBR = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}
STATE_ABBRS_BY_NAME = {
    state_name.lower(): abbr
    for abbr, state_name in STATE_NAMES_BY_ABBR.items()
}


@dataclass(slots=True)
class RunSummary:
    total: int = 0
    imported: int = 0
    updated: int = 0
    duplicates: int = 0
    file_not_found: int = 0
    skipped: int = 0
    errors: int = 0


# ── General helpers ──────────────────────────────────────────────────────

def configure_text_io() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def sheet_range(tab_name: str, range_name: str) -> str:
    escaped_tab_name = tab_name.replace("'", "''")
    return f"'{escaped_tab_name}'!{range_name}"


def scheduled_manufacturers(run_day: str) -> list[str]:
    return [
        manufacturer
        for manufacturer, days in MANUFACTURER_DAYS.items()
        if run_day in days
    ]


def norm(value: str) -> str:
    """
    Normalize a project/file name for matching downloaded files.
    """
    value = clean(value).lower()
    value = re.sub(r"\.(pdf|zip)$", "", value)
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(
        r"\b(addendum|revision|rev|bid|project|plans|plan|specs|specifications)\b",
        " ",
        value,
    )
    return re.sub(r"\s+", " ", value).strip()


def normalize_state_name(value: str) -> str:
    """
    Store US states consistently as full names.
    """
    value = clean(value)

    if not value:
        return ""

    upper_value = value.upper().rstrip(".")
    if upper_value in STATE_NAMES_BY_ABBR:
        return STATE_NAMES_BY_ABBR[upper_value]

    lower_value = re.sub(r"\s+", " ", value.lower().rstrip(".")).strip()
    abbr = STATE_ABBRS_BY_NAME.get(lower_value)
    if abbr:
        return STATE_NAMES_BY_ABBR[abbr]

    return value


def format_status_for_sheet(result: ImportResult) -> str:
    if result.status in {"imported", "updated", "duplicate_file"}:
        return "Imported"
    if result.status == "file_not_found":
        return "File not found"
    if result.status == "skipped":
        return "Skipped"
    return "Import Error"


def describe_result(result: ImportResult) -> str:
    parts = [result.message]

    if result.project_id:
        parts.append(f"project_id={result.project_id}")

    if result.uploaded_file_ids:
        parts.append(f"uploads={len(result.uploaded_file_ids)}")

    if result.skipped_files:
        parts.append(f"duplicates={len(result.skipped_files)}")

    return " | ".join(parts)


def update_summary(summary: RunSummary, result: ImportResult) -> None:
    if result.status == "imported":
        summary.imported += 1
    elif result.status == "updated":
        summary.updated += 1
    elif result.status == "duplicate_file":
        summary.duplicates += 1
    elif result.status == "file_not_found":
        summary.file_not_found += 1
    elif result.status == "skipped":
        summary.skipped += 1
    else:
        summary.errors += 1


# ── Google Sheets helpers ────────────────────────────────────────────────

def get_sheets_service():
    return build_sheets_service(
        SCOPES,
        token_filename="orchestrator_token.json",
    )


def read_values(service, sheet_id: str, range_name: str) -> list[list[str]]:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=range_name)
        .execute()
    )
    return result.get("values", [])


def update_sheet_status(
    service,
    item: ImportItem,
    status: str,
) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=item.source_sheet_id,
        range=sheet_range(item.source_tab, f"{item.import_status}{item.source_row}"),
        valueInputOption="USER_ENTERED",
        body={"values": [[status]]},
    ).execute()


# ── Sheet readers / normalizers ──────────────────────────────────────────

def read_buildingconnected_rows(service) -> list[ImportItem]:
    """
    Bid Board columns expected from existing flow:

    A Project
    B Scope
    C Location / Address
    D Project Size / Budget
    E Bid Date
    F Files
    G Status

    Rows with blank G are ready to import.
    """
    values = read_values(service, BC_SHEET_ID, sheet_range(BC_TAB_NAME, "A1:G1000"))

    rows: list[ImportItem] = []

    for i, row in enumerate(values[1:], start=2):
        padded = list(row) + [""] * (7 - len(row))

        project_name = clean(padded[0])
        scope = clean(padded[1])
        address_raw = clean(padded[2])
        budget = clean(padded[3])
        bid_date = clean(padded[4])
        import_status = clean(padded[6])

        if not project_name:
            continue

        if import_status:
            continue

        city, state = parse_city_state(address_raw)

        rows.append(
            ImportItem(
                source_system="BuildingConnected",
                source_sheet_id=BC_SHEET_ID,
                source_tab=BC_TAB_NAME,
                source_row=i,
                source_project_id="",
                project_name=project_name,
                address_raw=address_raw,
                city=city,
                state=state,
                county="",
                bid_date=bid_date,
                budget=budget,
                category_or_scope=scope,
                downloaded_status="",
                import_status=BC_STATUS_COL,
            )
        )

    return rows


def read_constructconnect_rows(service, tab_name: str) -> list[ImportItem]:
    """
    ConstructConnect columns expected from existing flow:

    A Project ID
    B Project Title
    C City
    D State
    E County
    F Bid Date
    G Stage
    H Project Value
    I Update Date
    J Subcategory
    K Download Status
    L Import/CQE Status

    Rows are ready to import when:
    - Project ID and title exist
    - K == Processed
    - L is blank
    """
    values = read_values(service, CC_SHEET_ID, sheet_range(tab_name, "A1:M1000"))

    rows: list[ImportItem] = []

    for i, row in enumerate(values[1:], start=2):
        padded = list(row) + [""] * (13 - len(row))

        project_id = clean(padded[0])
        title = clean(padded[1])
        city = clean(padded[2])
        state = normalize_state_name(padded[3])
        county = clean(padded[4])
        bid_date = clean(padded[5])
        project_value = clean(padded[7])
        subcategory = clean(padded[9])
        download_status = clean(padded[11]).lower()
        import_status = clean(padded[12])

        if not project_id or not title:
            continue

        if download_status != "processed":
            continue

        if import_status:
            continue

        address_raw = ", ".join(part for part in [city, state] if part)

        rows.append(
            ImportItem(
                source_system="ConstructConnect",
                source_sheet_id=CC_SHEET_ID,
                source_tab=tab_name,
                source_row=i,
                source_project_id=project_id,
                project_name=title,
                address_raw=address_raw,
                city=city,
                state=state,
                county=county,
                bid_date=bid_date,
                budget=project_value,
                category_or_scope=subcategory,
                downloaded_status=download_status,
                import_status=CC_IMPORT_STATUS_COL,
            )
        )

    return rows


def read_rows(service, source: str, constructconnect_tabs: list[str]) -> list[ImportItem]:
    if source == "bc":
        return read_buildingconnected_rows(service)

    if source == "cc":
        return [
            item
            for tab_name in constructconnect_tabs
            for item in read_constructconnect_rows(service, tab_name)
        ]

    return read_buildingconnected_rows(service) + [
        item
        for tab_name in constructconnect_tabs
        for item in read_constructconnect_rows(service, tab_name)
    ]


def parse_city_state(address_raw: str) -> tuple[str, str]:
    """
    Light V1 parser for BuildingConnected location strings.

    Handles common values like:
    - Atlanta, GA
    - 950 West Marietta Street Northwest, Atlanta, GA 30318, United States of America
    - Decatur, Georgia
    - Clarkston GA

    If uncertain, leaves city/state blank rather than over-parsing.
    """
    s = clean(address_raw)

    if not s:
        return "", ""

    state_abbreviations = set(STATE_NAMES_BY_ABBR)
    country_suffixes = {"usa", "united states", "united states of america"}
    parts = [part.strip() for part in s.split(",") if part.strip()]

    while parts and parts[-1].lower().rstrip(".") in country_suffixes:
        parts.pop()

    for index in range(len(parts) - 1, -1, -1):
        part = parts[index]

        m = re.search(r"\b([A-Z]{2})\b(?:\s+\d{5}(?:-\d{4})?)?$", part)
        if m and m.group(1) in state_abbreviations:
            city = part[: m.start()].strip(" ,")

            if not city and index > 0:
                city = parts[index - 1].strip()

            return city, normalize_state_name(m.group(1))

        lower_part = part.lower()
        for state_name in STATE_ABBRS_BY_NAME:
            m = re.search(rf"\b{re.escape(state_name)}\b(?:\s+\d{{5}}(?:-\d{{4}})?)?$", lower_part)
            if not m:
                continue

            city = part[: m.start()].strip(" ,")

            if not city and index > 0:
                city = parts[index - 1].strip()

            return city, normalize_state_name(state_name)

    return "", ""


# ── Download matching ────────────────────────────────────────────────────

def find_matching_download(item: ImportItem, downloads_dir: Path = DOWNLOADS) -> tuple[Path | None, float]:
    """
    Locate the downloaded ZIP/PDF corresponding to the import item.

    ConstructConnect gets first priority by project ID because the downloader
    saves files with the project ID in the filename.

    BuildingConnected falls back to fuzzy project-name matching.
    """
    if not downloads_dir.exists():
        return None, 0.0

    files = [
        p
        for p in downloads_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".pdf", ".zip"}
    ]

    if item.source_project_id:
        id_matches = [
            p for p in files
            if item.source_project_id in p.stem or item.source_project_id in p.name
        ]

        if id_matches:
            return max(id_matches, key=lambda p: p.stat().st_mtime), 1.0

    project_norm = norm(item.project_name)

    best_file: Path | None = None
    best_score = 0.0

    for file in files:
        file_norm = norm(file.stem)

        if not project_norm or not file_norm:
            continue

        score = SequenceMatcher(None, project_norm, file_norm).ratio()

        if project_norm in file_norm or file_norm in project_norm:
            score = max(score, 0.90)

        if score > best_score:
            best_file = file
            best_score = score

    if best_score >= MATCH_THRESHOLD:
        return best_file, best_score

    return None, best_score


# ── Runner ───────────────────────────────────────────────────────────────

def run_import(
    source: str,
    run_day: str,
    db_path: Path,
    storage_root: Path,
    downloads_dir: Path,
    dry_run: bool,
    update_sheets: bool,
) -> RunSummary:
    service = get_sheets_service()
    constructconnect_tabs = scheduled_manufacturers(run_day)
    if not constructconnect_tabs:
        constructconnect_tabs = [CC_TAB_NAME]

    if source in {"both", "cc"}:
        print(f"ConstructConnect tabs for {run_day.title()}: {', '.join(constructconnect_tabs)}")

    rows = read_rows(service, source, constructconnect_tabs)

    summary = RunSummary(total=len(rows))

    if not rows:
        print("No import-ready rows found.")
        return summary

    print(f"\nFound {len(rows)} import-ready row(s).")
    print(f"Database: {db_path}")
    print(f"Storage:  {storage_root}")
    print(f"Downloads: {downloads_dir}")

    if dry_run:
        print("Mode: DRY RUN — no database/files will be written.")

    if not update_sheets:
        print("Sheet updates disabled.")

    for item in rows:
        print("\n" + "-" * 80)
        print(f"{item.source_system} row {item.source_row}: {item.project_name}")

        matched_file, score = find_matching_download(item, downloads_dir)

        if not matched_file:
            result = ImportResult(
                status="file_not_found",
                message=f"No matching PDF/ZIP found in {downloads_dir} (best score {score:.2f})",
            )

            print(f"  {result.status}: {result.message}")

            if update_sheets and not dry_run:
                update_sheet_status(service, item, format_status_for_sheet(result))

            update_summary(summary, result)
            continue

        print(f"  Matched file: {matched_file.name} ({score:.2f})")

        result = import_project_from_source(
            item=item,
            matched_file=matched_file,
            db_path=db_path,
            storage_root=storage_root,
            dry_run=dry_run,
        )

        print(f"  {result.status}: {describe_result(result)}")

        if result.uploaded_file_ids and not dry_run:
            index_upload_ids(
                upload_ids=result.uploaded_file_ids,
                db_path=db_path,
            )

            extract_contacts_for_upload_ids(
                upload_ids=result.uploaded_file_ids,
                db_path=db_path,
            )

        if update_sheets and not dry_run:
            update_sheet_status(service, item, format_status_for_sheet(result))

        update_summary(summary, result)

    return summary


def print_summary(summary: RunSummary) -> None:
    print("\n" + "=" * 80)
    print("IMPORT SUMMARY")
    print("=" * 80)
    print(f"Total ready rows: {summary.total}")
    print(f"Imported projects/files: {summary.imported}")
    print(f"Updated existing projects: {summary.updated}")
    print(f"Duplicate-only imports: {summary.duplicates}")
    print(f"File not found: {summary.file_not_found}")
    print(f"Skipped: {summary.skipped}")
    print(f"Errors: {summary.errors}")


def main() -> int:
    configure_text_io()

    parser = argparse.ArgumentParser(
        description="Import BuildingConnected and ConstructConnect downloads into the local CQE app database/storage."
    )

    parser.add_argument(
        "--source",
        choices=["both", "bc", "cc"],
        default="both",
        help="Rows to process: both, bc=BuildingConnected, cc=ConstructConnect.",
    )

    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path.",
    )

    parser.add_argument(
        "--storage-root",
        default=str(DEFAULT_STORAGE_ROOT),
        help="Local app upload storage root.",
    )

    parser.add_argument(
        "--downloads-dir",
        default=str(DOWNLOADS),
        help="Directory where BC/CC downloaded files currently land.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check matching/import readiness without writing database/files or updating Sheets.",
    )

    parser.add_argument(
        "--run-day",
        choices=WEEKDAYS,
        default=datetime.now().strftime("%A").lower(),
        help="Pretend today is this weekday for scheduled ConstructConnect tabs.",
    )

    parser.add_argument(
        "--no-sheet-update",
        action="store_true",
        help="Do not update Google Sheet import statuses.",
    )

    args = parser.parse_args()

    summary = run_import(
        source=args.source,
        run_day=args.run_day,
        db_path=Path(args.db_path),
        storage_root=Path(args.storage_root),
        downloads_dir=Path(args.downloads_dir),
        dry_run=args.dry_run,
        update_sheets=not args.no_sheet_update,
    )

    print_summary(summary)

    return 0 if summary.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())