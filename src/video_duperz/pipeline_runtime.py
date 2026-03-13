from __future__ import annotations

import contextlib
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Condition, Event, Lock, Thread
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypeVar

from threep_commons.fs_paths import path_key

from .fingerprint import ALGO_VERSION, FingerprintError
from .matcher import build_duplicate_groups, find_duplicate_edges
from .models import (
    MatchStats,
    ProbeWorkerMode,
    ScanIssue,
    ScanLaneSnapshot,
    ScanProgress,
    ScanResult,
    VideoMeta,
    VideoRecord,
)
from .probe import ProbeError, ensure_ffprobe_available
from .scanner import build_physical_drive_scan_plan, enumerate_video_files

if TYPE_CHECKING:
    from .db import Database

_MIB = 1024.0 * 1024.0
ReturnT = TypeVar("ReturnT")
P = ParamSpec("P")
_ScanPlanFn = Callable[..., Any]
_EnumerateFn = Callable[..., tuple[list[VideoRecord], list[ScanIssue]]]
_FindEdgesFn = Callable[..., tuple[Any, MatchStats]]
_BuildGroupsFn = Callable[..., list[Any]]


class _AnalyzeOutputLike(Protocol):
    meta: VideoMeta
    hashes: list[int]
    probe_s: float
    fingerprint_s: float


@dataclass(slots=True)
class _AnalyzeTask:
    file: VideoRecord
    file_id: int
    cached_meta: VideoMeta | None
    lane: int
    source_root: str
    path: str
    size: int


@dataclass(slots=True)
class _ScanContext:
    db: Database
    roots: list[str]
    extensions: list[str]
    profile: str
    drive_worker_overrides: dict[str, int] | None
    cancel_event: Event | None
    progress_cb: Callable[[ScanProgress], None] | None
    analyze_file: Callable[[str, VideoMeta | None], _AnalyzeOutputLike]
    enumerate_video_files_fn: _EnumerateFn
    build_scan_plan_fn: _ScanPlanFn
    find_duplicate_edges_fn: _FindEdgesFn
    build_duplicate_groups_fn: _BuildGroupsFn
    db_batch_size: int
    db_flush_interval_s: float
    progress_emit_interval_s: float
    progress_emit_every_files: int
    scan_id: int
    issues: list[ScanIssue]
    requested_floor: int
    scan_plan: Any
    effective_worker_limit: int
    executor_worker_limit: int
    lane_runtime_caps: dict[int, int]
    lane_states: dict[int, ScanLaneSnapshot]
    lane_queues: dict[int, deque[_AnalyzeTask]]
    root_to_lane: dict[str, int]
    ready_lanes: deque[int]
    ready_set: set[int]
    active_by_lane: dict[int, int]
    futures: dict[Future[_AnalyzeOutputLike], _AnalyzeTask]
    done_futures: deque[Future[_AnalyzeOutputLike]]
    event_cond: Condition
    state_lock: Lock
    present_paths: set[str]
    streamed_path_keys: set[str]
    pending_discovered: list[VideoRecord]
    pending_meta_rows: list[tuple[int, VideoMeta]]
    pending_fp_rows: list[tuple[int, int, list[int]]]
    pending_probe_error_rows: list[tuple[int, str]]
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
    cancel_applied: bool
    enum_files: list[VideoRecord]
    enum_issues: list[ScanIssue]
    enum_error: Exception | None
    enum_queue: Queue[VideoRecord | object]
    enum_sentinel: object
    last_emit_at: float
    last_emit_stage: str
    last_emit_counter: int
    enum_thread: Thread | None = None
    cancel_thread: Thread | None = None


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _normalize_probe_worker_mode(value: str) -> ProbeWorkerMode:
    mode = str(value or "").strip().lower()
    if mode == "burst":
        return "burst"
    return "balanced"


def _should_emit_progress(
    *,
    stage: str,
    counter: int,
    force: bool,
    last_emit_stage: str,
    last_emit_counter: int,
    last_emit_at: float,
    progress_emit_every_files: int,
    progress_emit_interval_s: float,
) -> bool:
    if force or stage != last_emit_stage:
        return True
    if counter - last_emit_counter >= progress_emit_every_files:
        return True
    return (time.perf_counter() - last_emit_at) >= progress_emit_interval_s


def _create_context(
    db: Database,
    roots: list[str],
    extensions: list[str],
    profile: str,
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
    progress_cb: Callable[[ScanProgress], None] | None,
    analyze_file: Callable[[str, VideoMeta | None], _AnalyzeOutputLike],
    enumerate_video_files_fn: _EnumerateFn,
    build_scan_plan_fn: _ScanPlanFn,
    find_duplicate_edges_fn: _FindEdgesFn,
    build_duplicate_groups_fn: _BuildGroupsFn,
) -> _ScanContext:
    db_batch_size = _clamp(db_batch_size, 32, 4096)
    db_flush_interval_ms = _clamp(db_flush_interval_ms, 50, 2000)
    queue_max = _clamp(enum_queue_max, 256, 32768)
    progress_emit_interval_ms = _clamp(progress_emit_interval_ms, 50, 2000)
    progress_emit_every_files = _clamp(progress_emit_every_files, 10, 5000)
    db_flush_interval_s = float(db_flush_interval_ms) / 1000.0
    progress_emit_interval_s = float(progress_emit_interval_ms) / 1000.0

    scan_id = db.create_scan(profile=profile, roots=roots, extensions=extensions)
    requested_floor = max(1, int(max_workers))
    scan_plan = build_scan_plan_fn(
        roots=roots,
        max_workers=requested_floor,
        drive_worker_overrides=drive_worker_overrides,
    )
    configured_lane_limits = {
        int(lane): max(1, int(limit))
        for lane, limit in scan_plan.lane_worker_limits.items()
    }
    effective_worker_limit = max(0, int(scan_plan.effective_total_workers))
    executor_worker_limit = max(1, effective_worker_limit)
    mode = _normalize_probe_worker_mode(probe_worker_mode)
    lane_runtime_caps = {
        lane: (1 if mode == "balanced" else max(1, configured_lane_limits.get(lane, 1)))
        for lane in range(len(scan_plan.root_groups))
    }
    issues = list(scan_plan.issues)
    if scan_plan.requested_worker_target > scan_plan.effective_total_workers:
        issues.append(
            ScanIssue(
                stage="probe",
                path="",
                message=(
                    "Requested worker capacity "
                    f"({scan_plan.requested_worker_target}) reduced to "
                    f"{scan_plan.effective_total_workers} by per-drive hard caps."
                ),
            )
        )

    lane_states: dict[int, ScanLaneSnapshot] = {}
    lane_queues: dict[int, deque[_AnalyzeTask]] = {}
    for lane_idx, group in enumerate(scan_plan.root_groups):
        lane_states[lane_idx] = ScanLaneSnapshot(
            lane=lane_idx,
            roots=[str(Path(root)) for root in group],
            state="pending",
        )
        lane_queues[lane_idx] = deque()

    started_at = time.perf_counter()
    return _ScanContext(
        db=db,
        roots=roots,
        extensions=extensions,
        profile=profile,
        drive_worker_overrides=drive_worker_overrides,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        analyze_file=analyze_file,
        enumerate_video_files_fn=enumerate_video_files_fn,
        build_scan_plan_fn=build_scan_plan_fn,
        find_duplicate_edges_fn=find_duplicate_edges_fn,
        build_duplicate_groups_fn=build_duplicate_groups_fn,
        db_batch_size=db_batch_size,
        db_flush_interval_s=db_flush_interval_s,
        progress_emit_interval_s=progress_emit_interval_s,
        progress_emit_every_files=progress_emit_every_files,
        scan_id=scan_id,
        issues=issues,
        requested_floor=requested_floor,
        scan_plan=scan_plan,
        effective_worker_limit=effective_worker_limit,
        executor_worker_limit=executor_worker_limit,
        lane_runtime_caps=lane_runtime_caps,
        lane_states=lane_states,
        lane_queues=lane_queues,
        root_to_lane={
            path_key(root): lane for root, lane in scan_plan.root_to_group_index.items()
        },
        ready_lanes=deque(),
        ready_set=set(),
        active_by_lane=dict.fromkeys(lane_states, 0),
        futures={},
        done_futures=deque(),
        event_cond=Condition(),
        state_lock=Lock(),
        present_paths=set(),
        streamed_path_keys=set(),
        pending_discovered=[],
        pending_meta_rows=[],
        pending_fp_rows=[],
        pending_probe_error_rows=[],
        scan_started_at=started_at,
        stage_seconds={
            "enumerate": 0.0,
            "db_write": 0.0,
            "probe": 0.0,
            "fingerprint": 0.0,
            "matching": 0.0,
        },
        flush_count=0,
        flush_rows_total=0,
        rows_since_flush=0,
        max_queue_depth=0,
        last_tx_flush_at=started_at,
        last_pending_write_at=started_at,
        last_discovered_batch_at=started_at,
        cached_files=0,
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
        cancel_applied=False,
        enum_files=[],
        enum_issues=[],
        enum_error=None,
        enum_queue=Queue(maxsize=queue_max),
        enum_sentinel=object(),
        last_emit_at=0.0,
        last_emit_stage="",
        last_emit_counter=0,
    )


def _notify_event(ctx: _ScanContext) -> None:
    with ctx.event_cond:
        ctx.event_cond.notify_all()


def _effective_worker_limit_locked(ctx: _ScanContext) -> int:
    return max(0, int(ctx.effective_worker_limit))


def _lane_runtime_cap_locked(ctx: _ScanContext, lane: int) -> int:
    return max(1, int(ctx.lane_runtime_caps.get(lane, 1)))


def _rate(value: int, elapsed_s: float) -> float:
    if elapsed_s <= 0.0:
        return 0.0
    return float(value) / elapsed_s


def _mib_per_s(byte_count: int, elapsed_s: float) -> float:
    if elapsed_s <= 0.0:
        return 0.0
    return float(byte_count) / _MIB / elapsed_s


def _clone_lane_snapshots_locked(
    ctx: _ScanContext,
    elapsed_s: float,
) -> list[ScanLaneSnapshot]:
    snapshots: list[ScanLaneSnapshot] = []
    for lane_id in sorted(ctx.lane_states):
        lane = ctx.lane_states[lane_id]
        snapshots.append(
            ScanLaneSnapshot(
                lane=lane.lane,
                roots=list(lane.roots),
                state=lane.state,
                discovered=lane.discovered,
                discovered_bytes=lane.discovered_bytes,
                queued=lane.queued,
                analyzed=lane.analyzed,
                analyzed_bytes=lane.analyzed_bytes,
                completed=lane.completed,
                discovered_files_per_s=_rate(lane.discovered, elapsed_s),
                discovered_mib_per_s=_mib_per_s(lane.discovered_bytes, elapsed_s),
                analyzed_files_per_s=_rate(lane.analyzed, elapsed_s),
                analyzed_mib_per_s=_mib_per_s(lane.analyzed_bytes, elapsed_s),
                active_file=lane.active_file,
                workers=lane.workers,
            )
        )
    return snapshots


def _emit_progress(
    ctx: _ScanContext,
    stage: str,
    current: int,
    total: int,
    message: str,
    *,
    force: bool = False,
    file_counter: int | None = None,
) -> None:
    elapsed_s = max(0.0, time.perf_counter() - ctx.scan_started_at)
    with ctx.state_lock:
        snapshots = _clone_lane_snapshots_locked(ctx, elapsed_s)
        counter = int(
            file_counter
            if file_counter is not None
            else max(ctx.prepared_files, ctx.analyzed_files)
        )
        if not _should_emit_progress(
            stage=stage,
            counter=counter,
            force=force,
            last_emit_stage=ctx.last_emit_stage,
            last_emit_counter=ctx.last_emit_counter,
            last_emit_at=ctx.last_emit_at,
            progress_emit_every_files=ctx.progress_emit_every_files,
            progress_emit_interval_s=ctx.progress_emit_interval_s,
        ):
            return
        ctx.last_emit_at = time.perf_counter()
        ctx.last_emit_stage = stage
        ctx.last_emit_counter = counter
        cache_ratio = (
            float(ctx.cached_files) / float(ctx.prepared_files)
            if ctx.prepared_files > 0
            else 0.0
        )
        if ctx.progress_cb is None:
            return
        ctx.progress_cb(
            ScanProgress(
                stage=stage,
                current=current,
                total=total,
                message=message,
                active_workers=ctx.active_workers,
                worker_limit=_effective_worker_limit_locked(ctx),
                enumerated_roots=ctx.enumerated_roots,
                total_roots=ctx.total_roots,
                prepared_files=ctx.prepared_files,
                discovered_files=ctx.discovered_files,
                discovered_bytes=ctx.discovered_bytes,
                analyzed_files=ctx.analyzed_files,
                analyzed_bytes=ctx.analyzed_bytes,
                cached_files=ctx.cached_files,
                discovered_files_per_s=_rate(ctx.discovered_files, elapsed_s),
                discovered_mib_per_s=_mib_per_s(ctx.discovered_bytes, elapsed_s),
                analyzed_files_per_s=_rate(ctx.analyzed_files, elapsed_s),
                analyzed_mib_per_s=_mib_per_s(ctx.analyzed_bytes, elapsed_s),
                cache_hit_ratio=cache_ratio,
                elapsed_s=elapsed_s,
                total_analyze_files=ctx.total_analyze_files,
                lane_snapshots=snapshots,
            )
        )


def _ensure_lane_state_locked(
    ctx: _ScanContext,
    lane: int,
    source_root: str = "",
) -> ScanLaneSnapshot:
    state = ctx.lane_states.get(lane)
    if state is None:
        roots_for_lane = [source_root] if source_root else []
        state = ScanLaneSnapshot(lane=lane, roots=roots_for_lane, state="pending")
        ctx.lane_states[lane] = state
    elif source_root and source_root not in state.roots:
        state.roots.append(source_root)
    ctx.lane_queues.setdefault(lane, deque())
    return state


def _refresh_lane_state_locked(ctx: _ScanContext, lane: int) -> None:
    state = _ensure_lane_state_locked(ctx, lane)
    queue_size = len(ctx.lane_queues.get(lane, ()))
    lane_active_count = int(ctx.active_by_lane.get(lane, 0))
    state.workers = lane_active_count
    if lane_active_count > 0:
        state.state = "running"
    elif queue_size > 0:
        state.state = "queued"
    elif ctx.enum_finished and state.completed >= state.discovered:
        state.state = "done"
    elif state.discovered > 0:
        state.state = "idle"


def _queue_lane_if_ready_locked(ctx: _ScanContext, lane: int) -> None:
    queue_size = len(ctx.lane_queues.get(lane, ()))
    if queue_size <= 0:
        return
    if int(ctx.active_by_lane.get(lane, 0)) >= _lane_runtime_cap_locked(ctx, lane):
        return
    if lane in ctx.ready_set:
        return
    ctx.ready_lanes.append(lane)
    ctx.ready_set.add(lane)


def _prepare_total_locked(ctx: _ScanContext) -> int:
    if ctx.enum_finished and ctx.enum_files:
        return len(ctx.enum_files)
    return max(1, ctx.prepared_files)


def _record_db_write(ctx: _ScanContext, elapsed_s: float, row_count: int) -> None:
    with ctx.state_lock:
        ctx.stage_seconds["db_write"] += max(0.0, float(elapsed_s))
    ctx.rows_since_flush += max(0, int(row_count))


def _timed_db_write[**P, ReturnT](
    ctx: _ScanContext,
    write_fn: Callable[P, ReturnT],
    row_count: int,
    *args: P.args,
    **kwargs: P.kwargs,
) -> ReturnT:
    started = time.perf_counter()
    out = write_fn(*args, **kwargs)
    _record_db_write(ctx, time.perf_counter() - started, row_count)
    return out


def _flush_scan_transaction(ctx: _ScanContext, *, force: bool = False) -> None:
    if ctx.rows_since_flush <= 0:
        return
    now = time.perf_counter()
    if (
        not force
        and ctx.rows_since_flush < ctx.db_batch_size
        and (now - ctx.last_tx_flush_at) < ctx.db_flush_interval_s
    ):
        return
    started = time.perf_counter()
    ctx.db.flush_scan_transaction()
    _record_db_write(ctx, time.perf_counter() - started, 0)
    ctx.flush_count += 1
    ctx.flush_rows_total += ctx.rows_since_flush
    ctx.rows_since_flush = 0
    ctx.last_tx_flush_at = now


def _flush_pending_analysis_batches(
    ctx: _ScanContext,
    *,
    force: bool = False,
) -> None:
    pending_total = (
        len(ctx.pending_meta_rows)
        + len(ctx.pending_fp_rows)
        + len(ctx.pending_probe_error_rows)
    )
    if pending_total <= 0:
        return
    now = time.perf_counter()
    if (
        not force
        and pending_total < ctx.db_batch_size
        and (now - ctx.last_pending_write_at) < ctx.db_flush_interval_s
    ):
        return
    while ctx.pending_meta_rows:
        chunk = ctx.pending_meta_rows[: ctx.db_batch_size]
        del ctx.pending_meta_rows[: len(chunk)]
        _timed_db_write(ctx, ctx.db.save_video_meta_batch, len(chunk), chunk)
    while ctx.pending_fp_rows:
        chunk = ctx.pending_fp_rows[: ctx.db_batch_size]
        del ctx.pending_fp_rows[: len(chunk)]
        _timed_db_write(ctx, ctx.db.save_fingerprints_batch, len(chunk), chunk)
    while ctx.pending_probe_error_rows:
        chunk = ctx.pending_probe_error_rows[: ctx.db_batch_size]
        del ctx.pending_probe_error_rows[: len(chunk)]
        _timed_db_write(ctx, ctx.db.save_probe_errors_batch, len(chunk), chunk)
    ctx.last_pending_write_at = now


def _queue_enum_item(ctx: _ScanContext, item: object) -> None:
    while True:
        try:
            ctx.enum_queue.put(item, timeout=0.1)
            break
        except Full:
            if (
                ctx.cancel_event
                and ctx.cancel_event.is_set()
                and item is not ctx.enum_sentinel
            ):
                continue
    with ctx.state_lock:
        ctx.max_queue_depth = max(ctx.max_queue_depth, int(ctx.enum_queue.qsize()))
    _notify_event(ctx)


def _on_file_discovered(ctx: _ScanContext, file: object) -> None:
    key = path_key(str(getattr(file, "path", "")))
    with ctx.state_lock:
        if key in ctx.streamed_path_keys:
            return
        ctx.streamed_path_keys.add(key)
    _queue_enum_item(ctx, file)


def _on_enumerate_progress(
    ctx: _ScanContext,
    current: int,
    total: int,
    message: str,
) -> None:
    root_from_message = ""
    marker = " [workers "
    if message.startswith("Enumerated ") and marker in message:
        root_from_message = message[len("Enumerated ") : message.rfind(marker)]
    with ctx.state_lock:
        ctx.enumerated_roots = current
        ctx.total_roots = max(1, total)
        if root_from_message:
            lane = ctx.root_to_lane.get(path_key(root_from_message))
            if lane is not None:
                lane_state = _ensure_lane_state_locked(ctx, lane, root_from_message)
                if lane_state.state == "pending":
                    lane_state.state = "idle"
        discovered_now = ctx.discovered_files
    _emit_progress(
        ctx,
        "enumerate",
        current,
        total,
        message,
        file_counter=discovered_now,
    )


def _handle_enumerate_progress(
    ctx: _ScanContext,
    current: int,
    total: int,
    message: str,
) -> None:
    _on_enumerate_progress(ctx, current, total, message)


def _handle_file_discovered(ctx: _ScanContext, file: object) -> None:
    _on_file_discovered(ctx, file)


def _run_enumeration(ctx: _ScanContext) -> None:
    enumerate_started = time.perf_counter()
    try:
        files, local_issues = ctx.enumerate_video_files_fn(
            scan_id=ctx.scan_id,
            roots=ctx.roots,
            extensions=ctx.extensions,
            max_workers=max(ctx.requested_floor, len(ctx.scan_plan.root_groups)),
            drive_worker_overrides=ctx.drive_worker_overrides,
            cancel_event=ctx.cancel_event,
            progress_cb=partial(_handle_enumerate_progress, ctx),
            on_file_discovered=partial(_handle_file_discovered, ctx),
        )
        ctx.enum_files = list(files)
        ctx.enum_issues = list(local_issues)
        # Keep compatibility with tests that bypass the streaming callback.
        for file in ctx.enum_files:
            key = path_key(str(getattr(file, "path", "")))
            with ctx.state_lock:
                if key in ctx.streamed_path_keys:
                    continue
                ctx.streamed_path_keys.add(key)
            _queue_enum_item(ctx, file)
    except Exception as exc:
        ctx.enum_error = exc
    finally:
        elapsed = max(0.0, time.perf_counter() - enumerate_started)
        with ctx.state_lock:
            ctx.stage_seconds["enumerate"] += elapsed
            ctx.enum_finished = True
            for lane_id in list(ctx.lane_states):
                _refresh_lane_state_locked(ctx, lane_id)
        _queue_enum_item(ctx, ctx.enum_sentinel)


def _drain_enum_queue(ctx: _ScanContext) -> list[VideoRecord | object]:
    drained: list[VideoRecord | object] = []
    while True:
        try:
            drained.append(ctx.enum_queue.get_nowait())
        except Empty:
            break
    with ctx.state_lock:
        ctx.max_queue_depth = max(ctx.max_queue_depth, int(ctx.enum_queue.qsize()))
    return drained


def _process_discovered_batch(ctx: _ScanContext, batch: list[VideoRecord]) -> None:
    if not batch:
        return
    upsert_payload: list[dict[str, object]] = []
    cache_payload: list[dict[str, object]] = []
    valid: list[tuple[VideoRecord, int, str, int, str]] = []
    for file in batch:
        lane = int(getattr(file, "parallel_lane", 0))
        source_root = str(getattr(file, "source_root", ""))
        path = str(getattr(file, "path", ""))
        if not path:
            continue
        file_size = max(0, int(getattr(file, "size", 0)))
        with ctx.state_lock:
            lane_state = _ensure_lane_state_locked(ctx, lane, source_root)
            lane_state.discovered += 1
            lane_state.discovered_bytes += file_size
            if lane_state.state in {"pending", "enumerating"}:
                lane_state.state = "idle"
            ctx.discovered_files += 1
            ctx.discovered_bytes += file_size
            ctx.prepared_files += 1
            prepared_now = ctx.prepared_files
            prepare_total = _prepare_total_locked(ctx)
        ctx.present_paths.add(path)
        upsert_payload.append(
            {
                "path": path,
                "size": file_size,
                "mtime_ns": int(getattr(file, "mtime_ns", 0)),
                "ctime_ns": int(getattr(file, "ctime_ns", 0)),
                "ext": str(getattr(file, "ext", "")),
                "scan_id": ctx.scan_id,
            }
        )
        cache_payload.append(
            {
                "path": path,
                "size": file_size,
                "mtime_ns": int(getattr(file, "mtime_ns", 0)),
            }
        )
        valid.append((file, lane, source_root, file_size, path))
        _emit_progress(
            ctx,
            "prepare",
            prepared_now,
            max(1, prepare_total),
            f"Prepared {path}",
            file_counter=prepared_now,
        )
    if not valid:
        return

    by_path = _timed_db_write(
        ctx,
        ctx.db.upsert_files_batch,
        len(valid),
        upsert_payload,
    )
    cache_by_path = ctx.db.load_cached_artifacts_batch(cache_payload)
    for file, lane, source_root, file_size, path in valid:
        file_id = int(by_path.get(path, 0))
        if file_id <= 0:
            continue
        file.file_id = file_id
        cache = cache_by_path.get(path)
        if cache and "meta" in cache and "fingerprint" in cache:
            fp = cache["fingerprint"]
            if int(fp.get("algo_version", -1)) == ALGO_VERSION:
                with ctx.state_lock:
                    ctx.cached_files += 1
                    lane_state = _ensure_lane_state_locked(ctx, lane, source_root)
                    lane_state.completed += 1
                    _refresh_lane_state_locked(ctx, lane)
                continue

        cached_meta = cache.get("meta") if cache else None
        task = _AnalyzeTask(
            file=file,
            file_id=file_id,
            cached_meta=cached_meta,
            lane=lane,
            source_root=source_root,
            path=path,
            size=file_size,
        )
        with ctx.state_lock:
            ctx.lane_queues.setdefault(lane, deque()).append(task)
            lane_state = _ensure_lane_state_locked(ctx, lane, source_root)
            lane_state.queued += 1
            ctx.total_analyze_files += 1
            _queue_lane_if_ready_locked(ctx, lane)
            _refresh_lane_state_locked(ctx, lane)
    ctx.last_discovered_batch_at = time.perf_counter()


def _on_future_done(ctx: _ScanContext, future: Future[_AnalyzeOutputLike]) -> None:
    with ctx.event_cond:
        ctx.done_futures.append(future)
        ctx.event_cond.notify_all()


def _submit_next_for_lane(
    ctx: _ScanContext,
    executor: ThreadPoolExecutor,
    lane: int,
) -> bool:
    with ctx.state_lock:
        lane_queue = ctx.lane_queues.get(lane)
        if not lane_queue:
            return False
        lane_active_count = int(ctx.active_by_lane.get(lane, 0))
        if lane_active_count >= _lane_runtime_cap_locked(ctx, lane):
            return False
        task = lane_queue.popleft()
        lane_state = _ensure_lane_state_locked(ctx, lane, task.source_root)
        lane_state.queued = max(0, lane_state.queued - 1)
        lane_state.active_file = task.path
        ctx.active_by_lane[lane] = lane_active_count + 1
        ctx.active_workers += 1
        _queue_lane_if_ready_locked(ctx, lane)
        _refresh_lane_state_locked(ctx, lane)
    future = executor.submit(ctx.analyze_file, task.path, task.cached_meta)
    future.add_done_callback(lambda done: _on_future_done(ctx, done))
    with ctx.state_lock:
        ctx.futures[future] = task
    return True


def _submit_ready_lanes(ctx: _ScanContext, executor: ThreadPoolExecutor) -> int:
    submitted = 0
    while True:
        with ctx.state_lock:
            if (
                len(ctx.futures) >= _effective_worker_limit_locked(ctx)
                or not ctx.ready_lanes
            ):
                return submitted
            lane = ctx.ready_lanes.popleft()
            ctx.ready_set.discard(lane)
        if _submit_next_for_lane(ctx, executor, lane):
            submitted += 1


def _record_future_error(
    ctx: _ScanContext,
    task: _AnalyzeTask,
    exc: Exception,
) -> None:
    if isinstance(exc, ProbeError):
        ctx.pending_probe_error_rows.append((task.file_id, str(exc)))
        ctx.issues.append(ScanIssue(stage="probe", path=task.path, message=str(exc)))
        return
    if isinstance(exc, FingerprintError):
        ctx.issues.append(
            ScanIssue(stage="fingerprint", path=task.path, message=str(exc))
        )
        return
    ctx.issues.append(ScanIssue(stage="analyze", path=task.path, message=str(exc)))


def _record_future_success(
    ctx: _ScanContext,
    task: _AnalyzeTask,
    output: _AnalyzeOutputLike,
) -> None:
    if task.cached_meta is None:
        ctx.pending_meta_rows.append((task.file_id, output.meta))
    ctx.pending_fp_rows.append((task.file_id, ALGO_VERSION, output.hashes))
    ctx.fingerprinted_files += 1
    with ctx.state_lock:
        ctx.stage_seconds["probe"] += max(0.0, float(output.probe_s))
        ctx.stage_seconds["fingerprint"] += max(0.0, float(output.fingerprint_s))


def _finalize_task(ctx: _ScanContext, task: _AnalyzeTask) -> tuple[int, int]:
    with ctx.state_lock:
        ctx.analyzed_files += 1
        ctx.analyzed_bytes += task.size
        ctx.active_workers = max(0, ctx.active_workers - 1)
        lane_workers = max(0, int(ctx.active_by_lane.get(task.lane, 0)) - 1)
        ctx.active_by_lane[task.lane] = lane_workers
        lane_state = _ensure_lane_state_locked(ctx, task.lane, task.source_root)
        lane_state.analyzed += 1
        lane_state.analyzed_bytes += task.size
        lane_state.completed += 1
        lane_state.active_file = ""
        _queue_lane_if_ready_locked(ctx, task.lane)
        _refresh_lane_state_locked(ctx, task.lane)
        return ctx.analyzed_files, max(1, ctx.total_analyze_files)


def _process_done_futures(ctx: _ScanContext) -> int:
    processed = 0
    while True:
        with ctx.event_cond:
            if not ctx.done_futures:
                return processed
            future = ctx.done_futures.popleft()
        with ctx.state_lock:
            task = ctx.futures.pop(future, None)
        if task is None:
            continue
        processed += 1
        try:
            output = future.result()
        except Exception as exc:
            _record_future_error(ctx, task, exc)
        else:
            _record_future_success(ctx, task, output)
        probe_done, probe_total = _finalize_task(ctx, task)
        _emit_progress(
            ctx,
            "probe",
            probe_done,
            probe_total,
            f"Analyzed {task.path}",
            file_counter=probe_done,
        )


def _apply_cancel_state(ctx: _ScanContext) -> None:
    with ctx.state_lock:
        ctx.ready_lanes.clear()
        ctx.ready_set.clear()
        for lane_id, queue in ctx.lane_queues.items():
            queue.clear()
            _refresh_lane_state_locked(ctx, lane_id)


def _collect_metrics(ctx: _ScanContext, match_stats: MatchStats) -> dict[str, object]:
    avg_rows_per_flush = (
        float(ctx.flush_rows_total) / float(ctx.flush_count)
        if ctx.flush_count > 0
        else 0.0
    )
    return {
        "stage_seconds": {
            name: round(value, 6) for name, value in ctx.stage_seconds.items()
        },
        "matching": asdict(match_stats),
        "flush_count": int(ctx.flush_count),
        "avg_rows_per_flush": float(avg_rows_per_flush),
        "max_queue_depth": int(ctx.max_queue_depth),
    }


def _best_effort_end_scan_tx(ctx: _ScanContext) -> None:
    with contextlib.suppress(Exception):
        _flush_pending_analysis_batches(ctx, force=True)
    with contextlib.suppress(Exception):
        _flush_scan_transaction(ctx, force=True)
    with contextlib.suppress(Exception):
        ctx.db.end_scan_transaction()


def _ingest_discovered_queue(ctx: _ScanContext) -> bool:
    drained = _drain_enum_queue(ctx)
    if not drained:
        return False
    for queued in drained:
        if queued is ctx.enum_sentinel or not isinstance(queued, VideoRecord):
            continue
        if ctx.cancel_requested:
            continue
        ctx.pending_discovered.append(queued)
    return True


def _process_pending_discovered(ctx: _ScanContext) -> bool:
    if ctx.cancel_requested:
        if ctx.pending_discovered:
            ctx.pending_discovered.clear()
            return True
        return False
    if not ctx.pending_discovered:
        return False
    now = time.perf_counter()
    should_process = (
        len(ctx.pending_discovered) >= ctx.db_batch_size
        or (now - ctx.last_discovered_batch_at) >= ctx.db_flush_interval_s
        or ctx.enum_finished
        or not ctx.futures
    )
    if not should_process:
        return False
    processed_any = False
    while ctx.pending_discovered and (
        len(ctx.pending_discovered) >= ctx.db_batch_size
        or ctx.enum_finished
        or ctx.cancel_requested
    ):
        chunk = ctx.pending_discovered[: ctx.db_batch_size]
        del ctx.pending_discovered[: len(chunk)]
        _process_discovered_batch(ctx, chunk)
        processed_any = True
    if ctx.pending_discovered and (
        not ctx.futures
        or (time.perf_counter() - ctx.last_discovered_batch_at)
        >= ctx.db_flush_interval_s
    ):
        chunk = ctx.pending_discovered[: ctx.db_batch_size]
        del ctx.pending_discovered[: len(chunk)]
        _process_discovered_batch(ctx, chunk)
        processed_any = True
    return processed_any


def _pipeline_should_stop(ctx: _ScanContext) -> bool:
    with ctx.state_lock:
        waiting_items = any(bool(queue) for queue in ctx.lane_queues.values())
        active_count = len(ctx.futures)
    pending_writes = bool(
        ctx.pending_meta_rows or ctx.pending_fp_rows or ctx.pending_probe_error_rows
    )
    if ctx.cancel_requested and ctx.enum_finished and active_count == 0:
        _flush_pending_analysis_batches(ctx, force=True)
        _flush_scan_transaction(ctx, force=True)
        return True
    if (
        ctx.enum_finished
        and active_count == 0
        and not waiting_items
        and not ctx.pending_discovered
        and not pending_writes
        and ctx.enum_queue.empty()
    ):
        _flush_pending_analysis_batches(ctx, force=True)
        _flush_scan_transaction(ctx, force=True)
        return True
    return False


def _wait_for_pipeline_event(ctx: _ScanContext) -> None:
    pending_writes = bool(
        ctx.pending_discovered
        or ctx.pending_meta_rows
        or ctx.pending_fp_rows
        or ctx.pending_probe_error_rows
    )
    with ctx.event_cond:
        if not ctx.done_futures and ctx.enum_queue.empty():
            timeout = ctx.db_flush_interval_s if pending_writes else None
            ctx.event_cond.wait(timeout=timeout)


def _run_pipeline_loop(ctx: _ScanContext, executor: ThreadPoolExecutor) -> None:
    while True:
        if ctx.cancel_event and ctx.cancel_event.is_set():
            ctx.cancel_requested = True
        if ctx.cancel_requested and not ctx.cancel_applied:
            _apply_cancel_state(ctx)
            ctx.cancel_applied = True

        made_progress = _ingest_discovered_queue(ctx)
        if _process_pending_discovered(ctx):
            made_progress = True
        if _process_done_futures(ctx) > 0:
            made_progress = True
        if not ctx.cancel_requested and _submit_ready_lanes(ctx, executor) > 0:
            made_progress = True

        _flush_pending_analysis_batches(ctx, force=ctx.cancel_requested)
        _flush_scan_transaction(ctx, force=ctx.cancel_requested)

        if _pipeline_should_stop(ctx):
            return
        if not made_progress:
            _wait_for_pipeline_event(ctx)


def _join_enumeration_thread(ctx: _ScanContext) -> None:
    if ctx.enum_thread is None:
        return
    ctx.enum_thread.join(timeout=5.0)
    if ctx.enum_thread.is_alive():
        ctx.issues.append(
            ScanIssue(
                stage="enumerate",
                path="",
                message="Enumeration thread did not stop cleanly after timeout",
            )
        )


def _cancelled_result(ctx: _ScanContext, scanned_files: int) -> ScanResult:
    ctx.db.end_scan_transaction()
    ctx.db.complete_scan(ctx.scan_id, status="cancelled")
    _emit_progress(ctx, "done", 1, 1, "Scan cancelled", force=True)
    return ScanResult(
        scan_id=ctx.scan_id,
        groups=ctx.db.load_duplicate_groups(ctx.scan_id),
        issues=ctx.issues,
        scanned_files=scanned_files,
        cached_files=ctx.cached_files,
        fingerprinted_files=ctx.fingerprinted_files,
        metrics=_collect_metrics(ctx, MatchStats()),
    )


def _completed_result(ctx: _ScanContext, scanned_files: int) -> ScanResult:
    _timed_db_write(
        ctx,
        ctx.db.mark_missing_for_scan,
        len(ctx.present_paths),
        ctx.scan_id,
        ctx.present_paths,
    )
    _flush_scan_transaction(ctx, force=True)
    _emit_progress(ctx, "matching", 0, 1, "Matching duplicates", force=True)
    matching_started = time.perf_counter()
    items = ctx.db.list_match_items_for_scan(
        scan_id=ctx.scan_id,
        algo_version=ALGO_VERSION,
    )
    edges, match_stats = ctx.find_duplicate_edges_fn(items, profile=ctx.profile)
    groups = ctx.build_duplicate_groups_fn(
        items=items,
        edges=edges,
        profile=ctx.profile,
    )
    with ctx.state_lock:
        ctx.stage_seconds["matching"] += max(
            0.0,
            time.perf_counter() - matching_started,
        )

    _timed_db_write(ctx, ctx.db.clear_duplicate_groups, 0, ctx.scan_id)
    group_row_count = len(groups) + sum(len(group.items) for group in groups)
    _timed_db_write(
        ctx,
        ctx.db.insert_duplicate_groups_batch,
        group_row_count,
        ctx.scan_id,
        ctx.profile,
        groups,
    )
    _flush_scan_transaction(ctx, force=True)

    ctx.db.end_scan_transaction()
    ctx.db.complete_scan(ctx.scan_id, status="done")
    _emit_progress(ctx, "done", 1, 1, "Scan complete", force=True)
    return ScanResult(
        scan_id=ctx.scan_id,
        groups=ctx.db.load_duplicate_groups(ctx.scan_id),
        issues=ctx.issues,
        scanned_files=scanned_files,
        cached_files=ctx.cached_files,
        fingerprinted_files=ctx.fingerprinted_files,
        metrics=_collect_metrics(ctx, match_stats),
    )


def _start_runtime_threads(ctx: _ScanContext) -> None:
    ctx.db.begin_scan_transaction()
    ctx.enum_thread = Thread(
        target=lambda: _run_enumeration(ctx),
        name="video-duperz-enumeration",
        daemon=True,
    )
    ctx.enum_thread.start()
    if ctx.cancel_event is None:
        return
    cancel_event = ctx.cancel_event
    ctx.cancel_thread = Thread(
        target=lambda: (cancel_event.wait(), _notify_event(ctx)),
        daemon=True,
    )
    ctx.cancel_thread.start()


def _emit_worker_cap_warning(ctx: _ScanContext) -> None:
    if ctx.scan_plan.requested_worker_target <= ctx.scan_plan.effective_total_workers:
        return
    _emit_progress(
        ctx,
        "prepare",
        0,
        1,
        (
            "Requested worker capacity "
            f"({ctx.scan_plan.requested_worker_target}) reduced to "
            f"{ctx.scan_plan.effective_total_workers} by per-drive hard caps."
        ),
        force=True,
    )


def run_scan_runtime(
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
    cancel_event: Event | None = None,
    progress_cb: Callable[[ScanProgress], None] | None = None,
    analyze_file: Callable[[str, VideoMeta | None], _AnalyzeOutputLike],
    ensure_ffprobe_available_fn: Callable[[], object] = ensure_ffprobe_available,
    enumerate_video_files_fn: _EnumerateFn = enumerate_video_files,
    build_scan_plan_fn: _ScanPlanFn = build_physical_drive_scan_plan,
    find_duplicate_edges_fn: _FindEdgesFn = find_duplicate_edges,
    build_duplicate_groups_fn: _BuildGroupsFn = build_duplicate_groups,
) -> ScanResult:
    ensure_ffprobe_available_fn()
    ctx = _create_context(
        db,
        roots,
        extensions,
        profile,
        max_workers,
        drive_worker_overrides,
        probe_worker_mode,
        db_batch_size=db_batch_size,
        db_flush_interval_ms=db_flush_interval_ms,
        enum_queue_max=enum_queue_max,
        progress_emit_interval_ms=progress_emit_interval_ms,
        progress_emit_every_files=progress_emit_every_files,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        analyze_file=analyze_file,
        enumerate_video_files_fn=enumerate_video_files_fn,
        build_scan_plan_fn=build_scan_plan_fn,
        find_duplicate_edges_fn=find_duplicate_edges_fn,
        build_duplicate_groups_fn=build_duplicate_groups_fn,
    )
    _start_runtime_threads(ctx)
    _emit_worker_cap_warning(ctx)

    try:
        with ThreadPoolExecutor(max_workers=ctx.executor_worker_limit) as executor:
            _run_pipeline_loop(ctx, executor)
        _join_enumeration_thread(ctx)
        if ctx.enum_error and not ctx.cancel_requested:
            raise ctx.enum_error
        ctx.issues.extend(ctx.enum_issues)
        scanned_files = (
            len(ctx.enum_files) if ctx.enum_files else len(ctx.present_paths)
        )
        if ctx.cancel_requested:
            return _cancelled_result(ctx, scanned_files)
        return _completed_result(ctx, scanned_files)
    except Exception:
        _best_effort_end_scan_tx(ctx)
        raise
