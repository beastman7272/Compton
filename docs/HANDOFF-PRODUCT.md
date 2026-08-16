# BuildingConnected / CQE — Product & Architecture Handoff

Stable reference for onboarding (Codex, Cursor, or future maintainers).  
Repo path: **`BuildingConnected/`** inside workspace **`Bid Site Applications`**.

---

## Purpose

Automate Compton Sales’ bid-board workflow:

1. Ingest bid-related emails from Gmail into Google Sheets
2. Download project documents from **BuildingConnected (BC)** and **ConstructConnect (CC)** via Playwright
3. Import downloaded files into the **CQE** app (SQLite + file storage) for review/processing

The Flask web app (`app_web.py`) is the production UI and workflow host on Railway.

---

## Daily workflow (6 steps)

Orchestrated by `scripts/run_daily_workflow.py`, triggered manually or via HTTP:

```http
POST /internal/daily-workflow/run
Authorization: Bearer $DAILY_WORKFLOW_TOKEN
```

Optional query/body params: `run_day`, `dry_run`, `no_sheet_update`.

| Step | Script | What it does |
|------|--------|--------------|
| 1 | `bid_board_orchestrator.py --check-buildingconnected-login` | Verify BC session in Playwright profile |
| 2 | `construct_connect_processor.py` | Gmail → CC Google Sheet (Excel attachments) |
| 3 | `construct_connect_playwright.py --non-interactive` | CC Playwright downloads by manufacturer tab |
| 4 | `stage1_email_processor.py` | BC-related Gmail → Email Queue sheet |
| 5 | `bid_board_orchestrator.py --run-playwright-workflow` | Phase 1 sheet logic + Phase 2 BC Playwright downloads |
| 6 | `scripts/run_import.py` | Import BC/CC downloads into CQE DB/storage |

**Important:** `--dry-run` and `--no-sheet-update` only affect step 6. Steps 1–5 always touch live Gmail, Sheets, and the browser.

### ConstructConnect manufacturer schedule

Driven by `--run-day` (weekday name), **not** a fixed Railway env var:

| Day | Manufacturer |
|-----|--------------|
| Monday | Citadel |
| Tuesday | Fortress |
| Wednesday | Fabral |
| Thursday | Metal-Era |
| Friday | none |
| Saturday | Roof Schedule |
| Sunday | Fortress Alt Words SE |

---

## Railway deployment

| Item | Value |
|------|-------|
| Project | Compton |
| Service | BuildingConnected |
| Branch | `railway-deployment-prep` |
| Public URL | `https://buildingconnected-production.up.railway.app` |
| Volume mount | `/app/data` |
| Start command | `sh -c 'xvfb-run -a gunicorn -w 1 -b 0.0.0.0:${PORT:-8080} app_web:app'` |

**Architecture decisions:**

- **SQLite** on Railway volume (not Postgres yet)
- **Single web service** owns the volume (volumes are not shared across services)
- **Playwright runs in the same container** as Gunicorn
- **Future cron:** POST daily workflow at **10 AM America/New_York**
- **No public UI auth** for now

See also: `RAILWAY.md`, `docs/google-auth-railway.md`, `docs/railway-buildingconnected-login-bootstrap.md`.

---

## Runtime paths (Railway defaults)

With `CQE_DATA_ROOT=/app/data` or `RAILWAY_VOLUME_MOUNT_PATH=/app/data`:

| Path | Purpose |
|------|---------|
| `/app/data/cqe.db` | SQLite database |
| `/app/data/uploads` | CQE file storage |
| `/app/data/downloads` | BC/CC Playwright downloads |
| `/app/data/logs/daily-workflow` | Workflow logs |
| `/app/data/logs/daily-workflow/bc-playwright-failures/` | BC failure screenshots |
| `/app/data/credentials/` | Materialized Google token/SA JSON |
| `/app/data/playwright_bc_profile/chromium` | BC browser profile |
| `/app/data/playwright_cc_profile` | CC browser profile |
| `/app/data/run_state.json` | Pending BC Playwright tasks (Phase 1 → 2) |
| `/app/data/playwright_results.json` | Last Phase 2 results |
| `/app/data/comet_prompt.txt` | Legacy Comet prompt output (Phase 1) |

Path resolution: `app/config.py`.

---

## Google authentication

| Use case | Auth |
|----------|------|
| Gmail (steps 2, 4) | OAuth user token → `GOOGLE_OAUTH_TOKEN_JSON_B64` |
| Sheets (most scripts) | Service account → `GOOGLE_SERVICE_ACCOUNT_JSON_B64` |
| Sheets fallback | OAuth token |

- Gmail OAuth account: **`billeastmanai@gmail.com`**
- Stage 1 filters unread mail from **`beastman7272@gmail.com`** (test forwarding; production senders may differ)
- CC processor searches unread manufacturer emails with Excel attachments (subject contains manufacturer name)

Token files materialized on volume under `/app/data/credentials/` (`token.json`, `stage1_token.json`, etc.).

---

## Playwright / browser conventions

| Environment | Browser | Profile path |
|-------------|---------|--------------|
| Local dev | Chrome (default) | `playwright_bc_profile/chrome` |
| Railway | Bundled Chromium only | `/app/data/playwright_bc_profile/chromium` |

- Dockerfile runs `playwright install chromium` (no Google Chrome)
- Env: `CQE_PLAYWRIGHT_BROWSER=chromium` on Railway
- Hosted default: `config.default_playwright_browser()` → chromium when `HOSTED_RUNTIME`
- Daily workflow passes `--playwright-browser` from config for BC steps

### BC login bootstrap (one-time / re-auth)

Temporary Railway start command:

```sh
bash scripts/run_bc_login_bootstrap.sh
```

Opens noVNC on the public URL for manual Autodesk login/MFA. Profile **must** be `/app/data/playwright_bc_profile/chromium`. Restore normal Gunicorn start command after login.

---

## Key environment variables (Railway)

Already configured (representative list):

```text
RAILWAY_VOLUME_MOUNT_PATH=/app/data
CQE_DATA_ROOT=/app/data
DAILY_WORKFLOW_TOKEN
OPENAI_API_KEY
CONSTRUCTCONNECT_USERNAME
CONSTRUCTCONNECT_PASSWORD
BID_BOARD_SHEET_ID
EMAILS_SHEET_ID
CONSTRUCTCONNECT_SHEET_ID
GOOGLE_SERVICE_ACCOUNT_JSON_B64
GOOGLE_OAUTH_TOKEN_JSON_B64
CQE_PLAYWRIGHT_BROWSER=chromium
```

Optional / cleanup: `BC_BOOTSTRAP_VNC_PASSWORD`, legacy `GOOGLE_TOKEN_JSON_B64`.

Other supported vars: see `RAILWAY.md` (`CQE_DB_PATH`, `CQE_DOWNLOADS_DIR`, `CQE_USE_XVFB`, etc.).

---

## Main code files

| File | Role |
|------|------|
| `app_web.py` | Flask app, daily workflow HTTP trigger |
| `app/config.py` | Hosted vs local paths, `default_playwright_browser()` |
| `app/google_runtime.py` | Google credential loading |
| `buildingconnected_playwright.py` | BC browser automation |
| `bid_board_orchestrator.py` | BC sheet queue (Phase 1), Playwright (Phase 2), finalize (Phase 3) |
| `construct_connect_playwright.py` | CC Playwright |
| `construct_connect_processor.py` | CC Gmail → Sheets |
| `stage1_email_processor.py` | BC Gmail → Email Queue |
| `scripts/run_daily_workflow.py` | Daily orchestration |
| `scripts/run_import.py` | CQE import |
| `scripts/run_bc_login_bootstrap.sh` | Temporary noVNC BC login |
| `Dockerfile` | Playwright base image, Chromium, xvfb/noVNC |

### BC orchestrator phases

| Phase | Entry | Purpose |
|-------|-------|---------|
| 1 | `python bid_board_orchestrator.py` | Process Email Queue, update statuses, write `run_state.json` |
| 2 | `--playwright-run` or part of `--run-playwright-workflow` | Playwright downloads from BC |
| 3 | `--finalize` | Apply Comet output (legacy); Phase 2 can apply via `--apply-playwright-results` |

---

## Verification status on Railway (high level)

| Component | Status |
|-----------|--------|
| Web app / Gunicorn | Working |
| Volume, Google auth | Working |
| CC email processor | Tested (Fabral / Saturday) |
| CC Playwright | Tested |
| Stage 1 email processor | Tested |
| BC login check | See incident doc |
| BC `--playwright-run` | See incident doc |
| End-to-end CQE import | Not fully validated |

---

## Current blocker (stub)

**Open issue:** BuildingConnected Playwright automation fails on Railway under `xvfb-run`.

- **Works:** noVNC bootstrap shows valid BC session (Bid Board + Undecided tab); ConstructConnect automation works
- **Fails:** `bid_board_orchestrator.py --check-buildingconnected-login` and `--playwright-run`
- **Branch:** `railway-deployment-prep`
- **Full details:** `docs/HANDOFF-INCIDENT.md`

Do not assume BC automation is production-ready until this is resolved.