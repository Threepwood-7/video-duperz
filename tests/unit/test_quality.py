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
    *,
    bit_depth: int = 8,
    codec_profile: str = "",
    codec_level: str = "",
    is_interlaced: bool = False,
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
        audio_stream_count=1,
        audio_codec="aac",
        audio_bitrate=128000,
        audio_languages="eng",
        subtitle_languages="",
        hashes=[0] * 12,
        bit_depth=bit_depth,
        codec_profile=codec_profile,
        codec_level=codec_level,
        is_interlaced=is_interlaced,
    )


def test_quality_score_prefers_higher_resolution_and_bitrate() -> None:
    low = _item(1, 1280, 720, 1_000_000, "h264", 100, "a.mp4")
    high = _item(2, 1920, 1080, 2_000_000, "h264", 200, "b.mp4")
    assert quality_score(high) > quality_score(low)


def test_choose_keep_tie_breakers() -> None:
    first = _item(1, 1920, 1080, 2_000_000, "h264", 10, "x.mp4")
    second = _item(2, 1920, 1080, 2_000_000, "h264", 20, "much_longer_name.mp4")
    assert choose_keep_file_id([second, first]) == 1


def test_quality_score_prefers_higher_bit_depth_when_other_specs_match() -> None:
    low = _item(1, 1920, 1080, 2_000_000, "h264", 10, "a.mp4", bit_depth=8)
    high = _item(2, 1920, 1080, 2_000_000, "h264", 20, "b.mp4", bit_depth=10)
    assert quality_score(high) > quality_score(low)


def test_quality_score_prefers_twelve_bit_over_ten_bit() -> None:
    ten_bit = _item(1, 1920, 1080, 2_000_000, "h264", 10, "a.mp4", bit_depth=10)
    twelve_bit = _item(
        2,
        1920,
        1080,
        2_000_000,
        "h264",
        20,
        "b.mp4",
        bit_depth=12,
    )
    assert quality_score(twelve_bit) > quality_score(ten_bit)


def test_quality_score_prefers_higher_codec_profile_when_other_specs_match() -> None:
    main = _item(
        1,
        1920,
        1080,
        2_000_000,
        "h264",
        10,
        "a.mp4",
        codec_profile="Main",
    )
    high = _item(
        2,
        1920,
        1080,
        2_000_000,
        "h264",
        20,
        "b.mp4",
        codec_profile="High",
    )
    assert quality_score(high) > quality_score(main)


def test_choose_keep_prefers_progressive_when_other_specs_match() -> None:
    interlaced = _item(
        1,
        1920,
        1080,
        2_000_000,
        "h264",
        10,
        "a.mp4",
        codec_profile="High",
        codec_level="4.1",
        is_interlaced=True,
    )
    progressive = _item(
        2,
        1920,
        1080,
        2_000_000,
        "h264",
        20,
        "b.mp4",
        codec_profile="High",
        codec_level="4.1",
        is_interlaced=False,
    )
    assert choose_keep_file_id([interlaced, progressive]) == 2
