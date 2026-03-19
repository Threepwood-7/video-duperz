from __future__ import annotations

import json
import subprocess

import pytest

from video_duperz.audio_fingerprint import (
    AudioFingerprintError,
    compute_audio_fingerprint,
)


def test_compute_audio_fingerprint_reads_fpcalc_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeProcess:
        returncode = 0
        pid = 123

        def communicate(
            self,
            timeout: float | None = None,
        ) -> tuple[str, str]:
            _ = timeout
            return (json.dumps({"fingerprint": "abc123"}), "")

    monkeypatch.setattr(
        "video_duperz.audio_fingerprint.ensure_fpcalc_available",
        lambda fpcalc_exe_path="": str(fpcalc_exe_path or "fpcalc"),
    )
    monkeypatch.setattr(
        "video_duperz.audio_fingerprint.subprocess.Popen",
        lambda command, **kwargs: _FakeProcess(),
    )

    fingerprint = compute_audio_fingerprint(
        "D:/Videos/clip.mp4",
        fpcalc_exe_path=r"C:\Tools\fpcalc.exe",
    )

    assert fingerprint == "abc123"


def test_compute_audio_fingerprint_raises_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeProcess:
        returncode = 0
        pid = 123

        def communicate(
            self,
            timeout: float | None = None,
        ) -> tuple[str, str]:
            raise subprocess.TimeoutExpired(cmd="fpcalc", timeout=timeout or 0.0)

    monkeypatch.setattr(
        "video_duperz.audio_fingerprint.ensure_fpcalc_available",
        lambda fpcalc_exe_path="": str(fpcalc_exe_path or "fpcalc"),
    )
    monkeypatch.setattr(
        "video_duperz.audio_fingerprint.subprocess.Popen",
        lambda command, **kwargs: _FakeProcess(),
    )

    with pytest.raises(AudioFingerprintError, match="timed out"):
        compute_audio_fingerprint("D:/Videos/clip.mp4")
