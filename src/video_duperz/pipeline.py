from __future__ import annotations

import contextlib
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Condition, Event, Lock, Thread
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from threep_commons.fs_paths import path_key

from .fingerprint import ALGO_VERSION, FingerprintError, build_fingerprint_record
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
from .probe import ProbeError, ensure_ffprobe_available, probe_video
from .scanner import build_physical_drive_scan_plan, enumerate_video_files

if TYPE_CHECKING:
    from .db import Database

ProgressCallback = Callable[[ScanProgress], None]
_MIB = 1024.0 * 1024.0
_ReturnT = TypeVar("_ReturnT")
_P = ParamSpec("_P")


def _emit(
    progress_cb: ProgressCallback | None,
    stage: str,
    current: int,
    total: int,
    message: str = "",
    *,
    active_workers: int | None = None,
    worker_limit: int | None = None,
    enumerated_roots: int | None = None,
    total_roots: int | None = None,
    prepared_files: int | None = None,
    discovered_files: int | None = None,
    discovered_bytes: int | None = None,
    analyzed_files: int | None = None,
    analyzed_bytes: int | None = None,
    cached_files: int | None = None,
    discovered_files_per_s: float | None = None,
    discovered_mib_per_s: float | None = None,
    analyzed_files_per_s: float | None = None,
    analyzed_mib_per_s: float | None = None,
    cache_hit_ratio: float | None = None,
    elapsed_s: float | None = None,
    total_analyze_files: int | None = None,
    lane_snapshots: list[ScanLaneSnapshot] | None = None,
) -> None:
    if progress_cb:
        progress_cb(
            ScanProgress(
                stage=stage,
                current=current,
                total=total,
                message=message,
                active_workers=active_workers,
                worker_limit=worker_limit,
                enumerated_roots=enumerated_roots,
                total_roots=total_roots,
                prepared_files=prepared_files,
                discovered_files=discovered_files,
                discovered_bytes=discovered_bytes,
                analyzed_files=analyzed_files,
                analyzed_bytes=analyzed_bytes,
                cached_files=cached_files,
                discovered_files_per_s=discovered_files_per_s,
                discovered_mib_per_s=discovered_mib_per_s,
                analyzed_files_per_s=analyzed_files_per_s,
                analyzed_mib_per_s=analyzed_mib_per_s,
                cache_hit_ratio=cache_hit_ratio,
                elapsed_s=elapsed_s,
                total_analyze_files=total_analyze_files,
                lane_snapshots=lane_snapshots,
            )
        )


def _normalize_probe_worker_mode(value: str) -> ProbeWorkerMode:
    mode = str(value or "").strip().lower()
    if mode == "burst":
        return "burst"
    return "balanced"


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


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


@dataclass(slots=True)
class _AnalyzeOutput:
    meta: VideoMeta
    hashes: list[int]
    probe_s: float = 0.0
    fingerprint_s: float = 0.0


@dataclass(slots=True)
class _AnalyzeTask:
    file: VideoRecord
    file_id: int
    cached_meta: VideoMeta | None
    lane: int
    source_root: str
    path: str
    size: int


def _analyze_file(path: str, cached_meta: VideoMeta | None) -> _AnalyzeOutput:
    if cached_meta is None:
        probe_started = time.perf_counter()
        meta = probe_video(path)
        probe_s = max(0.0, time.perf_counter() - probe_started)
    else:
        meta = cached_meta
        probe_s = 0.0
    fp_started = time.perf_counter()
    fp_record = build_fingerprint_record(
        file_id=0, duration_s=meta.duration_s, path=path
    )
    fingerprint_s = max(0.0, time.perf_counter() - fp_started)
    return _AnalyzeOutput(
        meta=meta, hashes=fp_record.hashes, probe_s=probe_s, fingerprint_s=fingerprint_s
    )


def run_scan(
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
    progress_cb: ProgressCallback | None = None,
) -> ScanResult:
    ensure_ffprobe_available()
    db_batch_size = _clamp(db_batch_size, 32, 4096)
    db_flush_interval_ms = _clamp(db_flush_interval_ms, 50, 2000)
    enum_queue_max = _clamp(enum_queue_max, 256, 32768)
    progress_emit_interval_ms = _clamp(progress_emit_interval_ms, 50, 2000)
    progress_emit_every_files = _clamp(progress_emit_every_files, 10, 5000)
    db_flush_interval_s = float(db_flush_interval_ms) / 1000.0
    progress_emit_interval_s = float(progress_emit_interval_ms) / 1000.0

    scan_id = db.create_scan(profile=profile, roots=roots, extensions=extensions)
    issues: list[ScanIssue] = []
    cached_files = 0
    fingerprinted_files = 0
    requested_floor = max(1, int(max_workers))
    scan_plan = build_physical_drive_scan_plan(
        roots=roots,
        max_workers=requested_floor,
        drive_worker_overrides=drive_worker_overrides,
    )
    issues.extend(scan_plan.issues)
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

    root_to_lane = {
        path_key(root): lane for root, lane in scan_plan.root_to_group_index.items()
    }
    ready_lanes: deque[int] = deque()
    ready_set: set[int] = set()
    active_by_lane: dict[int, int] = dict.fromkeys(lane_states, 0)
    futures: dict[Future[_AnalyzeOutput], _AnalyzeTask] = {}
    done_futures: deque[Future[_AnalyzeOutput]] = deque()
    event_cond = Condition()
    state_lock = Lock()

    present_paths: set[str] = set()
    streamed_path_keys: set[str] = set()
    pending_discovered: list[VideoRecord] = []
    pending_meta_rows: list[tuple[int, VideoMeta]] = []
    pending_fp_rows: list[tuple[int, int, list[int]]] = []
    pending_probe_error_rows: list[tuple[int, str]] = []

    scan_started_at = time.perf_counter()
    stage_seconds: dict[str, float] = {
        "enumerate": 0.0,
        "db_write": 0.0,
        "probe": 0.0,
        "fingerprint": 0.0,
        "matching": 0.0,
    }
    flush_count = 0
    flush_rows_total = 0
    rows_since_flush = 0
    max_queue_depth = 0
    last_tx_flush_at = scan_started_at
    last_pending_write_at = scan_started_at
    last_discovered_batch_at = scan_started_at

    prepared_files = 0
    discovered_files = 0
    discovered_bytes = 0
    analyzed_files = 0
    analyzed_bytes = 0
    total_analyze_files = 0
    active_workers = 0
    enumerated_roots = 0
    total_roots = max(1, len(roots))
    enum_finished = False
    cancel_requested = False
    cancel_applied = False

    enum_files: list[VideoRecord] = []
    enum_issues: list[ScanIssue] = []
    enum_error: Exception | None = None

    enum_queue: Queue[VideoRecord | object] = Queue(maxsize=enum_queue_max)
    enum_sentinel = object()

    last_emit_at = 0.0
    last_emit_stage = ""
    last_emit_counter = 0

    def _notify_event() -> None:
        with event_cond:
            event_cond.notify_all()

    def _effective_worker_limit_locked() -> int:
        return max(0, int(effective_worker_limit))

    def _lane_runtime_cap_locked(lane: int) -> int:
        return max(1, int(lane_runtime_caps.get(lane, 1)))

    def _rate(value: int, elapsed_s: float) -> float:
        if elapsed_s <= 0.0:
            return 0.0
        return float(value) / elapsed_s

    def _mib_per_s(byte_count: int, elapsed_s: float) -> float:
        if elapsed_s <= 0.0:
            return 0.0
        return float(byte_count) / _MIB / elapsed_s

    def _clone_lane_snapshots_locked(elapsed_s: float) -> list[ScanLaneSnapshot]:
        snapshots: list[ScanLaneSnapshot] = []
        for lane_id in sorted(lane_states):
            lane = lane_states[lane_id]
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
        stage: str,
        current: int,
        total: int,
        message: str,
        *,
        force: bool = False,
        file_counter: int | None = None,
    ) -> None:
        nonlocal last_emit_at, last_emit_stage, last_emit_counter
        elapsed_s = max(0.0, time.perf_counter() - scan_started_at)
        with state_lock:
            snapshots = _clone_lane_snapshots_locked(elapsed_s)
            workers_now = active_workers
            workers_total = _effective_worker_limit_locked()
            enum_now = enumerated_roots
            enum_total = total_roots
            prepared_now = prepared_files
            discovered_now = discovered_files
            discovered_bytes_now = discovered_bytes
            analyze_now = analyzed_files
            analyze_bytes_now = analyzed_bytes
            cached_now = cached_files
            analyze_total = total_analyze_files
        counter = int(
            file_counter if file_counter is not None else max(prepared_now, analyze_now)
        )
        if not _should_emit_progress(
            stage=stage,
            counter=counter,
            force=force,
            last_emit_stage=last_emit_stage,
            last_emit_counter=last_emit_counter,
            last_emit_at=last_emit_at,
            progress_emit_every_files=progress_emit_every_files,
            progress_emit_interval_s=progress_emit_interval_s,
        ):
            return
        now = time.perf_counter()
        last_emit_at = now
        last_emit_stage = stage
        last_emit_counter = counter
        cache_ratio = (
            (float(cached_now) / float(prepared_now)) if prepared_now > 0 else 0.0
        )
        _emit(
            progress_cb,
            stage,
            current,
            total,
            message,
            active_workers=workers_now,
            worker_limit=workers_total,
            enumerated_roots=enum_now,
            total_roots=enum_total,
            prepared_files=prepared_now,
            discovered_files=discovered_now,
            discovered_bytes=discovered_bytes_now,
            analyzed_files=analyze_now,
            analyzed_bytes=analyze_bytes_now,
            cached_files=cached_now,
            discovered_files_per_s=_rate(discovered_now, elapsed_s),
            discovered_mib_per_s=_mib_per_s(discovered_bytes_now, elapsed_s),
            analyzed_files_per_s=_rate(analyze_now, elapsed_s),
            analyzed_mib_per_s=_mib_per_s(analyze_bytes_now, elapsed_s),
            cache_hit_ratio=cache_ratio,
            elapsed_s=elapsed_s,
            total_analyze_files=analyze_total,
            lane_snapshots=snapshots,
        )

    def _ensure_lane_state_locked(lane: int, source_root: str = "") -> ScanLaneSnapshot:
        state = lane_states.get(lane)
        if state is None:
            roots_for_lane = [source_root] if source_root else []
            state = ScanLaneSnapshot(lane=lane, roots=roots_for_lane, state="pending")
            lane_states[lane] = state
        elif source_root and source_root not in state.roots:
            state.roots.append(source_root)
        lane_queues.setdefault(lane, deque())
        return state

    def _refresh_lane_state_locked(lane: int) -> None:
        state = _ensure_lane_state_locked(lane)
        queue_size = len(lane_queues.get(lane, ()))
        lane_active_count = int(active_by_lane.get(lane, 0))
        lane_is_active = lane_active_count > 0
        state.workers = lane_active_count
        if lane_is_active:
            state.state = "running"
            return
        if queue_size > 0:
            state.state = "queued"
            return
        if enum_finished and state.completed >= state.discovered:
            state.state = "done"
            return
        if state.discovered > 0:
            state.state = "idle"

    def _queue_lane_if_ready_locked(lane: int) -> None:
        queue_size = len(lane_queues.get(lane, ()))
        if queue_size <= 0:
            return
        if int(active_by_lane.get(lane, 0)) >= _lane_runtime_cap_locked(lane):
            return
        if lane in ready_set:
            return
        ready_lanes.append(lane)
        ready_set.add(lane)

    def _prepare_total_locked() -> int:
        if enum_finished and enum_files:
            return len(enum_files)
        return max(1, prepared_files)

    def _record_db_write(elapsed_s: float, row_count: int) -> None:
        nonlocal rows_since_flush
        with state_lock:
            stage_seconds["db_write"] += max(0.0, float(elapsed_s))
        rows_since_flush += max(0, int(row_count))

    def _timed_db_write(
        write_fn: Callable[_P, _ReturnT],
        row_count: int,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _ReturnT:
        started = time.perf_counter()
        out = write_fn(*args, **kwargs)
        _record_db_write(time.perf_counter() - started, row_count)
        return out

    def _flush_scan_transaction(*, force: bool = False) -> None:
        nonlocal flush_count, flush_rows_total, rows_since_flush, last_tx_flush_at
        if rows_since_flush <= 0:
            return
        now = time.perf_counter()
        if (
            not force
            and rows_since_flush < db_batch_size
            and (now - last_tx_flush_at) < db_flush_interval_s
        ):
            return
        started = time.perf_counter()
        db.flush_scan_transaction()
        _record_db_write(time.perf_counter() - started, 0)
        flush_count += 1
        flush_rows_total += rows_since_flush
        rows_since_flush = 0
        last_tx_flush_at = now

    def _flush_pending_analysis_batches(*, force: bool = False) -> None:
        nonlocal last_pending_write_at
        pending_total = (
            len(pending_meta_rows)
            + len(pending_fp_rows)
            + len(pending_probe_error_rows)
        )
        if pending_total <= 0:
            return
        now = time.perf_counter()
        if (
            not force
            and pending_total < db_batch_size
            and (now - last_pending_write_at) < db_flush_interval_s
        ):
            return
        while pending_meta_rows:
            chunk = pending_meta_rows[:db_batch_size]
            del pending_meta_rows[: len(chunk)]
            _timed_db_write(db.save_video_meta_batch, len(chunk), chunk)
        while pending_fp_rows:
            chunk = pending_fp_rows[:db_batch_size]
            del pending_fp_rows[: len(chunk)]
            _timed_db_write(db.save_fingerprints_batch, len(chunk), chunk)
        while pending_probe_error_rows:
            chunk = pending_probe_error_rows[:db_batch_size]
            del pending_probe_error_rows[: len(chunk)]
            _timed_db_write(db.save_probe_errors_batch, len(chunk), chunk)
        last_pending_write_at = now

    def _queue_enum_item(item: object) -> None:
        nonlocal max_queue_depth
        while True:
            try:
                enum_queue.put(item, timeout=0.1)
                break
            except Full:
                if cancel_event and cancel_event.is_set() and item is not enum_sentinel:
                    continue
        with state_lock:
            max_queue_depth = max(max_queue_depth, int(enum_queue.qsize()))
        _notify_event()

    def _on_file_discovered(file: object) -> None:
        key = path_key(str(getattr(file, "path", "")))
        with state_lock:
            if key in streamed_path_keys:
                return
            streamed_path_keys.add(key)
        _queue_enum_item(file)

    def _on_enumerate_progress(current: int, total: int, message: str) -> None:
        nonlocal enumerated_roots, total_roots
        root_from_message = ""
        marker = " [workers "
        if message.startswith("Enumerated ") and marker in message:
            root_from_message = message[len("Enumerated ") : message.rfind(marker)]
        with state_lock:
            enumerated_roots = current
            total_roots = max(1, total)
            if root_from_message:
                lane = root_to_lane.get(path_key(root_from_message))
                if lane is not None:
                    lane_state = _ensure_lane_state_locked(lane, root_from_message)
                    if lane_state.state == "pending":
                        lane_state.state = "idle"
            discovered_now = discovered_files
        _emit_progress(
            "enumerate", current, total, message, file_counter=discovered_now
        )

    def _run_enumeration() -> None:
        nonlocal enum_files, enum_issues, enum_error, enum_finished
        enumerate_started = time.perf_counter()
        try:
            files, local_issues = enumerate_video_files(
                scan_id=scan_id,
                roots=roots,
                extensions=extensions,
                max_workers=max(requested_floor, len(scan_plan.root_groups)),
                drive_worker_overrides=drive_worker_overrides,
                cancel_event=cancel_event,
                progress_cb=_on_enumerate_progress,
                on_file_discovered=_on_file_discovered,
            )
            enum_files = list(files)
            enum_issues = list(local_issues)
            # Compatibility for callers/tests that monkeypatch enumerate_video_files
            # and ignore the streaming callback.
            for file in enum_files:
                key = path_key(str(getattr(file, "path", "")))
                with state_lock:
                    if key in streamed_path_keys:
                        continue
                    streamed_path_keys.add(key)
                _queue_enum_item(file)
        except Exception as exc:
            enum_error = exc
        finally:
            elapsed = max(0.0, time.perf_counter() - enumerate_started)
            with state_lock:
                stage_seconds["enumerate"] += elapsed
                enum_finished = True
                for lane_id in list(lane_states):
                    _refresh_lane_state_locked(lane_id)
            _queue_enum_item(enum_sentinel)

    def _drain_enum_queue() -> list[VideoRecord | object]:
        nonlocal max_queue_depth
        drained: list[VideoRecord | object] = []
        while True:
            try:
                drained.append(enum_queue.get_nowait())
            except Empty:
                break
        with state_lock:
            max_queue_depth = max(max_queue_depth, int(enum_queue.qsize()))
        return drained

    def _process_discovered_batch(batch: list[VideoRecord]) -> None:
        nonlocal cached_files, prepared_files, total_analyze_files
        nonlocal discovered_files, discovered_bytes, last_discovered_batch_at
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
            with state_lock:
                lane_state = _ensure_lane_state_locked(lane, source_root)
                lane_state.discovered += 1
                lane_state.discovered_bytes += file_size
                if lane_state.state in {"pending", "enumerating"}:
                    lane_state.state = "idle"
                discovered_files += 1
                discovered_bytes += file_size
                prepared_files += 1
                prepared_now = prepared_files
                prepare_total = _prepare_total_locked()
            present_paths.add(path)
            upsert_payload.append(
                {
                    "path": path,
                    "size": file_size,
                    "mtime_ns": int(getattr(file, "mtime_ns", 0)),
                    "ctime_ns": int(getattr(file, "ctime_ns", 0)),
                    "ext": str(getattr(file, "ext", "")),
                    "scan_id": scan_id,
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
                "prepare",
                prepared_now,
                max(1, prepare_total),
                f"Prepared {path}",
                file_counter=prepared_now,
            )
        if not valid:
            return

        by_path = _timed_db_write(db.upsert_files_batch, len(valid), upsert_payload)
        cache_by_path = db.load_cached_artifacts_batch(cache_payload)
        for file, lane, source_root, file_size, path in valid:
            file_id = int(by_path.get(path, 0))
            if file_id <= 0:
                continue
            file.file_id = file_id
            cache = cache_by_path.get(path)
            if cache and "meta" in cache and "fingerprint" in cache:
                fp = cache["fingerprint"]
                if int(fp.get("algo_version", -1)) == ALGO_VERSION:
                    with state_lock:
                        cached_files += 1
                        lane_state = _ensure_lane_state_locked(lane, source_root)
                        lane_state.completed += 1
                        _refresh_lane_state_locked(lane)
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
            with state_lock:
                lane_queue = lane_queues.setdefault(lane, deque())
                lane_queue.append(task)
                lane_state = _ensure_lane_state_locked(lane, source_root)
                lane_state.queued += 1
                total_analyze_files += 1
                _queue_lane_if_ready_locked(lane)
                _refresh_lane_state_locked(lane)
        last_discovered_batch_at = time.perf_counter()

    def _on_future_done(future: Future[_AnalyzeOutput]) -> None:
        with event_cond:
            done_futures.append(future)
            event_cond.notify_all()

    def _submit_next_for_lane(executor: ThreadPoolExecutor, lane: int) -> bool:
        nonlocal active_workers
        with state_lock:
            lane_queue = lane_queues.get(lane)
            if not lane_queue:
                return False
            lane_active_count = int(active_by_lane.get(lane, 0))
            if lane_active_count >= _lane_runtime_cap_locked(lane):
                return False
            task = lane_queue.popleft()
            lane_state = _ensure_lane_state_locked(lane, task.source_root)
            lane_state.queued = max(0, lane_state.queued - 1)
            lane_state.active_file = task.path
            active_by_lane[lane] = lane_active_count + 1
            active_workers += 1
            _queue_lane_if_ready_locked(lane)
            _refresh_lane_state_locked(lane)
        future = executor.submit(_analyze_file, task.path, task.cached_meta)
        future.add_done_callback(_on_future_done)
        with state_lock:
            futures[future] = task
        return True

    def _submit_ready_lanes(executor: ThreadPoolExecutor) -> int:
        submitted = 0
        while True:
            with state_lock:
                worker_cap = _effective_worker_limit_locked()
                if len(futures) >= worker_cap or not ready_lanes:
                    return submitted
                lane = ready_lanes.popleft()
                ready_set.discard(lane)
            if _submit_next_for_lane(executor, lane):
                submitted += 1

    def _process_done_futures() -> int:
        nonlocal fingerprinted_files, analyzed_files, analyzed_bytes, active_workers
        processed = 0
        while True:
            with event_cond:
                if not done_futures:
                    break
                future = done_futures.popleft()
            with state_lock:
                task = futures.pop(future, None)
            if task is None:
                continue
            processed += 1
            try:
                output = future.result()
            except ProbeError as exc:
                pending_probe_error_rows.append((task.file_id, str(exc)))
                issues.append(
                    ScanIssue(stage="probe", path=task.path, message=str(exc))
                )
            except FingerprintError as exc:
                issues.append(
                    ScanIssue(stage="fingerprint", path=task.path, message=str(exc))
                )
            except Exception as exc:
                issues.append(
                    ScanIssue(stage="analyze", path=task.path, message=str(exc))
                )
            else:
                if task.cached_meta is None:
                    pending_meta_rows.append((task.file_id, output.meta))
                pending_fp_rows.append((task.file_id, ALGO_VERSION, output.hashes))
                fingerprinted_files += 1
                with state_lock:
                    stage_seconds["probe"] += max(0.0, float(output.probe_s))
                    stage_seconds["fingerprint"] += max(
                        0.0, float(output.fingerprint_s)
                    )

            with state_lock:
                analyzed_files += 1
                analyzed_bytes += task.size
                active_workers = max(0, active_workers - 1)
                active_by_lane[task.lane] = max(
                    0, int(active_by_lane.get(task.lane, 0)) - 1
                )
                lane_state = _ensure_lane_state_locked(task.lane, task.source_root)
                lane_state.analyzed += 1
                lane_state.analyzed_bytes += task.size
                lane_state.completed += 1
                lane_state.active_file = ""
                _queue_lane_if_ready_locked(task.lane)
                _refresh_lane_state_locked(task.lane)
                probe_total = max(1, total_analyze_files)
                probe_done = analyzed_files
            _emit_progress(
                "probe",
                probe_done,
                probe_total,
                f"Analyzed {task.path}",
                file_counter=probe_done,
            )
        return processed

    def _apply_cancel_state() -> None:
        with state_lock:
            ready_lanes.clear()
            ready_set.clear()
            for lane_id, queue in lane_queues.items():
                queue.clear()
                _refresh_lane_state_locked(lane_id)

    def _collect_metrics(match_stats: MatchStats) -> dict[str, object]:
        avg_rows_per_flush = (
            (float(flush_rows_total) / float(flush_count)) if flush_count > 0 else 0.0
        )
        return {
            "stage_seconds": {
                name: round(value, 6) for name, value in stage_seconds.items()
            },
            "matching": asdict(match_stats),
            "flush_count": int(flush_count),
            "avg_rows_per_flush": float(avg_rows_per_flush),
            "max_queue_depth": int(max_queue_depth),
        }

    def _best_effort_end_scan_tx() -> None:
        with contextlib.suppress(Exception):
            _flush_pending_analysis_batches(force=True)
        with contextlib.suppress(Exception):
            _flush_scan_transaction(force=True)
        with contextlib.suppress(Exception):
            db.end_scan_transaction()

    db.begin_scan_transaction()
    enum_thread = Thread(
        target=_run_enumeration, name="video-duperz-enumeration", daemon=True
    )
    enum_thread.start()

    if cancel_event is not None:
        cancel_thread = Thread(
            target=lambda: (cancel_event.wait(), _notify_event()), daemon=True
        )
        cancel_thread.start()

    if scan_plan.requested_worker_target > scan_plan.effective_total_workers:
        _emit_progress(
            "prepare",
            0,
            1,
            (
                "Requested worker capacity "
                f"({scan_plan.requested_worker_target}) reduced to "
                f"{scan_plan.effective_total_workers} by per-drive hard caps."
            ),
            force=True,
        )

    try:
        with ThreadPoolExecutor(max_workers=executor_worker_limit) as executor:
            while True:
                if cancel_event and cancel_event.is_set():
                    cancel_requested = True
                if cancel_requested and not cancel_applied:
                    _apply_cancel_state()
                    cancel_applied = True

                made_progress = False

                drained = _drain_enum_queue()
                if drained:
                    made_progress = True
                for queued in drained:
                    if queued is enum_sentinel:
                        continue
                    if not isinstance(queued, VideoRecord):
                        continue
                    if cancel_requested:
                        continue
                    pending_discovered.append(queued)

                now = time.perf_counter()
                if cancel_requested:
                    pending_discovered.clear()
                elif pending_discovered:
                    should_process = (
                        len(pending_discovered) >= db_batch_size
                        or (now - last_discovered_batch_at) >= db_flush_interval_s
                        or enum_finished
                        or not futures
                    )
                    if should_process:
                        while pending_discovered and (
                            len(pending_discovered) >= db_batch_size
                            or enum_finished
                            or cancel_requested
                        ):
                            chunk = pending_discovered[:db_batch_size]
                            del pending_discovered[: len(chunk)]
                            _process_discovered_batch(chunk)
                            made_progress = True
                        if pending_discovered and (
                            not futures
                            or (time.perf_counter() - last_discovered_batch_at)
                            >= db_flush_interval_s
                        ):
                            chunk = pending_discovered[:db_batch_size]
                            del pending_discovered[: len(chunk)]
                            _process_discovered_batch(chunk)
                            made_progress = True

                done_count = _process_done_futures()
                if done_count > 0:
                    made_progress = True

                if not cancel_requested and _submit_ready_lanes(executor) > 0:
                    made_progress = True

                _flush_pending_analysis_batches(force=cancel_requested)
                _flush_scan_transaction(force=cancel_requested)

                with state_lock:
                    waiting_items = any(bool(queue) for queue in lane_queues.values())
                    active_count = len(futures)
                pending_writes = bool(
                    pending_meta_rows or pending_fp_rows or pending_probe_error_rows
                )

                if cancel_requested and enum_finished and active_count == 0:
                    _flush_pending_analysis_batches(force=True)
                    _flush_scan_transaction(force=True)
                    break
                if (
                    enum_finished
                    and active_count == 0
                    and not waiting_items
                    and not pending_discovered
                    and not pending_writes
                    and enum_queue.empty()
                ):
                    _flush_pending_analysis_batches(force=True)
                    _flush_scan_transaction(force=True)
                    break

                if not made_progress:
                    with event_cond:
                        if not done_futures and enum_queue.empty():
                            wait_timeout = (
                                db_flush_interval_s
                                if (pending_discovered or pending_writes)
                                else None
                            )
                            event_cond.wait(timeout=wait_timeout)

        enum_thread.join(timeout=5.0)
        if enum_thread.is_alive():
            issues.append(
                ScanIssue(
                    stage="enumerate",
                    path="",
                    message="Enumeration thread did not stop cleanly after timeout",
                )
            )

        if enum_error and not cancel_requested:
            raise enum_error

        issues.extend(enum_issues)
        scanned_files = len(enum_files) if enum_files else len(present_paths)

        if cancel_requested:
            db.end_scan_transaction()
            db.complete_scan(scan_id, status="cancelled")
            _emit_progress("done", 1, 1, "Scan cancelled", force=True)
            return ScanResult(
                scan_id=scan_id,
                groups=db.load_duplicate_groups(scan_id),
                issues=issues,
                scanned_files=scanned_files,
                cached_files=cached_files,
                fingerprinted_files=fingerprinted_files,
                metrics=_collect_metrics(MatchStats()),
            )

        _timed_db_write(
            db.mark_missing_for_scan, len(present_paths), scan_id, present_paths
        )
        _flush_scan_transaction(force=True)
        _emit_progress("matching", 0, 1, "Matching duplicates", force=True)
        matching_started = time.perf_counter()
        items = db.list_match_items_for_scan(scan_id=scan_id, algo_version=ALGO_VERSION)
        edges, match_stats = find_duplicate_edges(items, profile=profile)
        groups = build_duplicate_groups(items=items, edges=edges, profile=profile)
        with state_lock:
            stage_seconds["matching"] += max(
                0.0, time.perf_counter() - matching_started
            )

        _timed_db_write(db.clear_duplicate_groups, 0, scan_id)
        group_row_count = len(groups) + sum(len(group.items) for group in groups)
        _timed_db_write(
            db.insert_duplicate_groups_batch, group_row_count, scan_id, profile, groups
        )
        _flush_scan_transaction(force=True)

        db.end_scan_transaction()
        db.complete_scan(scan_id, status="done")
        _emit_progress("done", 1, 1, "Scan complete", force=True)
        return ScanResult(
            scan_id=scan_id,
            groups=db.load_duplicate_groups(scan_id),
            issues=issues,
            scanned_files=scanned_files,
            cached_files=cached_files,
            fingerprinted_files=fingerprinted_files,
            metrics=_collect_metrics(match_stats),
        )
    except Exception:
        _best_effort_end_scan_tx()
        raise
