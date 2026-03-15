"""Video fingerprint generation utilities built on sampled frame hashes."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from statistics import median
from typing import TYPE_CHECKING, Protocol, cast

import numpy as np
from threep_commons.subprocess_helpers import windows_no_window_run_kwargs

from .models import FingerprintRecord, FrameDecodeBackendId, utc_now_iso

if TYPE_CHECKING:
    from collections.abc import Iterable

try:
    import cv2  # type: ignore
except ImportError:
    cv2 = None  # type: ignore[assignment]

ALGO_VERSION = 1
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


class FingerprintError(RuntimeError):
    """Raised when fingerprint generation cannot complete for a video."""

    pass


class _AvVideoFrameLike(Protocol):
    """Protocol describing the PyAV frame APIs used by fingerprint fallbacks."""

    pts: object
    best_effort_timestamp: object
    time_base: object

    def to_ndarray(self, **kwargs: object) -> np.ndarray: ...


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

        ...


@dataclass(slots=True)
class FingerprintBuildResult:
    """Fingerprint payload plus fallback provenance for one analyzed file."""

    record: FingerprintRecord
    fallback_decoder: FrameDecodeBackendId | None = None


def sample_timestamps(duration_s: float) -> list[float]:
    """Choose normalized timestamps used when sampling frames from a video."""
    if duration_s <= 0:
        return [0.0] * len(SAMPLE_PERCENTS)
    return [duration_s * p for p in SAMPLE_PERCENTS]


def _resize_nearest(gray: np.ndarray, width: int, height: int) -> np.ndarray:
    """Resize one grayscale frame without OpenCV."""
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


def ensure_ffmpeg_available() -> str:
    """Return the ffmpeg executable path or raise when it is unavailable."""
    path = shutil.which("ffmpeg")
    if not path:
        raise FingerprintError(
            "ffmpeg not found on PATH. Install ffmpeg and add it to PATH."
        )
    return path


def ensure_fingerprint_fallback_chain_available() -> None:
    """Raise when the non-OpenCV fallback frame decoders are unavailable."""
    _import_av()
    ensure_ffmpeg_available()


def _relaxed_media_options() -> dict[str, str]:
    """Return tolerant FFmpeg/libav options used on fallback decoding."""
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
            "PyAV fallback decoder is unavailable because the 'av' package "
            "is not installed."
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


def _hash_gray_frames(
    path: str,
    gray_frames: list[np.ndarray | None],
) -> list[int]:
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
        for ts in sample_timestamps(duration_s):
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, ts * 1000.0))
            ok, frame = cap.read()
            if not ok:
                samples.append(None)
                continue
            samples.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    finally:
        cap.release()
    return samples


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
    try:
        creationflags_raw = hidden_kwargs.get("creationflags", 0)
        creationflags = (
            int(creationflags_raw)
            if isinstance(creationflags_raw, int | float | str)
            else 0
        )
        startupinfo_raw = hidden_kwargs.get("startupinfo")
        startupinfo = (
            startupinfo_raw
            if isinstance(startupinfo_raw, subprocess.STARTUPINFO)
            else None
        )
        if startupinfo is not None:
            proc = subprocess.run(
                command,
                capture_output=True,
                check=True,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        elif creationflags:
            proc = subprocess.run(
                command,
                capture_output=True,
                check=True,
                creationflags=creationflags,
            )
        else:
            proc = subprocess.run(
                command,
                capture_output=True,
                check=True,
            )
    except subprocess.CalledProcessError:
        return None
    stdout = proc.stdout
    if len(stdout) < _FFMPEG_GRAY_BYTES:
        return None
    return np.frombuffer(stdout[:_FFMPEG_GRAY_BYTES], dtype=np.uint8).reshape(
        (_FFMPEG_GRAY_HEIGHT, _FFMPEG_GRAY_WIDTH)
    )


def _ffmpeg_gray_samples(path: str, duration_s: float) -> list[np.ndarray | None]:
    """Decode sampled grayscale frames through ffmpeg."""
    ffmpeg_path = ensure_ffmpeg_available()
    return [
        _ffmpeg_gray_frame(ffmpeg_path, path, timestamp_s)
        for timestamp_s in sample_timestamps(duration_s)
    ]


def compute_video_hashes(path: str, duration_s: float) -> list[int]:
    """Extract sampled frames through the primary OpenCV path and hash them."""
    return _hash_gray_frames(path, _opencv_gray_samples(path, duration_s))


def build_fingerprint_record(
    file_id: int,
    duration_s: float,
    path: str,
) -> FingerprintRecord:
    """Build the persisted fingerprint payload for a scanned video file."""
    hashes = compute_video_hashes(path, duration_s)
    return FingerprintRecord(
        file_id=file_id,
        algo_version=ALGO_VERSION,
        frame_count=len(hashes),
        hashes=hashes,
        created_at=utc_now_iso(),
    )


def build_fingerprint_record_with_fallback(
    file_id: int,
    duration_s: float,
    path: str,
) -> FingerprintBuildResult:
    """Build a fingerprint record using ordered decoder fallbacks."""
    try:
        hashes = compute_video_hashes(path, duration_s)
        return FingerprintBuildResult(
            record=FingerprintRecord(
                file_id=file_id,
                algo_version=ALGO_VERSION,
                frame_count=len(hashes),
                hashes=hashes,
                created_at=utc_now_iso(),
            )
        )
    except FingerprintError:
        pass

    try:
        pyav_hashes = _hash_gray_frames(path, _pyav_gray_samples(path, duration_s))
        return FingerprintBuildResult(
            record=FingerprintRecord(
                file_id=file_id,
                algo_version=ALGO_VERSION,
                frame_count=len(pyav_hashes),
                hashes=pyav_hashes,
                created_at=utc_now_iso(),
            ),
            fallback_decoder="pyav",
        )
    except FingerprintError:
        ffmpeg_hashes = _hash_gray_frames(path, _ffmpeg_gray_samples(path, duration_s))
        return FingerprintBuildResult(
            record=FingerprintRecord(
                file_id=file_id,
                algo_version=ALGO_VERSION,
                frame_count=len(ffmpeg_hashes),
                hashes=ffmpeg_hashes,
                created_at=utc_now_iso(),
            ),
            fallback_decoder="ffmpeg",
        )
