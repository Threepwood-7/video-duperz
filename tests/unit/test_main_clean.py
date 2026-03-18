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


def test_benchmark_eval_parser_accepts_internal_flags() -> None:
    parser = app_main._build_parser()
    args = parser.parse_args(
        [
            "benchmark-eval",
            "--roots",
            "R:/",
            "S:/",
            "--sample-set",
            "mixed_240",
            "--dispatch-strategy",
            "micro_batch",
            "--workers",
            "3",
        ]
    )
    assert args.command == "benchmark-eval"
    assert args.roots == ["R:/", "S:/"]
    assert args.sample_set == "mixed_240"
    assert args.dispatch_strategy == "micro_batch"
    assert args.workers == 3


def test_cmd_clean_requires_guard(capsys) -> None:
    rc = app_main._cmd_clean(
        argparse.Namespace(full_reset=False, delay_ms=0, relaunch=False)
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "--full-reset is required" in captured.err


def test_cmd_benchmark_eval_prints_json_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        app_main,
        "load_settings",
        lambda: types.SimpleNamespace(
            max_workers=2,
            probe_backend="pyav",
            drive_worker_overrides={},
        ),
    )
    monkeypatch.setattr(
        app_main,
        "run_benchmark_evaluation",
        lambda roots, **kwargs: types.SimpleNamespace(
            to_json=lambda: '{"ok": true}',
            roots=roots,
            kwargs=kwargs,
        ),
    )

    rc = app_main._cmd_benchmark_eval(
        argparse.Namespace(
            roots=["R:/", "S:/"],
            sample_set="mixed_240",
            dispatch_strategy="ordered",
            workers=None,
        )
    )

    assert rc == 0
    assert capsys.readouterr().out.strip() == '{"ok": true}'


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
    monkeypatch.setattr(
        app_main, "ensure_probe_backend_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        app_main, "ensure_fingerprint_fallback_chain_available", lambda: None
    )

    class _FakeDb:
        def close(self) -> None:
            return None

    monkeypatch.setattr(app_main, "Database", lambda: _FakeDb())
    monkeypatch.setattr(
        app_main,
        "load_settings",
        lambda: types.SimpleNamespace(probe_backend="ffprobe"),
    )

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


def test_cmd_gui_reports_fingerprint_backend_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        app_main, "load_settings", lambda: types.SimpleNamespace(probe_backend="pyav")
    )
    monkeypatch.setattr(
        app_main, "ensure_probe_backend_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        app_main,
        "ensure_fingerprint_fallback_chain_available",
        lambda: (_ for _ in ()).throw(app_main.FingerprintError("ffmpeg missing")),
    )

    class _FakeApp:
        def __init__(self, _argv):
            pass

    critical_calls: list[tuple[str, str]] = []

    class _FakeMsgBox:
        @staticmethod
        def critical(_parent, title: str, message: str) -> None:
            critical_calls.append((title, message))

    qtwidgets = types.SimpleNamespace(QApplication=_FakeApp, QMessageBox=_FakeMsgBox)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", qtwidgets)
    fake_main_window_module = types.ModuleType("video_duperz.ui.main_window")
    fake_main_window_module.MainWindow = object  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "video_duperz.ui.main_window", fake_main_window_module
    )

    rc = app_main._cmd_gui(argparse.Namespace())

    assert rc == 2
    assert critical_calls == [("Scan Backend Unavailable", "ffmpeg missing")]


def test_cmd_scan_applies_and_restores_scan_process_priority(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        app_main,
        "load_settings",
        lambda: types.SimpleNamespace(
            probe_backend="pyav",
            normalized_extensions=lambda: ["mp4"],
            max_workers=2,
            drive_worker_overrides={},
            probe_worker_mode="balanced",
            ffmpeg_exe_path="",
            ffprobe_exe_path="",
            scan_child_cpu_priority="high",
            scan_child_io_mode="background",
            scan_parent_cpu_priority="below_normal",
            scan_parent_io_mode="background",
            scan_db_batch_size=512,
            scan_db_flush_interval_ms=200,
            scan_enum_queue_max=4096,
            scan_progress_emit_interval_ms=200,
            scan_progress_emit_every_files=100,
        ),
    )
    monkeypatch.setattr(
        app_main, "ensure_probe_backend_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        app_main, "ensure_fingerprint_fallback_chain_available", lambda *_a, **_k: None
    )

    calls: list[str] = []

    monkeypatch.setattr(
        app_main,
        "apply_scan_priority_to_current_process",
        lambda cpu_priority, io_mode: (
            calls.append(f"apply:{cpu_priority}:{io_mode}") or "state"
        ),
    )
    monkeypatch.setattr(
        app_main,
        "restore_scan_priority_to_current_process",
        lambda state: calls.append(f"restore:{state}"),
    )

    class _FakeDb:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(app_main, "Database", lambda: _FakeDb())
    monkeypatch.setattr(
        app_main,
        "run_scan",
        lambda **kwargs: (
            calls.append(
                "child:"
                f"{kwargs['scan_child_cpu_priority']}:{kwargs['scan_child_io_mode']}"
            )
            or types.SimpleNamespace(
                scan_id=7,
                scanned_files=2,
                cached_files=0,
                fingerprinted_files=2,
                groups=[],
                issues=[],
                metrics={},
            )
        ),
    )

    rc = app_main._cmd_scan(
        argparse.Namespace(
            roots=["D:/Videos"],
            profile="balanced",
        )
    )

    assert rc == 0
    assert calls == [
        "apply:below_normal:background",
        "child:high:background",
        "restore:state",
    ]
    assert "scan_id=7" in capsys.readouterr().out
