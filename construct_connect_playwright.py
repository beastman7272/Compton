from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from playwright.sync_api import Download, Locator, Page, sync_playwright

SPREADSHEET_ID = "1vqEd71BGHNMDJdBcymM4cgGzEQhlXsib3sFXY9Qlt7U"
TAB_NAME = "Fortress"
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

SEARCH_URL = "https://insight.cmdgroup.com/SearchResult/ProjectSearchResult/Index"
DOWNLOAD_DIR = Path.home() / "Downloads"
HEADLESS = False
EXPORT_CLEANUP_ATTEMPTS = 3
EXPORT_CLEANUP_WAIT_MS = 5000
MENU_CLICK_TIMEOUT_MS = 60000
DOWNLOAD_TIMEOUT_MS = 120000
EXPORT_WAIT_TIMEOUT_MS = 90000

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


@dataclass
class Project:
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


def get_creds() -> Credentials:
    creds = None

    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return creds


def clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def get_sheets_service():
    creds = get_creds()
    return build("sheets", "v4", credentials=creds)


def fetch_projects() -> list[Project]:
    sheets_service = get_sheets_service()
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"{TAB_NAME}!A:L")
        .execute()
    )

    values = result.get("values", [])
    
    if len(values) < 2:
        raise RuntimeError(f"No project rows found in tab '{TAB_NAME}'.")

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


def wait_for_manual_login(page: Page) -> None:
    log("Opening ConstructConnect search page")
    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    try:
        if page.locator("#demo-input-local").first.is_visible():
            log("Existing ConstructConnect session detected")
            return
    except Exception:
        pass

    print("\nPlease log in to ConstructConnect manually in the opened browser.")
    print("This login should persist in the Playwright profile until ConstructConnect times out.")
    print("When login is complete and the site is ready, press Enter here.\n")
    input()
    page.wait_for_timeout(1500)


def open_search(page: Page) -> None:
    log("Opening search page")
    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    wait_visible(page, "#demo-input-local")


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


def row_menu_arrow(row: Locator) -> Locator:
    container = row.locator("xpath=ancestor::*[.//img[contains(@class,'miniDownArrow')]][1]").first
    return container.locator("img.miniDownArrow:visible").first


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

                arrow = row_menu_arrow(row)
                arrow.wait_for(state="visible", timeout=MENU_CLICK_TIMEOUT_MS)
                arrow.click(force=True, timeout=MENU_CLICK_TIMEOUT_MS)
                doc_page.wait_for_timeout(1200)

                container = row.locator(
                    "xpath=ancestor::*[.//img[contains(@class,'miniDownArrow')]][1]"
                ).first

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
        print(f"• Project ID: {p.project_id}")
        print(f"• State: {p.state}")
        print(f"• Bid Date: {p.bid_date}")
        print(f"• Project Value: {p.project_value}")

        if p.error:
            print(f"• Documents Exported: ERROR - {p.error}")
        else:
            docs = p.documents_exported or ["No qualifying documents found"]
            print(f"• Documents Exported: {', '.join(docs)}")




def update_sheet_status(sheets_service, row_number: int, value: str) -> None:
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{TAB_NAME}!L{row_number}",
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()


def main() -> int:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    sheets_service = get_sheets_service()
    projects = fetch_projects()

    if not projects:
        print("\nNo unprocessed projects found.")
        return 0

    print("\nProjects queued:")
    for p in projects:
        print(f"[Row {p.row_number}] - {p.title} - {p.project_id} - {p.status}")

    with sync_playwright() as pw:
        profile_dir = Path("playwright_cc_profile")
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=HEADLESS,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        wait_for_manual_login(page)

        for project in projects:
            if project.status == "Skipped":
                continue

            print(f"\nProcessing Row {project.row_number}: {project.title} ({project.project_id})")
            try:
                process_project(page, project)
                update_sheet_status(sheets_service, project.row_number, "Processed")
            except Exception as exc:
                project.error = str(exc)
                update_sheet_status(sheets_service, project.row_number, "Error")
                print(f"  ERROR: {exc}")

        pending = verify_all_processed(projects)
        if pending:
            print("\nRetrying pending projects once...")
            for project in pending:
                try:
                    process_project(page, project)
                    project.error = ""
                    update_sheet_status(sheets_service, project.row_number, "Processed")
                except Exception as exc:
                    project.error = str(exc)
                    update_sheet_status(sheets_service, project.row_number, "Error")
                    print(f"  RETRY ERROR: {project.project_id} - {exc}")

        context.close()

    print_summary(projects)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
