"""Shared media-format classification helpers used by runtime policies."""

from __future__ import annotations

from pathlib import PurePath

PROBLEMATIC_MEDIA_SUFFIXES = frozenset(
    {".wmv", ".asf", ".avi", ".mov", ".mpg", ".mpeg", ".flv"}
)


def normalize_media_suffix(path_or_suffix: str) -> str:
    """Return the normalized lowercase media suffix for one path-like value."""
    candidate = str(path_or_suffix).strip()
    if not candidate:
        return ""
    if candidate.startswith(".") and candidate.count(".") == 1:
        return candidate.lower()
    return PurePath(candidate).suffix.lower()


def is_problematic_media_path(path_or_suffix: str) -> bool:
    """Return whether one media path/suffix uses the problematic-format policy."""
    return normalize_media_suffix(path_or_suffix) in PROBLEMATIC_MEDIA_SUFFIXES
