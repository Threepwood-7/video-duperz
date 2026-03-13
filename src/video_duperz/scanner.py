from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock

from threep_commons.fs_paths import path_key
from threep_commons.platform.windows.storage import (
    is_local_windows_path,
)
from threep_commons.platform.windows.storage import (
    list_windows_storage_roots as _list_windows_storage_roots,
)
from threep_commons.platform.windows.storage import (
    resolve_physical_disk_tokens as _resolve_physical_disk_tokens,
)
from threep_commons.platform.windows.storage import (
    resolve_volume_identity as _resolve_volume_identity,
)
from threep_commons.platform.windows.volumes import (
    get_volume_mount_point as _get_volume_mount_point,
)

from .models import ScanIssue, VideoRecord

ProgressFn = Callable[[int, int, str], None]
FileDiscoveredFn = Callable[[VideoRecord], None]
DiskToken = str
RootTokens = tuple[str, set[DiskToken]]


@dataclass(slots=True)
class PhysicalDriveInfo:
    root: str
    volume_identity: str
    disk_tokens: list[DiskToken]
    total_bytes: int | None
    free_bytes: int | None
    used_percent: float | None
    lookup_error: str | None = None


@dataclass(slots=True)
class PhysicalDriveScanPlan:
    root_tokens: list[RootTokens]
    root_groups: list[list[str]]
    root_to_group_index: dict[str, int]
    matched_volume_identities: set[str]
    lane_worker_limits: dict[int, int]
    lane_volume_identities: dict[int, list[str]]
    requested_worker_target: int
    effective_total_workers: int
    issues: list[ScanIssue]


def _windows_mount_point_for_path(path: str) -> str:
    return str(_get_volume_mount_point(path))


def _candidate_physical_drive_roots(roots: list[str] | None = None) -> list[str]:
    if roots:
        candidates = [str(Path(root)) for root in roots if str(root).strip()]
    elif os.name == "nt":
        candidates = [str(path) for path in _list_windows_storage_roots()]
    else:
        candidates = ["/"]

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not is_local_windows_path(candidate):
            continue
        root = str(Path(candidate))
        if os.name == "nt":
            try:
                root = _windows_mount_point_for_path(root)
            except OSError:
                drive = Path(root).drive
                if drive:
                    root = f"{drive}\\"
        if not os.path.isdir(root):
            continue
        key = path_key(root)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(root)
    return normalized


def list_physical_drives(roots: list[str] | None = None) -> list[PhysicalDriveInfo]:
    discovered: list[PhysicalDriveInfo] = []
    for root in _candidate_physical_drive_roots(roots):
        volume_identity = _resolve_volume_identity(root)
        lookup_error: str | None = None
        try:
            tokens = sorted(_resolve_physical_disk_tokens(root))
        except OSError as exc:
            lookup_error = str(exc).strip() or exc.__class__.__name__
            tokens = [volume_identity]

        total_bytes: int | None = None
        free_bytes: int | None = None
        used_percent: float | None = None
        try:
            usage = shutil.disk_usage(root)
            total_bytes = int(usage.total)
            free_bytes = int(usage.free)
            used_percent = (
                (((total_bytes - free_bytes) / total_bytes) * 100.0)
                if total_bytes > 0
                else 0.0
            )
        except OSError:
            pass

        discovered.append(
            PhysicalDriveInfo(
                root=root,
                volume_identity=volume_identity,
                disk_tokens=tokens,
                total_bytes=total_bytes,
                free_bytes=free_bytes,
                used_percent=used_percent,
                lookup_error=lookup_error,
            )
        )

    discovered.sort(key=lambda item: path_key(item.root))
    return discovered


def _normalize_worker_override_map(value: dict[str, int] | None) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for raw_key, raw_workers in value.items():
        key = str(raw_key).strip()
        if not key:
            continue
        try:
            workers = int(raw_workers)
        except (TypeError, ValueError):
            continue
        normalized[key] = max(1, workers)
    return normalized


def build_physical_drive_scan_plan(
    roots: list[str],
    max_workers: int,
    drive_worker_overrides: dict[str, int] | None = None,
) -> PhysicalDriveScanPlan:
    normalized_roots = [str(Path(root)) for root in roots]
    requested_floor = max(1, int(max_workers))
    normalized_overrides = _normalize_worker_override_map(drive_worker_overrides)
    if not normalized_roots:
        return PhysicalDriveScanPlan(
            root_tokens=[],
            root_groups=[],
            root_to_group_index={},
            matched_volume_identities=set(),
            lane_worker_limits={},
            lane_volume_identities={},
            requested_worker_target=requested_floor,
            effective_total_workers=0,
            issues=[],
        )

    root_tokens: list[RootTokens] = []
    issues: list[ScanIssue] = []
    matched_volume_identities: set[str] = set()
    root_volume_identity: dict[str, str] = {}

    for idx, root in enumerate(normalized_roots):
        if not is_local_windows_path(root) or not os.path.isdir(root):
            root_tokens.append((root, {f"root:{idx}"}))
            continue

        volume_identity = _resolve_volume_identity(root)
        root_volume_identity[root] = volume_identity
        matched_volume_identities.add(volume_identity)
        try:
            tokens = _resolve_physical_disk_tokens(root)
            root_tokens.append((root, tokens or {volume_identity}))
        except OSError as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            issues.append(
                ScanIssue(
                    stage="enumerate",
                    path=root,
                    message=(
                        "Physical disk lookup failed; using volume identity "
                        f"fallback: {detail}"
                    ),
                )
            )
            root_tokens.append((root, {volume_identity or f"root:{idx}"}))

    groups = _group_roots_by_token_connectivity(root_tokens)
    root_to_group_index: dict[str, int] = {}
    lane_volume_identities: dict[int, list[str]] = {}
    lane_worker_limits: dict[int, int] = {}

    for lane_idx, group_roots in enumerate(groups):
        for root in group_roots:
            root_to_group_index[root] = lane_idx
        lane_identities = sorted(
            {
                root_volume_identity[root]
                for root in group_roots
                if root_volume_identity.get(root)
            },
            key=str.casefold,
        )
        lane_volume_identities[lane_idx] = lane_identities
        if lane_identities:
            lane_caps = [
                normalized_overrides.get(identity, 1) for identity in lane_identities
            ]
            lane_limit = min(lane_caps) if lane_caps else 1
        else:
            lane_limit = 1
        lane_worker_limits[lane_idx] = max(1, int(lane_limit))

    sum_lane_caps = sum(lane_worker_limits.values())
    requested_worker_target = max(requested_floor, sum_lane_caps)
    effective_total_workers = sum_lane_caps
    return PhysicalDriveScanPlan(
        root_tokens=root_tokens,
        root_groups=groups,
        root_to_group_index=root_to_group_index,
        matched_volume_identities=matched_volume_identities,
        lane_worker_limits=lane_worker_limits,
        lane_volume_identities=lane_volume_identities,
        requested_worker_target=requested_worker_target,
        effective_total_workers=effective_total_workers,
        issues=issues,
    )


def _group_roots_by_token_connectivity(
    root_tokens: list[RootTokens],
) -> list[list[str]]:
    if not root_tokens:
        return []
    parents = list(range(len(root_tokens)))

    def _find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def _union(a: int, b: int) -> None:
        root_a = _find(a)
        root_b = _find(b)
        if root_a != root_b:
            parents[root_b] = root_a

    token_owner: dict[DiskToken, int] = {}
    for idx, (_, tokens_raw) in enumerate(root_tokens):
        tokens = tokens_raw or {f"root:{idx}"}
        for token in tokens:
            owner = token_owner.get(token)
            if owner is None:
                token_owner[token] = idx
                continue
            _union(idx, owner)

    groups: dict[int, list[str]] = {}
    for idx, (root, _) in enumerate(root_tokens):
        group_key = _find(idx)
        groups.setdefault(group_key, []).append(root)
    return list(groups.values())


def _enumerate_root(
    scan_id: int,
    root: str,
    ext_set: set[str],
    cancel_event: Event | None,
    source_root: str,
    parallel_lane: int,
    on_file_discovered: FileDiscoveredFn | None = None,
) -> tuple[list[VideoRecord], list[ScanIssue]]:
    found: list[VideoRecord] = []
    issues: list[ScanIssue] = []
    if not is_local_windows_path(root):
        issues.append(
            ScanIssue(
                stage="enumerate",
                path=root,
                message="Network paths are not supported in v1",
            )
        )
        return found, issues
    if not os.path.exists(root):
        issues.append(
            ScanIssue(stage="enumerate", path=root, message="Path does not exist")
        )
        return found, issues
    if not os.path.isdir(root):
        issues.append(
            ScanIssue(stage="enumerate", path=root, message="Path is not a directory")
        )
        return found, issues

    stack: list[str] = [root]
    while stack:
        if cancel_event and cancel_event.is_set():
            break
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    if cancel_event and cancel_event.is_set():
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError as exc:
                        issues.append(
                            ScanIssue(
                                stage="enumerate",
                                path=entry.path,
                                message=f"Unreadable entry: {exc}",
                            )
                        )
                        continue

                    ext = Path(entry.name).suffix.lower().lstrip(".")
                    if ext not in ext_set:
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        issues.append(
                            ScanIssue(
                                stage="enumerate",
                                path=entry.path,
                                message=f"Unreadable file: {exc}",
                            )
                        )
                        continue
                    found.append(
                        VideoRecord(
                            path=entry.path,
                            size=int(st.st_size),
                            mtime_ns=int(st.st_mtime_ns),
                            ctime_ns=int(st.st_ctime_ns),
                            ext=ext,
                            scan_id=scan_id,
                            source_root=source_root,
                            parallel_lane=parallel_lane,
                        )
                    )
                    if on_file_discovered:
                        on_file_discovered(found[-1])
        except OSError as exc:
            issues.append(
                ScanIssue(
                    stage="enumerate",
                    path=current_dir,
                    message=f"Unreadable directory: {exc}",
                )
            )
    return found, issues


def enumerate_video_files(
    scan_id: int,
    roots: list[str],
    extensions: list[str],
    max_workers: int = 1,
    drive_worker_overrides: dict[str, int] | None = None,
    cancel_event: Event | None = None,
    progress_cb: ProgressFn | None = None,
    on_file_discovered: FileDiscoveredFn | None = None,
) -> tuple[list[VideoRecord], list[ScanIssue]]:
    ext_set = {e.lower().lstrip(".") for e in extensions}
    found: list[VideoRecord] = []
    issues: list[ScanIssue] = []
    normalized_roots = [str(Path(root)) for root in roots]
    root_count = max(1, len(normalized_roots))
    plan = build_physical_drive_scan_plan(
        roots=normalized_roots,
        max_workers=max_workers,
        drive_worker_overrides=drive_worker_overrides,
    )
    issues.extend(plan.issues)

    groups = plan.root_groups
    if not groups:
        return found, issues

    worker_count = min(len(groups), max(1, int(plan.requested_worker_target)))
    progress_lock = Lock()
    completed_roots = 0
    active_workers = 0

    def _mark_root_complete(root: str) -> None:
        nonlocal completed_roots
        with progress_lock:
            completed_roots += 1
            current = completed_roots
            active = active_workers
        if progress_cb:
            progress_cb(
                current,
                root_count,
                f"Enumerated {root} [workers {active}/{worker_count}]",
            )

    def _set_worker_delta(delta: int) -> None:
        nonlocal active_workers
        with progress_lock:
            active_workers = max(0, active_workers + delta)

    def _enumerate_group(
        lane_index: int, group_roots: list[str]
    ) -> tuple[list[VideoRecord], list[ScanIssue]]:
        group_found: list[VideoRecord] = []
        group_issues: list[ScanIssue] = []
        _set_worker_delta(1)
        try:
            for root in group_roots:
                if cancel_event and cancel_event.is_set():
                    break
                root_found, root_issues = _enumerate_root(
                    scan_id=scan_id,
                    root=root,
                    ext_set=ext_set,
                    cancel_event=cancel_event,
                    source_root=root,
                    parallel_lane=lane_index,
                    on_file_discovered=on_file_discovered,
                )
                group_found.extend(root_found)
                group_issues.extend(root_issues)
                _mark_root_complete(root)
        finally:
            _set_worker_delta(-1)
        return group_found, group_issues

    if worker_count == 1:
        for lane_idx, group in enumerate(groups):
            group_found, group_issues = _enumerate_group(lane_idx, group)
            found.extend(group_found)
            issues.extend(group_issues)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures: dict[
                Future[tuple[list[VideoRecord], list[ScanIssue]]], tuple[int, list[str]]
            ] = {
                executor.submit(_enumerate_group, lane_idx, group): (lane_idx, group)
                for lane_idx, group in enumerate(groups)
            }
            for future in as_completed(futures):
                _lane_idx, group = futures[future]
                try:
                    group_found, group_issues = future.result()
                except Exception as exc:
                    group_path = ", ".join(group)
                    issues.append(
                        ScanIssue(
                            stage="enumerate",
                            path=group_path,
                            message=f"Worker failed: {exc}",
                        )
                    )
                    continue
                found.extend(group_found)
                issues.extend(group_issues)

    return found, issues
