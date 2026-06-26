#!/usr/bin/env python3
"""
Bid Board Orchestrator (Comet-Lite Edition)
============================================
Minimizes Comet browser usage by handling ALL Google Sheets logic in Python.
Comet only navigates BuildingConnected and downloads files.

Three-phase workflow:
  Phase 1:  python bid_board_orchestrator.py
            → Processes queue, handles skips/statuses/dedup via Sheets API
            → Generates a SINGLE consolidated Comet prompt for browser tasks
            → Saves run state for Phase 3

  Phase 2:  Paste the single Comet prompt into Comet sidebar. Comet ONLY:
            → Navigates to each project on BuildingConnected
            → Downloads files
            → Reports back project details + filenames

  Phase 3:  python bid_board_orchestrator.py --finalize
            → You paste Comet's output, script updates Bid Board rows

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
"""

import os
import sys
import re
import argparse
import json
from datetime import datetime
from difflib import SequenceMatcher

from app import config
from app.google_runtime import resolve_google_credentials_file, resolve_google_token_file

try:
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("Missing dependencies. Run:")
    print("  pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)


def configure_text_io():
    """Avoid Windows code page crashes when printing Unicode status text."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def open_utf8(path, mode):
    return open(path, mode, encoding="utf-8")


def explain_http_error(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status != 403:
        return False

    print("\nERROR: Google Sheets denied the request (403 Permission).")
    print("Most likely causes:")
    print("  1. The Google account used during OAuth only has Viewer access to the sheet.")
    print("  2. token.json was created with the wrong Google account.")
    print("  3. The destination sheet/tab is protected against edits.")
    print("\nWhat to check:")
    print(f"  - Confirm you can manually edit the Bid Board spreadsheet: {BID_BOARD_SHEET_ID}")
    print(f"  - Confirm you can manually edit the Email Queue spreadsheet: {EMAILS_SHEET_ID}")
    print("  - If you signed into the wrong account before, delete token.json and rerun the script to re-auth.")
    return True


configure_text_io()


# ── Config ────────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_FILE = resolve_google_token_file("orchestrator_token.json", use_file_env=False)
CREDENTIALS_FILE = resolve_google_credentials_file("credentials.json")

def env_value(name, default):
    return os.getenv(name, default).strip()


EMAILS_SHEET_ID = env_value("EMAILS_SHEET_ID", "1mhCWXwSUtV-AbBxLmEBS-jezkeujVPMYY9RD5ENlPFU")
EMAILS_TAB_NAME = env_value("EMAILS_TAB_NAME", "Sheet1")
BID_BOARD_SHEET_ID = env_value("BID_BOARD_SHEET_ID", "14PMQx_SiNkSX2gLWtfjsIEpovmADJpv1f53bhICQKfY")
BID_BOARD_TAB_NAME = env_value("BID_BOARD_TAB_NAME", "Sheet1")

COMET_PROMPT_FILE = config.COMET_PROMPT_FILE
RUN_STATE_FILE = config.RUN_STATE_FILE
PLAYWRIGHT_RESULTS_FILE = config.PLAYWRIGHT_RESULTS_FILE

SKIP_BRANDS = []
# SKIP_BRANDS = ["advance auto parts", "aldi", "autozone", "barnes & noble", "basss pro shops", "bath & body works", 
# "best western", "boa", "bojangles", "chick-fil-a", "circle k", "cvs", "dollar tree", "dutch bros", "fifth third bank", 
# "five guys", "floor & decor", "harbor freight", "holiday inn", "home 2 suites", "hyatt", "insomnia cookies", "kia", 
# "kroger", "lidl", "long john silvers", "members exchange", "officemax", "old navy", "o’reilly", "pilot", "prequal", 
# "publix", "sam’s club", "savers", "smuckers", "speedway", "sprouts", "staybridge suites", "ta travel center", 
# "total wine & more", "toyota", "tractor supply", "u-haul", "us bank", "usps", "valvoline", "victoria’s secret", 
# "visionworks", "wawa", "walmart", "whole foods", "wingstop", "winn-dixie", "wm", "xfinity", "zaxby"]

SKIP_FOLDER_KEYWORDS = [
    "assessment", "budget", "cad", "drawings", "drws", "dwgs", "geotech", "geo-tech", "geotechnical", "manual", "narrative",
    "pay request", "permit", "photos", "pricing", "procedure", "purchase order"
]

# Exception: folders that would normally be skipped should be OPENED if the
# folder name also contains one of these words (e.g. "Drawings & Specifications")
FOLDER_SKIP_EXCEPTIONS = ["plans", "specifications", "specs"]

SKIP_FILE_KEYWORDS = SKIP_FOLDER_KEYWORDS + [
"affidavit", "air conditioner", "assessment", "budget", "certification", "civil", "communications", "conditions", "concrete", "conductors",
"control", "controls", "conveying systems", "drainage", "drawing", "ductwork", "earthwork", "electrical", "electronic", "engine",
"equipment", "erosion", "fittings", "fixtures", "fuses", "generator", "geotech", "geotechnical", "grounding", "heat pump", "hvac",
"insurance", "instructions", "landscape", "lighting", "lightning", "masonry", "mechanical", "metals", "motor", "outlet",
"outlets", "paint", "pavement", "photo", "photos", "piping", "plans", "plumbing", "power", "procurement",
"questionnaire", "requirements", "refrigerant", "retail", "revisions", "sample standard", "sanitary", "seals", "sedimentation", "seismic",
"sewage", "specialties", "stormwater", "structural", "surge", "terms", "testing", "utilities.pdf", "ventilator", "vibration",
"voice", "windows", "wiring", "wood & plastics"
]


# ── Auth ──────────────────────────────────────────────────────────────────
def get_sheets_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                if os.path.exists(TOKEN_FILE):
                    os.remove(TOKEN_FILE)
                creds = None

        if not creds or not creds.valid:
            if not os.path.exists(CREDENTIALS_FILE):
                print(f"ERROR: {CREDENTIALS_FILE} not found.")
                print("Download OAuth credentials from Google Cloud Console.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open_utf8(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return build("sheets", "v4", credentials=creds)


# ── Sheet helpers ─────────────────────────────────────────────────────────
def read_sheet(service, sheet_id, range_str):
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=range_str
    ).execute()
    return result.get("values", [])


def update_cell(service, sheet_id, cell, value):
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=cell,
        valueInputOption="USER_ENTERED", body={"values": [[value]]}
    ).execute()


def batch_update(service, sheet_id, data):
    """data = [{"range": "Sheet1!F2", "values": [["Processed"]]}]"""
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data}
    ).execute()


def append_row(service, sheet_id, range_str, row):
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=range_str,
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [row]}
    ).execute()


def format_bid_date_for_sheet(raw) -> str:
    """Bid Board column E: store MM/DD/YYYY (matches CQE and avoids long BC prose strings)."""
    if raw is None:
        return "--"
    s = str(raw).strip()
    if not s or s == "--":
        return "--"

    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if iso:
        y, mo, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        return datetime(y, mo, d).strftime("%m/%d/%Y")

    mdy = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if mdy:
        return datetime(int(mdy.group(3)), int(mdy.group(1)), int(mdy.group(2))).strftime("%m/%d/%Y")

    date_part = s.split(" at ", 1)[0].strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(date_part, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue

    return s


# ── Name helpers ──────────────────────────────────────────────────────────
def normalize_name(name):
    name = name.lower().strip()
    name = re.sub(r"\*:.*$", "", name)
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def clean_name(name):
    return re.sub(r"\*:.*$", "", name).strip()


def extract_scope(name):
    m = re.search(r"\*:\s*(.+)$", name)
    return m.group(1).strip() if m else ""


def fuzzy_match(a, b, threshold=0.80):
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


def find_bb_match(project_name, bb_data):
    for i, row in enumerate(bb_data):
        if row and fuzzy_match(project_name, row[0]):
            return i, row
    return None, None


def should_skip(name):
    lower = name.lower()
    for brand in SKIP_BRANDS:
        if brand in lower:
            return True, f"brand:{brand}"
    if "budget" in lower.split():
        return True, "name:Budget"
    return False, ""


# ── Phase 1: Process queue ────────────────────────────────────────────────
def phase1_process(service):
    print("\n" + "=" * 70)
    print("PHASE 1 — Pre-process queue & generate Comet prompt")
    print("=" * 70)

    email_rows = read_sheet(service, EMAILS_SHEET_ID, sheet_range(EMAILS_TAB_NAME, "A1:F1000"))
    bb_rows = read_sheet(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A1:G1000"))

    if len(email_rows) < 2:
        print("No data in email sheet.")
        return

    email_data = email_rows[1:]
    bb_data = bb_rows[1:] if len(bb_rows) > 1 else []

    # Find unprocessed rows
    unprocessed = []
    for i, row in enumerate(email_data):
        while len(row) < 6:
            row.append("")
        if row[5].strip() == "":
            unprocessed.append((i, row))

    if not unprocessed:
        print("\n✓ No unprocessed rows. Nothing to do.")
        return

    # Sort oldest first
    def parse_date(r):
        try:
            return datetime.strptime(r[0], "%m/%d/%Y")
        except (ValueError, IndexError):
            return datetime.max
    unprocessed.sort(key=lambda x: parse_date(x[1]))

    print(f"\nFound {len(unprocessed)} unprocessed row(s)")

    status_updates = []
    comet_tasks = []
    seen_projects = {}

    for data_idx, row in unprocessed:
        sheet_row = data_idx + 2
        project_name = row[2]
        category = row[3].strip().lower()
        doc_links = row[4] if len(row) > 4 else ""
        cn = clean_name(project_name)
        norm = normalize_name(project_name)
        scope = extract_scope(project_name)

        print(f"\n  Row {sheet_row}: {cn} [{category}]", end="")

        # Branch C
        if category == "other":
            status_updates.append({"range": sheet_range(EMAILS_TAB_NAME, f"F{sheet_row}"), "values": [["Needs Review"]]})
            print(" → Needs Review")
            continue

        # Skip filter
        skip, reason = should_skip(cn)
        if skip:
            status_updates.append({"range": sheet_range(EMAILS_TAB_NAME, f"F{sheet_row}"), "values": [["Skipped"]]})
            print(f" → Skipped ({reason})")
            continue

        # Dedup
        if norm in seen_projects:
            first = seen_projects[norm]
            bb_idx, _ = find_bb_match(cn, bb_data)
            if bb_idx is not None:
                comet_tasks.append({
                    "action": "download_files",
                    "project": cn, "scope": scope,
                    "doc_links": doc_links,
                    "bb_row": bb_idx + 2, "email_row": sheet_row,
                })
            status_updates.append({"range": sheet_range(EMAILS_TAB_NAME, f"F{sheet_row}"), "values": [["Processed"]]})
            print(f" → Dedup (see row {first['email_row']})")
            continue

        # Check bid board
        bb_idx, bb_match = find_bb_match(cn, bb_data)

        if category in ("addendum", "specifications", "revision"):
            if bb_idx is not None:
                comet_tasks.append({
                    "action": "download_files",
                    "project": cn, "scope": scope, "doc_links": doc_links,
                    "bb_row": bb_idx + 2, "email_row": sheet_row,
                })
                status_updates.append({"range": sheet_range(EMAILS_TAB_NAME, f"F{sheet_row}"), "values": [["Awaiting Comet"]]})
                print(f" → Branch B (append to BB row {bb_idx + 2})")
            else:
                comet_tasks.append({
                    "action": "new_project",
                    "project": cn, "scope": scope, "doc_links": doc_links,
                    "email_row": sheet_row,
                    "note": "Logged via document update — no prior project record found.",
                })
                status_updates.append({"range": sheet_range(EMAILS_TAB_NAME, f"F{sheet_row}"), "values": [["Awaiting Comet"]]})
                print(f" → Branch B→A (no BB match, treating as new)")

        elif category == "new project":
            if bb_idx is not None:
                comet_tasks.append({
                    "action": "download_files",
                    "project": cn, "scope": scope, "doc_links": doc_links,
                    "bb_row": bb_idx + 2, "email_row": sheet_row,
                })
                status_updates.append({"range": sheet_range(EMAILS_TAB_NAME, f"F{sheet_row}"), "values": [["Awaiting Comet"]]})
                print(f" → Exists on BB row {bb_idx + 2}, download only")
            else:
                comet_tasks.append({
                    "action": "new_project",
                    "project": cn, "scope": scope,
                    "email_row": sheet_row,
                })
                status_updates.append({"range": sheet_range(EMAILS_TAB_NAME, f"F{sheet_row}"), "values": [["Awaiting Comet"]]})
                print(f" → New project, needs Comet")
        else:
            status_updates.append({"range": sheet_range(EMAILS_TAB_NAME, f"F{sheet_row}"), "values": [["Needs Review"]]})
            print(f" → Unknown category, Needs Review")
            continue

        seen_projects[norm] = {"email_row": sheet_row, "project": cn}

    # Batch-update statuses
    if status_updates:
        print(f"\n\nUpdating {len(status_updates)} email statuses...")
        batch_update(service, EMAILS_SHEET_ID, status_updates)
        for su in status_updates:
            row_num = su["range"].split("!F")[1]
            val = su["values"][0][0]
            print(f"  Row {row_num} → {val}")

    # Save run state
    with open_utf8(RUN_STATE_FILE, "w") as f:
        json.dump(comet_tasks, f, indent=2)

    # Generate Comet prompt
    if not comet_tasks:
        print("\n✓ No browser tasks needed. All rows handled by Python.")
        return

    print(f"\n\nGenerating Comet prompt for {len(comet_tasks)} project(s)...")
    prompt = build_comet_prompt(comet_tasks)

    with open_utf8(COMET_PROMPT_FILE, "w") as f:
        f.write(prompt)

    print(f"\nPrompt saved to: {COMET_PROMPT_FILE}")
    print(f"Character count: {len(prompt):,}")
    print(f"\n{'=' * 70}")
    print("NEXT STEPS")
    print("=" * 70)
    print(f"1. Open {COMET_PROMPT_FILE}")
    print(f"2. Paste the ENTIRE prompt into Comet's sidebar")
    print(f"3. Let Comet process all {len(comet_tasks)} project(s)")
    print(f"4. Copy Comet's output")
    print(f"5. Run: python bid_board_orchestrator.py --finalize")


def build_comet_prompt(tasks):
    skip_folders = ", ".join(SKIP_FOLDER_KEYWORDS)
    skip_files = ", ".join(SKIP_FILE_KEYWORDS)
    folder_exceptions = ", ".join(FOLDER_SKIP_EXCEPTIONS)

    lines = []
    lines.append("TASK: Process BuildingConnected projects — BROWSER NAVIGATION ONLY")
    lines.append("All Google Sheets updates have already been handled. You only need to:")
    lines.append("  1. Navigate to each project on BuildingConnected")
    lines.append("  2. Collect project details (Location, Project Size, Due Date)")
    lines.append("  3. Download eligible files from the Files tab")
    lines.append("  4. Report results in the structured format below")
    lines.append("")
    lines.append("GLOBAL FILE RULES (apply to ALL projects below):")
    lines.append(f"  Skip FOLDERS with these words in their name: [{skip_folders}]")
    lines.append(f"    EXCEPTION: If the folder name ALSO contains any of [{folder_exceptions}],")
    lines.append(f"    then OPEN the folder anyway (e.g. \"Drawings & Specifications\" → open it).")
    lines.append(f"  Skip FILES with these words in their name: [{skip_files}]")
    lines.append("")
    lines.append("STARTING POINT: https://app.buildingconnected.com/opportunities/pipeline")
    lines.append("Use the 'Undecided' tab. Do NOT use the Find button. Do NOT go to Archived.")
    lines.append("")
    lines.append("TRUNCATION / PARTIAL MATCH RULE:")
    lines.append("  BuildingConnected may truncate long project names in the Bid Board row. Do not require the full target name to be visible in the row.")
    lines.append("")
    lines.append("  A row counts as a LIKELY MATCH if the visible project name contains the distinctive core words from the target project name, even if the end is cut off with '...'.")
    lines.append("")
    lines.append("  For each LIKELY MATCH:")
    lines.append("    1. Open the project row.")
    lines.append("    2. Check the full project name on the project detail page.")
    lines.append("    3. Treat the project as FOUND if the full name is an exact match or an obvious variant of the target project.")
    lines.append("    4. If it is not the correct project, return to the Undecided Bid Board and continue scanning.")
    lines.append("")
    lines.append("  Example:")
    lines.append("    - Target: 'Tower Renovation - Louisville, KY'")
    lines.append("      Visible row: 'Tower Renovation...'")
    lines.append("      Action: Open and verify if location/client/details suggest the same project.")
    lines.append("")
    lines.append("  Do NOT mark a project NOT FOUND just because the visible row text is truncated.")
    lines.append("")
    lines.append("PROJECT SEARCH RULES:")
    lines.append("  1. Work only in the Undecided tab.")
    lines.append("  2. Do NOT use Find/Search.")
    lines.append("  3. Sort projects in the Undecided tab by Name.")
    lines.append("  4. SORT OPTIMIZATION: Sort by Name ascending once, then scan top-to-bottom. Do NOT toggle between ascending and descending — pick one direction and complete the full scan before trying the other.")
    lines.append("  5. Read every visible row before each scroll.")
    lines.append("  6. LAZY-LOAD RULE: Before scrolling, use read_page or get_page_text to check whether the target already appears in the loaded DOM. Scroll only if the target is not present and more rows might exist below the current viewport.")
    lines.append("  7. After each scroll, wait for the list to finish loading, then continue scanning every visible row.")
    lines.append("  8. Continue until you either find the target project (note the Truncation / Partial Match Rule) or reach the end of the Undecided list.")
    lines.append("  9. STOPPING RULE: If the visible project names on the current page have all passed alphabetically beyond the target (e.g., you're on 'D' names and the target starts with 'C'), go to the next page. If you've reached the end of the list without finding a match, report NOT FOUND — do NOT re-scan in the opposite sort direction.")
    lines.append("  10. MULTIPLE MATCHES: When multiple rows share the same core words, prefer the row whose visible name contains words that also appear in the target's distinctive parts (e.g., 'TI Montgomery' → pick the 'TI' row, not the '(Eastchase)' row).")
    lines.append("  11. Do NOT report NOT FOUND unless you have completed a full top-to-bottom scan of the entire Undecided list.")
    lines.append("")
    lines.append("  DO NOT STOP TO ASK FOR PERMISSION OR CONFIRMATION! You must operate unattended.")
    lines.append("")
    lines.append(f"PROCESS THESE {len(tasks)} PROJECT(S) IN ORDER:")
    lines.append("=" * 60)

    for i, task in enumerate(tasks, 1):
        lines.append("")
        lines.append(f"PROJECT {i}/{len(tasks)}: {task['project']}")
        lines.append("-" * 40)

        if task["action"] == "new_project":
            lines.append("Action: Find project → note details → download files")
            lines.append("Steps:")
            lines.append(f"  1. Find \"{task['project']}\" in the Undecided tab")
            lines.append("  2. Note the Location, Project Size, and Due Date")
            lines.append("  3. Go to the Files tab")
            lines.append("  4. Open all folders EXCEPT those matching the folder skip list above")
            lines.append("     (but DO open folders that also contain Plans/Specifications/Specs)")
            lines.append("  5. Select all files EXCEPT those matching the file skip list above")
            lines.append("  6. Click Download Selected, wait 60 seconds")
        elif task["action"] == "download_files":
            lines.append("Action: Find project → download files only")
            if task.get("doc_links"):
                lines.append(f"Look specifically for: {task['doc_links']}")
            lines.append("Steps:")
            lines.append(f"  1. Find \"{task['project']}\" in the Undecided tab")
            lines.append("  2. Go to the Files tab")
            lines.append("  3. Select files applying the skip rules above")
            lines.append("  4. Click Download Selected, wait 60 seconds")

        lines.append("")

    lines.append("=" * 60)
    lines.append("REPORT FORMAT — Reply with this EXACT structure for each project:")
    lines.append("")
    lines.append("---PROJECT RESULT---")
    lines.append("Project: [exact project name]")
    lines.append("Status: [FOUND / NOT FOUND]")
    lines.append("Location: [location from project page]")
    lines.append("Project Size: [size from project page]")
    lines.append("Due Date: [MM/DD/YYYY from project page — date only, e.g. 04/28/2026]")
    lines.append("Files Downloaded:")
    lines.append("  - [filename1]")
    lines.append("  - [filename2]")
    lines.append("---END PROJECT---")
    lines.append("")
    lines.append("If a project is NOT FOUND, still report it with Status: NOT FOUND")

    return "\n".join(lines)


# ── Phase 3: Finalize ────────────────────────────────────────────────────
def phase3_finalize(service):
    print("\n" + "=" * 70)
    print("PHASE 3 — Finalize: record Comet results → Bid Board")
    print("=" * 70)

    if not os.path.exists(RUN_STATE_FILE):
        print(f"\nNo {RUN_STATE_FILE} found. Run Phase 1 first.")
        return True

    with open_utf8(RUN_STATE_FILE, "r") as f:
        tasks = json.load(f)

    if not tasks:
        print("\nNo pending tasks in run state.")
        return

    bb_rows = read_sheet(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A1:G1000"))
    bb_data = bb_rows[1:] if len(bb_rows) > 1 else []

    print(f"\n{len(tasks)} project(s) to finalize.")
    print("Type 'PASTE' to paste Comet's bulk output, or 'ONE' for manual entry:\n")

    mode = input("Mode (PASTE/ONE): ").strip().upper()

    if mode == "PASTE":
        finalize_from_paste(service, tasks, bb_data)
    else:
        finalize_interactive(service, tasks, bb_data)

    os.remove(RUN_STATE_FILE)
    print("\n✓ Finalization complete. Run state cleared.")


def parse_comet_output(text):
    results = []
    blocks = re.split(r"---PROJECT RESULT---", text)
    for block in blocks:
        if "---END PROJECT---" not in block:
            continue
        block = block.split("---END PROJECT---")[0]
        result = {}
        for line in block.strip().split("\n"):
            line = line.strip()
            if line.startswith("Project:"):
                result["project"] = line.split(":", 1)[1].strip()
            elif line.startswith("Status:"):
                result["status"] = line.split(":", 1)[1].strip().upper()
            elif line.startswith("Location:"):
                result["location"] = line.split(":", 1)[1].strip()
            elif line.startswith("Project Size:"):
                result["project_size"] = line.split(":", 1)[1].strip()
            elif line.startswith("Due Date:"):
                result["due_date"] = line.split(":", 1)[1].strip()
            elif line.startswith("- "):
                result.setdefault("files", []).append(line[2:].strip())
        if result.get("project"):
            results.append(result)
    return results


def finalize_from_paste(service, tasks, bb_data):
    print("\nPaste Comet's output (end with a line containing only 'END'):\n")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)

    comet_text = "\n".join(lines)
    results = parse_comet_output(comet_text)

    if not results:
        print("\nCouldn't parse any results. Falling back to interactive.\n")
        finalize_interactive(service, tasks, bb_data)
        return

    print(f"\nParsed {len(results)} result(s). Matching to tasks...")

    for task in tasks:
        matched_result = None
        for r in results:
            if fuzzy_match(task["project"], r.get("project", "")):
                matched_result = r
                break

        if not matched_result:
            print(f"\n  {task['project']}: No matching Comet result")
            update_cell(service, EMAILS_SHEET_ID,
                        sheet_range(EMAILS_TAB_NAME, f"F{task['email_row']}"), "Processing Error")
            continue

        apply_result(service, task, matched_result, bb_data)

        if task["action"] == "new_project":
            bb_rows = read_sheet(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A1:G1000"))
            bb_data = bb_rows[1:] if len(bb_rows) > 1 else []


def finalize_interactive(service, tasks, bb_data):
    for task in tasks:
        print(f"\n{'─' * 50}")
        print(f"Project: {task['project']} (email row {task['email_row']})")
        print(f"Action: {task['action']}")
        print(f"{'─' * 50}")

        status = input("  Found? (y/n/skip): ").strip().lower()
        if status == "skip":
            continue

        result = {"project": task["project"]}
        if status == "n":
            result["status"] = "NOT FOUND"
        else:
            result["status"] = "FOUND"
            result["location"] = input("  Location: ").strip() or "--"
            result["project_size"] = input("  Project Size: ").strip() or "--"
            result["due_date"] = input("  Due Date: ").strip() or "--"
            print("  Filenames (one per line, blank to finish):")
            files = []
            while True:
                f = input("    > ").strip()
                if not f:
                    break
                files.append(f)
            result["files"] = files

        apply_result(service, task, result, bb_data)
        if task["action"] == "new_project":
            bb_rows = read_sheet(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A1:G1000"))
            bb_data = bb_rows[1:] if len(bb_rows) > 1 else []


def apply_result(service, task, result, bb_data):
    project = task["project"]
    email_row = task["email_row"]
    scope = task.get("scope", "")
    files_str = ", ".join(result.get("files", [])) or "No files downloaded"
    note = task.get("note", "")
    if note:
        files_str += f", {note}"

    if result.get("status") in ("NOT FOUND", "ERROR"):
        status = result.get("status")
        error_note = result.get("error") or "Project not found on BuildingConnected"
        print(f"  → {project}: {status} — Processing Error")
        update_cell(service, EMAILS_SHEET_ID, sheet_range(EMAILS_TAB_NAME, f"F{email_row}"), "Processing Error")
        append_row(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A:G"), [
            project, scope, "--", "--", "--", error_note, "Processing Error"
        ])
        return

    if task["action"] == "download_files" and task.get("bb_row"):
        bb_row_num = task["bb_row"]
        bb_idx = bb_row_num - 2
        existing = bb_data[bb_idx][5] if len(bb_data[bb_idx]) > 5 else ""
        updated = (existing + ", " + files_str) if existing else files_str
        update_cell(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, f"F{bb_row_num}"), updated)
        update_cell(service, EMAILS_SHEET_ID, sheet_range(EMAILS_TAB_NAME, f"F{email_row}"), "Processed")
        print(f"  → {project}: Files appended to BB row {bb_row_num}")

    elif task["action"] == "new_project":
        location = result.get("location", "--")
        size = result.get("project_size", "--")
        due = format_bid_date_for_sheet(result.get("due_date", "--"))
        append_row(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A:G"), [
            project, scope, location, size, due, files_str, ""
        ])
        update_cell(service, EMAILS_SHEET_ID, sheet_range(EMAILS_TAB_NAME, f"F{email_row}"), "Processed")
        print(f"  → {project}: New BB row + Processed")

    else:
        bb_idx, _ = find_bb_match(project, bb_data)
        if bb_idx is not None:
            bb_row_num = bb_idx + 2
            existing = bb_data[bb_idx][5] if len(bb_data[bb_idx]) > 5 else ""
            updated = (existing + ", " + files_str) if existing else files_str
            update_cell(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, f"F{bb_row_num}"), updated)
        else:
            location = result.get("location", "--")
            size = result.get("project_size", "--")
            due = format_bid_date_for_sheet(result.get("due_date", "--"))
            append_row(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A:G"), [
                project, scope, location, size, due, files_str, ""
            ])
        update_cell(service, EMAILS_SHEET_ID, sheet_range(EMAILS_TAB_NAME, f"F{email_row}"), "Processed")
        print(f"  → {project}: Updated + Processed")


# ── Phase 2: Playwright browser run ───────────────────────────────────────
def phase2_playwright_run(
    service,
    *,
    browser="chrome",
    cdp_url=None,
    headless=False,
    select_files=False,
    download_files=False,
    apply_results=False,
):
    print("\n" + "=" * 70)
    print("PHASE 2 — Playwright: process BuildingConnected browser tasks")
    print("=" * 70)

    if not os.path.exists(RUN_STATE_FILE):
        print(f"\nNo {RUN_STATE_FILE} found. Run Phase 1 first.")
        return True

    with open_utf8(RUN_STATE_FILE, "r") as f:
        tasks = json.load(f)

    if not tasks:
        print("\nNo pending browser tasks in run state.")
        return True

    if apply_results and not (select_files or download_files):
        print("\nRefusing to apply Playwright results without file selection.")
        print("Rerun with --playwright-select-files or --playwright-download-files.")
        return False

    try:
        from playwright.sync_api import sync_playwright
        from buildingconnected_playwright import (
            DOWNLOAD_DIR,
            NAV_TIMEOUT_MS,
            attach_context,
            launch_context,
            process_project,
        )
    except ImportError as exc:
        print(f"\nMissing Playwright dependency or helper import: {exc}")
        print("Install Playwright dependencies before running --playwright-run.")
        return False

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    with sync_playwright() as pw:
        browser_instance = None
        if cdp_url:
            browser_instance, context = attach_context(pw, cdp_url)
        else:
            context = launch_context(pw, browser, headless)

        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            for idx, task in enumerate(tasks, start=1):
                project = task["project"]
                print(f"\n[{idx}/{len(tasks)}] {project}")
                result = process_project(
                    page,
                    project,
                    select_files=select_files or download_files,
                    download_files=download_files,
                    non_interactive=True,
                    reuse_current_page=False,
                )
                results.append({"task": task, "result": result})

                status = result.get("status", "ERROR")
                files = result.get("files", [])
                print(f"  Status: {status}")
                print(f"  Files: {len(files)}")
                if result.get("download_path"):
                    print(f"  Download: {result['download_path']}")
                if result.get("error"):
                    print(f"  Error: {result['error']}")
        finally:
            if not cdp_url:
                context.close()
            elif browser_instance:
                browser_instance.close()

    with open_utf8(PLAYWRIGHT_RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nPlaywright results saved to: {PLAYWRIGHT_RESULTS_FILE}")

    if not apply_results:
        print("\nSheets were not updated. Rerun with --apply-playwright-results after reviewing results.")
        return True

    errors = [
        item
        for item in results
        if item["result"].get("status") == "ERROR"
    ]
    if errors:
        print("\nPlaywright returned errors; Sheets were not updated and run_state.json was kept.")
        if any(item["result"].get("login_required") for item in errors):
            print("BuildingConnected login/MFA refresh is required before the scheduled workflow can continue.")
        print("Review playwright_results.json, fix the issue, then rerun the workflow.")
        return False

    bb_rows = read_sheet(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A1:G1000"))
    bb_data = bb_rows[1:] if len(bb_rows) > 1 else []

    print("\nApplying Playwright results to Sheets...")
    for item in results:
        task = item["task"]
        result = item["result"]
        apply_result(service, task, result, bb_data)
        if task["action"] == "new_project":
            bb_rows = read_sheet(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A1:G1000"))
            bb_data = bb_rows[1:] if len(bb_rows) > 1 else []

    os.remove(RUN_STATE_FILE)
    print("\n✓ Playwright finalization complete. Run state cleared.")
    return True


def check_buildingconnected_login(*, browser="chrome", cdp_url=None, headless=False):
    print("\n" + "=" * 70)
    print("BUILDINGCONNECTED LOGIN CHECK")
    print("=" * 70)

    try:
        from playwright.sync_api import sync_playwright
        from buildingconnected_playwright import (
            BuildingConnectedLoginRequired,
            NAV_TIMEOUT_MS,
            attach_context,
            ensure_pipeline,
            launch_context,
        )
    except ImportError as exc:
        print(f"\nMissing Playwright dependency or helper import: {exc}")
        print("Install Playwright dependencies before running the login check.")
        return False

    try:
        with sync_playwright() as pw:
            browser_instance = None
            if cdp_url:
                browser_instance, context = attach_context(pw, cdp_url)
            else:
                context = launch_context(pw, browser, headless)

            try:
                context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                page = context.pages[0] if context.pages else context.new_page()
                ensure_pipeline(page, non_interactive=True)
            finally:
                if not cdp_url:
                    context.close()
                elif browser_instance:
                    browser_instance.close()
    except BuildingConnectedLoginRequired as exc:
        print(f"\n{exc}")
        return False
    except Exception as exc:
        print(f"\nBuildingConnected login check failed: {exc}")
        return False

    print("\n✓ BuildingConnected session is active.")
    return True


# ── Status ────────────────────────────────────────────────────────────────
def show_status(service):
    email_rows = read_sheet(service, EMAILS_SHEET_ID, sheet_range(EMAILS_TAB_NAME, "A1:F1000"))
    bb_rows = read_sheet(service, BID_BOARD_SHEET_ID, sheet_range(BID_BOARD_TAB_NAME, "A1:G1000"))

    print("\n" + "=" * 70)
    print("QUEUE STATUS")
    print("=" * 70)

    counts = {}
    for row in email_rows[1:]:
        while len(row) < 6:
            row.append("")
        status = row[5].strip() or "(unprocessed)"
        counts[status] = counts.get(status, 0) + 1

    print(f"\nEmail Queue ({len(email_rows) - 1} rows):")
    for s in sorted(counts):
        flag = ""
        if s == "(unprocessed)":
            flag = "  ← run Phase 1"
        elif s == "Awaiting Comet":
            flag = "  ← run --playwright-run or Comet, then finalize/apply"
        print(f"  {s:25s} {counts[s]}{flag}")

    print(f"\nBid Board: {len(bb_rows) - 1} entries")

    if os.path.exists(RUN_STATE_FILE):
        with open_utf8(RUN_STATE_FILE, "r") as f:
            tasks = json.load(f)
        print(f"\nPending: {len(tasks)} browser task(s)")
        print("  → Playwright: --playwright-run --playwright-select-files")
        print(f"  → Comet fallback: use {COMET_PROMPT_FILE}, then --finalize")


# ── Entry point ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Bid Board Orchestrator (Comet-Lite)")
    parser.add_argument("--status", action="store_true", help="Show queue status")
    parser.add_argument("--finalize", action="store_true",
                        help="Phase 3: record Comet results and update sheets")
    parser.add_argument("--playwright-run", action="store_true",
                        help="Phase 2: process run_state.json browser tasks with Playwright")
    parser.add_argument("--run-playwright-workflow", action="store_true",
                        help="Run Phase 1, then Playwright download/apply in one command")
    parser.add_argument("--check-buildingconnected-login", action="store_true",
                        help="Open the Playwright profile and verify BuildingConnected is logged in")
    parser.add_argument("--playwright-browser", choices=["auto", "chrome", "msedge", "chromium"],
                        default="chrome", help="Browser channel for Playwright runs")
    parser.add_argument("--playwright-cdp-url",
                        help="Attach Playwright to an existing Chrome/Edge debugging session")
    parser.add_argument("--playwright-headless", action="store_true",
                        help="Run Playwright browser headless")
    parser.add_argument("--playwright-select-files", action="store_true",
                        help="Select eligible files but do not download them")
    parser.add_argument("--playwright-download-files", action="store_true",
                        help="Download selected eligible files to ~/Downloads")
    parser.add_argument("--apply-playwright-results", action="store_true",
                        help="Apply Playwright results to Sheets and clear run_state.json")
    args = parser.parse_args()

    playwright_flag_without_run = (
        args.playwright_browser != "chrome"
        or args.playwright_cdp_url
        or args.playwright_headless
        or args.playwright_select_files
        or args.playwright_download_files
        or args.apply_playwright_results
    )
    if not args.playwright_run and playwright_flag_without_run:
        if not args.run_playwright_workflow and not args.check_buildingconnected_login:
            parser.error(
                "Playwright options require --playwright-run, "
                "--run-playwright-workflow, or --check-buildingconnected-login"
            )

    try:
        if args.check_buildingconnected_login:
            return 0 if check_buildingconnected_login(
                browser=args.playwright_browser,
                cdp_url=args.playwright_cdp_url,
                headless=args.playwright_headless,
            ) else 1

        service = get_sheets_service()

        if args.status:
            show_status(service)
            return 0
        elif args.finalize:
            phase3_finalize(service)
            return 0
        elif args.run_playwright_workflow:
            phase1_process(service)
            ok = phase2_playwright_run(
                service,
                browser=args.playwright_browser,
                cdp_url=args.playwright_cdp_url,
                headless=args.playwright_headless,
                select_files=True,
                download_files=True,
                apply_results=True,
            )
            return 0 if ok else 1
        elif args.playwright_run:
            ok = phase2_playwright_run(
                service,
                browser=args.playwright_browser,
                cdp_url=args.playwright_cdp_url,
                headless=args.playwright_headless,
                select_files=args.playwright_select_files,
                download_files=args.playwright_download_files,
                apply_results=args.apply_playwright_results,
            )
            return 0 if ok else 1
        else:
            phase1_process(service)
            return 0
    except HttpError as exc:
        if explain_http_error(exc):
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
