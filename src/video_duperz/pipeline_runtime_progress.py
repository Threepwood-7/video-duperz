"""Progress and worker-limit helpers for the streaming scan runtime."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .models import ScanLaneSnapshot, ScanProgress

if TYPE_CHECKING:
    from .pipeline_runtime_context import ScanContext as _ScanContext

_MIB = 1024.0 * 1024.0


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
    """Return whether a progress frame should be emitted right now."""
    if force or stage != last_emit_stage:
        return True
    if counter - last_emit_counter >= progress_emit_every_files:
        return True
    return (time.perf_counter() - last_emit_at) >= progress_emit_interval_s


def notify_event(ctx: _ScanContext) -> None:
    """Wake the pipeline loop after state changes or completed work."""
    with ctx.event_cond:
        ctx.event_cond.notify_all()


def effective_worker_limit_locked(ctx: _ScanContext) -> int:
    """Return the normalized global worker limit while holding the lock."""
    return max(0, int(ctx.effective_worker_limit))


def lane_runtime_cap_locked(ctx: _ScanContext, lane: int) -> int:
    """Return the normalized per-lane runtime cap while holding the lock."""
    return max(1, int(ctx.lane_runtime_caps.get(lane, 1)))


def _rate(value: int, elapsed_s: float) -> float:
    """Convert a running count to a per-second rate."""
    if elapsed_s <= 0.0:
        return 0.0
    return float(value) / elapsed_s


def _mib_per_s(byte_count: int, elapsed_s: float) -> float:
    """Convert a running byte count to MiB per second."""
    if elapsed_s <= 0.0:
        return 0.0
    return float(byte_count) / _MIB / elapsed_s


def _clone_lane_snapshots_locked(
    ctx: _ScanContext,
    elapsed_s: float,
) -> list[ScanLaneSnapshot]:
    """Clone lane telemetry snapshots for a progress emission."""
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


def emit_progress(
    ctx: _ScanContext,
    stage: str,
    current: int,
    total: int,
    message: str,
    *,
    force: bool = False,
    file_counter: int | None = None,
) -> None:
    """Emit a progress frame when stage or throughput thresholds require it."""
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
                worker_limit=effective_worker_limit_locked(ctx),
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
