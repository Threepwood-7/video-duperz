"""Background worker helpers used by the UI for scans and preview tasks."""

from __future__ import annotations

import traceback
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QRunnable, Signal

from ..db import Database
from ..exact_match import (
    ExactMatchFile,
    compare_group_files,
    normalize_exact_sample_pair,
)
from ..pipeline import run_scan
from ..process_priority import normalize_scan_cpu_priority, normalize_scan_io_mode
from .thumbnails import extract_thumbnail_pair, normalize_frame_pair

if TYPE_CHECKING:
    from ..models import (
        CrossResolutionMode,
        ProbeBackendId,
        ScanProcessCpuPriority,
        ScanProcessIoMode,
    )


class ScanWorkerSignals(QObject):
    """Signals emitted by the background scan worker."""

    progress = Signal(object)
    issue = Signal(object)
    finished = Signal(object)
    error = Signal(str)


class ScanWorker(QRunnable):
    """Background QRunnable that executes the scan pipeline off the UI thread."""

    def __init__(
        self,
        db_file: str,
        roots: list[str],
        extensions: list[str],
        scan_size_mib_min: int,
        scan_size_mib_max: int,
        profile: str,
        custom_similarity_threshold: float,
        duration_tolerance_s: float,
        scene_aware_sampling: bool,
        audio_fingerprint_enabled: bool,
        cross_resolution_mode: CrossResolutionMode,
        fingerprint_timeout_s: float,
        max_workers: int,
        drive_worker_overrides: dict[str, int] | None = None,
        probe_backend: ProbeBackendId = "pyav",
        probe_worker_mode: str = "balanced",
        ffmpeg_exe_path: str = "",
        ffprobe_exe_path: str = "",
        fpcalc_exe_path: str = "",
        scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
        scan_child_io_mode: ScanProcessIoMode = "normal",
        db_batch_size: int = 512,
        db_flush_interval_ms: int = 200,
        enum_queue_max: int = 4096,
        progress_emit_interval_ms: int = 200,
        progress_emit_every_files: int = 100,
        resume_scan_id: int | None = None,
        retry_failed_files: bool = True,
    ) -> None:
        super().__init__()
        self.signals = ScanWorkerSignals()
        self._db_file = db_file
        self._roots = roots
        self._extensions = extensions
        self._scan_size_mib_min = max(0, int(scan_size_mib_min))
        self._scan_size_mib_max = max(0, int(scan_size_mib_max))
        self._profile = profile
        self._custom_similarity_threshold = float(custom_similarity_threshold)
        self._duration_tolerance_s = max(0.0, float(duration_tolerance_s))
        self._scene_aware_sampling = bool(scene_aware_sampling)
        self._audio_fingerprint_enabled = bool(audio_fingerprint_enabled)
        self._cross_resolution_mode: CrossResolutionMode = cross_resolution_mode
        self._fingerprint_timeout_s = max(0.1, float(fingerprint_timeout_s))
        self._max_workers = max_workers
        self._drive_worker_overrides = dict(drive_worker_overrides or {})
        self._probe_backend: ProbeBackendId = (
            "ffprobe" if probe_backend == "ffprobe" else "pyav"
        )
        self._probe_worker_mode = str(probe_worker_mode or "balanced")
        self._ffmpeg_exe_path = str(ffmpeg_exe_path or "")
        self._ffprobe_exe_path = str(ffprobe_exe_path or "")
        self._fpcalc_exe_path = str(fpcalc_exe_path or "")
        self._scan_child_cpu_priority: ScanProcessCpuPriority = (
            normalize_scan_cpu_priority(scan_child_cpu_priority)
        )
        self._scan_child_io_mode: ScanProcessIoMode = normalize_scan_io_mode(
            scan_child_io_mode
        )
        self._db_batch_size = max(32, int(db_batch_size))
        self._db_flush_interval_ms = max(50, int(db_flush_interval_ms))
        self._enum_queue_max = max(256, int(enum_queue_max))
        self._progress_emit_interval_ms = max(50, int(progress_emit_interval_ms))
        self._progress_emit_every_files = max(10, int(progress_emit_every_files))
        self._resume_scan_id = (
            int(resume_scan_id) if resume_scan_id is not None else None
        )
        self._retry_failed_files = bool(retry_failed_files)
        self._cancel = Event()
        self._pause = Event()

    def cancel(self) -> None:
        self._cancel.set()

    def pause(self) -> None:
        """Request a graceful pause for the active scan."""
        self._pause.set()

    def run(self) -> None:
        try:
            with Database(self._db_file) as db:
                result = run_scan(
                    db=db,
                    roots=self._roots,
                    extensions=self._extensions,
                    scan_size_mib_min=self._scan_size_mib_min,
                    scan_size_mib_max=self._scan_size_mib_max,
                    profile=self._profile,
                    custom_similarity_threshold=self._custom_similarity_threshold,
                    duration_tolerance_s=self._duration_tolerance_s,
                    scene_aware_sampling=self._scene_aware_sampling,
                    audio_fingerprint_enabled=self._audio_fingerprint_enabled,
                    cross_resolution_mode=self._cross_resolution_mode,
                    fingerprint_timeout_s=self._fingerprint_timeout_s,
                    max_workers=self._max_workers,
                    drive_worker_overrides=self._drive_worker_overrides,
                    probe_backend=self._probe_backend,
                    probe_worker_mode=self._probe_worker_mode,
                    ffmpeg_exe_path=self._ffmpeg_exe_path,
                    ffprobe_exe_path=self._ffprobe_exe_path,
                    fpcalc_exe_path=self._fpcalc_exe_path,
                    scan_child_cpu_priority=self._scan_child_cpu_priority,
                    scan_child_io_mode=self._scan_child_io_mode,
                    db_batch_size=self._db_batch_size,
                    db_flush_interval_ms=self._db_flush_interval_ms,
                    enum_queue_max=self._enum_queue_max,
                    progress_emit_interval_ms=self._progress_emit_interval_ms,
                    progress_emit_every_files=self._progress_emit_every_files,
                    cancel_event=self._cancel,
                    pause_event=self._pause,
                    progress_cb=lambda p: self.signals.progress.emit(p),
                    issue_cb=lambda issue: self.signals.issue.emit(issue),
                    resume_scan_id=self._resume_scan_id,
                    retry_failed_files=self._retry_failed_files,
                )
            self.signals.finished.emit(result)
        except Exception:
            self.signals.error.emit(traceback.format_exc())


class ThumbnailWorkerSignals(QObject):
    """Signals emitted while a thumbnail pair worker completes or fails."""

    ready = Signal(object)
    error = Signal(object)


class ThumbnailPairWorker(QRunnable):
    """Background QRunnable that materializes cached thumbnail pairs."""

    def __init__(
        self,
        row_token: str,
        file_id: int,
        source_path: str,
        cache_path_a: str,
        cache_path_b: str,
        width: int,
        height: int,
        frame_a_pct: int,
        frame_b_pct: int,
    ) -> None:
        super().__init__()
        self.signals = ThumbnailWorkerSignals()
        self.worker_id = id(self)
        self._row_token = row_token
        self._file_id = file_id
        self._source_path = source_path
        self._cache_path_a = cache_path_a
        self._cache_path_b = cache_path_b
        self._width = width
        self._height = height
        self._frame_a_pct, self._frame_b_pct = normalize_frame_pair(
            frame_a_pct, frame_b_pct
        )

    def run(self) -> None:
        payload = {
            "token": self._row_token,
            "file_id": self._file_id,
            "cache_path_a": self._cache_path_a,
            "cache_path_b": self._cache_path_b,
            "worker_id": self.worker_id,
        }
        src = Path(self._source_path)
        if not src.exists():
            self.signals.error.emit({**payload, "message": "source file missing"})
            return

        out_a = Path(self._cache_path_a)
        out_b = Path(self._cache_path_b)
        if out_a.exists() and out_b.exists():
            self.signals.ready.emit(payload)
            return

        ok, err = extract_thumbnail_pair(
            path=self._source_path,
            output_path_a=self._cache_path_a,
            output_path_b=self._cache_path_b,
            target_w=self._width,
            target_h=self._height,
            frame_a_pct=self._frame_a_pct,
            frame_b_pct=self._frame_b_pct,
        )
        if ok:
            self.signals.ready.emit(payload)
            return
        self.signals.error.emit(
            {**payload, "message": err or "thumbnail generation failed"}
        )


class ExactMatchWorkerSignals(QObject):
    """Signals emitted while exact-match labels are computed for a group."""

    ready = Signal(object)
    error = Signal(object)


class ExactMatchGroupWorker(QRunnable):
    """Background QRunnable that compares one visible group for exact matches."""

    def __init__(
        self,
        row_token: str,
        group_key: str,
        files: list[dict[str, object]],
        block_mib: int,
        sample_a_pct: int,
        sample_b_pct: int,
    ) -> None:
        super().__init__()
        self.signals = ExactMatchWorkerSignals()
        self.worker_id = id(self)
        self._row_token = str(row_token)
        self._group_key = str(group_key)
        self._files = list(files)
        self._block_mib = max(1, int(block_mib))
        self._sample_a_pct, self._sample_b_pct = normalize_exact_sample_pair(
            sample_a_pct, sample_b_pct
        )

    def run(self) -> None:
        payload = {
            "token": self._row_token,
            "group_key": self._group_key,
            "worker_id": self.worker_id,
        }
        try:
            entries: list[ExactMatchFile] = []
            for raw in self._files:
                file_id_raw = raw.get("file_id", 0)
                path_raw = raw.get("path", "")
                size_raw = raw.get("size", 0)
                file_id = int(file_id_raw) if isinstance(file_id_raw, int | str) else 0
                path = str(path_raw).strip()
                size = int(size_raw) if isinstance(size_raw, int | str) else 0
                if file_id <= 0 or not path:
                    continue
                entries.append(
                    ExactMatchFile(file_id=file_id, path=path, size=max(0, size))
                )

            result = compare_group_files(
                files=entries,
                block_mib=self._block_mib,
                sample_a_pct=self._sample_a_pct,
                sample_b_pct=self._sample_b_pct,
            )
            self.signals.ready.emit(
                {
                    **payload,
                    "labels": result.labels,
                    "errors": result.errors,
                }
            )
        except Exception as exc:
            self.signals.error.emit({**payload, "message": str(exc)})
