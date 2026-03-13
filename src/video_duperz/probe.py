"""ffprobe integration helpers for extracting video metadata."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction

from .models import VideoMeta


class ProbeError(RuntimeError):
    """Raised when ffprobe is unavailable or returns unusable metadata."""

    pass


def ensure_ffprobe_available() -> str:
    """Return the ffprobe executable path or raise when it is unavailable."""
    path = shutil.which("ffprobe")
    if not path:
        raise ProbeError(
            "ffprobe not found on PATH. Install ffmpeg and add it to PATH."
        )
    return path


def _parse_fps(rate: str) -> float:
    if not rate:
        return 0.0
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_video(path: str) -> VideoMeta:
    """Run ffprobe for a path and normalize the result into `VideoMeta`."""
    ensure_ffprobe_available()
    cmd = [
        "ffprobe",
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
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise ProbeError(exc.stderr.strip() or f"ffprobe failed for {path}") from exc
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"Invalid ffprobe JSON for {path}") from exc

    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ProbeError("No video stream found")
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    has_audio = bool(audio_streams)

    fmt = payload.get("format", {})
    duration_s = float(fmt.get("duration") or 0.0)
    bitrate = int(float(fmt.get("bit_rate") or 0))
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = _parse_fps(str(video.get("r_frame_rate", "0/0")))
    codec = str(video.get("codec_name") or "").lower()
    audio_codec = (
        str(audio_streams[0].get("codec_name") or "").lower() if audio_streams else ""
    )
    audio_bitrate = int(
        sum(float(s.get("bit_rate") or 0.0) for s in audio_streams)
        or (float(audio_streams[0].get("bit_rate") or 0.0) if audio_streams else 0.0)
    )
    audio_languages = ",".join(
        sorted(
            {
                str(s.get("tags", {}).get("language") or "").strip().lower()
                for s in audio_streams
                if str(s.get("tags", {}).get("language") or "").strip()
            }
        )
    )
    subtitle_languages = ",".join(
        sorted(
            {
                str(s.get("tags", {}).get("language") or "").strip().lower()
                for s in subtitle_streams
                if str(s.get("tags", {}).get("language") or "").strip()
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
