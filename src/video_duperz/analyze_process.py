"""Process-isolated analyze helpers and child-process transport."""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from threep_commons.subprocess_helpers import (
    merge_subprocess_kwargs,
    windows_no_window_popen_kwargs,
    windows_no_window_run_kwargs,
)

from .fingerprint import (
    FingerprintError,
    build_fingerprint_record_with_fallback,
    ensure_fingerprint_fallback_chain_available,
)
from .models import FrameDecodeBackendId, ProbeBackendId, VideoMeta
from .probe import ProbeError, ensure_probe_fallback_chain_available, probe_video

if TYPE_CHECKING:
    from collections.abc import Callable

ANALYZE_STOP_GRACE_S = 2.0


@dataclass(slots=True)
class AnalyzeOutput:
    """Analyze result payload returned from the child process."""

    meta: VideoMeta
    hashes: list[int]
    probe_s: float = 0.0
    fingerprint_s: float = 0.0
    probe_fallback_backend: ProbeBackendId | None = None
    fingerprint_fallback_decoder: FrameDecodeBackendId | None = None


@dataclass(slots=True)
class AnalyzeProcessResult:
    """Normalized exit payload collected by the parent runtime."""

    kind: Literal["success", "error", "crash"]
    output: AnalyzeOutput | None = None
    issue_stage: str = "analyze"
    issue_message: str = ""


class AnalyzeTaskHandle(Protocol):
    """Parent-side handle for one in-flight analyze child process."""

    def is_running(self) -> bool:
        """Return whether the child is still running."""
        ...

    def request_stop(self) -> None:
        """Ask the child to stop cooperatively."""
        ...

    def kill(self) -> None:
        """Forcefully terminate the child process tree."""
        ...

    def collect_result(self) -> AnalyzeProcessResult | None:
        """Return the exit payload once the child has finished."""
        ...


class AnalyzeLauncher(Protocol):
    """Parent-side launcher used by the scan runtime."""

    def launch(self, path: str, cached_meta: VideoMeta | None) -> AnalyzeTaskHandle:
        """Start one analyze child process."""
        ...


class _AnalyzeStopRequestedError(RuntimeError):
    """Raised when the parent requested a cooperative child shutdown."""


def _alternate_probe_backend(backend: ProbeBackendId) -> ProbeBackendId:
    """Return the alternate metadata backend for a scan."""
    if backend == "ffprobe":
        return "pyav"
    return "ffprobe"


def ensure_analyze_fallback_chain_available(
    probe_backend: ProbeBackendId = "pyav",
) -> None:
    """Raise when the analyze fallback chain is not fully available."""
    ensure_probe_fallback_chain_available(probe_backend)
    ensure_fingerprint_fallback_chain_available()


def _raise_if_stop_requested(stop_requested: Callable[[], bool] | None) -> None:
    """Abort cooperatively when the parent requested child shutdown."""
    if stop_requested is not None and stop_requested():
        raise _AnalyzeStopRequestedError("Analyze child stop requested.")


def _analyze_once(
    path: str,
    cached_meta: VideoMeta | None,
    *,
    probe_backend: ProbeBackendId,
    relaxed_probe: bool,
    stop_requested: Callable[[], bool] | None = None,
) -> AnalyzeOutput:
    """Run one full analyze attempt with one metadata backend selection."""
    if cached_meta is None:
        _raise_if_stop_requested(stop_requested)
        probe_started = time.perf_counter()
        meta = probe_video(path, backend=probe_backend, relaxed=relaxed_probe)
        probe_s = max(0.0, time.perf_counter() - probe_started)
    else:
        meta = cached_meta
        probe_s = 0.0

    _raise_if_stop_requested(stop_requested)
    fp_started = time.perf_counter()
    fp_result = build_fingerprint_record_with_fallback(
        file_id=0,
        duration_s=meta.duration_s,
        path=path,
    )
    fingerprint_s = max(0.0, time.perf_counter() - fp_started)
    return AnalyzeOutput(
        meta=meta,
        hashes=fp_result.record.hashes,
        probe_s=probe_s,
        fingerprint_s=fingerprint_s,
        fingerprint_fallback_decoder=fp_result.fallback_decoder,
    )


def analyze_video_file(
    path: str,
    cached_meta: VideoMeta | None,
    *,
    probe_backend: ProbeBackendId,
    stop_requested: Callable[[], bool] | None = None,
) -> AnalyzeOutput:
    """Run analyze with alternate metadata fallback after any primary failure."""
    try:
        return _analyze_once(
            path,
            cached_meta,
            probe_backend=probe_backend,
            relaxed_probe=False,
            stop_requested=stop_requested,
        )
    except Exception:
        if cached_meta is not None:
            raise
        fallback_backend = _alternate_probe_backend(probe_backend)
        _raise_if_stop_requested(stop_requested)
        output = _analyze_once(
            path,
            None,
            probe_backend=fallback_backend,
            relaxed_probe=True,
            stop_requested=stop_requested,
        )
        output.probe_fallback_backend = fallback_backend
        return output


def analyze_file_ffprobe(
    path: str,
    cached_meta: VideoMeta | None,
) -> AnalyzeOutput:
    """Preserve the legacy ffprobe-only analyze seam used by tests."""
    return analyze_video_file(
        path,
        cached_meta,
        probe_backend="ffprobe",
    )


def _meta_from_payload(payload: object) -> VideoMeta | None:
    """Decode one cached-meta payload from JSON."""
    payload_map = _payload_map(payload)
    if payload_map is None:
        return None
    return VideoMeta(
        duration_s=_float_field(payload_map, "duration_s", 0.0),
        width=_int_field(payload_map, "width", 0),
        height=_int_field(payload_map, "height", 0),
        fps=_float_field(payload_map, "fps", 0.0),
        codec=_str_field(payload_map, "codec", ""),
        bitrate=_int_field(payload_map, "bitrate", 0),
        has_audio=_bool_field(payload_map, "has_audio", False),
        audio_codec=_str_field(payload_map, "audio_codec", ""),
        audio_bitrate=_int_field(payload_map, "audio_bitrate", 0),
        audio_languages=_str_field(payload_map, "audio_languages", ""),
        subtitle_languages=_str_field(payload_map, "subtitle_languages", ""),
        is_hdr=_bool_field(payload_map, "is_hdr", False),
    )


def _payload_map(payload: object) -> dict[str, object] | None:
    """Normalize one JSON-like mapping payload to string keys."""
    if not isinstance(payload, dict):
        return None
    raw_payload = cast("dict[object, object]", payload)
    normalized: dict[str, object] = {}
    for raw_item in raw_payload.items():
        key_obj = raw_item[0]
        value_obj = raw_item[1]
        if isinstance(key_obj, str):
            normalized[key_obj] = value_obj
    return normalized


def _float_field(payload: dict[str, object], key: str, default: float) -> float:
    """Decode one float-like field from a JSON payload map."""
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        return float(int(raw))
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return default
    return default


def _int_field(payload: dict[str, object], key: str, default: int) -> int:
    """Decode one int-like field from a JSON payload map."""
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return default
    return default


def _str_field(payload: dict[str, object], key: str, default: str) -> str:
    """Decode one string-like field from a JSON payload map."""
    raw = payload.get(key, default)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int | float | bool):
        return str(raw)
    return default


def _bool_field(payload: dict[str, object], key: str, default: bool) -> bool:
    """Decode one boolean-like field from a JSON payload map."""
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float):
        return bool(raw)
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _int_list(value: object) -> list[int] | None:
    """Decode one integer list from a JSON payload field."""
    if not isinstance(value, list):
        return None
    raw_values = cast("list[object]", value)
    normalized: list[int] = []
    for item in raw_values:
        if isinstance(item, bool):
            normalized.append(int(item))
        elif isinstance(item, int):
            normalized.append(item)
        elif isinstance(item, float):
            normalized.append(int(item))
        elif isinstance(item, str):
            try:
                normalized.append(int(item))
            except ValueError:
                return None
        else:
            return None
    return normalized


def _output_to_payload(output: AnalyzeOutput) -> dict[str, object]:
    """Encode one analyze result for child-process transport."""
    return {
        "status": "success",
        "meta": asdict(output.meta),
        "hashes": list(output.hashes),
        "probe_s": float(output.probe_s),
        "fingerprint_s": float(output.fingerprint_s),
        "probe_fallback_backend": output.probe_fallback_backend,
        "fingerprint_fallback_decoder": output.fingerprint_fallback_decoder,
    }


def _error_payload(stage: str, exc: Exception) -> dict[str, object]:
    """Encode one structured child error payload."""
    return {
        "status": "error",
        "stage": str(stage),
        "error_type": exc.__class__.__name__,
        "message": str(exc),
    }


def _response_from_payload(
    payload: object,
    *,
    stderr_text: str,
    exit_code: int,
) -> AnalyzeProcessResult:
    """Normalize one child JSON response into a parent-side result."""
    payload_map = _payload_map(payload)
    if payload_map is None:
        return AnalyzeProcessResult(
            kind="crash",
            issue_stage="analyze_crash",
            issue_message=_crash_message(exit_code, stderr_text),
        )
    status = str(payload_map.get("status", "")).strip().lower()
    if status == "success":
        meta = _meta_from_payload(payload_map.get("meta"))
        hashes = _int_list(payload_map.get("hashes"))
        if meta is None or hashes is None:
            return AnalyzeProcessResult(
                kind="crash",
                issue_stage="analyze_crash",
                issue_message=_crash_message(exit_code, stderr_text),
            )
        return AnalyzeProcessResult(
            kind="success",
            output=AnalyzeOutput(
                meta=meta,
                hashes=hashes,
                probe_s=_float_field(payload_map, "probe_s", 0.0),
                fingerprint_s=_float_field(payload_map, "fingerprint_s", 0.0),
                probe_fallback_backend=_normalize_probe_backend(
                    payload_map.get("probe_fallback_backend")
                ),
                fingerprint_fallback_decoder=_normalize_decoder_backend(
                    payload_map.get("fingerprint_fallback_decoder")
                ),
            ),
        )
    if status == "error":
        return AnalyzeProcessResult(
            kind="error",
            issue_stage=str(payload_map.get("stage", "analyze") or "analyze"),
            issue_message=str(
                payload_map.get("message", "") or "Analyze child failed."
            ),
        )
    return AnalyzeProcessResult(
        kind="crash",
        issue_stage="analyze_crash",
        issue_message=_crash_message(exit_code, stderr_text),
    )


def _normalize_probe_backend(value: object) -> ProbeBackendId | None:
    """Normalize one optional probe backend identifier."""
    if str(value).strip().lower() == "ffprobe":
        return "ffprobe"
    if str(value).strip().lower() == "pyav":
        return "pyav"
    return None


def _normalize_decoder_backend(value: object) -> FrameDecodeBackendId | None:
    """Normalize one optional frame-decoder identifier."""
    text = str(value).strip().lower()
    if text == "opencv":
        return "opencv"
    if text == "pyav":
        return "pyav"
    if text == "ffmpeg":
        return "ffmpeg"
    return None


def _stderr_tail(stderr_text: str, *, limit: int = 400) -> str:
    """Return a compact stderr tail for crash diagnostics."""
    text = stderr_text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _crash_message(exit_code: int, stderr_text: str) -> str:
    """Build a user-facing crash message for one child-process failure."""
    stderr_tail = _stderr_tail(stderr_text)
    if stderr_tail:
        return (
            f"Analyze child crashed with exit code {int(exit_code)}. "
            f"stderr tail: {stderr_tail}"
        )
    return f"Analyze child crashed with exit code {int(exit_code)}."


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Forcefully terminate one analyze child and its descendants."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        command = ["taskkill", "/PID", str(process.pid), "/T", "/F"]
        kwargs = merge_subprocess_kwargs(
            {
                "capture_output": True,
                "text": True,
                "check": False,
            },
            windows_no_window_run_kwargs(),
        )
        try:
            subprocess.run(command, **kwargs)
        except Exception:
            process.kill()
    else:
        process.kill()


class SubprocessAnalyzeHandle:
    """Concrete handle that supervises one analyze child subprocess."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        temp_dir: str,
        stop_path: Path,
    ) -> None:
        self._process = process
        self._temp_dir = temp_dir
        self._stop_path = stop_path
        self._collected_result: AnalyzeProcessResult | None = None

    def is_running(self) -> bool:
        """Return whether the child subprocess is still running."""
        return self._process.poll() is None

    def request_stop(self) -> None:
        """Ask the child to stop cooperatively by touching the stop marker."""
        self._stop_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop_path.touch(exist_ok=True)

    def kill(self) -> None:
        """Forcefully terminate the analyze child process tree."""
        _kill_process_tree(self._process)
        with contextlib.suppress(Exception):
            self._process.communicate(timeout=0.1)
        self._cleanup()

    def collect_result(self) -> AnalyzeProcessResult | None:
        """Return the child exit payload once the process has finished."""
        if self._collected_result is not None:
            return self._collected_result
        if self._process.poll() is None:
            return None
        try:
            stdout_text, stderr_text = self._process.communicate(timeout=0.1)
        except subprocess.TimeoutExpired:
            return None
        try:
            payload = json.loads(stdout_text) if stdout_text.strip() else None
        except json.JSONDecodeError:
            payload = None
        self._collected_result = _response_from_payload(
            payload,
            stderr_text=stderr_text,
            exit_code=int(self._process.returncode or 0),
        )
        self._cleanup()
        return self._collected_result

    def _cleanup(self) -> None:
        """Delete the temporary control directory for this child."""
        shutil.rmtree(self._temp_dir, ignore_errors=True)


class SubprocessAnalyzeLauncher:
    """Launch analyze work in one hidden Python child process per file."""

    def __init__(self, probe_backend: ProbeBackendId) -> None:
        self._probe_backend: ProbeBackendId = (
            "ffprobe" if probe_backend == "ffprobe" else "pyav"
        )

    def launch(self, path: str, cached_meta: VideoMeta | None) -> AnalyzeTaskHandle:
        """Start one analyze child process and return its handle."""
        temp_dir = tempfile.mkdtemp(prefix="video-duperz-analyze-")
        stop_path = Path(temp_dir) / "stop.flag"
        payload = json.dumps(
            {
                "path": path,
                "cached_meta": asdict(cached_meta) if cached_meta is not None else None,
                "probe_backend": self._probe_backend,
                "stop_path": str(stop_path),
            }
        )
        command = [sys.executable, "-m", "video_duperz", "analyze-child"]
        kwargs = merge_subprocess_kwargs(
            {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            },
            windows_no_window_popen_kwargs(),
        )
        process = subprocess.Popen(command, **kwargs)
        if process.stdin is None:
            raise RuntimeError("Analyze child stdin pipe is unavailable.")
        process.stdin.write(payload)
        process.stdin.close()
        return SubprocessAnalyzeHandle(process, temp_dir, stop_path)


def run_analyze_child_from_stdio() -> int:
    """Execute one analyze child request read from standard input."""
    raw = sys.stdin.read()
    payload = json.loads(raw)
    payload_map = _payload_map(payload)
    if payload_map is None:
        raise ValueError("Analyze child request payload must be a JSON object.")
    path = str(payload_map.get("path", ""))
    if not path:
        raise ValueError("Analyze child request is missing 'path'.")
    probe_backend = _normalize_probe_backend(payload_map.get("probe_backend")) or "pyav"
    stop_path = Path(str(payload_map.get("stop_path", "") or "")).expanduser()
    cached_meta = _meta_from_payload(payload_map.get("cached_meta"))

    def _stop_requested() -> bool:
        return stop_path.exists()

    try:
        output = analyze_video_file(
            path,
            cached_meta,
            probe_backend=probe_backend,
            stop_requested=_stop_requested,
        )
    except ProbeError as exc:
        sys.stdout.write(json.dumps(_error_payload("probe", exc)))
        return 0
    except FingerprintError as exc:
        sys.stdout.write(json.dumps(_error_payload("fingerprint", exc)))
        return 0
    except _AnalyzeStopRequestedError as exc:
        sys.stdout.write(json.dumps(_error_payload("analyze", exc)))
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(_output_to_payload(output)))
    return 0
