from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is a runtime dependency.
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if load_dotenv:
    load_dotenv(PROJECT_ROOT / ".env")


def _clean_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _path_from_env(name: str, default: Path, *, base: Path = PROJECT_ROOT) -> Path:
    value = _clean_env(name)
    if not value:
        return default

    path = Path(value)
    if path.is_absolute():
        return path

    return base / path


def env_bool(name: str, default: bool = False) -> bool:
    value = _clean_env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def is_hosted_runtime() -> bool:
    return bool(_clean_env("CQE_DATA_ROOT") or _clean_env("RAILWAY_VOLUME_MOUNT_PATH"))


HOSTED_RUNTIME = is_hosted_runtime()
DATA_ROOT = _path_from_env(
    "CQE_DATA_ROOT",
    _path_from_env("RAILWAY_VOLUME_MOUNT_PATH", PROJECT_ROOT / "data"),
)

DB_PATH = _path_from_env("CQE_DB_PATH", DATA_ROOT / "cqe.db")
STORAGE_ROOT = _path_from_env("CQE_STORAGE_ROOT", DATA_ROOT / "uploads")
DOWNLOADS_DIR = _path_from_env(
    "CQE_DOWNLOADS_DIR",
    DATA_ROOT / "downloads" if HOSTED_RUNTIME else Path.home() / "Downloads",
)
LOG_ROOT = _path_from_env(
    "CQE_LOG_ROOT",
    DATA_ROOT / "logs" / "daily-workflow"
    if HOSTED_RUNTIME
    else PROJECT_ROOT / "logs" / "daily-workflow",
)
RUNTIME_CREDENTIALS_DIR = _path_from_env(
    "CQE_RUNTIME_CREDENTIALS_DIR",
    DATA_ROOT / "credentials" if HOSTED_RUNTIME else PROJECT_ROOT,
)
BC_PLAYWRIGHT_PROFILE_DIR = _path_from_env(
    "BC_PLAYWRIGHT_PROFILE_DIR",
    DATA_ROOT / "playwright_bc_profile"
    if HOSTED_RUNTIME
    else PROJECT_ROOT / "playwright_bc_profile",
)
CC_PLAYWRIGHT_PROFILE_DIR = _path_from_env(
    "CC_PLAYWRIGHT_PROFILE_DIR",
    DATA_ROOT / "playwright_cc_profile"
    if HOSTED_RUNTIME
    else PROJECT_ROOT / "playwright_cc_profile",
)
WORKFLOW_LOCK_FILE = _path_from_env(
    "CQE_WORKFLOW_LOCK_FILE",
    DATA_ROOT / "daily-workflow.lock" if HOSTED_RUNTIME else PROJECT_ROOT / "daily-workflow.lock",
)
COMET_PROMPT_FILE = _path_from_env(
    "CQE_COMET_PROMPT_FILE",
    DATA_ROOT / "comet_prompt.txt" if HOSTED_RUNTIME else PROJECT_ROOT / "comet_prompt.txt",
)
RUN_STATE_FILE = _path_from_env(
    "CQE_RUN_STATE_FILE",
    DATA_ROOT / "run_state.json" if HOSTED_RUNTIME else PROJECT_ROOT / "run_state.json",
)
PLAYWRIGHT_RESULTS_FILE = _path_from_env(
    "CQE_PLAYWRIGHT_RESULTS_FILE",
    DATA_ROOT / "playwright_results.json" if HOSTED_RUNTIME else PROJECT_ROOT / "playwright_results.json",
)

_PLAYWRIGHT_BROWSER_CHOICES = frozenset({"auto", "chrome", "msedge", "chromium"})


def default_playwright_browser() -> str:
    """
    Default BuildingConnected Playwright browser channel.

    Railway/hosted images install bundled Chromium only; local dev typically uses
    installed Chrome unless overridden.
    """
    value = _clean_env("CQE_PLAYWRIGHT_BROWSER")
    if value:
        normalized = value.lower()
        if normalized not in _PLAYWRIGHT_BROWSER_CHOICES:
            allowed = ", ".join(sorted(_PLAYWRIGHT_BROWSER_CHOICES))
            raise ValueError(f"CQE_PLAYWRIGHT_BROWSER must be one of: {allowed}")
        return normalized
    if HOSTED_RUNTIME:
        return "chromium"
    return "chrome"


def resolve_project_path(path: Path | str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def ensure_runtime_dirs() -> None:
    for folder in (
        DB_PATH.parent,
        STORAGE_ROOT,
        DOWNLOADS_DIR,
        LOG_ROOT,
        RUNTIME_CREDENTIALS_DIR,
        BC_PLAYWRIGHT_PROFILE_DIR,
        CC_PLAYWRIGHT_PROFILE_DIR,
        WORKFLOW_LOCK_FILE.parent,
    ):
        folder.mkdir(parents=True, exist_ok=True)
