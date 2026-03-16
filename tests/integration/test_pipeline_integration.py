from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from threading import Event

import pytest

from video_duperz.db import Database
from video_duperz.pipeline import run_scan


def _tools_available() -> bool:
    return bool(
        shutil.which("ffmpeg")
        and shutil.which("ffprobe")
        and importlib.util.find_spec("cv2")
    )


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
