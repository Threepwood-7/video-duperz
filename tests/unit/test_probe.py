from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace

import pytest

from video_duperz.models import VideoMeta
from video_duperz.probe import (
    ProbeError,
    ensure_probe_backend_available,
    ensure_probe_fallback_chain_available,
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

    monkeypatch.setattr(
        "video_duperz.probe.ensure_ffprobe_available",
        lambda: r"C:\ffmpeg\bin\ffprobe.exe",
    )
    monkeypatch.setattr(
        "video_duperz.probe.windows_no_window_run_kwargs",
        lambda: {"creationflags": 0x08000000},
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        recorded["cmd"] = list(cmd)
        recorded["kwargs"] = dict(kwargs)
        return SimpleNamespace(stdout=__import__("json").dumps(payload))

    monkeypatch.setattr("video_duperz.probe.subprocess.run", _fake_run)

    meta = probe_video(r"C:\videos\sample.mp4", backend="ffprobe")

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
        "capture_output": True,
        "text": True,
        "check": True,
        "creationflags": 0x08000000,
    }


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


def test_probe_video_pyav_maps_metadata_when_av_time_base_is_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            ]

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

    meta = probe_video(r"C:\videos\hdr.mkv", backend="pyav")

    assert meta.duration_s == 10.0
    assert meta.codec == "hevc"
    assert meta.width == 3840
    assert meta.height == 2160


def test_probe_video_ffprobe_relaxed_mode_adds_lenient_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}
    payload = {
        "format": {"duration": "1.0", "bit_rate": "1000"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1280,
                "height": 720,
                "r_frame_rate": "24/1",
            }
        ],
    }

    monkeypatch.setattr(
        "video_duperz.probe.ensure_ffprobe_available",
        lambda: r"C:\ffmpeg\bin\ffprobe.exe",
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        recorded["cmd"] = list(cmd)
        recorded["kwargs"] = dict(kwargs)
        return SimpleNamespace(stdout=__import__("json").dumps(payload))

    monkeypatch.setattr("video_duperz.probe.subprocess.run", _fake_run)

    probe_video(r"C:\videos\sample.mp4", backend="ffprobe", relaxed=True)

    assert recorded["cmd"][:11] == [
        r"C:\ffmpeg\bin\ffprobe.exe",
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
    ]


def test_ensure_probe_fallback_chain_checks_primary_and_alternate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _fake_ensure(backend: str = "pyav") -> None:
        calls.append(backend)

    monkeypatch.setattr(
        "video_duperz.probe.ensure_probe_backend_available",
        _fake_ensure,
    )

    ensure_probe_fallback_chain_available("pyav")

    assert calls == ["pyav", "ffprobe"]


def test_ensure_probe_backend_available_raises_for_missing_pyav(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> object:
        raise ProbeError("PyAV backend selected but the 'av' package is not installed.")

    monkeypatch.setattr("video_duperz.probe._import_av", _raise)

    with pytest.raises(ProbeError, match="av"):
        ensure_probe_backend_available("pyav")
