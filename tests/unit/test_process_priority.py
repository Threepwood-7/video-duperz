from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING

from video_duperz.process_priority import (
    apply_scan_child_process_io_mode,
    apply_scan_priority_to_current_process,
    apply_subprocess_cpu_priority_kwargs,
    normalize_scan_cpu_priority,
    normalize_scan_io_mode,
    restore_scan_priority_to_current_process,
)

if TYPE_CHECKING:
    import pytest


def test_process_priority_normalization_defaults_to_safe_values() -> None:
    assert normalize_scan_cpu_priority("HIGH") == "high"
    assert normalize_scan_cpu_priority("unknown") == "normal"
    assert normalize_scan_io_mode("background") == "background"
    assert normalize_scan_io_mode("unknown") == "normal"


def test_apply_subprocess_cpu_priority_kwargs_merges_creationflags() -> None:
    kwargs = apply_subprocess_cpu_priority_kwargs(
        {"creationflags": 0x08000000},
        "high",
    )

    assert kwargs["creationflags"] == (0x08000000 | int(subprocess.HIGH_PRIORITY_CLASS))


def test_apply_scan_priority_to_current_process_applies_cpu_and_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[int | None, int]] = []

    monkeypatch.setattr(
        "video_duperz.process_priority._get_current_process",
        lambda: 99,
    )
    monkeypatch.setattr(
        "video_duperz.process_priority._get_priority_class",
        lambda handle: int(subprocess.NORMAL_PRIORITY_CLASS),
    )
    monkeypatch.setattr(
        "video_duperz.process_priority._set_priority_class",
        lambda handle, priority_class: seen.append((handle, priority_class)) or True,
    )

    state = apply_scan_priority_to_current_process("above_normal", "background")

    assert state is not None
    assert seen == [
        (99, int(subprocess.ABOVE_NORMAL_PRIORITY_CLASS)),
        (99, 0x00100000),
    ]


def test_restore_scan_priority_to_current_process_ends_background_and_restores_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[int | None, int]] = []

    monkeypatch.setattr(
        "video_duperz.process_priority._get_current_process",
        lambda: 321,
    )
    monkeypatch.setattr(
        "video_duperz.process_priority._set_priority_class",
        lambda handle, priority_class: seen.append((handle, priority_class)) or True,
    )

    restore_scan_priority_to_current_process(
        SimpleNamespace(
            original_priority_class=int(subprocess.BELOW_NORMAL_PRIORITY_CLASS),
            background_mode_applied=True,
        )
    )

    assert seen == [
        (321, 0x00200000),
        (321, int(subprocess.BELOW_NORMAL_PRIORITY_CLASS)),
    ]


def test_apply_scan_child_process_io_mode_applies_background_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[int | None, int]] = []

    monkeypatch.setattr(
        "video_duperz.process_priority._open_process",
        lambda pid: pid,
    )
    monkeypatch.setattr(
        "video_duperz.process_priority._set_priority_class",
        lambda handle, priority_class: seen.append((handle, priority_class)) or True,
    )
    monkeypatch.setattr(
        "video_duperz.process_priority._close_handle",
        lambda handle: None,
    )

    apply_scan_child_process_io_mode(SimpleNamespace(pid=77), "normal")
    apply_scan_child_process_io_mode(SimpleNamespace(pid=77), "background")

    assert seen == [(77, 0x00100000)]
