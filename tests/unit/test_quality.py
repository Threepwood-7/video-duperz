from __future__ import annotations

from video_duperz.models import MatchItem
from video_duperz.quality import choose_keep_file_id, quality_score


def _item(
    file_id: int,
    width: int,
    height: int,
    bitrate: int,
    codec: str,
    mtime_ns: int,
    path: str,
) -> MatchItem:
    return MatchItem(
        file_id=file_id,
        path=path,
        size=1,
        mtime_ns=mtime_ns,
        ctime_ns=mtime_ns,
        duration_s=10.0,
        width=width,
        height=height,
        fps=30.0,
        codec=codec,
        bitrate=bitrate,
        audio_codec="aac",
        audio_bitrate=128000,
        audio_languages="eng",
        subtitle_languages="",
        is_hdr=False,
        hashes=[0] * 12,
    )


def test_quality_score_prefers_higher_resolution_and_bitrate() -> None:
    low = _item(1, 1280, 720, 1_000_000, "h264", 100, "a.mp4")
    high = _item(2, 1920, 1080, 2_000_000, "h264", 200, "b.mp4")
    assert quality_score(high) > quality_score(low)


def test_choose_keep_tie_breakers() -> None:
    first = _item(1, 1920, 1080, 2_000_000, "h264", 10, "x.mp4")
    second = _item(2, 1920, 1080, 2_000_000, "h264", 20, "much_longer_name.mp4")
    assert choose_keep_file_id([second, first]) == 1
