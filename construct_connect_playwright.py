from __future__ import annotations

import argparse
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv
from playwright.sync_api import Download, Locator, Page, sync_playwright

SPREADSHEET_ID = "1vqEd71BGHNMDJdBcymM4cgGzEQhlXsib3sFXY9Qlt7U"
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

SEARCH_URL = "https://insight.cmdgroup.com/SearchResult/ProjectSearchResult/Index"
DOWNLOAD_DIR = Path.home() / "Downloads"
HEADLESS = False
PROFILE_DIR = Path("playwright_cc_profile")
LOGIN_REQUIRED_MESSAGE = (
    "ConstructConnect login required. Open the Playwright browser profile, "
    "log in to ConstructConnect, or configure CONSTRUCTCONNECT_USERNAME and "
    "CONSTRUCTCONNECT_PASSWORD, then rerun the workflow."
)
EXPORT_CLEANUP_ATTEMPTS = 3
EXPORT_CLEANUP_WAIT_MS = 5000
MENU_CLICK_TIMEOUT_MS = 60000
DOWNLOAD_TIMEOUT_MS = 120000
EXPORT_WAIT_TIMEOUT_MS = 90000

MANUFACTURER_DAYS = {
    "Citadel": {"monday"},
    "Fortress": {"tuesday", "sunday"},
    "Fabral": {"wednesday", "saturday"},
    "Metal-Era": {"thursday"},
}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


@dataclass
class Project:
    tab_name: str
    row_number: int
    project_id: str
    title: str
    city: str
    state: str
    county: str
    bid_date: str
    stage: str
    project_value: str
    update_date: str
    subcategory: str
    sheet_status: str = ""
    status: str = "Pending"
    documents_exported: list[str] | None = None
    error: str = ""


def log(step: str) -> None:
    print(f"[STEP] {step}")


class ConstructConnectLoginRequired(RuntimeError):
    """Raised when the persistent browser profile needs a fresh ConstructConnect login."""


def get_creds() -> Credentials:
    creds = None

    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                Path(TOKEN_FILE).unlink(missing_ok=True)
                creds = None

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        Path(TOKEN_FILE).write_text(creds.to_json(), encoding="utf-8")

    return creds


def clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def get_sheets_service():
    creds = get_creds()
    return build("sheets", "v4", credentials=creds)


def sheet_range(tab_name: str, range_name: str) -> str:
    escaped_tab_name = tab_name.replace("'", "''")
    return f"'{escaped_tab_name}'!{range_name}"


def scheduled_manufacturers(run_day: str) -> list[str]:
    return [
        manufacturer
        for manufacturer, days in MANUFACTURER_DAYS.items()
        if run_day in days
    ]


def fetch_projects(sheets_service, tab_name: str) -> list[Project]:
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=sheet_range(tab_name, "A:L"))
        .execute()
    )

    values = result.get("values", [])
    
    if len(values) < 2:
        print(f"No project rows found in tab '{tab_name}'.")
        return []

    rows = values[1:]
    projects: list[Project] = []

    for idx, row in enumerate(rows, start=2):
        padded = row + [""] * (12 - len(row))
        stage = clean(padded[6])
        sheet_status = clean(padded[11])

        if stage.lower() == "pre-design":
            continue

        if sheet_status:
            continue

        project = Project(
            tab_name=tab_name,
            row_number=idx,
            project_id=clean(padded[0]),
            title=clean(padded[1]),
            city=clean(padded[2]),
            state=clean(padded[3]),
            county=clean(padded[4]),
            bid_date=clean(padded[5]),
            stage=stage,
            project_value=clean(padded[7]),
            update_date=clean(padded[8]),
            subcategory=clean(padded[9]),
            sheet_status=sheet_status,
        )
        
        if not project.project_id:
            project.status = "Skipped"
            project.error = "Missing Project ID"

        projects.append(project)

    return projects


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return name[:180] or "download"


def wait_visible(page: Page, selector: str, timeout: int = 60000) -> Locator:
    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=timeout)
    return locator


def has_search_ui(page: Page) -> bool:
    try:
        search_input = page.locator("#demo-input-local").first
        return search_input.count() > 0 and search_input.is_visible()
    except Exception:
        return False


def looks_like_login_page(page: Page) -> bool:
    current_url = (page.url or "").lower()
    if any(marker in current_url for marker in ("login", "signin", "sign-in", "account")):
        return True

    try:
        title = page.title().lower()
    except Exception:
        title = ""

    if any(marker in title for marker in ("login", "sign in", "constructconnect")):
        try:
            password_fields = page.locator('input[type="password"]').count()
            email_fields = page.locator('input[type="email"], input[name*="email" i]').count()
            return password_fields > 0 or email_fields > 0
        except Exception:
            return True

    return False


def first_configured_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return ""


def constructconnect_credentials() -> tuple[str, str]:
    username = first_configured_env(
        "CONSTRUCTCONNECT_USERNAME",
        "CONSTRUCTCONNECT_EMAIL",
        "CC_USERNAME",
        "CC_EMAIL",
    )
    password = first_configured_env(
        "CONSTRUCTCONNECT_PASSWORD",
        "CC_PASSWORD",
    )
    return username, password


def first_visible_locator(page: Page, selectors: list[str], timeout: int = 2000) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout)
            return locator
        except Exception:
            continue
    return None


def click_first_visible(page: Page, selectors: list[str], timeout: int = 3000) -> bool:
    locator = first_visible_locator(page, selectors, timeout=timeout)
    if not locator:
        return False
    locator.click()
    page.wait_for_timeout(1500)
    return True


def attempt_constructconnect_login(page: Page) -> None:
    username, password = constructconnect_credentials()
    if not username or not password:
        raise ConstructConnectLoginRequired(LOGIN_REQUIRED_MESSAGE)

    username_selectors = [
        'input[type="email"]',
        'input[name*="email" i]',
        'input[id*="email" i]',
        'input[name*="user" i]',
        'input[id*="user" i]',
        'input[placeholder*="email" i]',
        'input[placeholder*="username" i]',
    ]
    password_selectors = [
        'input[type="password"]',
        'input[name*="password" i]',
        'input[id*="password" i]',
        'input[placeholder*="password" i]',
    ]
    continue_selectors = [
        'button:has-text("Next")',
        'button:has-text("Continue")',
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'input[type="submit"]',
    ]

    log("Attempting ConstructConnect login with configured credentials")
    username_input = first_visible_locator(page, username_selectors, timeout=8000)
    if username_input:
        username_input.fill(username)
        click_first_visible(page, continue_selectors, timeout=4000)

    password_input = first_visible_locator(page, password_selectors, timeout=10000)
    if not password_input:
        raise ConstructConnectLoginRequired(LOGIN_REQUIRED_MESSAGE)

    password_input.fill(password)
    if not click_first_visible(page, continue_selectors, timeout=5000):
        password_input.press("Enter")

    page.wait_for_timeout(5000)
    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    if not has_search_ui(page):
        raise ConstructConnectLoginRequired(LOGIN_REQUIRED_MESSAGE)


def ensure_constructconnect_login(page: Page, *, non_interactive: bool = False) -> None:
    log("Opening ConstructConnect search page")
    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    if has_search_ui(page):
        log("Existing ConstructConnect session detected")
        return

    if non_interactive:
        attempt_constructconnect_login(page)
        return

    if looks_like_login_page(page):
        try:
            attempt_constructconnect_login(page)
            return
        except ConstructConnectLoginRequired:
            pass

    print("\nPlease log in to ConstructConnect manually in the opened browser.")
    print("This login should persist in the Playwright profile until ConstructConnect times out.")
    print("When login is complete and the site is ready, press Enter here.\n")
    input()
    page.wait_for_timeout(1500)

    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if not has_search_ui(page):
        raise ConstructConnectLoginRequired(LOGIN_REQUIRED_MESSAGE)


def open_search(page: Page) -> None:
    log("Opening search page")
    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    try:
        wait_visible(page, "#demo-input-local")
    except Exception as exc:
        if looks_like_login_page(page):
            raise ConstructConnectLoginRequired(LOGIN_REQUIRED_MESSAGE) from exc
        raise


def clear_existing_search_filters(page: Page) -> None:
    log("Clearing any existing search filters")
    close_links = page.locator('a.searchclose, div.searchClose.delete a, [id^="lnkClose"]')
    count = close_links.count()

    for _ in range(count):
        try:
            close_links.nth(0).click(force=True)
            page.wait_for_timeout(700)
        except Exception:
            break

    search_input = page.locator("#demo-input-local").first
    if search_input.count():
        try:
            search_input.click()
            search_input.fill("")
            page.wait_for_timeout(300)
        except Exception:
            pass


def search_project(page: Page, project_id: str, title: str) -> None:
    log(f"Waiting for search input for {project_id}")
    search_input = wait_visible(page, "#demo-input-local")
    search_button = wait_visible(page, "#btn_createList")

    clear_existing_search_filters(page)

    log(f"Entering project ID {project_id}")
    search_input.click()
    search_input.fill("")
    search_input.fill(project_id)

    log("Clicking search button")
    search_button.click()
    page.wait_for_timeout(3500)

    log(f"Waiting for search result for {project_id}")
    id_cell = page.locator(f'.x-grid-cell-colProjectCode:has-text("{project_id}")').first
    title_cell = page.locator(f'.x-grid-cell-colProjectName .x-tree-node-text:has-text("{title}")').first
    row = page.locator(f'tr.x-grid-row:has-text("{project_id}")').first

    if id_cell.count():
        try:
            id_cell.wait_for(state="visible", timeout=12000)
            return
        except Exception:
            pass

    if title_cell.count():
        try:
            title_cell.wait_for(state="visible", timeout=12000)
            return
        except Exception:
            pass

    row.wait_for(state="visible", timeout=12000)


def open_project(page: Page, project_id: str, title: str) -> None:
    log(f"Opening project row for {project_id}")

    title_cell = page.locator(f'.x-grid-cell-colProjectName .x-tree-node-text:has-text("{title}")').first
    id_cell = page.locator(f'.x-grid-cell-colProjectCode:has-text("{project_id}")').first
    row = page.locator(f'tr.x-grid-row:has-text("{project_id}")').first

    if title_cell.count():
        log("Clicking project title cell")
        title_cell.click()
    elif id_cell.count():
        log("Title cell not found; clicking project ID cell")
        id_cell.click()
    else:
        log("Falling back to clicking full row")
        row.click()

    page.wait_for_timeout(3000)


def open_document_center(page: Page) -> Page:
    log("Waiting for View Documents control")
    view_documents = page.locator('a:has-text("View Documents"), button:has-text("View Documents")').first
    view_documents.wait_for(state="visible", timeout=50000)

    log("Clicking View Documents and waiting for new tab")
    with page.context.expect_page(timeout=60000) as new_page_info:
        view_documents.click()

    doc_page = new_page_info.value
    doc_page.wait_for_load_state("domcontentloaded")
    doc_page.wait_for_timeout(3000)
    log(f"Document Center opened; frame count: {len(doc_page.frames)}")
    return doc_page


def get_export_rows(doc_page: Page) -> Locator:
    return doc_page.locator("div#exportItemHeader:visible")


def row_display_name(row: Locator) -> str:
    try:
        return row.locator("#exportShortFileName").first.inner_text(timeout=1500).strip()
    except Exception:
        return ""


def get_visible_export_names(doc_page: Page) -> list[str]:
    rows = get_export_rows(doc_page)
    names: list[str] = []
    seen: set[str] = set()

    for i in range(rows.count()):
        name = row_display_name(rows.nth(i))
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    return names


def export_row_container(row: Locator) -> Locator:
    container_selectors = [
        "xpath=ancestor-or-self::*[.//img[contains(@class,'miniDownArrow')]][1]",
        "xpath=ancestor-or-self::*[.//*[contains(@class,'miniDownArrow')]][1]",
        "xpath=ancestor-or-self::*[contains(@class,'export')][1]",
        "xpath=ancestor-or-self::*[contains(@id,'export')][1]",
    ]

    for selector in container_selectors:
        try:
            container = row.locator(selector).first
            if container.count():
                return container
        except Exception:
            continue

    return row


def open_row_menu(doc_page: Page, row: Locator) -> Locator:
    arrow_selectors = [
        "img.miniDownArrow",
        ".miniDownArrow",
        '[class*="miniDownArrow"]',
        '[class*="DownArrow"]',
        '[aria-haspopup="true"]',
        'button[aria-haspopup="true"]',
        "xpath=following::*[contains(@class,'miniDownArrow')][1]",
        "xpath=following::*[contains(@class,'DownArrow')][1]",
    ]

    deadline = time.monotonic() + (MENU_CLICK_TIMEOUT_MS / 1000)
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        container = export_row_container(row)

        try:
            row.scroll_into_view_if_needed(timeout=3000)
            row.hover(timeout=3000)
        except Exception:
            pass

        for selector in arrow_selectors:
            candidates = [container.locator(selector).first, row.locator(selector).first]
            for arrow in candidates:
                try:
                    if not arrow.count():
                        continue
                    arrow.click(force=True, timeout=5000)
                    return container
                except Exception as exc:
                    last_error = exc

        doc_page.wait_for_timeout(1000)

    if last_error:
        raise last_error

    raise RuntimeError("Could not find a row action menu control for the exported file.")


def click_any_visible_delete_all(doc_page: Page) -> bool:
    selectors = [
        "#DeleteAllButton:visible",
        "img#DeleteAllButton:visible",
        "button:has-text('Delete All'):visible",
        "a:has-text('Delete All'):visible",
        "span:has-text('Delete All'):visible",
        "text=DELETE ALL",
    ]

    for selector in selectors:
        try:
            locator = doc_page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.click(force=True)
                return True
        except Exception:
            continue
    return False


def click_any_confirm(doc_page: Page) -> bool:
    selectors = [
        "button:has-text('OK'):visible",
        "button:has-text('Yes'):visible",
        "button:has-text('Delete'):visible",
        "button:has-text('Confirm'):visible",
        "a:has-text('OK'):visible",
        "a:has-text('Yes'):visible",
        "a:has-text('Delete'):visible",
        "a:has-text('Confirm'):visible",
        "span:has-text('OK'):visible",
        "span:has-text('Yes'):visible",
        "span:has-text('Delete'):visible",
        "span:has-text('Confirm'):visible",
    ]

    for selector in selectors:
        try:
            locator = doc_page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.click(force=True)
                return True
        except Exception:
            continue
    return False


def purge_existing_exports(doc_page: Page) -> None:
    for attempt in range(1, EXPORT_CLEANUP_ATTEMPTS + 1):
        names = get_visible_export_names(doc_page)

        if not names:
            log("No leftover visible export rows found at start of project")
            return

        log(f"Cleanup attempt {attempt}: found leftover visible export rows: {names}")

        if click_any_visible_delete_all(doc_page):
            log("Clicked Delete All at start of project")
            doc_page.wait_for_timeout(2000)

            if click_any_confirm(doc_page):
                log("Confirmed Delete All at start of project")
            else:
                log("No confirmation control appeared for start-of-project cleanup")

            doc_page.wait_for_timeout(EXPORT_CLEANUP_WAIT_MS)
        else:
            log("Could not find Delete All control at start of project")
            doc_page.wait_for_timeout(EXPORT_CLEANUP_WAIT_MS)

    remaining = get_visible_export_names(doc_page)
    if remaining:
        raise RuntimeError(
            f"Could not clear leftover export rows before starting project: {remaining}"
        )


def select_specifications_section(doc_page: Page) -> None:
    log("Waiting for Specifications row")
    row = doc_page.locator(".fancytree-node.specDocs").first
    row.wait_for(state="visible", timeout=70000)

    log("Clicking Specifications checkbox")
    checkbox = row.locator(".fancytree-checkbox").first
    checkbox.click(force=True)
    doc_page.wait_for_timeout(1200)


def export_selected_documents(doc_page: Page) -> None:
    log("Waiting for Export All button")
    export_button = doc_page.locator("#BtnExport").first
    export_button.wait_for(state="visible", timeout=60000)

    log("Clicking Export All")
    export_button.click(force=True)
    doc_page.wait_for_timeout(8000)


def save_download(download: Download, project: Project) -> str:
    suggested = download.suggested_filename

    if project.project_id not in suggested:
        raise RuntimeError(
            f"Downloaded file mismatch. Current project ID {project.project_id} "
            f"was not found in suggested filename: {suggested}"
        )

    extension = Path(suggested).suffix or ".pdf"
    filename = safe_filename(f"{project.project_id}_{project.title}{extension}")

    target = DOWNLOAD_DIR / filename
    download.save_as(str(target))
    return target.name


def should_download_name(name: str, project: Project, before_names: list[str], seen_current: set[str]) -> bool:
    if not name:
        return False
    if name in before_names:
        return False
    if name in seen_current:
        return False
    if project.project_id not in name:
        return False
    return True



def download_new_exported_files(doc_page: Page, project: Project, before_names: list[str]) -> list[str]:
    log("Waiting for current project's exported file row")

    current_row = doc_page.locator(
        f'div#exportItemHeader:visible:has-text("{project.project_id}")'
    ).first

    current_row.wait_for(state="visible", timeout=EXPORT_WAIT_TIMEOUT_MS)

    rows = get_export_rows(doc_page)
    log(f"Visible export row count after export: {rows.count()}")

    downloaded: list[str] = []
    seen_current: set[str] = set()

    for i in range(rows.count()):
        row = rows.nth(i)
        display_name = row_display_name(row)

        if not should_download_name(display_name, project, before_names, seen_current):
            log(f"Skipping row: {display_name}")
            continue

        seen_current.add(display_name)

        for attempt in range(1, 4):
            try:
                log(f"Opening menu for row: {display_name} / attempt {attempt}")

                container = open_row_menu(doc_page, row)
                doc_page.wait_for_timeout(1200)

                menu_item = container.locator(
                    'a.ui-corner-all:has-text("Download file"):visible'
                ).first

                try:
                    menu_item.wait_for(state="visible", timeout=3000)
                except Exception:
                    menu_item = doc_page.locator(
                        'a.ui-corner-all:has-text("Download file"):visible'
                    ).first
                    menu_item.wait_for(state="visible", timeout=MENU_CLICK_TIMEOUT_MS)

                log(f"Downloading file for row: {display_name}")

                with doc_page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                    menu_item.click(force=True, timeout=MENU_CLICK_TIMEOUT_MS)

                saved_name = save_download(dl_info.value, project)
                downloaded.append(saved_name)
                log(f"Saved {saved_name}")
                doc_page.wait_for_timeout(1500)
                break

            except Exception as exc:
                log(f"Download attempt {attempt} failed for row '{display_name}': {exc}")

                try:
                    doc_page.keyboard.press("Escape")
                except Exception:
                    pass

                doc_page.wait_for_timeout(2500)

                if attempt == 3:
                    raise RuntimeError(
                        f"Failed to download current project export after 3 attempts: "
                        f"{display_name}"
                    ) from exc

    return downloaded


def delete_all_exports(doc_page: Page) -> None:
    for attempt in range(1, EXPORT_CLEANUP_ATTEMPTS + 1):
        before = get_visible_export_names(doc_page)

        if not before:
            log("No visible export rows present at cleanup")
            return

        log(f"Cleanup attempt {attempt} with visible export rows: {before}")

        if click_any_visible_delete_all(doc_page):
            log("Clicked Delete All")
            doc_page.wait_for_timeout(2000)

            if click_any_confirm(doc_page):
                log("Confirmed Delete All")
            else:
                log("No confirmation control appeared during cleanup")

            doc_page.wait_for_timeout(EXPORT_CLEANUP_WAIT_MS)
        else:
            log("Delete All control not found during cleanup")
            doc_page.wait_for_timeout(EXPORT_CLEANUP_WAIT_MS)

    after = get_visible_export_names(doc_page)
    if after:
        raise RuntimeError(f"Could not clear export rows after download: {after}")


def process_project(page: Page, project: Project) -> None:
    open_search(page)
    search_project(page, project.project_id, project.title)
    open_project(page, project.project_id, project.title)
    doc_page = open_document_center(page)

    try:
        purge_existing_exports(doc_page)
        before_names = get_visible_export_names(doc_page)
        log(f"Visible export rows present before current export: {before_names}")

        select_specifications_section(doc_page)
        export_selected_documents(doc_page)

        docs = download_new_exported_files(doc_page, project, before_names)
        project.documents_exported = docs or ["No qualifying documents found"]

        delete_all_exports(doc_page)
        project.status = "Complete"
    finally:
        log("Closing Document Center tab")
        doc_page.close()


def verify_all_processed(projects: list[Project]) -> list[Project]:
    return [p for p in projects if p.project_id and p.status == "Pending"]


def print_summary(projects: list[Project]) -> None:
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    attempted = [p for p in projects if p.status != "Skipped"]

    for i, p in enumerate(attempted, start=1):
        print(f"\nProject #{i}: {p.title}")
        print(f"• Manufacturer Tab: {p.tab_name}")
        print(f"• Project ID: {p.project_id}")
        print(f"• State: {p.state}")
        print(f"• Bid Date: {p.bid_date}")
        print(f"• Project Value: {p.project_value}")

        if p.error:
            print(f"• Documents Exported: ERROR - {p.error}")
        else:
            docs = p.documents_exported or ["No qualifying documents found"]
            print(f"• Documents Exported: {', '.join(docs)}")




def update_sheet_status(sheets_service, project: Project, value: str) -> None:
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=sheet_range(project.tab_name, f"L{project.row_number}"),
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()


def try_update_sheet_status(sheets_service, project: Project, value: str) -> bool:
    try:
        update_sheet_status(sheets_service, project, value)
        return True
    except Exception as exc:
        print(
            f"  WARNING: Could not write '{value}' to "
            f"{project.tab_name} row {project.row_number} Download_Status: {exc}"
        )
        return False


def launch_context(pw, *, headless: bool):
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        accept_downloads=True,
    )


def check_login(*, headless: bool) -> bool:
    print("\n" + "=" * 80)
    print("CONSTRUCTCONNECT LOGIN CHECK")
    print("=" * 80)

    try:
        with sync_playwright() as pw:
            context = launch_context(pw, headless=headless)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                ensure_constructconnect_login(page, non_interactive=True)
            finally:
                context.close()
    except ConstructConnectLoginRequired as exc:
        print(f"\n{exc}")
        return False
    except Exception as exc:
        print(f"\nConstructConnect login check failed: {exc}")
        return False

    print("\n✓ ConstructConnect session is active.")
    return True


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="ConstructConnect Playwright document downloader")
    parser.add_argument("--check-login", action="store_true",
                        help="Open the Playwright profile and verify ConstructConnect is logged in")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Fail instead of waiting for manual login")
    parser.add_argument("--headless", action="store_true",
                        help="Run the browser headless")
    parser.add_argument(
        "--run-day",
        choices=WEEKDAYS,
        default=datetime.now().strftime("%A").lower(),
        help="Pretend today is this weekday for manual testing",
    )
    args = parser.parse_args()

    headless = args.headless or HEADLESS

    if args.check_login:
        return 0 if check_login(headless=headless) else 1

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    sheets_service = get_sheets_service()
    manufacturers = scheduled_manufacturers(args.run_day)

    if not manufacturers:
        print(f"No ConstructConnect manufacturers scheduled for {args.run_day.title()}.")
        return 0

    print(f"Processing {args.run_day.title()} manufacturers: {', '.join(manufacturers)}")

    projects = []
    for manufacturer in manufacturers:
        projects.extend(fetch_projects(sheets_service, manufacturer))

    if not projects:
        print("\nNo unprocessed projects found in scheduled manufacturer tabs.")
        return 0

    print("\nProjects queued:")
    for p in projects:
        print(f"[{p.tab_name} Row {p.row_number}] - {p.title} - {p.project_id} - {p.status}")

    login_required = False
    sheet_update_failed = False

    with sync_playwright() as pw:
        context = launch_context(pw, headless=headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()

            try:
                ensure_constructconnect_login(page, non_interactive=args.non_interactive)
            except ConstructConnectLoginRequired as exc:
                print(f"\n{exc}")
                return 1

            for project in projects:
                if project.status == "Skipped":
                    continue

                print(
                    f"\nProcessing {project.tab_name} row {project.row_number}: "
                    f"{project.title} ({project.project_id})"
                )
                try:
                    process_project(page, project)
                    if not try_update_sheet_status(sheets_service, project, "Processed"):
                        sheet_update_failed = True
                except ConstructConnectLoginRequired as exc:
                    login_required = True
                    project.error = str(exc)
                    if not try_update_sheet_status(sheets_service, project, "Error"):
                        sheet_update_failed = True
                    print(f"  ERROR: {exc}")
                    break
                except Exception as exc:
                    project.error = str(exc)
                    if not try_update_sheet_status(sheets_service, project, "Error"):
                        sheet_update_failed = True
                    print(f"  ERROR: {exc}")

            pending = verify_all_processed(projects)
            if pending and not login_required:
                print("\nRetrying pending projects once...")
                for project in pending:
                    try:
                        process_project(page, project)
                        project.error = ""
                        if not try_update_sheet_status(sheets_service, project, "Processed"):
                            sheet_update_failed = True
                    except ConstructConnectLoginRequired as exc:
                        login_required = True
                        project.error = str(exc)
                        if not try_update_sheet_status(sheets_service, project, "Error"):
                            sheet_update_failed = True
                        print(f"  RETRY ERROR: {project.project_id} - {exc}")
                        break
                    except Exception as exc:
                        project.error = str(exc)
                        if not try_update_sheet_status(sheets_service, project, "Error"):
                            sheet_update_failed = True
                        print(f"  RETRY ERROR: {project.project_id} - {exc}")
        finally:
            context.close()

    print_summary(projects)
    if login_required:
        print("\nConstructConnect login refresh is required before the scheduled workflow can continue.")
        return 1
    if sheet_update_failed:
        print("\nOne or more ConstructConnect rows processed, but Sheet status updates failed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
