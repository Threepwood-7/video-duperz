"""Optional audio-fingerprint helpers built on Chromaprint's fpcalc."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any, cast

from threep_commons.subprocess_helpers import (
    merge_subprocess_kwargs,
    windows_no_window_popen_kwargs,
)

from .executable_paths import resolve_executable_path
from .process_priority import (
    apply_scan_child_process_io_mode,
    apply_subprocess_cpu_priority_kwargs,
)

if TYPE_CHECKING:
    from .models import ScanProcessCpuPriority, ScanProcessIoMode


class AudioFingerprintError(RuntimeError):
    """Raised when one audio fingerprint cannot be produced."""


def ensure_fpcalc_available(fpcalc_exe_path: str = "") -> str:
    """Resolve the fpcalc executable path or raise a user-facing error."""
    try:
        return resolve_executable_path(
            "fpcalc",
            fpcalc_exe_path,
            not_found_message=(
                "fpcalc not found on PATH. Install Chromaprint/fpcalc or set an "
                "override path."
            ),
        )
    except FileNotFoundError as exc:
        raise AudioFingerprintError(str(exc)) from exc


def _stderr_tail(stderr_text: str, *, limit: int = 400) -> str:
    """Return a compact stderr tail for fpcalc failures."""
    text = stderr_text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def compute_audio_fingerprint(
    path: str,
    *,
    fpcalc_exe_path: str = "",
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
    scan_child_io_mode: ScanProcessIoMode = "normal",
    timeout_s: float = 20.0,
) -> str:
    """Compute one Chromaprint audio fingerprint string for a media file."""
    fpcalc_path = ensure_fpcalc_available(fpcalc_exe_path)
    command = [fpcalc_path, "-json", path]
    kwargs: dict[str, Any] = apply_subprocess_cpu_priority_kwargs(
        merge_subprocess_kwargs(
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            },
            windows_no_window_popen_kwargs(),
        ),
        scan_child_cpu_priority,
    )
    try:
        process = subprocess.Popen(command, **kwargs)
        apply_scan_child_process_io_mode(process, scan_child_io_mode)
        stdout_text, stderr_text = process.communicate(
            timeout=max(0.1, float(timeout_s))
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioFingerprintError(
            f"fpcalc timed out after {float(timeout_s):.1f}s for {path}"
        ) from exc
    except OSError as exc:
        raise AudioFingerprintError(str(exc)) from exc

    if int(process.returncode or 0) != 0:
        stderr_tail = _stderr_tail(stderr_text)
        detail = f" stderr tail: {stderr_tail}" if stderr_tail else ""
        raise AudioFingerprintError(
            "fpcalc failed for "
            f"{path} with exit code {int(process.returncode)}.{detail}"
        )

    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise AudioFingerprintError(f"fpcalc returned invalid JSON for {path}") from exc
    if not isinstance(payload, dict):
        raise AudioFingerprintError(f"fpcalc returned an invalid payload for {path}")

    payload_map = cast("dict[str, object]", payload)
    fingerprint = str(payload_map.get("fingerprint", "") or "").strip()
    if not fingerprint:
        raise AudioFingerprintError(f"fpcalc returned no fingerprint for {path}")
    return fingerprint
