"""Video fingerprint generation utilities built on sampled frame hashes."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import traceback
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import PurePath
from statistics import median
from typing import Literal, Protocol, cast

import numpy as np
from threep_commons.subprocess_helpers import (
    merge_subprocess_kwargs,
    windows_no_window_popen_kwargs,
    windows_no_window_run_kwargs,
)

from .executable_paths import resolve_executable_path
from .models import FingerprintRecord, FrameDecodeBackendId, utc_now_iso

try:
    import cv2  # type: ignore
except ImportError:
    cv2 = None  # type: ignore[assignment]

ALGO_VERSION = 1
FINGERPRINT_DECODER_TIMEOUT_S = 15.0
SAMPLE_PERCENTS = [
    0.05,
    0.13,
    0.21,
    0.29,
    0.37,
    0.45,
    0.53,
    0.61,
    0.69,
    0.77,
    0.85,
    0.93,
]
_FFMPEG_GRAY_WIDTH = 32
_FFMPEG_GRAY_HEIGHT = 32
_FFMPEG_GRAY_BYTES = _FFMPEG_GRAY_WIDTH * _FFMPEG_GRAY_HEIGHT
_RISKY_FINGERPRINT_SUFFIXES = frozenset({".wmv", ".asf"})


class FingerprintError(RuntimeError):
    """Raised when fingerprint generation cannot complete for a video."""


class _AvVideoFrameLike(Protocol):
    """Protocol describing the PyAV frame APIs used by fingerprint fallbacks."""

    pts: object
    best_effort_timestamp: object
    time_base: object

    def to_ndarray(self, **kwargs: object) -> np.ndarray:
        """Convert one decoded frame to a NumPy array."""
        ...


class _AvStreamLike(Protocol):
    """Protocol describing the PyAV stream APIs used by fingerprint fallbacks."""

    type: str
    time_base: object


class _AvContainerLike(Protocol):
    """Protocol describing the PyAV container APIs used by fingerprint fallbacks."""

    streams: Iterable[_AvStreamLike]

    def __enter__(self) -> _AvContainerLike:
        """Enter the PyAV container context manager."""
        ...

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> object:
        """Exit the PyAV container context manager."""
        ...

    def decode(self, stream: _AvStreamLike) -> Iterable[_AvVideoFrameLike]:
        """Decode frames from one stream."""
        ...

    def seek(
        self,
        offset: int,
        *,
        _backward: bool = False,
        _any_frame: bool = False,
        stream: _AvStreamLike | None = None,
    ) -> object:
        """Seek within the container."""
        ...


class _AvModuleLike(Protocol):
    """Protocol describing the subset of PyAV used by fingerprint fallbacks."""

    def open(
        self,
        path: str,
        options: dict[str, str] | None = None,
    ) -> _AvContainerLike:
        """Open one media container."""
        del path, options
        raise NotImplementedError


DecoderAttemptStatus = Literal["success", "error", "timeout"]
_DecoderAttemptRunner = Callable[
    [str, float, FrameDecodeBackendId, float],
    "_DecoderAttemptResult",
]


@dataclass(slots=True)
class FingerprintDecoderAttempt:
    """One decoder attempt outcome captured during fingerprint construction."""

    decoder_backend: FrameDecodeBackendId
    status: DecoderAttemptStatus
    message: str = ""


@dataclass(slots=True)
class FingerprintProvenance:
    """Persisted quiet provenance for the decoder path used by one file."""

    decoder_backend: FrameDecodeBackendId
    attempts: list[FingerprintDecoderAttempt]
    risky_format_bypass: bool = False

    def to_json(self) -> str:
        """Serialize the decoder provenance payload for the database."""
        payload = {
            "decoder_backend": self.decoder_backend,
            "risky_format_bypass": self.risky_format_bypass,
            "attempts": [
                {
                    "decoder_backend": attempt.decoder_backend,
                    "status": attempt.status,
                    "message": attempt.message,
                }
                for attempt in self.attempts
            ],
        }
        return json.dumps(payload, sort_keys=True)


@dataclass(slots=True)
class FingerprintBuildResult:
    """Fingerprint payload plus decoder provenance for one analyzed file."""

    record: FingerprintRecord
    decoder_backend: FrameDecodeBackendId
    provenance_json: str
    fallback_decoder: FrameDecodeBackendId | None = None


@dataclass(slots=True)
class _DecoderAttemptResult:
    """Normalized parent-side outcome for one decoder child attempt."""

    status: DecoderAttemptStatus
    hashes: list[int] | None = None
    message: str = ""


def sample_timestamps(duration_s: float) -> list[float]:
    """Choose normalized timestamps used when sampling frames from a video."""
    if duration_s <= 0:
        return [0.0] * len(SAMPLE_PERCENTS)
    return [duration_s * percent for percent in SAMPLE_PERCENTS]


def _resize_nearest(gray: np.ndarray, width: int, height: int) -> np.ndarray:
    """Resize one grayscale frame without relying on OpenCV."""
    src_h, src_w = gray.shape[:2]
    if src_h == 0 or src_w == 0:
        return np.zeros((height, width), dtype=np.uint8)
    y_idx = cast("np.ndarray", np.linspace(0, src_h - 1, num=height)).astype(int)
    x_idx = cast("np.ndarray", np.linspace(0, src_w - 1, num=width)).astype(int)
    indexer = cast("tuple[np.ndarray, np.ndarray]", np.ix_(y_idx, x_idx))
    return gray[indexer]


def dhash_from_gray(gray_frame: np.ndarray) -> int:
    """Compute a perceptual dHash value from a grayscale frame."""
    if cv2 is not None:
        gray32 = cv2.resize(gray_frame, (32, 32), interpolation=cv2.INTER_AREA)
        small = cv2.resize(gray32, (9, 8), interpolation=cv2.INTER_AREA)
    else:
        gray32 = _resize_nearest(gray_frame, 32, 32)
        small = _resize_nearest(gray32, 9, 8)
    diff = small[:, 1:] > small[:, :-1]
    value = 0
    for bit in diff.flatten():
        value = (value << 1) | int(bool(bit))
    return value


def hamming_distance(a: int, b: int) -> int:
    """Return the bit distance between two dHash values."""
    return (a ^ b).bit_count()


def normalized_median_distance(hashes_a: list[int], hashes_b: list[int]) -> float:
    """Compare two hash sequences using the normalized median Hamming distance."""
    if len(hashes_a) != len(hashes_b) or not hashes_a:
        return 1.0
    distances = [
        hamming_distance(a, b) for a, b in zip(hashes_a, hashes_b, strict=True)
    ]
    return float(median(distances)) / 64.0


def ensure_ffmpeg_available(ffmpeg_exe_path: str = "") -> str:
    """Return the ffmpeg executable path or raise when it is unavailable."""
    try:
        return resolve_executable_path(
            "ffmpeg",
            ffmpeg_exe_path,
            not_found_message=(
                "ffmpeg not found on PATH. Install ffmpeg and add it to PATH."
            ),
        )
    except FileNotFoundError as exc:
        raise FingerprintError(str(exc)) from exc


def ensure_fingerprint_fallback_chain_available(ffmpeg_exe_path: str = "") -> None:
    """Raise when the guarded decoder fallback chain is not fully available."""
    _import_av()
    ensure_ffmpeg_available(ffmpeg_exe_path)


def _relaxed_media_options() -> dict[str, str]:
    """Return tolerant libav options used by fallback decoders."""
    return {
        "analyzeduration": "200M",
        "probesize": "200M",
        "fflags": "+discardcorrupt+genpts",
        "err_detect": "ignore_err",
    }


def _import_av() -> _AvModuleLike:
    """Import PyAV and normalize the error to `FingerprintError`."""
    try:
        import av

        return cast("_AvModuleLike", av)
    except ModuleNotFoundError as exc:
        raise FingerprintError(
            "PyAV decoder is unavailable because the 'av' package is not installed."
        ) from exc


def _ratio_to_float(value: object) -> float:
    """Normalize rationals and numeric-like values to float."""
    if value is None:
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        try:
            return float(Fraction(str(value)))
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0


def _frame_time_s(frame: _AvVideoFrameLike, stream: _AvStreamLike) -> float:
    """Return the decoded frame timestamp in seconds."""
    pts = getattr(frame, "pts", None)
    if pts is None:
        pts = getattr(frame, "best_effort_timestamp", None)
    if pts is None:
        return 0.0
    scale = _ratio_to_float(getattr(frame, "time_base", None))
    if scale <= 0.0:
        scale = _ratio_to_float(getattr(stream, "time_base", None))
    if scale <= 0.0:
        return 0.0
    return max(0.0, float(pts) * scale)


def _open_av_container(
    av_module: _AvModuleLike,
    path: str,
    *,
    relaxed: bool,
) -> _AvContainerLike:
    """Open one PyAV container, retrying without options if needed."""
    if not relaxed:
        return av_module.open(path)
    try:
        return av_module.open(path, options=_relaxed_media_options())
    except TypeError:
        return av_module.open(path)


def _hash_gray_frames(path: str, gray_frames: list[np.ndarray | None]) -> list[int]:
    """Convert sampled grayscale frames into dHash values."""
    hashes: list[int] = []
    for gray_frame in gray_frames:
        if gray_frame is None or gray_frame.size <= 0:
            hashes.append(0)
            continue
        hashes.append(dhash_from_gray(gray_frame))
    if not any(hashes):
        raise FingerprintError(f"Could not decode sample frames for {path}")
    return hashes


def _opencv_gray_samples(path: str, duration_s: float) -> list[np.ndarray | None]:
    """Decode sampled grayscale frames through OpenCV."""
    if cv2 is None:
        raise FingerprintError("opencv-python is not installed")
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FingerprintError(f"Unable to open video: {path}")

    samples: list[np.ndarray | None] = []
    try:
        for timestamp_s in sample_timestamps(duration_s):
            cap.set(subprocess_cv_pos_msec(), max(0.0, timestamp_s * 1000.0))
            ok, frame = cap.read()
            if not ok:
                samples.append(None)
                continue
            samples.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    finally:
        cap.release()
    return samples


def subprocess_cv_pos_msec() -> int:
    """Return the OpenCV position constant without confusing static analyzers."""
    if cv2 is None:
        raise FingerprintError("opencv-python is not installed")
    return int(cv2.CAP_PROP_POS_MSEC)


def _pyav_gray_samples(path: str, duration_s: float) -> list[np.ndarray | None]:
    """Decode sampled grayscale frames through PyAV."""
    av_module = _import_av()
    try:
        container = _open_av_container(av_module, path, relaxed=True)
    except Exception as exc:
        raise FingerprintError(f"PyAV failed to open {path}: {exc}") from exc

    targets = sample_timestamps(duration_s)
    samples: list[np.ndarray | None] = [None] * len(targets)
    try:
        with container:
            streams = [stream for stream in container.streams if stream.type == "video"]
            if not streams:
                raise FingerprintError(f"PyAV found no video stream for {path}")
            video_stream = streams[0]
            target_idx = 0
            last_gray: np.ndarray | None = None
            for frame in container.decode(video_stream):
                frame_time_s = _frame_time_s(frame, video_stream)
                gray = frame.to_ndarray(format="gray")
                last_gray = gray
                while target_idx < len(targets) and frame_time_s >= max(
                    0.0, targets[target_idx]
                ):
                    samples[target_idx] = gray
                    target_idx += 1
                if target_idx >= len(targets):
                    break
            while target_idx < len(targets):
                samples[target_idx] = last_gray
                target_idx += 1
    except FingerprintError:
        raise
    except Exception as exc:
        raise FingerprintError(f"PyAV frame decode failed for {path}: {exc}") from exc
    return samples


def _ffmpeg_gray_frame(
    ffmpeg_path: str,
    path: str,
    timestamp_s: float,
) -> np.ndarray | None:
    """Extract one grayscale sample frame through ffmpeg."""
    hidden_kwargs = windows_no_window_run_kwargs()
    command = [
        ffmpeg_path,
        "-v",
        "error",
        "-analyzeduration",
        "200M",
        "-probesize",
        "200M",
        "-fflags",
        "+discardcorrupt+genpts",
        "-err_detect",
        "ignore_err",
        "-ss",
        f"{max(0.0, timestamp_s):.6f}",
        "-i",
        path,
        "-frames:v",
        "1",
        "-vf",
        "scale=32:32:flags=area,format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    kwargs = merge_subprocess_kwargs(
        {
            "capture_output": True,
            "check": True,
        },
        hidden_kwargs,
    )
    try:
        proc = cast(
            "subprocess.CompletedProcess[bytes]",
            subprocess.run(command, **kwargs),
        )
    except subprocess.CalledProcessError:
        return None
    stdout = proc.stdout
    if len(stdout) < _FFMPEG_GRAY_BYTES:
        return None
    return np.frombuffer(stdout[:_FFMPEG_GRAY_BYTES], dtype=np.uint8).reshape(
        (_FFMPEG_GRAY_HEIGHT, _FFMPEG_GRAY_WIDTH)
    )


def _ffmpeg_gray_samples(
    path: str,
    duration_s: float,
    *,
    ffmpeg_exe_path: str = "",
) -> list[np.ndarray | None]:
    """Decode sampled grayscale frames through ffmpeg."""
    ffmpeg_path = ensure_ffmpeg_available(ffmpeg_exe_path)
    return [
        _ffmpeg_gray_frame(ffmpeg_path, path, timestamp_s)
        for timestamp_s in sample_timestamps(duration_s)
    ]


def _compute_hashes_for_decoder(
    path: str,
    duration_s: float,
    decoder_backend: FrameDecodeBackendId,
    *,
    ffmpeg_exe_path: str = "",
) -> list[int]:
    """Compute hashes through one concrete decoder backend."""
    if decoder_backend == "opencv":
        return _hash_gray_frames(path, _opencv_gray_samples(path, duration_s))
    if decoder_backend == "pyav":
        return _hash_gray_frames(path, _pyav_gray_samples(path, duration_s))
    return _hash_gray_frames(
        path,
        _ffmpeg_gray_samples(path, duration_s, ffmpeg_exe_path=ffmpeg_exe_path),
    )


def compute_video_hashes(path: str, duration_s: float) -> list[int]:
    """Extract sampled frames through the legacy OpenCV path and hash them."""
    return _compute_hashes_for_decoder(path, duration_s, "opencv")


def _is_risky_fingerprint_format(path: str) -> bool:
    """Return whether the file should bypass OpenCV for fingerprinting."""
    return PurePath(path).suffix.lower() in _RISKY_FINGERPRINT_SUFFIXES


def _decoder_sequence_for_path(path: str) -> list[FrameDecodeBackendId]:
    """Return the ordered decoder chain for one file path."""
    if _is_risky_fingerprint_format(path):
        return ["ffmpeg", "pyav"]
    return ["opencv", "pyav", "ffmpeg"]


def _decode_backend(value: object) -> FrameDecodeBackendId | None:
    """Normalize one decoder backend identifier from a JSON payload."""
    text = str(value).strip().lower()
    if text == "opencv":
        return "opencv"
    if text == "pyav":
        return "pyav"
    if text == "ffmpeg":
        return "ffmpeg"
    return None


def _parse_int_list(value: object) -> list[int] | None:
    """Decode one integer list from a JSON-like payload."""
    if not isinstance(value, list):
        return None
    raw_values = cast("list[object]", value)
    parsed: list[int] = []
    for item in raw_values:
        if isinstance(item, bool):
            parsed.append(int(item))
        elif isinstance(item, int):
            parsed.append(item)
        elif isinstance(item, float):
            parsed.append(int(item))
        elif isinstance(item, str):
            try:
                parsed.append(int(item))
            except ValueError:
                return None
        else:
            return None
    return parsed


def _stderr_tail(stderr_text: str, *, limit: int = 400) -> str:
    """Return a compact stderr tail for decoder child diagnostics."""
    text = stderr_text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _crash_message(
    decoder_backend: FrameDecodeBackendId,
    exit_code: int,
    stderr_text: str,
) -> str:
    """Build a user-facing crash message for one decoder child failure."""
    stderr_tail = _stderr_tail(stderr_text)
    prefix = (
        f"Fingerprint decoder '{decoder_backend}' crashed with exit code "
        f"{int(exit_code)}."
    )
    if not stderr_tail:
        return prefix
    return f"{prefix} stderr tail: {stderr_tail}"


def _success_payload(hashes: list[int]) -> dict[str, object]:
    """Encode a successful decoder child result."""
    return {
        "status": "success",
        "hashes": list(hashes),
    }


def _error_payload(
    decoder_backend: FrameDecodeBackendId,
    exc: Exception,
) -> dict[str, object]:
    """Encode one structured decoder child error payload."""
    return {
        "status": "error",
        "decoder_backend": decoder_backend,
        "message": str(exc),
    }


def _payload_map(payload: object) -> dict[str, object] | None:
    """Normalize one JSON-like mapping payload to string keys."""
    if not isinstance(payload, dict):
        return None
    raw_payload = cast("dict[object, object]", payload)
    normalized: dict[str, object] = {}
    for key_obj, value_obj in raw_payload.items():
        if isinstance(key_obj, str):
            normalized[key_obj] = value_obj
    return normalized


def _response_from_payload(
    payload: object,
    *,
    decoder_backend: FrameDecodeBackendId,
    stderr_text: str,
    exit_code: int,
) -> _DecoderAttemptResult:
    """Normalize one child JSON response into a parent-side attempt result."""
    payload_map = _payload_map(payload)
    if payload_map is None:
        return _DecoderAttemptResult(
            status="error",
            message=_crash_message(decoder_backend, exit_code, stderr_text),
        )
    status = str(payload_map.get("status", "")).strip().lower()
    if status == "success":
        hashes = _parse_int_list(payload_map.get("hashes"))
        if hashes is None:
            return _DecoderAttemptResult(
                status="error",
                message=_crash_message(decoder_backend, exit_code, stderr_text),
            )
        return _DecoderAttemptResult(status="success", hashes=hashes)
    if status == "error":
        return _DecoderAttemptResult(
            status="error",
            message=str(payload_map.get("message", "") or "Decoder attempt failed."),
        )
    return _DecoderAttemptResult(
        status="error",
        message=_crash_message(decoder_backend, exit_code, stderr_text),
    )


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Forcefully terminate one decoder child and its descendants."""
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
            return
        except Exception:
            process.kill()
            return
    process.kill()


def _run_decoder_attempt_subprocess(
    path: str,
    duration_s: float,
    decoder_backend: FrameDecodeBackendId,
    timeout_s: float,
    *,
    ffmpeg_exe_path: str = "",
) -> _DecoderAttemptResult:
    """Run one decoder attempt in a killable child process."""
    payload = json.dumps(
        {
            "path": path,
            "duration_s": duration_s,
            "decoder_backend": decoder_backend,
            "ffmpeg_exe_path": ffmpeg_exe_path,
        }
    )
    command = [sys.executable, "-m", "video_duperz", "fingerprint-child"]
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
    try:
        stdout_text, stderr_text = process.communicate(
            input=payload,
            timeout=max(0.1, float(timeout_s)),
        )
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        with contextlib.suppress(Exception):
            process.communicate(timeout=0.1)
        return _DecoderAttemptResult(
            status="timeout",
            message=(
                f"Decoder '{decoder_backend}' timed out after {float(timeout_s):.1f}s."
            ),
        )

    try:
        raw_payload: object = json.loads(stdout_text) if stdout_text.strip() else None
    except json.JSONDecodeError:
        raw_payload = None
    return _response_from_payload(
        raw_payload,
        decoder_backend=decoder_backend,
        stderr_text=stderr_text,
        exit_code=int(process.returncode or 0),
    )


def build_fingerprint_record_with_fallback(
    file_id: int,
    duration_s: float,
    path: str,
    *,
    attempt_runner: _DecoderAttemptRunner | None = None,
    timeout_s: float = FINGERPRINT_DECODER_TIMEOUT_S,
    ffmpeg_exe_path: str = "",
) -> FingerprintBuildResult:
    """Build a fingerprint record using guarded decoder fallbacks."""

    def _default_attempt_runner(
        attempt_path: str,
        attempt_duration_s: float,
        decoder_backend: FrameDecodeBackendId,
        attempt_timeout_s: float,
    ) -> _DecoderAttemptResult:
        """Execute one default decoder child attempt with the active override."""
        return _run_decoder_attempt_subprocess(
            attempt_path,
            attempt_duration_s,
            decoder_backend,
            attempt_timeout_s,
            ffmpeg_exe_path=ffmpeg_exe_path,
        )

    attempts: list[FingerprintDecoderAttempt] = []
    decoder_sequence = _decoder_sequence_for_path(path)
    risky_format_bypass = decoder_sequence[0] != "opencv"
    active_attempt_runner = attempt_runner or _default_attempt_runner
    for index, decoder_backend in enumerate(decoder_sequence):
        attempt = active_attempt_runner(path, duration_s, decoder_backend, timeout_s)
        attempts.append(
            FingerprintDecoderAttempt(
                decoder_backend=decoder_backend,
                status=attempt.status,
                message=attempt.message,
            )
        )
        if attempt.status != "success" or attempt.hashes is None:
            continue
        record = FingerprintRecord(
            file_id=file_id,
            algo_version=ALGO_VERSION,
            frame_count=len(attempt.hashes),
            hashes=attempt.hashes,
            created_at=utc_now_iso(),
        )
        provenance = FingerprintProvenance(
            decoder_backend=decoder_backend,
            attempts=attempts,
            risky_format_bypass=risky_format_bypass,
        )
        fallback_decoder = decoder_backend if index > 0 else None
        return FingerprintBuildResult(
            record=record,
            decoder_backend=decoder_backend,
            provenance_json=provenance.to_json(),
            fallback_decoder=fallback_decoder,
        )

    failure_messages = [attempt.message for attempt in attempts if attempt.message]
    failure_summary = " | ".join(failure_messages)
    raise FingerprintError(
        failure_summary or f"Could not decode sample frames for {path}"
    )


def build_fingerprint_record(
    file_id: int,
    duration_s: float,
    path: str,
    *,
    ffmpeg_exe_path: str = "",
) -> FingerprintRecord:
    """Build the persisted fingerprint payload for a scanned video file."""
    return build_fingerprint_record_with_fallback(
        file_id=file_id,
        duration_s=duration_s,
        path=path,
        ffmpeg_exe_path=ffmpeg_exe_path,
    ).record


def run_fingerprint_child_from_stdio() -> int:
    """Execute one decoder child request read from standard input."""
    raw = sys.stdin.read()
    payload = json.loads(raw)
    payload_map = _payload_map(payload)
    if payload_map is None:
        raise ValueError("Fingerprint child request payload must be a JSON object.")
    path = str(payload_map.get("path", ""))
    if not path:
        raise ValueError("Fingerprint child request is missing 'path'.")
    decoder_backend = _decode_backend(payload_map.get("decoder_backend"))
    if decoder_backend is None:
        raise ValueError("Fingerprint child request is missing 'decoder_backend'.")
    ffmpeg_exe_path = str(payload_map.get("ffmpeg_exe_path", "") or "")
    duration_raw = payload_map.get("duration_s", 0.0)
    duration_s = float(duration_raw) if isinstance(duration_raw, int | float) else 0.0
    try:
        hashes = _compute_hashes_for_decoder(
            path,
            duration_s,
            decoder_backend,
            ffmpeg_exe_path=ffmpeg_exe_path,
        )
    except FingerprintError as exc:
        sys.stdout.write(json.dumps(_error_payload(decoder_backend, exc)))
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(_success_payload(hashes)))
    return 0


@contextlib.contextmanager
def fingerprint_child_stdio(
    raw_payload: str,
) -> Iterator[tuple[io.StringIO, io.StringIO]]:
    """Temporarily replace stdio streams while exercising the child entrypoint."""
    stdin_io = io.StringIO(raw_payload)
    stdout_io = io.StringIO()
    stderr_io = io.StringIO()
    original_streams = (sys.stdin, sys.stdout, sys.stderr)
    sys.stdin = stdin_io
    sys.stdout = stdout_io
    sys.stderr = stderr_io
    try:
        yield stdout_io, stderr_io
    finally:
        sys.stdin, sys.stdout, sys.stderr = original_streams
