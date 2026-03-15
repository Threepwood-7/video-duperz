"""High-level scan orchestration entry points built on the runtime engine."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from .fingerprint import (
    build_fingerprint_record_with_fallback,
    ensure_fingerprint_fallback_chain_available,
)
from .matcher import build_duplicate_groups, find_duplicate_edges
from .models import (
    FrameDecodeBackendId,
    ProbeBackendId,
    ScanProgress,
    ScanResult,
    VideoMeta,
)
from .pipeline_runtime import run_scan_runtime
from .probe import ensure_probe_fallback_chain_available, probe_video
from .scanner import build_physical_drive_scan_plan, enumerate_video_files

if TYPE_CHECKING:
    import threading

    from .db import Database

ProgressCallback = Callable[[ScanProgress], None]


@dataclass(slots=True)
class _AnalyzeOutput:
    """Analyze result payload emitted back into the runtime loop."""

    meta: VideoMeta
    hashes: list[int]
    probe_s: float = 0.0
    fingerprint_s: float = 0.0
    probe_fallback_backend: ProbeBackendId | None = None
    fingerprint_fallback_decoder: FrameDecodeBackendId | None = None


def _alternate_probe_backend(backend: ProbeBackendId) -> ProbeBackendId:
    """Return the alternate metadata backend for a scan."""
    if backend == "ffprobe":
        return "pyav"
    return "ffprobe"


def ensure_analyze_fallback_chain_available(
    probe_backend: ProbeBackendId = "pyav",
) -> None:
    """Raise when the analyze fallback chain is not fully available."""
    ensure_probe_fallback_chain_available(probe_backend)
    ensure_fingerprint_fallback_chain_available()


def _analyze_once(
    path: str,
    cached_meta: VideoMeta | None,
    *,
    probe_backend: ProbeBackendId,
    relaxed_probe: bool,
) -> _AnalyzeOutput:
    """Run one full analyze attempt with one metadata backend selection."""
    probe_fallback_backend: ProbeBackendId | None = None
    if cached_meta is None:
        probe_started = time.perf_counter()
        meta = probe_video(path, backend=probe_backend, relaxed=relaxed_probe)
        probe_s = max(0.0, time.perf_counter() - probe_started)
    else:
        meta = cached_meta
        probe_s = 0.0

    fp_started = time.perf_counter()
    fp_result = build_fingerprint_record_with_fallback(
        file_id=0,
        duration_s=meta.duration_s,
        path=path,
    )
    fingerprint_s = max(0.0, time.perf_counter() - fp_started)
    return _AnalyzeOutput(
        meta=meta,
        hashes=fp_result.record.hashes,
        probe_s=probe_s,
        fingerprint_s=fingerprint_s,
        probe_fallback_backend=probe_fallback_backend,
        fingerprint_fallback_decoder=fp_result.fallback_decoder,
    )


def _analyze_file_with_fallbacks(
    path: str,
    cached_meta: VideoMeta | None,
    *,
    probe_backend: ProbeBackendId,
) -> _AnalyzeOutput:
    """Run analyze with alternate metadata fallback after any primary failure."""
    try:
        return _analyze_once(
            path,
            cached_meta,
            probe_backend=probe_backend,
            relaxed_probe=False,
        )
    except Exception:
        if cached_meta is not None:
            raise
        fallback_backend = _alternate_probe_backend(probe_backend)
        output = _analyze_once(
            path,
            None,
            probe_backend=fallback_backend,
            relaxed_probe=True,
        )
        output.probe_fallback_backend = fallback_backend
        return output


def _analyze_file(path: str, cached_meta: VideoMeta | None) -> _AnalyzeOutput:
    """Preserve the legacy ffprobe analyze seam used by tests and callers."""
    return _analyze_file_with_fallbacks(
        path,
        cached_meta,
        probe_backend="ffprobe",
    )


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
    """Run a full scan using the fallback-capable analyze pipeline."""
    primary_backend: ProbeBackendId = (
        "ffprobe" if probe_backend == "ffprobe" else "pyav"
    )
    ensure_analyze_fallback_chain_available(primary_backend)
    analyze_file: Callable[[str, VideoMeta | None], _AnalyzeOutput]
    if primary_backend == "ffprobe":
        analyze_file = _analyze_file
    else:
        analyze_file = partial(
            _analyze_file_with_fallbacks,
            probe_backend=primary_backend,
        )
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
        analyze_file=analyze_file,
        enumerate_video_files_fn=enumerate_video_files,
        build_scan_plan_fn=build_physical_drive_scan_plan,
        find_duplicate_edges_fn=find_duplicate_edges,
        build_duplicate_groups_fn=build_duplicate_groups,
    )
