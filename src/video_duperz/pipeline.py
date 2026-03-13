"""High-level scan orchestration entry points built on the runtime engine."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .fingerprint import build_fingerprint_record
from .matcher import build_duplicate_groups, find_duplicate_edges
from .models import ScanProgress, ScanResult, VideoMeta
from .pipeline_runtime import run_scan_runtime
from .probe import ensure_ffprobe_available, probe_video
from .scanner import build_physical_drive_scan_plan, enumerate_video_files

if TYPE_CHECKING:
    import threading

    from .db import Database

ProgressCallback = Callable[[ScanProgress], None]


@dataclass(slots=True)
class _AnalyzeOutput:
    meta: VideoMeta
    hashes: list[int]
    probe_s: float = 0.0
    fingerprint_s: float = 0.0


def _analyze_file(path: str, cached_meta: VideoMeta | None) -> _AnalyzeOutput:
    if cached_meta is None:
        probe_started = time.perf_counter()
        meta = probe_video(path)
        probe_s = max(0.0, time.perf_counter() - probe_started)
    else:
        meta = cached_meta
        probe_s = 0.0
    fp_started = time.perf_counter()
    fp_record = build_fingerprint_record(
        file_id=0,
        duration_s=meta.duration_s,
        path=path,
    )
    fingerprint_s = max(0.0, time.perf_counter() - fp_started)
    return _AnalyzeOutput(
        meta=meta,
        hashes=fp_record.hashes,
        probe_s=probe_s,
        fingerprint_s=fingerprint_s,
    )


def run_scan(
    db: Database,
    roots: list[str],
    extensions: list[str],
    profile: str = "balanced",
    max_workers: int = 2,
    drive_worker_overrides: dict[str, int] | None = None,
    probe_worker_mode: str = "balanced",
    *,
    db_batch_size: int,
    db_flush_interval_ms: int,
    enum_queue_max: int,
    progress_emit_interval_ms: int,
    progress_emit_every_files: int,
    cancel_event: threading.Event | None = None,
    progress_cb: ProgressCallback | None = None,
) -> ScanResult:
    """Run a full scan using the default probe, fingerprint, and matcher pipeline."""
    # Preserve module-level monkeypatch seams while the runtime engine lives
    # in its own module.
    return run_scan_runtime(
        db=db,
        roots=roots,
        extensions=extensions,
        profile=profile,
        max_workers=max_workers,
        drive_worker_overrides=drive_worker_overrides,
        probe_worker_mode=probe_worker_mode,
        db_batch_size=db_batch_size,
        db_flush_interval_ms=db_flush_interval_ms,
        enum_queue_max=enum_queue_max,
        progress_emit_interval_ms=progress_emit_interval_ms,
        progress_emit_every_files=progress_emit_every_files,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        analyze_file=_analyze_file,
        ensure_ffprobe_available_fn=ensure_ffprobe_available,
        enumerate_video_files_fn=enumerate_video_files,
        build_scan_plan_fn=build_physical_drive_scan_plan,
        find_duplicate_edges_fn=find_duplicate_edges,
        build_duplicate_groups_fn=build_duplicate_groups,
    )
