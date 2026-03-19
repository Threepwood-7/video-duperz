from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from video_duperz import benchmark_eval
from video_duperz.fingerprint import FingerprintError
from video_duperz.models import VideoMeta, VideoRecord


def _video(
    path: str,
    *,
    source_root: str,
    lane: int,
    ext: str = "mp4",
) -> VideoRecord:
    return VideoRecord(
        path=path,
        size=10,
        mtime_ns=1,
        ctime_ns=1,
        ext=ext,
        scan_id=0,
        source_root=source_root,
        parallel_lane=lane,
    )


def test_build_benchmark_sample_plan_ghetto_is_deterministic(monkeypatch) -> None:
    records = [
        _video("S:/x.mp4", source_root="S:/", lane=1),
        _video("R:/B_GHETTO.mp4", source_root="R:/", lane=0),
        _video("R:/a_ghetto.mp4", source_root="R:/", lane=0),
    ]
    monkeypatch.setattr(
        benchmark_eval,
        "enumerate_video_files",
        lambda **_kwargs: (list(records), []),
    )
    monkeypatch.setattr(
        benchmark_eval,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: SimpleNamespace(
            root_groups=[[roots[0]], [roots[1]]],
        ),
    )

    plan = benchmark_eval.build_benchmark_sample_plan(
        ["R:/", "S:/"],
        set_id="ghetto",
    )

    assert [item.path for item in plan.files] == [
        "R:/a_ghetto.mp4",
        "R:/B_GHETTO.mp4",
    ]
    assert plan.total_candidates == 3
    assert plan.lane_roots == {0: ["R:/"], 1: ["S:/"]}


def test_build_benchmark_sample_plan_mixed_240_prefers_risky_then_normal(
    monkeypatch,
) -> None:
    records = [
        _video("R:/01.avi", source_root="R:/", lane=0, ext="avi"),
        _video("R:/02.wmv", source_root="R:/", lane=0, ext="wmv"),
        _video("R:/03.mp4", source_root="R:/", lane=0, ext="mp4"),
        _video("S:/01.mov", source_root="S:/", lane=1, ext="mov"),
        _video("S:/02.mp4", source_root="S:/", lane=1, ext="mp4"),
        _video("S:/03.mkv", source_root="S:/", lane=1, ext="mkv"),
    ]
    monkeypatch.setattr(
        benchmark_eval,
        "enumerate_video_files",
        lambda **_kwargs: (list(records), []),
    )
    monkeypatch.setattr(
        benchmark_eval,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: SimpleNamespace(
            root_groups=[[roots[0]], [roots[1]]],
        ),
    )

    plan = benchmark_eval.build_benchmark_sample_plan(
        ["R:/", "S:/"],
        set_id="mixed_240",
    )

    assert [item.path for item in plan.files] == [
        "R:/01.avi",
        "R:/02.wmv",
        "S:/01.mov",
        "R:/03.mp4",
        "S:/02.mp4",
        "S:/03.mkv",
    ]
    risky_by_path = {item.path: item.is_risky for item in plan.files}
    assert risky_by_path["R:/01.avi"] is True
    assert risky_by_path["R:/02.wmv"] is True
    assert risky_by_path["S:/01.mov"] is True
    assert risky_by_path["R:/03.mp4"] is False


def test_build_benchmark_sample_plan_incomplete_prioritizes_incomplete_paths(
    monkeypatch,
) -> None:
    records = [
        _video("R:/done/a.mp4", source_root="R:/", lane=0),
        _video("R:/incomplete/b.mp4", source_root="R:/", lane=0),
        _video("R:/INCOMPLETE/c.mp4", source_root="R:/", lane=0),
    ]
    monkeypatch.setattr(
        benchmark_eval,
        "enumerate_video_files",
        lambda **_kwargs: (list(records), []),
    )
    monkeypatch.setattr(
        benchmark_eval,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: SimpleNamespace(
            root_groups=[[roots[0]]],
        ),
    )

    plan = benchmark_eval.build_benchmark_sample_plan(["R:/"], set_id="incomplete")

    assert [item.path for item in plan.files] == [
        "R:/incomplete/b.mp4",
        "R:/INCOMPLETE/c.mp4",
    ]


def test_run_benchmark_plan_never_runs_two_files_from_same_lane() -> None:
    plan = benchmark_eval.BenchmarkSamplePlan(
        set_id="mixed_240",
        files=[
            benchmark_eval.BenchmarkSampleFile(
                path="lane0-a",
                source_root="R:/",
                lane=0,
                size=1,
                ext="mp4",
                is_risky=False,
            ),
            benchmark_eval.BenchmarkSampleFile(
                path="lane0-b",
                source_root="R:/",
                lane=0,
                size=1,
                ext="mp4",
                is_risky=False,
            ),
            benchmark_eval.BenchmarkSampleFile(
                path="lane1-a",
                source_root="S:/",
                lane=1,
                size=1,
                ext="mp4",
                is_risky=False,
            ),
            benchmark_eval.BenchmarkSampleFile(
                path="lane2-a",
                source_root="T:/",
                lane=2,
                size=1,
                ext="mp4",
                is_risky=False,
            ),
        ],
        total_candidates=4,
        roots=["R:/", "S:/", "T:/"],
        lane_roots={0: ["R:/"], 1: ["S:/"], 2: ["T:/"]},
    )
    active_by_lane: dict[int, int] = {}
    max_by_lane: dict[int, int] = {}
    lane_by_path = {sample.path: sample.lane for sample in plan.files}
    lock = threading.Lock()

    def _analyze(path: str) -> benchmark_eval.AnalyzeOutputLike:
        lane = lane_by_path[path]
        with lock:
            active = active_by_lane.get(lane, 0) + 1
            active_by_lane[lane] = active
            max_by_lane[lane] = max(active, max_by_lane.get(lane, 0))
        time.sleep(0.01)
        with lock:
            active_by_lane[lane] = max(0, active_by_lane.get(lane, 0) - 1)
        return SimpleNamespace(
            meta=VideoMeta(
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
                hdr_format="",
            ),
            hashes=[1, 2, 3],
            fingerprint_decoder_backend="opencv",
            fingerprint_provenance_json=json.dumps(
                {
                    "attempts": [
                        {
                            "decoder_backend": "opencv",
                            "status": "success",
                            "message": "",
                        }
                    ]
                }
            ),
        )

    summary = benchmark_eval.run_benchmark_plan(
        plan,
        analyze_file=_analyze,
        requested_workers=5,
        dispatch_strategy="ordered",
    )

    assert summary.analyzed_files == 4
    assert all(value <= 1 for value in max_by_lane.values())


def test_select_micro_batch_lanes_rotates_across_ready_lanes() -> None:
    lanes = benchmark_eval._select_micro_batch_lanes(
        [0, 1, 2],
        [0, 1, 2],
        start_index=1,
        slots=2,
    )

    assert lanes == [1, 2]


def test_run_benchmark_plan_tracks_fallbacks_timeouts_and_failures() -> None:
    plan = benchmark_eval.BenchmarkSamplePlan(
        set_id="ghetto",
        files=[
            benchmark_eval.BenchmarkSampleFile(
                path="ok-primary",
                source_root="R:/",
                lane=0,
                size=1,
                ext="mp4",
                is_risky=False,
            ),
            benchmark_eval.BenchmarkSampleFile(
                path="ok-fallback",
                source_root="S:/",
                lane=1,
                size=1,
                ext="wmv",
                is_risky=True,
            ),
            benchmark_eval.BenchmarkSampleFile(
                path="fail-fp",
                source_root="R:/",
                lane=0,
                size=1,
                ext="mp4",
                is_risky=False,
            ),
        ],
        total_candidates=3,
        roots=["R:/", "S:/"],
        lane_roots={0: ["R:/"], 1: ["S:/"]},
    )

    def _analyze(path: str) -> benchmark_eval.AnalyzeOutputLike:
        if path == "fail-fp":
            raise FingerprintError("fingerprint boom")
        if path == "ok-fallback":
            provenance = {
                "attempts": [
                    {
                        "decoder_backend": "opencv",
                        "status": "timeout",
                        "message": "timed out",
                    },
                    {
                        "decoder_backend": "pyav",
                        "status": "success",
                        "message": "",
                    },
                ]
            }
            backend = "pyav"
        else:
            provenance = {
                "attempts": [
                    {
                        "decoder_backend": "opencv",
                        "status": "success",
                        "message": "",
                    }
                ]
            }
            backend = "opencv"
        return SimpleNamespace(
            meta=VideoMeta(
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
                hdr_format="",
            ),
            hashes=[1, 2, 3],
            fingerprint_decoder_backend=backend,
            fingerprint_provenance_json=json.dumps(provenance),
        )

    summary = benchmark_eval.run_benchmark_plan(
        plan,
        analyze_file=_analyze,
        requested_workers=2,
        dispatch_strategy="micro_batch",
    )

    assert summary.success_count == 2
    assert summary.failure_count == 1
    assert summary.fallback_count == 1
    assert summary.decoder_timeout_count == 1
    lane0 = next(metric for metric in summary.lane_metrics if metric.lane == 0)
    lane1 = next(metric for metric in summary.lane_metrics if metric.lane == 1)
    assert lane0.failures == 1
    assert lane1.decoder_mix == {"pyav": 1}
