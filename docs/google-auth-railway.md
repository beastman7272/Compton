# Google Auth on Railway

Railway cannot complete an interactive Google OAuth browser flow. Gmail access
therefore requires a pre-generated OAuth user token supplied through the
environment.

## Gmail Workflows

Use OAuth user credentials for any script that calls Gmail:

- `stage1_email_processor.py`
- `construct_connect_processor.py`

Required Railway variable:

- `GOOGLE_OAUTH_TOKEN_JSON_B64`: base64-encoded OAuth authorized-user token JSON with the Gmail scopes required by the script.

Optional Railway variable:

- `GOOGLE_OAUTH_CREDENTIALS_JSON_B64`: base64-encoded OAuth client credentials JSON. This is useful for local token generation or for preserving the client metadata alongside the token, but Railway still cannot run the interactive flow.

Legacy aliases still supported:

- `GOOGLE_TOKEN_JSON_B64`: legacy alias for `GOOGLE_OAUTH_TOKEN_JSON_B64`.
- `GOOGLE_CREDENTIALS_JSON_B64`: legacy alias for `GOOGLE_OAUTH_CREDENTIALS_JSON_B64`.

## Sheets-Only Workflows

Sheets-only scripts prefer a Google service account when configured:

- `bid_board_orchestrator.py`
- `construct_connect_playwright.py`
- `scripts/run_import.py`

Preferred Railway variable:

- `GOOGLE_SERVICE_ACCOUNT_JSON_B64`: base64-encoded Google service account JSON. Share each target spreadsheet with the service account email.

Fallback:

- If no service account is configured, the Sheets-only scripts use OAuth via `GOOGLE_OAUTH_TOKEN_JSON_B64` or the legacy `GOOGLE_TOKEN_JSON_B64`.

Legacy local path still supported:

- `GOOGLE_SA_FILE`: local path to a service account JSON file.

## Safe Verification

This command checks credential JSON shape and OAuth token scopes without making
Gmail or Sheets API calls:

```powershell
python scripts/verify_google_auth.py --gmail-mode modify
python scripts/verify_google_auth.py --gmail-mode modify --token-filename stage1_token.json
```

Use `--gmail-mode readonly` when verifying a token for a Gmail-readonly workflow.
