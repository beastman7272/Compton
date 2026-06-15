#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

START_URL = "https://app.buildingconnected.com/opportunities/pipeline"
PROFILE_DIR = Path("playwright_bc_profile")
DOWNLOAD_DIR = Path.home() / "Downloads"
DEFAULT_PROJECT = "Lee County General Services Building Expansion"
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--start-maximized"]
NAV_TIMEOUT_MS = 120_000
DOWNLOAD_TIMEOUT_MS = 180_000
SKIP_FOLDER_KEYWORDS = [
    "assessment", "budget", "cad", "drawings", "drws", "dwgs", "geotech", "geo-tech",
    "geotechnical", "manual", "narrative", "pay request", "permit", "photos", "pricing",
    "procedure", "purchase order",
]
FOLDER_SKIP_EXCEPTIONS = ["plans", "specifications", "specs"]
SKIP_FILE_KEYWORDS = SKIP_FOLDER_KEYWORDS + [
    "affidavit", "air conditioner", "certification", "civil", "communications", "conditions",
    "concrete", "conductors", "control", "controls", "conveying systems", "drainage",
    "drawing", "ductwork", "earthwork", "electrical", "electronic", "engine", "equipment",
    "erosion", "fittings", "fixtures", "fuses", "generator", "grounding", "heat pump",
    "hvac", "insurance", "instructions", "landscape", "lighting", "lightning", "masonry",
    "mechanical", "metals", "motor", "outlet", "outlets", "paint", "pavement", "photo",
    "piping", "plans", "plumbing", "power", "procurement", "questionnaire", "requirements",
    "refrigerant", "retail", "revisions", "sample standard", "sanitary", "seals",
    "sedimentation", "seismic", "sewage", "specialties", "stormwater", "structural", "surge",
    "terms", "testing", "utilities.pdf", "ventilator", "vibration", "voice", "windows",
    "wiring", "wood & plastics",
]


@dataclass(slots=True)
class Candidate:
    text: str
    title: str
    aria: str
    tag: str
    score: float
    href: str = ""

    @property
    def combined(self) -> str:
        return " ".join(x for x in (self.text, self.title, self.aria) if x)


@dataclass(slots=True)
class BidRow:
    text: str
    tag: str
    role: str
    href: str
    x: float
    y: float


def log(message: str) -> None:
    print(f"[BC] {message}")


def norm(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def project_tokens(project: str) -> list[str]:
    return [w for w in norm(project).split() if len(w) > 2]


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def likely_match(project: str, candidate_text: str) -> bool:
    target = norm(project)
    candidate = norm(candidate_text)
    tokens = project_tokens(project)
    hits = sum(1 for token in tokens if token in candidate)
    return target in candidate or hits >= max(3, len(tokens) - 1) or similarity(project, candidate) >= 0.82


def bad_candidate_text(value: str) -> bool:
    lower = (value or "").lower()
    return (
        "recently viewed" in lower
        or "compton sales projects bid board qualifications" in lower
        or len(value) > 260
    )


def is_project_detail_url(url: str) -> bool:
    return bool(re.search(r"/opportunities/[0-9a-f]+/", url))


def pause_for_user(reason: str) -> None:
    print(f"\n{reason}")
    input("Fix/confirm the browser state, then press Enter to continue...")


def click_text_if_visible(page: Page, text: str, timeout: int = 8_000) -> bool:
    try:
        locator = page.get_by_text(text, exact=False).first
        locator.wait_for(state="visible", timeout=timeout)
        locator.click()
        page.wait_for_timeout(1_500)
        return True
    except Exception:
        return False


def open_pipeline(page: Page) -> None:
    try:
        page.goto(START_URL, wait_until="commit", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pause_for_user("Timed out opening BuildingConnected. The browser may be waiting on Autodesk login.")

    page.wait_for_timeout(3_000)


def ensure_pipeline(page: Page) -> None:
    log("Opening BuildingConnected pipeline")
    open_pipeline(page)

    if not click_text_if_visible(page, "Undecided", timeout=12_000):
        pause_for_user("Could not click the Undecided tab. Login/MFA may be required.")
        open_pipeline(page)
        if not click_text_if_visible(page, "Undecided", timeout=12_000):
            raise RuntimeError("Could not reach the Undecided bid board.")


def sort_by_name(page: Page) -> None:
    if click_text_if_visible(page, "Name", timeout=5_000):
        log("Clicked Name header for sorting")
    else:
        log("Name header not found; continuing with current sort")
    page.wait_for_timeout(2_000)


def dom_candidates(page: Page, project: str) -> list[Candidate]:
    tokens = project_tokens(project)
    raw = page.evaluate(
        """
        ({ tokens, target }) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const norm = (s) => clean(s).toLowerCase().replace(/[^\\w\\s]/g, ' ');
          const targetNorm = norm(target);
          const nodes = [...document.querySelectorAll('a,button,[role=row],tr,[title],[aria-label]')];
          const seen = new Set();
          const out = [];

          for (const el of nodes) {
            if (['SCRIPT', 'STYLE', 'SVG', 'PATH'].includes(el.tagName)) continue;
            const text = clean(el.innerText || el.textContent || '');
            const title = clean(el.getAttribute('title'));
            const aria = clean(el.getAttribute('aria-label'));
            const combined = clean([text, title, aria].filter(Boolean).join(' '));
            const context = clean(el.closest('nav,aside,header,[class*=recent],[class*=Recent]')?.innerText || '');
            if (!combined || combined.length > 260) continue;
            if (/recently viewed/i.test(combined) || /recently viewed/i.test(context)) continue;
            if (/compton sales projects bid board qualifications/i.test(combined)) continue;

            const key = combined.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);

            const haystack = norm(combined);
            const hits = tokens.filter((token) => haystack.includes(token)).length;
            if (hits < 2 && !haystack.includes(targetNorm)) continue;

            let score = hits / Math.max(tokens.length, 1);
            if (haystack.includes(targetNorm)) score += 1;
            if (el.matches('a,button,[role=row],tr')) score += 0.2;

            const link = el.closest('a[href*="/opportunities/"]') || el.querySelector?.('a[href*="/opportunities/"]');
            out.push({ text, title, aria, tag: el.tagName, score, href: link?.href || '' });
          }

          return out.sort((a, b) => b.score - a.score).slice(0, 10);
        }
        """,
        {"tokens": tokens, "target": project},
    )
    return [Candidate(**item) for item in raw]


def click_best_dom_candidate(page: Page, project: str) -> Candidate | None:
    tokens = project_tokens(project)
    raw = page.evaluate(
        """
        ({ tokens, target }) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const norm = (s) => clean(s).toLowerCase().replace(/[^\\w\\s]/g, ' ');
          const targetNorm = norm(target);
          const nodes = [...document.querySelectorAll('a,button,[role=row],tr,[title],[aria-label]')];
          let best = null;

          for (const el of nodes) {
            const text = clean(el.innerText || el.textContent || '');
            const title = clean(el.getAttribute('title'));
            const aria = clean(el.getAttribute('aria-label'));
            const combined = clean([text, title, aria].filter(Boolean).join(' '));
            const context = clean(el.closest('nav,aside,header,[class*=recent],[class*=Recent]')?.innerText || '');
            if (!combined || combined.length > 260) continue;
            if (/recently viewed/i.test(combined) || /recently viewed/i.test(context)) continue;
            if (/compton sales projects bid board qualifications/i.test(combined)) continue;

            const haystack = norm(combined);
            const hits = tokens.filter((token) => haystack.includes(token)).length;
            if (hits < 3 && !haystack.includes(targetNorm)) continue;

            let score = hits / Math.max(tokens.length, 1);
            if (haystack.includes(targetNorm)) score += 1;
            if (el.matches('a,button,[role=row],tr')) score += 0.2;

            const link = el.closest('a[href*="/opportunities/"]') || el.querySelector?.('a[href*="/opportunities/"]');
            const clickable = link || el.closest('a,button,[role=row],tr') || el;
            if (link) score += 0.4;

            if (!best || score > best.score) {
              best = { el, clickable, text, title, aria, tag: el.tagName, score, href: link?.href || '' };
            }
          }

          if (!best) return null;
          best.clickable.scrollIntoView({ block: 'center' });
          best.clickable.click();
          return {
            text: best.text,
            title: best.title,
            aria: best.aria,
            tag: best.tag,
            score: best.score,
            href: best.href,
          };
        }
        """,
        {"tokens": tokens, "target": project},
    )
    return Candidate(**raw) if raw else None


def click_project_text_fallback(page: Page, project: str) -> bool:
    log("Trying visible project text/coordinate fallbacks")
    for exact in (True, False):
        try:
            locator = page.get_by_text(project, exact=exact).first
            locator.wait_for(state="visible", timeout=4_000)
            locator.scroll_into_view_if_needed(timeout=4_000)
            log(f"Trying text click fallback exact={exact}")
            locator.click(timeout=4_000)
            page.wait_for_timeout(2_000)
            if is_project_detail_url(page.url):
                return True

            page.keyboard.press("Enter")
            page.wait_for_timeout(2_000)
            if is_project_detail_url(page.url):
                return True

            locator.dblclick(timeout=4_000)
            page.wait_for_timeout(2_000)
            if is_project_detail_url(page.url):
                return True
        except Exception:
            pass

    boxes = page.evaluate(
        """
        (target) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const targetLower = clean(target).toLowerCase();
          const boxes = [];
          const nodes = [...document.querySelectorAll('a,button,[role=row],tr,div,span')];

          for (const el of nodes) {
            const text = clean(el.innerText || el.textContent || '');
            if (!text.toLowerCase().includes(targetLower)) continue;

            const chain = [el];
            let parent = el.parentElement;
            for (let i = 0; parent && i < 5; i += 1, parent = parent.parentElement) {
              chain.push(parent);
            }

            for (const node of chain) {
              const rect = node.getBoundingClientRect();
              if (rect.width < 20 || rect.height < 12) continue;
              if (rect.bottom < 0 || rect.top > window.innerHeight) continue;
              if (rect.right < 0 || rect.left > window.innerWidth) continue;

              boxes.push({
                tag: node.tagName,
                role: node.getAttribute('role') || '',
                cls: String(node.className || '').slice(0, 80),
                text: clean(node.innerText || node.textContent || '').slice(0, 160),
                x: Math.max(5, Math.min(rect.left + Math.min(120, rect.width / 2), window.innerWidth - 5)),
                y: Math.max(5, Math.min(rect.top + Math.min(24, rect.height / 2), window.innerHeight - 5)),
              });
            }
          }

          return boxes.slice(0, 10);
        }
        """,
        project,
    )

    log(f"Coordinate fallback candidates: {len(boxes)}")
    for idx, box in enumerate(boxes, start=1):
        log(f"Trying coordinate click {idx}: {box['tag']} role={box['role']} class={box['cls']}")
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(2_000)
        if is_project_detail_url(page.url):
            return True

        page.mouse.dblclick(box["x"], box["y"])
        page.wait_for_timeout(2_000)
        if is_project_detail_url(page.url):
            return True

    return False


def scroll_bid_board(page: Page) -> bool:
    before_text = page.locator("body").inner_text(timeout=5_000)[:4_000]
    changed = page.evaluate(
        """
        () => {
          const candidates = [...document.querySelectorAll('div, main, section, [role=grid], [role=table]')]
            .map((el) => ({
              el,
              overflow: getComputedStyle(el).overflowY,
              room: el.scrollHeight - el.clientHeight,
              top: el.scrollTop,
              rect: el.getBoundingClientRect(),
            }))
            .filter((item) => item.room > 80 && item.rect.height > 120)
            .sort((a, b) => b.room - a.room);

          const target = candidates[0];
          if (target) {
            target.el.scrollTop = Math.min(target.top + 240, target.el.scrollHeight);
            return target.el.scrollTop !== target.top;
          }

          const doc = document.scrollingElement;
          const before = doc.scrollTop;
          doc.scrollTop = Math.min(before + 240, doc.scrollHeight);
          return doc.scrollTop !== before;
        }
        """
    )
    page.wait_for_timeout(2_200)

    if not changed:
        page.mouse.wheel(0, 360)
        page.wait_for_timeout(2_200)

    after_wheel = page.locator("body").inner_text(timeout=5_000)[:4_000]
    if after_wheel != before_text:
        return True

    page.keyboard.press("PageDown")
    page.wait_for_timeout(2_200)
    after_key = page.locator("body").inner_text(timeout=5_000)[:4_000]
    return after_key != before_text


def open_exact_project_text(page: Page, project: str) -> Candidate | None:
    boxes = page.evaluate(
        """
        (project) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const target = clean(project).toLowerCase();
          const out = [];

          for (const el of document.querySelectorAll('a,button,[role=row],tr,div,span')) {
            const text = clean(el.innerText || el.textContent || '');
            if (!text.toLowerCase().includes(target)) continue;

            const rect = el.getBoundingClientRect();
            const visible =
              rect.width > 40 &&
              rect.height > 12 &&
              rect.bottom >= 0 &&
              rect.top <= window.innerHeight &&
              rect.right >= 0 &&
              rect.left <= window.innerWidth;
            if (!visible) continue;

            const link = el.closest('a[href*="/opportunities/"]') || el.querySelector?.('a[href*="/opportunities/"]');
            out.push({
              tag: el.tagName,
              text,
              href: link?.href || '',
              x: Math.max(5, Math.min(rect.left + Math.min(180, rect.width / 3), window.innerWidth - 5)),
              y: Math.max(5, Math.min(rect.top + rect.height / 2, window.innerHeight - 5)),
            });
          }

          return out.sort((a, b) => a.text.length - b.text.length).slice(0, 8);
        }
        """,
        project,
    )

    for box in boxes:
        log(f"Found exact project text in visible DOM: {box['text'][:120]}")
        if box["href"]:
            page.goto(box["href"], wait_until="commit", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(2_500)
            if is_project_detail_url(page.url):
                return Candidate(project, "", "", box["tag"], 2.0, box["href"])

        for click_count in (1, 2):
            page.mouse.click(box["x"], box["y"], click_count=click_count)
            page.wait_for_timeout(2_500)
            if is_project_detail_url(page.url):
                return Candidate(project, "", "", box["tag"], 2.0)

    try:
        locator = page.get_by_text(project, exact=False).first
        locator.wait_for(state="attached", timeout=1_500)
        locator.scroll_into_view_if_needed(timeout=4_000)
        page.wait_for_timeout(800)
        log("Found exact project text outside visible-row scan")

        for click_count in (1, 2):
            locator.click(timeout=4_000, click_count=click_count)
            page.wait_for_timeout(2_500)
            if is_project_detail_url(page.url):
                return Candidate(project, "", "", "TEXT", 2.0)

        if click_project_text_fallback(page, project):
            return Candidate(project, "", "", "TEXT", 2.0)
    except Exception:
        return None

    return None


def reset_bid_board_scroll(page: Page) -> None:
    page.evaluate(
        """
        () => {
          for (const el of document.querySelectorAll('div, main, section, [role=grid], [role=table]')) {
            if (el.scrollHeight - el.clientHeight > 80) el.scrollTop = 0;
          }
          if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
        }
        """
    )
    page.wait_for_timeout(1_000)


def go_to_next_bid_board_page(page: Page) -> bool:
    changed = page.evaluate(
        """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const controls = [...document.querySelectorAll('button, a, [role=button]')];

          for (const el of controls) {
            const label = clean([
              el.innerText || el.textContent || '',
              el.getAttribute('aria-label') || '',
              el.getAttribute('title') || '',
            ].join(' '));

            const disabled =
              el.disabled ||
              el.getAttribute('aria-disabled') === 'true' ||
              /disabled/i.test(el.className || '');

            if (!disabled && /(^|\\b)(next|caret-right|›|»)(\\b|$)/i.test(label)) {
              el.scrollIntoView({ block: 'center' });
              el.click();
              return true;
            }
          }

          return false;
        }
        """
    )
    if changed:
        page.wait_for_timeout(3_000)
        reset_bid_board_scroll(page)
    return bool(changed)


def visible_bid_rows(page: Page) -> list[BidRow]:
    rows = page.evaluate(
        """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const nodes = [...document.querySelectorAll(
            '[role=row], tr, [class*=Row], [class*=row]'
          )];
          const seen = new Set();
          const out = [];

          for (const el of nodes) {
            const rect = el.getBoundingClientRect();
            if (rect.width < 250 || rect.height < 16) continue;
            if (rect.bottom < 0 || rect.top > window.innerHeight) continue;

            const text = clean(el.innerText || el.textContent || '');
            if (text.length < 25 || text.length > 500) continue;

            const lower = text.toLowerCase();
            if (lower.includes('filtered by') || lower.includes('assign name bid date')) continue;

            const rowLike =
              /\\d{1,2}\\/\\d{1,2}\\/\\d{4}/.test(text) ||
              /\\d+h\\s+\\d+m/.test(text) ||
              lower.includes('bidding') ||
              lower.includes('decline');
            if (!rowLike) continue;

            if (seen.has(text)) continue;
            seen.add(text);

            const link = el.querySelector?.('a[href*="/opportunities/"]') || el.closest?.('a[href*="/opportunities/"]');
            out.push({
              text,
              tag: el.tagName,
              role: el.getAttribute('role') || '',
              href: link?.href || '',
              x: Math.max(5, Math.min(rect.left + Math.min(180, rect.width / 3), window.innerWidth - 5)),
              y: Math.max(5, Math.min(rect.top + rect.height / 2, window.innerHeight - 5)),
            });
          }

          return out.sort((a, b) => a.y - b.y);
        }
        """
    )
    return [BidRow(**row) for row in rows]


def row_label(text: str) -> str:
    match = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d+h\s+\d+m\b", text)
    label = text[: match.start()].strip() if match else text
    return re.sub(r"\s+", " ", label)[:180]


def print_visible_bid_rows(scan: int, rows: list[BidRow]) -> None:
    print(f"[BC] Scan {scan}: {len(rows)} visible bid rows")
    for idx, row in enumerate(rows, start=1):
        print(f"     {idx:02d}. {row_label(row.text)}")


def row_matches_project(project: str, row: BidRow) -> bool:
    target = norm(project)
    row_text = norm(row.text)
    return target in row_text or all(token in row_text for token in project_tokens(project))


def open_bid_row(page: Page, row: BidRow) -> bool:
    if row.href:
        log(f"Opening row href directly: {row.href}")
        page.goto(row.href, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(3_000)
        return is_project_detail_url(page.url)

    log(f"Opening matching row by coordinates: {row_label(row.text)}")
    for click_count in (1, 2):
        page.mouse.click(row.x, row.y, click_count=click_count)
        page.wait_for_timeout(2_500)
        if is_project_detail_url(page.url):
            return True
    return False


def current_page_has_project(page: Page, project: str) -> Candidate | None:
    if not is_project_detail_url(page.url):
        return None

    candidates = [c for c in dom_candidates(page, project) if not bad_candidate_text(c.combined)]
    if not candidates:
        return None

    best = candidates[0]
    if likely_match(project, best.combined):
        log("Current page already appears to be the target project")
        return best

    return None


def print_search_debug(page: Page, project: str) -> None:
    print(f"\nSearch debug URL: {page.url}")
    try:
        title = page.title()
        if title:
            print(f"Search debug title: {title}")
    except Exception:
        pass

    body = page.locator("body").inner_text(timeout=5_000)
    tokens = project_tokens(project)
    hints = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and any(token in norm(line) for token in tokens)
    ][:12]

    if hints:
        print("Search debug matching text:")
        for line in hints:
            print(f"  {line[:180]}")
    else:
        print("Search debug matching text: -- none --")

    controls = page.evaluate(
        """
        () => [...document.querySelectorAll('button, a, [role=button]')]
          .map((el) => ({
            text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
            aria: el.getAttribute('aria-label') || '',
            title: el.getAttribute('title') || '',
            disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true',
          }))
          .filter((x) => x.text || x.aria || x.title)
          .slice(-25)
        """
    )
    print("Search debug controls:")
    for control in controls:
        label = " | ".join(x for x in (control["text"], control["aria"], control["title"]) if x)
        print(f"  {'disabled ' if control['disabled'] else ''}{label[:160]}")


def find_and_open_project(page: Page, project: str) -> Candidate | None:
    log(f"Searching Undecided for: {project}")

    current = current_page_has_project(page, project)
    if current:
        return current

    for scan in range(1, 80):
        exact = open_exact_project_text(page, project)
        if exact:
            return exact

        rows = visible_bid_rows(page)
        print_visible_bid_rows(scan, rows)

        for row in rows:
            if not row_matches_project(project, row):
                continue

            log(f"Matched visible row: {row_label(row.text)}")
            if open_bid_row(page, row) or click_project_text_fallback(page, project):
                return Candidate(row.text, "", "", row.tag, 2.0, row.href)
            log("Matching row found, but project detail page did not open")

        if scroll_bid_board(page):
            log("Scrolled bid board")
        elif go_to_next_bid_board_page(page):
            log("Moved to next bid-board page")
        else:
            break

    print_search_debug(page, project)
    return None


def extract_details(page: Page) -> dict[str, str]:
    body = page.locator("body").inner_text(timeout=10_000)
    lines = [line.strip() for line in body.splitlines() if line.strip()]

    def after_labels(*labels: str) -> str:
        for label in labels:
            inline = re.search(rf"{re.escape(label)}\s*:?\s*([^\n]+)", body, re.I)
            if inline and inline.group(1).strip().lower() != label.lower():
                return inline.group(1).strip()[:160]

            for idx, line in enumerate(lines[:-1]):
                if line.lower().rstrip(":") == label.lower().rstrip(":"):
                    return lines[idx + 1][:160]

        return ""

    due = ""
    due_match = re.search(
        r"(Due Date|Bid Date|Bids Due|Bid Due|Due)\s*:?\s*"
        r"([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
        body,
        re.I,
    )
    if due_match:
        due = due_match.group(2).strip()
    else:
        for idx, line in enumerate(lines):
            if re.fullmatch(r"(due date|bid date|bids due|bid due|due):?", line, re.I):
                due = lines[idx + 1] if idx + 1 < len(lines) else ""
                break

    return {
        "Location": after_labels("Location", "Project Location", "Address"),
        "Project Size": after_labels("Project Size", "Size", "Square Feet", "Sq Ft"),
        "Due Date": due,
    }


def print_detail_hints(page: Page) -> None:
    body = page.locator("body").inner_text(timeout=10_000)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    hints = [
        line
        for line in lines
        if re.search(r"location|address|size|square|sq ft|due|bid date", line, re.I)
    ][:20]

    if hints:
        print("\nDetail-related page text:")
        for line in hints:
            print(f"  {line[:180]}")


def open_files_tab(page: Page) -> bool:
    log("Opening Files tab")
    for text in ("Files", "Project Files", "Documents"):
        if click_text_if_visible(page, text, timeout=8_000):
            page.wait_for_timeout(4_000)
            return True
    return False


def visible_file_items(page: Page) -> list[dict[str, str]]:
    return page.evaluate(
        """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const nodes = [...document.querySelectorAll(
            '[role=row], tr, a, button, [title], [aria-label], [class*=file], [class*=File], [class*=folder], [class*=Folder]'
          )];
          const seen = new Set();
          const out = [];

          for (const el of nodes) {
            const text = clean(el.innerText || el.textContent || '');
            const title = clean(el.getAttribute('title'));
            const aria = clean(el.getAttribute('aria-label'));
            const combined = clean([text, title, aria].filter(Boolean).join(' | '));
            if (!combined || combined.length > 500) continue;

            const lower = combined.toLowerCase();
            const looksRelevant =
              /\\.(pdf|zip|docx?|xlsx?|dwg)\\b/i.test(combined) ||
              lower.includes('folder') ||
              lower.includes('download') ||
              lower.includes('spec') ||
              lower.includes('plan') ||
              lower.includes('addendum');

            if (!looksRelevant) continue;
            if (seen.has(combined.toLowerCase())) continue;
            seen.add(combined.toLowerCase());

            out.push({
              tag: el.tagName,
              role: el.getAttribute('role') || '',
              text,
              title,
              aria,
            });
          }

          return out.slice(0, 80);
        }
        """
    )


def print_visible_file_items(page: Page) -> None:
    items = visible_file_items(page)
    print("\nVisible Files Tab Items:")
    if not items:
        print("  -- none detected --")
        return

    for idx, item in enumerate(items, start=1):
        parts = [item["text"], item["title"], item["aria"]]
        label = " | ".join(part for part in parts if part)
        print(f"  {idx:02d}. [{item['tag']}{' role=' + item['role'] if item['role'] else ''}] {label[:240]}")


def extract_file_name(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    match = re.match(r"(.+?)\s+(?:\d+(?:\.\d+)?\s*(?:KB|MB|GB)|\d{1,2}/\d{1,2}/\d{4})\b", text, re.I)
    return (match.group(1) if match else text).strip()


def is_probable_file(name: str) -> bool:
    return bool(re.search(r"\.(pdf|zip|docx?|xlsx?|dwg|rvt|txt)\b", name, re.I))


def should_skip_folder(name: str) -> tuple[bool, str]:
    lower = name.lower()
    skip = next((word for word in SKIP_FOLDER_KEYWORDS if word in lower), "")
    exception = next((word for word in FOLDER_SKIP_EXCEPTIONS if word in lower), "")
    if skip and not exception:
        return True, f"folder skip keyword: {skip}"
    if skip and exception:
        return False, f"folder exception: {exception}"
    return False, "folder allowed"


def should_skip_file(name: str) -> tuple[bool, str]:
    lower = name.lower()
    skip = next((word for word in SKIP_FILE_KEYWORDS if word in lower), "")
    if skip:
        return True, f"file skip keyword: {skip}"
    return False, "file allowed"


def classified_file_items(page: Page) -> list[tuple[str, str, str, str]]:
    items = visible_file_items(page)
    seen: set[str] = set()
    classified = []

    for item in items:
        raw = item["text"] or item["title"] or item["aria"]
        name = extract_file_name(raw)
        if not name or name.lower() in {"download all", "plan room pro"}:
            continue
        if "download all" in name.lower() or "copy all to internal files" in name.lower():
            continue
        if " name indicator size date modified " in f" {name.lower()} ":
            continue
        if "\n" in name or len(name) > 180:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())

        kind = "file" if is_probable_file(name) else "folder"
        if kind == "folder":
            skip, reason = should_skip_folder(name)
            action = "SKIP" if skip else "OPEN"
        else:
            skip, reason = should_skip_file(name)
            action = "SKIP" if skip else "SELECT"

        classified.append((kind, action, name, reason))

    return classified


def print_file_classification(page: Page) -> None:
    print("\nFile Classification Preview:")
    rows = classified_file_items(page)
    if not rows:
        print("  -- no classifiable files/folders detected --")
        return

    for kind, action, name, reason in rows:
        print(f"  {action:6} {kind:6} {name} ({reason})")


def print_classification_rows(rows: list[tuple[str, str, str, str]]) -> None:
    if not rows:
        print("  -- no newly visible files/folders detected --")
        return

    for kind, action, name, reason in rows:
        print(f"  {action:6} {kind:6} {name} ({reason})")


def select_file_by_name(page: Page, file_name: str) -> bool:
    return bool(
        page.evaluate(
            """
            (fileName) => {
              const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const target = clean(fileName).toLowerCase();
              const nodes = [...document.querySelectorAll('div, span, a, [role=row], tr')]
                .map((el) => ({ el, text: clean(el.innerText || el.textContent || '') }))
                .filter(({ text }) => {
                  const lower = text.toLowerCase();
                  return lower === target || lower.startsWith(`${target} `);
                })
                .sort((a, b) => a.text.length - b.text.length);

              for (const { el } of nodes) {
                const row = el.closest('[role=row], tr, [class*=row], [class*=Row]') || el.parentElement || el;
                const checkbox = row.querySelector(
                  'input[type=checkbox], [role=checkbox], [aria-checked]'
                );

                if (checkbox) {
                  checkbox.scrollIntoView({ block: 'center' });
                  checkbox.click();
                  return true;
                }

                const rect = row.getBoundingClientRect();
                if (rect.width > 60 && rect.height > 12) {
                  const x = Math.max(5, Math.min(rect.left + 22, window.innerWidth - 5));
                  const y = Math.max(5, Math.min(rect.top + rect.height / 2, window.innerHeight - 5));
                  const targetAtPoint = document.elementFromPoint(x, y);
                  if (targetAtPoint) {
                    targetAtPoint.click();
                    return true;
                  }
                }
              }

              return false;
            }
            """,
            file_name,
        )
    )


def select_classified_files(page: Page, rows: list[tuple[str, str, str, str]], label: str) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    files = [name for kind, action, name, _reason in rows if kind == "file" and action == "SELECT"]

    if not files:
        print(f"\nSelection Preview ({label}): -- no selectable files --")
        return selected

    print(f"\nSelection Preview ({label}):")
    for file_name in files:
        key = file_name.lower()
        if key in seen:
            continue
        seen.add(key)

        if select_file_by_name(page, file_name):
            selected.append(file_name)
            print(f"  SELECTED {file_name}")
        else:
            print(f"  FAILED   {file_name}")

    page.wait_for_timeout(1_000)
    return selected


def open_folder_by_name(page: Page, folder_name: str) -> bool:
    before = page.locator("body").inner_text(timeout=5_000)
    for exact in (True, False):
        try:
            locator = page.get_by_text(folder_name, exact=exact).first
            locator.wait_for(state="visible", timeout=4_000)
            locator.scroll_into_view_if_needed(timeout=4_000)
            log(f"Opening folder: {folder_name}")
            locator.click(timeout=4_000)
            page.wait_for_timeout(3_000)
            after = page.locator("body").inner_text(timeout=5_000)
            if after != before:
                return True

            locator.dblclick(timeout=4_000)
            page.wait_for_timeout(3_000)
            after = page.locator("body").inner_text(timeout=5_000)
            if after != before:
                return True
        except Exception:
            pass

    return False


def return_to_files_root(page: Page, root_url: str) -> None:
    try:
        page.go_back(wait_until="commit", timeout=10_000)
        page.wait_for_timeout(2_000)
    except Exception:
        pass

    if page.url != root_url:
        try:
            page.goto(root_url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(2_000)
        except Exception:
            pass

    open_files_tab(page)


def reset_files_view(page: Page) -> None:
    """Reload the project files view so inline-expanded folders collapse."""
    try:
        page.reload(wait_until="commit", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(3_000)
    except Exception:
        pass
    open_files_tab(page)


def preview_allowed_folders(page: Page) -> None:
    root_rows = classified_file_items(page)
    root_names = {name.lower() for _kind, _action, name, _reason in root_rows}
    folders = [
        name
        for kind, action, name, _reason in root_rows
        if kind == "folder" and action == "OPEN"
    ]

    if not folders:
        print("\nAllowed Folder Preview: -- no allowed folders detected --")
        return

    root_url = page.url
    print("\nAllowed Folder Preview:")
    for folder_name in folders:
        print(f"\nFolder: {folder_name}")
        if not open_folder_by_name(page, folder_name):
            print("  -- could not open folder --")
            continue

        child_rows = [
            row
            for row in classified_file_items(page)
            if row[2].lower() not in root_names
        ]
        print_classification_rows(child_rows)
        return_to_files_root(page, root_url)


def select_allowed_files(page: Page) -> list[str]:
    selected: list[str] = []
    reset_files_view(page)

    root_rows = classified_file_items(page)
    root_names = {name.lower() for _kind, _action, name, _reason in root_rows}
    selected.extend(select_classified_files(page, root_rows, "root files"))

    folders = [
        name
        for kind, action, name, _reason in root_rows
        if kind == "folder" and action == "OPEN"
    ]

    root_url = page.url
    for folder_name in folders:
        reset_files_view(page)
        if not open_folder_by_name(page, folder_name):
            print(f"\nSelection Preview ({folder_name}): -- could not open folder --")
            continue

        child_rows = [
            row
            for row in classified_file_items(page)
            if row[2].lower() not in root_names
        ]
        selected.extend(select_classified_files(page, child_rows, folder_name))
        return_to_files_root(page, root_url)

    print("\nSelected Files Summary:")
    if selected:
        unique_selected = list(dict.fromkeys(selected))
        for file_name in unique_selected:
            print(f"  - {file_name}")
    else:
        print("  -- no files selected --")

    return list(dict.fromkeys(selected))


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return name[:180] or "buildingconnected_download"


def unique_download_path(filename: str) -> Path:
    target = DOWNLOAD_DIR / safe_filename(filename)
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    for idx in range(1, 100):
        candidate = DOWNLOAD_DIR / f"{stem} ({idx}){suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not create unique download path for {filename}")


def download_selected_files(page: Page) -> Path:
    log("Clicking Download All for selected files")
    button = page.get_by_text("Download All", exact=False).first
    button.wait_for(state="visible", timeout=20_000)

    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
        button.click()

    download = download_info.value
    target = unique_download_path(download.suggested_filename)
    download.save_as(str(target))
    print(f"\nDownloaded File: {target}")
    return target


def launch_context(pw, browser: str, headless: bool):
    channels = {
        "auto": ["chrome", "msedge", None],
        "chrome": ["chrome"],
        "msedge": ["msedge"],
        "chromium": [None],
    }[browser]

    last_error = None
    for channel in channels:
        try:
            label = channel or "bundled chromium"
            profile_dir = PROFILE_DIR / (channel or "chromium")
            log(f"Launching {label} with profile {profile_dir}")
            kwargs = {
                "user_data_dir": str(profile_dir),
                "headless": headless,
                "accept_downloads": True,
                "args": LAUNCH_ARGS,
                "viewport": None,
            }
            if channel:
                kwargs["channel"] = channel
            return pw.chromium.launch_persistent_context(**kwargs)
        except PlaywrightError as exc:
            last_error = exc
            log(f"Could not launch {channel or 'bundled chromium'}: {exc}")

    raise RuntimeError(f"Could not launch any requested browser: {last_error}")


def attach_context(pw, cdp_url: str):
    log(f"Attaching to browser at {cdp_url}")
    browser = pw.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else browser.new_context(accept_downloads=True)
    return browser, context


def main() -> int:
    parser = argparse.ArgumentParser(description="BuildingConnected Playwright POC")
    parser.add_argument("project", nargs="?", default=DEFAULT_PROJECT)
    parser.add_argument("--browser", choices=["auto", "chrome", "msedge", "chromium"], default="auto")
    parser.add_argument("--cdp-url", help="Attach to an already-open Chrome/Edge debugging session.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--project-url", help="Open a known BuildingConnected project URL and skip bid-board search.")
    parser.add_argument("--select-files", action="store_true", help="Select eligible files but do not download.")
    parser.add_argument("--download-files", action="store_true", help="Select eligible files and download them to ~/Downloads.")
    args = parser.parse_args()

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = None
        if args.cdp_url:
            browser, context = attach_context(pw, args.cdp_url)
        else:
            context = launch_context(pw, args.browser, args.headless)

        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page = context.pages[0] if context.pages else context.new_page()

        if args.project_url:
            log(f"Opening supplied project URL: {args.project_url}")
            page.goto(args.project_url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(3_000)
            candidate = Candidate(args.project, "", "", "URL", 2.0, args.project_url)
        else:
            existing_candidate = current_page_has_project(page, args.project)
            if existing_candidate:
                candidate = existing_candidate
            else:
                ensure_pipeline(page)
                sort_by_name(page)

                candidate = find_and_open_project(page, args.project)
        if not candidate:
            pause_for_user("Project was not found automatically.")
            candidate = find_and_open_project(page, args.project)

        print("\n" + "=" * 70)
        if not candidate:
            print(f"Status: NOT FOUND\nProject: {args.project}")
        else:
            details = extract_details(page)
            print(f"Status: FOUND\nProject: {args.project}")
            print(f"URL: {page.url}")
            print(f"Matched DOM Text: {candidate.combined[:220]}")
            if not is_project_detail_url(page.url):
                print("Project detail page was not opened; skipping detail/files extraction.")
            else:
                for label, value in details.items():
                    print(f"{label}: {value or '--'}")
                if any(not value or value == "--" for value in details.values()):
                    print_detail_hints(page)
                if open_files_tab(page):
                    print_visible_file_items(page)
                    print_file_classification(page)
                    preview_allowed_folders(page)
                    if args.select_files or args.download_files:
                        selected = select_allowed_files(page)
                        if args.download_files:
                            if selected:
                                download_selected_files(page)
                            else:
                                print("\nDownload skipped: no files were selected.")
                else:
                    print("\nFiles tab was not found/opened.")
        print("=" * 70)

        if not args.cdp_url:
            input("\nPress Enter to close the browser...")

        if not args.cdp_url:
            context.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
