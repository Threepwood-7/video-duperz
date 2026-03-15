from __future__ import annotations

import json

import numpy as np
import pytest

from video_duperz.fingerprint import (
    FingerprintError,
    _DecoderAttemptResult,
    build_fingerprint_record_with_fallback,
    dhash_from_gray,
    fingerprint_child_stdio,
    normalized_median_distance,
    run_fingerprint_child_from_stdio,
)


def test_dhash_deterministic() -> None:
    arr = np.arange(32 * 32, dtype=np.uint8).reshape(32, 32)
    first = dhash_from_gray(arr)
    second = dhash_from_gray(arr.copy())
    assert first == second


def test_normalized_distance_bounds() -> None:
    a = [0xAAAAAAAAAAAAAAAA] * 12
    b = [0xAAAAAAAAAAAAAAAA] * 12
    c = [0x5555555555555555] * 12
    assert normalized_median_distance(a, b) == 0.0
    assert normalized_median_distance(a, c) > 0.9


def test_risky_formats_bypass_opencv() -> None:
    seen_backends: list[str] = []

    def _runner(
        path: str,
        duration_s: float,
        decoder_backend: str,
        timeout_s: float,
    ) -> _DecoderAttemptResult:
        _ = path, duration_s, timeout_s
        seen_backends.append(decoder_backend)
        return _DecoderAttemptResult(status="success", hashes=[1, 2, 3])

    result = build_fingerprint_record_with_fallback(
        file_id=7,
        duration_s=12.5,
        path="D:/Videos/stuck.wmv",
        attempt_runner=_runner,
    )

    provenance = json.loads(result.provenance_json)
    assert seen_backends == ["pyav"]
    assert result.decoder_backend == "pyav"
    assert provenance["risky_format_bypass"] is True


def test_non_risky_formats_try_opencv_first_then_fallback() -> None:
    seen_backends: list[str] = []

    def _runner(
        path: str,
        duration_s: float,
        decoder_backend: str,
        timeout_s: float,
    ) -> _DecoderAttemptResult:
        _ = path, duration_s, timeout_s
        seen_backends.append(decoder_backend)
        if decoder_backend == "opencv":
            return _DecoderAttemptResult(
                status="timeout",
                message="Decoder 'opencv' timed out after 15.0s.",
            )
        return _DecoderAttemptResult(status="success", hashes=[4, 5, 6])

    result = build_fingerprint_record_with_fallback(
        file_id=8,
        duration_s=33.0,
        path="D:/Videos/ok.mp4",
        attempt_runner=_runner,
    )

    provenance = json.loads(result.provenance_json)
    assert seen_backends == ["opencv", "pyav"]
    assert result.decoder_backend == "pyav"
    assert result.fallback_decoder == "pyav"
    assert provenance["attempts"][0]["status"] == "timeout"
    assert provenance["attempts"][1]["status"] == "success"


def test_pyav_failure_falls_back_to_ffmpeg() -> None:
    seen_backends: list[str] = []

    def _runner(
        path: str,
        duration_s: float,
        decoder_backend: str,
        timeout_s: float,
    ) -> _DecoderAttemptResult:
        _ = path, duration_s, timeout_s
        seen_backends.append(decoder_backend)
        if decoder_backend == "pyav":
            return _DecoderAttemptResult(status="error", message="pyav failed")
        return _DecoderAttemptResult(status="success", hashes=[7, 8, 9])

    result = build_fingerprint_record_with_fallback(
        file_id=9,
        duration_s=44.0,
        path="D:/Videos/risky.asf",
        attempt_runner=_runner,
    )

    assert seen_backends == ["pyav", "ffmpeg"]
    assert result.decoder_backend == "ffmpeg"
    assert result.fallback_decoder == "ffmpeg"


def test_full_decoder_chain_failure_raises_fingerprint_error() -> None:
    def _runner(
        path: str,
        duration_s: float,
        decoder_backend: str,
        timeout_s: float,
    ) -> _DecoderAttemptResult:
        _ = path, duration_s, timeout_s
        return _DecoderAttemptResult(
            status="error",
            message=f"{decoder_backend} failed",
        )

    with pytest.raises(FingerprintError) as exc_info:
        build_fingerprint_record_with_fallback(
            file_id=10,
            duration_s=55.0,
            path="D:/Videos/fail.mp4",
            attempt_runner=_runner,
        )

    message = str(exc_info.value)
    assert "opencv failed" in message
    assert "pyav failed" in message
    assert "ffmpeg failed" in message


def test_fingerprint_child_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "video_duperz.fingerprint._compute_hashes_for_decoder",
        lambda path, duration_s, decoder_backend: [11, 22, 33],
    )
    payload = json.dumps(
        {
            "path": "D:/Videos/ok.mp4",
            "duration_s": 12.0,
            "decoder_backend": "opencv",
        }
    )

    with fingerprint_child_stdio(payload) as (stdout_io, stderr_io):
        rc = run_fingerprint_child_from_stdio()

    assert rc == 0
    assert stderr_io.getvalue() == ""
    assert json.loads(stdout_io.getvalue()) == {
        "status": "success",
        "hashes": [11, 22, 33],
    }


def test_fingerprint_child_reports_structured_fingerprint_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(path: str, duration_s: float, decoder_backend: str) -> list[int]:
        _ = path, duration_s, decoder_backend
        raise FingerprintError("decoder failed")

    monkeypatch.setattr("video_duperz.fingerprint._compute_hashes_for_decoder", _raise)
    payload = json.dumps(
        {
            "path": "D:/Videos/bad.wmv",
            "duration_s": 5.0,
            "decoder_backend": "pyav",
        }
    )

    with fingerprint_child_stdio(payload) as (stdout_io, stderr_io):
        rc = run_fingerprint_child_from_stdio()

    assert rc == 0
    assert stderr_io.getvalue() == ""
    assert json.loads(stdout_io.getvalue()) == {
        "status": "error",
        "decoder_backend": "pyav",
        "message": "decoder failed",
    }
