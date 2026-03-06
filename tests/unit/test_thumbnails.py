from __future__ import annotations

from pathlib import Path

from video_duperz.ui.thumbnails import (
    build_thumbnail_cache_key,
    normalize_frame_pair,
    normalize_thumbnail_size_key,
    thumbnail_cache_dir,
    thumbnail_cache_path,
    thumbnail_dimensions,
    thumbnail_pair_cache_paths,
)


def test_cache_key_changes_for_inputs() -> None:
    base = build_thumbnail_cache_key("C:/vid/a.mp4", size=100, mtime_ns=10, size_key="96x54", frame_pct=23, slot="a")
    changed_path = build_thumbnail_cache_key(
        "C:/vid/b.mp4", size=100, mtime_ns=10, size_key="96x54", frame_pct=23, slot="a"
    )
    changed_size = build_thumbnail_cache_key(
        "C:/vid/a.mp4", size=101, mtime_ns=10, size_key="96x54", frame_pct=23, slot="a"
    )
    changed_mtime = build_thumbnail_cache_key(
        "C:/vid/a.mp4", size=100, mtime_ns=11, size_key="96x54", frame_pct=23, slot="a"
    )
    changed_preset = build_thumbnail_cache_key(
        "C:/vid/a.mp4", size=100, mtime_ns=10, size_key="128x72", frame_pct=23, slot="a"
    )
    changed_frame = build_thumbnail_cache_key(
        "C:/vid/a.mp4", size=100, mtime_ns=10, size_key="96x54", frame_pct=77, slot="a"
    )
    changed_slot = build_thumbnail_cache_key(
        "C:/vid/a.mp4", size=100, mtime_ns=10, size_key="96x54", frame_pct=23, slot="b"
    )

    assert base != changed_path
    assert base != changed_size
    assert base != changed_mtime
    assert base != changed_preset
    assert base != changed_frame
    assert base != changed_slot


def test_cache_path_uses_app_data_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    out = thumbnail_cache_path("C:/vid/a.mp4", size=100, mtime_ns=10, size_key="96x54", frame_pct=23, slot="a")
    cache_root = thumbnail_cache_dir()

    assert str(out).startswith(str(cache_root))
    assert out.suffix == ".jpg"
    assert cache_root.exists()


def test_size_normalization_and_dimensions() -> None:
    assert normalize_thumbnail_size_key("128x72") == "128x72"
    assert normalize_thumbnail_size_key(" 128x72 ") == "128x72"
    assert normalize_thumbnail_size_key("not-a-size") == "96x54"
    assert thumbnail_dimensions("80x45") == (80, 45)
    assert thumbnail_dimensions("not-a-size") == (96, 54)


def test_frame_pair_normalization() -> None:
    assert normalize_frame_pair(23, 77) == (23, 77)
    assert normalize_frame_pair(120, 120) == (100, 99)


def test_pair_cache_paths_are_distinct() -> None:
    a, b = thumbnail_pair_cache_paths(
        "C:/vid/a.mp4",
        size=100,
        mtime_ns=10,
        size_key="96x54",
        frame_a_pct=23,
        frame_b_pct=77,
    )
    assert a != b
