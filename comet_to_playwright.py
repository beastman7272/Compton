# buildingconnected_file_downloader_v1.py

from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
import time

START_URL = "https://app.buildingconnected.com/opportunities/pipeline"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

PROJECTS = [
    "Cornelia Amphitheater",
    "Gordon Lee High School Tennis Courts",
]

FOLDER_SKIP_WORDS = [
    "assessment", "budget", "cad", "drawings", "dwgs", "geo-tech", "geotechnical",
    "manual", "narrative", "pay request", "permit", "photospricing", "procedure",
    "purchase order",
]

FOLDER_EXCEPTION_WORDS = ["plans", "specifications", "specs"]

FILE_SKIP_WORDS = [
    "assessment", "budget", "cad", "drawings", "dwgs", "geo-tech", "geotechnical",
    "manual", "narrative", "pay request", "permit", "photospricing", "procedure",
    "purchase order", "affidavit", "air conditioner", "certification", "civil",
    "communications", "conditions", "concrete", "conductors", "control", "controls",
    "conveying systems", "drainage", "drawing", "ductwork", "earthwork",
    "electrical", "electronic", "engine", "equipment", "erosion", "fittings",
    "fixtures", "fuses", "generator", "geotech", "grounding", "heat pump", "hvac",
    "insurance", "instructions", "landscape", "lighting", "lightning", "masonry",
    "mechanical", "metals", "motor", "outlet", "outlets", "paint", "pavement",
    "photo", "photos", "piping", "plans", "plumbing", "power", "procurement",
    "questionnaire", "requirements", "refrigerant", "retail", "revisions",
    "sample standard", "sanitary", "seals", "sedimentation", "seismic", "sewage",
    "specialties", "stormwater", "structural", "surge", "terms", "testing",
    "utilities.pdf", "ventilator", "vibration", "voice", "windows", "wiring",
    "wood & plastics",
]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_project_search_name(raw: str) -> str:
    name = norm(raw)

    # High-confidence delimiter from your example.
    if "*" in name:
        name = name.split("*", 1)[0].strip()

    # Soft cleanup for noisy email-derived subjects.
    cleanup_prefixes = [
        "fwd:",
        "re:",
        "bid invite:",
        "reminder:",
    ]

    lower = name.lower()
    for prefix in cleanup_prefixes:
        if lower.startswith(prefix):
            name = name[len(prefix):].strip()
            lower = name.lower()

    cleanup_phrases = [
        " bid date correction",
        " please note",
        " all bids are due",
        " reply to ",
    ]

    lower = name.lower()
    for phrase in cleanup_phrases:
        idx = lower.find(phrase)
        if idx > 0:
            name = name[:idx].strip()
            lower = name.lower()

    return norm(name)


def should_skip_folder(folder_name: str) -> bool:
    name = folder_name.lower()

    has_skip = any(word in name for word in FOLDER_SKIP_WORDS)
    has_exception = any(word in name for word in FOLDER_EXCEPTION_WORDS)

    return has_skip and not has_exception


def should_skip_file(filename: str) -> bool:
    name = filename.lower()
    return any(word in name for word in FILE_SKIP_WORDS)


def safe_text(locator, default=""):
    try:
        return norm(locator.inner_text(timeout=2_000))
    except Exception:
        return default


def click_if_visible(page, text: str, timeout=5_000) -> bool:
    try:
        loc = page.get_by_text(text, exact=False).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.click()
        return True
    except Exception:
        return False


def go_to_undecided(page):
    page.goto(START_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3_000)

    if not click_if_visible(page, "Undecided", timeout=10_000):
        raise RuntimeError("Could not find/click the Undecided tab.")

    page.wait_for_timeout(3_000)


def find_and_open_project(page, project_name: str) -> bool:
    """
    First-pass approach:
    - Search visible page text for the cleaned project name.
    - Click the matching project title/card/row.
    - Pagination/scrolling can be added after we see actual behavior.
    """

    search_name = clean_project_search_name(project_name)
    print(f"[INFO] Looking for project: {search_name}")

    # Try direct visible text match first.
    candidates = [
        page.get_by_text(search_name, exact=True),
        page.get_by_text(search_name, exact=False),
    ]

    for candidate in candidates:
        try:
            loc = candidate.first
            loc.wait_for(state="visible", timeout=7_000)
            loc.click()
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            page.wait_for_timeout(3_000)
            return True
        except Exception:
            pass

    # Try partial fallback using first several words.
    words = search_name.split()
    if len(words) >= 3:
        partial = " ".join(words[:3])
        try:
            loc = page.get_by_text(partial, exact=False).first
            loc.wait_for(state="visible", timeout=5_000)
            loc.click()
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            page.wait_for_timeout(3_000)
            return True
        except Exception:
            pass

    return False


def extract_project_details(page):
    """
    First-pass text-based extraction.
    We will harden this after seeing the actual DOM labels.
    """

    body = safe_text(page.locator("body"), "")

    def grab_after(label):
        pattern = rf"{re.escape(label)}\s*[:\n]?\s*(.+)"
        match = re.search(pattern, body, re.IGNORECASE)
        if not match:
            return ""
        value = match.group(1).split("\n")[0].strip()
        return value[:120]

    location = grab_after("Location")
    project_size = grab_after("Project Size")

    due_date = ""
    due_match = re.search(
        r"(Due Date|Bid Date|Bids Due)\s*[:\n]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
        body,
        re.IGNORECASE,
    )
    if due_match:
        due_date = due_match.group(2).strip()

    return {
        "Location": location,
        "Project Size": project_size,
        "Due Date": due_date,
    }


def go_to_files_tab(page):
    if not click_if_visible(page, "Files", timeout=10_000):
        raise RuntimeError("Could not find/click Files tab.")

    page.wait_for_timeout(3_000)


def get_visible_file_rows(page):
    """
    Generic first-pass row detection.
    We capture rows that appear to contain file/folder-like text.
    This may need selector tuning after a run.
    """

    possible_rows = page.locator("tr, [role='row'], .row, [class*='file'], [class*='File']")
    rows = []

    count = min(possible_rows.count(), 300)

    for i in range(count):
        row = possible_rows.nth(i)
        text = safe_text(row)
        if not text:
            continue

        lower = text.lower()

        looks_relevant = (
            ".pdf" in lower
            or ".zip" in lower
            or "folder" in lower
            or "download" in lower
            or "spec" in lower
            or "plan" in lower
        )

        if looks_relevant:
            rows.append((row, text))

    return rows


def row_looks_like_folder(text: str) -> bool:
    lower = text.lower()

    # Conservative guesses.
    if ".pdf" in lower or ".zip" in lower or ".doc" in lower or ".xls" in lower:
        return False

    return "folder" in lower or "files" in lower


def extract_probable_filename(row_text: str) -> str:
    lines = [norm(x) for x in row_text.splitlines() if norm(x)]

    # Prefer a line with a recognizable extension.
    for line in lines:
        if re.search(r"\.(pdf|zip|docx?|xlsx?)\b", line, re.IGNORECASE):
            return line

    return lines[0] if lines else row_text[:120]


def select_eligible_main_files(page):
    rows = get_visible_file_rows(page)

    selected_files = []
    skipped_files = []
    folder_candidates = []

    for row, text in rows:
        filename = extract_probable_filename(text)

        if row_looks_like_folder(text):
            if should_skip_folder(filename):
                print(f"[SKIP FOLDER] {filename}")
            else:
                print(f"[FOLDER CANDIDATE - NOT OPENED IN V1] {filename}")
                folder_candidates.append(filename)
            continue

        if should_skip_file(filename):
            print(f"[SKIP FILE] {filename}")
            skipped_files.append(filename)
            continue

        # Try to click a checkbox in the row.
        try:
            checkbox = row.locator("input[type='checkbox']").first
            checkbox.wait_for(state="attached", timeout=1_000)
            checkbox.check(force=True)
            print(f"[SELECT FILE] {filename}")
            selected_files.append(filename)
            continue
        except Exception:
            pass

        # Alternate checkbox patterns.
        try:
            row.get_by_role("checkbox").first.check(force=True, timeout=1_000)
            print(f"[SELECT FILE] {filename}")
            selected_files.append(filename)
            continue
        except Exception:
            print(f"[WARN] Could not select row: {filename}")

    return selected_files, skipped_files, folder_candidates


def click_download_selected(page):
    """
    Attempts to click Download Selected and save any browser download.
    Some sites generate a zip asynchronously; this may need adjustment.
    """

    button_texts = [
        "Download Selected",
        "Download selected",
        "Download",
    ]

    for text in button_texts:
        try:
            button = page.get_by_text(text, exact=False).first
            button.wait_for(state="visible", timeout=5_000)

            with page.expect_download(timeout=20_000) as download_info:
                button.click()

            download = download_info.value
            target = DOWNLOAD_DIR / download.suggested_filename
            download.save_as(target)
            print(f"[DOWNLOADED] {target}")
            return str(target)

        except PlaywrightTimeoutError:
            # Some apps prepare downloads after a modal or do not trigger immediately.
            pass
        except Exception as e:
            print(f"[WARN] Download click attempt failed for '{text}': {e}")

    print("[WARN] Download may not have started. Check browser manually.")
    return ""


def process_project(page, raw_project_name: str):
    clean_name = clean_project_search_name(raw_project_name)

    result = {
        "Project": clean_name,
        "Status": "NOT FOUND",
        "Location": "",
        "Project Size": "",
        "Due Date": "",
        "Files Downloaded": [],
        "Skipped Files": [],
        "Folder Candidates": [],
        "Download Path": "",
    }

    go_to_undecided(page)

    found = find_and_open_project(page, raw_project_name)
    if not found:
        return result

    result["Status"] = "FOUND"

    details = extract_project_details(page)
    result.update(details)

    go_to_files_tab(page)

    selected_files, skipped_files, folder_candidates = select_eligible_main_files(page)

    result["Files Downloaded"] = selected_files
    result["Skipped Files"] = skipped_files
    result["Folder Candidates"] = folder_candidates

    if selected_files:
        download_path = click_download_selected(page)
        result["Download Path"] = download_path

    return result


def print_result(result):
    print("\n---PROJECT RESULT---")
    print(f"Project: {result['Project']}")
    print(f"Status: {result['Status']}")
    print(f"Location: {result['Location']}")
    print(f"Project Size: {result['Project Size']}")
    print(f"Due Date: {result['Due Date']}")
    print("Files Downloaded:")

    if result["Files Downloaded"]:
        for f in result["Files Downloaded"]:
            print(f"  - {f}")
    else:
        print("  - None selected/downloaded in v1")

    if result["Folder Candidates"]:
        print("Folder Candidates Not Opened In V1:")
        for f in result["Folder Candidates"]:
            print(f"  - {f}")

    if result["Download Path"]:
        print(f"Download Path: {result['Download Path']}")

    print("---END PROJECT---\n")


def main():
    with sync_playwright() as p:
        # Persistent profile keeps your login session.
        browser_context = p.chromium.launch_persistent_context(
            user_data_dir="bc_browser_profile",
            channel="chrome",
            headless=False,
            accept_downloads=True,
            downloads_path=str(DOWNLOAD_DIR),
            viewport={"width": 1400, "height": 900},
        )

        page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")

        input(
            "\nLog in manually, complete any Autodesk/CAPTCHA steps, "
            "and make sure you are on the BuildingConnected pipeline page. "
            "Then press Enter here to continue...\n"
        )

        all_results = []

        for raw_project in PROJECTS:
            try:
                result = process_project(page, raw_project)
                all_results.append(result)
                print_result(result)
            except Exception as e:
                print(f"[ERROR] Failed processing project: {raw_project}")
                print(e)

        input("\nReview the browser if needed. Press Enter to close...\n")
        browser_context.close()


if __name__ == "__main__":
    main()