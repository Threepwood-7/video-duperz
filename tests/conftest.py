from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(qtbot, tmp_path: Path):
    from video_duperz.config import default_settings
    from video_duperz.db import Database
    from video_duperz.ui.main_window import MainWindow

    db = Database(tmp_path / "app.db")
    settings = default_settings()
    settings.scan_roots = [str(tmp_path)]
    win = MainWindow(db=db, settings=settings)
    qtbot.addWidget(win)
    win.show()
    yield win
    win.close()
    db.close()
