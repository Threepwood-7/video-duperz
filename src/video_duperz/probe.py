"""Probe backends for extracting video metadata."""

from __future__ import annotations

import json
import re
import subprocess
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Protocol, cast

from threep_commons.subprocess_helpers import windows_no_window_popen_kwargs

from .executable_paths import resolve_executable_path
from .models import (
    ProbeBackendId,
    ScanProcessCpuPriority,
    ScanProcessIoMode,
    VideoMeta,
)
from .process_priority import (
    apply_scan_child_process_io_mode,
    apply_subprocess_cpu_priority_kwargs,
    normalize_scan_cpu_priority,
    normalize_scan_io_mode,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class ProbeError(RuntimeError):
    """Raised when a probe backend is unavailable or returns unusable metadata."""

    pass


class ProbeBackend(Protocol):
    """Interface implemented by metadata probe backends."""

    def ensure_available(self) -> object:
        """Return a backend-specific availability token or raise on failure."""

        ...

    def probe_video(self, path: str) -> VideoMeta:
        """Extract `VideoMeta` for one video path."""

        ...


class _AvStreamLike(Protocol):
    """Small protocol describing the PyAV stream surface used by the probe layer."""

    type: str


class _AvContainerLike(Protocol):
    """Protocol describing the PyAV container surface used during probing."""

    duration: object
    bit_rate: object
    streams: Iterable[_AvStreamLike]

    def __enter__(self) -> _AvContainerLike:
        """Enter the PyAV container context manager."""

        ...

    def __exit__(
        self,
        _exc_type: object,
        _exc: object,
        _tb: object,
    ) -> object:
        """Exit the PyAV container context manager."""

        ...


class _AvModuleLike(Protocol):
    """Protocol describing the subset of the `av` module we rely on."""

    time_base: object

    def open(self, path: str) -> _AvContainerLike:
        """Open one media container."""

        ...


def ensure_ffprobe_available(ffprobe_exe_path: str = "") -> str:
    """Return the ffprobe executable path or raise when it is unavailable."""
    try:
        return resolve_executable_path(
            "ffprobe",
            ffprobe_exe_path,
            not_found_message=(
                "ffprobe not found on PATH. Install ffmpeg and add it to PATH."
            ),
        )
    except FileNotFoundError as exc:
        raise ProbeError(str(exc)) from exc


def ensure_probe_backend_available(
    backend: ProbeBackendId = "pyav",
    *,
    ffprobe_exe_path: str = "",
) -> None:
    """Raise `ProbeError` when the selected probe backend is unavailable."""
    get_probe_backend(backend, ffprobe_exe_path=ffprobe_exe_path).ensure_available()


def get_probe_backend(
    backend: ProbeBackendId = "pyav",
    *,
    ffprobe_exe_path: str = "",
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
    scan_child_io_mode: ScanProcessIoMode = "normal",
) -> ProbeBackend:
    """Return the selected probe backend implementation."""
    if backend == "pyav":
        return _PYAV_BACKEND
    return _FfprobeBackend(
        ffprobe_exe_path,
        scan_child_cpu_priority=scan_child_cpu_priority,
        scan_child_io_mode=scan_child_io_mode,
    )


def probe_video(
    path: str,
    *,
    backend: ProbeBackendId = "pyav",
    ffprobe_exe_path: str = "",
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
    scan_child_io_mode: ScanProcessIoMode = "normal",
) -> VideoMeta:
    """Run the selected probe backend and normalize the result into `VideoMeta`."""
    return get_probe_backend(
        backend,
        ffprobe_exe_path=ffprobe_exe_path,
        scan_child_cpu_priority=scan_child_cpu_priority,
        scan_child_io_mode=scan_child_io_mode,
    ).probe_video(path)


def _parse_fps(rate: str) -> float:
    if not rate:
        return 0.0
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _positive_float(value: object) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return max(0.0, float(value))
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        return 0.0


def _positive_int(value: object) -> int:
    return int(_positive_float(value))


def _normalize_language(value: object) -> str:
    return str(value or "").strip().lower()


def _string_object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    raw_map = cast("dict[object, object]", value)
    return {
        str(key): raw
        for key, raw in raw_map.items()
        if isinstance(key, str | int | float | bool)
    }


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    raw_items = cast("list[object]", value)
    normalized: list[dict[str, object]] = []
    for item in raw_items:
        if isinstance(item, dict):
            normalized.append(_string_object_dict(cast("object", item)))
    return normalized


def _sorted_languages(streams: list[Any]) -> str:
    languages = {
        _normalize_language(getattr(stream, "metadata", {}).get("language"))
        for stream in streams
    }
    languages.discard("")
    return ",".join(sorted(languages))


def _codec_name(stream: Any) -> str:
    codec_context = getattr(stream, "codec_context", None)
    codec = getattr(codec_context, "codec", None)
    for candidate in (
        getattr(codec_context, "name", ""),
        getattr(codec, "name", ""),
        getattr(stream, "name", ""),
    ):
        text = str(candidate or "").strip().lower()
        if text:
            return text
    return ""


def _stream_bitrate(stream: Any) -> int:
    codec_context = getattr(stream, "codec_context", None)
    return max(
        _positive_int(getattr(stream, "bit_rate", 0)),
        _positive_int(getattr(codec_context, "bit_rate", 0)),
    )


def _ratio_to_float(value: object) -> float:
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


def _duration_seconds_from_container(
    container: _AvContainerLike, av_module: _AvModuleLike
) -> float:
    duration = getattr(container, "duration", None)
    if duration is None:
        return 0.0
    time_base = getattr(av_module, "time_base", None)
    scale = _ratio_to_float(time_base)
    if scale > 0.0:
        if scale <= 1.0:
            return max(0.0, float(duration) * scale)
        return max(0.0, float(duration) / scale)
    return max(0.0, float(duration) / 1_000_000.0)


def _duration_seconds_from_stream(stream: Any) -> float:
    duration = getattr(stream, "duration", None)
    if duration is None:
        return 0.0
    scale = _ratio_to_float(getattr(stream, "time_base", None))
    if scale <= 0.0:
        return 0.0
    return max(0.0, float(duration) * scale)


def _video_dimensions(stream: Any) -> tuple[int, int]:
    codec_context = getattr(stream, "codec_context", None)
    width = max(
        _positive_int(getattr(stream, "width", 0)),
        _positive_int(getattr(codec_context, "width", 0)),
    )
    height = max(
        _positive_int(getattr(stream, "height", 0)),
        _positive_int(getattr(codec_context, "height", 0)),
    )
    return width, height


def _video_fps(stream: Any) -> float:
    for candidate in (
        getattr(stream, "average_rate", None),
        getattr(stream, "guessed_rate", None),
        getattr(stream, "base_rate", None),
    ):
        fps = _ratio_to_float(candidate)
        if fps > 0.0:
            return fps
    return 0.0


def _pixel_format_bit_depth(pixel_format: str) -> int:
    """Infer one bit depth from a pixel-format name."""
    cleaned = str(pixel_format or "").strip().lower()
    if not cleaned:
        return 0
    for pattern in (
        r"p0?(10|12|14|16)(?:le|be)?$",
        r"(10|12|14|16)(?:le|be)?$",
    ):
        match = re.search(pattern, cleaned)
        if match is not None:
            return _positive_int(match.group(1))
    return 8 if cleaned else 0


def _normalize_bit_depth(value: object) -> int:
    """Clamp one loosely typed bit-depth value to a usable integer."""
    parsed = _positive_int(value)
    if parsed >= 16:
        return 16
    if parsed >= 14:
        return 14
    if parsed >= 12:
        return 12
    if parsed >= 10:
        return 10
    return 8


def _flatten_metadata_texts(value: object) -> list[str]:
    """Collect lower-cased metadata strings from nested probe payload values."""
    if value is None:
        return []
    if isinstance(value, bytes):
        try:
            return [value.decode("utf-8", errors="ignore").lower()]
        except Exception:
            return []
    if isinstance(value, str | int | float | bool):
        return [str(value).strip().lower()]
    if isinstance(value, dict):
        raw_map = cast("dict[object, object]", value)
        texts: list[str] = []
        for key, raw in raw_map.items():
            texts.extend(_flatten_metadata_texts(key))
            texts.extend(_flatten_metadata_texts(raw))
        return texts
    if isinstance(value, list | tuple):
        raw_items = cast("list[object] | tuple[object, ...]", value)
        texts = []
        for item in raw_items:
            texts.extend(_flatten_metadata_texts(item))
        return texts
    if isinstance(value, set):
        raw_items = cast("set[object]", value)
        texts = []
        for item in raw_items:
            texts.extend(_flatten_metadata_texts(item))
        return texts
    return [str(value).strip().lower()]


def _hdr_format_from_metadata(
    *,
    transfer: str,
    primaries: str,
    metadata_values: tuple[object, ...],
) -> str:
    """Derive one HDR format label from transfer, primaries, and side metadata."""
    haystacks: list[str] = []
    for value in metadata_values:
        haystacks.extend(_flatten_metadata_texts(value))
    joined = " ".join(text for text in haystacks if text)
    if (
        "dolby vision" in joined
        or "dovi" in joined
        or "dv_profile" in joined
        or "side_data_type dv" in joined
    ):
        return "DV"
    if "hdr10+" in joined or "dynamic hdr plus" in joined:
        return "HDR10+"
    normalized_transfer = str(transfer or "").strip().lower()
    normalized_primaries = str(primaries or "").strip().lower()
    if normalized_transfer == "smpte2084":
        return "HDR10"
    if normalized_transfer == "arib-std-b67":
        return "HLG"
    if normalized_transfer == "bt2020-10":
        return "HDR10"
    if normalized_primaries in {"bt2020", "bt2020nc", "bt2020c"} and (
        "mastering display metadata" in joined
        or "content light level metadata" in joined
    ):
        return "HDR10"
    return ""


def _ffprobe_bit_depth(video: dict[str, object]) -> int:
    """Return one normalized ffprobe bit depth for a video stream payload."""
    bits_per_raw_sample = _positive_int(video.get("bits_per_raw_sample"))
    if bits_per_raw_sample > 0:
        return _normalize_bit_depth(bits_per_raw_sample)
    return _normalize_bit_depth(
        _pixel_format_bit_depth(str(video.get("pix_fmt") or ""))
    )


def _pyav_bit_depth(stream: Any) -> int:
    """Return one normalized PyAV bit depth for a video stream."""
    codec_context = getattr(stream, "codec_context", None)
    for candidate in (
        getattr(stream, "bits_per_raw_sample", 0),
        getattr(stream, "bits_per_coded_sample", 0),
        getattr(codec_context, "bits_per_raw_sample", 0),
        getattr(codec_context, "bits_per_coded_sample", 0),
    ):
        parsed = _positive_int(candidate)
        if parsed > 0:
            return _normalize_bit_depth(parsed)
    for format_obj in (
        getattr(codec_context, "format", None),
        getattr(stream, "format", None),
    ):
        if format_obj is None:
            continue
        parsed_from_name = _pixel_format_bit_depth(str(getattr(format_obj, "name", "")))
        if parsed_from_name > 0:
            return _normalize_bit_depth(parsed_from_name)
        components = getattr(format_obj, "components", None)
        if isinstance(components, list | tuple):
            component_items = cast("list[object] | tuple[object, ...]", components)
            component_bits = [
                _positive_int(getattr(component, "bits", 0))
                for component in component_items
            ]
            if component_bits:
                return _normalize_bit_depth(max(component_bits))
    return 8


def _pyav_hdr_format(stream: Any) -> str:
    """Return one normalized HDR-format label for a PyAV stream."""
    codec_context = getattr(stream, "codec_context", None)
    transfer = str(
        getattr(codec_context, "color_trc", "") or getattr(stream, "color_trc", "")
    )
    primaries = str(
        getattr(codec_context, "color_primaries", "")
        or getattr(stream, "color_primaries", "")
    )
    metadata_values = (
        getattr(stream, "metadata", {}),
        getattr(stream, "side_data", []),
        getattr(stream, "side_data_list", []),
        getattr(codec_context, "extradata", b""),
    )
    return _hdr_format_from_metadata(
        transfer=transfer,
        primaries=primaries,
        metadata_values=metadata_values,
    )


def _import_av() -> _AvModuleLike:
    try:
        import av

        return cast("_AvModuleLike", av)
    except ModuleNotFoundError as exc:
        raise ProbeError(
            "PyAV backend selected but the 'av' package is not installed."
        ) from exc


class _FfprobeBackend:
    """Metadata probe backend that shells out to ffprobe."""

    def __init__(
        self,
        ffprobe_exe_path: str = "",
        *,
        scan_child_cpu_priority: ScanProcessCpuPriority = "normal",
        scan_child_io_mode: ScanProcessIoMode = "normal",
    ) -> None:
        """Store one optional ffprobe override path for future launches."""
        self._ffprobe_exe_path = ffprobe_exe_path
        self._scan_child_cpu_priority: ScanProcessCpuPriority = (
            normalize_scan_cpu_priority(scan_child_cpu_priority)
        )
        self._scan_child_io_mode: ScanProcessIoMode = normalize_scan_io_mode(
            scan_child_io_mode
        )

    def ensure_available(self) -> str:
        if self._ffprobe_exe_path:
            return ensure_ffprobe_available(self._ffprobe_exe_path)
        return ensure_ffprobe_available()

    def probe_video(self, path: str) -> VideoMeta:
        ffprobe_path = self.ensure_available()
        cmd = [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,bit_rate:"
                "stream=index,codec_type,codec_name,width,height,r_frame_rate,bit_rate,"
                "color_transfer,color_primaries,color_space,pix_fmt,"
                "bits_per_raw_sample,side_data_list:"
                "stream_tags=language"
            ),
            "-of",
            "json",
            path,
        ]
        popen_kwargs: dict[str, Any] = apply_subprocess_cpu_priority_kwargs(
            {
                **windows_no_window_popen_kwargs(),
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            },
            self._scan_child_cpu_priority,
        )
        try:
            process: subprocess.Popen[str] = subprocess.Popen(cmd, **popen_kwargs)
            apply_scan_child_process_io_mode(process, self._scan_child_io_mode)
            stdout, stderr = process.communicate()
        except OSError as exc:
            raise ProbeError(f"ffprobe failed for {path}: {exc}") from exc
        if process.returncode:
            raise ProbeError((stderr or "").strip() or f"ffprobe failed for {path}")
        try:
            raw_payload: object = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"Invalid ffprobe JSON for {path}") from exc

        payload = _string_object_dict(raw_payload)
        streams = _dict_list(payload.get("streams"))
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not video:
            raise ProbeError("No video stream found")
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
        has_audio = bool(audio_streams)

        fmt = _string_object_dict(payload.get("format"))
        duration_s = _positive_float(fmt.get("duration"))
        bitrate = _positive_int(fmt.get("bit_rate"))
        width = _positive_int(video.get("width"))
        height = _positive_int(video.get("height"))
        fps = _parse_fps(str(video.get("r_frame_rate", "0/0")))
        codec = str(video.get("codec_name") or "").lower()
        audio_codec = (
            str(audio_streams[0].get("codec_name") or "").lower()
            if audio_streams
            else ""
        )
        audio_bitrate = int(
            sum(_positive_float(s.get("bit_rate")) for s in audio_streams)
            or (
                _positive_float(audio_streams[0].get("bit_rate"))
                if audio_streams
                else 0.0
            )
        )
        audio_languages = ",".join(
            sorted(
                {
                    str(_string_object_dict(s.get("tags")).get("language") or "")
                    .strip()
                    .lower()
                    for s in audio_streams
                    if str(
                        _string_object_dict(s.get("tags")).get("language") or ""
                    ).strip()
                }
            )
        )
        subtitle_languages = ",".join(
            sorted(
                {
                    str(_string_object_dict(s.get("tags")).get("language") or "")
                    .strip()
                    .lower()
                    for s in subtitle_streams
                    if str(
                        _string_object_dict(s.get("tags")).get("language") or ""
                    ).strip()
                }
            )
        )
        transfer = str(video.get("color_transfer") or "").lower()
        primaries = str(video.get("color_primaries") or "").lower()
        bit_depth = _ffprobe_bit_depth(video)
        hdr_format = _hdr_format_from_metadata(
            transfer=transfer,
            primaries=primaries,
            metadata_values=(
                video.get("side_data_list"),
                video,
                _string_object_dict(video.get("tags")),
            ),
        )

        if duration_s <= 0 or width <= 0 or height <= 0:
            raise ProbeError("Invalid video metadata")

        return VideoMeta(
            duration_s=duration_s,
            width=width,
            height=height,
            fps=fps,
            bit_depth=bit_depth,
            hdr_format=hdr_format,
            codec=codec,
            bitrate=bitrate,
            has_audio=has_audio,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            audio_languages=audio_languages,
            subtitle_languages=subtitle_languages,
        )


class _PyAvBackend:
    """Metadata probe backend that reads media data through PyAV."""

    def ensure_available(self) -> _AvModuleLike:
        return _import_av()

    def probe_video(self, path: str) -> VideoMeta:
        av_module = self.ensure_available()
        try:
            container: _AvContainerLike = av_module.open(path)
        except Exception as exc:
            raise ProbeError(f"PyAV failed to open {path}: {exc}") from exc
        try:
            with container:
                streams = list(container.streams)
                video_stream = next(
                    (stream for stream in streams if stream.type == "video"),
                    None,
                )
                if video_stream is None:
                    raise ProbeError("No video stream found")
                audio_streams = [stream for stream in streams if stream.type == "audio"]
                subtitle_streams = [
                    stream for stream in streams if stream.type == "subtitle"
                ]

                width, height = _video_dimensions(video_stream)
                duration_s = _duration_seconds_from_container(container, av_module)
                if duration_s <= 0.0:
                    duration_s = _duration_seconds_from_stream(video_stream)
                fps = _video_fps(video_stream)
                codec = _codec_name(video_stream)
                bitrate = max(
                    _positive_int(getattr(container, "bit_rate", 0)),
                    _stream_bitrate(video_stream),
                )
                has_audio = bool(audio_streams)
                audio_codec = _codec_name(audio_streams[0]) if audio_streams else ""
                audio_bitrate = sum(_stream_bitrate(stream) for stream in audio_streams)
                audio_languages = _sorted_languages(audio_streams)
                subtitle_languages = _sorted_languages(subtitle_streams)
                bit_depth = _pyav_bit_depth(video_stream)
                hdr_format = _pyav_hdr_format(video_stream)

                if duration_s <= 0.0 or width <= 0 or height <= 0:
                    raise ProbeError("Invalid video metadata")

                return VideoMeta(
                    duration_s=duration_s,
                    width=width,
                    height=height,
                    fps=fps,
                    bit_depth=bit_depth,
                    hdr_format=hdr_format,
                    codec=codec,
                    bitrate=bitrate,
                    has_audio=has_audio,
                    audio_codec=audio_codec,
                    audio_bitrate=audio_bitrate,
                    audio_languages=audio_languages,
                    subtitle_languages=subtitle_languages,
                )
        except ProbeError:
            raise
        except Exception as exc:
            raise ProbeError(f"PyAV failed for {path}: {exc}") from exc


_PYAV_BACKEND = _PyAvBackend()
