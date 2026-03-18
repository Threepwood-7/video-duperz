"""Windows scan-priority helpers for current and child processes."""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from .models import ScanProcessCpuPriority, ScanProcessIoMode

LOGGER = logging.getLogger(__name__)

CPU_PRIORITY_OPTIONS: tuple[tuple[str, ScanProcessCpuPriority], ...] = (
    ("Idle", "idle"),
    ("Below Normal", "below_normal"),
    ("Normal", "normal"),
    ("Above Normal", "above_normal"),
    ("High", "high"),
)
IO_MODE_OPTIONS: tuple[tuple[str, ScanProcessIoMode], ...] = (
    ("Normal", "normal"),
    ("Background", "background"),
)
_CPU_PRIORITY_FLAG_NAMES: dict[ScanProcessCpuPriority, str | None] = {
    "idle": "IDLE_PRIORITY_CLASS",
    "below_normal": "BELOW_NORMAL_PRIORITY_CLASS",
    "normal": None,
    "above_normal": "ABOVE_NORMAL_PRIORITY_CLASS",
    "high": "HIGH_PRIORITY_CLASS",
}
_CPU_PRIORITY_DEFAULT: ScanProcessCpuPriority = "normal"
_IO_MODE_DEFAULT: ScanProcessIoMode = "normal"
_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_ACCESS = (
    _PROCESS_SET_INFORMATION
    | _PROCESS_QUERY_INFORMATION
    | _PROCESS_QUERY_LIMITED_INFORMATION
)
_PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
_PROCESS_MODE_BACKGROUND_END = 0x00200000


class _Kernel32Function(Protocol):
    """Callable Win32 function pointer with configurable ctypes metadata."""

    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> object:
        """Invoke the wrapped Win32 API function."""
        ...


@dataclass(slots=True)
class CurrentProcessPriorityState:
    """Best-effort snapshot used to restore the app process after a scan."""

    original_priority_class: int | None
    background_mode_applied: bool = False


def normalize_scan_cpu_priority(
    value: object,
    default: ScanProcessCpuPriority = _CPU_PRIORITY_DEFAULT,
) -> ScanProcessCpuPriority:
    """Normalize one persisted CPU priority string to a supported value."""
    text = str(value or "").strip().lower()
    if text in _CPU_PRIORITY_FLAG_NAMES:
        return text
    return default


def normalize_scan_io_mode(
    value: object,
    default: ScanProcessIoMode = _IO_MODE_DEFAULT,
) -> ScanProcessIoMode:
    """Normalize one persisted scan I/O mode string to a supported value."""
    text = str(value or "").strip().lower()
    if text in {"normal", "background"}:
        return cast("ScanProcessIoMode", text)
    return default


def apply_subprocess_cpu_priority_kwargs(
    kwargs: dict[str, object],
    cpu_priority: ScanProcessCpuPriority,
) -> dict[str, object]:
    """Return subprocess kwargs merged with the requested CPU priority class."""
    merged = dict(kwargs)
    priority_flags = cpu_priority_creationflags(cpu_priority)
    existing_raw = merged.get("creationflags", 0)
    existing_flags = (
        int(existing_raw) if isinstance(existing_raw, int | float | str) else 0
    )
    combined = existing_flags | priority_flags
    if combined:
        merged["creationflags"] = combined
    elif "creationflags" in merged:
        merged["creationflags"] = existing_flags
    return merged


def cpu_priority_creationflags(cpu_priority: ScanProcessCpuPriority) -> int:
    """Return the Windows creation flags for one child CPU priority choice."""
    if sys.platform != "win32":
        return 0
    flag_name = _CPU_PRIORITY_FLAG_NAMES[normalize_scan_cpu_priority(cpu_priority)]
    if not flag_name:
        return 0
    raw_value = getattr(subprocess, flag_name, 0)
    return int(raw_value) if isinstance(raw_value, int | float | str) else 0


def apply_scan_priority_to_current_process(
    cpu_priority: ScanProcessCpuPriority,
    io_mode: ScanProcessIoMode,
) -> CurrentProcessPriorityState | None:
    """Apply scan-time CPU and I/O priority to the current process."""
    if sys.platform != "win32":
        return None
    handle = _get_current_process()
    original_priority_class = _get_priority_class(handle)
    normalized_cpu = normalize_scan_cpu_priority(cpu_priority)
    normalized_io = normalize_scan_io_mode(io_mode)
    if normalized_cpu != "normal":
        _set_priority_class(handle, _priority_class_for_cpu(normalized_cpu))
    background_mode_applied = False
    if normalized_io == "background":
        background_mode_applied = _set_priority_class(
            handle,
            _PROCESS_MODE_BACKGROUND_BEGIN,
        )
    return CurrentProcessPriorityState(
        original_priority_class=original_priority_class,
        background_mode_applied=background_mode_applied,
    )


def restore_scan_priority_to_current_process(
    state: CurrentProcessPriorityState | None,
) -> None:
    """Restore the current process priority after one scan completes."""
    if sys.platform != "win32" or state is None:
        return
    handle = _get_current_process()
    if state.background_mode_applied:
        _set_priority_class(handle, _PROCESS_MODE_BACKGROUND_END)
    if state.original_priority_class:
        _set_priority_class(handle, state.original_priority_class)


def apply_scan_child_process_io_mode(
    process: subprocess.Popen[str] | subprocess.Popen[bytes],
    io_mode: ScanProcessIoMode,
) -> None:
    """Apply scan child-process I/O background mode when requested."""
    if sys.platform != "win32":
        return
    if normalize_scan_io_mode(io_mode) != "background":
        return
    handle = _open_process(process.pid)
    if handle is None:
        return
    try:
        _set_priority_class(handle, _PROCESS_MODE_BACKGROUND_BEGIN)
    finally:
        _close_handle(handle)


def _priority_class_for_cpu(cpu_priority: ScanProcessCpuPriority) -> int:
    """Return the Win32 priority class value for one CPU priority selection."""
    flag_name = _CPU_PRIORITY_FLAG_NAMES[normalize_scan_cpu_priority(cpu_priority)]
    if not flag_name:
        return 0
    raw_value = getattr(subprocess, flag_name, 0)
    return int(raw_value) if isinstance(raw_value, int | float | str) else 0


def _kernel32() -> ctypes.WinDLL | None:
    """Return the Windows kernel32 DLL when available."""
    if sys.platform != "win32":
        return None
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _named_kernel32_function(name: str) -> _Kernel32Function | None:
    """Return one kernel32 function pointer when available."""
    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    function = getattr(kernel32, name, None)
    return cast("_Kernel32Function | None", function)


def _get_current_process() -> int | None:
    """Return the current-process pseudo handle."""
    func = _named_kernel32_function("GetCurrentProcess")
    if func is None:
        return None
    func.restype = ctypes.c_void_p
    handle = cast("int | None", func())
    return int(handle) if handle else None


def _open_process(pid: int) -> int | None:
    """Open one process handle for priority adjustments."""
    func = _named_kernel32_function("OpenProcess")
    if func is None:
        return None
    func.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    func.restype = ctypes.c_void_p
    handle = cast("int | None", func(_PROCESS_ACCESS, 0, max(0, int(pid))))
    if not handle:
        _log_last_error("OpenProcess")
        return None
    return int(handle)


def _close_handle(handle: int | None) -> None:
    """Close one Win32 handle when needed."""
    if not handle:
        return
    func = _named_kernel32_function("CloseHandle")
    if func is None:
        return
    func.argtypes = [ctypes.c_void_p]
    func.restype = ctypes.c_int
    func(ctypes.c_void_p(handle))


def _get_priority_class(handle: int | None) -> int | None:
    """Return the current Win32 priority class for one process handle."""
    if not handle:
        return None
    func = _named_kernel32_function("GetPriorityClass")
    if func is None:
        return None
    func.argtypes = [ctypes.c_void_p]
    func.restype = ctypes.c_uint32
    value = int(cast("int", func(ctypes.c_void_p(handle))))
    if value == 0:
        _log_last_error("GetPriorityClass")
        return None
    return value


def _set_priority_class(handle: int | None, priority_class: int) -> bool:
    """Set one Win32 priority/background mode value on a process handle."""
    if not handle or priority_class <= 0:
        return False
    func = _named_kernel32_function("SetPriorityClass")
    if func is None:
        return False
    func.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    func.restype = ctypes.c_int
    success = bool(cast("int", func(ctypes.c_void_p(handle), int(priority_class))))
    if not success:
        _log_last_error("SetPriorityClass")
    return success


def _log_last_error(operation: str) -> None:
    """Log the last Win32 error without interrupting the scan flow."""
    error_code = int(ctypes.get_last_error())
    LOGGER.debug("%s failed with Win32 error %s", operation, error_code)
