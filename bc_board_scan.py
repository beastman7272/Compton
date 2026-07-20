#!/usr/bin/env python3
"""
BuildingConnected Undecided bid-board scanner.

Scans the Undecided bid board directly, filters projects by bid date, and
(optionally) downloads their documents and appends them to the Bid Board sheet
so scripts/run_import.py can pick them up.

This replaces the email-driven, search-by-name lookup in bid_board_orchestrator.py,
which searches the board by project name and fails with NOT FOUND. Here we walk
the whole board, read the bid date straight off each row, and navigate directly
to each qualifying row's href -- no name search.

Everything BuildingConnected-specific is reused from buildingconnected_playwright.py;
this script only adds enumeration, date filtering, and the run loop.

The current Bid Board markup exposes no per-row <a href> (rows navigate via JS),
so visible_bid_rows returns an empty href for every row. We therefore open each
qualifying project with open_bid_row -- which uses the href when present and
otherwise clicks the row by coordinate. Because those coordinates are only valid
while the row is on screen, the download phase re-scans the live board and clicks
rows in place (saving/restoring the scroll position to resume) rather than reusing
the coordinates captured during enumeration.

Runtime wiring matches the deployed (railway-deployment-prep) code:
  * Downloads land in config.DOWNLOADS_DIR, which is DATA_ROOT/downloads on the
    hosted runtime (CQE_DATA_ROOT=/app/data -> /app/data/downloads) and
    ~/Downloads locally. buildingconnected_playwright.DOWNLOAD_DIR already points
    there, so the reused download helpers save to the right place.
  * The Bid Board sheet is written through scripts/run_import.py's Sheets service,
    which builds credentials via app.google_runtime.build_sheets_service.

Safe by default: runs as a dry run (lists what it would download, touches nothing)
unless you pass --no-dry-run. The board holds ~262 projects and individual files
can be 200MB+, so preview before committing to downloads.

Usage:
    python bc_board_scan.py                       # dry run, cutoff = next Monday
    python bc_board_scan.py --after-date 2026-08-01
    python bc_board_scan.py --limit 5 --no-dry-run --headless
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

# Make the repo-root modules importable no matter the working directory.
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402
import buildingconnected_playwright as bc  # noqa: E402
from buildingconnected_playwright import (  # noqa: E402
    BidRow,
    BuildingConnectedLoginRequired,
    download_selected_files,
    ensure_pipeline,
    go_to_next_bid_board_page,
    launch_context,
    open_bid_row,
    open_files_tab,
    reset_bid_board_scroll,
    row_label,
    scroll_bid_board,
    select_allowed_files,
    visible_bid_rows,
)

# Guardrails so a broken next-page control can never spin forever.
MAX_PAGES = 60
MAX_SCROLLS_PER_PAGE = 500
# Stop scrolling a page only after this many consecutive scans add no new rows.
# The virtualized board frequently stalls for a beat while lazy-loading, so a
# single no-movement scroll is not a reliable "end of list" signal.
STABLE_SCROLLS_TO_STOP = 5

BID_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


@dataclass(slots=True)
class QualifyingProject:
    name: str
    bid_date: date
    bid_date_str: str
    href: str
    row: BidRow
    location: str = ""
    project_size: str = ""
    downloaded_files: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | downloaded | failed
    error: str = ""


def log(message: str) -> None:
    print(f"[SCAN] {message}")


def next_monday(today: date) -> date:
    """The next Monday strictly after today (if today is Monday, the following one)."""
    days_ahead = (7 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def parse_bid_date(text: str) -> tuple[date, str] | None:
    """Pull the first MM/DD/YYYY out of a row's text and parse it."""
    match = BID_DATE_RE.search(text or "")
    if not match:
        return None
    raw = match.group(0)
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date(), raw
    except ValueError:
        return None


def row_key(row: BidRow) -> str:
    """Dedupe key: href when present, else the normalized project label."""
    href = (row.href or "").strip()
    if href:
        return href
    return "label:" + bc.norm(row_label(row.text))


def collect_current_page(page, seen: set[str], collected: list[BidRow]) -> int:
    """Scroll the current bid-board page, gathering new rows until it stops growing."""
    added_total = 0
    stall = 0

    for _ in range(MAX_SCROLLS_PER_PAGE):
        added = 0
        for row in visible_bid_rows(page):
            key = row_key(row)
            if key in seen:
                continue
            seen.add(key)
            collected.append(row)
            added += 1
        added_total += added

        # Only "no new rows for several scans in a row" ends the page. Ignore a
        # single scroll_bid_board() reporting no movement -- keep scrolling so the
        # board has a chance to lazy-load the rest.
        stall = 0 if added else stall + 1
        if stall >= STABLE_SCROLLS_TO_STOP:
            break
        scroll_bid_board(page)

    return added_total


def enumerate_board(page) -> list[BidRow]:
    """Walk every page of the Undecided board and return deduped rows."""
    seen: set[str] = set()
    collected: list[BidRow] = []

    reset_bid_board_scroll(page)
    for page_no in range(1, MAX_PAGES + 1):
        added = collect_current_page(page, seen, collected)
        log(f"Page {page_no}: {added} new rows (running total {len(collected)})")

        if not go_to_next_bid_board_page(page):
            log("No further bid-board pages.")
            break
    else:
        log(f"Stopped after hitting the {MAX_PAGES}-page cap.")

    return collected


def filter_qualifying(
    rows: list[BidRow], cutoff: date
) -> tuple[list[QualifyingProject], int]:
    """Keep rows whose bid date is on or after the cutoff. Returns (qualifying, undated)."""
    qualifying: list[QualifyingProject] = []
    undated = 0

    for row in rows:
        parsed = parse_bid_date(row.text)
        if parsed is None:
            undated += 1
            continue
        bid_date, raw = parsed
        if bid_date < cutoff:
            continue
        qualifying.append(
            QualifyingProject(
                name=row_label(row.text),
                bid_date=bid_date,
                bid_date_str=raw,
                href=(row.href or "").strip(),
                row=row,
            )
        )

    qualifying.sort(key=lambda q: (q.bid_date, q.name.lower()))
    return qualifying, undated


def print_qualifying_table(qualifying: list[QualifyingProject]) -> None:
    print("\nQualifying projects (bid date >= cutoff):")
    if not qualifying:
        print("  -- none --")
        return
    for idx, q in enumerate(qualifying, start=1):
        # Rows navigate via JS (no per-row href), so the download phase clicks
        # them in place; only show a link on the rare row that exposes one.
        suffix = f"  {q.href}" if q.href else ""
        print(f"  {idx:03d}. {q.bid_date_str}  {q.name[:80]}{suffix}")


_SCROLL_CONTAINER_JS = """
  const cands = [...document.querySelectorAll('div, main, section, [role=grid], [role=table]')]
    .map((el) => ({ el, room: el.scrollHeight - el.clientHeight, rect: el.getBoundingClientRect() }))
    .filter((i) => i.room > 80 && i.rect.height > 120)
    .sort((a, b) => b.room - a.room);
"""


def scroll_top(page) -> float:
    """Read the scrollTop of the board's main scroll container (0 if none found)."""
    return page.evaluate(
        "() => {" + _SCROLL_CONTAINER_JS
        + "  if (cands[0]) return cands[0].el.scrollTop;"
        + "  return document.scrollingElement ? document.scrollingElement.scrollTop : 0;"
        + "}"
    )


def set_scroll_top(page, top: float) -> None:
    """Restore the board scroll position captured by scroll_top()."""
    page.evaluate(
        "(top) => {" + _SCROLL_CONTAINER_JS
        + "  if (cands[0]) { cands[0].el.scrollTop = top; return; }"
        + "  if (document.scrollingElement) document.scrollingElement.scrollTop = top;"
        + "}",
        top,
    )
    page.wait_for_timeout(1_500)


def return_to_board(page, board_url: str) -> None:
    """Go back to the Undecided board after downloading a project."""
    page.goto(board_url, wait_until="commit", timeout=bc.NAV_TIMEOUT_MS)
    page.wait_for_timeout(2_500)
    bc.click_text_if_visible(page, "Undecided", timeout=8_000)
    page.wait_for_timeout(1_500)


def download_one(page, q: QualifyingProject, row: BidRow) -> None:
    """Open one project's row (href or coordinate click) and download its files."""
    if not open_bid_row(page, row):
        q.status = "failed"
        q.error = "Could not open row (no href and coordinate click did not open the detail page)."
        return

    if bc.looks_like_login_page(page):
        raise BuildingConnectedLoginRequired(bc.LOGIN_REQUIRED_MESSAGE)

    if not bc.is_project_detail_url(page.url):
        q.status = "failed"
        q.error = f"Detail page did not open (landed on {page.url})."
        return

    details = bc.extract_details(page)
    q.location = details.get("Location", "") or ""
    q.project_size = details.get("Project Size", "") or ""

    if not open_files_tab(page):
        q.status = "failed"
        q.error = "Files tab was not found/opened."
        return

    selected = select_allowed_files(page)
    if not selected:
        q.status = "failed"
        q.error = "No eligible files were selected."
        return

    download_selected_files(page)
    q.downloaded_files = selected
    q.status = "downloaded"


def run_download_sweep(
    page, qualifying: list[QualifyingProject], board_url: str
) -> tuple[int, int]:
    """
    Download every qualifying project by sweeping the live board and clicking
    each row while it is on screen (coordinates are only valid for visible rows).
    After each download we return to the board and restore the scroll position so
    the sweep resumes where it left off instead of restarting from the top.

    Returns (downloaded, failed).
    """
    targets: dict[str, QualifyingProject] = {}
    for q in qualifying:
        targets.setdefault(bc.norm(q.name), q)

    processed: set[str] = set()
    downloaded = 0
    failed = 0

    return_to_board(page, board_url)
    reset_bid_board_scroll(page)

    guard = 0
    max_iters = MAX_PAGES * MAX_SCROLLS_PER_PAGE
    while len(processed) < len(targets) and guard < max_iters:
        guard += 1

        match = None
        for row in visible_bid_rows(page):
            key = bc.norm(row_label(row.text))
            if key in targets and key not in processed:
                match = (key, row)
                break

        if match is not None:
            key, row = match
            q = targets[key]
            processed.add(key)
            resume_top = scroll_top(page)
            log(f"[{len(processed)}/{len(targets)}] {q.name} (bid {q.bid_date_str})")
            try:
                download_one(page, q, row)
            except BuildingConnectedLoginRequired:
                raise
            except Exception as exc:  # keep going on per-project failures
                q.status = "failed"
                q.error = str(exc)

            if q.status == "downloaded":
                downloaded += 1
                log(f"  downloaded {len(q.downloaded_files)} file(s)")
            else:
                failed += 1
                log(f"  FAILED: {q.error}")

            return_to_board(page, board_url)
            set_scroll_top(page, resume_top)
            continue

        # No unprocessed target visible here: scroll, then try the next page.
        if not scroll_bid_board(page) and not go_to_next_bid_board_page(page):
            break

    # Anything we never located on the board counts as a failure.
    for key, q in targets.items():
        if key not in processed:
            q.status = "failed"
            q.error = "Row was not found on the board during the download sweep."
            failed += 1

    return downloaded, failed


def append_to_bid_board_sheet(projects: list[QualifyingProject]) -> list[str]:
    """
    Append qualifying projects to the Bid Board Google Sheet using the same
    schema scripts/run_import.py reads (A:G, blank G = import-ready):

        A Project   B Scope   C Location/Address   D Project Size/Budget
        E Bid Date  F Files   G Status (left blank)

    Existing projects (matched by normalized name) are skipped. Returns the
    list of project names actually appended.

    Reuses run_import's Sheets service (app.google_runtime.build_sheets_service)
    and sheet config verbatim so credentials and the schema match the deployed
    importer. Imported lazily so a dry run never pulls in the import stack.
    """
    scripts_dir = REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import run_import as ri  # noqa: E402

    service = ri.get_sheets_service()
    existing = ri.read_values(
        service, ri.BC_SHEET_ID, ri.sheet_range(ri.BC_TAB_NAME, "A1:G1000")
    )
    existing_names = {
        bc.norm(ri.clean(row[0]))
        for row in existing[1:]
        if row and ri.clean(row[0])
    }

    new_rows: list[list[str]] = []
    appended: list[str] = []
    for q in projects:
        key = bc.norm(q.name)
        if not key or key in existing_names:
            continue
        existing_names.add(key)
        appended.append(q.name)
        new_rows.append(
            [
                q.name,                         # A Project
                "",                             # B Scope
                q.location,                     # C Location / Address
                q.project_size,                 # D Project Size / Budget
                q.bid_date_str,                 # E Bid Date
                ", ".join(q.downloaded_files),  # F Files
                "",                             # G Status (blank => import-ready)
            ]
        )

    if new_rows:
        service.spreadsheets().values().append(
            spreadsheetId=ri.BC_SHEET_ID,
            range=ri.sheet_range(ri.BC_TAB_NAME, "A1"),
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()

    return appended


def print_summary(
    *,
    scanned: int,
    undated: int,
    passed: int,
    downloaded: int,
    failed: int,
    appended: list[str] | None,
    dry_run: bool,
) -> None:
    print("\n" + "=" * 70)
    print("BID BOARD SCAN SUMMARY")
    print("=" * 70)
    print(f"Total rows scanned:      {scanned}")
    print(f"  (undated / skipped):   {undated}")
    print(f"Passed date filter:      {passed}")
    if dry_run:
        print("Downloaded:              0 (dry run -- nothing downloaded)")
        print("Failed:                  0 (dry run)")
    else:
        print(f"Downloaded:              {downloaded}")
        print(f"Failed:                  {failed}")
        if appended is not None:
            print(f"Appended to Bid Board:   {len(appended)}")
    print("=" * 70)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan the BuildingConnected Undecided bid board and download "
        "documents for projects bidding on or after a cutoff date."
    )
    parser.add_argument(
        "--after-date",
        metavar="YYYY-MM-DD",
        help="Cutoff bid date (inclusive). Defaults to next Monday.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="List what would be downloaded without downloading or writing the "
        "sheet. On by default; pass --no-dry-run to actually download.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Cap the number of qualifying projects processed.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser headless.",
    )
    return parser.parse_args(argv)


def resolve_cutoff(after_date: str | None) -> date:
    if after_date:
        try:
            return datetime.strptime(after_date, "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit(f"Invalid --after-date {after_date!r}; expected YYYY-MM-DD.")
    return next_monday(date.today())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cutoff = resolve_cutoff(args.after_date)

    log(f"Cutoff bid date (inclusive): {cutoff.isoformat()} ({cutoff:%A})")
    log(f"Mode: {'DRY RUN (no downloads)' if args.dry_run else 'DOWNLOAD'}")
    if not args.dry_run:
        # bc.DOWNLOAD_DIR is already config.DOWNLOADS_DIR; make sure it exists.
        config.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        log(f"Downloads will be saved to: {config.DOWNLOADS_DIR}")

    with sync_playwright() as pw:
        context = launch_context(pw, config.default_playwright_browser(), args.headless)
        context.set_default_navigation_timeout(bc.NAV_TIMEOUT_MS)
        page = context.pages[0] if context.pages else context.new_page()

        board_url = ""
        try:
            # non_interactive=True so login trouble raises instead of blocking on input().
            ensure_pipeline(page, non_interactive=True)
            board_url = page.url
            rows = enumerate_board(page)
        except BuildingConnectedLoginRequired as exc:
            print(f"\n{exc}")
            context.close()
            return 1
        except RuntimeError as exc:
            print(f"\nCould not scan the bid board: {exc}")
            context.close()
            return 1

        qualifying, undated = filter_qualifying(rows, cutoff)
        if args.limit is not None and args.limit >= 0 and len(qualifying) > args.limit:
            log(f"Limiting to first {args.limit} of {len(qualifying)} qualifying projects.")
            qualifying = qualifying[: args.limit]

        print_qualifying_table(qualifying)

        if args.dry_run:
            print_summary(
                scanned=len(rows),
                undated=undated,
                passed=len(qualifying),
                downloaded=0,
                failed=0,
                appended=None,
                dry_run=True,
            )
            context.close()
            return 0

        login_aborted = False
        try:
            downloaded, failed = run_download_sweep(page, qualifying, board_url)
        except BuildingConnectedLoginRequired as exc:
            print(f"\n{exc}")
            login_aborted = True
            downloaded = sum(1 for q in qualifying if q.status == "downloaded")
            failed = sum(1 for q in qualifying if q.status == "failed")

        # Append everything we attempted (successes and failures alike) so
        # run_import can pick them up; already-present rows are skipped.
        processed = [q for q in qualifying if q.status in {"downloaded", "failed"}]
        appended: list[str] = []
        if processed:
            try:
                appended = append_to_bid_board_sheet(processed)
                log(f"Appended {len(appended)} new row(s) to the Bid Board sheet.")
            except Exception as exc:
                log(f"Could not append to the Bid Board sheet: {exc}")

        print_summary(
            scanned=len(rows),
            undated=undated,
            passed=len(qualifying),
            downloaded=downloaded,
            failed=failed,
            appended=appended,
            dry_run=False,
        )

        context.close()
        return 1 if login_aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
