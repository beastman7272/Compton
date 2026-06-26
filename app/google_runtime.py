from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Iterable, Sequence

from google.auth.credentials import Credentials as BaseCredentials
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials

from app import config


def _clean_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_path(name: str) -> Path | None:
    value = _clean_env(name)
    if not value:
        return None

    path = Path(value)
    if path.is_absolute():
        return path
    return config.PROJECT_ROOT / path


def _first_clean_env(*names: str) -> str | None:
    for name in names:
        value = _clean_env(name)
        if value:
            return value
    return None


def _materialize_json(
    *,
    json_envs: Sequence[str],
    b64_envs: Sequence[str],
    filename: str,
) -> Path | None:
    raw_json = _first_clean_env(*json_envs)
    raw_b64 = _first_clean_env(*b64_envs)

    if not raw_json and not raw_b64:
        return None

    if raw_b64:
        content = base64.b64decode(raw_b64).decode("utf-8")
    else:
        content = raw_json or ""

    json.loads(content)

    config.RUNTIME_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    target = config.RUNTIME_CREDENTIALS_DIR / filename
    if not target.exists() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
    return target


def _first_env_path(*names: str) -> Path | None:
    for name in names:
        path = _env_path(name)
        if path:
            return path
    return None


def resolve_google_oauth_credentials_file(default_filename: str = "credentials.json") -> Path:
    explicit_path = _first_env_path("GOOGLE_OAUTH_CREDENTIALS_FILE", "GOOGLE_CREDENTIALS_FILE")
    if explicit_path:
        return explicit_path

    materialized = _materialize_json(
        json_envs=("GOOGLE_OAUTH_CREDENTIALS_JSON", "GOOGLE_CREDENTIALS_JSON"),
        b64_envs=("GOOGLE_OAUTH_CREDENTIALS_JSON_B64", "GOOGLE_CREDENTIALS_JSON_B64"),
        filename=default_filename,
    )
    if materialized:
        return materialized

    if config.HOSTED_RUNTIME:
        config.RUNTIME_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        return config.RUNTIME_CREDENTIALS_DIR / default_filename
    return config.PROJECT_ROOT / default_filename


def resolve_google_oauth_token_file(default_filename: str, *, use_file_env: bool = True) -> Path:
    explicit_path = (
        _first_env_path("GOOGLE_OAUTH_TOKEN_FILE", "GOOGLE_TOKEN_FILE")
        if use_file_env
        else None
    )
    if explicit_path:
        return explicit_path

    materialized = _materialize_json(
        json_envs=("GOOGLE_OAUTH_TOKEN_JSON", "GOOGLE_TOKEN_JSON"),
        b64_envs=("GOOGLE_OAUTH_TOKEN_JSON_B64", "GOOGLE_TOKEN_JSON_B64"),
        filename=default_filename,
    )
    if materialized:
        return materialized

    if config.HOSTED_RUNTIME:
        config.RUNTIME_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        return config.RUNTIME_CREDENTIALS_DIR / default_filename
    return config.PROJECT_ROOT / default_filename


def resolve_google_service_account_file(default_filename: str = "service-account.json") -> Path | None:
    explicit_path = _first_env_path(
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GOOGLE_SA_FILE",
    )
    if explicit_path:
        return explicit_path

    return _materialize_json(
        json_envs=("GOOGLE_SERVICE_ACCOUNT_JSON",),
        b64_envs=("GOOGLE_SERVICE_ACCOUNT_JSON_B64",),
        filename=default_filename,
    )


# Backward-compatible aliases. These names now mean OAuth client credentials/token.
def resolve_google_credentials_file(default_filename: str = "credentials.json") -> Path:
    return resolve_google_oauth_credentials_file(default_filename)


def resolve_google_token_file(default_filename: str, *, use_file_env: bool = True) -> Path:
    return resolve_google_oauth_token_file(default_filename, use_file_env=use_file_env)


def _scopes_set(scopes: Iterable[str]) -> set[str]:
    return {scope.strip() for scope in scopes if scope and scope.strip()}


def _token_has_scopes(creds: Credentials, scopes: Sequence[str]) -> bool:
    required = _scopes_set(scopes)
    granted = _scopes_set(creds.scopes or [])
    if not granted:
        granted = _scopes_set(creds.granted_scopes or [])
    return required.issubset(granted)


def _token_file_has_scopes(token_file: Path, scopes: Sequence[str]) -> bool:
    data = json.loads(token_file.read_text(encoding="utf-8"))
    raw_scopes = data.get("scopes") or data.get("granted_scopes")
    if isinstance(raw_scopes, str):
        granted = _scopes_set(raw_scopes.split())
    elif raw_scopes:
        granted = _scopes_set(raw_scopes)
    else:
        granted = set()
    return _scopes_set(scopes).issubset(granted)


def load_oauth_credentials(
    scopes: Sequence[str],
    *,
    token_filename: str,
    credentials_filename: str = "credentials.json",
    use_file_env: bool = True,
    allow_interactive: bool | None = None,
) -> Credentials:
    """
    Load OAuth user credentials for Gmail-capable workflows.

    Railway cannot complete an interactive OAuth browser flow, so hosted runs
    must provide GOOGLE_OAUTH_TOKEN_JSON_B64 or the legacy GOOGLE_TOKEN_JSON_B64.
    Local runs keep the existing credentials.json/token-file behavior.
    """
    token_file = resolve_google_oauth_token_file(token_filename, use_file_env=use_file_env)
    credentials_file = resolve_google_oauth_credentials_file(credentials_filename)
    allow_interactive = (not config.HOSTED_RUNTIME) if allow_interactive is None else allow_interactive

    creds: Credentials | None = None
    if token_file.exists():
        if not _token_file_has_scopes(token_file, scopes):
            if config.HOSTED_RUNTIME:
                raise RuntimeError(
                    f"{token_file.name} does not include all required Google OAuth scopes: "
                    f"{', '.join(scopes)}"
                )
            token_file.unlink(missing_ok=True)
            creds = None
        else:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)

    if creds:
        if not _token_has_scopes(creds, scopes):
            if config.HOSTED_RUNTIME:
                raise RuntimeError(
                    f"{token_file.name} does not include all required Google OAuth scopes: "
                    f"{', '.join(scopes)}"
                )
            token_file.unlink(missing_ok=True)
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            if config.HOSTED_RUNTIME:
                raise
            token_file.unlink(missing_ok=True)
            creds = None

    if not creds or not creds.valid:
        if not allow_interactive:
            raise RuntimeError(
                "Google OAuth token is missing or invalid. Generate it locally, then set "
                "GOOGLE_OAUTH_TOKEN_JSON_B64 in Railway."
            )
        if not credentials_file.exists():
            raise FileNotFoundError(f"Missing OAuth credentials file: {credentials_file}")
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes)
        creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return creds


def load_service_account_credentials(
    scopes: Sequence[str],
    *,
    default_filename: str = "service-account.json",
) -> service_account.Credentials | None:
    sa_file = resolve_google_service_account_file(default_filename)
    if not sa_file or not sa_file.exists():
        return None
    return service_account.Credentials.from_service_account_file(
        str(sa_file),
        scopes=scopes,
    )


def load_sheets_credentials(
    scopes: Sequence[str],
    *,
    token_filename: str = "orchestrator_token.json",
    prefer_service_account: bool = True,
) -> BaseCredentials:
    if prefer_service_account:
        sa_creds = load_service_account_credentials(scopes)
        if sa_creds:
            return sa_creds
    return load_oauth_credentials(scopes, token_filename=token_filename)


def build_oauth_services(
    service_names: Sequence[str],
    scopes: Sequence[str],
    *,
    token_filename: str,
    credentials_filename: str = "credentials.json",
    use_file_env: bool = True,
):
    creds = load_oauth_credentials(
        scopes,
        token_filename=token_filename,
        credentials_filename=credentials_filename,
        use_file_env=use_file_env,
    )
    from googleapiclient.discovery import build

    return tuple(build(name, "v1" if name == "gmail" else "v4", credentials=creds) for name in service_names)


def build_sheets_service(
    scopes: Sequence[str],
    *,
    token_filename: str = "orchestrator_token.json",
    prefer_service_account: bool = True,
):
    creds = load_sheets_credentials(
        scopes,
        token_filename=token_filename,
        prefer_service_account=prefer_service_account,
    )
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=creds)
