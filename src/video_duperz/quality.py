"""Quality heuristics used to choose the preferred file within a group."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import MatchItem

CODEC_RANK = {
    "hevc": 1.0,
    "h265": 1.0,
    "av1": 1.0,
    "h264": 0.9,
    "vp9": 0.85,
}


def codec_rank(codec: str) -> float:
    """Return a coarse quality weight for a video codec name."""
    return CODEC_RANK.get(codec.lower(), 0.7)


def bit_depth_bonus(bit_depth: int) -> float:
    """Return the quality multiplier for one normalized bit depth."""
    if bit_depth >= 12:
        return 1.10
    if bit_depth >= 10:
        return 1.05
    return 1.00


def quality_score(item: MatchItem) -> float:
    """Score a match item by resolution, bitrate, and codec preference."""
    pixels = float(item.width * item.height)
    base_score = (
        0.65 * pixels + 0.25 * float(item.bitrate) + 0.10 * codec_rank(item.codec)
    )
    return base_score * bit_depth_bonus(item.bit_depth)


def choose_keep_file_id(items: list[MatchItem]) -> int:
    """Choose the file id that should be kept by default within a group."""
    if not items:
        raise ValueError("choose_keep_file_id requires at least one item")
    sorted_items = sorted(
        items,
        key=lambda i: (
            -quality_score(i),  # higher quality first
            i.mtime_ns,  # older (smaller mtime) first
            i.path.lower(),  # deterministic lexical tie-break
        ),
    )
    return sorted_items[0].file_id
