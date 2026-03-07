from __future__ import annotations

import importlib.util
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from video_duperz.ui.thumbnails import extract_thumbnail, extract_thumbnail_pair

if TYPE_CHECKING:
    from pathlib import Path


def _tools_available() -> bool:
    return bool(shutil.which("ffmpeg") and importlib.util.find_spec("cv2"))


def _run_ffmpeg(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


@pytest.mark.skipif(not _tools_available(), reason="ffmpeg/cv2 not available")
def test_extract_thumbnail_writes_file(tmp_path: Path) -> None:
    source = tmp_path / "sample.mp4"
    thumb = tmp_path / "sample.jpg"
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=24",
            "-t",
            "2",
            str(source),
        ]
    )

    ok, err = extract_thumbnail(path=str(source), output_path=thumb, target_w=96, target_h=54)
    assert ok, err
    assert thumb.exists()
    assert thumb.stat().st_size > 0


@pytest.mark.skipif(not _tools_available(), reason="ffmpeg/cv2 not available")
def test_extract_thumbnail_pair_writes_files(tmp_path: Path) -> None:
    source = tmp_path / "sample_pair.mp4"
    thumb_a = tmp_path / "sample_a.jpg"
    thumb_b = tmp_path / "sample_b.jpg"
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=24",
            "-t",
            "2",
            str(source),
        ]
    )

    ok, err = extract_thumbnail_pair(
        path=str(source),
        output_path_a=thumb_a,
        output_path_b=thumb_b,
        target_w=96,
        target_h=54,
        frame_a_pct=23,
        frame_b_pct=77,
    )
    assert ok, err
    assert thumb_a.exists()
    assert thumb_b.exists()
    assert thumb_a.stat().st_size > 0
    assert thumb_b.stat().st_size > 0
