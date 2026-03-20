"""Helpers for normalizing, discovering, and resolving external executable paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from threep_commons.executables import (
    find_first_available_executable as commons_find_first_available_executable,
)
from threep_commons.executables import (
    program_files_candidates as commons_program_files_candidates,
)
from threep_commons.executables import (
    resolve_executable_path as commons_resolve_executable_path,
)
from threep_commons.fs_paths import is_explicit_path_text, normalize_windows_path_text

if TYPE_CHECKING:
    from collections.abc import Sequence

_VISIBLE_TOOL_SETTING_KEYS: tuple[str, ...] = (
    "ffmpeg_exe_path",
    "ffprobe_exe_path",
    "fpcalc_exe_path",
    "mediainfo_exe_path",
    "everything_exe_path",
)

_DISCOVERY_COMMAND_NAMES: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("ffmpeg", "ffmpeg.exe"),
    "ffprobe": ("ffprobe", "ffprobe.exe"),
    "fpcalc": ("fpcalc", "fpcalc.exe"),
    "mediainfo": ("mediainfo", "MediaInfo", "MediaInfo.exe"),
    "everything": ("Everything.exe", "Everything"),
}

_DISCOVERY_PROGRAM_FILES_RELATIVE_PATHS: dict[str, tuple[Path, ...]] = {
    "ffmpeg": (Path("ffmpeg") / "bin" / "ffmpeg.exe",),
    "ffprobe": (Path("ffmpeg") / "bin" / "ffprobe.exe",),
    "fpcalc": (Path("Chromaprint") / "fpcalc.exe",),
    "mediainfo": (Path("MediaInfo") / "MediaInfo.exe",),
    "everything": (Path("Everything") / "Everything.exe",),
}


def normalize_executable_override_path(value: object) -> str:
    """Normalize one persisted executable override path.

    Args:
        value: Raw persisted or widget-provided value.

    Returns:
        Expanded path text in canonical Windows form, or an empty string when unset.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    expanded_text = str(Path(text).expanduser())
    return normalize_windows_path_text(expanded_text)


def resolve_executable_path(
    tool_name: str,
    override_path: str = "",
    *,
    not_found_message: str,
    fallback_paths: Sequence[str] = (),
) -> str:
    """Resolve one external executable path from override or PATH.

    Args:
        tool_name: Human-facing executable id, such as ``ffmpeg``.
        override_path: Optional configured override path.
        not_found_message: Error message used when PATH lookup fails.
        fallback_paths: Extra absolute candidate paths checked after PATH lookup.

    Returns:
        Concrete executable path to launch.

    Raises:
        FileNotFoundError: The override path is invalid or resolution failed.
    """
    normalized_override = normalize_executable_override_path(override_path)
    if normalized_override:
        override_target = Path(normalized_override)
        if override_target.is_file():
            return normalize_executable_override_path(str(override_target))
        raise FileNotFoundError(
            f"{tool_name} executable override path is invalid: {normalized_override}"
        )

    resolved = commons_resolve_executable_path(tool_name)
    if resolved is not None and resolved.is_file():
        return normalize_executable_override_path(str(resolved))

    for candidate in _normalized_fallback_paths(
        tool_name,
        fallback_paths=fallback_paths,
    ):
        if candidate.is_file():
            return normalize_executable_override_path(str(candidate))
    raise FileNotFoundError(not_found_message)


def common_executable_candidate_paths(tool_name: str) -> list[str]:
    """Return the common app-level candidate paths for one known tool.

    Args:
        tool_name: Tool id or executable name.

    Returns:
        Candidate filesystem paths in the order they should be tested.
    """
    tool_key = _tool_key(tool_name)
    if not tool_key:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for relative_path in _DISCOVERY_PROGRAM_FILES_RELATIVE_PATHS.get(tool_key, ()):
        for candidate in commons_program_files_candidates(relative_path):
            _append_candidate_text(
                candidates,
                seen,
                normalize_executable_override_path(str(candidate)),
            )
    if tool_key == "everything":
        local_appdata_text = normalize_windows_path_text(
            str(os.environ.get("LOCALAPPDATA", "")).strip()
        )
        if local_appdata_text:
            _append_candidate_text(
                candidates,
                seen,
                normalize_executable_override_path(
                    str(
                        Path(local_appdata_text)
                        / "Programs"
                        / "Everything"
                        / "Everything.exe"
                    )
                ),
            )
    return candidates


def discover_executable_override_path(tool_name: str, current_value: str = "") -> str:
    """Discover one concrete executable path for the given tool.

    Args:
        tool_name: Logical tool id, such as ``ffmpeg`` or ``everything``.
        current_value: Current text from the UI field, if any.

    Returns:
        One normalized absolute path when discovery succeeds, otherwise an empty
        string.
    """
    tool_key = _tool_key(tool_name)
    if not tool_key:
        return ""
    preferred_text = normalize_executable_override_path(current_value)
    resolved = commons_find_first_available_executable(
        preferred=preferred_text or None,
        command_names=_DISCOVERY_COMMAND_NAMES.get(tool_key, ()),
        candidate_paths=common_executable_candidate_paths(tool_key),
    )
    if resolved is None or not resolved.is_file():
        return ""
    return normalize_executable_override_path(str(resolved))


def discover_default_executable_settings() -> dict[str, str]:
    """Return first-run discovered values for the visible Sources tool fields."""
    return {
        setting_key: discover_executable_override_path(
            setting_key.removesuffix("_exe_path")
        )
        for setting_key in _VISIBLE_TOOL_SETTING_KEYS
    }


def _tool_key(tool_name: str) -> str:
    """Normalize one logical tool name into the internal discovery key."""
    text = str(tool_name or "").strip().strip('"').casefold()
    if text.endswith(".exe"):
        text = text.removesuffix(".exe")
    return text


def _normalized_fallback_paths(
    tool_name: str,
    *,
    fallback_paths: Sequence[str],
) -> list[Path]:
    """Return normalized fallback candidate paths for one tool."""
    candidates = (
        list(fallback_paths)
        if fallback_paths
        else common_executable_candidate_paths(tool_name)
    )
    normalized_candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized_text = normalize_executable_override_path(candidate)
        if not normalized_text or not is_explicit_path_text(normalized_text):
            continue
        key = normalized_text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized_candidates.append(Path(normalized_text))
    return normalized_candidates


def _append_candidate_text(
    candidates: list[str],
    seen: set[str],
    candidate_text: str,
) -> None:
    """Append one normalized candidate path if it is not already present."""
    if not candidate_text:
        return
    key = candidate_text.casefold()
    if key in seen:
        return
    seen.add(key)
    candidates.append(candidate_text)
