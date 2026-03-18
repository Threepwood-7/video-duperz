"""Deterministic benchmark planning and evaluation helpers."""

from __future__ import annotations

import json
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from .fingerprint import ensure_fingerprint_fallback_chain_available
from .media_format_policy import is_problematic_media_path
from .pipeline import build_analyze_file
from .probe import ensure_probe_backend_available
from .scanner import build_physical_drive_scan_plan, enumerate_video_files

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from .models import FrameDecodeBackendId, ProbeBackendId, VideoRecord
    from .pipeline_runtime_context import AnalyzeOutputLike

BenchmarkDispatchStrategy = Literal["ordered", "micro_batch"]
BenchmarkSetId = Literal["ghetto", "mixed_240", "incomplete"]
BenchmarkFileStatus = Literal["success", "probe", "fingerprint", "analyze"]

_BENCHMARK_VIDEO_EXTENSIONS = (
    "mp4",
    "mkv",
    "avi",
    "wmv",
    "asf",
    "mov",
    "mpg",
    "mpeg",
    "m4v",
    "ts",
    "mts",
    "m2ts",
    "webm",
    "flv",
    "vob",
)


@dataclass(slots=True)
class BenchmarkSampleFile:
    """One planned benchmark input file."""

    path: str
    source_root: str
    lane: int
    size: int
    ext: str
    is_risky: bool


@dataclass(slots=True)
class BenchmarkSamplePlan:
    """Deterministic file selection for one benchmark scenario."""

    set_id: BenchmarkSetId
    files: list[BenchmarkSampleFile]
    total_candidates: int
    roots: list[str]
    lane_roots: dict[int, list[str]]


@dataclass(slots=True)
class BenchmarkFileResult:
    """Observed result for one analyzed benchmark file."""

    path: str
    lane: int
    status: BenchmarkFileStatus
    elapsed_s: float
    fallback_count: int
    decoder_timeout_count: int
    fingerprint_decoder_backend: FrameDecodeBackendId | None
    failure_message: str = ""


@dataclass(slots=True)
class BenchmarkLaneMetrics:
    """Aggregated service metrics for one physical-drive lane."""

    lane: int
    roots: list[str]
    files: int = 0
    successes: int = 0
    failures: int = 0
    fallback_count: int = 0
    decoder_timeout_count: int = 0
    average_service_s: float = 0.0
    peak_service_s: float = 0.0
    decoder_mix: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class BenchmarkWorkerGuidance:
    """Recommendation derived from benchmark lane-pressure telemetry."""

    recommended_workers: int
    reason: str


@dataclass(slots=True)
class BenchmarkRunSummary:
    """Structured output emitted by the benchmark harness."""

    set_id: BenchmarkSetId
    dispatch_strategy: BenchmarkDispatchStrategy
    requested_workers: int
    worker_limit: int
    analyzed_files: int
    success_count: int
    failure_count: int
    fallback_count: int
    decoder_timeout_count: int
    elapsed_s: float
    lane_metrics: list[BenchmarkLaneMetrics]
    guidance: BenchmarkWorkerGuidance

    def to_json(self) -> str:
        """Serialize the benchmark summary to deterministic JSON."""

        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _sorted_records(records: Iterable[VideoRecord]) -> list[VideoRecord]:
    """Return a stable path-sorted list of scan records."""

    return sorted(
        records,
        key=lambda item: (
            str(item.source_root).casefold(),
            str(item.path).casefold(),
        ),
    )


def _sample_file_from_record(record: VideoRecord) -> BenchmarkSampleFile:
    """Convert one enumerated record into a benchmark sample file."""
    return BenchmarkSampleFile(
        path=record.path,
        source_root=record.source_root,
        lane=int(record.parallel_lane),
        size=int(record.size),
        ext=str(record.ext).lower(),
        is_risky=is_problematic_media_path(record.path),
    )


def _select_ghetto_records(records: list[VideoRecord]) -> list[VideoRecord]:
    """Return the deterministic focused `ghetto` subset."""

    matches = [record for record in records if "ghetto" in str(record.path).casefold()]
    return matches[:24]


def _select_incomplete_records(records: list[VideoRecord]) -> list[VideoRecord]:
    """Return the deterministic incomplete-media subset."""

    incomplete = [
        record for record in records if "incomplete" in str(record.path).casefold()
    ]
    selected = incomplete if incomplete else records
    return selected[:120]


def _take_records(
    records: list[VideoRecord],
    *,
    limit: int,
    selected_paths: set[str],
) -> list[VideoRecord]:
    """Take up to `limit` records while respecting the selected-path set."""

    out: list[VideoRecord] = []
    for record in records:
        if record.path in selected_paths:
            continue
        out.append(record)
        selected_paths.add(record.path)
        if len(out) >= limit:
            break
    return out


def _select_mixed_records(records: list[VideoRecord]) -> list[VideoRecord]:
    """Return the deterministic mixed 240-file benchmark set."""

    by_root: dict[str, list[VideoRecord]] = {}
    for record in records:
        by_root.setdefault(record.source_root, []).append(record)
    selected_paths: set[str] = set()
    selected: list[VideoRecord] = []
    roots = sorted(by_root, key=str.casefold)

    for root in roots:
        risky = [
            record
            for record in by_root[root]
            if _sample_file_from_record(record).is_risky
        ]
        selected.extend(_take_records(risky, limit=80, selected_paths=selected_paths))
    for root in roots:
        normal = [
            record
            for record in by_root[root]
            if not _sample_file_from_record(record).is_risky
        ]
        selected.extend(_take_records(normal, limit=40, selected_paths=selected_paths))
    if len(selected) >= 240:
        return selected[:240]

    remaining = [record for record in records if record.path not in selected_paths]
    selected.extend(
        _take_records(
            remaining,
            limit=240 - len(selected),
            selected_paths=selected_paths,
        )
    )
    return selected


def _select_records_for_set(
    set_id: BenchmarkSetId,
    records: list[VideoRecord],
) -> list[VideoRecord]:
    """Choose the deterministic sample subset for the requested set id."""

    if set_id == "ghetto":
        return _select_ghetto_records(records)
    if set_id == "incomplete":
        return _select_incomplete_records(records)
    return _select_mixed_records(records)


def build_benchmark_sample_plan(
    roots: list[str],
    *,
    set_id: BenchmarkSetId,
    drive_worker_overrides: dict[str, int] | None = None,
) -> BenchmarkSamplePlan:
    """Enumerate candidate files and build one deterministic benchmark plan."""

    enumeration = enumerate_video_files(
        scan_id=0,
        roots=roots,
        extensions=list(_BENCHMARK_VIDEO_EXTENSIONS),
        max_workers=max(1, len(roots)),
        drive_worker_overrides=drive_worker_overrides,
    )
    records = enumeration.files
    sorted_records = _sorted_records(records)
    selected_records = _select_records_for_set(set_id, sorted_records)
    plan = build_physical_drive_scan_plan(
        roots=roots,
        max_workers=max(1, len(roots)),
        drive_worker_overrides=drive_worker_overrides,
    )
    lane_roots = {
        int(lane): [str(root) for root in group]
        for lane, group in enumerate(plan.root_groups)
    }
    return BenchmarkSamplePlan(
        set_id=set_id,
        files=[_sample_file_from_record(record) for record in selected_records],
        total_candidates=len(sorted_records),
        roots=[str(root) for root in roots],
        lane_roots=lane_roots,
    )


def _parse_provenance_metrics(
    provenance_json: str,
    decoder_backend: FrameDecodeBackendId,
) -> tuple[int, int]:
    """Extract fallback and timeout counts from one provenance payload."""

    if not provenance_json.strip():
        return (0, 0)
    try:
        payload_raw = json.loads(provenance_json)
    except json.JSONDecodeError:
        return (0, 0)
    if not isinstance(payload_raw, dict):
        return (0, 0)
    payload_map = cast("dict[object, object]", payload_raw)
    payload: dict[str, object] = {}
    for key_obj, value_obj in payload_map.items():
        if isinstance(key_obj, str | int | float | bool):
            payload[str(key_obj)] = value_obj
    attempts_raw = payload.get("attempts", [])
    if not isinstance(attempts_raw, list):
        return (0, 0)
    attempt_items = cast("list[object]", attempts_raw)
    attempts: list[dict[str, object]] = []
    for raw_item in attempt_items:
        if isinstance(raw_item, dict):
            attempts.append(cast("dict[str, object]", raw_item))
    timeout_count = sum(
        1
        for attempt in attempts
        if str(attempt.get("status", "")).strip().lower() == "timeout"
    )
    if not attempts:
        return (0, timeout_count)
    first_backend = str(attempts[0].get("decoder_backend", "")).strip().lower()
    fallback_count = 1 if first_backend and first_backend != decoder_backend else 0
    return (fallback_count, timeout_count)


def _run_one_file(
    sample: BenchmarkSampleFile,
    analyze_file: Callable[[str], AnalyzeOutputLike],
) -> BenchmarkFileResult:
    """Analyze one benchmark file and normalize its outcome."""

    started = time.perf_counter()
    try:
        output = analyze_file(sample.path)
    except Exception as exc:
        elapsed_s = max(0.0, time.perf_counter() - started)
        message = str(exc)
        if exc.__class__.__name__ == "ProbeError":
            status: BenchmarkFileStatus = "probe"
        elif exc.__class__.__name__ == "FingerprintError":
            status = "fingerprint"
        else:
            status = "analyze"
        return BenchmarkFileResult(
            path=sample.path,
            lane=sample.lane,
            status=status,
            elapsed_s=elapsed_s,
            fallback_count=0,
            decoder_timeout_count=0,
            fingerprint_decoder_backend=None,
            failure_message=message,
        )

    elapsed_s = max(0.0, time.perf_counter() - started)
    fallback_count, timeout_count = _parse_provenance_metrics(
        str(output.fingerprint_provenance_json),
        output.fingerprint_decoder_backend,
    )
    return BenchmarkFileResult(
        path=sample.path,
        lane=sample.lane,
        status="success",
        elapsed_s=elapsed_s,
        fallback_count=fallback_count,
        decoder_timeout_count=timeout_count,
        fingerprint_decoder_backend=output.fingerprint_decoder_backend,
    )


def _lane_order(plan: BenchmarkSamplePlan) -> list[int]:
    """Return the stable lane order for dispatch decisions."""

    return sorted({file.lane for file in plan.files})


def _select_ordered_lanes(
    ready_lanes: list[int],
    slots: int,
) -> list[int]:
    """Return the next lanes for the ordered dispatch strategy."""

    return ready_lanes[:slots]


def _select_micro_batch_lanes(
    ready_lanes: list[int],
    lane_order: list[int],
    *,
    start_index: int,
    slots: int,
) -> list[int]:
    """Return the next distinct-lane batch for the micro-batch strategy."""

    if not lane_order or not ready_lanes or slots <= 0:
        return []
    ready_set = set(ready_lanes)
    selected: list[int] = []
    for offset in range(len(lane_order)):
        lane = lane_order[(start_index + offset) % len(lane_order)]
        if lane not in ready_set:
            continue
        selected.append(lane)
        if len(selected) >= slots:
            break
    return selected


def _build_guidance(
    worker_limit: int,
    lane_metrics: list[BenchmarkLaneMetrics],
) -> BenchmarkWorkerGuidance:
    """Recommend a future worker limit from lane-pressure telemetry."""

    timeout_count = sum(metric.decoder_timeout_count for metric in lane_metrics)
    failure_count = sum(metric.failures for metric in lane_metrics)
    fallback_count = sum(metric.fallback_count for metric in lane_metrics)
    lane_count = max(1, len(lane_metrics))
    if failure_count > 0:
        recommended = max(1, min(worker_limit, lane_count))
        return BenchmarkWorkerGuidance(
            recommended_workers=recommended,
            reason="Failures detected; keep future runs bounded to distinct lanes.",
        )
    if timeout_count > 0 or fallback_count >= lane_count:
        recommended = max(1, min(worker_limit, lane_count))
        return BenchmarkWorkerGuidance(
            recommended_workers=recommended,
            reason="Lane pressure is visible; prefer one active file per lane.",
        )
    return BenchmarkWorkerGuidance(
        recommended_workers=max(1, worker_limit),
        reason="Current lane pressure looks healthy for the requested worker limit.",
    )


def _submit_benchmark_batch(
    *,
    executor: ThreadPoolExecutor,
    queues: dict[int, deque[BenchmarkSampleFile]],
    lane_order: list[int],
    active_lanes: set[int],
    futures: dict[Future[BenchmarkFileResult], int],
    analyze_file: Callable[[str], AnalyzeOutputLike],
    worker_limit: int,
    dispatch_strategy: BenchmarkDispatchStrategy,
    lane_cursor: int,
) -> int:
    """Submit the next ready file batch and return the updated lane cursor."""

    slots = worker_limit - len(futures)
    if slots <= 0:
        return lane_cursor
    ready = [
        lane for lane in lane_order if queues.get(lane) and lane not in active_lanes
    ]
    if dispatch_strategy == "micro_batch":
        selected_lanes = _select_micro_batch_lanes(
            ready,
            lane_order,
            start_index=lane_cursor,
            slots=slots,
        )
    else:
        selected_lanes = _select_ordered_lanes(ready, slots)
    updated_cursor = lane_cursor
    for lane in selected_lanes:
        queue = queues.get(lane)
        if not queue:
            continue
        sample = queue.popleft()
        future = executor.submit(_run_one_file, sample, analyze_file)
        futures[future] = lane
        active_lanes.add(lane)
        if dispatch_strategy == "micro_batch" and lane_order:
            updated_cursor = (lane_order.index(lane) + 1) % len(lane_order)
    return updated_cursor


def _drain_completed_benchmark_futures(
    futures: dict[Future[BenchmarkFileResult], int],
    active_lanes: set[int],
    results: list[BenchmarkFileResult],
) -> None:
    """Wait for one completion batch and retire those futures."""

    if not futures:
        return
    completed, _pending = wait(futures, return_when=FIRST_COMPLETED)
    for future in completed:
        lane = futures.pop(future)
        active_lanes.discard(lane)
        results.append(future.result())


def _build_lane_metrics(
    *,
    results: list[BenchmarkFileResult],
    lane_order: list[int],
    lane_roots: dict[int, list[str]],
) -> list[BenchmarkLaneMetrics]:
    """Aggregate per-lane service metrics from file-level benchmark results."""

    lane_metrics_map: dict[int, BenchmarkLaneMetrics] = {
        lane: BenchmarkLaneMetrics(lane=lane, roots=list(lane_roots.get(lane, [])))
        for lane in lane_order
    }
    decoder_mix: dict[int, Counter[str]] = {lane: Counter() for lane in lane_order}
    elapsed_by_lane: dict[int, list[float]] = {lane: [] for lane in lane_order}

    for result in results:
        lane_metric = lane_metrics_map.setdefault(
            result.lane,
            BenchmarkLaneMetrics(lane=result.lane, roots=[]),
        )
        lane_metric.files += 1
        if result.status == "success":
            lane_metric.successes += 1
            if result.fingerprint_decoder_backend is not None:
                decoder_mix[result.lane][result.fingerprint_decoder_backend] += 1
        else:
            lane_metric.failures += 1
        lane_metric.fallback_count += result.fallback_count
        lane_metric.decoder_timeout_count += result.decoder_timeout_count
        elapsed_by_lane[result.lane].append(result.elapsed_s)

    for lane, metric in lane_metrics_map.items():
        samples = elapsed_by_lane.get(lane, [])
        if samples:
            metric.average_service_s = sum(samples) / len(samples)
            metric.peak_service_s = max(samples)
        metric.decoder_mix = dict(decoder_mix.get(lane, Counter()))
    return [lane_metrics_map[lane] for lane in sorted(lane_metrics_map)]


def run_benchmark_plan(
    plan: BenchmarkSamplePlan,
    *,
    analyze_file: Callable[[str], AnalyzeOutputLike],
    requested_workers: int,
    dispatch_strategy: BenchmarkDispatchStrategy,
) -> BenchmarkRunSummary:
    """Execute one benchmark plan with lane-safe scheduling."""

    queues: dict[int, deque[BenchmarkSampleFile]] = {}
    for file in plan.files:
        queues.setdefault(file.lane, deque()).append(file)
    lane_order = _lane_order(plan)
    worker_limit = min(max(1, int(requested_workers)), max(1, len(lane_order)))
    lane_cursor = 0
    active_lanes: set[int] = set()
    futures: dict[Future[BenchmarkFileResult], int] = {}
    results: list[BenchmarkFileResult] = []
    started = time.perf_counter()

    with ThreadPoolExecutor(max_workers=worker_limit) as executor:
        while True:
            lane_cursor = _submit_benchmark_batch(
                executor=executor,
                queues=queues,
                lane_order=lane_order,
                active_lanes=active_lanes,
                futures=futures,
                analyze_file=analyze_file,
                worker_limit=worker_limit,
                dispatch_strategy=dispatch_strategy,
                lane_cursor=lane_cursor,
            )
            if not futures:
                break
            _drain_completed_benchmark_futures(futures, active_lanes, results)

    lane_metrics = _build_lane_metrics(
        results=results,
        lane_order=lane_order,
        lane_roots=plan.lane_roots,
    )
    guidance = _build_guidance(worker_limit, lane_metrics)
    return BenchmarkRunSummary(
        set_id=plan.set_id,
        dispatch_strategy=dispatch_strategy,
        requested_workers=max(1, int(requested_workers)),
        worker_limit=worker_limit,
        analyzed_files=len(results),
        success_count=sum(1 for result in results if result.status == "success"),
        failure_count=sum(1 for result in results if result.status != "success"),
        fallback_count=sum(result.fallback_count for result in results),
        decoder_timeout_count=sum(result.decoder_timeout_count for result in results),
        elapsed_s=max(0.0, time.perf_counter() - started),
        lane_metrics=lane_metrics,
        guidance=guidance,
    )


def _build_analyze_callable(
    probe_backend: ProbeBackendId,
) -> Callable[[str], AnalyzeOutputLike]:
    """Build the benchmark analyze callable for the selected probe backend."""

    return build_analyze_file(probe_backend)


def run_benchmark_evaluation(
    roots: list[str],
    *,
    set_id: BenchmarkSetId,
    probe_backend: ProbeBackendId,
    requested_workers: int,
    dispatch_strategy: BenchmarkDispatchStrategy,
    drive_worker_overrides: dict[str, int] | None = None,
) -> BenchmarkRunSummary:
    """Build and execute one full benchmark evaluation."""

    ensure_probe_backend_available(probe_backend)
    ensure_fingerprint_fallback_chain_available()
    plan = build_benchmark_sample_plan(
        roots,
        set_id=set_id,
        drive_worker_overrides=drive_worker_overrides,
    )
    analyze_file = _build_analyze_callable(probe_backend)
    return run_benchmark_plan(
        plan,
        analyze_file=analyze_file,
        requested_workers=requested_workers,
        dispatch_strategy=dispatch_strategy,
    )
