from __future__ import annotations

import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from .config import app_data_dir

if TYPE_CHECKING:
    from pathlib import Path

FULL_RESET_DEFAULT_DELAY_MS = 1500


def run_full_reset(delay_ms: int = FULL_RESET_DEFAULT_DELAY_MS, relaunch: bool = False) -> int:
    try:
        delay = max(0, int(delay_ms))
    except (TypeError, ValueError):
        delay = FULL_RESET_DEFAULT_DELAY_MS
    time.sleep(delay / 1000.0)

    target: Path = app_data_dir()
    try:
        if target.exists():
            shutil.rmtree(target)
    except Exception as exc:
        print(f"ERROR: full reset cleanup failed: {exc}", file=sys.stderr)
        return 3

    if relaunch:
        try:
            subprocess.Popen([sys.executable, "-m", "video_duperz", "gui"])
        except Exception as exc:
            print(f"ERROR: full reset relaunch failed: {exc}", file=sys.stderr)
            return 3

    return 0
