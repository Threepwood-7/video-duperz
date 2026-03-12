from __future__ import annotations

from video_duperz.scan_sets import build_scan_set_key


def test_scan_set_key_is_order_insensitive_for_roots_and_extensions() -> None:
    key_a = build_scan_set_key(
        roots=["D:/Videos", "E:/Archive"],
        similarity_profile="balanced",
        extensions=[".MP4", "mkv"],
    )
    key_b = build_scan_set_key(
        roots=["e:/archive", "d:/videos"],
        similarity_profile="BALANCED",
        extensions=["MKV", "mp4"],
    )
    assert key_a == key_b


def test_scan_set_key_changes_with_profile() -> None:
    balanced = build_scan_set_key(
        roots=["D:/Videos"],
        similarity_profile="balanced",
        extensions=["mp4"],
    )
    aggressive = build_scan_set_key(
        roots=["D:/Videos"],
        similarity_profile="aggressive",
        extensions=["mp4"],
    )
    assert balanced != aggressive
