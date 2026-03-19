from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from threading import Event

import pytest

from video_duperz import pipeline
from video_duperz.db import Database
from video_duperz.models import MatchStats, VideoMeta, VideoRecord
from video_duperz.pipeline import run_scan


def _tools_available() -> bool:
    return bool(
        shutil.which("ffmpeg")
        and shutil.which("ffprobe")
        and importlib.util.find_spec("cv2")
    )


def _audio_tools_available() -> bool:
    return bool(_tools_available() and shutil.which("fpcalc"))


def _run_ffmpeg(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


REAL_DUPES_ROOT_ENV = "VIDEO_DUPERZ_REAL_DUPES_ROOT"
REAL_PAUSE_ROOT_ENV = "VIDEO_DUPERZ_REAL_PAUSE_ROOT"
EXPECTED_REAL_DUPLICATE_GROUPS = (
    frozenset({"DUPE_A01.wmv", "DUPE_A02.wmv"}),
    frozenset({"DUPE_B01.mkv", "DUPE_B02.mp4"}),
    frozenset({"DUPE_C01.mp4", "DUPE_C02.avi", "DUPE_C03.avi"}),
    frozenset({"DUPE_D01.mp4", "DUPE_D02.mp4"}),
)


def _real_dupes_root() -> Path | None:
    raw = os.environ.get(REAL_DUPES_ROOT_ENV, "").strip()
    if not raw:
        return None
    root = Path(raw)
    if not root.is_dir():
        return None
    return root


def _real_pause_root() -> Path | None:
    raw = os.environ.get(REAL_PAUSE_ROOT_ENV, "").strip()
    if not raw:
        return None
    root = Path(raw)
    if not root.is_dir():
        return None
    return root


def _real_pause_subset(
    root: Path,
    *,
    limit: int = 30,
) -> tuple[list[Path], list[str], list[str]]:
    video_exts = {".mp4", ".wmv", ".mkv", ".avi", ".mov", ".m4v"}
    selected_files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in video_exts
        ),
        key=lambda path: (str(path).lower(), str(path)),
    )[:limit]
    roots = sorted(
        {str(path.parent) for path in selected_files},
        key=lambda path: (path.lower(), path),
    )
    extensions = sorted(
        {path.suffix.lower().lstrip(".") for path in selected_files},
    )
    return (selected_files, roots, extensions)


def _fake_lane_plan(roots: list[str]) -> object:
    return type(
        "_LanePlan",
        (),
        {
            "root_groups": [list(roots)],
            "root_to_group_index": dict.fromkeys(roots, 0),
            "lane_worker_limits": {0: 1},
            "effective_total_workers": 1,
            "requested_worker_target": 1,
            "issues": [],
        },
    )()


def _fake_meta() -> VideoMeta:
    return VideoMeta(
        duration_s=3.0,
        width=320,
        height=240,
        fps=24.0,
        codec="h264",
        bitrate=1000,
        has_audio=True,
        audio_codec="aac",
        audio_bitrate=128000,
        audio_languages="eng",
        subtitle_languages="",
        hdr_format="",
    )


@pytest.mark.skipif(not _tools_available(), reason="ffmpeg/ffprobe not available")
def test_pipeline_detects_reencoded_duplicates(tmp_path: Path) -> None:
    base = tmp_path / "base.mp4"
    copy = tmp_path / "copy.mp4"
    reenc = tmp_path / "reenc.mp4"
    other = tmp_path / "other.mp4"

    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=24",
            "-t",
            "3",
            str(base),
        ]
    )
    shutil.copy2(base, copy)
    _run_ffmpeg(["ffmpeg", "-y", "-i", str(base), str(reenc)])
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:r=24",
            "-t",
            "3",
            str(other),
        ]
    )

    with Database(tmp_path / "app.db") as db:
        result = run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            profile="balanced",
            probe_backend="ffprobe",
            scan_size_mib_min=0,
            db_batch_size=512,
            db_flush_interval_ms=200,
            enum_queue_max=4096,
            progress_emit_interval_ms=200,
            progress_emit_every_files=100,
        )
        assert result.scanned_files >= 4
        assert len(result.groups) >= 1
        path_groups = [{item.path for item in g.items} for g in result.groups]
        assert any(str(base) in g and str(copy) in g for g in path_groups)
        assert any(str(reenc) in g for g in path_groups)


def test_pipeline_incremental_rescan_reuses_completed_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    first_scan_files = [
        VideoRecord(
            path=str(root / "same.mp4"),
            size=10,
            mtime_ns=1,
            ctime_ns=1,
            ext="mp4",
            scan_id=0,
            source_root=str(root),
            parallel_lane=0,
        ),
        VideoRecord(
            path=str(root / "mod.mp4"),
            size=20,
            mtime_ns=1,
            ctime_ns=1,
            ext="mp4",
            scan_id=0,
            source_root=str(root),
            parallel_lane=0,
        ),
        VideoRecord(
            path=str(root / "gone.mp4"),
            size=30,
            mtime_ns=1,
            ctime_ns=1,
            ext="mp4",
            scan_id=0,
            source_root=str(root),
            parallel_lane=0,
        ),
    ]
    second_scan_files = [
        VideoRecord(
            path=str(root / "same.mp4"),
            size=10,
            mtime_ns=1,
            ctime_ns=1,
            ext="mp4",
            scan_id=0,
            source_root=str(root),
            parallel_lane=0,
        ),
        VideoRecord(
            path=str(root / "mod.mp4"),
            size=20,
            mtime_ns=2,
            ctime_ns=2,
            ext="mp4",
            scan_id=0,
            source_root=str(root),
            parallel_lane=0,
        ),
        VideoRecord(
            path=str(root / "new.mp4"),
            size=40,
            mtime_ns=3,
            ctime_ns=3,
            ext="mp4",
            scan_id=0,
            source_root=str(root),
            parallel_lane=0,
        ),
    ]
    enumerate_round = 0
    analyze_calls: list[str] = []

    monkeypatch.setattr(pipeline, "ensure_ffprobe_available", lambda *args: None)
    monkeypatch.setattr(
        pipeline, "ensure_fingerprint_fallback_chain_available", lambda *args: None
    )
    monkeypatch.setattr(
        pipeline,
        "build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: _fake_lane_plan(roots),
    )
    monkeypatch.setattr(
        pipeline,
        "find_duplicate_edges",
        lambda items, profile, **kwargs: ([], MatchStats()),
    )
    monkeypatch.setattr(
        pipeline, "build_duplicate_groups", lambda items, edges, profile: []
    )

    def _fake_enumerate(**kwargs: object) -> tuple[list[VideoRecord], list[object]]:
        nonlocal enumerate_round
        enumerate_round += 1
        emitted = first_scan_files if enumerate_round == 1 else second_scan_files
        on_file_discovered = kwargs.get("on_file_discovered")
        assert callable(on_file_discovered)
        for item in emitted:
            on_file_discovered(item)
        return (list(emitted), [])

    def _fake_analyze(path: str, cached_meta: object | None) -> pipeline._AnalyzeOutput:
        _ = cached_meta
        analyze_calls.append(path)
        return pipeline._AnalyzeOutput(meta=_fake_meta(), hashes=[1, 2, 3])

    monkeypatch.setattr(pipeline, "enumerate_video_files", _fake_enumerate)
    monkeypatch.setattr(pipeline, "_analyze_file", _fake_analyze)

    with Database(tmp_path / "incremental.db") as db:
        first = run_scan(
            db=db,
            roots=[str(root)],
            extensions=["mp4"],
            scan_size_mib_min=0,
            profile="balanced",
            probe_backend="ffprobe",
            db_batch_size=64,
            db_flush_interval_ms=50,
            enum_queue_max=512,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
        )
        assert first.scan_id > 0
        assert set(analyze_calls) == {
            str(root / "same.mp4"),
            str(root / "mod.mp4"),
            str(root / "gone.mp4"),
        }

        analyze_calls.clear()
        second = run_scan(
            db=db,
            roots=[str(root)],
            extensions=["mp4"],
            scan_size_mib_min=0,
            profile="balanced",
            probe_backend="ffprobe",
            db_batch_size=64,
            db_flush_interval_ms=50,
            enum_queue_max=512,
            progress_emit_interval_ms=50,
            progress_emit_every_files=1,
        )

        assert second.scan_id > first.scan_id
        assert second.cached_files == 1
        assert second.fingerprinted_files == 2
        assert analyze_calls == [str(root / "mod.mp4"), str(root / "new.mp4")]
        assert second.metrics["incremental_base_scan_id"] == first.scan_id
        assert second.metrics["incremental_unchanged_files"] == 1
        assert second.metrics["incremental_modified_files"] == 1
        assert second.metrics["incremental_new_files"] == 1
        assert second.metrics["incremental_deleted_files"] == 1

        first_snapshot = db.load_scan_file_snapshot(first.scan_id)
        second_snapshot = db.load_scan_file_snapshot(second.scan_id)
        assert set(first_snapshot) == {
            str(root / "same.mp4"),
            str(root / "mod.mp4"),
            str(root / "gone.mp4"),
        }
        assert set(second_snapshot) == {
            str(root / "same.mp4"),
            str(root / "mod.mp4"),
            str(root / "new.mp4"),
        }


@pytest.mark.skipif(not _tools_available(), reason="ffmpeg/ffprobe not available")
def test_pipeline_detects_trimmed_duration_difference_duplicates(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core.mp4"
    longer = tmp_path / "longer.mp4"
    shorter = tmp_path / "shorter.mp4"
    outro = tmp_path / "outro.mp4"

    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=24",
            "-t",
            "20",
            str(core),
        ]
    )
    shutil.copy2(core, shorter)
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:r=24",
            "-t",
            "5",
            str(outro),
        ]
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(core),
            "-i",
            str(outro),
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            str(longer),
        ]
    )

    with Database(tmp_path / "app.db") as db:
        result = run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            scan_size_mib_min=0,
            profile="balanced",
            duration_tolerance_s=8.0,
            db_batch_size=512,
            db_flush_interval_ms=200,
            enum_queue_max=4096,
            progress_emit_interval_ms=200,
            progress_emit_every_files=100,
        )

        groups = [
            {Path(item.path).name: item for item in group.items}
            for group in result.groups
        ]
        target_group = next(
            (
                group
                for group in groups
                if {"longer.mp4", "shorter.mp4"}.issubset(group)
            ),
            None,
        )
        assert target_group is not None
        assert target_group["longer.mp4"].match_reason == "trimmed_match"
        assert target_group["shorter.mp4"].match_reason == "trimmed_match"
        assert target_group["longer.mp4"].match_duration_delta_s == pytest.approx(5.0)


@pytest.mark.skipif(
    not _audio_tools_available(),
    reason="ffmpeg/ffprobe/fpcalc not available",
)
def test_pipeline_audio_fingerprint_rescues_visual_miss(tmp_path: Path) -> None:
    black = tmp_path / "black.mp4"
    blue = tmp_path / "blue.mp4"

    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100",
            "-shortest",
            "-t",
            "3",
            str(black),
        ]
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100",
            "-shortest",
            "-t",
            "3",
            str(blue),
        ]
    )

    with Database(tmp_path / "app.db") as db:
        result = run_scan(
            db=db,
            roots=[str(tmp_path)],
            extensions=["mp4"],
            scan_size_mib_min=0,
            profile="balanced",
            audio_fingerprint_enabled=True,
            db_batch_size=512,
            db_flush_interval_ms=200,
            enum_queue_max=4096,
            progress_emit_interval_ms=200,
            progress_emit_every_files=100,
        )

        target_group = next(
            (
                group
                for group in result.groups
                if {Path(item.path).name for item in group.items}
                == {"black.mp4", "blue.mp4"}
            ),
            None,
        )

        assert target_group is not None
        assert any(item.match_reason == "audio_match" for item in target_group.items)


@pytest.mark.skipif(not _tools_available(), reason="ffmpeg/ffprobe not available")
def test_pipeline_resume_real_duplicate_corpus_skips_unchanged_files(
    tmp_path: Path,
) -> None:
    root = _real_dupes_root()
    if root is None:
        pytest.skip(f"{REAL_DUPES_ROOT_ENV} is not set to a readable directory")

    expected_files = sorted(
        {name for group in EXPECTED_REAL_DUPLICATE_GROUPS for name in group}
    )
    expected_extensions = sorted(
        {Path(name).suffix.lower().lstrip(".") for name in expected_files}
    )
    missing = [name for name in expected_files if not (root / name).is_file()]
    if missing:
        pytest.skip(f"real duplicate corpus is missing expected files: {missing}")

    pause_event = Event()

    def _pause_after_first_persistable_probe(step) -> None:
        if step.stage == "probe" and step.current >= 1:
            pause_event.set()

    with Database(tmp_path / "real-dupes.db") as db:
        paused = run_scan(
            db=db,
            roots=[str(root)],
            extensions=expected_extensions,
            profile="balanced",
            db_batch_size=256,
            db_flush_interval_ms=100,
            enum_queue_max=4096,
            progress_emit_interval_ms=100,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_first_persistable_probe,
        )

        processed_rows = db.conn.execute(
            """
            SELECT f.path, vm.probed_at, fp.created_at
            FROM files f
            JOIN video_meta vm
              ON vm.file_id = f.id AND vm.probe_backend = 'pyav'
            JOIN fingerprints fp
              ON fp.file_id = f.id AND fp.probe_backend = 'pyav'
            WHERE f.scan_id = ?
            ORDER BY f.path
            """,
            (paused.scan_id,),
        ).fetchall()
        assert processed_rows
        timestamps_before = {
            str(Path(str(row["path"])).name): (
                str(row["probed_at"]),
                str(row["created_at"]),
            )
            for row in processed_rows
        }

        pause_event.clear()
        resumed = run_scan(
            db=db,
            roots=[str(root)],
            extensions=expected_extensions,
            profile="balanced",
            db_batch_size=256,
            db_flush_interval_ms=100,
            enum_queue_max=4096,
            progress_emit_interval_ms=100,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
        )

        processed_rows_after = db.conn.execute(
            """
            SELECT f.path, vm.probed_at, fp.created_at
            FROM files f
            JOIN video_meta vm
              ON vm.file_id = f.id AND vm.probe_backend = 'pyav'
            JOIN fingerprints fp
              ON fp.file_id = f.id AND fp.probe_backend = 'pyav'
            WHERE f.scan_id = ?
            ORDER BY f.path
            """,
            (resumed.scan_id,),
        ).fetchall()
        timestamps_after = {
            str(Path(str(row["path"])).name): (
                str(row["probed_at"]),
                str(row["created_at"]),
            )
            for row in processed_rows_after
        }
        actual_groups = {
            frozenset(Path(item.path).name for item in group.items)
            for group in resumed.groups
        }

        assert resumed.scan_id == paused.scan_id
        for basename, timestamps in timestamps_before.items():
            assert timestamps_after[basename] == timestamps
        for expected_group in EXPECTED_REAL_DUPLICATE_GROUPS:
            assert expected_group in actual_groups


@pytest.mark.skipif(not _tools_available(), reason="ffmpeg/ffprobe not available")
def test_pipeline_pause_and_resume_real_subset_reuses_cached_work(
    tmp_path: Path,
) -> None:
    root = _real_pause_root()
    if root is None:
        pytest.skip(f"{REAL_PAUSE_ROOT_ENV} is not set to a readable directory")

    selected_files, roots, extensions = _real_pause_subset(root)
    if len(selected_files) < 10 or not roots or not extensions:
        pytest.skip("real pause/resume source did not yield enough video files")

    pause_event = Event()

    def _pause_after_five_probes(step) -> None:
        if step.stage == "probe" and step.current >= 5:
            pause_event.set()

    with Database(tmp_path / "real-pause-resume.db") as db:
        paused = run_scan(
            db=db,
            roots=roots,
            extensions=extensions,
            profile="balanced",
            max_workers=2,
            probe_backend="pyav",
            probe_worker_mode="balanced",
            db_batch_size=128,
            db_flush_interval_ms=100,
            enum_queue_max=4096,
            progress_emit_interval_ms=100,
            progress_emit_every_files=1,
            pause_event=pause_event,
            progress_cb=_pause_after_five_probes,
        )

        paused_summary = db.scan_summary(paused.scan_id)
        pause_event.clear()

        resumed = run_scan(
            db=db,
            roots=roots,
            extensions=extensions,
            profile="balanced",
            max_workers=2,
            probe_backend="pyav",
            probe_worker_mode="balanced",
            db_batch_size=128,
            db_flush_interval_ms=100,
            enum_queue_max=4096,
            progress_emit_interval_ms=100,
            progress_emit_every_files=1,
            resume_scan_id=paused.scan_id,
        )

        resumed_summary = db.scan_summary(resumed.scan_id)

        assert paused_summary["status"] == "paused"
        assert resumed.scan_id == paused.scan_id
        assert resumed_summary["status"] == "done"
        assert paused.fingerprinted_files >= 5
        assert resumed.metrics["resume_cache_hits"] == paused.fingerprinted_files
        assert resumed.cached_files == paused.fingerprinted_files
        assert resumed.cached_files + resumed.fingerprinted_files == len(selected_files)
