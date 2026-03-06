from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock

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

    @property
    def effective_workers(self) -> int:
        # Backward-compatible alias used by pre-plan UI/tests.
        return int(self.effective_total_workers)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _GET_VOLUME_PATH_NAME = _KERNEL32.GetVolumePathNameW
    _GET_VOLUME_PATH_NAME.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    _GET_VOLUME_PATH_NAME.restype = wintypes.BOOL

    _GET_LOGICAL_DRIVES = _KERNEL32.GetLogicalDrives
    _GET_LOGICAL_DRIVES.argtypes = []
    _GET_LOGICAL_DRIVES.restype = wintypes.DWORD

    _GET_VOLUME_NAME_FOR_MOUNT = _KERNEL32.GetVolumeNameForVolumeMountPointW
    _GET_VOLUME_NAME_FOR_MOUNT.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    _GET_VOLUME_NAME_FOR_MOUNT.restype = wintypes.BOOL

    _GET_VOLUME_PATH_NAMES_FOR_VOLUME_NAME = _KERNEL32.GetVolumePathNamesForVolumeNameW
    _GET_VOLUME_PATH_NAMES_FOR_VOLUME_NAME.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _GET_VOLUME_PATH_NAMES_FOR_VOLUME_NAME.restype = wintypes.BOOL

    _FIND_FIRST_VOLUME = _KERNEL32.FindFirstVolumeW
    _FIND_FIRST_VOLUME.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
    _FIND_FIRST_VOLUME.restype = wintypes.HANDLE

    _FIND_NEXT_VOLUME = _KERNEL32.FindNextVolumeW
    _FIND_NEXT_VOLUME.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
    _FIND_NEXT_VOLUME.restype = wintypes.BOOL

    _FIND_VOLUME_CLOSE = _KERNEL32.FindVolumeClose
    _FIND_VOLUME_CLOSE.argtypes = [wintypes.HANDLE]
    _FIND_VOLUME_CLOSE.restype = wintypes.BOOL

    _CREATE_FILE = _KERNEL32.CreateFileW
    _CREATE_FILE.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CREATE_FILE.restype = wintypes.HANDLE

    _DEVICE_IO_CONTROL = _KERNEL32.DeviceIoControl
    _DEVICE_IO_CONTROL.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _DEVICE_IO_CONTROL.restype = wintypes.BOOL

    _CLOSE_HANDLE = _KERNEL32.CloseHandle
    _CLOSE_HANDLE.argtypes = [wintypes.HANDLE]
    _CLOSE_HANDLE.restype = wintypes.BOOL

    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _ERROR_MORE_DATA = 234
    _ERROR_INSUFFICIENT_BUFFER = 122
    _ERROR_NO_MORE_FILES = 18
    _IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000

    class _DiskExtent(ctypes.Structure):
        _fields_ = [
            ("DiskNumber", wintypes.DWORD),
            ("StartingOffset", ctypes.c_longlong),
            ("ExtentLength", ctypes.c_longlong),
        ]

    class _VolumeDiskExtents(ctypes.Structure):
        _fields_ = [
            ("NumberOfDiskExtents", wintypes.DWORD),
            ("Extents", _DiskExtent * 1),
        ]

    _VOLUME_DISK_EXTENTS_EXTENTS_OFFSET = _VolumeDiskExtents.Extents.offset
    _DISK_EXTENT_SIZE = ctypes.sizeof(_DiskExtent)
    _DWORD_SIZE = ctypes.sizeof(wintypes.DWORD)

    def _parse_disk_numbers_from_volume_extents_payload(
        payload: bytes,
        extent_count: int,
        *,
        extent_size: int,
        extents_offset: int,
    ) -> set[int]:
        disks: set[int] = set()
        for idx in range(max(0, int(extent_count))):
            start = int(extents_offset) + (idx * int(extent_size))
            end = start + int(_DWORD_SIZE)
            if end > len(payload):
                raise OSError("Volume disk extent payload ended unexpectedly")
            disks.add(int.from_bytes(payload[start:end], "little"))
        return disks

    def _windows_mount_point_for_path(path: str) -> str:
        normalized = os.path.abspath(path)
        mount_point = ctypes.create_unicode_buffer(4096)
        if not _GET_VOLUME_PATH_NAME(normalized, mount_point, len(mount_point)):
            raise ctypes.WinError(ctypes.get_last_error())
        mount = str(mount_point.value)
        if mount and not mount.endswith("\\"):
            mount = f"{mount}\\"
        return mount

    def _windows_volume_name_for_path(path: str) -> str:
        mount = _windows_mount_point_for_path(path)
        volume = ctypes.create_unicode_buffer(4096)
        if not _GET_VOLUME_NAME_FOR_MOUNT(mount, volume, len(volume)):
            raise ctypes.WinError(ctypes.get_last_error())
        return str(volume.value)

    def _windows_mount_points_for_volume_name(volume_name: str) -> list[str]:
        size = 512
        max_size = 1 << 20  # 1 MiB hard cap for pathological path-multi-string results.
        while size <= max_size:
            buffer = ctypes.create_unicode_buffer(size)
            required = wintypes.DWORD(0)
            ok = _GET_VOLUME_PATH_NAMES_FOR_VOLUME_NAME(
                volume_name,
                buffer,
                size,
                ctypes.byref(required),
            )
            if ok:
                raw = ctypes.wstring_at(buffer, size)
                return [item if item.endswith("\\") else f"{item}\\" for item in raw.split("\x00") if item]
            err = ctypes.get_last_error()
            if err in (_ERROR_MORE_DATA, _ERROR_INSUFFICIENT_BUFFER):
                size = max(size * 2, int(required.value) + 1)
                continue
            raise ctypes.WinError(err)
        raise OSError("Windows mount-point query exceeded supported buffer sizes")

    def _windows_mounted_volume_paths() -> list[str]:
        volume = ctypes.create_unicode_buffer(4096)
        handle = _FIND_FIRST_VOLUME(volume, len(volume))
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        mounts: list[str] = []
        try:
            while True:
                volume_name = str(volume.value)
                mounts.extend(_windows_mount_points_for_volume_name(volume_name))
                ok = _FIND_NEXT_VOLUME(handle, volume, len(volume))
                if ok:
                    continue
                err = ctypes.get_last_error()
                if err == _ERROR_NO_MORE_FILES:
                    break
                raise ctypes.WinError(err)
            return mounts
        finally:
            _FIND_VOLUME_CLOSE(handle)

    def _windows_disk_numbers_for_path(path: str) -> set[int]:
        volume_name = _windows_volume_name_for_path(path).rstrip("\\")
        handle = _CREATE_FILE(
            volume_name,
            0,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = 4096
            max_size = 1 << 20  # 1 MiB hard cap for pathological multi-extent cases.
            while size <= max_size:
                out_buffer = ctypes.create_string_buffer(size)
                bytes_returned = wintypes.DWORD(0)
                ok = _DEVICE_IO_CONTROL(
                    handle,
                    _IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                    None,
                    0,
                    out_buffer,
                    size,
                    ctypes.byref(bytes_returned),
                    None,
                )
                if not ok:
                    err = ctypes.get_last_error()
                    if err in (_ERROR_MORE_DATA, _ERROR_INSUFFICIENT_BUFFER):
                        size *= 2
                        continue
                    raise ctypes.WinError(err)
                if bytes_returned.value < int(_VOLUME_DISK_EXTENTS_EXTENTS_OFFSET):
                    raise OSError("No disk extent data returned")
                count = int.from_bytes(out_buffer.raw[: int(_DWORD_SIZE)], "little")
                if count <= 0:
                    raise OSError("Volume has no disk extents")
                needed = int(_VOLUME_DISK_EXTENTS_EXTENTS_OFFSET) + (int(_DISK_EXTENT_SIZE) * count)
                if bytes_returned.value < needed:
                    size = max(size * 2, needed)
                    continue
                disks = _parse_disk_numbers_from_volume_extents_payload(
                    out_buffer.raw,
                    count,
                    extent_size=int(_DISK_EXTENT_SIZE),
                    extents_offset=int(_VOLUME_DISK_EXTENTS_EXTENTS_OFFSET),
                )
                if disks:
                    return disks
                raise OSError("Volume disk extent query returned no disk numbers")
            raise OSError("Volume disk extent query output exceeded supported buffer sizes")
        finally:
            _CLOSE_HANDLE(handle)

else:
    def _windows_mount_point_for_path(path: str) -> str:
        raise OSError(f"Windows volume API is unavailable on this platform: {path}")

    def _windows_volume_name_for_path(path: str) -> str:
        raise OSError(f"Windows volume API is unavailable on this platform: {path}")

    def _windows_mount_points_for_volume_name(volume_name: str) -> list[str]:
        raise OSError(f"Windows mount-point API is unavailable on this platform: {volume_name}")

    def _windows_mounted_volume_paths() -> list[str]:
        raise OSError("Windows volume enumeration API is unavailable on this platform")

    def _windows_disk_numbers_for_path(path: str) -> set[int]:
        raise OSError(f"Windows disk extent API is unavailable on this platform: {path}")


def is_local_windows_path(path: str) -> bool:
    # v1 scope is local paths only.
    return not path.startswith("\\\\")


def _normalized_path_key(path: str) -> str:
    return str(Path(path)).replace("\\", "/").casefold()


def _resolve_volume_identity(path: str) -> str:
    normalized = str(Path(path))
    if os.name == "nt":
        try:
            volume_name = _windows_volume_name_for_path(normalized)
            return f"volume:{volume_name.casefold()}"
        except OSError:
            pass
    try:
        return f"dev:{int(os.stat(normalized).st_dev)}"
    except OSError:
        anchor = Path(normalized).anchor or normalized
        return f"path:{anchor.casefold()}"


def _resolve_physical_disk_tokens(path: str) -> set[DiskToken]:
    normalized = str(Path(path))
    if os.name == "nt":
        disks = _windows_disk_numbers_for_path(normalized)
        if not disks:
            raise OSError("No physical disks found for volume")
        return {f"disk:{disk}" for disk in sorted(disks)}
    return {f"dev:{int(os.stat(normalized).st_dev)}"}


def _candidate_physical_drive_roots(roots: list[str] | None = None) -> list[str]:
    if roots:
        candidates = [str(Path(root)) for root in roots if str(root).strip()]
    elif os.name == "nt":
        try:
            candidates = _windows_mounted_volume_paths()
        except OSError:
            bitmask = int(_GET_LOGICAL_DRIVES())
            candidates = [f"{chr(ord('A') + idx)}:\\" for idx in range(26) if bitmask & (1 << idx)]
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
        key = _normalized_path_key(root)
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
            used_percent = (((total_bytes - free_bytes) / total_bytes) * 100.0) if total_bytes > 0 else 0.0
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

    discovered.sort(key=lambda item: _normalized_path_key(item.root))
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
                    message=f"Physical disk lookup failed; using volume identity fallback: {detail}",
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
            lane_caps = [normalized_overrides.get(identity, 1) for identity in lane_identities]
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


def _group_roots_by_token_connectivity(root_tokens: list[RootTokens]) -> list[list[str]]:
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
        issues.append(ScanIssue(stage="enumerate", path=root, message="Network paths are not supported in v1"))
        return found, issues
    if not os.path.exists(root):
        issues.append(ScanIssue(stage="enumerate", path=root, message="Path does not exist"))
        return found, issues
    if not os.path.isdir(root):
        issues.append(ScanIssue(stage="enumerate", path=root, message="Path is not a directory"))
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
                        issues.append(ScanIssue(stage="enumerate", path=entry.path, message=f"Unreadable file: {exc}"))
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
            progress_cb(current, root_count, f"Enumerated {root} [workers {active}/{worker_count}]")

    def _set_worker_delta(delta: int) -> None:
        nonlocal active_workers
        with progress_lock:
            active_workers = max(0, active_workers + delta)

    def _enumerate_group(lane_index: int, group_roots: list[str]) -> tuple[list[VideoRecord], list[ScanIssue]]:
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
            futures: dict[Future[tuple[list[VideoRecord], list[ScanIssue]]], tuple[int, list[str]]] = {
                executor.submit(_enumerate_group, lane_idx, group): (lane_idx, group)
                for lane_idx, group in enumerate(groups)
            }
            for future in as_completed(futures):
                _lane_idx, group = futures[future]
                try:
                    group_found, group_issues = future.result()
                except Exception as exc:
                    group_path = ", ".join(group)
                    issues.append(ScanIssue(stage="enumerate", path=group_path, message=f"Worker failed: {exc}"))
                    continue
                found.extend(group_found)
                issues.extend(group_issues)

    return found, issues
