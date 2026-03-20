from __future__ import annotations

from typing import TYPE_CHECKING

from video_duperz.executable_paths import (
    common_executable_candidate_paths,
    discover_executable_override_path,
    normalize_executable_override_path,
    resolve_executable_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_normalize_executable_override_path_uses_windows_separators() -> None:
    assert (
        normalize_executable_override_path("C:/Tools/ffmpeg/bin/ffmpeg.exe")
        == r"C:\Tools\ffmpeg\bin\ffmpeg.exe"
    )


def test_normalize_executable_override_path_keeps_command_names() -> None:
    assert normalize_executable_override_path("ffmpeg") == "ffmpeg"


def test_common_executable_candidate_paths_include_everything_local_appdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Test\AppData\Local")

    candidates = common_executable_candidate_paths("everything")

    assert r"C:\Program Files\Everything\Everything.exe" in candidates
    assert r"C:\Program Files (x86)\Everything\Everything.exe" in candidates
    assert (
        r"C:\Users\Test\AppData\Local\Programs\Everything\Everything.exe"
        in candidates
    )


def test_discover_executable_override_path_preserves_valid_current_path(
    tmp_path: Path,
) -> None:
    exe_path = tmp_path / "ffmpeg.exe"
    exe_path.write_text("", encoding="utf-8")

    assert discover_executable_override_path("ffmpeg", str(exe_path)) == str(exe_path)


def test_resolve_executable_path_uses_common_candidates_when_path_lookup_misses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "ffmpeg" / "bin" / "ffprobe.exe"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", "")
    monkeypatch.setattr(
        "video_duperz.executable_paths.commons_resolve_executable_path",
        lambda _tool_name: None,
    )

    resolved = resolve_executable_path(
        "ffprobe",
        not_found_message="missing",
    )

    assert resolved == normalize_executable_override_path(str(candidate))
