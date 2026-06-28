# BuildingConnected Playwright — Railway Incident Handoff

**Read after** `docs/HANDOFF-PRODUCT.md`.  
**Active branch:** `railway-deployment-prep`  
**Service URL:** `https://buildingconnected-production.up.railway.app`

---

## Problem statement

BuildingConnected Playwright **fails on Railway under `xvfb-run`**, while **manual login via noVNC bootstrap** shows a valid session (Bid Board, Undecided tab, project list).

ConstructConnect Playwright on the same container works. The blocker is **BC pipeline navigation** in `buildingconnected_playwright.py` and `bid_board_orchestrator.py` Phase 2.

---

## Success criteria

1. Login check passes reliably:

```sh
xvfb-run -a python -u bid_board_orchestrator.py --check-buildingconnected-login
```

Expected: `✓ BuildingConnected session is active.`

2. Phase 2 completes pending tasks:

```sh
xvfb-run -a python -u bid_board_orchestrator.py --playwright-run \
  --playwright-select-files --playwright-download-files --apply-playwright-results
```

3. Downloads appear under `/app/data/downloads`

4. Email Queue rows **90–91** move from **Awaiting Comet** to final status (**Processed** or appropriate)

---

## What works (confirmed)

| Evidence | Detail |
|----------|--------|
| noVNC bootstrap | Bid Board loaded, **Undecided (144)**, projects visible |
| Pipeline URL | `https://app.buildingconnected.com/opportunities/pipeline` |
| BC profile | `/app/data/playwright_bc_profile/chromium` (matches bootstrap) |
| Login check | **Passed at least once** after clearing Singleton lock files |
| CC automation | Email processor + Playwright tested successfully |
| Stage 1 | `stage1_email_processor.py` ran successfully on Railway |
| Google OAuth | Regenerated; Gmail + Sheets verified |

---

## What fails

### Login check (current)

```text
[BC] Launching bundled chromium with profile /app/data/playwright_bc_profile/chromium
[BC] Opening BuildingConnected pipeline
[BC] Pipeline failure (pipeline_ready_timeout): url='https://app.buildingconnected.com/opportunities/pipeline' title=''

BuildingConnected login check failed: BuildingConnected pipeline did not finish loading.
```

### Login check (earlier variant)

```text
Pipeline failure (pipeline_ready_timeout): url='...pipeline' title='Bid Board'
Saved failure screenshot: /app/data/logs/daily-workflow/bc-playwright-failures/20260628-154324.png
```

### Phase 2 (when login check had passed)

```text
[1/2] Morningstar Hollywood, SC
  Error: Could not reach the BuildingConnected Undecided tab. (older)
  OR: BuildingConnected pipeline did not finish loading. (newer)

[2/2] Dunkin Donuts #835 New Construction (Modular) - Bristol, TN
  Error: Page.goto: Page crashed
```

After crashes, noVNC showed **“Restore pages? Chromium didn’t shut down correctly.”** with a **Restore** button.

---

## Pending queue state

Phase 1 already ran. Email Queue rows **90–91** are **Awaiting Comet**:

| Row | Project |
|-----|---------|
| 90 | Morningstar Hollywood, SC |
| 91 | Dunkin Donuts #835 New Construction (Modular) - Bristol, TN |

- `run_state.json` — **retained** (2 browser tasks)
- `playwright_results.json` — last run had ERRORs
- **Retry with `--playwright-run` only** (do not re-run Phase 1 unless intentionally re-queuing)

---

## Repro commands (Railway console)

```sh
cd /app

# Clear profile locks (run before every BC Playwright attempt)
rm -f /app/data/playwright_bc_profile/chromium/SingletonLock \
      /app/data/playwright_bc_profile/chromium/SingletonSocket \
      /app/data/playwright_bc_profile/chromium/SingletonCookie \
      /app/data/playwright_bc_profile/chromium/lockfile

# Login check
xvfb-run -a python -u bid_board_orchestrator.py --check-buildingconnected-login

# Phase 2 only
xvfb-run -a python -u bid_board_orchestrator.py --playwright-run \
  --playwright-select-files --playwright-download-files --apply-playwright-results
```

### Diagnostic script

```sh
xvfb-run -a python - <<'PY'
from playwright.sync_api import sync_playwright
from buildingconnected_playwright import launch_context, START_URL
import time
with sync_playwright() as pw:
    ctx = launch_context(pw, "chromium", False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(START_URL, wait_until="load", timeout=120000)
    for i in range(6):
        try:
            body = page.locator("body").inner_text(timeout=3000) or ""
        except Exception as e:
            body = f"<err:{e}>"
        print(f"t={i*5}s url={page.url!r} title={page.title()!r} body_len={len(body)}")
        time.sleep(5)
    ctx.close()
PY
```

### Resource checks

```sh
free -h
df -h /dev/shm
ls -lt /app/data/logs/daily-workflow/bc-playwright-failures/
```

### View failure screenshot locally

```sh
base64 -w0 /app/data/logs/daily-workflow/bc-playwright-failures/20260628-154324.png
```

Decode on Windows and open in an image viewer.

---

## Timeline of fixes attempted

| Commit / theme | Change | Outcome |
|----------------|--------|---------|
| Railway deployment prep | Volume, Docker, xvfb, workflow HTTP trigger | Deploy OK |
| Google OAuth on Railway | `GOOGLE_OAUTH_TOKEN_JSON_B64` | Gmail steps work |
| BC noVNC bootstrap | Seed profile at `.../chromium` | Manual login works |
| `CQE_PLAYWRIGHT_BROWSER=chromium` | Hosted browser default | Fixes “Chrome not found” |
| `72ecd19` | Add missing `sheet_range()` | Phase 1 runs |
| `9d5cc35` | Pipeline hardening: domcontentloaded, Undecided strategies, 45s wait | Still flaky |
| `cecd53c` | Docker Chromium args, dismiss restore UI, failure screenshots | Crashes reduced; ready check still fails |
| `29a233d` | `pipeline_is_ready()` polling + URL/title/body fallbacks | Title sometimes `Bid Board`, sometimes `''`; still times out |

---

## Leading hypothesis

**Not an auth problem.** Session is valid in the persistent profile (proven by noVNC).

**Likely causes:**

1. **Chromium under `xvfb-run` in Docker** — goto reaches pipeline URL but SPA never renders reliably (`title=''`, no detectable Undecided/Bid Board text)
2. **Page crashes** — unclean shutdown → “Restore pages?” infobar; may block automation
3. **Resource contention** — Gunicorn + Playwright share container RAM; `/dev/shm` limits
4. **Selector / a11y mismatch** — UI visible in noVNC but not as Playwright-visible elements under xvfb

---

## Key code to read first

1. **`buildingconnected_playwright.py`**
   - `launch_context()` — Docker args, viewport 1440×1000
   - `navigate_to_pipeline()` — goto, `dismiss_browser_recovery_ui()`, `wait_for_pipeline_ready()`
   - `pipeline_is_ready()` / `wait_for_pipeline_ready()` — **current failure point**
   - `ensure_pipeline()` / `click_undecided_tab()` / `is_on_undecided_view()`

2. **`bid_board_orchestrator.py`**
   - `check_buildingconnected_login()`
   - `phase2_playwright_run()` — fresh page after any ERROR

3. **`scripts/run_bc_login_bootstrap.sh`** — profile path must stay aligned

---

## Do NOT retry (already ruled out)

- Installing Google Chrome in Docker
- `--playwright-browser chrome` on Railway
- Re-bootstrap auth unless noVNC shows a login screen
- Fixed `CONSTRUCTCONNECT_TAB_NAME` env var
- Changing ConstructConnect Playwright (it works)
- Expecting `--dry-run` on workflow steps 1–5
- Using `playwright_bc_profile/chrome` on Railway (bootstrap uses `chromium`)

---

## Suggested investigation directions

1. Compare **noVNC (headed)** vs **`xvfb-run` (automation)** — same profile; log body length, screenshot, console errors after goto
2. **Navigation waits** — `load` / `networkidle`, retry goto, longer hosted timeout (90s+)
3. **Ready fallback** — if URL is pipeline and not login page, keep polling for body content before failing
4. **Resources** — check RAM and `/dev/shm`; test with more Railway memory or Gunicorn idle
5. **Profile hygiene** — clear crash-recovery session state on hosted launch
6. **Per-task browser context** on hosted if crashes cascade
7. **Structural fallback** — separate Playwright worker service (last resort)

---

## Bootstrap reference (re-auth only if session dead)

| Step | Command |
|------|---------|
| Bootstrap | `bash scripts/run_bc_login_bootstrap.sh` |
| Restore prod | `sh -c 'xvfb-run -a gunicorn -w 1 -b 0.0.0.0:${PORT:-8080} app_web:app'` |

Docs: `docs/railway-buildingconnected-login-bootstrap.md`

In noVNC: click **Restore** on crash dialog if present, confirm Undecided tab, then restore Gunicorn start command.

---

## Artifacts to collect

- Latest PNG in `bc-playwright-failures/`
- Diagnostic script output (url, title, body_len over 30s)
- `free -h` / `df -h /dev/shm`
- Full stdout from login check and `--playwright-run`
- `/app/data/run_state.json` (2 pending tasks)

---

## Suggested Codex prompts

### Audit (no code changes)

> Read `docs/HANDOFF-PRODUCT.md` and the files listed in “Key code to read first.” Do not edit code. Produce: (1) how BC pipeline navigation is supposed to work, (2) ranked root-cause hypotheses for `pipeline_ready_timeout`, (3) assessment of prior fixes, (4) one minimal next experiment, (5) what not to retry.

### Fix (after audit)

> Implement the agreed minimal fix on `railway-deployment-prep`. Verify with compile. User will run login check then `--playwright-run` on Railway console.