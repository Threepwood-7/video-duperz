from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from video_duperz.db import Database
from video_duperz.pipeline import run_scan


def _tools_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe") and importlib.util.find_spec("cv2"))


def _run_ffmpeg(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


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
