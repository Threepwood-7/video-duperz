"""High-level scan orchestration entry points built on the runtime engine."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .analyze_process import (
    AnalyzeLauncher,
    SubprocessAnalyzeLauncher,
    ensure_analyze_fallback_chain_available,
)
from .matcher import build_duplicate_groups, find_duplicate_edges
from .models import ProbeBackendId, ScanProgress, ScanResult
from .pipeline_runtime import run_scan_runtime
from .scanner import build_physical_drive_scan_plan, enumerate_video_files

if TYPE_CHECKING:
    import threading

    from .db import Database

ProgressCallback = Callable[[ScanProgress], None]


def create_analyze_launcher(probe_backend: ProbeBackendId) -> AnalyzeLauncher:
    """Build the production analyze launcher for the selected probe backend."""
    return SubprocessAnalyzeLauncher(probe_backend)


def run_scan(
    db: Database,
    roots: list[str],
    extensions: list[str],
    profile: str = "balanced",
    max_workers: int = 2,
    drive_worker_overrides: dict[str, int] | None = None,
    probe_backend: ProbeBackendId = "pyav",
    probe_worker_mode: str = "balanced",
    *,
    analysis_timeout_s: int = 60,
    db_batch_size: int,
    db_flush_interval_ms: int,
    enum_queue_max: int,
    progress_emit_interval_ms: int,
    progress_emit_every_files: int,
    cancel_event: threading.Event | None = None,
    progress_cb: ProgressCallback | None = None,
) -> ScanResult:
    """Run a full scan using the process-isolated analyze pipeline."""
    primary_backend: ProbeBackendId = (
        "ffprobe" if probe_backend == "ffprobe" else "pyav"
    )
    ensure_analyze_fallback_chain_available(primary_backend)
    return run_scan_runtime(
        db=db,
        roots=roots,
        extensions=extensions,
        profile=profile,
        max_workers=max_workers,
        drive_worker_overrides=drive_worker_overrides,
        probe_backend=primary_backend,
        probe_worker_mode=probe_worker_mode,
        analysis_timeout_s=analysis_timeout_s,
        db_batch_size=db_batch_size,
        db_flush_interval_ms=db_flush_interval_ms,
        enum_queue_max=enum_queue_max,
        progress_emit_interval_ms=progress_emit_interval_ms,
        progress_emit_every_files=progress_emit_every_files,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        analyze_launcher=create_analyze_launcher(primary_backend),
        enumerate_video_files_fn=enumerate_video_files,
        build_scan_plan_fn=build_physical_drive_scan_plan,
        find_duplicate_edges_fn=find_duplicate_edges,
        build_duplicate_groups_fn=build_duplicate_groups,
    )
