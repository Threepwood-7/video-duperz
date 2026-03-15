"""Control-loop and finalization helpers for the streaming scan runtime."""

from __future__ import annotations

import contextlib
import time
from dataclasses import asdict
from threading import Thread
from typing import TYPE_CHECKING, Protocol

from .fingerprint import ALGO_VERSION
from .models import MatchStats, ScanIssue, ScanResult
from .pipeline_runtime_progress import emit_progress, notify_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from .pipeline_runtime_context import ScanContext as _ScanContext


class _TimedDbWriteFn(Protocol):
    """Protocol for runtime DB write timing callbacks."""

    def __call__(
        self,
        write_fn: Callable[..., object],
        row_count: int,
        /,
        *args: object,
    ) -> object:
        """Invoke one DB write while recording timing and row counts."""


def collect_metrics(ctx: _ScanContext, match_stats: MatchStats) -> dict[str, object]:
    """Collect summary telemetry for the completed scan runtime.

    Args:
        ctx: Shared scan runtime context.
        match_stats: Duplicate-matching statistics for the finished scan.

    Returns:
        Serializable runtime metrics for the final scan result.
    """
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


def best_effort_end_scan_tx(
    flush_pending_batches: Callable[[], None],
    flush_scan_tx: Callable[[], None],
    end_scan_tx: Callable[[], None],
) -> None:
    """Best-effort cleanup for an exceptional runtime exit path.

    Args:
        flush_pending_batches: Callback that flushes staged analysis rows.
        flush_scan_tx: Callback that flushes the open scan transaction.
        end_scan_tx: Callback that ends the scan transaction.
    """
    with contextlib.suppress(Exception):
        flush_pending_batches()
    with contextlib.suppress(Exception):
        flush_scan_tx()
    with contextlib.suppress(Exception):
        end_scan_tx()


def pipeline_should_stop(
    ctx: _ScanContext,
    flush_pending_batches: Callable[[], None],
    flush_scan_tx: Callable[[], None],
) -> bool:
    """Return whether the runtime loop has fully drained.

    Args:
        ctx: Shared scan runtime context.
        flush_pending_batches: Callback that flushes staged analysis rows.
        flush_scan_tx: Callback that flushes the open scan transaction.

    Returns:
        ``True`` when the pipeline can terminate cleanly.
    """
    with ctx.state_lock:
        waiting_items = any(bool(queue) for queue in ctx.lane_queues.values())
        active_count = len(ctx.active_processes)
    pending_writes = bool(
        ctx.pending_meta_rows
        or ctx.pending_fp_rows
        or ctx.pending_probe_error_rows
        or ctx.pending_analysis_issue_rows
        or ctx.pending_analysis_issue_clear_ids
    )
    if ctx.cancel_requested and ctx.enum_finished and active_count == 0:
        flush_pending_batches()
        flush_scan_tx()
        return True
    if (
        ctx.enum_finished
        and active_count == 0
        and not waiting_items
        and not ctx.pending_discovered
        and not pending_writes
        and ctx.enum_queue.empty()
    ):
        flush_pending_batches()
        flush_scan_tx()
        return True
    return False


def wait_for_pipeline_event(ctx: _ScanContext) -> None:
    """Block until future completions or queue activity wakes the runtime loop.

    Args:
        ctx: Shared scan runtime context.
    """
    pending_writes = bool(
        ctx.pending_discovered
        or ctx.pending_meta_rows
        or ctx.pending_fp_rows
        or ctx.pending_probe_error_rows
        or ctx.pending_analysis_issue_rows
        or ctx.pending_analysis_issue_clear_ids
    )
    next_timeout_s: float | None = None
    active_process_count = 0
    with ctx.state_lock:
        active_process_count = len(ctx.active_processes)
        if ctx.active_processes:
            now = time.perf_counter()
            remaining_deadlines = [
                max(0.0, (active.started_at + float(ctx.analysis_timeout_s)) - now)
                for active in ctx.active_processes.values()
            ]
            remaining_deadlines.extend(
                max(
                    0.0,
                    (
                        float(active.terminate_requested_at)
                        + float(ctx.analyze_stop_grace_s)
                    )
                    - now,
                )
                for active in ctx.active_processes.values()
                if active.terminate_requested_at is not None
            )
            if remaining_deadlines:
                next_timeout_s = min(remaining_deadlines)
    with ctx.event_cond:
        if ctx.enum_queue.empty():
            timeout = ctx.db_flush_interval_s if pending_writes else None
            if next_timeout_s is not None:
                timeout = (
                    next_timeout_s
                    if timeout is None
                    else min(float(timeout), next_timeout_s)
                )
            if active_process_count > 0:
                timeout = 0.1 if timeout is None else min(float(timeout), 0.1)
            ctx.event_cond.wait(timeout=timeout)


def join_enumeration_thread(ctx: _ScanContext) -> None:
    """Join the enumeration thread and report a timeout as a scan issue.

    Args:
        ctx: Shared scan runtime context.
    """
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


def cancelled_result(ctx: _ScanContext, scanned_files: int) -> ScanResult:
    """Build the final result payload for a cancelled scan.

    Args:
        ctx: Shared scan runtime context.
        scanned_files: Number of scanned files to report.

    Returns:
        Final scan result for the cancelled run.
    """
    ctx.db.end_scan_transaction()
    ctx.db.complete_scan(ctx.scan_id, status="cancelled")
    emit_progress(ctx, "done", 1, 1, "Scan cancelled", force=True)
    return ScanResult(
        scan_id=ctx.scan_id,
        groups=ctx.db.load_duplicate_groups(ctx.scan_id),
        issues=ctx.issues,
        scanned_files=scanned_files,
        cached_files=ctx.cached_files,
        fingerprinted_files=ctx.fingerprinted_files,
        metrics=collect_metrics(ctx, MatchStats()),
    )


def completed_result(
    ctx: _ScanContext,
    scanned_files: int,
    timed_db_write: _TimedDbWriteFn,
    flush_scan_tx: Callable[[], None],
) -> ScanResult:
    """Build the final result payload for a completed scan.

    Args:
        ctx: Shared scan runtime context.
        scanned_files: Number of scanned files to report.
        timed_db_write: Callback that records DB write timing and row counts.
        flush_scan_tx: Callback that flushes the open scan transaction.

    Returns:
        Final scan result for the completed run.
    """
    timed_db_write(
        ctx.db.mark_missing_for_scan,
        len(ctx.present_paths),
        ctx.scan_id,
        ctx.present_paths,
    )
    flush_scan_tx()
    emit_progress(ctx, "matching", 0, 1, "Matching duplicates", force=True)
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

    timed_db_write(ctx.db.clear_duplicate_groups, 0, ctx.scan_id)
    group_row_count = len(groups) + sum(len(group.items) for group in groups)
    timed_db_write(
        ctx.db.insert_duplicate_groups_batch,
        group_row_count,
        ctx.scan_id,
        ctx.profile,
        groups,
    )
    flush_scan_tx()

    ctx.db.end_scan_transaction()
    ctx.db.complete_scan(ctx.scan_id, status="done")
    emit_progress(ctx, "done", 1, 1, "Scan complete", force=True)
    return ScanResult(
        scan_id=ctx.scan_id,
        groups=ctx.db.load_duplicate_groups(ctx.scan_id),
        issues=ctx.issues,
        scanned_files=scanned_files,
        cached_files=ctx.cached_files,
        fingerprinted_files=ctx.fingerprinted_files,
        metrics=collect_metrics(ctx, match_stats),
    )


def start_runtime_threads(
    ctx: _ScanContext,
    run_enumeration: Callable[[], None],
) -> None:
    """Start the runtime's enumeration and cancellation threads.

    Args:
        ctx: Shared scan runtime context.
        run_enumeration: Callback that executes the enumeration worker loop.
    """
    ctx.db.begin_scan_transaction()
    ctx.enum_thread = Thread(
        target=run_enumeration,
        name="video-duperz-enumeration",
        daemon=True,
    )
    ctx.enum_thread.start()
    if ctx.cancel_event is None:
        return
    cancel_event = ctx.cancel_event
    ctx.cancel_thread = Thread(
        target=lambda: (cancel_event.wait(), notify_event(ctx)),
        daemon=True,
    )
    ctx.cancel_thread.start()


def emit_worker_cap_warning(ctx: _ScanContext, worker_cap_message: str | None) -> None:
    """Emit the worker-cap reduction warning as an initial progress frame.

    Args:
        ctx: Shared scan runtime context.
        worker_cap_message: Warning text to emit, if any.
    """
    if worker_cap_message is None:
        return
    emit_progress(
        ctx,
        "prepare",
        0,
        1,
        worker_cap_message,
        force=True,
    )
