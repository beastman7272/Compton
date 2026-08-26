#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import time
from datetime import datetime
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from app import config
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

START_URL = "https://app.buildingconnected.com/opportunities/pipeline"
PROFILE_DIR = config.BC_PLAYWRIGHT_PROFILE_DIR
DOWNLOAD_DIR = config.DOWNLOADS_DIR
DEFAULT_PROJECT = "Lee County General Services Building Expansion"
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--start-maximized"]
HOSTED_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-session-crashed-bubble",
    "--no-sandbox",
]
RECOVERY_UI_TIMEOUT_MS = 2_000
PIPELINE_READY_POLL_MS = 500
BODY_TEXT_PROBE_TIMEOUT_MS = 1_500
NAV_TIMEOUT_MS = 120_000
DOWNLOAD_TIMEOUT_MS = 180_000
HOSTED_PIPELINE_READY_TIMEOUT_MS = 45_000
HOSTED_PIPELINE_STABILITY_MS = 7_000
LOCAL_PIPELINE_READY_TIMEOUT_MS = 12_000
HOSTED_UNDECIDED_TAB_TIMEOUT_MS = 15_000
LOCAL_UNDECIDED_TAB_TIMEOUT_MS = 8_000
HOSTED_VIEWPORT = {"width": 1440, "height": 1000}
LOG_ENABLED = True
LOGIN_REQUIRED_MESSAGE = (
    "BuildingConnected login/MFA required. Open the Playwright browser profile, "
    "complete Autodesk login, then rerun the scheduled workflow."
)
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


class BuildingConnectedLoginRequired(RuntimeError):
    """Raised when the persistent browser profile needs a fresh Autodesk login."""


def log(message: str) -> None:
    if LOG_ENABLED:
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


def project_detail_panel_is_open(page: Page) -> bool:
    """Detect BC's in-page detail panel, which keeps the /pipeline URL."""
    try:
        return bool(
            page.evaluate(
                r"""
                () => {
                  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && rect.bottom >= 0 &&
                      rect.top <= window.innerHeight && style.display !== 'none' &&
                      style.visibility !== 'hidden';
                  };
                  const labels = [...document.querySelectorAll('a, button, [role=tab]')]
                    .filter(visible)
                    .map((el) => clean(el.innerText || el.textContent || ''));
                  return labels.some((x) => /^files(?:\s+\d+)?$/.test(x)) &&
                    labels.some((x) => /^messages(?:\s+\d+)?$/.test(x)) &&
                    labels.some((x) => /^bid form$/.test(x));
                }
                """
            )
        )
    except Exception:
        return False


def is_project_detail_view(page: Page) -> bool:
    return is_project_detail_url(page.url) or project_detail_panel_is_open(page)


def pause_for_user(reason: str) -> None:
    print(f"\n{reason}")
    input("Fix/confirm the browser state, then press Enter to continue...")


def looks_like_login_page(page: Page) -> bool:
    current_url = (page.url or "").lower()
    login_markers = (
        "login.autodesk.com",
        "signin",
        "sign-in",
        "oauth",
        "authorize",
        "authentication",
    )
    if any(marker in current_url for marker in login_markers):
        return True

    try:
        title = page.title().lower()
    except Exception:
        title = ""

    return "autodesk" in title and any(
        marker in title for marker in ("sign in", "login", "verify", "authentication")
    )


def click_text_if_visible(page: Page, text: str, timeout: int = 8_000) -> bool:
    try:
        locator = page.get_by_text(text, exact=False).first
        locator.wait_for(state="visible", timeout=timeout)
        locator.click()
        page.wait_for_timeout(1_500)
        return True
    except Exception:
        return False


def pipeline_ready_timeout_ms() -> int:
    return HOSTED_PIPELINE_READY_TIMEOUT_MS if config.HOSTED_RUNTIME else LOCAL_PIPELINE_READY_TIMEOUT_MS


def undecided_tab_timeout_ms() -> int:
    return HOSTED_UNDECIDED_TAB_TIMEOUT_MS if config.HOSTED_RUNTIME else LOCAL_UNDECIDED_TAB_TIMEOUT_MS


def playwright_launch_args() -> list[str]:
    args = list(LAUNCH_ARGS)
    if config.HOSTED_RUNTIME:
        args.extend(HOSTED_LAUNCH_ARGS)
    return args


def dismiss_browser_recovery_ui(page: Page) -> bool:
    """Dismiss Chromium 'Restore pages?' infobar if it blocks the Bid Board UI."""
    button_patterns = [
        ("restore", re.compile(r"^Restore$", re.I)),
        ("restore_pages", re.compile(r"Restore pages", re.I)),
        ("cancel", re.compile(r"^Cancel$", re.I)),
        ("close", re.compile(r"^Close$", re.I)),
        ("not_now", re.compile(r"Not now", re.I)),
        ("dont_restore", re.compile(r"Don'?t restore", re.I)),
    ]
    for label, pattern in button_patterns:
        try:
            button = page.get_by_role("button", name=pattern).first
            button.wait_for(state="visible", timeout=RECOVERY_UI_TIMEOUT_MS)
            button.click()
            page.wait_for_timeout(500)
            log(f"Dismissed crash-restore dialog via {label}")
            return True
        except Exception:
            continue

    for label, pattern in button_patterns:
        try:
            button = page.locator("button").filter(has_text=pattern).first
            button.wait_for(state="visible", timeout=RECOVERY_UI_TIMEOUT_MS)
            button.click()
            page.wait_for_timeout(500)
            log(f"Dismissed crash-restore dialog via button filter ({label})")
            return True
        except Exception:
            continue

    text_patterns = [
        ("restore_text", re.compile(r"^Restore$", re.I)),
        ("restore_pages_text", re.compile(r"Restore pages", re.I)),
        ("cancel_text", re.compile(r"^Cancel$", re.I)),
        ("close_text", re.compile(r"^Close$", re.I)),
    ]
    for label, pattern in text_patterns:
        try:
            target = page.get_by_text(pattern).first
            target.wait_for(state="visible", timeout=RECOVERY_UI_TIMEOUT_MS)
            target.click()
            page.wait_for_timeout(500)
            log(f"Dismissed crash-restore dialog via text locator ({label})")
            return True
        except Exception:
            continue

    try:
        page.get_by_text(
            re.compile(r"Restore pages|didn['']t shut down correctly|Chromium didn", re.I)
        ).first.wait_for(state="visible", timeout=800)
        with contextlib.suppress(Exception):
            page.get_by_text(re.compile(r"^Restore$", re.I)).first.click()
            page.wait_for_timeout(500)
            log("Dismissed crash-restore dialog via infobar Restore text")
            return True
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        log("Dismissed crash-restore dialog via Escape")
        return True
    except Exception:
        pass

    with contextlib.suppress(Exception):
        page.keyboard.press("Escape")
    return False


def _locator_visible(locator: Locator, *, timeout_ms: int = 500) -> bool:
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def pipeline_is_ready(page: Page) -> tuple[bool, str]:
    """Return whether the Bid Board pipeline appears loaded and which check matched."""
    undecided_tab = page.get_by_role("tab", name=re.compile(r"Undecided", re.I)).first
    if _locator_visible(undecided_tab):
        return True, "undecided_tab"

    bid_board_text = page.get_by_text("Bid Board", exact=False).first
    if _locator_visible(bid_board_text):
        return True, "bid_board_text"

    try:
        url = (page.url or "").lower()
        title = page.title().lower()
        if "opportunities/pipeline" in url and "bid board" in title:
            return True, "url_title"
    except Exception:
        pass

    undecided_text = page.get_by_text(re.compile(r"Undecided", re.I)).first
    if _locator_visible(undecided_text):
        return True, "undecided_text"

    try:
        body_text = page.locator("body").inner_text(timeout=BODY_TEXT_PROBE_TIMEOUT_MS)
        if re.search(r"Undecided", body_text, re.I):
            return True, "body_undecided"
    except Exception:
        pass

    return False, ""


def is_on_undecided_view(page: Page) -> bool:
    try:
        selected_tab = page.locator("[role='tab'][aria-selected='true']").filter(
            has_text=re.compile(r"Undecided", re.I)
        ).first
        if _locator_visible(selected_tab, timeout_ms=800):
            return True
    except Exception:
        pass

    try:
        tab = page.get_by_role("tab", name=re.compile(r"Undecided", re.I)).first
        if tab.is_visible() and tab.get_attribute("aria-selected") == "true":
            return True
    except Exception:
        pass

    return False


def log_pipeline_failure_debug(page: Page, *, reason: str) -> None:
    if not config.HOSTED_RUNTIME:
        return

    try:
        url = page.url
    except Exception:
        url = "<unavailable>"
    try:
        title = page.title()
    except Exception:
        title = "<unavailable>"
    log(f"Pipeline failure ({reason}): url={url!r} title={title!r}")

    failure_dir = config.LOG_ROOT / "bc-playwright-failures"
    failure_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = failure_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    with contextlib.suppress(Exception):
        page.screenshot(path=str(screenshot_path), full_page=True)
        log(f"Saved failure screenshot: {screenshot_path}")


def wait_for_pipeline_ready(page: Page, *, timeout_ms: int | None = None) -> None:
    timeout_ms = timeout_ms or pipeline_ready_timeout_ms()
    deadline = time.monotonic() + (timeout_ms / 1000)

    while time.monotonic() < deadline:
        ready, reason = pipeline_is_ready(page)
        if ready:
            log(f"Pipeline ready via {reason} (timeout budget={timeout_ms}ms)")
            return
        remaining_ms = int(max(0, (deadline - time.monotonic()) * 1000))
        page.wait_for_timeout(min(PIPELINE_READY_POLL_MS, remaining_ms))

    raise PlaywrightTimeoutError(f"Pipeline not ready within {timeout_ms}ms")


def wait_for_pipeline_stability(page: Page) -> None:
    """Ensure hosted Chromium survives the initial BC SPA hydration window."""
    if not config.HOSTED_RUNTIME:
        return

    deadline = time.monotonic() + (HOSTED_PIPELINE_STABILITY_MS / 1000)
    while time.monotonic() < deadline:
        # A crashed renderer can still expose page.url; evaluate forces a round trip
        # to the renderer and raises PlaywrightError when the target has crashed.
        page.evaluate("document.readyState")
        remaining_ms = int(max(0, (deadline - time.monotonic()) * 1000))
        page.wait_for_timeout(min(PIPELINE_READY_POLL_MS, remaining_ms))

    ready, reason = pipeline_is_ready(page)
    if not ready:
        raise PlaywrightTimeoutError(
            "Pipeline stopped being ready during the hosted stability check"
        )
    log(f"Pipeline remained stable via {reason} for {HOSTED_PIPELINE_STABILITY_MS}ms")


def navigate_to_pipeline(page: Page, *, non_interactive: bool = False) -> None:
    try:
        page.goto(START_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        if non_interactive:
            raise BuildingConnectedLoginRequired(LOGIN_REQUIRED_MESSAGE)
        pause_for_user("Timed out opening BuildingConnected. The browser may be waiting on Autodesk login.")
        return

    dismiss_browser_recovery_ui(page)

    try:
        wait_for_pipeline_ready(page)
    except PlaywrightTimeoutError:
        dismiss_browser_recovery_ui(page)
        if looks_like_login_page(page):
            if non_interactive:
                raise BuildingConnectedLoginRequired(LOGIN_REQUIRED_MESSAGE)
            pause_for_user(LOGIN_REQUIRED_MESSAGE)
            return
        log_pipeline_failure_debug(page, reason="pipeline_ready_timeout")
        if non_interactive:
            raise RuntimeError(
                "BuildingConnected pipeline did not finish loading. "
                "Login/MFA may be required, or the Bid Board UI may have changed."
            )
        pause_for_user("BuildingConnected pipeline did not finish loading.")

    if looks_like_login_page(page):
        if non_interactive:
            raise BuildingConnectedLoginRequired(LOGIN_REQUIRED_MESSAGE)
        pause_for_user(LOGIN_REQUIRED_MESSAGE)


def click_undecided_tab(page: Page, *, non_interactive: bool = False) -> bool:
    if is_on_undecided_view(page):
        log("Already on Undecided view")
        return True

    per_strategy_timeout = undecided_tab_timeout_ms()
    retry_delay_ms = 1_500
    strategies = [
        ("role_tab", lambda: page.get_by_role("tab", name=re.compile(r"Undecided", re.I)).first),
        (
            "tab_locator",
            lambda: page.locator("[role='tab']").filter(
                has_text=re.compile(r"Undecided", re.I)
            ).first,
        ),
        ("text_undecided", lambda: page.get_by_text(re.compile(r"^Undecided$", re.I)).first),
    ]

    def try_all_strategies(attempt: int) -> str | None:
        for name, locator_fn in strategies:
            try:
                locator = locator_fn()
                locator.wait_for(state="visible", timeout=per_strategy_timeout)
                locator.click()
                page.wait_for_timeout(retry_delay_ms)
                log(f"Undecided tab clicked via {name} (attempt {attempt})")
                return name
            except Exception:
                continue
        return None

    if try_all_strategies(1):
        return True

    page.wait_for_timeout(retry_delay_ms)
    if try_all_strategies(2):
        return True

    log("Reloading pipeline URL before final Undecided tab attempt")
    navigate_to_pipeline(page, non_interactive=non_interactive)
    return try_all_strategies(3) is not None


def open_pipeline(page: Page, *, non_interactive: bool = False) -> None:
    navigate_to_pipeline(page, non_interactive=non_interactive)


def ensure_pipeline(page: Page, *, non_interactive: bool = False) -> None:
    log("Opening BuildingConnected pipeline")
    navigate_to_pipeline(page, non_interactive=non_interactive)

    if click_undecided_tab(page, non_interactive=non_interactive):
        wait_for_pipeline_stability(page)
        return

    if looks_like_login_page(page):
        if non_interactive:
            raise BuildingConnectedLoginRequired(LOGIN_REQUIRED_MESSAGE)
        pause_for_user(LOGIN_REQUIRED_MESSAGE)
        navigate_to_pipeline(page, non_interactive=non_interactive)
        if click_undecided_tab(page, non_interactive=non_interactive):
            wait_for_pipeline_stability(page)
            return

    if non_interactive:
        log_pipeline_failure_debug(page, reason="undecided_tab_unreachable")
        raise RuntimeError(
            "Could not reach the BuildingConnected Undecided tab. "
            "Login/MFA may be required, or the Bid Board UI may have changed."
        )
    pause_for_user("Could not click the Undecided tab. Login/MFA may be required.")
    navigate_to_pipeline(page, non_interactive=non_interactive)
    if not click_undecided_tab(page, non_interactive=non_interactive):
        log_pipeline_failure_debug(page, reason="undecided_tab_unreachable")
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
    # A partial-text locator can resolve to the entire row; clicking its center can
    # land on the Location link. Use only the project-name element here.
    for exact in (True,):
        try:
            locator = page.get_by_text(project, exact=exact).first
            locator.wait_for(state="visible", timeout=4_000)
            locator.scroll_into_view_if_needed(timeout=4_000)
            log(f"Trying text click fallback exact={exact}")
            locator.click(timeout=4_000)
            page.wait_for_timeout(2_000)
            if is_project_detail_view(page):
                return True

            page.keyboard.press("Enter")
            page.wait_for_timeout(2_000)
            if is_project_detail_view(page):
                return True

            locator.dblclick(timeout=4_000)
            page.wait_for_timeout(2_000)
            if is_project_detail_view(page):
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
              if (rect.width < 20 || rect.height < 12 || rect.width > 650 || rect.height > 90) continue;
              if (rect.bottom < 0 || rect.top > window.innerHeight) continue;
              if (rect.right < 0 || rect.left > window.innerWidth) continue;

              boxes.push({
                tag: node.tagName,
                role: node.getAttribute('role') || '',
                cls: String(node.className || '').slice(0, 80),
                text: clean(node.innerText || node.textContent || '').slice(0, 160),
                width: rect.width,
                height: rect.height,
                x: Math.max(5, Math.min(rect.left + Math.min(120, rect.width / 2), window.innerWidth - 5)),
                y: Math.max(5, Math.min(rect.top + Math.min(24, rect.height / 2), window.innerHeight - 5)),
              });
            }
          }

          return boxes.sort((a, b) => (a.width * a.height) - (b.width * b.height)).slice(0, 10);
        }
        """,
        project,
    )

    log(f"Coordinate fallback candidates: {len(boxes)}")
    for idx, box in enumerate(boxes, start=1):
        log(f"Trying coordinate click {idx}: {box['tag']} role={box['role']} class={box['cls']}")
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(2_000)
        if is_project_detail_view(page):
            return True

        page.mouse.dblclick(box["x"], box["y"])
        page.wait_for_timeout(2_000)
        if is_project_detail_view(page):
            return True

    return False


def scroll_bid_board(page: Page) -> bool:
    before_text = page.locator("body").inner_text(timeout=5_000)[:4_000]
    # Advance most of a viewport but keep ~1-2 rows of overlap so lazy-loaded
    # projects are not skipped between scans.
    changed = page.evaluate(
        """
        () => {
          const overlapPx = 140;
          const stepFor = (clientHeight) => {
            const h = clientHeight || 600;
            return Math.max(240, Math.min(900, h - overlapPx));
          };

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
            const step = stepFor(target.el.clientHeight);
            target.el.scrollTop = Math.min(target.top + step, target.el.scrollHeight);
            return target.el.scrollTop !== target.top;
          }

          const doc = document.scrollingElement;
          const before = doc.scrollTop;
          const step = stepFor(doc.clientHeight || window.innerHeight);
          doc.scrollTop = Math.min(before + step, doc.scrollHeight);
          return doc.scrollTop !== before;
        }
        """
    )
    page.wait_for_timeout(2_200)

    if not changed:
        page.mouse.wheel(0, 720)
        page.wait_for_timeout(2_200)

    after_wheel = page.locator("body").inner_text(timeout=5_000)[:4_000]
    if after_wheel != before_text:
        return True

    page.keyboard.press("PageDown")
    page.wait_for_timeout(2_200)
    after_key = page.locator("body").inner_text(timeout=5_000)[:4_000]
    return after_key != before_text


def current_project_heading(page: Page) -> str:
    """Read the visible project title near the top of the open detail view."""
    try:
        headings = page.evaluate(
            r"""
            () => {
              const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 80 && rect.height > 12 && rect.top >= 0 && rect.top < 260 &&
                  rect.bottom <= window.innerHeight && style.display !== 'none' &&
                  style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
              };
              const generic = /^(bid board|overview|files|messages|bid form|due date|status|links)$/i;
              return [...document.querySelectorAll(
                'h1, h2, [role=heading], [data-testid*="project" i], ' +
                '[class*="project-title" i], [class*="opportunity-title" i]'
              )]
                .filter(visible)
                .map((el) => clean(el.innerText || el.textContent || el.getAttribute('title') || ''))
                .filter((text) => text.length >= 4 && text.length <= 240 && !generic.test(text));
            }
            """
        )
    except Exception:
        headings = []
    return str(headings[0]).strip() if headings else ""


def detail_page_matches_project(page: Page, project: str, *, timeout_ms: int = 10_000) -> bool:
    """True only when the visible detail heading is the intended project."""
    deadline = time.monotonic() + max(timeout_ms, 0) / 1_000
    while True:
        if is_project_detail_view(page):
            heading = current_project_heading(page)
            if heading and likely_match(project, heading):
                return True
            if is_project_detail_url(page.url):
                with contextlib.suppress(Exception):
                    if likely_match(project, page.title()):
                        return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(400)


def accept_opened_project(
    page: Page,
    project: str,
    *,
    matched_text: str = "",
    tag: str = "",
    href: str = "",
) -> Candidate | None:
    if not is_project_detail_view(page):
        return None
    if not detail_page_matches_project(page, project):
        log(
            "Detail page does not match target project "
            f"(opened text: {(matched_text or page.url)[:120]})"
        )
        return None
    log("Confirmed opened detail page matches target project")
    return Candidate(matched_text or project, "", "", tag or "DETAIL", 2.0, href or page.url)


def return_to_undecided_board(page: Page, *, non_interactive: bool = True) -> None:
    """Leave a wrong/partial opportunity page and get back to the Undecided list."""
    log("Returning to Undecided bid board after mismatched open")
    with contextlib.suppress(Exception):
        page.go_back(wait_until="commit", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(2_000)

    if is_project_detail_url(page.url) or "pipeline" not in (page.url or "").lower():
        ensure_pipeline(page, non_interactive=non_interactive)
        sort_by_name(page)
        return

    if not click_undecided_tab(page, non_interactive=non_interactive):
        ensure_pipeline(page, non_interactive=non_interactive)
        sort_by_name(page)


def open_exact_project_text(page: Page, project: str) -> Candidate | None:
    # Only consider row-sized nodes. Fat ancestors can contain the target name
    # somewhere below the fold while their first opportunity link is a different project.
    boxes = page.evaluate(
        """
        (project) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const target = clean(project).toLowerCase();
          if (!target) return [];
          const out = [];
          const nodes = [...document.querySelectorAll(
            '[role=row], tr, a[href*="/opportunities/"], [class*=Row], [class*=row]'
          )];

          for (const el of nodes) {
            const text = clean(el.innerText || el.textContent || '');
            if (text.length < 10 || text.length > 500) continue;
            if (!text.toLowerCase().includes(target)) continue;

            const lower = text.toLowerCase();
            if (lower.includes('filtered by') || lower.includes('assign name bid date')) continue;

            const rect = el.getBoundingClientRect();
            const visible =
              rect.width > 40 &&
              rect.height > 12 &&
              rect.bottom >= 0 &&
              rect.top <= window.innerHeight &&
              rect.right >= 0 &&
              rect.left <= window.innerWidth;
            if (!visible) continue;

            let link = null;
            if (el.matches?.('a[href*="/opportunities/"]')) {
              link = el;
            } else {
              link = el.querySelector?.('a[href*="/opportunities/"]') || null;
            }

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
        label = row_label(box["text"])
        if norm(project) not in norm(box["text"]) and not likely_match(project, label):
            continue

        log(f"Found exact project text in visible DOM: {box['text'][:120]}")
        opened = False
        if box["href"]:
            page.goto(box["href"], wait_until="commit", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(2_500)
            opened = is_project_detail_view(page)

        if not opened:
            for click_count in (1, 2):
                page.mouse.click(box["x"], box["y"], click_count=click_count)
                page.wait_for_timeout(2_500)
                if is_project_detail_view(page):
                    opened = True
                    break

        if not opened:
            continue

        accepted = accept_opened_project(
            page,
            project,
            matched_text=box["text"],
            tag=box["tag"],
            href=box["href"] or page.url,
        )
        if accepted:
            return accepted
        return_to_undecided_board(page)

    try:
        locator = page.get_by_text(project, exact=True).first
        locator.wait_for(state="attached", timeout=1_500)
        locator.scroll_into_view_if_needed(timeout=4_000)
        page.wait_for_timeout(800)
        log("Found exact project text outside visible-row scan")

        opened = False
        for click_count in (1, 2):
            locator.click(timeout=4_000, click_count=click_count)
            page.wait_for_timeout(2_500)
            if is_project_detail_view(page):
                opened = True
                break

        if not opened and click_project_text_fallback(page, project):
            opened = is_project_detail_view(page)

        if opened:
            accepted = accept_opened_project(page, project, matched_text=project, tag="TEXT")
            if accepted:
                return accepted
            return_to_undecided_board(page)
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
        return is_project_detail_view(page)

    log(f"Opening matching row by coordinates: {row_label(row.text)}")
    for click_count in (1, 2):
        page.mouse.click(row.x, row.y, click_count=click_count)
        page.wait_for_timeout(2_500)
        if is_project_detail_view(page):
            return True
    return False


def current_page_has_project(page: Page, project: str) -> Candidate | None:
    if not is_project_detail_view(page):
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

        mismatched_open = False
        for row in rows:
            if not row_matches_project(project, row):
                continue

            log(f"Matched visible row: {row_label(row.text)}")
            if open_bid_row(page, row) or click_project_text_fallback(page, project):
                accepted = accept_opened_project(
                    page,
                    project,
                    matched_text=row.text,
                    tag=row.tag,
                    href=row.href,
                )
                if accepted:
                    return accepted
                return_to_undecided_board(page)
                mismatched_open = True
                log("Matching row opened wrong project; continuing scan")
                break
            log("Matching row found, but project detail page did not open")

        if mismatched_open:
            continue

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
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return (
              rect.width > 0 &&
              rect.height > 0 &&
              rect.bottom >= 0 &&
              rect.top <= window.innerHeight &&
              rect.right >= 0 &&
              rect.left <= window.innerWidth &&
              style.display !== 'none' &&
              style.visibility !== 'hidden'
            );
          };
          const fileGrid = [...document.querySelectorAll('[role=grid][aria-label=grid]')]
            .find((grid) =>
              visible(grid) &&
              grid.querySelector('[data-id=file-name-link], [data-id=file-checkbox]')
            );
          if (!fileGrid) return [];

          const nodes = [...fileGrid.querySelectorAll(
            '[role=row], tr, a, button, [title], [aria-label], [class*=file], [class*=File], [class*=folder], [class*=Folder]'
          )];
          const seen = new Set();
          const out = [];

          for (const el of nodes) {
            if (!visible(el)) continue;
            if (el.closest('[data-id="file-list-tools"]')) continue;
            const text = clean(el.innerText || el.textContent || '');
            const title = clean(el.getAttribute('title'));
            const aria = clean(el.getAttribute('aria-label'));
            const className = clean(el.className?.toString() || '');
            const href = clean(el.getAttribute('href'));
            const combined = clean([text, title, aria].filter(Boolean).join(' | '));
            if (!combined || combined.length > 500) continue;

            const lower = combined.toLowerCase();
            const classLower = className.toLowerCase();
            const looksRelevant =
              /\\.(pdf|zip|docx?|xlsx?|dwg)\\b/i.test(combined) ||
              classLower.includes('folder') ||
              classLower.includes('file') ||
              lower.includes('folder') ||
              lower.includes('download') ||
              lower.includes('spec') ||
              lower.includes('plan') ||
              lower.includes('drawing') ||
              lower.includes('document') ||
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
              className,
              href,
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


def print_files_dom_diagnostics(page: Page) -> None:
    """Print read-only DOM evidence for Files-tab selector investigation."""
    payload = page.evaluate(
        """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const className = (el) => clean(
            typeof el?.className === 'string'
              ? el.className
              : el?.className?.baseVal || ''
          );
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return (
              rect.width > 0 &&
              rect.height > 0 &&
              rect.bottom >= 0 &&
              rect.top <= window.innerHeight &&
              rect.right >= 0 &&
              rect.left <= window.innerWidth &&
              style.display !== 'none' &&
              style.visibility !== 'hidden'
            );
          };
          const summary = (el) => {
            const rect = el.getBoundingClientRect();
            return {
              tag: el.tagName,
              id: el.id || '',
              role: el.getAttribute('role') || '',
              className: className(el).slice(0, 240),
              text: clean(el.innerText || el.textContent || '').slice(0, 320),
              title: clean(el.getAttribute('title')).slice(0, 160),
              ariaLabel: clean(el.getAttribute('aria-label')).slice(0, 160),
              ariaChecked: el.getAttribute('aria-checked') || '',
              ariaExpanded: el.getAttribute('aria-expanded') || '',
              href: clean(el.getAttribute('href')).slice(0, 240),
              visible: visible(el),
              rect: {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
              },
            };
          };
          const ancestors = (el) => {
            const out = [];
            let node = el;
            for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
              out.push(summary(node));
            }
            return out;
          };
          const unique = (nodes, limit) => {
            const seen = new Set();
            const out = [];
            for (const el of nodes) {
              const item = summary(el);
              const key = [
                item.tag,
                item.id,
                item.className,
                item.text,
                item.ariaLabel,
                item.title,
              ].join('|');
              if (seen.has(key)) continue;
              seen.add(key);
              out.push({
                ...item,
                outerHTML: (el.outerHTML || '').slice(0, 1200),
                ancestors: ancestors(el),
              });
              if (out.length >= limit) break;
            }
            return out;
          };

          const currentSelector = [
            ...document.querySelectorAll(
              '[role=row], tr, a, button, [title], [aria-label], ' +
              '[class*=file], [class*=File], [class*=folder], [class*=Folder]'
            ),
          ];
          const all = [...document.querySelectorAll('body *')];
          const fileText = all.filter((el) => {
            const text = clean(el.innerText || el.textContent || '');
            if (!visible(el) || !text || text.length > 360) return false;
            return (
              /\\.(pdf|zip|docx?|xlsx?|dwg|rvt|txt)\\b/i.test(text) ||
              /\\b(addenda?|specifications?|drawings?|general documents)\\b/i.test(text)
            );
          }).sort((a, b) =>
            clean(a.innerText || a.textContent || '').length -
            clean(b.innerText || b.textContent || '').length
          );
          const checkboxLike = [
            ...document.querySelectorAll(
              'input, [role=checkbox], [aria-checked], [class*=check], [class*=Check]'
            ),
          ].filter(visible);
          const tabLike = [
            ...document.querySelectorAll('[role=tab], a, button, [aria-selected]'),
          ].filter((el) => /files?/i.test(clean([
            el.innerText || el.textContent || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('title') || '',
          ].join(' '))));

          const bodyLines = clean(document.body?.innerText || '')
            .split(/(?=\\b(?:Download All|Name|Indicator|Size|Date Modified)\\b)/i)
            .filter((line) =>
              /\\.(pdf|zip|docx?|xlsx?|dwg|rvt|txt)\\b/i.test(line) ||
              /Download All|Date Modified|addenda?|specifications?|drawings?/i.test(line)
            )
            .slice(0, 40)
            .map((line) => line.slice(0, 500));

          return {
            url: location.href,
            title: document.title,
            viewport: { width: window.innerWidth, height: window.innerHeight },
            markers: {
              downloadTestId: Boolean(document.querySelector('[data-testid="download-all-bttn"]')),
              fileListTools: Boolean(document.querySelector('[data-id="file-list-tools"]')),
              currentSelectorCount: currentSelector.length,
              fileTextCount: fileText.length,
              checkboxLikeCount: checkboxLike.length,
              tabLikeCount: tabLike.length,
            },
            tabs: unique(tabLike, 20),
            checkboxLike: unique(checkboxLike, 50),
            fileTextCandidates: unique(fileText, 80),
            currentSelectorCandidates: unique(currentSelector, 80),
            bodyLines,
          };
        }
        """
    )
    print("\n" + "=" * 70)
    print("BUILDINGCONNECTED FILES DOM DIAGNOSTICS (READ ONLY)")
    print("=" * 70)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("=" * 70)


def extract_file_name(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    match = re.match(r"(.+?)\s+(?:\d+(?:\.\d+)?\s*(?:KB|MB|GB)|\d{1,2}/\d{1,2}/\d{4})\b", text, re.I)
    return (match.group(1) if match else text).strip()


SELECTABLE_FILE_EXT_RE = re.compile(r"\.(pdf|zip|docx?|xlsx?|dwg|rvt|txt)\b", re.I)
ANY_FILE_EXT_RE = re.compile(r"\.[a-z0-9]{2,5}\b", re.I)


def is_probable_file(name: str) -> bool:
    return bool(SELECTABLE_FILE_EXT_RE.search(name or ""))


def has_file_extension(name: str) -> bool:
    return bool(ANY_FILE_EXT_RE.search(name or ""))


def item_looks_like_folder(item: dict[str, str]) -> bool:
    class_name = item.get("className", "").lower()
    href = item.get("href", "").lower()
    combined = " ".join(
        item.get(key, "")
        for key in ("text", "title", "aria")
    ).lower()

    return (
        "folder" in class_name
        or "folder" in href
        or "folder" in combined
        or "directory" in class_name
    )


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
        if not name or name.lower() in {
            "download all",
            "download selected",
            "plan room pro",
        }:
            continue
        if (
            "download all" in name.lower()
            or "download selected" in name.lower()
            or "copy all to internal files" in name.lower()
        ):
            continue
        if " name indicator size date modified " in f" {name.lower()} ":
            continue
        if "\n" in name or len(name) > 180:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())

        # Never treat extension-bearing rows (e.g. .mpp) as folders.
        if is_probable_file(name):
            kind = "file"
            skip, reason = should_skip_file(name)
            action = "SKIP" if skip else "SELECT"
        elif has_file_extension(name):
            kind = "file"
            action = "SKIP"
            reason = "non-selectable file type"
        elif item_looks_like_folder(item) or not has_file_extension(name):
            kind = "folder"
            skip, reason = should_skip_folder(name)
            action = "SKIP" if skip else "OPEN"
        else:
            kind = "file"
            action = "SKIP"
            reason = "unclassified file item"

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


def select_file_by_name(page: Page, file_name: str) -> tuple[bool, str]:
    """Try to check the Files-tab checkbox for file_name. Returns (ok, reason)."""
    for attempt in range(1, 4):
        result = page.evaluate(
            """
            async (fileName) => {
              const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const target = clean(fileName).toLowerCase();
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const checked = (el) => {
                const className = (el?.className?.toString() || '').toLowerCase();
                return (
                  Boolean(el?.checked) ||
                  el?.getAttribute?.('aria-checked') === 'true' ||
                  (className.includes('checked') && !className.includes('unchecked'))
                );
              };

              const rowChecked = (row) => {
                const boxes = [...row.querySelectorAll(
                  'input[type=checkbox], [role=checkbox], [aria-checked]'
                )];
                return boxes.some(checked);
              };

              const nodes = [...document.querySelectorAll(
                '[role=row], tr, [class*=row], [class*=Row], div, span, a'
              )]
                .map((el) => ({ el, text: clean(el.innerText || el.textContent || '') }))
                .filter(({ text }) => {
                  const lower = text.toLowerCase();
                  return lower === target || lower.startsWith(`${target} `);
                })
                .sort((a, b) => a.text.length - b.text.length);

              if (!nodes.length) return { ok: false, reason: 'row not found' };

              for (const { el } of nodes) {
                const containers = [];
                let node = el;
                for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                  containers.push(node);
                }

                const row = containers.find((candidate) =>
                  candidate.matches?.('[role=row], tr')
                ) || containers.find((candidate) =>
                  candidate.matches?.('[class*=row], [class*=Row]')
                ) || containers.find((candidate) => {
                  const rect = candidate.getBoundingClientRect();
                  return rect.width > 250 && rect.height > 18 && rect.height < 120;
                }) || el;

                try {
                  row.scrollIntoView({ block: 'center', inline: 'nearest' });
                } catch (e) {}
                await sleep(200);

                const checkbox = row.querySelector(
                  'input[type=checkbox], [role=checkbox], [aria-checked]'
                );
                if (!checkbox) {
                  // Fall through to coordinate click near the left edge.
                } else {
                  checkbox.scrollIntoView({ block: 'center' });
                  if (!checked(checkbox)) {
                    checkbox.click();
                    await sleep(400);
                  }
                  if (checked(checkbox) || rowChecked(row)) {
                    return { ok: true, reason: 'checkbox checked' };
                  }
                  // Some BC controls toggle on a parent button/div, not the input itself.
                  const clickable = checkbox.closest('button, [role=checkbox], label') || checkbox;
                  clickable.click();
                  await sleep(400);
                  if (checked(checkbox) || rowChecked(row)) {
                    return { ok: true, reason: 'checkbox checked via wrapper' };
                  }
                }

                const rect = row.getBoundingClientRect();
                if (rect.width > 60 && rect.height > 12) {
                  const x = Math.max(5, Math.min(rect.left + 18, window.innerWidth - 5));
                  const y = Math.max(5, Math.min(rect.top + rect.height / 2, window.innerHeight - 5));
                  const targetAtPoint = document.elementFromPoint(x, y);
                  if (targetAtPoint) {
                    targetAtPoint.click();
                    await sleep(400);
                    if (rowChecked(row)) {
                      return { ok: true, reason: 'checked via coordinate click' };
                    }
                  }
                }
              }

              return { ok: false, reason: 'checkbox not checked' };
            }
            """,
            file_name,
        )
        if result and result.get("ok"):
            return True, str(result.get("reason") or "selected")

        reason = str((result or {}).get("reason") or "unknown")
        if attempt < 3:
            page.wait_for_timeout(500)
            continue
        return False, reason

    return False, "unknown"


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

        ok, reason = select_file_by_name(page, file_name)
        if ok:
            selected.append(file_name)
            print(f"  SELECTED {file_name}")
        else:
            print(f"  FAILED   {file_name} ({reason})")

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
    # BuildingConnected expands folders inline without changing the URL.
    # Going back in that state leaves the project and clears its selections.
    if page.url == root_url:
        return

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

    folders = [
        name
        for kind, action, name, _reason in root_rows
        if kind == "folder" and action == "OPEN"
    ]

    # Select root files before expanding folders so no reload or navigation is
    # required after the first selection.
    selected.extend(select_classified_files(page, root_rows, "root files"))

    root_url = page.url
    for folder_name in folders:
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


def selected_download_button(
    page: Page,
    *,
    selection_expected: bool = False,
) -> Locator | None:
    if not selection_expected:
        return None

    candidates = [
        (
            'data-testid="download-all-bttn"',
            page.locator('[data-testid="download-all-bttn"]').first,
        ),
        (
            'role="button" name="Download Selected"',
            page.get_by_role(
                "button",
                name=re.compile(r"^\s*download\s+selected\s*$", re.I),
            ).first,
        ),
        (
            "button text",
            page.locator("button:has-text('Download Selected')").first,
        ),
        (
            "input value",
            page.locator(
                "input[type=button][value='Download Selected' i], "
                "input[type=submit][value='Download Selected' i]"
            ).first,
        ),
    ]

    for source, candidate in candidates:
        try:
            candidate.wait_for(state="visible", timeout=4_000)
            label = candidate.evaluate(
                """
                (el) => (
                  el.innerText ||
                  el.textContent ||
                  el.value ||
                  el.getAttribute('aria-label') ||
                  ''
                ).replace(/\\s+/g, ' ').trim()
                """
            )
            if re.fullmatch(r"download\s+selected", str(label), re.I):
                log(f"Confirmed Download Selected control via {source}")
                return candidate
        except Exception:
            continue

    return None


def selected_download_diagnostics(page: Page, expected_selected: int) -> str:
    details = page.evaluate(
        """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const target = document.querySelector('[data-testid="download-all-bttn"]');
          const targetStyle = target ? getComputedStyle(target) : null;
          const targetDetails = target ? {
            label: clean(
              target.innerText ||
              target.textContent ||
              target.value ||
              target.getAttribute('aria-label') ||
              ''
            ),
            visible:
              target.getBoundingClientRect().width > 0 &&
              target.getBoundingClientRect().height > 0 &&
              targetStyle.visibility !== 'hidden' &&
              targetStyle.display !== 'none',
            disabled: Boolean(target.disabled) || target.getAttribute('aria-disabled') === 'true',
          } : null;

          const isChecked = (el) => {
            const className = (el?.className?.toString() || '').toLowerCase();
            return (
              Boolean(el?.checked) ||
              el?.getAttribute?.('aria-checked') === 'true' ||
              (className.includes('checked') && !className.includes('unchecked'))
            );
          };
          const checked = [...document.querySelectorAll(
            'input[type=checkbox], [role=checkbox], [aria-checked]'
          )].filter(isChecked).length;
          return { targetDetails, checked };
        }
        """
    )
    target = details.get("targetDetails")
    checked = details.get("checked", 0)
    return (
        f"target control: {target or 'not found'}; "
        f"verified file names: {expected_selected}; checked controls: {checked}"
    )


def click_selected_download_button(button: Locator) -> None:
    label = button.evaluate(
        """
        (el) => (
          el.innerText ||
          el.textContent ||
          el.value ||
          el.getAttribute('aria-label') ||
          ''
        ).replace(/\\s+/g, ' ').trim()
        """
    )
    if not re.fullmatch(r"download\s+selected", str(label), re.I):
        raise RuntimeError(
            f"Download control changed to {label!r}; refusing to click it."
        )
    button.click(timeout=10_000)


def download_selected_files(page: Page, selected_count: int) -> Path:
    log("Clicking selected-files download control")
    page.wait_for_timeout(1_000)
    button = selected_download_button(page, selection_expected=selected_count > 0)
    if button is None:
        diagnostics = selected_download_diagnostics(page, selected_count)
        raise RuntimeError(
            "No selected-files download button was found; refusing to click Download All. "
            f"Diagnostics: {diagnostics}"
        )

    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
        click_selected_download_button(button)

    download = download_info.value
    target = unique_download_path(download.suggested_filename)
    download.save_as(str(target))
    print(f"\nDownloaded File: {target}")
    return target


def launch_context(pw, browser: str, headless: bool):
    # Headed Chromium is unstable for BC under Railway's Xvfb display, while the
    # same persistent profile is stable with Chromium headless. The noVNC login
    # bootstrap launches Chromium directly and remains headed for login/MFA.
    effective_headless = (
        config.HOSTED_RUNTIME
        or headless
        or config.env_bool("CQE_PLAYWRIGHT_HEADLESS", False)
    )
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
            mode = "headless" if effective_headless else "headed"
            log(f"Launching {label} {mode} with profile {profile_dir}")
            kwargs = {
                "user_data_dir": str(profile_dir),
                "headless": effective_headless,
                "accept_downloads": True,
                "args": playwright_launch_args(),
                "viewport": HOSTED_VIEWPORT if config.HOSTED_RUNTIME else None,
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


def base_result(project: str) -> dict[str, object]:
    return {
        "project": project,
        "status": "NOT FOUND",
        "location": "",
        "project_size": "",
        "due_date": "",
        "files": [],
        "download_path": "",
        "url": "",
        "matched_text": "",
        "error": "",
    }


def page_is_unusable(page: Page, result: dict[str, object] | None = None) -> bool:
    try:
        if page.is_closed():
            return True
    except Exception:
        return True

    if result and result.get("status") == "ERROR":
        error = str(result.get("error") or "").lower()
        crash_markers = (
            "page crashed",
            "target closed",
            "context destroyed",
            "has been closed",
            "execution context was destroyed",
        )
        if any(marker in error for marker in crash_markers):
            return True

    try:
        page.evaluate("document.readyState")
    except Exception:
        return True
    return False


def open_fresh_page(context, page: Page | None = None) -> Page:
    # Persistent Chromium contexts may close when their final page is closed,
    # so create the replacement before disposing of the failed page.
    fresh = context.new_page()
    fresh.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    if page is not None:
        with contextlib.suppress(Exception):
            if not page.is_closed():
                page.close()
    log("Opened fresh browser page after crash/unusable state")
    return fresh


def process_project(
    page: Page,
    project: str,
    *,
    project_url: str | None = None,
    select_files: bool = False,
    download_files: bool = False,
    non_interactive: bool = True,
    reuse_current_page: bool = True,
) -> dict[str, object]:
    """Run one BuildingConnected project and return orchestrator-ready data."""
    result = base_result(project)

    try:
        if project_url:
            log(f"Opening supplied project URL: {project_url}")
            page.goto(project_url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(3_000)
            candidate = Candidate(project, "", "", "URL", 2.0, project_url)
        else:
            existing_candidate = current_page_has_project(page, project) if reuse_current_page else None
            if existing_candidate:
                candidate = existing_candidate
            else:
                ensure_pipeline(page, non_interactive=non_interactive)
                sort_by_name(page)
                candidate = find_and_open_project(page, project)

        if not candidate and not non_interactive:
            pause_for_user("Project was not found automatically.")
            candidate = find_and_open_project(page, project)

        result["url"] = page.url
        if not candidate:
            return result

        result["status"] = "FOUND"
        result["matched_text"] = candidate.combined[:220]

        if not is_project_detail_view(page):
            result["status"] = "ERROR"
            result["error"] = "Project matched, but the detail page did not open."
            return result

        details = extract_details(page)
        result["location"] = details.get("Location", "") or "--"
        result["project_size"] = details.get("Project Size", "") or "--"
        result["due_date"] = details.get("Due Date", "") or "--"

        if not open_files_tab(page):
            result["error"] = "Files tab was not found/opened."
            return result

        if select_files or download_files:
            selected = select_allowed_files(page)
            result["files"] = selected
            if download_files:
                if selected:
                    result["download_path"] = str(
                        download_selected_files(page, len(selected))
                    )
                else:
                    log("Download skipped: no files were selected.")
        else:
            result["classification"] = [
                {"kind": kind, "action": action, "name": name, "reason": reason}
                for kind, action, name, reason in classified_file_items(page)
            ]

        return result
    except BuildingConnectedLoginRequired as exc:
        result["status"] = "ERROR"
        result["url"] = page.url
        result["error"] = str(exc)
        result["login_required"] = True
        return result
    except Exception as exc:
        result["status"] = "ERROR"
        result["url"] = page.url
        result["error"] = str(exc)
        return result


def print_result(result: dict[str, object]) -> None:
    print("\n" + "=" * 70)
    print(f"Status: {result.get('status', '')}")
    print(f"Project: {result.get('project', '')}")
    if result.get("url"):
        print(f"URL: {result['url']}")
    if result.get("matched_text"):
        print(f"Matched DOM Text: {str(result['matched_text'])[:220]}")
    if result.get("status") == "FOUND":
        print(f"Location: {result.get('location') or '--'}")
        print(f"Project Size: {result.get('project_size') or '--'}")
        print(f"Due Date: {result.get('due_date') or '--'}")
        files = result.get("files") or []
        if files:
            print("\nSelected Files Summary:")
            for file_name in files:
                print(f"  - {file_name}")
        if result.get("download_path"):
            print(f"\nDownloaded File: {result['download_path']}")
    if result.get("classification"):
        print("\nFile Classification Preview:")
        for row in result["classification"]:
            print(f"  {row['action']:6} {row['kind']:6} {row['name']} ({row['reason']})")
    if result.get("error"):
        print(f"Error: {result['error']}")
    print("=" * 70)


def main() -> int:
    global LOG_ENABLED

    parser = argparse.ArgumentParser(description="BuildingConnected Playwright POC")
    parser.add_argument("project", nargs="?", default=DEFAULT_PROJECT)
    parser.add_argument("--browser", choices=["auto", "chrome", "msedge", "chromium"], default="auto")
    parser.add_argument("--cdp-url", help="Attach to an already-open Chrome/Edge debugging session.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--project-url", help="Open a known BuildingConnected project URL and skip bid-board search.")
    parser.add_argument("--select-files", action="store_true", help="Select eligible files but do not download.")
    parser.add_argument("--download-files", action="store_true", help="Select eligible files and download them to ~/Downloads.")
    parser.add_argument(
        "--diagnose-files-dom",
        action="store_true",
        help="Print read-only Files-tab DOM diagnostics after opening the project.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON result data.")
    parser.add_argument("--output-file", help="Write structured JSON result data to this path.")
    parser.add_argument("--non-interactive", action="store_true", help="Return errors instead of pausing for manual browser fixes.")
    args = parser.parse_args()
    if args.diagnose_files_dom and args.json:
        parser.error("--diagnose-files-dom cannot be combined with --json.")
    LOG_ENABLED = not args.json

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = None
        if args.cdp_url:
            browser, context = attach_context(pw, args.cdp_url)
        else:
            context = launch_context(pw, args.browser, args.headless)

        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page = context.pages[0] if context.pages else context.new_page()

        if args.json:
            with contextlib.redirect_stdout(io.StringIO()):
                result = process_project(
                    page,
                    args.project,
                    project_url=args.project_url,
                    select_files=args.select_files,
                    download_files=args.download_files,
                    non_interactive=args.non_interactive,
                )
        else:
            result = process_project(
                page,
                args.project,
                project_url=args.project_url,
                select_files=args.select_files,
                download_files=args.download_files,
                non_interactive=args.non_interactive,
            )

        if args.diagnose_files_dom:
            print_files_dom_diagnostics(page)

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print_result(result)

        if args.output_file:
            with open(args.output_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)

        if not args.cdp_url and not args.non_interactive:
            input("\nPress Enter to close the browser...")

        if not args.cdp_url:
            context.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
