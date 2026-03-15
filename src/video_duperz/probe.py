"""Probe backends for extracting video metadata."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Protocol, cast

from threep_commons.subprocess_helpers import windows_no_window_run_kwargs

from .models import ProbeBackendId, VideoMeta

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


def ensure_ffprobe_available() -> str:
    """Return the ffprobe executable path or raise when it is unavailable."""

    path = shutil.which("ffprobe")
    if not path:
        raise ProbeError(
            "ffprobe not found on PATH. Install ffmpeg and add it to PATH."
        )
    return path


def ensure_probe_backend_available(backend: ProbeBackendId = "pyav") -> None:
    """Raise `ProbeError` when the selected probe backend is unavailable."""

    get_probe_backend(backend).ensure_available()


def get_probe_backend(backend: ProbeBackendId = "pyav") -> ProbeBackend:
    """Return the selected probe backend implementation."""

    if backend == "pyav":
        return _PYAV_BACKEND
    return _FFPROBE_BACKEND


def probe_video(path: str, *, backend: ProbeBackendId = "pyav") -> VideoMeta:
    """Run the selected probe backend and normalize the result into `VideoMeta`."""

    return get_probe_backend(backend).probe_video(path)


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
        return max(0.0, float(duration) * scale)
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


def _video_hdr(stream: Any) -> bool:
    codec_context = getattr(stream, "codec_context", None)
    transfer = str(
        getattr(codec_context, "color_trc", "") or getattr(stream, "color_trc", "")
    ).lower()
    primaries = str(
        getattr(codec_context, "color_primaries", "")
        or getattr(stream, "color_primaries", "")
    ).lower()
    return transfer in {"smpte2084", "arib-std-b67"} or primaries in {
        "bt2020",
        "bt2020nc",
        "bt2020c",
    }


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

    def ensure_available(self) -> str:
        return ensure_ffprobe_available()

    def probe_video(self, path: str) -> VideoMeta:
        ffprobe_path = self.ensure_available()
        hidden_kwargs = windows_no_window_run_kwargs()
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
        cmd = [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,bit_rate:"
                "stream=index,codec_type,codec_name,width,height,r_frame_rate,bit_rate,"
                "color_transfer,color_primaries,color_space,pix_fmt:"
                "stream_tags=language"
            ),
            "-of",
            "json",
            path,
        ]
        try:
            if startupinfo is not None:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    creationflags=creationflags,
                    startupinfo=startupinfo,
                )
            elif creationflags:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    creationflags=creationflags,
                )
            else:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            raise ProbeError(
                stderr.strip() or f"ffprobe failed for {path}"
            ) from exc
        stdout = proc.stdout
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
        is_hdr = transfer in {"smpte2084", "arib-std-b67"} or primaries in {
            "bt2020",
            "bt2020nc",
            "bt2020c",
        }

        if duration_s <= 0 or width <= 0 or height <= 0:
            raise ProbeError("Invalid video metadata")

        return VideoMeta(
            duration_s=duration_s,
            width=width,
            height=height,
            fps=fps,
            codec=codec,
            bitrate=bitrate,
            has_audio=has_audio,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            audio_languages=audio_languages,
            subtitle_languages=subtitle_languages,
            is_hdr=is_hdr,
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
                is_hdr = _video_hdr(video_stream)

                if duration_s <= 0.0 or width <= 0 or height <= 0:
                    raise ProbeError("Invalid video metadata")

                return VideoMeta(
                    duration_s=duration_s,
                    width=width,
                    height=height,
                    fps=fps,
                    codec=codec,
                    bitrate=bitrate,
                    has_audio=has_audio,
                    audio_codec=audio_codec,
                    audio_bitrate=audio_bitrate,
                    audio_languages=audio_languages,
                    subtitle_languages=subtitle_languages,
                    is_hdr=is_hdr,
                )
        except ProbeError:
            raise
        except Exception as exc:
            raise ProbeError(f"PyAV failed for {path}: {exc}") from exc


_FFPROBE_BACKEND = _FfprobeBackend()
_PYAV_BACKEND = _PyAvBackend()
