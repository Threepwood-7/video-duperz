from __future__ import annotations

import io
from dataclasses import asdict

from video_duperz import analyze_process
from video_duperz.models import VideoMeta


def test_subprocess_analyze_launcher_uses_hidden_window_kwargs(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class _FakeStdin:
        def __init__(self) -> None:
            self.parts: list[str] = []

        def write(self, text: str) -> None:
            self.parts.append(text)

        def close(self) -> None:
            return None

    class _FakeProcess:
        def __init__(self) -> None:
            self.stdin = _FakeStdin()
            self.stdout = None
            self.stderr = None

        def poll(self) -> int | None:
            return None

    fake_process = _FakeProcess()

    monkeypatch.setattr(
        analyze_process,
        "windows_no_window_popen_kwargs",
        lambda: {"creationflags": 0x08000000},
    )

    def _fake_popen(cmd: list[str], **kwargs: object) -> _FakeProcess:
        recorded["cmd"] = list(cmd)
        recorded["kwargs"] = dict(kwargs)
        return fake_process

    monkeypatch.setattr(analyze_process.subprocess, "Popen", _fake_popen)

    launcher = analyze_process.SubprocessAnalyzeLauncher("pyav")
    handle = launcher.launch(
        r"C:\videos\sample.mp4",
        VideoMeta(
            duration_s=1.0,
            width=1920,
            height=1080,
            fps=24.0,
            codec="h264",
            bitrate=1000,
            has_audio=True,
            audio_codec="aac",
            audio_bitrate=100,
            audio_languages="eng",
            subtitle_languages="",
            is_hdr=False,
        ),
    )

    assert recorded["cmd"] == [
        analyze_process.sys.executable,
        "-m",
        "video_duperz",
        "analyze-child",
    ]
    assert recorded["kwargs"] == {
        "stdin": analyze_process.subprocess.PIPE,
        "stdout": analyze_process.subprocess.PIPE,
        "stderr": analyze_process.subprocess.PIPE,
        "text": True,
        "creationflags": 0x08000000,
    }
    payload = "".join(fake_process.stdin.parts)
    assert r'"path": "C:\\videos\\sample.mp4"' in payload
    assert '"probe_backend": "pyav"' in payload
    assert handle is not None


def test_run_analyze_child_from_stdio_emits_success_payload(monkeypatch) -> None:
    output = analyze_process.AnalyzeOutput(
        meta=VideoMeta(
            duration_s=1.0,
            width=1920,
            height=1080,
            fps=24.0,
            codec="h264",
            bitrate=1000,
            has_audio=True,
            audio_codec="aac",
            audio_bitrate=100,
            audio_languages="eng",
            subtitle_languages="",
            is_hdr=False,
        ),
        hashes=[1, 2, 3],
        probe_s=0.1,
        fingerprint_s=0.2,
        probe_fallback_backend="ffprobe",
        fingerprint_fallback_decoder="ffmpeg",
    )
    payload = {
        "path": r"C:\videos\sample.mp4",
        "cached_meta": asdict(output.meta),
        "probe_backend": "pyav",
        "stop_path": "",
    }
    stdin = io.StringIO(analyze_process.json.dumps(payload))
    stdout = io.StringIO()

    monkeypatch.setattr(analyze_process.sys, "stdin", stdin)
    monkeypatch.setattr(analyze_process.sys, "stdout", stdout)
    monkeypatch.setattr(
        analyze_process,
        "analyze_video_file",
        lambda path, cached_meta, *, probe_backend, stop_requested=None: output,
    )

    rc = analyze_process.run_analyze_child_from_stdio()

    assert rc == 0
    response = analyze_process.json.loads(stdout.getvalue())
    assert response["status"] == "success"
    assert response["hashes"] == [1, 2, 3]
    assert response["probe_fallback_backend"] == "ffprobe"
    assert response["fingerprint_fallback_decoder"] == "ffmpeg"


def test_run_analyze_child_from_stdio_emits_structured_probe_error(monkeypatch) -> None:
    payload = {
        "path": r"C:\videos\sample.mp4",
        "cached_meta": None,
        "probe_backend": "pyav",
        "stop_path": "",
    }
    stdin = io.StringIO(analyze_process.json.dumps(payload))
    stdout = io.StringIO()

    monkeypatch.setattr(analyze_process.sys, "stdin", stdin)
    monkeypatch.setattr(analyze_process.sys, "stdout", stdout)
    monkeypatch.setattr(
        analyze_process,
        "analyze_video_file",
        lambda path, cached_meta, *, probe_backend, stop_requested=None: (
            _ for _ in ()
        ).throw(analyze_process.ProbeError("boom")),
    )

    rc = analyze_process.run_analyze_child_from_stdio()

    assert rc == 0
    response = analyze_process.json.loads(stdout.getvalue())
    assert response == {
        "status": "error",
        "stage": "probe",
        "error_type": "ProbeError",
        "message": "boom",
    }
