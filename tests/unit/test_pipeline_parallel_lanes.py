from __future__ import annotations

import time
from collections import defaultdict
from threading import Event, Lock
from types import SimpleNamespace

from video_duperz import pipeline
from video_duperz.db import Database
from video_duperz.fingerprint import ALGO_VERSION
from video_duperz.models import MatchStats, ScanProgress, VideoMeta, VideoRecord


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

    def upsert_file(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        ctime_ns: int,
        ext: str,
        scan_id: int,
    ) -> int:
        self._next_file_id += 1
        return self._next_file_id

    def load_cached_artifacts_batch(
        self,
        files: list[dict[str, object]],
        probe_backend: str = "pyav",
    ) -> dict[str, dict]:
        _ = files, probe_backend
        return {}

    def get_cached_artifacts(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        probe_backend: str = "pyav",
    ) -> dict:
        _ = path, size, mtime_ns, probe_backend
        return {}

    def save_probe_errors_batch(
        self,
        rows: list[tuple[int, str]],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        _ = rows, probe_backend
        return None

    def save_probe_error(
        self,
        file_id: int,
        text: str,
        probe_backend: str = "pyav",
    ) -> None:
        _ = file_id, text, probe_backend
        return None

    def save_video_meta_batch(
        self,
        rows: list[tuple[int, object]],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        _ = rows, probe_backend
        return None

    def save_video_meta(
        self,
        file_id: int,
        meta: object,
        probe_backend: str = "pyav",
    ) -> None:
        _ = file_id, meta, probe_backend
        return None

    def save_fingerprints_batch(
        self,
        rows: list[tuple[int, int, list[int]]],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        _ = rows, probe_backend
        return None

    def save_fingerprint(
        self,
        file_id: int,
        algo_version: int,
        hashes: list[int],
        probe_backend: str = "pyav",
    ) -> None:
        _ = file_id, algo_version, hashes, probe_backend
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

    def insert_duplicate_group(
        self, scan_id: int, profile: str, total_size_bytes: int
    ) -> int:
        return 1

    def insert_duplicate_groups_batch(
        self, scan_id: int, profile: str, groups: list[object]
    ) -> list[int]:
        return list(range(1, len(groups) + 1))

    def insert_duplicate_item(self, group_id: int, item: object) -> None:
        return None

    def complete_scan(self, scan_id: int, status: str = "done") -> None:
        self.status = status

    def load_duplicate_groups(self, scan_id: int) -> list:
        return []


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


def test_run_scan_pyav_backend_routes_probe_calls(monkeypatch) -> None:
    files = [_video("lane0-a.mp4", lane=0)]
    calls: list[object] = []

    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(files), [])
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline, "find_duplicate_edges", lambda items, profile: ([], MatchStats())
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )
    monkeypatch.setattr(
        pipeline,
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
        pipeline,
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

    monkeypatch.setattr(
        pipeline, "ensure_analyze_fallback_chain_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(files), [])
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline, "find_duplicate_edges", lambda items, profile: ([], MatchStats())
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )
    monkeypatch.setattr(
        pipeline,
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
    ):
        calls.append((backend, relaxed, path))
        if backend == "pyav":
            raise RuntimeError("pyav analyze failed")
        return VideoMeta(
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

    monkeypatch.setattr(pipeline, "probe_video", _fake_probe)

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

    monkeypatch.setattr(
        pipeline, "ensure_analyze_fallback_chain_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(files), [])
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline, "find_duplicate_edges", lambda items, profile: ([], MatchStats())
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )
    monkeypatch.setattr(
        pipeline,
        "probe_video",
        lambda path, *, backend="ffprobe", relaxed=False: VideoMeta(
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
        ),
    )
    monkeypatch.setattr(
        pipeline,
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

    monkeypatch.setattr(
        pipeline, "ensure_analyze_fallback_chain_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(files), [])
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 3, 1: 2, 2: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline, "find_duplicate_edges", lambda items, profile: ([], MatchStats())
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    lock = Lock()
    active_total = 0
    max_active_total = 0
    active_by_lane: dict[int, int] = defaultdict(int)
    max_active_by_lane: dict[int, int] = defaultdict(int)

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        nonlocal active_total, max_active_total
        lane = lane_by_path[path]
        with lock:
            active_total += 1
            max_active_total = max(max_active_total, active_total)
            active_by_lane[lane] += 1
            max_active_by_lane[lane] = max(
                max_active_by_lane[lane], active_by_lane[lane]
            )
        time.sleep(0.01)
        with lock:
            active_by_lane[lane] -= 1
            active_total -= 1
        return pipeline._AnalyzeOutput(
            meta=SimpleNamespace(duration_s=1.0), hashes=[11, 22, 33]
        )

    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    progress: list[ScanProgress] = []
    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
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
    telemetry_frames = [
        step
        for step in progress
        if step.stage in {"prepare", "probe"} and step.lane_snapshots
    ]
    assert telemetry_frames
    assert any((step.discovered_files_per_s or 0.0) >= 0.0 for step in telemetry_frames)
    assert any((step.discovered_mib_per_s or 0.0) >= 0.0 for step in telemetry_frames)
    assert any((step.analyzed_files_per_s or 0.0) >= 0.0 for step in telemetry_frames)
    assert any((step.analyzed_mib_per_s or 0.0) >= 0.0 for step in telemetry_frames)
    assert all((step.cache_hit_ratio or 0.0) >= 0.0 for step in telemetry_frames)
    assert all((step.cache_hit_ratio or 0.0) <= 1.0 for step in telemetry_frames)
    assert any((step.discovered_bytes or 0) > 0 for step in telemetry_frames)
    assert any(
        all(
            snapshot.discovered_files_per_s >= 0.0
            and snapshot.analyzed_files_per_s >= 0.0
            for snapshot in (step.lane_snapshots or [])
        )
        for step in telemetry_frames
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

    monkeypatch.setattr(
        pipeline, "ensure_analyze_fallback_chain_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(files), [])
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 3, 1: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline, "find_duplicate_edges", lambda items, profile: ([], MatchStats())
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    lock = Lock()
    active_total = 0
    max_active_total = 0
    active_by_lane: dict[int, int] = defaultdict(int)
    max_active_by_lane: dict[int, int] = defaultdict(int)

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        nonlocal active_total, max_active_total
        lane = lane_by_path[path]
        with lock:
            active_total += 1
            max_active_total = max(max_active_total, active_total)
            active_by_lane[lane] += 1
            max_active_by_lane[lane] = max(
                max_active_by_lane[lane], active_by_lane[lane]
            )
        time.sleep(0.01)
        with lock:
            active_by_lane[lane] -= 1
            active_total -= 1
        return pipeline._AnalyzeOutput(
            meta=SimpleNamespace(duration_s=1.0), hashes=[11, 22, 33]
        )

    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
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
    files = [
        _video("lane0-a.mp4", lane=0),
        _video("lane1-a.mp4", lane=1),
    ]

    monkeypatch.setattr(
        pipeline, "ensure_analyze_fallback_chain_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(files), [])
    )
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
        pipeline, "find_duplicate_edges", lambda items, profile: ([], MatchStats())
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )
    monkeypatch.setattr(
        pipeline,
        "_analyze_file",
        lambda path, cached_meta: pipeline._AnalyzeOutput(
            meta=SimpleNamespace(duration_s=1.0),
            hashes=[11, 22, 33],
        ),
    )

    progress: list[ScanProgress] = []
    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
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
    files = [
        _video("lane0-a.mp4", lane=0),
        _video("lane1-a.mp4", lane=1),
    ]
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

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_starts.append(time.perf_counter())
        time.sleep(0.01)
        return pipeline._AnalyzeOutput(
            meta=SimpleNamespace(duration_s=1.0), hashes=[11, 22, 33]
        )

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
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

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        time.sleep(0.01)
        return pipeline._AnalyzeOutput(
            meta=SimpleNamespace(duration_s=1.0), hashes=[11, 22, 33]
        )

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
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
    files = [
        _video("lane0-ok.mp4", lane=0),
        _video("lane1-stuck.mp4", lane=1),
    ]

    monkeypatch.setattr(
        pipeline, "ensure_analyze_fallback_chain_available", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(files), [])
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1, 1: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline, "find_duplicate_edges", lambda items, profile: ([], MatchStats())
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        _ = cached_meta
        if path.endswith("stuck.mp4"):
            time.sleep(1.6)
            return pipeline._AnalyzeOutput(
                meta=VideoMeta(
                    duration_s=9.0,
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
                hashes=[7, 8, 9],
            )
        time.sleep(0.05)
        return pipeline._AnalyzeOutput(
            meta=VideoMeta(
                duration_s=1.0,
                width=1280,
                height=720,
                fps=30.0,
                codec="h264",
                bitrate=500_000,
                has_audio=True,
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="",
                is_hdr=False,
            ),
            hashes=[1, 2, 3],
        )

    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

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

        time.sleep(0.8)
        match_items = db.list_match_items_for_scan(
            scan_id=result.scan_id,
            algo_version=ALGO_VERSION,
        )
        assert [item.path for item in match_items] == ["lane0-ok.mp4"]
