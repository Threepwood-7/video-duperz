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
    return CODEC_RANK.get(codec.lower(), 0.7)


def quality_score(item: MatchItem) -> float:
    pixels = float(item.width * item.height)
    return 0.65 * pixels + 0.25 * float(item.bitrate) + 0.10 * codec_rank(item.codec)


def choose_keep_file_id(items: list[MatchItem]) -> int:
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
