from __future__ import annotations

import base64
import os
from pathlib import Path

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


def _materialize_json(
    *,
    json_env: str,
    b64_env: str,
    filename: str,
) -> Path | None:
    raw_json = _clean_env(json_env)
    raw_b64 = _clean_env(b64_env)

    if not raw_json and not raw_b64:
        return None

    if raw_b64:
        content = base64.b64decode(raw_b64).decode("utf-8")
    else:
        content = raw_json or ""

    config.RUNTIME_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    target = config.RUNTIME_CREDENTIALS_DIR / filename
    if not target.exists() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
    return target


def resolve_google_credentials_file(default_filename: str = "credentials.json") -> Path:
    explicit_path = _env_path("GOOGLE_CREDENTIALS_FILE")
    if explicit_path:
        return explicit_path

    materialized = _materialize_json(
        json_env="GOOGLE_CREDENTIALS_JSON",
        b64_env="GOOGLE_CREDENTIALS_JSON_B64",
        filename=default_filename,
    )
    if materialized:
        return materialized

    if config.HOSTED_RUNTIME:
        config.RUNTIME_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        return config.RUNTIME_CREDENTIALS_DIR / default_filename
    return config.PROJECT_ROOT / default_filename


def resolve_google_token_file(default_filename: str, *, use_file_env: bool = True) -> Path:
    explicit_path = _env_path("GOOGLE_TOKEN_FILE") if use_file_env else None
    if explicit_path:
        return explicit_path

    materialized = _materialize_json(
        json_env="GOOGLE_TOKEN_JSON",
        b64_env="GOOGLE_TOKEN_JSON_B64",
        filename=default_filename,
    )
    if materialized:
        return materialized

    if config.HOSTED_RUNTIME:
        config.RUNTIME_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        return config.RUNTIME_CREDENTIALS_DIR / default_filename
    return config.PROJECT_ROOT / default_filename
