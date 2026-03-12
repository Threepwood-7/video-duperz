from __future__ import annotations

import argparse
import sys
import types
from typing import TYPE_CHECKING

from video_duperz import __main__ as app_main
from video_duperz import cleaner
from video_duperz.config import app_data_dir

if TYPE_CHECKING:
    from pathlib import Path


def test_clean_parser_accepts_flags() -> None:
    parser = app_main._build_parser()
    args = parser.parse_args(
        ["clean", "--full-reset", "--delay-ms", "250", "--relaunch"]
    )
    assert args.command == "clean"
    assert args.full_reset is True
    assert args.delay_ms == 250
    assert args.relaunch is True


def test_parser_accepts_runtime_override_flags() -> None:
    parser = app_main._build_parser()
    args = parser.parse_args(
        ["--config-dir", "C:/cfg", "--data-dir", "C:/data", "clean", "--full-reset"]
    )
    assert args.config_dir == "C:/cfg"
    assert args.data_dir == "C:/data"
    assert args.command == "clean"
    assert args.full_reset is True


def test_cmd_clean_requires_guard(capsys) -> None:
    rc = app_main._cmd_clean(
        argparse.Namespace(full_reset=False, delay_ms=0, relaunch=False)
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "--full-reset is required" in captured.err


def test_run_full_reset_removes_entire_app_data_dir(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    root = app_data_dir()
    (root / "settings.json").write_text("{}", encoding="utf-8")
    (root / "app.db").write_bytes(b"x")
    thumbs = root / "thumbnails"
    thumbs.mkdir(parents=True, exist_ok=True)
    (thumbs / "a.jpg").write_bytes(b"x")

    rc = cleaner.run_full_reset(delay_ms=0, relaunch=False)
    assert rc == 0
    assert not root.exists()


def test_run_full_reset_relaunches_on_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    app_data_dir().mkdir(parents=True, exist_ok=True)
    called: list[list[str]] = []

    def _fake_popen(cmd, **_kwargs):
        called.append([str(part) for part in cmd])
        return object()

    monkeypatch.setattr("video_duperz.cleaner.subprocess.Popen", _fake_popen)
    rc = cleaner.run_full_reset(delay_ms=0, relaunch=True)
    assert rc == 0
    assert called
    assert called[0][:4] == [sys.executable, "-m", "video_duperz", "gui"]


def test_run_full_reset_failure_does_not_relaunch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    app_data_dir().mkdir(parents=True, exist_ok=True)
    called = {"popen": False}

    def _raise(*_args, **_kwargs):
        raise OSError("boom")

    def _fake_popen(*_args, **_kwargs):
        called["popen"] = True
        return object()

    monkeypatch.setattr("video_duperz.cleaner.shutil.rmtree", _raise)
    monkeypatch.setattr("video_duperz.cleaner.subprocess.Popen", _fake_popen)
    rc = cleaner.run_full_reset(delay_ms=0, relaunch=True)
    assert rc == 3
    assert called["popen"] is False


def test_cmd_gui_spawns_cleaner_when_full_reset_requested(monkeypatch) -> None:
    monkeypatch.setattr(app_main, "ensure_ffprobe_available", lambda: None)

    class _FakeDb:
        def close(self) -> None:
            return None

    monkeypatch.setattr(app_main, "Database", lambda: _FakeDb())
    monkeypatch.setattr(app_main, "load_settings", lambda: object())

    class _FakeApp:
        def __init__(self, _argv):
            pass

        def exec(self) -> int:
            return 0

    class _FakeMsgBox:
        @staticmethod
        def critical(*_args, **_kwargs):
            return None

    qtwidgets = types.SimpleNamespace(QApplication=_FakeApp, QMessageBox=_FakeMsgBox)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", qtwidgets)

    fake_main_window_module = types.ModuleType("video_duperz.ui.main_window")

    class _FakeMainWindow:
        def __init__(self, db, settings):
            self.db = db
            self.settings = settings

        def show(self) -> None:
            return None

        def consume_full_reset_requested(self) -> bool:
            return True

    fake_main_window_module.MainWindow = _FakeMainWindow  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "video_duperz.ui.main_window", fake_main_window_module
    )

    called: list[list[str]] = []

    def _fake_popen(cmd, **_kwargs):
        called.append([str(part) for part in cmd])
        return object()

    monkeypatch.setattr(app_main.subprocess, "Popen", _fake_popen)

    rc = app_main._cmd_gui(argparse.Namespace())
    assert rc == 0
    assert called
    assert called[0][:5] == [
        sys.executable,
        "-m",
        "video_duperz",
        "clean",
        "--full-reset",
    ]
    assert "--relaunch" in called[0]
