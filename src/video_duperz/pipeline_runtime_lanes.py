"""Lane-state and scheduling helpers for the streaming scan runtime."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from .models import ScanLaneSnapshot
from .pipeline_runtime_progress import (
    effective_worker_limit_locked,
    lane_runtime_cap_locked,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future, ThreadPoolExecutor

    from .pipeline_runtime_context import AnalyzeOutputLike as _AnalyzeOutputLike
    from .pipeline_runtime_context import AnalyzeTask as _AnalyzeTask
    from .pipeline_runtime_context import ScanContext as _ScanContext


def ensure_lane_state_locked(
    ctx: _ScanContext,
    lane: int,
    source_root: str = "",
) -> ScanLaneSnapshot:
    """Return the mutable lane snapshot while holding the state lock.

    Args:
        ctx: Shared scan runtime context.
        lane: Lane identifier whose state should be ensured.
        source_root: Optional source root to append to the lane metadata.

    Returns:
        The ensured lane snapshot for the requested lane.
    """
    state = ctx.lane_states.get(lane)
    if state is None:
        roots_for_lane = [source_root] if source_root else []
        state = ScanLaneSnapshot(lane=lane, roots=roots_for_lane, state="pending")
        ctx.lane_states[lane] = state
    elif source_root and source_root not in state.roots:
        state.roots.append(source_root)
    ctx.lane_queues.setdefault(lane, deque())
    return state


def refresh_lane_state_locked(ctx: _ScanContext, lane: int) -> None:
    """Refresh one lane snapshot after queue or worker-count changes.

    Args:
        ctx: Shared scan runtime context.
        lane: Lane identifier whose state should be recomputed.
    """
    state = ensure_lane_state_locked(ctx, lane)
    queue_size = len(ctx.lane_queues.get(lane, ()))
    lane_active_count = int(ctx.active_by_lane.get(lane, 0))
    state.workers = lane_active_count
    if lane_active_count > 0:
        state.state = "running"
    elif queue_size > 0:
        state.state = "queued"
    elif state.discovery_complete and state.completed >= state.discovered:
        state.state = "done"
    elif state.discovered > 0:
        state.state = "idle"


def _ordered_lane_ids_locked(ctx: _ScanContext) -> list[int]:
    """Return the stable lane ordering used by the round-robin dispatcher."""
    return sorted(int(lane) for lane in ctx.lane_queues)


def _next_ready_lane_locked(ctx: _ScanContext) -> int | None:
    """Return the next lane that can submit work under the current caps."""
    lane_ids = _ordered_lane_ids_locked(ctx)
    if not lane_ids:
        return None
    start = int(ctx.dispatch_lane_cursor) % len(lane_ids)
    ordered = lane_ids[start:] + lane_ids[:start]
    for lane in ordered:
        if not ctx.lane_queues.get(lane):
            continue
        if int(ctx.active_by_lane.get(lane, 0)) >= lane_runtime_cap_locked(ctx, lane):
            continue
        return lane
    return None


def _advance_lane_cursor_locked(ctx: _ScanContext, lane: int) -> None:
    """Advance the scheduler cursor after dispatching one lane task."""
    lane_ids = _ordered_lane_ids_locked(ctx)
    if not lane_ids:
        ctx.dispatch_lane_cursor = 0
        return
    lane_index = lane_ids.index(int(lane))
    ctx.dispatch_lane_cursor = (lane_index + 1) % len(lane_ids)


def submit_ready_lanes(
    ctx: _ScanContext,
    executor: ThreadPoolExecutor,
    on_future_done: Callable[[Future[_AnalyzeOutputLike]], None],
    on_task_started: Callable[[_AnalyzeTask], None] | None = None,
) -> int:
    """Submit ready lanes until the scheduler reaches its worker limit.

    Args:
        ctx: Shared scan runtime context.
        executor: Thread pool used for file analysis tasks.
        on_future_done: Callback attached to submitted futures.

    Returns:
        Number of tasks submitted during this scheduling pass.
    """
    submitted = 0
    while True:
        with ctx.state_lock:
            if len(ctx.futures) >= effective_worker_limit_locked(ctx):
                return submitted
            lane = _next_ready_lane_locked(ctx)
            if lane is None:
                return submitted
            lane_queue = ctx.lane_queues.get(lane)
            if not lane_queue:
                return submitted
            task = lane_queue.popleft()
            lane_active_count = int(ctx.active_by_lane.get(task.lane, 0))
            lane_state = ensure_lane_state_locked(ctx, task.lane, task.source_root)
            lane_state.queued = max(0, lane_state.queued - 1)
            lane_state.active_file = task.path
            ctx.active_by_lane[task.lane] = lane_active_count + 1
            ctx.active_workers += 1
            _advance_lane_cursor_locked(ctx, lane)
            refresh_lane_state_locked(ctx, task.lane)
        future = executor.submit(ctx.analyze_file, task.path, task.cached_meta)
        future.add_done_callback(on_future_done)
        with ctx.state_lock:
            ctx.futures[future] = task
        if on_task_started is not None:
            on_task_started(task)
        submitted += 1


def finalize_task(ctx: _ScanContext, task: _AnalyzeTask) -> tuple[int, int]:
    """Finalize counters and lane telemetry for one completed task.

    Args:
        ctx: Shared scan runtime context.
        task: Completed analysis task to retire.

    Returns:
        Tuple of completed-work count and total work target.
    """
    with ctx.state_lock:
        ctx.analyzed_files += 1
        ctx.analyzed_bytes += task.size
        ctx.active_workers = max(0, ctx.active_workers - 1)
        lane_workers = max(0, int(ctx.active_by_lane.get(task.lane, 0)) - 1)
        ctx.active_by_lane[task.lane] = lane_workers
        lane_state = ensure_lane_state_locked(ctx, task.lane, task.source_root)
        lane_state.analyzed += 1
        lane_state.analyzed_bytes += task.size
        lane_state.completed += 1
        lane_state.active_file = ""
        refresh_lane_state_locked(ctx, task.lane)
        total_work_files = (
            len(ctx.enum_files)
            if ctx.enum_finished and ctx.enum_files
            else max(1, ctx.prepared_files)
        )
        return (ctx.cached_files + ctx.analyzed_files, total_work_files)


def apply_cancel_state(ctx: _ScanContext) -> None:
    """Clear queued lane work after cancellation is requested.

    Args:
        ctx: Shared scan runtime context.
    """
    with ctx.state_lock:
        for lane_id, queue in ctx.lane_queues.items():
            queue.clear()
            refresh_lane_state_locked(ctx, lane_id)
