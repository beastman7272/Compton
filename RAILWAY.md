# Railway Deployment

This app is prepared to run as one Railway web service that owns a persistent
volume. The initial deployment keeps SQLite and file uploads on the volume.

## Required Railway Setup

Mount a Railway Volume at:

```text
/app/data
```

Set this env var if Railway does not inject it automatically:

```text
RAILWAY_VOLUME_MOUNT_PATH=/app/data
```

The Dockerfile starts the web service with:

```sh
xvfb-run -a gunicorn -w 1 -b 0.0.0.0:${PORT:-8080} app_web:app
```

Use one worker for SQLite. The `xvfb-run` wrapper lets daily Playwright
subprocesses run headed inside the container.

## Daily Workflow Trigger

The web service exposes:

```text
POST /internal/daily-workflow/run
```

Set a secret:

```text
DAILY_WORKFLOW_TOKEN=...
```

Trigger with either header:

```sh
curl -X POST "https://your-service.railway.app/internal/daily-workflow/run" \
  -H "Authorization: Bearer $DAILY_WORKFLOW_TOKEN"
```

The endpoint returns quickly, starts `scripts/run_daily_workflow.py` in the
web container, and prevents overlap with a lock file under the data root.

Schedule the trigger for 10 AM America/New_York using Railway's scheduler or an
external cron service that sends the POST request above.

Optional trigger parameters can be sent as query params or JSON:

```text
run_day=monday
dry_run=true
no_sheet_update=true
```

## Runtime Paths

Local defaults still use the existing project files and folders. When
`CQE_DATA_ROOT` or `RAILWAY_VOLUME_MOUNT_PATH` is present, runtime data moves
under the data root.

Supported env vars:

```text
CQE_DATA_ROOT
CQE_DB_PATH
CQE_STORAGE_ROOT
CQE_DOWNLOADS_DIR
CQE_LOG_ROOT
CQE_RUNTIME_CREDENTIALS_DIR
BC_PLAYWRIGHT_PROFILE_DIR
CC_PLAYWRIGHT_PROFILE_DIR
CQE_WORKFLOW_LOCK_FILE
CQE_PLAYWRIGHT_HEADLESS
CQE_USE_XVFB
DAILY_WORKFLOW_TOKEN
```

Default Railway paths with `/app/data`:

```text
/app/data/cqe.db
/app/data/uploads
/app/data/downloads
/app/data/logs/daily-workflow
/app/data/credentials
/app/data/playwright_bc_profile
/app/data/playwright_cc_profile
```

## Google Credentials

Existing local file behavior is preserved:

```text
GOOGLE_CREDENTIALS_FILE
GOOGLE_TOKEN_FILE
```

Railway can also provide JSON directly:

```text
GOOGLE_CREDENTIALS_JSON
GOOGLE_CREDENTIALS_JSON_B64
GOOGLE_TOKEN_JSON
GOOGLE_TOKEN_JSON_B64
```

These are materialized at runtime under the credentials directory and are not
committed. If the daily workflow needs multiple Google tokens with different
scopes, provide compatible token JSON or use `GOOGLE_TOKEN_FILE` pointed at a
volume-backed token prepared outside git.

## Local Commands

Run the web app locally:

```sh
python app_web.py
```

Run the cross-platform daily workflow:

```sh
python scripts/run_daily_workflow.py
```

Safe import-only behavior can be requested for the import step:

```sh
python scripts/run_daily_workflow.py --dry-run --no-sheet-update
```

Override the ConstructConnect schedule day:

```sh
python scripts/run_daily_workflow.py --run-day monday
```

## Manual Setup Notes

Do not commit or ship these local artifacts:

```text
playwright_bc_profile/
playwright_cc_profile/
credentials.json
token.json
stage1_token.json
orchestrator_token.json
data/
logs/
```

BuildingConnected may still require a pre-seeded authenticated browser profile
because Autodesk/MFA login is not automated. ConstructConnect can use
`CONSTRUCTCONNECT_USERNAME` and `CONSTRUCTCONNECT_PASSWORD`, but the persistent
profile should still live on the Railway volume.
