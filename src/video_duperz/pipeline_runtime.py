"""Streaming scan runtime that coordinates enumeration, probing, and persistence."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from queue import Empty, Full
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from threep_commons.fs_paths import path_key

from .fingerprint import ALGO_VERSION, FingerprintError
from .matcher import build_duplicate_groups, find_duplicate_edges
from .models import (
    MatchStats,
    ProbeBackendId,
    ScanIssue,
    ScanProgress,
    ScanResult,
    VideoMeta,
    VideoRecord,
)
from .pipeline_runtime_context import (
    AnalyzeOutputLike as _AnalyzeOutputLike,
)
from .pipeline_runtime_context import (
    AnalyzeTask as _AnalyzeTask,
)
from .pipeline_runtime_context import (
    ScanContext as _ScanContext,
)
from .pipeline_runtime_context import (
    create_context as _create_context,
)
from .pipeline_runtime_context import (
    worker_cap_message as _worker_cap_message,
)
from .pipeline_runtime_control import (
    best_effort_end_scan_tx,
    cancelled_result,
    completed_result,
    emit_worker_cap_warning,
    join_enumeration_thread,
    paused_result,
    pipeline_should_stop,
    record_issue,
    start_runtime_threads,
    wait_for_pipeline_event,
)
from .pipeline_runtime_lanes import (
    apply_cancel_state,
    ensure_lane_state_locked,
    finalize_task,
    refresh_lane_state_locked,
    submit_ready_lanes,
)
from .pipeline_runtime_progress import emit_progress, notify_event
from .probe import ProbeError, ensure_ffprobe_available
from .scanner import build_physical_drive_scan_plan, enumerate_video_files

if TYPE_CHECKING:
    from threading import Event

    from .db import Database
    from .db_artifacts import CachedArtifacts as _CachedArtifacts

ReturnT = TypeVar("ReturnT")
P = ParamSpec("P")
_ScanPlanFn = Callable[..., Any]
_EnumerateFn = Callable[..., tuple[list[VideoRecord], list[ScanIssue]]]
_FindEdgesFn = Callable[..., tuple[Any, MatchStats]]
_BuildGroupsFn = Callable[..., list[Any]]


@dataclass(slots=True)
class _CacheReuseDecision:
    """Normalized cache decision for one discovered file."""

    cached_meta: VideoMeta | None
    skip_analysis: bool


def _decide_cached_analysis(cache: _CachedArtifacts | None) -> _CacheReuseDecision:
    """Return whether a file can fully reuse persisted analysis rows."""
    if cache is None:
        return _CacheReuseDecision(cached_meta=None, skip_analysis=False)
    cached_meta = cache.get("meta")
    cached_fp = cache.get("fingerprint")
    if (
        cached_meta is not None
        and cached_fp is not None
        and int(cached_fp["algo_version"]) == ALGO_VERSION
    ):
        return _CacheReuseDecision(cached_meta=cached_meta, skip_analysis=True)
    return _CacheReuseDecision(cached_meta=cached_meta, skip_analysis=False)


def _video_record_sort_key(record: VideoRecord) -> tuple[str, str]:
    """Return the canonical alpha-order sort key for one discovered record."""
    normalized_path = str(record.path)
    return (path_key(normalized_path), normalized_path)


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
        + len(ctx.pending_fp_provenance_rows)
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
        _timed_db_write(
            ctx,
            ctx.db.save_video_meta_batch,
            len(chunk),
            chunk,
            probe_backend=ctx.probe_backend,
        )
    while ctx.pending_fp_rows:
        chunk = ctx.pending_fp_rows[: ctx.db_batch_size]
        del ctx.pending_fp_rows[: len(chunk)]
        _timed_db_write(
            ctx,
            ctx.db.save_fingerprints_batch,
            len(chunk),
            chunk,
            probe_backend=ctx.probe_backend,
        )
    while ctx.pending_fp_provenance_rows:
        chunk = ctx.pending_fp_provenance_rows[: ctx.db_batch_size]
        del ctx.pending_fp_provenance_rows[: len(chunk)]
        _timed_db_write(
            ctx,
            ctx.db.save_fingerprint_provenance_batch,
            len(chunk),
            chunk,
            probe_backend=ctx.probe_backend,
        )
    while ctx.pending_probe_error_rows:
        chunk = ctx.pending_probe_error_rows[: ctx.db_batch_size]
        del ctx.pending_probe_error_rows[: len(chunk)]
        _timed_db_write(
            ctx,
            ctx.db.save_probe_errors_batch,
            len(chunk),
            chunk,
            probe_backend=ctx.probe_backend,
        )
    ctx.last_pending_write_at = now


def _queue_runtime_issue(ctx: _ScanContext, issue: ScanIssue) -> None:
    """Queue one issue from a non-runtime thread for serialized handling."""
    with ctx.state_lock:
        ctx.queued_issues.append(issue)
    notify_event(ctx)


def _queue_enum_item(ctx: _ScanContext, item: object) -> None:
    while True:
        try:
            ctx.enum_queue.put(item, timeout=0.1)
            break
        except Full:
            if ctx.stop_event.is_set() and item is not ctx.enum_sentinel:
                return
    with ctx.state_lock:
        ctx.max_queue_depth = max(ctx.max_queue_depth, int(ctx.enum_queue.qsize()))
    notify_event(ctx)


def _on_file_discovered(ctx: _ScanContext, file: object) -> None:
    _ = ctx, file


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
                lane_state = ensure_lane_state_locked(ctx, lane, root_from_message)
                if lane_state.state == "pending":
                    lane_state.state = "idle"
        discovered_now = ctx.discovered_files
    emit_progress(
        ctx,
        "enumerate",
        current,
        total,
        message,
        file_counter=discovered_now,
        subject_path=root_from_message,
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
            cancel_event=ctx.stop_event,
            progress_cb=partial(_handle_enumerate_progress, ctx),
            on_file_discovered=partial(_handle_file_discovered, ctx),
            issue_cb=partial(_queue_runtime_issue, ctx),
        )
        ctx.enum_files = sorted(files, key=_video_record_sort_key)
        for issue in local_issues:
            _queue_runtime_issue(ctx, issue)
        for file in ctx.enum_files:
            _queue_enum_item(ctx, file)
    except Exception as exc:
        ctx.enum_error = exc
    finally:
        elapsed = max(0.0, time.perf_counter() - enumerate_started)
        with ctx.state_lock:
            ctx.stage_seconds["enumerate"] += elapsed
            ctx.enum_finished = True
            for lane_id in list(ctx.lane_states):
                refresh_lane_state_locked(ctx, lane_id)
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
    lane_sorted_batch = sorted(
        batch,
        key=lambda record: (
            int(getattr(record, "parallel_lane", 0)),
            *_video_record_sort_key(record),
        ),
    )
    upsert_payload: list[dict[str, object]] = []
    cache_payload: list[dict[str, object]] = []
    valid: list[tuple[VideoRecord, int, str, int, str]] = []
    for file in lane_sorted_batch:
        lane = int(getattr(file, "parallel_lane", 0))
        source_root = str(getattr(file, "source_root", ""))
        path = str(getattr(file, "path", ""))
        if not path:
            continue
        file_size = max(0, int(getattr(file, "size", 0)))
        with ctx.state_lock:
            lane_state = ensure_lane_state_locked(ctx, lane, source_root)
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
        emit_progress(
            ctx,
            "prepare",
            prepared_now,
            max(1, prepare_total),
            f"Prepared {path}",
            file_counter=prepared_now,
            subject_path=path,
        )
    if not valid:
        return

    by_path = _timed_db_write(
        ctx,
        ctx.db.upsert_files_batch,
        len(valid),
        upsert_payload,
    )
    cache_by_path = ctx.db.load_cached_artifacts_batch(
        cache_payload,
        probe_backend=ctx.probe_backend,
    )
    valid.sort(
        key=lambda item: (item[1], *_video_record_sort_key(item[0])),
    )
    for file, lane, source_root, file_size, path in valid:
        file_id = int(by_path.get(path, 0))
        if file_id <= 0:
            continue
        file.file_id = file_id
        cache = cache_by_path.get(path)
        cache_decision = _decide_cached_analysis(cache)
        if cache_decision.skip_analysis:
            with ctx.state_lock:
                ctx.cached_files += 1
                if ctx.resume_scan_id is not None:
                    ctx.resume_cache_hits += 1
                lane_state = ensure_lane_state_locked(ctx, lane, source_root)
                lane_state.completed += 1
                refresh_lane_state_locked(ctx, lane)
            continue

        task = _AnalyzeTask(
            file=file,
            file_id=file_id,
            cached_meta=cache_decision.cached_meta,
            lane=lane,
            source_root=source_root,
            path=path,
            size=file_size,
            mtime_ns=int(getattr(file, "mtime_ns", 0)),
        )
        with ctx.state_lock:
            if ctx.resume_scan_id is not None:
                ctx.resume_reprocessed_files += 1
            ctx.lane_queues.setdefault(lane, deque()).append(task)
            lane_state = ensure_lane_state_locked(ctx, lane, source_root)
            lane_state.queued += 1
            ctx.total_analyze_files += 1
            refresh_lane_state_locked(ctx, lane)
    ctx.last_discovered_batch_at = time.perf_counter()


def _on_future_done(ctx: _ScanContext, future: Future[_AnalyzeOutputLike]) -> None:
    with ctx.event_cond:
        ctx.done_futures.append(future)
        ctx.event_cond.notify_all()


def _record_future_error(
    ctx: _ScanContext,
    task: _AnalyzeTask,
    exc: Exception,
) -> None:
    if isinstance(exc, ProbeError):
        ctx.pending_probe_error_rows.append(
            (task.file_id, task.size, task.mtime_ns, str(exc))
        )
        record_issue(ctx, ScanIssue(stage="probe", path=task.path, message=str(exc)))
        return
    if isinstance(exc, FingerprintError):
        record_issue(
            ctx,
            ScanIssue(stage="fingerprint", path=task.path, message=str(exc)),
        )
        return
    record_issue(ctx, ScanIssue(stage="analyze", path=task.path, message=str(exc)))


def _record_future_success(
    ctx: _ScanContext,
    task: _AnalyzeTask,
    output: _AnalyzeOutputLike,
) -> None:
    if task.cached_meta is None:
        ctx.pending_meta_rows.append(
            (task.file_id, task.size, task.mtime_ns, output.meta)
        )
    ctx.pending_fp_rows.append(
        (task.file_id, task.size, task.mtime_ns, ALGO_VERSION, output.hashes)
    )
    ctx.pending_fp_provenance_rows.append(
        (
            task.file_id,
            output.fingerprint_decoder_backend,
            output.fingerprint_provenance_json,
        )
    )
    ctx.fingerprinted_files += 1
    with ctx.state_lock:
        ctx.stage_seconds["probe"] += max(0.0, float(output.probe_s))
        ctx.stage_seconds["fingerprint"] += max(0.0, float(output.fingerprint_s))


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
        probe_done, probe_total = finalize_task(ctx, task)
        emit_progress(
            ctx,
            "probe",
            probe_done,
            probe_total,
            f"Analyzed {task.path}",
            file_counter=probe_done,
            subject_path=task.path,
        )


def _process_queued_issues(ctx: _ScanContext) -> int:
    """Persist queued issues from enumeration and other worker threads."""
    processed = 0
    while True:
        with ctx.state_lock:
            if not ctx.queued_issues:
                return processed
            issue = ctx.queued_issues.popleft()
        record_issue(ctx, issue)
        processed += 1


def _ingest_discovered_queue(ctx: _ScanContext) -> bool:
    drained = _drain_enum_queue(ctx)
    if not drained:
        return False
    stop_requested = ctx.cancel_requested or ctx.pause_requested
    for queued in drained:
        if queued is ctx.enum_sentinel or not isinstance(queued, VideoRecord):
            continue
        if stop_requested:
            continue
        ctx.pending_discovered.append(queued)
    ctx.pending_discovered.sort(key=_video_record_sort_key)
    return True


def _process_pending_discovered(ctx: _ScanContext) -> bool:
    if ctx.cancel_requested or ctx.pause_requested:
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
        or ctx.pause_requested
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


def _run_pipeline_loop(ctx: _ScanContext, executor: ThreadPoolExecutor) -> None:
    while True:
        if ctx.cancel_event and ctx.cancel_event.is_set():
            ctx.cancel_requested = True
        if ctx.pause_event and ctx.pause_event.is_set():
            ctx.pause_requested = True
        if (ctx.cancel_requested or ctx.pause_requested) and not ctx.stop_applied:
            apply_cancel_state(ctx)
            ctx.stop_applied = True

        made_progress = _ingest_discovered_queue(ctx)
        if _process_queued_issues(ctx) > 0:
            made_progress = True
        if _process_pending_discovered(ctx):
            made_progress = True
        if _process_done_futures(ctx) > 0:
            made_progress = True
        if (
            not ctx.cancel_requested
            and not ctx.pause_requested
            and submit_ready_lanes(
                ctx,
                executor,
                lambda done: _on_future_done(ctx, done),
            )
            > 0
        ):
            made_progress = True

        _flush_pending_analysis_batches(
            ctx,
            force=ctx.cancel_requested or ctx.pause_requested,
        )
        _flush_scan_transaction(
            ctx,
            force=ctx.cancel_requested or ctx.pause_requested,
        )

        if pipeline_should_stop(
            ctx,
            lambda: _flush_pending_analysis_batches(ctx, force=True),
            lambda: _flush_scan_transaction(ctx, force=True),
        ):
            return
        if not made_progress:
            wait_for_pipeline_event(ctx)


def run_scan_runtime(
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
    cancel_event: Event | None = None,
    pause_event: Event | None = None,
    progress_cb: Callable[[ScanProgress], None] | None = None,
    issue_cb: Callable[[ScanIssue], None] | None = None,
    analyze_file: Callable[[str, VideoMeta | None], _AnalyzeOutputLike],
    ensure_ffprobe_available_fn: Callable[[], object] = ensure_ffprobe_available,
    enumerate_video_files_fn: _EnumerateFn = enumerate_video_files,
    build_scan_plan_fn: _ScanPlanFn = build_physical_drive_scan_plan,
    find_duplicate_edges_fn: _FindEdgesFn = find_duplicate_edges,
    build_duplicate_groups_fn: _BuildGroupsFn = build_duplicate_groups,
    resume_scan_id: int | None = None,
) -> ScanResult:
    """Execute the scan runtime with injectable seams for tests and UI workflows."""
    ensure_ffprobe_available_fn()
    ctx = _create_context(
        db,
        roots,
        extensions,
        profile,
        probe_backend,
        max_workers,
        drive_worker_overrides,
        probe_worker_mode,
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
        enumerate_video_files_fn=enumerate_video_files_fn,
        build_scan_plan_fn=build_scan_plan_fn,
        find_duplicate_edges_fn=find_duplicate_edges_fn,
        build_duplicate_groups_fn=build_duplicate_groups_fn,
        resume_scan_id=resume_scan_id,
    )
    start_runtime_threads(ctx, lambda: _run_enumeration(ctx))
    for issue in ctx.scan_plan.issues:
        record_issue(ctx, issue)
    worker_cap_warning = _worker_cap_message(ctx.scan_plan)
    if worker_cap_warning is not None:
        record_issue(
            ctx,
            ScanIssue(stage="probe", path="", message=worker_cap_warning),
        )
    emit_worker_cap_warning(ctx, worker_cap_warning)

    try:
        with ThreadPoolExecutor(max_workers=ctx.executor_worker_limit) as executor:
            _run_pipeline_loop(ctx, executor)
        join_enumeration_thread(ctx)
        if ctx.enum_error and not ctx.cancel_requested and not ctx.pause_requested:
            raise ctx.enum_error
        scanned_files = (
            len(ctx.enum_files) if ctx.enum_files else len(ctx.present_paths)
        )
        if ctx.cancel_requested:
            return cancelled_result(ctx, scanned_files)
        if ctx.pause_requested:
            return paused_result(ctx, scanned_files)
        return completed_result(
            ctx,
            scanned_files,
            lambda write_fn, row_count, *args: _timed_db_write(
                ctx,
                write_fn,
                row_count,
                *args,
            ),
            lambda: _flush_scan_transaction(ctx, force=True),
        )
    except Exception:
        best_effort_end_scan_tx(
            lambda: _flush_pending_analysis_batches(ctx, force=True),
            lambda: _flush_scan_transaction(ctx, force=True),
            ctx.db.end_scan_transaction,
        )
        raise
