from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from threading import Event, Lock
from types import SimpleNamespace
from typing import TYPE_CHECKING

from video_duperz import analyze_process, pipeline
from video_duperz.analyze_process import AnalyzeOutput, AnalyzeProcessResult
from video_duperz.db import Database
from video_duperz.fingerprint import ALGO_VERSION
from video_duperz.models import MatchStats, ScanProgress, VideoMeta, VideoRecord

if TYPE_CHECKING:
    from collections.abc import Callable


class _FakeDb:
    def __init__(self) -> None:
        self._scan_id = 17
        self._next_file_id = 100
        self.status = "running"
        self._path_to_id: dict[str, int] = {}
        self.analysis_issue_rows: list[tuple[int, str, str, str]] = []
        self.deleted_analysis_issue_ids: list[tuple[int, str]] = []

    def create_scan(
        self,
        profile: str,
        roots: list[str],
        extensions: list[str] | None = None,
        probe_backend: str = "pyav",
    ) -> int:
        _ = profile, roots, extensions, probe_backend
        return self._scan_id

    def begin_scan_transaction(self) -> None:
        return None

    def flush_scan_transaction(self) -> None:
        return None

    def end_scan_transaction(self) -> None:
        return None

    def mark_missing_for_scan(self, scan_id: int, present_paths: set[str]) -> None:
        return None

    def upsert_files_batch(self, files: list[dict[str, object]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for file in files:
            path = str(file.get("path", ""))
            if not path:
                continue
            file_id = self._path_to_id.get(path)
            if file_id is None:
                self._next_file_id += 1
                file_id = self._next_file_id
                self._path_to_id[path] = file_id
            out[path] = file_id
        return out

    def load_cached_artifacts_batch(
        self,
        files: list[dict[str, object]],
        probe_backend: str = "pyav",
    ) -> dict[str, dict]:
        _ = files, probe_backend
        return {}

    def save_probe_errors_batch(
        self,
        rows: list[tuple[int, str]],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        _ = rows, probe_backend
        return None

    def save_video_meta_batch(
        self,
        rows: list[tuple[int, object]],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        _ = rows, probe_backend
        return None

    def save_fingerprints_batch(
        self,
        rows: list[tuple[int, int, list[int]]],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        _ = rows, probe_backend
        return None

    def save_analysis_issues_batch(
        self,
        rows: list[tuple[int, str, str]],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        for file_id, stage, message in rows:
            self.analysis_issue_rows.append(
                (int(file_id), str(probe_backend), str(stage), str(message))
            )

    def delete_analysis_issues_batch(
        self,
        file_ids: list[int],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        for file_id in file_ids:
            self.deleted_analysis_issue_ids.append((int(file_id), str(probe_backend)))

    def list_match_items_for_scan(self, scan_id: int, algo_version: int) -> list:
        return []

    def clear_duplicate_groups(self, scan_id: int) -> None:
        return None

    def insert_duplicate_groups_batch(
        self, scan_id: int, profile: str, groups: list[object]
    ) -> list[int]:
        _ = scan_id, profile
        return list(range(1, len(groups) + 1))

    def complete_scan(self, scan_id: int, status: str = "done") -> None:
        _ = scan_id
        self.status = status

    def load_duplicate_groups(self, scan_id: int) -> list:
        _ = scan_id
        return []


@dataclass(slots=True)
class _FakeAnalyzePlan:
    delay_s: float
    runner: Callable[[str, VideoMeta | None], AnalyzeProcessResult]
    ignore_stop: bool = False
    stop_delay_s: float = 0.01
    on_start: Callable[[str], None] | None = None
    on_finish: Callable[[str], None] | None = None


class _FakeAnalyzeHandle:
    def __init__(
        self,
        path: str,
        cached_meta: VideoMeta | None,
        plan: _FakeAnalyzePlan,
    ) -> None:
        self._path = path
        self._cached_meta = cached_meta
        self._plan = plan
        self._ready_at = time.perf_counter() + float(plan.delay_s)
        self._stop_requested_at: float | None = None
        self._killed = False
        self._collected: AnalyzeProcessResult | None = None
        if self._plan.on_start is not None:
            self._plan.on_start(self._path)

    def is_running(self) -> bool:
        return self._collected is None and not self._killed

    def request_stop(self) -> None:
        if self._stop_requested_at is None:
            self._stop_requested_at = time.perf_counter()

    def kill(self) -> None:
        if not self._killed:
            self._killed = True
            self._finish()

    def collect_result(self) -> AnalyzeProcessResult | None:
        if self._collected is not None:
            return self._collected
        now = time.perf_counter()
        if self._killed:
            return self._collected
        if (
            self._stop_requested_at is not None
            and not self._plan.ignore_stop
            and (now - self._stop_requested_at) >= float(self._plan.stop_delay_s)
        ):
            self._collected = AnalyzeProcessResult(
                kind="error",
                issue_stage="analyze",
                issue_message="Analyze child stop requested.",
            )
            self._finish()
            return self._collected
        if now < self._ready_at:
            return None
        self._collected = self._plan.runner(self._path, self._cached_meta)
        self._finish()
        return self._collected

    def _finish(self) -> None:
        if self._plan.on_finish is not None:
            self._plan.on_finish(self._path)


class _FakeAnalyzeLauncher:
    def __init__(
        self,
        *,
        default_plan: _FakeAnalyzePlan,
        path_plans: dict[str, _FakeAnalyzePlan] | None = None,
    ) -> None:
        self._default_plan = default_plan
        self._path_plans = dict(path_plans or {})

    def launch(self, path: str, cached_meta: VideoMeta | None) -> _FakeAnalyzeHandle:
        plan = self._path_plans.get(path, self._default_plan)
        return _FakeAnalyzeHandle(path, cached_meta, plan)


def _success_output(duration_s: float = 1.0) -> AnalyzeOutput:
    return AnalyzeOutput(
        meta=VideoMeta(
            duration_s=duration_s,
            width=1920,
            height=1080,
            fps=24.0,
            codec="h264",
            bitrate=1_000_000,
            has_audio=True,
            audio_codec="aac",
            audio_bitrate=128000,
            audio_languages="eng",
            subtitle_languages="",
            is_hdr=False,
        ),
        hashes=[1, 2, 3],
    )


def _success_result(duration_s: float = 1.0) -> AnalyzeProcessResult:
    return AnalyzeProcessResult(kind="success", output=_success_output(duration_s))


def _analyze_runner(
    probe_backend: str,
) -> Callable[[str, VideoMeta | None], AnalyzeProcessResult]:
    def _runner(path: str, cached_meta: VideoMeta | None) -> AnalyzeProcessResult:
        try:
            output = analyze_process.analyze_video_file(
                path,
                cached_meta,
                probe_backend="ffprobe" if probe_backend == "ffprobe" else "pyav",
            )
        except analyze_process.ProbeError as exc:
            return AnalyzeProcessResult(
                kind="error",
                issue_stage="probe",
                issue_message=str(exc),
            )
        except analyze_process.FingerprintError as exc:
            return AnalyzeProcessResult(
                kind="error",
                issue_stage="fingerprint",
                issue_message=str(exc),
            )
        except Exception as exc:
            return AnalyzeProcessResult(
                kind="error",
                issue_stage="analyze",
                issue_message=str(exc),
            )
        return AnalyzeProcessResult(kind="success", output=output)

    return _runner


def _video(path: str, lane: int) -> VideoRecord:
    return VideoRecord(
        path=path,
        size=10,
        mtime_ns=1,
        ctime_ns=1,
        ext="mp4",
        scan_id=17,
        source_root=f"root-{lane}",
        parallel_lane=lane,
    )


def _lane_plan_for_roots(
    roots: list[str],
    lane_worker_limits: dict[int, int],
    requested_worker_target: int | None = None,
) -> SimpleNamespace:
    root_groups: list[list[str]] = []
    root_to_group_index: dict[str, int] = {}
    for lane, root in enumerate(roots):
        root_groups.append([root])
        root_to_group_index[root] = lane
    effective_total_workers = sum(
        max(1, int(value)) for value in lane_worker_limits.values()
    )
    requested = (
        int(requested_worker_target)
        if requested_worker_target is not None
        else effective_total_workers
    )
    return SimpleNamespace(
        root_tokens=[],
        root_groups=root_groups,
        root_to_group_index=root_to_group_index,
        matched_volume_identities=set(),
        lane_worker_limits={
            int(k): max(1, int(v)) for k, v in lane_worker_limits.items()
        },
        lane_volume_identities={int(k): [] for k in lane_worker_limits},
        requested_worker_target=max(1, requested),
        effective_total_workers=effective_total_workers,
        issues=[],
    )


def _install_common_scan_monkeypatches(monkeypatch, files: list[VideoRecord]) -> None:
    monkeypatch.setattr(
        pipeline, "ensure_analyze_fallback_chain_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(files), [])
    )
    monkeypatch.setattr(
        pipeline,
        "find_duplicate_edges",
        lambda items, profile: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline,
        "build_duplicate_groups",
        lambda items, edges, profile: [],
    )


def test_run_scan_pyav_backend_routes_probe_calls(monkeypatch) -> None:
    files = [_video("lane0-a.mp4", lane=0)]
    calls: list[object] = []

    _install_common_scan_monkeypatches(monkeypatch, files)
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "create_analyze_launcher",
        lambda _backend: _FakeAnalyzeLauncher(
            default_plan=_FakeAnalyzePlan(
                delay_s=0.0,
                runner=_analyze_runner("pyav"),
            )
        ),
    )
    monkeypatch.setattr(
        analyze_process,
        "build_fingerprint_record_with_fallback",
        lambda **kwargs: SimpleNamespace(
            record=SimpleNamespace(hashes=[1, 2, 3]),
            fallback_decoder=None,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "ensure_analyze_fallback_chain_available",
        lambda backend="ffprobe": calls.append(("ensure", backend)),
    )
    monkeypatch.setattr(
        analyze_process,
        "probe_video",
        lambda path, *, backend="ffprobe", relaxed=False: (
            calls.append(("probe", backend, path))
            or SimpleNamespace(
                duration_s=1.0,
                width=1920,
                height=1080,
                fps=24.0,
                codec="h264",
                bitrate=1,
                has_audio=True,
                audio_codec="aac",
                audio_bitrate=1,
                audio_languages="eng",
                subtitle_languages="",
                is_hdr=False,
            )
        ),
    )

    result = pipeline.run_scan(
        db=_FakeDb(),
        roots=["root-0"],
        extensions=["mp4"],
        profile="balanced",
        max_workers=1,
        probe_backend="pyav",
        probe_worker_mode="balanced",
        db_batch_size=32,
        db_flush_interval_ms=50,
        enum_queue_max=256,
        progress_emit_interval_ms=50,
        progress_emit_every_files=10,
    )

    assert result.scanned_files == 1
    assert ("ensure", "pyav") in calls
    assert ("probe", "pyav", "lane0-a.mp4") in calls


def test_run_scan_records_probe_fallback_provenance(monkeypatch) -> None:
    files = [_video("lane0-a.mp4", lane=0)]
    calls: list[tuple[str, bool, str]] = []

    _install_common_scan_monkeypatches(monkeypatch, files)
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "create_analyze_launcher",
        lambda _backend: _FakeAnalyzeLauncher(
            default_plan=_FakeAnalyzePlan(
                delay_s=0.0,
                runner=_analyze_runner("pyav"),
            )
        ),
    )
    monkeypatch.setattr(
        analyze_process,
        "build_fingerprint_record_with_fallback",
        lambda **kwargs: SimpleNamespace(
            record=SimpleNamespace(hashes=[1, 2, 3]),
            fallback_decoder=None,
        ),
    )

    def _fake_probe(
        path: str,
        *,
        backend: str = "ffprobe",
        relaxed: bool = False,
    ) -> VideoMeta:
        calls.append((backend, relaxed, path))
        if backend == "pyav":
            raise RuntimeError("pyav analyze failed")
        return _success_output().meta

    monkeypatch.setattr(analyze_process, "probe_video", _fake_probe)

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,
        roots=["root-0"],
        extensions=["mp4"],
        profile="balanced",
        max_workers=1,
        probe_backend="pyav",
        probe_worker_mode="balanced",
        db_batch_size=32,
        db_flush_interval_ms=50,
        enum_queue_max=256,
        progress_emit_interval_ms=50,
        progress_emit_every_files=10,
    )

    assert result.fingerprinted_files == 1
    assert ("pyav", False, "lane0-a.mp4") in calls
    assert ("ffprobe", True, "lane0-a.mp4") in calls
    assert any(
        probe_backend == "pyav"
        and stage == "probe_fallback"
        and "ffprobe fallback succeeded" in message
        for _file_id, probe_backend, stage, message in db.analysis_issue_rows
    )


def test_run_scan_records_fingerprint_fallback_provenance(monkeypatch) -> None:
    files = [_video("lane0-a.mp4", lane=0)]

    _install_common_scan_monkeypatches(monkeypatch, files)
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "create_analyze_launcher",
        lambda _backend: _FakeAnalyzeLauncher(
            default_plan=_FakeAnalyzePlan(
                delay_s=0.0,
                runner=_analyze_runner("pyav"),
            )
        ),
    )
    monkeypatch.setattr(
        analyze_process, "probe_video", lambda *args, **kwargs: _success_output().meta
    )
    monkeypatch.setattr(
        analyze_process,
        "build_fingerprint_record_with_fallback",
        lambda **kwargs: SimpleNamespace(
            record=SimpleNamespace(hashes=[1, 2, 3]),
            fallback_decoder="ffmpeg",
        ),
    )

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,
        roots=["root-0"],
        extensions=["mp4"],
        profile="balanced",
        max_workers=1,
        probe_backend="pyav",
        probe_worker_mode="balanced",
        db_batch_size=32,
        db_flush_interval_ms=50,
        enum_queue_max=256,
        progress_emit_interval_ms=50,
        progress_emit_every_files=10,
    )

    assert result.fingerprinted_files == 1
    assert any(
        probe_backend == "pyav"
        and stage == "fingerprint_fallback"
        and "ffmpeg fallback succeeded" in message
        for _file_id, probe_backend, stage, message in db.analysis_issue_rows
    )


def test_run_scan_probe_parallel_lanes_and_telemetry(monkeypatch) -> None:
    files = [
        _video("lane0-a.mp4", lane=0),
        _video("lane0-b.mp4", lane=0),
        _video("lane1-a.mp4", lane=1),
        _video("lane1-b.mp4", lane=1),
        _video("lane2-a.mp4", lane=2),
        _video("lane2-b.mp4", lane=2),
    ]
    lane_by_path = {file.path: file.parallel_lane for file in files}

    _install_common_scan_monkeypatches(monkeypatch, files)
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 3, 1: 2, 2: 1},
        ),
    )

    lock = Lock()
    active_total = 0
    max_active_total = 0
    active_by_lane: dict[int, int] = defaultdict(int)
    max_active_by_lane: dict[int, int] = defaultdict(int)

    def _on_start(path: str) -> None:
        nonlocal active_total, max_active_total
        lane = lane_by_path[path]
        with lock:
            active_total += 1
            max_active_total = max(max_active_total, active_total)
            active_by_lane[lane] += 1
            max_active_by_lane[lane] = max(
                max_active_by_lane[lane], active_by_lane[lane]
            )

    def _on_finish(path: str) -> None:
        nonlocal active_total
        lane = lane_by_path[path]
        with lock:
            active_by_lane[lane] -= 1
            active_total -= 1

    monkeypatch.setattr(
        pipeline,
        "create_analyze_launcher",
        lambda _backend: _FakeAnalyzeLauncher(
            default_plan=_FakeAnalyzePlan(
                delay_s=0.01,
                runner=lambda _path, _cached: _success_result(),
                on_start=_on_start,
                on_finish=_on_finish,
            )
        ),
    )

    progress: list[ScanProgress] = []
    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,
        roots=["R:/A", "S:/B", "T:/C"],
        extensions=["mp4"],
        max_workers=2,
        probe_backend="ffprobe",
        probe_worker_mode="balanced",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
        progress_cb=progress.append,
    )

    assert result.scan_id == 17
    assert result.fingerprinted_files == 6
    assert max_active_total == 3
    assert all(count <= 1 for count in max_active_by_lane.values())
    assert any(
        step.worker_limit == 6
        for step in progress
        if step.stage in {"prepare", "probe"}
    )
    assert any(step.stage == "probe" for step in progress)
    assert any(
        step.worker_limit is not None and step.active_workers is not None
        for step in progress
    )
    assert any(
        step.lane_snapshots for step in progress if step.stage in {"prepare", "probe"}
    )


def test_run_scan_burst_mode_allows_multiple_workers_per_lane_up_to_caps(
    monkeypatch,
) -> None:
    files = [
        _video("lane0-a.mp4", lane=0),
        _video("lane0-b.mp4", lane=0),
        _video("lane0-c.mp4", lane=0),
        _video("lane0-d.mp4", lane=0),
        _video("lane0-e.mp4", lane=0),
        _video("lane1-a.mp4", lane=1),
        _video("lane1-b.mp4", lane=1),
    ]
    lane_by_path = {file.path: file.parallel_lane for file in files}

    _install_common_scan_monkeypatches(monkeypatch, files)
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 3, 1: 1},
        ),
    )

    lock = Lock()
    active_total = 0
    max_active_total = 0
    active_by_lane: dict[int, int] = defaultdict(int)
    max_active_by_lane: dict[int, int] = defaultdict(int)

    def _on_start(path: str) -> None:
        nonlocal active_total, max_active_total
        lane = lane_by_path[path]
        with lock:
            active_total += 1
            max_active_total = max(max_active_total, active_total)
            active_by_lane[lane] += 1
            max_active_by_lane[lane] = max(
                max_active_by_lane[lane], active_by_lane[lane]
            )

    def _on_finish(path: str) -> None:
        nonlocal active_total
        lane = lane_by_path[path]
        with lock:
            active_by_lane[lane] -= 1
            active_total -= 1

    monkeypatch.setattr(
        pipeline,
        "create_analyze_launcher",
        lambda _backend: _FakeAnalyzeLauncher(
            default_plan=_FakeAnalyzePlan(
                delay_s=0.01,
                runner=lambda _path, _cached: _success_result(),
                on_start=_on_start,
                on_finish=_on_finish,
            )
        ),
    )

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,
        roots=["R:/A", "S:/B"],
        extensions=["mp4"],
        max_workers=1,
        probe_backend="ffprobe",
        probe_worker_mode="burst",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
    )

    assert result.scan_id == 17
    assert result.fingerprinted_files == len(files)
    assert max_active_by_lane[0] >= 2
    assert max_active_by_lane[0] <= 3
    assert max_active_by_lane[1] <= 1
    assert max_active_total <= 4


def test_run_scan_reports_worker_capacity_reduction_when_hard_caps_apply(
    monkeypatch,
) -> None:
    files = [_video("lane0-a.mp4", lane=0), _video("lane1-a.mp4", lane=1)]

    _install_common_scan_monkeypatches(monkeypatch, files)
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1, 1: 1},
            requested_worker_target=6,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "create_analyze_launcher",
        lambda _backend: _FakeAnalyzeLauncher(
            default_plan=_FakeAnalyzePlan(
                delay_s=0.0,
                runner=lambda _path, _cached: _success_result(),
            )
        ),
    )

    progress: list[ScanProgress] = []
    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,
        roots=["R:/A", "S:/B"],
        extensions=["mp4"],
        max_workers=2,
        probe_backend="ffprobe",
        probe_worker_mode="burst",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
        progress_cb=progress.append,
    )

    assert any(
        issue.stage == "probe"
        and "Requested worker capacity (6) reduced to 2 by per-drive hard caps."
        in issue.message
        for issue in result.issues
    )
    reduction_messages = [
        step.message
        for step in progress
        if "reduced to 2 by per-drive hard caps" in step.message
    ]
    assert len(reduction_messages) == 1


def test_run_scan_streams_enumeration_into_analysis(monkeypatch) -> None:
    files = [_video("lane0-a.mp4", lane=0), _video("lane1-a.mp4", lane=1)]
    timeline: dict[str, float] = {}
    analyze_starts: list[float] = []

    monkeypatch.setattr(
        pipeline, "ensure_analyze_fallback_chain_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pipeline, "find_duplicate_edges", lambda items, profile: ([], MatchStats())
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_enumerate(**kwargs):
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        timeline["enum_start"] = time.perf_counter()
        on_file_discovered(files[0])
        time.sleep(0.03)
        on_file_discovered(files[1])
        time.sleep(0.03)
        timeline["enum_end"] = time.perf_counter()
        return list(files), []

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(
        pipeline,
        "create_analyze_launcher",
        lambda _backend: _FakeAnalyzeLauncher(
            default_plan=_FakeAnalyzePlan(
                delay_s=0.01,
                runner=lambda _path, _cached: _success_result(),
                on_start=lambda _path: analyze_starts.append(time.perf_counter()),
            )
        ),
    )

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,
        roots=["R:/A", "S:/B"],
        extensions=["mp4"],
        max_workers=2,
        probe_backend="ffprobe",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
    )

    assert result.fingerprinted_files == 2
    assert analyze_starts
    assert timeline["enum_end"] > min(analyze_starts)


def test_run_scan_cancellation_during_streaming_overlap(monkeypatch) -> None:
    files = [
        _video("lane0-a.mp4", lane=0),
        _video("lane0-b.mp4", lane=0),
        _video("lane1-a.mp4", lane=1),
    ]
    cancel_event = Event()

    monkeypatch.setattr(
        pipeline, "ensure_analyze_fallback_chain_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pipeline, "find_duplicate_edges", lambda items, profile: ([], MatchStats())
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_enumerate(**kwargs):
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        local_cancel = kwargs.get("cancel_event")
        for idx, item in enumerate(files):
            if local_cancel and local_cancel.is_set():
                break
            on_file_discovered(item)
            if idx == 0:
                cancel_event.set()
            time.sleep(0.005)
        return list(files), []

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(
        pipeline,
        "create_analyze_launcher",
        lambda _backend: _FakeAnalyzeLauncher(
            default_plan=_FakeAnalyzePlan(
                delay_s=0.2,
                runner=lambda _path, _cached: _success_result(),
            )
        ),
    )

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,
        roots=["R:/A", "S:/B"],
        extensions=["mp4"],
        max_workers=2,
        probe_backend="ffprobe",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
        cancel_event=cancel_event,
    )

    assert result.scan_id == 17
    assert db.status == "cancelled"


def test_run_scan_marks_timed_out_analysis_for_manual_review(
    tmp_path,
    monkeypatch,
) -> None:
    files = [_video("lane0-ok.mp4", lane=0), _video("lane1-stuck.mp4", lane=1)]

    _install_common_scan_monkeypatches(monkeypatch, files)
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1, 1: 1},
        ),
    )
    monkeypatch.setattr(
        "video_duperz.pipeline_runtime_context.ANALYZE_STOP_GRACE_S", 0.05
    )
    monkeypatch.setattr(
        pipeline,
        "create_analyze_launcher",
        lambda _backend: _FakeAnalyzeLauncher(
            default_plan=_FakeAnalyzePlan(
                delay_s=0.05,
                runner=lambda _path, _cached: _success_result(),
            ),
            path_plans={
                "lane1-stuck.mp4": _FakeAnalyzePlan(
                    delay_s=10.0,
                    runner=lambda _path, _cached: _success_result(duration_s=9.0),
                    ignore_stop=True,
                )
            },
        ),
    )

    with Database(tmp_path / "timeout.db") as db:
        started = time.perf_counter()
        result = pipeline.run_scan(
            db=db,
            roots=["R:/A", "S:/B"],
            extensions=["mp4"],
            max_workers=2,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            analysis_timeout_s=1,
            db_batch_size=64,
            db_flush_interval_ms=50,
            enum_queue_max=512,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
        )
        elapsed = time.perf_counter() - started

        assert elapsed < 1.5
        assert result.scanned_files == 2
        assert result.fingerprinted_files == 1
        assert any(
            issue.stage == "analyze_timeout"
            and "analysis timeout after 1s; manual review required" in issue.message
            for issue in result.issues
        )

        persisted = db.list_analysis_issues_for_scan(result.scan_id)
        assert len(persisted) == 1
        assert persisted[0]["path"] == "lane1-stuck.mp4"
        assert persisted[0]["stage"] == "analyze_timeout"
        assert "manual review required" in persisted[0]["message"]

        match_items = db.list_match_items_for_scan(
            scan_id=result.scan_id,
            algo_version=ALGO_VERSION,
        )
        assert [item.path for item in match_items] == ["lane0-ok.mp4"]
