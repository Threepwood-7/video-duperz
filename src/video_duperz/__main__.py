"""Command-line entry points for the Video Duperz application."""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

from threep_commons.paths import configure_qsettings

from .cleaner import FULL_RESET_DEFAULT_DELAY_MS, run_full_reset
from .config import load_settings
from .constants import APP_IDENTITY
from .db import Database
from .exporters import export_scan
from .pipeline import run_scan
from .probe import ProbeError, ensure_probe_backend_available


def _metrics_map(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        raw_map = cast("dict[object, object]", value)
        for key, raw in raw_map.items():
            if isinstance(key, str | int | float | bool):
                normalized[str(key)] = raw
        return normalized
    return {}


def _metric_float(metrics: dict[str, object], key: str, default: float = 0.0) -> float:
    value = metrics.get(key, default)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _metric_int(metrics: dict[str, object], key: str, default: int = 0) -> int:
    value = metrics.get(key, default)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-duperz", description="Duplicate video finder"
    )
    parser.add_argument(
        "--config-dir",
        dest="config_dir",
        required=False,
        default=None,
        help=(
            "Override QSettings INI root directory (takes precedence over CONFIG_DIR)."
        ),
    )
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        required=False,
        default=None,
        help="Override runtime data root directory (takes precedence over DATA_DIR).",
    )
    sub = parser.add_subparsers(dest="command")

    gui = sub.add_parser("gui", help="Launch desktop UI")
    gui.set_defaults(func=_cmd_gui)

    scan = sub.add_parser("scan", help="Run a headless scan")
    scan.add_argument("--roots", nargs="+", required=True, help="Root folders to scan")
    scan.add_argument(
        "--profile",
        default="balanced",
        choices=["balanced", "conservative", "aggressive"],
    )
    scan.set_defaults(func=_cmd_scan)

    export = sub.add_parser("export", help="Export duplicate groups to CSV and JSON")
    export.add_argument(
        "--scan-id", type=int, default=None, help="Scan id (latest if omitted)"
    )
    export.add_argument("--out", required=True, help="Output directory")
    export.set_defaults(func=_cmd_export)

    clean = sub.add_parser("clean", help="Run full reset cleaner")
    clean.add_argument(
        "--full-reset",
        action="store_true",
        help="Required guard for destructive full reset",
    )
    clean.add_argument(
        "--delay-ms",
        type=int,
        default=FULL_RESET_DEFAULT_DELAY_MS,
        help="Delay before cleanup",
    )
    clean.add_argument(
        "--relaunch", action="store_true", help="Relaunch GUI after cleanup"
    )
    clean.set_defaults(func=_cmd_clean)
    return parser


def _apply_runtime_overrides(args: argparse.Namespace) -> None:
    if args.config_dir:
        os.environ["CONFIG_DIR"] = str(Path(args.config_dir).expanduser())
    if args.data_dir:
        os.environ["DATA_DIR"] = str(Path(args.data_dir).expanduser())
    configure_qsettings(APP_IDENTITY)


def _cmd_gui(_args: argparse.Namespace) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .ui.main_window import MainWindow

    settings = load_settings()
    try:
        ensure_probe_backend_available(settings.probe_backend)
    except ProbeError as exc:
        app = QApplication(sys.argv)
        QMessageBox.critical(None, "Probe Backend Unavailable", str(exc))
        return 2

    app = QApplication(sys.argv)
    db = Database()
    window = MainWindow(db=db, settings=settings)
    window.show()
    exit_code = app.exec()
    full_reset_requested = bool(
        getattr(window, "consume_full_reset_requested", lambda: False)()
    )
    with contextlib.suppress(Exception):
        db.close()
    if full_reset_requested:
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "video_duperz",
                    "clean",
                    "--full-reset",
                    "--delay-ms",
                    str(FULL_RESET_DEFAULT_DELAY_MS),
                    "--relaunch",
                ]
            )
        except Exception as exc:
            print(f"ERROR: failed to launch full reset cleaner: {exc}", file=sys.stderr)
            return 3
        return 0
    return int(exit_code)


def _cmd_scan(args: argparse.Namespace) -> int:
    settings = load_settings()
    try:
        ensure_probe_backend_available(settings.probe_backend)
    except ProbeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    extensions = settings.normalized_extensions()
    with Database() as db:
        result = run_scan(
            db=db,
            roots=[str(Path(p)) for p in args.roots],
            extensions=extensions,
            profile=args.profile,
            max_workers=settings.max_workers,
            drive_worker_overrides=settings.drive_worker_overrides,
            probe_backend=settings.probe_backend,
            probe_worker_mode=settings.probe_worker_mode,
            db_batch_size=settings.scan_db_batch_size,
            db_flush_interval_ms=settings.scan_db_flush_interval_ms,
            enum_queue_max=settings.scan_enum_queue_max,
            progress_emit_interval_ms=settings.scan_progress_emit_interval_ms,
            progress_emit_every_files=settings.scan_progress_emit_every_files,
        )
        print(f"scan_id={result.scan_id}")
        print(f"scanned_files={result.scanned_files}")
        print(f"cached_files={result.cached_files}")
        print(f"fingerprinted_files={result.fingerprinted_files}")
        print(f"duplicate_groups={len(result.groups)}")
        print(f"issues={len(result.issues)}")
        metrics = _metrics_map(result.metrics)
        stage_seconds = _metrics_map(metrics.get("stage_seconds", {}))
        if stage_seconds:
            enumerate_s = _metric_float(stage_seconds, "enumerate")
            db_write_s = _metric_float(stage_seconds, "db_write")
            probe_s = _metric_float(stage_seconds, "probe")
            fingerprint_s = _metric_float(stage_seconds, "fingerprint")
            matching_s = _metric_float(stage_seconds, "matching")
            print(
                "timings_s="
                f"enumerate:{enumerate_s:.3f},db_write:{db_write_s:.3f},probe:{probe_s:.3f},"
                f"fingerprint:{fingerprint_s:.3f},matching:{matching_s:.3f}"
            )
        print(f"flush_count={_metric_int(metrics, 'flush_count')}")
        print(f"avg_rows_per_flush={_metric_float(metrics, 'avg_rows_per_flush'):.2f}")
        print(f"max_queue_depth={_metric_int(metrics, 'max_queue_depth')}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    out = Path(args.out)
    with Database() as db:
        scan_id = args.scan_id if args.scan_id is not None else db.latest_scan_id()
        if scan_id is None:
            print("ERROR: no scans available", file=sys.stderr)
            return 2
        csv_path, json_path = export_scan(db, scan_id=scan_id, out_dir=out)
        print(f"scan_id={scan_id}")
        print(f"csv={csv_path}")
        print(f"json={json_path}")
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    if not bool(args.full_reset):
        print("ERROR: --full-reset is required for clean", file=sys.stderr)
        return 2
    return int(run_full_reset(delay_ms=args.delay_ms, relaunch=bool(args.relaunch)))


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch to the selected command handler."""
    parser = _build_parser()
    argv_list = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(argv_list)
    if not args.command:
        args = parser.parse_args([*argv_list, "gui"])
    _apply_runtime_overrides(args)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
