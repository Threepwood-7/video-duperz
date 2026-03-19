"""High-level scan orchestration entry points built on the runtime engine."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from .audio_fingerprint import (
    AudioFingerprintError,
    compute_audio_fingerprint,
    ensure_fpcalc_available,
)
from .fingerprint import (
    build_fingerprint_record_with_fallback,
    ensure_fingerprint_fallback_chain_available,
)
from .matcher import build_duplicate_groups, find_duplicate_edges
from .models import (
    CrossResolutionMode,
    FrameDecodeBackendId,
    ProbeBackendId,
    ScanIssue,
    ScanProcessCpuPriority,
    ScanProcessIoMode,
    ScanProgress,
    ScanResult,
    VideoMeta,
)
from .pipeline_runtime import run_scan_runtime
from .probe import ensure_ffprobe_available, ensure_probe_backend_available, probe_video
from .scanner import build_physical_drive_scan_plan, enumerate_video_files

if TYPE_CHECKING:
    from threading import Event

    from .db import Database

ProgressCallback = Callable[[ScanProgress], None]
IssueCallback = Callable[[ScanIssue], None]


@dataclass(slots=True)
class _AnalyzeOutput:
    """Collected probe and fingerprint payload produced for one file."""

    meta: VideoMeta
    hashes: list[int]
    probe_s: float = 0.0
    fingerprint_s: float = 0.0
    fingerprint_decoder_backend: FrameDecodeBackendId = "opencv"
    fingerprint_provenance_json: str = ""
    visual_algo_version: int = 1
    audio_fingerprint: str = ""
    audio_fingerprint_error: str = ""


@dataclass(slots=True)
class _AnalyzeRuntimeOptions:
    """Active module-level scan analysis options used by the runtime seam."""

    probe_backend: ProbeBackendId = "pyav"
    ffmpeg_exe_path: str = ""
    ffprobe_exe_path: str = ""
    fpcalc_exe_path: str = ""
    scene_aware_sampling: bool = False
    audio_fingerprint_enabled: bool = False
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal"
    scan_child_io_mode: ScanProcessIoMode = "normal"


_ANALYZE_RUNTIME_OPTIONS = _AnalyzeRuntimeOptions()


def _analyze_file_with_probe(
    path: str,
    cached_meta: VideoMeta | None,
    *,
    probe_video_fn: Callable[[str], VideoMeta],
    scene_aware_sampling: bool = False,
    audio_fingerprint_enabled: bool = False,
    ffmpeg_exe_path: str = "",
    fpcalc_exe_path: str = "",
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
    scan_child_io_mode: ScanProcessIoMode = "normal",
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
    if ffmpeg_exe_path:
        fp_result = build_fingerprint_record_with_fallback(
            file_id=0,
            duration_s=meta.duration_s,
            path=path,
            scene_aware_sampling=scene_aware_sampling,
            ffmpeg_exe_path=ffmpeg_exe_path,
            scan_child_cpu_priority=scan_child_cpu_priority,
            scan_child_io_mode=scan_child_io_mode,
        )
    else:
        fp_result = build_fingerprint_record_with_fallback(
            file_id=0,
            duration_s=meta.duration_s,
            path=path,
            scene_aware_sampling=scene_aware_sampling,
            scan_child_cpu_priority=scan_child_cpu_priority,
            scan_child_io_mode=scan_child_io_mode,
        )
    fingerprint_s = max(0.0, time.perf_counter() - fp_started)
    audio_fingerprint = ""
    audio_fingerprint_error = ""
    if audio_fingerprint_enabled and meta.has_audio:
        try:
            audio_fingerprint = compute_audio_fingerprint(
                path,
                fpcalc_exe_path=fpcalc_exe_path,
                scan_child_cpu_priority=scan_child_cpu_priority,
                scan_child_io_mode=scan_child_io_mode,
            )
        except AudioFingerprintError as exc:
            audio_fingerprint_error = str(exc)
    return _AnalyzeOutput(
        meta=meta,
        hashes=fp_result.record.hashes,
        probe_s=probe_s,
        fingerprint_s=fingerprint_s,
        fingerprint_decoder_backend=fp_result.decoder_backend,
        fingerprint_provenance_json=fp_result.provenance_json,
        visual_algo_version=int(fp_result.record.algo_version),
        audio_fingerprint=audio_fingerprint,
        audio_fingerprint_error=audio_fingerprint_error,
    )


def _configure_runtime_analyze_options(
    probe_backend: ProbeBackendId,
    *,
    ffmpeg_exe_path: str = "",
    ffprobe_exe_path: str = "",
    fpcalc_exe_path: str = "",
    scene_aware_sampling: bool = False,
    audio_fingerprint_enabled: bool = False,
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
    scan_child_io_mode: ScanProcessIoMode = "normal",
) -> None:
    """Persist the active analyze options for the module-level runtime seam."""
    _ANALYZE_RUNTIME_OPTIONS.probe_backend = (
        "ffprobe" if probe_backend == "ffprobe" else probe_backend
    )
    _ANALYZE_RUNTIME_OPTIONS.ffmpeg_exe_path = str(ffmpeg_exe_path or "")
    _ANALYZE_RUNTIME_OPTIONS.ffprobe_exe_path = str(ffprobe_exe_path or "")
    _ANALYZE_RUNTIME_OPTIONS.fpcalc_exe_path = str(fpcalc_exe_path or "")
    _ANALYZE_RUNTIME_OPTIONS.scene_aware_sampling = bool(scene_aware_sampling)
    _ANALYZE_RUNTIME_OPTIONS.audio_fingerprint_enabled = bool(audio_fingerprint_enabled)
    _ANALYZE_RUNTIME_OPTIONS.scan_child_cpu_priority = scan_child_cpu_priority
    _ANALYZE_RUNTIME_OPTIONS.scan_child_io_mode = scan_child_io_mode


def _analyze_file(path: str, cached_meta: VideoMeta | None) -> _AnalyzeOutput:
    """Probe and fingerprint one file using the configured module-level options."""
    probe_video_kwargs: dict[str, str] = {}
    if _ANALYZE_RUNTIME_OPTIONS.ffprobe_exe_path:
        probe_video_kwargs["ffprobe_exe_path"] = (
            _ANALYZE_RUNTIME_OPTIONS.ffprobe_exe_path
        )
    if _ANALYZE_RUNTIME_OPTIONS.scan_child_cpu_priority != "normal":
        probe_video_kwargs["scan_child_cpu_priority"] = (
            _ANALYZE_RUNTIME_OPTIONS.scan_child_cpu_priority
        )
    if _ANALYZE_RUNTIME_OPTIONS.scan_child_io_mode != "normal":
        probe_video_kwargs["scan_child_io_mode"] = (
            _ANALYZE_RUNTIME_OPTIONS.scan_child_io_mode
        )
    return _analyze_file_with_probe(
        path,
        cached_meta,
        probe_video_fn=partial(
            probe_video,
            backend=_ANALYZE_RUNTIME_OPTIONS.probe_backend,
            **probe_video_kwargs,
        ),
        scene_aware_sampling=_ANALYZE_RUNTIME_OPTIONS.scene_aware_sampling,
        audio_fingerprint_enabled=_ANALYZE_RUNTIME_OPTIONS.audio_fingerprint_enabled,
        ffmpeg_exe_path=_ANALYZE_RUNTIME_OPTIONS.ffmpeg_exe_path,
        fpcalc_exe_path=_ANALYZE_RUNTIME_OPTIONS.fpcalc_exe_path,
        scan_child_cpu_priority=_ANALYZE_RUNTIME_OPTIONS.scan_child_cpu_priority,
        scan_child_io_mode=_ANALYZE_RUNTIME_OPTIONS.scan_child_io_mode,
    )


def build_analyze_file(
    probe_backend: ProbeBackendId,
    *,
    ffmpeg_exe_path: str = "",
    ffprobe_exe_path: str = "",
    fpcalc_exe_path: str = "",
    scene_aware_sampling: bool = False,
    audio_fingerprint_enabled: bool = False,
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
    scan_child_io_mode: ScanProcessIoMode = "normal",
) -> Callable[[str], _AnalyzeOutput]:
    """Build a single-path analyze callable for the selected probe backend."""
    runtime_analyze = _build_runtime_analyze_file(
        probe_backend,
        ffmpeg_exe_path=ffmpeg_exe_path,
        ffprobe_exe_path=ffprobe_exe_path,
        fpcalc_exe_path=fpcalc_exe_path,
        scene_aware_sampling=scene_aware_sampling,
        audio_fingerprint_enabled=audio_fingerprint_enabled,
        scan_child_cpu_priority=scan_child_cpu_priority,
        scan_child_io_mode=scan_child_io_mode,
    )

    def _analyze_uncached(path: str) -> _AnalyzeOutput:
        return runtime_analyze(path, None)

    return _analyze_uncached


def _build_runtime_analyze_file(
    probe_backend: ProbeBackendId,
    *,
    ffmpeg_exe_path: str = "",
    ffprobe_exe_path: str = "",
    fpcalc_exe_path: str = "",
    scene_aware_sampling: bool = False,
    audio_fingerprint_enabled: bool = False,
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
    scan_child_io_mode: ScanProcessIoMode = "normal",
) -> Callable[[str, VideoMeta | None], _AnalyzeOutput]:
    """Build the runtime analyze callable used by the threaded pipeline."""
    _configure_runtime_analyze_options(
        probe_backend,
        ffmpeg_exe_path=ffmpeg_exe_path,
        ffprobe_exe_path=ffprobe_exe_path,
        fpcalc_exe_path=fpcalc_exe_path,
        scene_aware_sampling=scene_aware_sampling,
        audio_fingerprint_enabled=audio_fingerprint_enabled,
        scan_child_cpu_priority=scan_child_cpu_priority,
        scan_child_io_mode=scan_child_io_mode,
    )
    return _analyze_file


def run_scan(
    db: Database,
    roots: list[str],
    extensions: list[str],
    scan_size_mib_min: int = 50,
    scan_size_mib_max: int = 0,
    profile: str = "balanced",
    custom_similarity_threshold: float = 0.18,
    duration_tolerance_s: float = 8.0,
    scene_aware_sampling: bool = False,
    audio_fingerprint_enabled: bool = False,
    cross_resolution_mode: CrossResolutionMode = "off",
    max_workers: int = 2,
    drive_worker_overrides: dict[str, int] | None = None,
    probe_backend: ProbeBackendId = "pyav",
    probe_worker_mode: str = "balanced",
    ffmpeg_exe_path: str = "",
    ffprobe_exe_path: str = "",
    fpcalc_exe_path: str = "",
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
    scan_child_io_mode: ScanProcessIoMode = "normal",
    *,
    db_batch_size: int,
    db_flush_interval_ms: int,
    enum_queue_max: int,
    progress_emit_interval_ms: int,
    progress_emit_every_files: int,
    cancel_event: Event | None = None,
    pause_event: Event | None = None,
    progress_cb: ProgressCallback | None = None,
    issue_cb: IssueCallback | None = None,
    resume_scan_id: int | None = None,
    retry_failed_files: bool = True,
) -> ScanResult:
    """Run a full scan using the default probe, fingerprint, and matcher pipeline."""
    audio_enabled_for_run = bool(audio_fingerprint_enabled)
    if audio_enabled_for_run:
        try:
            ensure_fpcalc_available(fpcalc_exe_path)
        except AudioFingerprintError as exc:
            audio_enabled_for_run = False
            if issue_cb is not None:
                issue_cb(
                    ScanIssue(
                        stage="audio_fingerprint",
                        path="",
                        message=(f"Audio fingerprinting disabled for this scan: {exc}"),
                    )
                )
    analyze_file = _build_runtime_analyze_file(
        probe_backend,
        ffmpeg_exe_path=ffmpeg_exe_path,
        ffprobe_exe_path=ffprobe_exe_path,
        fpcalc_exe_path=fpcalc_exe_path,
        scene_aware_sampling=scene_aware_sampling,
        audio_fingerprint_enabled=audio_enabled_for_run,
        scan_child_cpu_priority=scan_child_cpu_priority,
        scan_child_io_mode=scan_child_io_mode,
    )
    if probe_backend == "ffprobe":
        ensure_available_fn = (
            (lambda: ensure_ffprobe_available(ffprobe_exe_path))
            if ffprobe_exe_path
            else ensure_ffprobe_available
        )
    else:
        ensure_available_fn = (
            partial(
                ensure_probe_backend_available,
                backend=probe_backend,
                ffprobe_exe_path=ffprobe_exe_path,
            )
            if ffprobe_exe_path
            else partial(
                ensure_probe_backend_available,
                backend=probe_backend,
            )
        )
    if ffmpeg_exe_path:
        ensure_fingerprint_fallback_chain_available(ffmpeg_exe_path)
    else:
        ensure_fingerprint_fallback_chain_available()
    # Preserve module-level monkeypatch seams while the runtime engine lives
    # in its own module.
    return run_scan_runtime(
        db=db,
        roots=roots,
        extensions=extensions,
        scan_size_mib_min=scan_size_mib_min,
        scan_size_mib_max=scan_size_mib_max,
        profile=profile,
        custom_similarity_threshold=custom_similarity_threshold,
        duration_tolerance_s=duration_tolerance_s,
        scene_aware_sampling=scene_aware_sampling,
        audio_fingerprint_enabled=audio_enabled_for_run,
        cross_resolution_mode=cross_resolution_mode,
        max_workers=max_workers,
        drive_worker_overrides=drive_worker_overrides,
        probe_backend=probe_backend,
        probe_worker_mode=probe_worker_mode,
        db_batch_size=db_batch_size,
        db_flush_interval_ms=db_flush_interval_ms,
        enum_queue_max=enum_queue_max,
        progress_emit_interval_ms=progress_emit_interval_ms,
        progress_emit_every_files=progress_emit_every_files,
        cancel_event=cancel_event,
        pause_event=pause_event,
        progress_cb=progress_cb,
        issue_cb=issue_cb,
        analyze_file=analyze_file,
        ensure_ffprobe_available_fn=ensure_available_fn,
        enumerate_video_files_fn=enumerate_video_files,
        build_scan_plan_fn=build_physical_drive_scan_plan,
        find_duplicate_edges_fn=find_duplicate_edges,
        build_duplicate_groups_fn=build_duplicate_groups,
        resume_scan_id=resume_scan_id,
        retry_failed_files=retry_failed_files,
    )
