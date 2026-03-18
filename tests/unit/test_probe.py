from __future__ import annotations

import subprocess
from fractions import Fraction
from types import SimpleNamespace

import pytest

from video_duperz.models import VideoMeta
from video_duperz.probe import (
    ProbeError,
    ensure_ffprobe_available,
    ensure_probe_backend_available,
    probe_video,
)


def test_probe_video_ffprobe_uses_hidden_window_kwargs_and_resolved_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "format": {"duration": "10.5", "bit_rate": "123456"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30000/1001",
                "color_transfer": "",
                "color_primaries": "",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "bit_rate": "64000",
                "tags": {"language": "eng"},
            },
        ],
    }
    recorded: dict[str, object] = {}
    io_modes: list[object] = []

    monkeypatch.setattr(
        "video_duperz.probe.ensure_ffprobe_available",
        lambda: r"C:\ffmpeg\bin\ffprobe.exe",
    )
    monkeypatch.setattr(
        "video_duperz.probe.windows_no_window_popen_kwargs",
        lambda: {"creationflags": 0x08000000},
    )
    monkeypatch.setattr(
        "video_duperz.probe.apply_scan_child_process_io_mode",
        lambda process, io_mode: io_modes.append((process.pid, io_mode)),
    )

    class _FakeProcess:
        pid = 4321
        returncode = 0

        def communicate(self) -> tuple[str, str]:
            return (__import__("json").dumps(payload), "")

    def _fake_popen(cmd: list[str], **kwargs: object) -> _FakeProcess:
        recorded["cmd"] = list(cmd)
        recorded["kwargs"] = dict(kwargs)
        return _FakeProcess()

    monkeypatch.setattr("video_duperz.probe.subprocess.Popen", _fake_popen)

    meta = probe_video(
        r"C:\videos\sample.mp4",
        backend="ffprobe",
        scan_child_cpu_priority="high",
        scan_child_io_mode="background",
    )

    assert meta == VideoMeta(
        duration_s=10.5,
        width=1920,
        height=1080,
        fps=30000 / 1001,
        codec="h264",
        bitrate=123456,
        has_audio=True,
        audio_codec="aac",
        audio_bitrate=64000,
        audio_languages="eng",
        subtitle_languages="",
        is_hdr=False,
    )
    assert recorded["cmd"] == [
        r"C:\ffmpeg\bin\ffprobe.exe",
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
        r"C:\videos\sample.mp4",
    ]
    assert recorded["kwargs"] == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "creationflags": 0x08000000 | int(subprocess.HIGH_PRIORITY_CLASS),
    }
    assert io_modes == [(4321, "background")]


def test_ensure_ffprobe_available_uses_path_lookup_when_override_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "video_duperz.executable_paths.shutil.which",
        lambda tool_name: (
            r"C:\ffmpeg\bin\ffprobe.exe" if tool_name == "ffprobe" else None
        ),
    )

    assert ensure_ffprobe_available("") == r"C:\ffmpeg\bin\ffprobe.exe"


def test_ensure_ffprobe_available_raises_for_invalid_override_path() -> None:
    with pytest.raises(
        ProbeError,
        match=r"ffprobe executable override path is invalid: C:\\missing\\ffprobe\.exe",
    ):
        ensure_ffprobe_available(r"C:\missing\ffprobe.exe")


def test_probe_video_pyav_maps_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeCodecContext:
        def __init__(
            self,
            *,
            name: str,
            width: int = 0,
            height: int = 0,
            bit_rate: int = 0,
            color_trc: str = "",
            color_primaries: str = "",
        ) -> None:
            self.name = name
            self.width = width
            self.height = height
            self.bit_rate = bit_rate
            self.color_trc = color_trc
            self.color_primaries = color_primaries
            self.codec = SimpleNamespace(name=name)

    class _FakeStream:
        def __init__(
            self,
            *,
            stream_type: str,
            codec_context: _FakeCodecContext,
            average_rate: object = None,
            duration: int | None = None,
            time_base: object = None,
            metadata: dict[str, str] | None = None,
            bit_rate: int = 0,
        ) -> None:
            self.type = stream_type
            self.codec_context = codec_context
            self.average_rate = average_rate
            self.duration = duration
            self.time_base = time_base
            self.metadata = metadata or {}
            self.bit_rate = bit_rate
            self.width = codec_context.width
            self.height = codec_context.height

    class _FakeContainer:
        def __init__(self) -> None:
            self.duration = 10_000_000
            self.bit_rate = 8_000_000
            self.streams = [
                _FakeStream(
                    stream_type="video",
                    codec_context=_FakeCodecContext(
                        name="hevc",
                        width=3840,
                        height=2160,
                        bit_rate=7_000_000,
                        color_trc="smpte2084",
                        color_primaries="bt2020",
                    ),
                    average_rate=Fraction(24000, 1001),
                    duration=240,
                    time_base=Fraction(1, 24),
                ),
                _FakeStream(
                    stream_type="audio",
                    codec_context=_FakeCodecContext(name="eac3", bit_rate=640000),
                    metadata={"language": "eng"},
                    bit_rate=640000,
                ),
                _FakeStream(
                    stream_type="audio",
                    codec_context=_FakeCodecContext(name="aac", bit_rate=128000),
                    metadata={"language": "deu"},
                    bit_rate=128000,
                ),
                _FakeStream(
                    stream_type="subtitle",
                    codec_context=_FakeCodecContext(name="subrip"),
                    metadata={"language": "pol"},
                ),
            ]

        def __enter__(self) -> _FakeContainer:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "video_duperz.probe._import_av",
        lambda: SimpleNamespace(
            time_base=Fraction(1, 1_000_000),
            open=lambda _path: _FakeContainer(),
        ),
    )

    meta = probe_video(r"C:\videos\hdr.mkv", backend="pyav")

    assert meta == VideoMeta(
        duration_s=10.0,
        width=3840,
        height=2160,
        fps=24000 / 1001,
        codec="hevc",
        bitrate=8_000_000,
        has_audio=True,
        audio_codec="eac3",
        audio_bitrate=768000,
        audio_languages="deu,eng",
        subtitle_languages="pol",
        is_hdr=True,
    )


def test_probe_video_pyav_handles_integer_time_base_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeCodecContext:
        def __init__(self, *, name: str, width: int, height: int) -> None:
            self.name = name
            self.width = width
            self.height = height
            self.bit_rate = 0
            self.color_trc = ""
            self.color_primaries = ""
            self.codec = SimpleNamespace(name=name)

    class _FakeStream:
        def __init__(self) -> None:
            self.type = "video"
            self.codec_context = _FakeCodecContext(
                name="h264",
                width=1280,
                height=720,
            )
            self.average_rate = Fraction(24, 1)
            self.duration = 1680
            self.time_base = Fraction(1, 24)
            self.metadata: dict[str, str] = {}
            self.bit_rate = 0
            self.width = 1280
            self.height = 720

    class _FakeContainer:
        def __init__(self) -> None:
            self.duration = 310_822_993
            self.bit_rate = 2_000_000
            self.streams = [_FakeStream()]

        def __enter__(self) -> _FakeContainer:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "video_duperz.probe._import_av",
        lambda: SimpleNamespace(
            time_base=1_000_000,
            open=lambda _path: _FakeContainer(),
        ),
    )

    meta = probe_video(r"C:\videos\sample.mp4", backend="pyav")

    assert meta.duration_s == pytest.approx(310.822993)
    assert meta.width == 1280
    assert meta.height == 720
    assert meta.codec == "h264"


def test_ensure_probe_backend_available_raises_for_missing_pyav(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> object:
        raise ProbeError("PyAV backend selected but the 'av' package is not installed.")

    monkeypatch.setattr("video_duperz.probe._import_av", _raise)

    with pytest.raises(ProbeError, match="av"):
        ensure_probe_backend_available("pyav")
