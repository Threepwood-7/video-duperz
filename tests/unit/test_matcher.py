from __future__ import annotations

from video_duperz.matcher import build_duplicate_groups, find_duplicate_edges
from video_duperz.models import MatchItem


def _item(
    file_id: int, path: str, hashes: list[int], duration: float = 10.0
) -> MatchItem:
    return MatchItem(
        file_id=file_id,
        path=path,
        size=1000 + file_id,
        mtime_ns=file_id,
        ctime_ns=file_id,
        duration_s=duration,
        width=1920,
        height=1080,
        fps=30.0,
        codec="h264",
        bitrate=2_000_000,
        audio_codec="aac",
        audio_bitrate=192000,
        audio_languages="eng",
        subtitle_languages="",
        is_hdr=False,
        hashes=hashes,
    )


def test_find_duplicate_edges_balanced() -> None:
    same = [0x1111111111111111] * 12
    near = [0x1111111111111111] * 11 + [0x1111111111111110]
    far = [0xFFFFFFFFFFFFFFFF] * 12
    items = [
        _item(1, "a.mp4", same),
        _item(2, "b.mp4", near),
        _item(3, "c.mp4", far),
    ]
    edges, stats = find_duplicate_edges(items, profile="balanced")
    pairs = {(e.file_a, e.file_b) for e in edges} | {
        (e.file_b, e.file_a) for e in edges
    }
    assert (1, 2) in pairs
    assert (1, 3) not in pairs
    assert stats.accepted_pairs >= 1
    assert stats.full_distance_pairs >= stats.accepted_pairs


def test_build_duplicate_groups() -> None:
    same = [0x1234567890ABCDEF] * 12
    items = [
        _item(1, "a.mp4", same),
        _item(2, "b.mp4", same),
    ]
    edges, stats = find_duplicate_edges(items, profile="balanced")
    groups = build_duplicate_groups(items, edges, profile="balanced")
    assert len(groups) == 1
    assert len(groups[0].items) == 2
    assert stats.accepted_pairs == 1


def test_prefilter_reduces_full_distance_evaluations() -> None:
    base = [0x1111111111111111] * 12
    near = [0x1111111111111111] * 11 + [0x1111111111111110]
    far_a = [0x0000000000000000] * 12
    far_b = [0xFFFFFFFFFFFFFFFF] * 12
    far_c = [0xAAAAAAAAAAAAAAAA] * 12
    items = [
        _item(1, "a.mp4", base),
        _item(2, "b.mp4", near),
        _item(3, "c.mp4", far_a),
        _item(4, "d.mp4", far_b),
        _item(5, "e.mp4", far_c),
    ]
    _edges, stats = find_duplicate_edges(items, profile="balanced")
    assert stats.candidate_pairs > 0
    assert stats.prefilter_pairs > 0
    assert stats.prefilter_rejected_pairs > 0
    assert stats.full_distance_pairs < stats.prefilter_pairs
