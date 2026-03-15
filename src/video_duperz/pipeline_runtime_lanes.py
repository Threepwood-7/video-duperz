"""Lane-state and scheduling helpers for the streaming scan runtime."""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING

from .models import ScanLaneSnapshot
from .pipeline_runtime_context import ActiveAnalyzeProcess as _ActiveAnalyzeProcess
from .pipeline_runtime_progress import (
    effective_worker_limit_locked,
    lane_runtime_cap_locked,
)

if TYPE_CHECKING:
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
    elif ctx.enum_finished and state.completed >= state.discovered:
        state.state = "done"
    elif state.discovered > 0:
        state.state = "idle"


def queue_lane_if_ready_locked(ctx: _ScanContext, lane: int) -> None:
    """Queue a lane for scheduling when capacity and pending work allow it.

    Args:
        ctx: Shared scan runtime context.
        lane: Lane identifier that may be ready for worker submission.
    """
    queue_size = len(ctx.lane_queues.get(lane, ()))
    if queue_size <= 0:
        return
    if int(ctx.active_by_lane.get(lane, 0)) >= lane_runtime_cap_locked(ctx, lane):
        return
    if lane in ctx.ready_set:
        return
    ctx.ready_lanes.append(lane)
    ctx.ready_set.add(lane)


def submit_next_for_lane(
    ctx: _ScanContext,
    lane: int,
) -> bool:
    """Launch the next queued analysis task for one lane.

    Args:
        ctx: Shared scan runtime context.
        lane: Lane identifier to submit from.

    Returns:
        ``True`` when a task was launched, otherwise ``False``.
    """
    with ctx.state_lock:
        lane_queue = ctx.lane_queues.get(lane)
        if not lane_queue:
            return False
        lane_active_count = int(ctx.active_by_lane.get(lane, 0))
        if lane_active_count >= lane_runtime_cap_locked(ctx, lane):
            return False
        task = lane_queue.popleft()
        lane_state = ensure_lane_state_locked(ctx, lane, task.source_root)
        lane_state.queued = max(0, lane_state.queued - 1)
    handle = ctx.analyze_launcher.launch(task.path, task.cached_meta)
    started_at = time.perf_counter()
    with ctx.state_lock:
        active_process = _ActiveAnalyzeProcess(
            task=task,
            handle=handle,
            started_at=started_at,
        )
        lane_state.active_file = task.path
        ctx.active_by_lane[lane] = lane_active_count + 1
        ctx.active_workers += 1
        ctx.active_processes[task.task_id] = active_process
        queue_lane_if_ready_locked(ctx, lane)
        refresh_lane_state_locked(ctx, lane)
    return True


def submit_ready_lanes(
    ctx: _ScanContext,
) -> int:
    """Launch ready lanes until the scheduler reaches its worker limit.

    Args:
        ctx: Shared scan runtime context.

    Returns:
        Number of tasks launched during this scheduling pass.
    """
    submitted = 0
    while True:
        with ctx.state_lock:
            if (
                len(ctx.active_processes) >= effective_worker_limit_locked(ctx)
                or not ctx.ready_lanes
            ):
                return submitted
            lane = ctx.ready_lanes.popleft()
            ctx.ready_set.discard(lane)
        if submit_next_for_lane(ctx, lane):
            submitted += 1


def release_task_slot(
    ctx: _ScanContext,
    task: _AnalyzeTask,
    *,
    count_as_analyzed: bool,
) -> tuple[int, int]:
    """Release one active lane slot and optionally count it as analyzed.

    Args:
        ctx: Shared scan runtime context.
        task: Completed or retired analysis task to release.
        count_as_analyzed: Whether the task should advance analyzed counters.

    Returns:
        Tuple of analyzed-file count and total analysis target.
    """
    with ctx.state_lock:
        if count_as_analyzed:
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
        queue_lane_if_ready_locked(ctx, task.lane)
        refresh_lane_state_locked(ctx, task.lane)
        return ctx.analyzed_files, max(1, ctx.total_analyze_files)


def finalize_task(ctx: _ScanContext, task: _AnalyzeTask) -> tuple[int, int]:
    """Finalize counters and lane telemetry for one completed task."""
    return release_task_slot(ctx, task, count_as_analyzed=True)


def apply_cancel_state(ctx: _ScanContext) -> None:
    """Clear queued lane work after cancellation is requested.

    Args:
        ctx: Shared scan runtime context.
    """
    with ctx.state_lock:
        ctx.ready_lanes.clear()
        ctx.ready_set.clear()
        for lane_id, queue in ctx.lane_queues.items():
            queue.clear()
            refresh_lane_state_locked(ctx, lane_id)
