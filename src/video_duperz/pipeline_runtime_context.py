"""Context models and setup helpers for the streaming scan runtime."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from threading import Condition, Event, Lock, Thread
from typing import TYPE_CHECKING, Any, Protocol, cast

from threep_commons.fs_paths import path_key

from .models import (
    FrameDecodeBackendId,
    ProbeBackendId,
    ProbeWorkerMode,
    ScanEnumerationResult,
    ScanIssue,
    ScanLaneSnapshot,
    ScanLinkRecord,
    ScanProgress,
    ScanWorkKind,
    VideoMeta,
    VideoRecord,
)

if TYPE_CHECKING:
    from .db import Database

_ScanPlanFn = Callable[..., Any]
_EnumerateFn = Callable[
    ...,
    ScanEnumerationResult | tuple[list[VideoRecord], list[ScanIssue]],
]
_FindEdgesFn = Callable[..., tuple[Any, Any]]
_BuildGroupsFn = Callable[..., list[Any]]


class AnalyzeOutputLike(Protocol):
    """Protocol describing the probe and fingerprint payload of one analysis."""

    meta: VideoMeta
    hashes: list[int]
    probe_s: float
    fingerprint_s: float
    fingerprint_decoder_backend: FrameDecodeBackendId
    fingerprint_provenance_json: str


class _FailedFilePathKeysFn(Protocol):
    """Protocol for the optional DB helper that lists failed path keys."""

    def __call__(self, scan_id: int) -> set[str]:
        """Return normalized failed-file path keys for one scan."""
        ...


@dataclass(slots=True)
class AnalyzeTask:
    """Queued analysis work item for a single discovered file."""

    file: VideoRecord
    file_id: int
    cached_meta: VideoMeta | None
    lane: int
    source_root: str
    path: str
    size: int
    mtime_ns: int
    work_kind: ScanWorkKind


@dataclass(slots=True)
class ScanContext:
    """Mutable shared state used by the streaming runtime threads."""

    db: Database
    roots: list[str]
    extensions: list[str]
    scan_size_mib_min: int
    scan_size_mib_max: int
    profile: str
    probe_backend: ProbeBackendId
    drive_worker_overrides: dict[str, int] | None
    cancel_event: Event | None
    pause_event: Event | None
    stop_event: Event
    progress_cb: Callable[[ScanProgress], None] | None
    issue_cb: Callable[[ScanIssue], None] | None
    analyze_file: Callable[[str, VideoMeta | None], AnalyzeOutputLike]
    enumerate_video_files_fn: _EnumerateFn
    build_scan_plan_fn: _ScanPlanFn
    find_duplicate_edges_fn: _FindEdgesFn
    build_duplicate_groups_fn: _BuildGroupsFn
    db_batch_size: int
    db_flush_interval_s: float
    progress_emit_interval_s: float
    progress_emit_every_files: int
    scan_id: int
    resume_scan_id: int | None
    retry_failed_files: bool
    issues: list[ScanIssue]
    requested_floor: int
    scan_plan: Any
    effective_worker_limit: int
    executor_worker_limit: int
    lane_runtime_caps: dict[int, int]
    lane_states: dict[int, ScanLaneSnapshot]
    lane_queues: dict[int, deque[AnalyzeTask]]
    root_to_lane: dict[str, int]
    lane_pending_roots: dict[int, int]
    enumerated_root_keys: set[str]
    dispatch_lane_cursor: int
    active_by_lane: dict[int, int]
    futures: dict[Any, AnalyzeTask]
    done_futures: deque[Any]
    event_cond: Condition
    state_lock: Lock
    queued_issues: deque[ScanIssue]
    recorded_issue_keys: set[tuple[str, str, str]]
    present_paths: set[str]
    streamed_path_keys: set[str]
    failed_path_keys: set[str]
    pending_discovered: list[VideoRecord]
    pending_scan_links: list[ScanLinkRecord]
    pending_meta_rows: list[tuple[int, int, int, VideoMeta]]
    pending_fp_rows: list[tuple[int, int, int, int, list[int]]]
    pending_fp_provenance_rows: list[tuple[int, FrameDecodeBackendId, str]]
    pending_probe_error_rows: list[tuple[int, int, int, str]]
    scan_started_at: float
    stage_seconds: dict[str, float]
    flush_count: int
    flush_rows_total: int
    rows_since_flush: int
    max_queue_depth: int
    last_tx_flush_at: float
    last_pending_write_at: float
    last_discovered_batch_at: float
    cached_files: int
    resume_cache_hits: int
    resume_reprocessed_files: int
    skipped_failed_files: int
    fingerprint_only_files: int
    probe_and_fingerprint_files: int
    fingerprinted_files: int
    prepared_files: int
    discovered_files: int
    discovered_bytes: int
    analyzed_files: int
    analyzed_bytes: int
    total_analyze_files: int
    active_workers: int
    enumerated_roots: int
    total_roots: int
    enum_finished: bool
    cancel_requested: bool
    pause_requested: bool
    stop_applied: bool
    enum_files: list[VideoRecord]
    enum_issues: list[ScanIssue]
    enum_error: Exception | None
    enum_queue: Queue[VideoRecord | ScanLinkRecord | object]
    enum_sentinel: object
    last_emit_at: float
    last_emit_stage: str
    last_emit_counter: int
    enum_thread: Thread | None = None
    cancel_thread: Thread | None = None
    pause_thread: Thread | None = None


@dataclass(slots=True)
class _RuntimeSettings:
    db_batch_size: int
    db_flush_interval_s: float
    enum_queue_max: int
    progress_emit_interval_s: float
    progress_emit_every_files: int
    requested_floor: int
    probe_worker_mode: ProbeWorkerMode


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _normalize_probe_worker_mode(value: str) -> ProbeWorkerMode:
    mode = str(value or "").strip().lower()
    if mode == "burst":
        return "burst"
    return "balanced"


def _build_runtime_settings(
    max_workers: int,
    probe_worker_mode: str,
    *,
    db_batch_size: int,
    db_flush_interval_ms: int,
    enum_queue_max: int,
    progress_emit_interval_ms: int,
    progress_emit_every_files: int,
) -> _RuntimeSettings:
    """Normalize runtime tuning knobs before creating the scan context."""
    db_batch_size = _clamp(db_batch_size, 32, 4096)
    db_flush_interval_ms = _clamp(db_flush_interval_ms, 50, 2000)
    enum_queue_max = _clamp(enum_queue_max, 256, 32768)
    progress_emit_interval_ms = _clamp(progress_emit_interval_ms, 50, 2000)
    progress_emit_every_files = _clamp(progress_emit_every_files, 10, 5000)
    return _RuntimeSettings(
        db_batch_size=db_batch_size,
        db_flush_interval_s=float(db_flush_interval_ms) / 1000.0,
        enum_queue_max=enum_queue_max,
        progress_emit_interval_s=float(progress_emit_interval_ms) / 1000.0,
        progress_emit_every_files=progress_emit_every_files,
        requested_floor=max(1, int(max_workers)),
        probe_worker_mode=_normalize_probe_worker_mode(probe_worker_mode),
    )


def worker_cap_message(scan_plan: Any) -> str | None:
    """Return the hard-cap warning text when drive limits reduce concurrency."""
    requested = int(scan_plan.requested_worker_target)
    effective = int(scan_plan.effective_total_workers)
    if requested <= effective:
        return None
    return (
        "Requested worker capacity "
        f"({requested}) reduced to {effective} by per-drive hard caps."
    )


def _build_lane_runtime_caps(
    scan_plan: Any,
    probe_worker_mode: ProbeWorkerMode,
) -> dict[int, int]:
    """Derive per-lane runtime caps from the scan plan and probe mode."""
    configured_lane_limits = {
        int(lane): max(1, int(limit))
        for lane, limit in scan_plan.lane_worker_limits.items()
    }
    if probe_worker_mode == "balanced":
        return dict.fromkeys(range(len(scan_plan.root_groups)), 1)
    return {
        lane: max(1, configured_lane_limits.get(lane, 1))
        for lane in range(len(scan_plan.root_groups))
    }


def _build_lane_runtime_state(
    scan_plan: Any,
) -> tuple[
    dict[int, ScanLaneSnapshot],
    dict[int, deque[AnalyzeTask]],
    dict[int, int],
]:
    """Initialize per-lane telemetry snapshots and pending task queues."""
    lane_states: dict[int, ScanLaneSnapshot] = {}
    lane_queues: dict[int, deque[AnalyzeTask]] = {}
    lane_pending_roots: dict[int, int] = {}
    for lane_idx, group in enumerate(scan_plan.root_groups):
        lane_states[lane_idx] = ScanLaneSnapshot(
            lane=lane_idx,
            roots=[str(Path(root)) for root in group],
            state="pending",
        )
        lane_queues[lane_idx] = deque()
        lane_pending_roots[lane_idx] = len(group)
    return lane_states, lane_queues, lane_pending_roots


def _build_root_to_lane(scan_plan: Any) -> dict[str, int]:
    """Normalize scan-plan root keys so queued files resolve to the right lane."""
    return {
        path_key(root): lane for root, lane in scan_plan.root_to_group_index.items()
    }


def _initial_stage_seconds() -> dict[str, float]:
    """Track cumulative time spent in the major runtime phases."""
    return {
        "enumerate": 0.0,
        "db_write": 0.0,
        "probe": 0.0,
        "fingerprint": 0.0,
        "matching": 0.0,
    }


def _load_failed_path_keys(db: object, scan_id: int) -> set[str]:
    """Load explicit failed-file path keys when the DB supports that API."""
    failed_file_path_keys = getattr(db, "failed_file_path_keys", None)
    if not callable(failed_file_path_keys) or scan_id <= 0:
        return set()
    getter = cast("_FailedFilePathKeysFn", failed_file_path_keys)
    return set(getter(scan_id))


def create_context(
    db: Database,
    roots: list[str],
    extensions: list[str],
    scan_size_mib_min: int,
    scan_size_mib_max: int,
    profile: str,
    probe_backend: ProbeBackendId,
    max_workers: int,
    drive_worker_overrides: dict[str, int] | None,
    probe_worker_mode: str,
    *,
    db_batch_size: int,
    db_flush_interval_ms: int,
    enum_queue_max: int,
    progress_emit_interval_ms: int,
    progress_emit_every_files: int,
    cancel_event: Event | None,
    pause_event: Event | None,
    progress_cb: Callable[[ScanProgress], None] | None,
    issue_cb: Callable[[ScanIssue], None] | None,
    analyze_file: Callable[[str, VideoMeta | None], AnalyzeOutputLike],
    enumerate_video_files_fn: _EnumerateFn,
    build_scan_plan_fn: _ScanPlanFn,
    find_duplicate_edges_fn: _FindEdgesFn,
    build_duplicate_groups_fn: _BuildGroupsFn,
    resume_scan_id: int | None = None,
    retry_failed_files: bool = True,
) -> ScanContext:
    """Create the mutable runtime context used by the streaming pipeline."""
    runtime_settings = _build_runtime_settings(
        max_workers,
        probe_worker_mode,
        db_batch_size=db_batch_size,
        db_flush_interval_ms=db_flush_interval_ms,
        enum_queue_max=enum_queue_max,
        progress_emit_interval_ms=progress_emit_interval_ms,
        progress_emit_every_files=progress_emit_every_files,
    )
    scan_id = int(resume_scan_id or 0)
    if scan_id > 0:
        db.update_scan_definition(
            scan_id,
            profile=profile,
            roots=roots,
            extensions=extensions,
            probe_backend=probe_backend,
            status="running",
        )
    else:
        scan_id = db.create_scan(
            profile=profile,
            roots=roots,
            extensions=extensions,
            probe_backend=probe_backend,
        )
    db.delete_scan_links_for_scan(scan_id)
    scan_plan = build_scan_plan_fn(
        roots=roots,
        max_workers=runtime_settings.requested_floor,
        drive_worker_overrides=drive_worker_overrides,
    )
    effective_worker_limit = max(0, int(scan_plan.effective_total_workers))
    executor_worker_limit = max(1, effective_worker_limit)
    lane_runtime_caps = _build_lane_runtime_caps(
        scan_plan,
        runtime_settings.probe_worker_mode,
    )
    lane_states, lane_queues, lane_pending_roots = _build_lane_runtime_state(scan_plan)
    started_at = time.perf_counter()
    return ScanContext(
        db=db,
        roots=roots,
        extensions=extensions,
        scan_size_mib_min=max(0, int(scan_size_mib_min)),
        scan_size_mib_max=max(0, int(scan_size_mib_max)),
        profile=profile,
        probe_backend=probe_backend,
        drive_worker_overrides=drive_worker_overrides,
        cancel_event=cancel_event,
        pause_event=pause_event,
        stop_event=Event(),
        progress_cb=progress_cb,
        issue_cb=issue_cb,
        analyze_file=analyze_file,
        enumerate_video_files_fn=enumerate_video_files_fn,
        build_scan_plan_fn=build_scan_plan_fn,
        find_duplicate_edges_fn=find_duplicate_edges_fn,
        build_duplicate_groups_fn=build_duplicate_groups_fn,
        db_batch_size=runtime_settings.db_batch_size,
        db_flush_interval_s=runtime_settings.db_flush_interval_s,
        progress_emit_interval_s=runtime_settings.progress_emit_interval_s,
        progress_emit_every_files=runtime_settings.progress_emit_every_files,
        scan_id=scan_id,
        resume_scan_id=resume_scan_id,
        retry_failed_files=bool(retry_failed_files),
        issues=[],
        requested_floor=runtime_settings.requested_floor,
        scan_plan=scan_plan,
        effective_worker_limit=effective_worker_limit,
        executor_worker_limit=executor_worker_limit,
        lane_runtime_caps=lane_runtime_caps,
        lane_states=lane_states,
        lane_queues=lane_queues,
        root_to_lane=_build_root_to_lane(scan_plan),
        lane_pending_roots=lane_pending_roots,
        enumerated_root_keys=set(),
        dispatch_lane_cursor=0,
        active_by_lane=dict.fromkeys(lane_states, 0),
        futures={},
        done_futures=deque(),
        event_cond=Condition(),
        state_lock=Lock(),
        queued_issues=deque(),
        recorded_issue_keys=set(),
        present_paths=set(),
        streamed_path_keys=set(),
        failed_path_keys=_load_failed_path_keys(db, scan_id),
        pending_discovered=[],
        pending_scan_links=[],
        pending_meta_rows=[],
        pending_fp_rows=[],
        pending_fp_provenance_rows=[],
        pending_probe_error_rows=[],
        scan_started_at=started_at,
        stage_seconds=_initial_stage_seconds(),
        flush_count=0,
        flush_rows_total=0,
        rows_since_flush=0,
        max_queue_depth=0,
        last_tx_flush_at=started_at,
        last_pending_write_at=started_at,
        last_discovered_batch_at=started_at,
        cached_files=0,
        resume_cache_hits=0,
        resume_reprocessed_files=0,
        skipped_failed_files=0,
        fingerprint_only_files=0,
        probe_and_fingerprint_files=0,
        fingerprinted_files=0,
        prepared_files=0,
        discovered_files=0,
        discovered_bytes=0,
        analyzed_files=0,
        analyzed_bytes=0,
        total_analyze_files=0,
        active_workers=0,
        enumerated_roots=0,
        total_roots=max(1, len(roots)),
        enum_finished=False,
        cancel_requested=False,
        pause_requested=False,
        stop_applied=False,
        enum_files=[],
        enum_issues=[],
        enum_error=None,
        enum_queue=Queue[VideoRecord | ScanLinkRecord | object](
            maxsize=runtime_settings.enum_queue_max
        ),
        enum_sentinel=object(),
        last_emit_at=0.0,
        last_emit_stage="",
        last_emit_counter=0,
    )
