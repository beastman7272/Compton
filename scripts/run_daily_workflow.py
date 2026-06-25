#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Ensure local package imports work when running this file directly from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import config


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(slots=True)
class WorkflowStep:
    name: str
    command: list[str]
    needs_xvfb: bool = False


class WorkflowLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.log_path.open("a", encoding="utf-8")

    def close(self) -> None:
        self._file.close()

    def write(self, message: str = "") -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        print(line, flush=True)
        self._file.write(line + "\n")
        self._file.flush()

    def write_raw(self, message: str) -> None:
        print(message, end="", flush=True)
        self._file.write(message)
        self._file.flush()


def configure_text_io() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def should_use_xvfb() -> bool:
    if config.env_bool("CQE_PLAYWRIGHT_HEADLESS", False):
        return False
    return config.env_bool("CQE_USE_XVFB", config.HOSTED_RUNTIME)


def build_steps(args: argparse.Namespace) -> list[WorkflowStep]:
    python = sys.executable

    run_day_args = ["--run-day", args.run_day] if args.run_day else []
    import_args = []
    if args.run_day:
        import_args.extend(["--run-day", args.run_day])
    if args.dry_run:
        import_args.append("--dry-run")
    if args.no_sheet_update:
        import_args.append("--no-sheet-update")

    return [
        WorkflowStep(
            name="BuildingConnected login check",
            command=[python, "-u", "bid_board_orchestrator.py", "--check-buildingconnected-login"],
            needs_xvfb=True,
        ),
        WorkflowStep(
            name="ConstructConnect email processor",
            command=[python, "-u", "construct_connect_processor.py", *run_day_args],
        ),
        WorkflowStep(
            name="ConstructConnect Playwright workflow",
            command=[python, "-u", "construct_connect_playwright.py", "--non-interactive", *run_day_args],
            needs_xvfb=True,
        ),
        WorkflowStep(
            name="Stage 1 email processor",
            command=[python, "-u", "stage1_email_processor.py"],
        ),
        WorkflowStep(
            name="BuildingConnected workflow",
            command=[python, "-u", "bid_board_orchestrator.py", "--run-playwright-workflow"],
            needs_xvfb=True,
        ),
        WorkflowStep(
            name="CQE import workflow",
            command=[python, "-u", "scripts/run_import.py", *import_args],
        ),
    ]


def command_for_step(step: WorkflowStep) -> list[str]:
    if not step.needs_xvfb or not should_use_xvfb():
        return step.command

    xvfb_run = shutil.which("xvfb-run")
    if not xvfb_run:
        return step.command

    return [xvfb_run, "-a", *step.command]


def run_step(step: WorkflowStep, logger: WorkflowLogger) -> int:
    command = command_for_step(step)
    logger.write("")
    logger.write(f"START {step.name}")
    logger.write(f"COMMAND {' '.join(command)}")

    completed = subprocess.Popen(
        command,
        cwd=config.PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert completed.stdout is not None
    for line in completed.stdout:
        logger.write_raw(line)

    return_code = completed.wait()
    if return_code == 0:
        logger.write(f"DONE {step.name}")
    else:
        logger.write(f"FAILED {step.name} exit_code={return_code}")
    return return_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the daily BuildingConnected/ConstructConnect/CQE workflow."
    )
    parser.add_argument("--run-day", choices=WEEKDAYS, help="Override the weekday schedule.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass --dry-run to the CQE import step.",
    )
    parser.add_argument(
        "--no-sheet-update",
        action="store_true",
        help="Pass --no-sheet-update to the CQE import step.",
    )
    return parser.parse_args()


def main() -> int:
    configure_text_io()
    config.ensure_runtime_dirs()
    args = parse_args()

    log_path = config.LOG_ROOT / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    logger = WorkflowLogger(log_path)
    failures: list[tuple[str, int]] = []

    try:
        logger.write(f"Daily workflow log: {log_path}")
        for step in build_steps(args):
            return_code = run_step(step, logger)
            if return_code != 0:
                failures.append((step.name, return_code))

        if failures:
            logger.write("")
            logger.write("Workflow completed with failures:")
            for name, return_code in failures:
                logger.write(f"- {name}: exit_code={return_code}")
            return 1

        logger.write("")
        logger.write("Workflow completed successfully.")
        return 0
    finally:
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
