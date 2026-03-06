from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from video_duperz import scanner


def _write_video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_grouping_distinct_disk_tokens_stays_separate() -> None:
    groups = scanner._group_roots_by_token_connectivity(
        [
            ("R:/Videos", {"disk:0"}),
            ("S:/Archive", {"disk:1"}),
        ]
    )
    assert groups == [["R:/Videos"], ["S:/Archive"]]


def test_grouping_mount_points_sharing_disk_are_serialized_together() -> None:
    groups = scanner._group_roots_by_token_connectivity(
        [
            ("C:/Videos", {"disk:5"}),
            ("C:/mnt/videos", {"disk:5"}),
        ]
    )
    assert groups == [["C:/Videos", "C:/mnt/videos"]]


def test_grouping_overlapping_disk_token_sets_uses_connectivity() -> None:
    groups = scanner._group_roots_by_token_connectivity(
        [
            ("A:/root", {"disk:1"}),
            ("B:/root", {"disk:1", "disk:2"}),
            ("C:/root", {"disk:2"}),
        ]
    )
    assert groups == [["A:/root", "B:/root", "C:/root"]]


def test_candidate_physical_drive_roots_uses_windows_mount_points(monkeypatch) -> None:
    monkeypatch.setattr(scanner.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        scanner,
        "_windows_mounted_volume_paths",
        lambda: ["C:\\", "C:\\M\\DISK7\\", "C:\\M\\DISK7\\"],
    )
    monkeypatch.setattr(
        scanner,
        "_windows_mount_point_for_path",
        lambda path: path if path.endswith("\\") else f"{path}\\",
    )
    monkeypatch.setattr(scanner.os.path, "isdir", lambda _path: True)

    roots = scanner._candidate_physical_drive_roots()

    assert roots == ["C:\\", "C:\\M\\DISK7\\"]


def test_lookup_failure_falls_back_to_volume_identity_and_emits_issue(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    _write_video(root / "clip.mp4")

    def _raise_lookup(_path: str) -> set[str]:
        raise OSError("disk lookup boom")

    monkeypatch.setattr(scanner, "_resolve_physical_disk_tokens", _raise_lookup)
    monkeypatch.setattr(scanner, "_resolve_volume_identity", lambda _path: "volume:fallback")

    found, issues = scanner.enumerate_video_files(
        scan_id=7,
        roots=[str(root)],
        extensions=["mp4"],
        max_workers=4,
    )

    assert [Path(item.path).name for item in found] == ["clip.mp4"]
    assert any(
        issue.stage == "enumerate"
        and issue.path == str(root)
        and "Physical disk lookup failed" in issue.message
        for issue in issues
    )


def test_parallel_enumeration_returns_files_from_all_roots_without_global_sort(tmp_path: Path, monkeypatch) -> None:
    root_z = tmp_path / "z_root"
    root_a = tmp_path / "a_root"
    _write_video(root_z / "z.mp4")
    _write_video(root_a / "a.mp4")

    def _tokens(path: str) -> set[str]:
        if Path(path).name == "z_root":
            return {"disk:1"}
        return {"disk:2"}

    monkeypatch.setattr(scanner, "_resolve_physical_disk_tokens", _tokens)

    found, issues = scanner.enumerate_video_files(
        scan_id=11,
        roots=[str(root_z), str(root_a)],
        extensions=["mp4"],
        max_workers=2,
    )

    assert not issues
    assert {Path(item.path).name for item in found} == {"a.mp4", "z.mp4"}


def test_parallel_cancellation_returns_partial_results_without_crash(tmp_path: Path, monkeypatch) -> None:
    root_fast = tmp_path / "fast"
    root_slow = tmp_path / "slow"
    _write_video(root_fast / "fast.mp4")
    for idx in range(40):
        _write_video(root_slow / f"slow_{idx:03d}.mp4")

    def _tokens(path: str) -> set[str]:
        if Path(path).name == "fast":
            return {"disk:10"}
        return {"disk:20"}

    monkeypatch.setattr(scanner, "_resolve_physical_disk_tokens", _tokens)

    original_stat = scanner.os.stat

    def _slow_stat(path: str | os.PathLike[str], *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        text = str(path)
        if str(root_slow) in text and text.lower().endswith(".mp4"):
            time.sleep(0.003)
        return result

    monkeypatch.setattr(scanner.os, "stat", _slow_stat)

    cancel_event = Event()

    def _progress(current: int, _total: int, _message: str) -> None:
        if current >= 1:
            cancel_event.set()

    found, issues = scanner.enumerate_video_files(
        scan_id=19,
        roots=[str(root_fast), str(root_slow)],
        extensions=["mp4"],
        max_workers=2,
        cancel_event=cancel_event,
        progress_cb=_progress,
    )

    assert issues == []
    assert len(found) >= 1
    assert len(found) < 41


def test_list_physical_drives_reports_tokens_and_capacity(monkeypatch) -> None:
    monkeypatch.setattr(scanner, "_candidate_physical_drive_roots", lambda _roots=None: ["C:\\", "D:\\"])
    monkeypatch.setattr(scanner, "_resolve_volume_identity", lambda path: f"volume:{path.lower()}")
    monkeypatch.setattr(
        scanner,
        "_resolve_physical_disk_tokens",
        lambda path: {"disk:0"} if path.upper().startswith("C:") else {"disk:2", "disk:1"},
    )
    monkeypatch.setattr(scanner.shutil, "disk_usage", lambda _path: SimpleNamespace(total=100, free=40))

    drives = scanner.list_physical_drives()

    assert [item.root for item in drives] == ["C:\\", "D:\\"]
    assert drives[0].disk_tokens == ["disk:0"]
    assert drives[1].disk_tokens == ["disk:1", "disk:2"]
    assert drives[0].used_percent == 60.0
    assert drives[1].lookup_error is None


def test_list_physical_drives_falls_back_to_volume_token_on_lookup_error(monkeypatch) -> None:
    monkeypatch.setattr(scanner, "_candidate_physical_drive_roots", lambda _roots=None: ["E:\\"])
    monkeypatch.setattr(scanner, "_resolve_volume_identity", lambda _path: "volume:e")

    def _raise_lookup(_path: str) -> set[str]:
        raise OSError("no mapping")

    monkeypatch.setattr(scanner, "_resolve_physical_disk_tokens", _raise_lookup)
    monkeypatch.setattr(scanner.shutil, "disk_usage", lambda _path: SimpleNamespace(total=200, free=100))

    drives = scanner.list_physical_drives()

    assert len(drives) == 1
    assert drives[0].disk_tokens == ["volume:e"]
    assert drives[0].lookup_error == "no mapping"
    assert drives[0].used_percent == 50.0


def test_build_physical_drive_scan_plan_resolves_per_root_tokens_and_emits_lookup_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()

    monkeypatch.setattr(
        scanner,
        "_resolve_volume_identity",
        lambda path: "volume:a" if str(path) == str(root_a) else "volume:b",
    )
    monkeypatch.setattr(
        scanner,
        "_resolve_physical_disk_tokens",
        lambda path: {"disk:0"} if str(path) == str(root_a) else (_ for _ in ()).throw(OSError("lookup failed")),
    )

    plan = scanner.build_physical_drive_scan_plan(
        roots=[str(root_a), str(root_b)],
        max_workers=8,
        drive_worker_overrides={"volume:a": 3, "volume:b": 2},
    )

    lane_a = plan.root_to_group_index[str(root_a)]
    lane_b = plan.root_to_group_index[str(root_b)]
    assert plan.matched_volume_identities == {"volume:a", "volume:b"}
    assert lane_a != lane_b
    assert plan.lane_worker_limits[lane_a] == 3
    assert plan.lane_worker_limits[lane_b] == 2
    assert plan.effective_total_workers == 5
    assert plan.requested_worker_target == 8
    assert any(issue.path == str(root_b) and "using volume identity fallback" in issue.message for issue in plan.issues)


def test_build_physical_drive_scan_plan_lane_limit_uses_hardest_cap_with_shared_lane(
    tmp_path: Path, monkeypatch
) -> None:
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()

    monkeypatch.setattr(
        scanner,
        "_resolve_volume_identity",
        lambda path: "volume:a" if str(path) == str(root_a) else "volume:b",
    )
    monkeypatch.setattr(scanner, "_resolve_physical_disk_tokens", lambda _path: {"disk:1"})

    plan = scanner.build_physical_drive_scan_plan(
        roots=[str(root_a), str(root_b)],
        max_workers=1,
        drive_worker_overrides={"volume:a": 4, "volume:b": 2},
    )

    assert len(plan.root_groups) == 1
    assert plan.lane_volume_identities[0] == ["volume:a", "volume:b"]
    assert plan.lane_worker_limits[0] == 2
    assert plan.effective_total_workers == 2


def test_windows_disk_extent_payload_parser_uses_aligned_extents_offset() -> None:
    if not hasattr(scanner, "_parse_disk_numbers_from_volume_extents_payload"):
        pytest.skip("Windows disk-extent parser is unavailable on this platform")

    extent_size = 24
    extents_offset = 8
    payload = bytearray(extents_offset + (extent_size * 2))

    # Header: NumberOfDiskExtents = 2
    payload[0:4] = (2).to_bytes(4, "little")
    # Deliberately set header padding to a non-zero value to ensure parser does not read from offset 4.
    payload[4:8] = (999).to_bytes(4, "little")

    # First DISK_EXTENT starts at aligned offset 8.
    payload[8:12] = (7).to_bytes(4, "little")
    # Second DISK_EXTENT starts at offset 8 + 24.
    payload[32:36] = (42).to_bytes(4, "little")

    disks = scanner._parse_disk_numbers_from_volume_extents_payload(
        bytes(payload),
        2,
        extent_size=extent_size,
        extents_offset=extents_offset,
    )
    assert disks == {7, 42}


def test_enumerate_video_files_stamps_source_root_and_lane(tmp_path: Path, monkeypatch) -> None:
    root_a = tmp_path / "lane_a"
    root_b = tmp_path / "lane_b"
    _write_video(root_a / "a.mp4")
    _write_video(root_b / "b.mp4")

    monkeypatch.setattr(
        scanner,
        "_resolve_volume_identity",
        lambda path: "volume:a" if str(path) == str(root_a) else "volume:b",
    )
    monkeypatch.setattr(
        scanner,
        "_resolve_physical_disk_tokens",
        lambda path: {"disk:10"} if str(path) == str(root_a) else {"disk:20"},
    )

    found, issues = scanner.enumerate_video_files(
        scan_id=55,
        roots=[str(root_a), str(root_b)],
        extensions=["mp4"],
        max_workers=4,
    )

    assert not issues
    assert len(found) == 2
    source_roots = {item.source_root for item in found}
    assert source_roots == {str(root_a), str(root_b)}
    lanes_by_root = {item.source_root: item.parallel_lane for item in found}
    assert lanes_by_root[str(root_a)] != lanes_by_root[str(root_b)]
