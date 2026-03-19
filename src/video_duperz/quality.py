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

H264_PROFILE_BONUS = {
    "baseline": 1.000,
    "main": 1.006,
    "high": 1.012,
    "high 10": 1.018,
    "high 4:2:2": 1.022,
    "high 4:4:4": 1.026,
}

HEVC_PROFILE_BONUS = {
    "main": 1.000,
    "main 10": 1.014,
    "main 12": 1.020,
    "main 4:2:2 10": 1.024,
    "main 4:2:2 12": 1.028,
    "main 4:4:4": 1.030,
}


def codec_rank(codec: str) -> float:
    """Return a coarse quality weight for a video codec name."""
    return CODEC_RANK.get(codec.lower(), 0.7)


def _normalized_codec_family(codec: str) -> str:
    """Normalize one codec string to the broad family used by heuristics."""
    normalized = codec.strip().lower()
    if normalized == "h265":
        return "hevc"
    return normalized


def bit_depth_bonus(bit_depth: int) -> float:
    """Return the quality multiplier for one normalized bit depth."""
    if bit_depth >= 12:
        return 1.10
    if bit_depth >= 10:
        return 1.05
    return 1.00


def codec_profile_bonus(codec: str, codec_profile: str) -> float:
    """Return the conservative codec-profile quality multiplier."""
    family = _normalized_codec_family(codec)
    profile_key = codec_profile.strip().lower()
    if family == "h264":
        return H264_PROFILE_BONUS.get(profile_key, 1.0)
    if family == "hevc":
        return HEVC_PROFILE_BONUS.get(profile_key, 1.0)
    return 1.0


def codec_level_value(codec_level: str) -> float:
    """Parse one normalized codec-level string into a sortable float."""
    raw = codec_level.strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def progressive_bonus(is_interlaced: bool) -> float:
    """Return the small progressive/interlaced quality multiplier."""
    return 1.00 if is_interlaced else 1.01


def quality_score_from_fields(
    *,
    width: int,
    height: int,
    bit_depth: int,
    bitrate: int,
    codec: str,
    codec_profile: str,
    is_interlaced: bool,
) -> float:
    """Score one video using the shared keep-best heuristic."""
    pixels = float(width * height)
    base_score = 0.65 * pixels + 0.25 * float(bitrate) + 0.10 * codec_rank(codec)
    return (
        base_score
        * bit_depth_bonus(bit_depth)
        * codec_profile_bonus(codec, codec_profile)
        * progressive_bonus(is_interlaced)
    )


def quality_rank_tuple_from_fields(
    *,
    width: int,
    height: int,
    bit_depth: int,
    bitrate: int,
    codec: str,
    codec_profile: str,
    codec_level: str,
    is_interlaced: bool,
) -> tuple[float, float, int]:
    """Return the sortable quality tuple used by keep-best decisions."""
    return (
        quality_score_from_fields(
            width=width,
            height=height,
            bit_depth=bit_depth,
            bitrate=bitrate,
            codec=codec,
            codec_profile=codec_profile,
            is_interlaced=is_interlaced,
        ),
        codec_level_value(codec_level),
        1 if is_interlaced else 0,
    )


def quality_score(item: MatchItem) -> float:
    """Score a match item by resolution, bitrate, and codec preference."""
    return quality_score_from_fields(
        width=item.width,
        height=item.height,
        bit_depth=item.bit_depth,
        bitrate=item.bitrate,
        codec=item.codec,
        codec_profile=item.codec_profile,
        is_interlaced=item.is_interlaced,
    )


def choose_keep_file_id(items: list[MatchItem]) -> int:
    """Choose the file id that should be kept by default within a group."""
    if not items:
        raise ValueError("choose_keep_file_id requires at least one item")
    sorted_items = sorted(
        items,
        key=lambda i: (
            -quality_rank_tuple_from_fields(
                width=i.width,
                height=i.height,
                bit_depth=i.bit_depth,
                bitrate=i.bitrate,
                codec=i.codec,
                codec_profile=i.codec_profile,
                codec_level=i.codec_level,
                is_interlaced=i.is_interlaced,
            )[0],
            -quality_rank_tuple_from_fields(
                width=i.width,
                height=i.height,
                bit_depth=i.bit_depth,
                bitrate=i.bitrate,
                codec=i.codec,
                codec_profile=i.codec_profile,
                codec_level=i.codec_level,
                is_interlaced=i.is_interlaced,
            )[1],
            quality_rank_tuple_from_fields(
                width=i.width,
                height=i.height,
                bit_depth=i.bit_depth,
                bitrate=i.bitrate,
                codec=i.codec,
                codec_profile=i.codec_profile,
                codec_level=i.codec_level,
                is_interlaced=i.is_interlaced,
            )[2],
            i.mtime_ns,  # older (smaller mtime) first
            i.path.lower(),  # deterministic lexical tie-break
        ),
    )
    return sorted_items[0].file_id
