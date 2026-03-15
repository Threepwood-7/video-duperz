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
from .probe import ensure_ffprobe_available, ensure_probe_backend_available, probe_video
from .scanner import build_physical_drive_scan_plan, enumerate_video_files

if TYPE_CHECKING:
    import threading

    from .db import Database

ProgressCallback = Callable[[ScanProgress], None]


@dataclass(slots=True)
class _AnalyzeOutput:
    """Collected probe and fingerprint payload produced for one file."""

    meta: VideoMeta
    hashes: list[int]
    probe_s: float = 0.0
    fingerprint_s: float = 0.0
    fingerprint_decoder_backend: FrameDecodeBackendId = "opencv"
    fingerprint_provenance_json: str = ""


def _analyze_file(path: str, cached_meta: VideoMeta | None) -> _AnalyzeOutput:
    """Analyze one file through the legacy ffprobe-primary path."""
    return _analyze_file_with_probe(
        path,
        cached_meta,
        probe_video_fn=partial(probe_video, backend="ffprobe"),
    )


def _analyze_file_with_probe(
    path: str,
    cached_meta: VideoMeta | None,
    *,
    probe_video_fn: Callable[[str], VideoMeta],
) -> _AnalyzeOutput:
    """Probe and fingerprint one file through the configured backend."""
    if cached_meta is None:
        probe_started = time.perf_counter()
        meta = probe_video_fn(path)
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
        fingerprint_decoder_backend=fp_result.decoder_backend,
        fingerprint_provenance_json=fp_result.provenance_json,
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
    db_batch_size: int,
    db_flush_interval_ms: int,
    enum_queue_max: int,
    progress_emit_interval_ms: int,
    progress_emit_every_files: int,
    cancel_event: threading.Event | None = None,
    progress_cb: ProgressCallback | None = None,
) -> ScanResult:
    """Run a full scan using the default probe, fingerprint, and matcher pipeline."""
    if probe_backend == "ffprobe":
        analyze_file: Callable[[str, VideoMeta | None], _AnalyzeOutput] = _analyze_file
        ensure_available_fn: Callable[[], object] = ensure_ffprobe_available
    else:
        analyze_file = partial(
            _analyze_file_with_probe,
            probe_video_fn=partial(probe_video, backend=probe_backend),
        )
        ensure_available_fn = partial(
            ensure_probe_backend_available, backend=probe_backend
        )
    ensure_fingerprint_fallback_chain_available()
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
        analyze_file=analyze_file,
        ensure_ffprobe_available_fn=ensure_available_fn,
        enumerate_video_files_fn=enumerate_video_files,
        build_scan_plan_fn=build_physical_drive_scan_plan,
        find_duplicate_edges_fn=find_duplicate_edges,
        build_duplicate_groups_fn=build_duplicate_groups,
    )
