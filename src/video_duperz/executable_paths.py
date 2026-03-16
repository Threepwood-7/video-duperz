"""Helpers for normalizing and resolving external executable paths."""

from __future__ import annotations

import shutil
from pathlib import Path


def normalize_executable_override_path(value: object) -> str:
    """Normalize one persisted executable override path.

    Args:
        value: Raw persisted or widget-provided value.

    Returns:
        Expanded string path, or an empty string when unset.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return str(Path(text).expanduser())


def resolve_executable_path(
    tool_name: str,
    override_path: str = "",
    *,
    not_found_message: str,
) -> str:
    """Resolve one external executable path from override or PATH.

    Args:
        tool_name: Human-facing executable id, such as ``ffmpeg``.
        override_path: Optional configured override path.
        not_found_message: Error message used when PATH lookup fails.

    Returns:
        Concrete executable path to launch.

    Raises:
        FileNotFoundError: The override path is invalid or PATH resolution failed.
    """
    normalized_override = normalize_executable_override_path(override_path)
    if normalized_override:
        override_target = Path(normalized_override)
        if override_target.is_file():
            return str(override_target)
        raise FileNotFoundError(
            f"{tool_name} executable override path is invalid: {normalized_override}"
        )

    resolved = shutil.which(tool_name)
    if resolved:
        return resolved
    raise FileNotFoundError(not_found_message)
