#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.google_runtime import (  # noqa: E402
    load_service_account_credentials,
    resolve_google_oauth_token_file,
    resolve_google_service_account_file,
)


GMAIL_MODIFY_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]
GMAIL_READONLY_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def token_scopes(path: Path) -> set[str]:
    data = load_json(path)
    scopes = data.get("scopes") or data.get("granted_scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return {scope.strip() for scope in scopes if scope and scope.strip()}


def print_scope_check(label: str, available: set[str], required: list[str]) -> bool:
    missing = [scope for scope in required if scope not in available]
    if missing:
        print(f"{label}: missing required scopes")
        for scope in missing:
            print(f"  - {scope}")
        return False
    print(f"{label}: required scopes present")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify local/Railway Google credential configuration without making "
            "Gmail or Sheets API calls."
        )
    )
    parser.add_argument(
        "--gmail-mode",
        choices=["modify", "readonly"],
        default="modify",
        help="Scope set to verify for Gmail OAuth token use.",
    )
    parser.add_argument(
        "--token-filename",
        default="token.json",
        help="Local OAuth token filename to check when token env vars are not set.",
    )
    args = parser.parse_args()

    ok = True
    gmail_scopes = GMAIL_MODIFY_SCOPES if args.gmail_mode == "modify" else GMAIL_READONLY_SCOPES

    oauth_token = resolve_google_oauth_token_file(args.token_filename, use_file_env=True)
    if oauth_token.exists():
        available = token_scopes(oauth_token)
        ok = print_scope_check("OAuth token", available, gmail_scopes) and ok
    else:
        print("OAuth token: not found")
        ok = False

    service_account_file = resolve_google_service_account_file()
    if service_account_file and service_account_file.exists():
        try:
            load_service_account_credentials(SHEETS_SCOPES)
            print("Service account: valid JSON and usable for Sheets credentials")
        except Exception as exc:
            print(f"Service account: invalid ({exc})")
            ok = False
    else:
        print("Service account: not configured; Sheets-only scripts will use OAuth fallback")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
