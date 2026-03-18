"""Video-extension preset definitions shared by settings and UI layers."""

from __future__ import annotations

from .scan_sets import normalize_extensions

VIDEO_EXTENSION_PRESET_NAMES: tuple[str, str, str] = ("basic", "medium", "broad")
DEFAULT_VIDEO_EXTENSION_PRESET = "broad"
VIDEO_EXTENSION_PRESETS: dict[str, tuple[str, ...]] = {
    "basic": ("mp4", "mkv", "mov"),
    "medium": (
        "mp4",
        "mkv",
        "mov",
        "avi",
        "webm",
        "m4v",
        "mpg",
        "mpeg",
        "ts",
        "m2ts",
    ),
    "broad": (
        "mp4",
        "mkv",
        "mov",
        "avi",
        "webm",
        "m4v",
        "mpg",
        "mpeg",
        "ts",
        "m2ts",
        "mts",
        "wmv",
        "asf",
        "flv",
        "f4v",
        "3gp",
        "3g2",
        "vob",
        "ogv",
        "ogm",
        "mxf",
        "rm",
        "rmvb",
    ),
}
VIDEO_EXTENSION_PRESET_CSV: dict[str, str] = {
    name: ", ".join(extensions) for name, extensions in VIDEO_EXTENSION_PRESETS.items()
}
_VIDEO_EXTENSION_PRESET_KEYS: dict[str, frozenset[str]] = {
    name: frozenset(extensions) for name, extensions in VIDEO_EXTENSION_PRESETS.items()
}
COMMON_VIDEO_EXTENSIONS = list(VIDEO_EXTENSION_PRESETS[DEFAULT_VIDEO_EXTENSION_PRESET])


def normalize_video_extension_preset_name(value: str | None) -> str:
    """Return a known preset name, falling back to the default preset."""
    candidate = str(value or "").strip().lower()
    if candidate in VIDEO_EXTENSION_PRESETS:
        return candidate
    return DEFAULT_VIDEO_EXTENSION_PRESET


def video_extensions_for_preset(preset_name: str | None) -> list[str]:
    """Expand a preset name into the configured list of video extensions."""
    normalized_name = normalize_video_extension_preset_name(preset_name)
    return list(VIDEO_EXTENSION_PRESETS[normalized_name])


def video_extensions_csv_for_preset(preset_name: str | None) -> str:
    """Return the display-friendly comma-separated extension list for a preset."""
    normalized_name = normalize_video_extension_preset_name(preset_name)
    return VIDEO_EXTENSION_PRESET_CSV[normalized_name]


def detect_video_extension_preset(extensions: list[str]) -> str | None:
    """Match a normalized extension list back to a known preset when possible."""
    normalized = normalize_extensions(extensions)
    if not normalized:
        return None
    ext_set = frozenset(normalized)
    for name in VIDEO_EXTENSION_PRESET_NAMES:
        preset_set = _VIDEO_EXTENSION_PRESET_KEYS[name]
        if len(ext_set) == len(preset_set) and ext_set == preset_set:
            return name
    return None
