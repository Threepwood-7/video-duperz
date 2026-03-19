"""Shared constants and helper types for the results view modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict, cast

from PySide6.QtCore import Qt


class DeleteTarget(TypedDict):
    """Selected result-row metadata used for delete and rename operations."""

    row: int
    file_id: int
    group_db_id: int
    path: str


def payload_dict(value: Any) -> dict[str, object]:
    """Normalize loosely typed worker payloads into string-key dictionaries."""
    if not isinstance(value, dict):
        return {}
    raw_map = cast("dict[object, object]", value)
    normalized: dict[str, object] = {}
    for key, raw in raw_map.items():
        if isinstance(key, str | int | float | bool):
            normalized[str(key)] = raw
    return normalized


def coerce_int(value: object, default: int = 0) -> int:
    """Convert a loosely typed payload value into an integer."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


RESULTS_HEADERS = [
    "Group ID",
    "Checkbox",
    "=",
    "Thumbnail",
    "File Name",
    "Extension",
    "Container",
    "Size (Bytes)",
    "Resolution",
    "FPS",
    "Interlaced",
    "Bit Depth",
    "HDR Format",
    "Duration",
    "Video Codec",
    "Codec Profile",
    "Codec Level",
    "Audio Streams",
    "Audio Codec",
    "Audio Bitrate",
    "Audio Lang(s)",
    "Sub Lang(s)",
    "Bitrate",
    "Similarity",
    "Match",
    "Last Modified",
    "Parent Dir",
    "Full Path",
]

COL_GROUP_ID = 0
COL_CHECK = 1
COL_IDENTICAL = 2
COL_THUMB = 3
COL_FILE_NAME = 4
COL_EXTENSION = 5
COL_CONTAINER = 6
COL_SIZE = 7
COL_RESOLUTION = 8
COL_FPS = 9
COL_INTERLACED = 10
COL_BIT_DEPTH = 11
COL_HDR_FORMAT = 12
COL_DURATION = 13
COL_VIDEO_CODEC = 14
COL_CODEC_PROFILE = 15
COL_CODEC_LEVEL = 16
COL_AUDIO_STREAMS = 17
COL_AUDIO_CODEC = 18
COL_AUDIO_BITRATE = 19
COL_AUDIO_LANGS = 20
COL_SUB_LANGS = 21
COL_BITRATE = 22
COL_SIMILARITY = 23
COL_MATCH = 24
COL_LAST_MODIFIED = 25
COL_PARENT_DIR = 26
COL_FULL_PATH = 27

META_ROLE = Qt.ItemDataRole.UserRole
THUMB_GAP = 6
SORT_NONE = "none"
SORT_GROUP_SIZE_DESC = "group_size_desc"
SORT_GROUP_SIZE_ASC = "group_size_asc"
SORT_GROUP_COUNT_DESC = "group_count_desc"
SORT_GROUP_COUNT_ASC = "group_count_asc"
SORT_ROW_SIZE_DESC = "row_size_desc"
SORT_ROW_SIZE_ASC = "row_size_asc"
SORT_GROUP_SPREAD_DESC = "group_spread_desc"
SORT_GROUP_SPREAD_ASC = "group_spread_asc"
VALID_SORT_MODES = {
    SORT_NONE,
    SORT_GROUP_SIZE_DESC,
    SORT_GROUP_SIZE_ASC,
    SORT_GROUP_COUNT_DESC,
    SORT_GROUP_COUNT_ASC,
    SORT_ROW_SIZE_DESC,
    SORT_ROW_SIZE_ASC,
    SORT_GROUP_SPREAD_DESC,
    SORT_GROUP_SPREAD_ASC,
}
HDR_FILTER_ANY = "any"
HDR_FILTER_ONLY = "hdr_only"
HDR_FILTER_EXCLUDE = "sdr_only"
HDR_FILTER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Any", HDR_FILTER_ANY),
    ("HDR only", HDR_FILTER_ONLY),
    ("SDR only", HDR_FILTER_EXCLUDE),
)


@dataclass(slots=True)
class ResultsFilterState:
    """Normalized active filter state for the results table."""

    include_name_terms: tuple[str, ...] = ()
    include_path_terms: tuple[str, ...] = ()
    exclude_name_terms: tuple[str, ...] = ()
    exclude_path_terms: tuple[str, ...] = ()
    include_match_all: bool = False
    min_size_mib: float | None = None
    max_size_mib: float | None = None
    min_duration_s: float | None = None
    max_duration_s: float | None = None
    min_similarity: float | None = None
    min_width: int | None = None
    min_height: int | None = None
    extension: str = ""
    video_codec: str = ""
    hdr_mode: str = HDR_FILTER_ANY

    def has_include_filters(self) -> bool:
        """Return whether any include-style filter is currently active."""
        return bool(
            self.include_name_terms
            or self.include_path_terms
            or self.min_size_mib is not None
            or self.max_size_mib is not None
            or self.min_duration_s is not None
            or self.max_duration_s is not None
            or self.min_similarity is not None
            or self.min_width is not None
            or self.min_height is not None
            or self.extension
            or self.video_codec
            or self.hdr_mode != HDR_FILTER_ANY
        )


@dataclass(slots=True)
class RowMeta:
    """Per-row metadata attached to the results table for quick lookup."""

    group_db_id: int
    file_id: int
    path: str
    size: int
    mtime_ns: int
    width: int
    height: int
    bit_depth: int
    codec: str
    codec_profile: str
    codec_level: str
    container: str
    is_interlaced: bool
    bitrate: int
    similarity: float
    keep_default: bool


@dataclass(slots=True)
class GroupRenderContext:
    """Cached render state reused while populating one visible group."""

    group_index: int
    group_db_id: int
    display_group_id: str
    group_key: str
    cached_labels: dict[int, str]
    cached_errors: dict[int, str]
    cached_group_error: str
