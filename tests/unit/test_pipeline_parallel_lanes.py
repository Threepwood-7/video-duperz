from __future__ import annotations

import time
from collections import defaultdict
from threading import Event, Lock
from types import SimpleNamespace

from threep_commons.fs_paths import path_key

from video_duperz import pipeline
from video_duperz.db import Database
from video_duperz.fingerprint import ALGO_VERSION
from video_duperz.models import (
    MatchStats,
    ScanIssue,
    ScanProgress,
    VideoMeta,
    VideoRecord,
)
from video_duperz.scan_sets import build_scan_set_key


class _FakeDb:
    def __init__(self) -> None:
        self._scan_id = 17
        self._next_file_id = 100
        self.status = "running"
        self._path_to_id: dict[tuple[int, str], int] = {}
        self.latest_completed_by_set: dict[str, int] = {}
        self.scan_snapshots: dict[int, dict[str, dict[str, int | str]]] = {}
        self.cloned_paths: list[str] = []
        self.cloned_failed_paths: list[str] = []

    def create_scan(
        self,
        profile: str,
        roots: list[str],
        extensions: list[str] | None = None,
        custom_similarity_threshold: float = 0.18,
        scene_aware_sampling: bool = False,
        audio_fingerprint_enabled: bool = False,
        cross_resolution_mode: str = "off",
        probe_backend: str = "pyav",
    ) -> int:
        _ = (
            profile,
            roots,
            extensions,
            custom_similarity_threshold,
            scene_aware_sampling,
            audio_fingerprint_enabled,
            cross_resolution_mode,
            probe_backend,
        )
        return self._scan_id

    def update_scan_definition(
        self,
        scan_id: int,
        *,
        profile: str,
        roots: list[str],
        extensions: list[str] | None = None,
        custom_similarity_threshold: float = 0.18,
        scene_aware_sampling: bool = False,
        audio_fingerprint_enabled: bool = False,
        cross_resolution_mode: str = "off",
        probe_backend: str = "pyav",
        status: str | None = None,
    ) -> None:
        _ = (
            scan_id,
            profile,
            roots,
            extensions,
            custom_similarity_threshold,
            scene_aware_sampling,
            audio_fingerprint_enabled,
            cross_resolution_mode,
            probe_backend,
            status,
        )
        return None

    def begin_scan_transaction(self) -> None:
        return None

    def flush_scan_transaction(self) -> None:
        return None

    def end_scan_transaction(self) -> None:
        return None

    def mark_missing_for_scan(self, scan_id: int, present_paths: set[str]) -> None:
        return None

    def delete_scan_links_for_scan(self, scan_id: int) -> None:
        return None

    def upsert_files_batch(self, files: list[dict[str, object]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for file in files:
            path = str(file.get("path", ""))
            if not path:
                continue
            scan_id = int(file.get("scan_id", 0))
            key = (scan_id, path_key(path))
            file_id = self._path_to_id.get(key)
            if file_id is None:
                self._next_file_id += 1
                file_id = self._next_file_id
                self._path_to_id[key] = file_id
            out[path] = file_id
        return out

    def upsert_scan_links_batch(self, links: list[object]) -> None:
        _ = links
        return None

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
        self._path_to_id[(scan_id, path_key(path))] = self._next_file_id
        return self._next_file_id

    def latest_completed_scan_id_for_set(self, scan_set_key: str) -> int | None:
        return self.latest_completed_by_set.get(scan_set_key)

    def load_scan_file_snapshot(
        self,
        scan_id: int,
    ) -> dict[str, dict[str, int | str]]:
        snapshot = self.scan_snapshots.get(scan_id, {})
        return {
            path_key(path): {
                **row,
                "normalized_path": str(row.get("normalized_path", path_key(path))),
            }
            for path, row in snapshot.items()
        }

    def clone_scan_files_with_artifacts(
        self,
        *,
        source_scan_id: int,
        target_scan_id: int,
        paths: set[str],
        probe_backend: str = "pyav",
        algo_version: int,
        include_audio_fingerprint: bool = False,
    ) -> dict[str, int]:
        _ = source_scan_id, probe_backend, algo_version, include_audio_fingerprint
        out: dict[str, int] = {}
        for path in sorted(paths):
            self.cloned_paths.append(path)
            self._next_file_id += 1
            self._path_to_id[(target_scan_id, path_key(path))] = self._next_file_id
            out[path] = self._next_file_id
        return out

    def clone_failed_files_for_paths(
        self,
        *,
        source_scan_id: int,
        target_scan_id: int,
        paths: set[str],
    ) -> int:
        _ = source_scan_id, target_scan_id
        self.cloned_failed_paths.extend(sorted(paths))
        return len(paths)

    def load_cached_artifacts_batch(
        self,
        files: list[dict[str, object]],
        *,
        algo_version: int = 1,
        include_audio_fingerprint: bool = False,
        probe_backend: str = "pyav",
    ) -> dict[str, dict]:
        _ = files, algo_version, include_audio_fingerprint, probe_backend
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
        rows: list[tuple[int, int, int, str]],
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
        rows: list[tuple[int, int, int, object]],
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
        rows: list[tuple[int, int, int, int, list[int]]],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        _ = rows, probe_backend
        return None

    def save_fingerprint_provenance_batch(
        self,
        rows: list[tuple[int, int, str, str]],
        *,
        probe_backend: str = "pyav",
    ) -> None:
        _ = rows, probe_backend
        return None

    def save_audio_fingerprints_batch(
        self,
        rows: list[tuple[int, int, int, str]],
    ) -> None:
        _ = rows
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


def _video_with_stats(
    path: str,
    lane: int,
    *,
    size: int = 10,
    mtime_ns: int = 1,
) -> VideoRecord:
    return VideoRecord(
        path=path,
        size=size,
        mtime_ns=mtime_ns,
        ctime_ns=mtime_ns,
        ext="mp4",
        scan_id=17,
        source_root=f"root-{lane}",
        parallel_lane=lane,
    )


def _analysis_meta() -> VideoMeta:
    return VideoMeta(
        duration_s=1.0,
        width=1920,
        height=1080,
        fps=24.0,
        codec="h264",
        bitrate=1000,
        audio_stream_count=1,
        audio_codec="aac",
        audio_bitrate=128000,
        audio_languages="eng",
        subtitle_languages="",
        hdr_format="",
    )


def _analysis_timestamps(
    db: Database,
    path: str,
    *,
    probe_backend: str = "ffprobe",
) -> tuple[str, str]:
    row = db.conn.execute(
        """
        SELECT vm.probed_at, fp.created_at
        FROM files f
        JOIN video_meta vm
          ON vm.file_id = f.id AND vm.probe_backend = ?
        JOIN fingerprints fp
          ON fp.file_id = f.id AND fp.probe_backend = ?
        WHERE f.path = ?
        """,
        (probe_backend, probe_backend, path),
    ).fetchone()
    assert row is not None
    return (str(row["probed_at"]), str(row["created_at"]))


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
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )
    monkeypatch.setattr(
        pipeline,
        "build_fingerprint_record_with_fallback",
        lambda **kwargs: SimpleNamespace(
            record=SimpleNamespace(hashes=[1, 2, 3]),
            decoder_backend="opencv",
            provenance_json="{}",
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "ensure_probe_backend_available",
        lambda backend="ffprobe": calls.append(("ensure", backend)),
    )
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "probe_video",
        lambda path, *, backend="ffprobe": (
            calls.append(("probe", backend, path))
            or SimpleNamespace(
                duration_s=1.0,
                width=1920,
                height=1080,
                fps=24.0,
                codec="h264",
                bitrate=1,
                audio_stream_count=1,
                audio_codec="aac",
                audio_bitrate=1,
                audio_languages="eng",
                subtitle_languages="",
                hdr_format="",
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

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
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
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
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
    assert max_active_total >= 1
    assert max_active_total <= 3
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
    assert any(
        any(snapshot.active_file for snapshot in (step.lane_snapshots or []))
        for step in progress
        if step.stage in {"running", "fingerprint", "probe"}
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

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
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
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
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

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
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
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
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


def test_run_scan_waits_for_enumeration_before_analysis_to_preserve_order(
    monkeypatch,
) -> None:
    files = [
        _video("lane0-a.mp4", lane=0),
        _video("lane1-a.mp4", lane=1),
    ]
    timeline: dict[str, float] = {}
    analyze_starts: list[float] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
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
    assert min(analyze_starts) >= timeline["enum_end"]


def test_run_scan_dispatches_in_lane_local_alpha_order_across_roots(
    monkeypatch,
) -> None:
    files = [
        _video("z-root/zeta.mp4", lane=2),
        _video("a-root/alpha.mp4", lane=0),
        _video("a-root/bravo.mp4", lane=0),
        _video("m-root/mike.mp4", lane=1),
    ]
    analyze_calls: list[str] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1, 1: 1, 2: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(files), [])
    )
    monkeypatch.setattr(
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append(path)
        time.sleep(0.01)
        return pipeline._AnalyzeOutput(
            meta=SimpleNamespace(duration_s=1.0),
            hashes=[11, 22, 33],
        )

    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
        roots=["A:/a-root", "M:/m-root", "Z:/z-root"],
        extensions=["mp4"],
        max_workers=2,
        probe_backend="ffprobe",
        probe_worker_mode="balanced",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
    )

    expected_order = [
        "a-root/alpha.mp4",
        "m-root/mike.mp4",
        "z-root/zeta.mp4",
        "a-root/bravo.mp4",
    ]
    assert result.fingerprinted_files == 4
    assert analyze_calls == expected_order


def test_run_scan_completion_can_finish_out_of_order_while_dispatch_stays_sorted(
    monkeypatch,
) -> None:
    files = [
        _video("a-root/alpha.mp4", lane=0),
        _video("b-root/bravo.mp4", lane=1),
    ]
    analyze_calls: list[str] = []
    completed_paths: list[str] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
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
        pipeline, "enumerate_video_files", lambda **kwargs: (list(reversed(files)), [])
    )
    monkeypatch.setattr(
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append(path)
        if path.endswith("alpha.mp4"):
            time.sleep(0.03)
        else:
            time.sleep(0.005)
        completed_paths.append(path)
        return pipeline._AnalyzeOutput(
            meta=SimpleNamespace(duration_s=1.0),
            hashes=[11, 22, 33],
        )

    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    db = _FakeDb()
    pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
        roots=["A:/a-root", "B:/b-root"],
        extensions=["mp4"],
        max_workers=2,
        probe_backend="ffprobe",
        probe_worker_mode="balanced",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
    )

    assert analyze_calls == ["a-root/alpha.mp4", "b-root/bravo.mp4"]
    assert completed_paths == ["b-root/bravo.mp4", "a-root/alpha.mp4"]


def test_run_scan_burst_mode_preserves_lane_fifo_order(monkeypatch) -> None:
    files = [
        _video("lane0/charlie.mp4", lane=0),
        _video("lane1/delta.mp4", lane=1),
        _video("lane0/alpha.mp4", lane=0),
        _video("lane0/bravo.mp4", lane=0),
    ]
    analyze_calls: list[str] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 2, 1: 1},
        ),
    )
    monkeypatch.setattr(
        pipeline, "enumerate_video_files", lambda **kwargs: (list(reversed(files)), [])
    )
    monkeypatch.setattr(
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append(path)
        time.sleep(0.01)
        return pipeline._AnalyzeOutput(
            meta=SimpleNamespace(duration_s=1.0),
            hashes=[11, 22, 33],
        )

    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    db = _FakeDb()
    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
        roots=["L:/lane0", "M:/lane1"],
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

    assert result.fingerprinted_files == len(files)
    assert analyze_calls == [
        "lane0/alpha.mp4",
        "lane1/delta.mp4",
        "lane0/bravo.mp4",
        "lane0/charlie.mp4",
    ]


def test_run_scan_cancellation_during_streaming_overlap(monkeypatch) -> None:
    files = [
        _video("lane0-a.mp4", lane=0),
        _video("lane0-b.mp4", lane=0),
        _video("lane1-a.mp4", lane=1),
    ]
    cancel_event = Event()

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
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


def test_run_scan_pause_and_resume_keeps_same_scan_id(
    tmp_path,
    monkeypatch,
) -> None:
    files = [
        _video(str(tmp_path / "lane0-a.mp4"), lane=0),
        _video(str(tmp_path / "lane0-b.mp4"), lane=0),
    ]
    pause_event = Event()

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
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
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_enumerate(**kwargs):
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        on_file_discovered(files[0])
        time.sleep(0.01)
        on_file_discovered(files[1])
        return list(files), []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        _ = path, cached_meta
        time.sleep(0.02)
        return pipeline._AnalyzeOutput(
            meta=VideoMeta(
                duration_s=1.0,
                width=1920,
                height=1080,
                fps=24.0,
                codec="h264",
                bitrate=1000,
                audio_stream_count=1,
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="",
                hdr_format="",
            ),
            hashes=[11, 22, 33],
        )

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    def _pause_after_first_probe(step: ScanProgress) -> None:
        if step.stage == "probe" and step.current >= 1:
            pause_event.set()

    with Database(tmp_path / "app.db") as db:
        paused = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_first_probe,
        )

        assert db.scan_summary(paused.scan_id)["status"] == "paused"
        pause_event.clear()

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
        )

        assert resumed.scan_id == paused.scan_id
        assert db.scan_summary(paused.scan_id)["status"] == "done"
        assert resumed.cached_files >= 1


def test_resume_skips_unchanged_files_and_preserves_analysis_timestamps(
    tmp_path,
    monkeypatch,
) -> None:
    first = _video_with_stats(str(tmp_path / "cached.mp4"), lane=0, mtime_ns=11)
    second = _video_with_stats(str(tmp_path / "pending.mp4"), lane=0, mtime_ns=22)
    current_files = [first, second]
    enumerate_round = 0
    pause_event = Event()
    analyze_calls: list[tuple[str, bool]] = []
    resumed_progress: list[ScanProgress] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )

    def _fake_enumerate(**kwargs):
        nonlocal enumerate_round
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        enumerate_round += 1
        emitted = [first] if enumerate_round == 1 else list(current_files)
        for item in emitted:
            on_file_discovered(item)
        return emitted, []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append((path, cached_meta is not None))
        time.sleep(0.01)
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[11, 22, 33, 44])

    def _pause_after_first_probe(step: ScanProgress) -> None:
        if step.stage == "probe" and step.current >= 1:
            pause_event.set()

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    with Database(tmp_path / "resume-cache.db") as db:
        paused = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_first_probe,
        )

        assert paused.scan_id > 0
        assert analyze_calls == [(first.path, False)]
        first_timestamps_before = _analysis_timestamps(db, first.path)

        pause_event.clear()
        analyze_calls.clear()

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
            progress_cb=resumed_progress.append,
        )

        assert resumed.scan_id == paused.scan_id
        assert analyze_calls == [(second.path, False)]
        assert resumed.metrics["resume_cache_hits"] == 1
        assert resumed.metrics["resume_reprocessed_files"] == 1
        assert _analysis_timestamps(db, first.path) == first_timestamps_before
        assert any(
            step.stage == "cache"
            and step.work_kind == "cache_hit"
            and step.subject_path == first.path
            and step.completed_files == 1
            for step in resumed_progress
        )
        assert any(
            step.stage == "probe"
            and step.work_kind == "probe_and_fingerprint"
            and step.subject_path == second.path
            and step.completed_files == 2
            for step in resumed_progress
        )


def test_resume_dispatch_order_remains_lane_local_alpha(monkeypatch, tmp_path) -> None:
    alpha = _video_with_stats(str(tmp_path / "alpha.mp4"), lane=0, mtime_ns=11)
    bravo = _video_with_stats(str(tmp_path / "bravo.mp4"), lane=0, mtime_ns=22)
    zulu = _video_with_stats(str(tmp_path / "zulu.mp4"), lane=0, mtime_ns=33)
    current_files = [zulu, alpha, bravo]
    enumerate_round = 0
    pause_event = Event()
    analyze_calls: list[str] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )

    def _fake_enumerate(**kwargs):
        nonlocal enumerate_round
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        enumerate_round += 1
        emitted = [alpha] if enumerate_round == 1 else list(current_files)
        for item in emitted:
            on_file_discovered(item)
        return emitted, []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append(path)
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[11, 22, 33, 44])

    def _pause_after_first_probe(step: ScanProgress) -> None:
        if step.stage == "probe" and step.current >= 1:
            pause_event.set()

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    with Database(tmp_path / "resume-order.db") as db:
        paused = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_first_probe,
        )

        assert analyze_calls == [str(tmp_path / "alpha.mp4")]
        pause_event.clear()
        analyze_calls.clear()

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
        )

        assert resumed.scan_id == paused.scan_id
        assert analyze_calls == [
            str(tmp_path / "bravo.mp4"),
            str(tmp_path / "zulu.mp4"),
        ]


def test_resume_with_additive_roots_merges_new_files_into_lane_local_alpha_order(
    monkeypatch,
    tmp_path,
) -> None:
    original_root = tmp_path / "orig"
    added_root = tmp_path / "added"
    alpha = _video_with_stats(str(added_root / "alpha.mp4"), lane=1, mtime_ns=11)
    bravo = _video_with_stats(str(added_root / "bravo.mp4"), lane=1, mtime_ns=12)
    charlie = _video_with_stats(str(original_root / "charlie.mp4"), lane=0, mtime_ns=13)
    delta = _video_with_stats(str(original_root / "delta.mp4"), lane=0, mtime_ns=14)
    enumerate_round = 0
    pause_event = Event()
    analyze_calls: list[str] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1, 1: 1},
        ),
    )

    def _fake_enumerate(**kwargs):
        nonlocal enumerate_round
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        roots = list(kwargs.get("roots", []))
        enumerate_round += 1
        if enumerate_round == 1:
            emitted = [charlie]
        else:
            emitted = (
                [delta, charlie] if len(roots) == 1 else [delta, charlie, bravo, alpha]
            )
        for item in emitted:
            on_file_discovered(item)
        return emitted, []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append(path)
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[11, 22, 33, 44])

    def _pause_after_first_probe(step: ScanProgress) -> None:
        if step.stage == "probe" and step.current >= 1:
            pause_event.set()

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    with Database(tmp_path / "resume-additive.db") as db:
        paused = pipeline.run_scan(
            db=db,
            roots=[str(original_root)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_first_probe,
        )

        assert analyze_calls == [str(original_root / "charlie.mp4")]
        pause_event.clear()
        analyze_calls.clear()

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(original_root), str(added_root)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
        )

        assert resumed.scan_id == paused.scan_id
        assert analyze_calls == [
            str(original_root / "delta.mp4"),
            str(added_root / "alpha.mp4"),
            str(added_root / "bravo.mp4"),
        ]


def test_resume_reprocesses_when_file_stat_changes(
    tmp_path,
    monkeypatch,
) -> None:
    file_path = str(tmp_path / "changed.mp4")
    current_file = _video_with_stats(file_path, lane=0, size=10, mtime_ns=11)
    pause_event = Event()
    analyze_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )

    def _fake_enumerate(**kwargs):
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        on_file_discovered(current_file)
        return [current_file], []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append((path, cached_meta is not None))
        time.sleep(0.01)
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[11, 22, 33, 44])

    def _pause_after_first_probe(step: ScanProgress) -> None:
        if step.stage == "probe" and step.current >= 1:
            pause_event.set()

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    with Database(tmp_path / "resume-changed.db") as db:
        paused = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_first_probe,
        )

        initial_timestamps = _analysis_timestamps(db, file_path)
        pause_event.clear()
        analyze_calls.clear()
        time.sleep(0.01)
        current_file = _video_with_stats(file_path, lane=0, size=10, mtime_ns=22)

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
        )

        assert resumed.scan_id == paused.scan_id
        assert analyze_calls == [(file_path, False)]
        assert resumed.metrics["resume_cache_hits"] == 0
        assert resumed.metrics["resume_reprocessed_files"] == 1
        assert _analysis_timestamps(db, file_path) != initial_timestamps


def test_resume_reprocesses_when_file_size_changes(
    tmp_path,
    monkeypatch,
) -> None:
    file_path = str(tmp_path / "changed-size.mp4")
    current_file = _video_with_stats(file_path, lane=0, size=10, mtime_ns=11)
    pause_event = Event()
    analyze_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )

    def _fake_enumerate(**kwargs):
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        on_file_discovered(current_file)
        return [current_file], []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append((path, cached_meta is not None))
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[11, 22, 33, 44])

    def _pause_after_first_probe(step: ScanProgress) -> None:
        if step.stage == "probe" and step.current >= 1:
            pause_event.set()

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    with Database(tmp_path / "resume-size.db") as db:
        paused = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_first_probe,
        )

        initial_timestamps = _analysis_timestamps(db, file_path)
        pause_event.clear()
        analyze_calls.clear()
        time.sleep(0.01)
        current_file = _video_with_stats(file_path, lane=0, size=99, mtime_ns=11)

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
        )

        assert resumed.scan_id == paused.scan_id
        assert analyze_calls == [(file_path, False)]
        assert resumed.metrics["resume_cache_hits"] == 0
        assert resumed.metrics["resume_reprocessed_files"] == 1
        assert _analysis_timestamps(db, file_path) != initial_timestamps


def test_resume_reprocesses_partial_cached_state_without_reprobing(
    tmp_path,
    monkeypatch,
) -> None:
    file_path = str(tmp_path / "partial.mp4")
    record = _video_with_stats(file_path, lane=0, size=10, mtime_ns=11)

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )

    def _fake_enumerate(**kwargs):
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        on_file_discovered(record)
        return [record], []

    analyze_calls: list[tuple[str, bool]] = []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append((path, cached_meta is not None))
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[11, 22, 33, 44])

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)
    progress_steps: list[ScanProgress] = []

    with Database(tmp_path / "resume-partial.db") as db:
        scan_id = db.create_scan(
            profile="balanced",
            roots=[str(tmp_path)],
            extensions=["mp4"],
            probe_backend="ffprobe",
        )
        file_id = db.upsert_file(
            path=file_path,
            size=10,
            mtime_ns=11,
            ctime_ns=11,
            ext="mp4",
            scan_id=scan_id,
        )
        db.save_video_meta(file_id, _analysis_meta(), probe_backend="ffprobe")
        probe_timestamp_before = db.conn.execute(
            """
            SELECT probed_at
            FROM video_meta
            WHERE file_id = ? AND probe_backend = 'ffprobe'
            """,
            (file_id,),
        ).fetchone()
        assert probe_timestamp_before is not None
        db.complete_scan(scan_id, status="paused")

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=scan_id,
            progress_cb=progress_steps.append,
        )

        probe_timestamp_after = db.conn.execute(
            """
            SELECT probed_at
            FROM video_meta
            WHERE file_id = ? AND probe_backend = 'ffprobe'
            """,
            (file_id,),
        ).fetchone()
        fingerprint_row = db.conn.execute(
            """
            SELECT created_at
            FROM fingerprints
            WHERE file_id = ? AND probe_backend = 'ffprobe'
            """,
            (file_id,),
        ).fetchone()
        assert resumed.scan_id == scan_id
        assert analyze_calls == [(file_path, True)]
        assert resumed.metrics["resume_cache_hits"] == 0
        assert resumed.metrics["resume_reprocessed_files"] == 1
        assert probe_timestamp_after is not None
        assert fingerprint_row is not None
        assert str(probe_timestamp_before["probed_at"]) == str(
            probe_timestamp_after["probed_at"]
        )
        assert any(
            step.stage == "fingerprint"
            and step.work_kind == "fingerprint_only"
            and step.subject_path == file_path
            for step in progress_steps
        )
        assert not any(
            step.stage == "probe" and step.subject_path == file_path
            for step in progress_steps
        )


def test_resume_can_skip_previously_failed_files(
    tmp_path,
    monkeypatch,
) -> None:
    failed_path = str(tmp_path / "failed.mp4")
    good_path = str(tmp_path / "good.mp4")
    failed_record = _video_with_stats(failed_path, lane=0, size=10, mtime_ns=11)
    good_record = _video_with_stats(good_path, lane=0, size=20, mtime_ns=22)
    analyze_calls: list[str] = []
    progress_steps: list[ScanProgress] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )

    def _fake_enumerate(**kwargs):
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        on_file_discovered(failed_record)
        on_file_discovered(good_record)
        return [failed_record, good_record], []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        _ = cached_meta
        analyze_calls.append(path)
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[11, 22, 33, 44])

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    with Database(tmp_path / "resume-skip-failed.db") as db:
        scan_id = db.create_scan(
            profile="balanced",
            roots=[str(tmp_path)],
            extensions=["mp4"],
            probe_backend="ffprobe",
        )
        db.upsert_failed_file(
            scan_id,
            ScanIssue(
                stage="probe",
                path=failed_path,
                message="broken container",
            ),
        )
        db.complete_scan(scan_id, status="paused")

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=scan_id,
            retry_failed_files=False,
            progress_cb=progress_steps.append,
        )

        assert resumed.scan_id == scan_id
        assert analyze_calls == [good_path]
        assert resumed.metrics["skipped_failed_files"] == 1
        assert db.count_failed_files(scan_id) == 1
        assert any(
            step.stage == "skip"
            and step.subject_path == failed_path
            and step.completed_files == 1
            for step in progress_steps
        )
        assert any(
            step.stage == "probe"
            and step.subject_path == good_path
            and step.completed_files == 2
            for step in progress_steps
        )


def test_resume_reprocesses_when_fingerprint_algo_version_changes(
    tmp_path,
    monkeypatch,
) -> None:
    file_path = str(tmp_path / "algo-shift.mp4")
    record = _video_with_stats(file_path, lane=0, size=10, mtime_ns=11)
    pause_event = Event()
    analyze_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )

    def _fake_enumerate(**kwargs):
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        on_file_discovered(record)
        return [record], []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append((path, cached_meta is not None))
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[11, 22, 33, 44])

    def _pause_after_first_probe(step: ScanProgress) -> None:
        if step.stage == "probe" and step.current >= 1:
            pause_event.set()

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    with Database(tmp_path / "resume-algo.db") as db:
        paused = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_first_probe,
        )

        pause_event.clear()
        analyze_calls.clear()
        monkeypatch.setattr(
            "video_duperz.pipeline_runtime_context.ALGO_VERSION",
            9_999,
        )
        monkeypatch.setattr(
            "video_duperz.pipeline_runtime_context.SCENE_AWARE_ALGO_VERSION",
            10_000,
        )
        monkeypatch.setattr("video_duperz.fingerprint.ALGO_VERSION", 9_999)
        monkeypatch.setattr(
            "video_duperz.fingerprint.SCENE_AWARE_ALGO_VERSION",
            10_000,
        )

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
        )

        assert resumed.scan_id == paused.scan_id
        assert analyze_calls == [(file_path, True)]
        assert resumed.metrics["resume_cache_hits"] == 0
        assert resumed.metrics["resume_reprocessed_files"] == 1


def test_runtime_analyze_reuses_cached_meta_without_calling_probe(monkeypatch) -> None:
    probe_calls: list[str] = []

    def _fake_probe_video(path: str, **kwargs: object) -> VideoMeta:
        _ = kwargs
        probe_calls.append(path)
        return _analysis_meta()

    monkeypatch.setattr(pipeline, "probe_video", _fake_probe_video)
    monkeypatch.setattr(
        pipeline,
        "build_fingerprint_record_with_fallback",
        lambda **kwargs: SimpleNamespace(
            record=SimpleNamespace(hashes=[1, 2, 3], algo_version=ALGO_VERSION),
            decoder_backend="opencv",
            provenance_json="{}",
        ),
    )

    analyze = pipeline._build_runtime_analyze_file("ffprobe")
    cached_meta = _analysis_meta()
    output = analyze("cached.mp4", cached_meta)

    assert output.meta == cached_meta
    assert output.probe_s == 0.0
    assert output.fingerprint_s >= 0.0
    assert probe_calls == []


def test_runtime_analyze_threads_fingerprint_timeout_to_builder(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_probe_video(path: str, **kwargs: object) -> VideoMeta:
        _ = kwargs
        return _analysis_meta()

    def _fake_build_fingerprint_record_with_fallback(
        **kwargs: object,
    ) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            record=SimpleNamespace(hashes=[1, 2, 3], algo_version=ALGO_VERSION),
            decoder_backend="opencv",
            provenance_json="{}",
        )

    monkeypatch.setattr(pipeline, "probe_video", _fake_probe_video)
    monkeypatch.setattr(
        pipeline,
        "build_fingerprint_record_with_fallback",
        _fake_build_fingerprint_record_with_fallback,
    )

    analyze = pipeline._build_runtime_analyze_file(
        "ffprobe",
        fingerprint_timeout_s=45.5,
    )
    output = analyze("fresh.mp4", None)

    assert output.hashes == [1, 2, 3]
    assert captured["timeout_s"] == 45.5


def test_resume_keeps_duplicate_groups_correct_after_pause(
    tmp_path,
    monkeypatch,
) -> None:
    first_a = _video_with_stats(str(tmp_path / "pair-a-01.mp4"), lane=0, mtime_ns=11)
    second_a = _video_with_stats(str(tmp_path / "pair-a-02.mp4"), lane=0, mtime_ns=12)
    first_b = _video_with_stats(str(tmp_path / "pair-b-01.mp4"), lane=0, mtime_ns=21)
    second_b = _video_with_stats(str(tmp_path / "pair-b-02.mp4"), lane=0, mtime_ns=22)
    current_files = [first_a, second_a, first_b, second_b]
    enumerate_round = 0
    pause_event = Event()
    analyze_calls: list[str] = []
    hash_by_path = {
        first_a.path: [0x0000000000000000] * 6,
        second_a.path: [0x0000000000000000] * 6,
        first_b.path: [0xFFFFFFFFFFFFFFFF] * 6,
        second_b.path: [0xFFFFFFFFFFFFFFFF] * 6,
    }

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _lane_plan_for_roots(
            roots,
            lane_worker_limits={0: 1},
        ),
    )

    def _fake_enumerate(**kwargs):
        nonlocal enumerate_round
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        enumerate_round += 1
        emitted = [first_a] if enumerate_round == 1 else list(current_files)
        for item in emitted:
            on_file_discovered(item)
        return emitted, []

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append(path)
        return pipeline._AnalyzeOutput(
            meta=_analysis_meta(),
            hashes=hash_by_path[path],
        )

    def _pause_after_first_probe(step: ScanProgress) -> None:
        if step.stage == "probe" and step.current >= 1:
            pause_event.set()

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    with Database(tmp_path / "resume-duplicates.db") as db:
        paused = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_first_probe,
        )

        assert analyze_calls == [first_a.path]
        pause_event.clear()
        analyze_calls.clear()

        resumed = pipeline.run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            max_workers=1,
            probe_backend="ffprobe",
            probe_worker_mode="balanced",
            db_batch_size=32,
            db_flush_interval_ms=50,
            enum_queue_max=256,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
        )

        path_groups = {
            frozenset(item.path for item in group.items) for group in resumed.groups
        }
        assert resumed.scan_id == paused.scan_id
        assert resumed.metrics["resume_cache_hits"] == 1
        assert frozenset({first_a.path, second_a.path}) in path_groups
        assert frozenset({first_b.path, second_b.path}) in path_groups


def test_run_scan_incremental_rescan_carries_forward_unchanged_files(
    monkeypatch,
) -> None:
    files = [
        _video_with_stats("D:/Videos/same.mp4", lane=0, size=10, mtime_ns=1),
        _video_with_stats("D:/Videos/mod.mp4", lane=0, size=10, mtime_ns=2),
        _video_with_stats("D:/Videos/new.mp4", lane=0, size=30, mtime_ns=3),
    ]
    analyze_calls: list[str] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
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
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append(path)
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[1, 2, 3])

    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    db = _FakeDb()
    scan_set_key = build_scan_set_key(
        roots=["D:/Videos"],
        similarity_profile="balanced",
        extensions=["mp4"],
    )
    db.latest_completed_by_set[scan_set_key] = 12
    db.scan_snapshots[12] = {
        "D:/Videos/same.mp4": {
            "file_id": 1,
            "path": "D:/Videos/same.mp4",
            "size": 10,
            "mtime_ns": 1,
            "ctime_ns": 1,
            "ext": "mp4",
        },
        "D:/Videos/mod.mp4": {
            "file_id": 2,
            "path": "D:/Videos/mod.mp4",
            "size": 10,
            "mtime_ns": 1,
            "ctime_ns": 1,
            "ext": "mp4",
        },
        "D:/Videos/gone.mp4": {
            "file_id": 3,
            "path": "D:/Videos/gone.mp4",
            "size": 20,
            "mtime_ns": 1,
            "ctime_ns": 1,
            "ext": "mp4",
        },
    }

    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
        roots=["D:/Videos"],
        extensions=["mp4"],
        max_workers=1,
        probe_backend="ffprobe",
        probe_worker_mode="balanced",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
    )

    assert result.scanned_files == 3
    assert result.cached_files == 1
    assert result.fingerprinted_files == 2
    assert analyze_calls == ["D:/Videos/mod.mp4", "D:/Videos/new.mp4"]
    assert db.cloned_paths == ["D:/Videos/same.mp4"]
    assert db.cloned_failed_paths == ["D:/Videos/same.mp4"]
    assert result.metrics["incremental_base_scan_id"] == 12
    assert result.metrics["incremental_unchanged_files"] == 1
    assert result.metrics["incremental_modified_files"] == 1
    assert result.metrics["incremental_new_files"] == 1
    assert result.metrics["incremental_deleted_files"] == 1


def test_run_scan_resume_does_not_use_incremental_baseline_cloning(monkeypatch) -> None:
    files = [_video_with_stats("D:/Videos/same.mp4", lane=0, size=10, mtime_ns=1)]
    analyze_calls: list[str] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
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
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append(path)
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[1, 2, 3])

    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    db = _FakeDb()
    scan_set_key = build_scan_set_key(
        roots=["D:/Videos"],
        similarity_profile="balanced",
        extensions=["mp4"],
    )
    db.latest_completed_by_set[scan_set_key] = 12
    db.scan_snapshots[12] = {
        "D:/Videos/same.mp4": {
            "file_id": 1,
            "path": "D:/Videos/same.mp4",
            "size": 10,
            "mtime_ns": 1,
            "ctime_ns": 1,
            "ext": "mp4",
        }
    }

    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
        roots=["D:/Videos"],
        extensions=["mp4"],
        max_workers=1,
        probe_backend="ffprobe",
        probe_worker_mode="balanced",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
        resume_scan_id=55,
    )

    assert result.scan_id == 55
    assert analyze_calls == ["D:/Videos/same.mp4"]
    assert db.cloned_paths == []
    assert db.cloned_failed_paths == []
    assert result.metrics["incremental_base_scan_id"] is None


def test_run_scan_incremental_reuses_unchanged_files_despite_path_case_changes(
    monkeypatch,
) -> None:
    files = [_video_with_stats("D:/Videos/SAME.mp4", lane=0, size=10, mtime_ns=1)]
    analyze_calls: list[str] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda: None
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
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        analyze_calls.append(path)
        return pipeline._AnalyzeOutput(meta=_analysis_meta(), hashes=[1, 2, 3])

    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    db = _FakeDb()
    scan_set_key = build_scan_set_key(
        roots=["D:/Videos"],
        similarity_profile="balanced",
        extensions=["mp4"],
    )
    db.latest_completed_by_set[scan_set_key] = 12
    db.scan_snapshots[12] = {
        "d:/videos/same.mp4": {
            "file_id": 1,
            "path": "d:/videos/same.mp4",
            "size": 10,
            "mtime_ns": 1,
            "ctime_ns": 1,
            "ext": "mp4",
        }
    }

    result = pipeline.run_scan(
        db=db,  # type: ignore[arg-type]
        roots=["D:/Videos"],
        extensions=["mp4"],
        max_workers=1,
        probe_backend="ffprobe",
        probe_worker_mode="balanced",
        db_batch_size=64,
        db_flush_interval_ms=200,
        enum_queue_max=512,
        progress_emit_interval_ms=50,
        progress_emit_every_files=1,
    )

    assert result.cached_files == 1
    assert result.fingerprinted_files == 0
    assert analyze_calls == []
    assert db.cloned_paths == ["D:/Videos/SAME.mp4"]
