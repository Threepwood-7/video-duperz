from __future__ import annotations

import json

import numpy as np
import pytest

from video_duperz.fingerprint import (
    ALGO_VERSION,
    SCENE_AWARE_ALGO_VERSION,
    FingerprintError,
    _decoder_timeout_for_path,
    _DecoderAttemptResult,
    _ffmpeg_gray_samples,
    build_fingerprint_record_with_fallback,
    dhash_from_gray,
    ensure_ffmpeg_available,
    fingerprint_child_stdio,
    inner_median_distance,
    normalized_median_distance,
    plan_visual_sample_timestamps,
    run_fingerprint_child_from_stdio,
    sample_timestamps,
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


def test_inner_median_distance_uses_only_inner_frames() -> None:
    base = [0xAAAAAAAAAAAAAAAA] * 12
    polluted = base.copy()
    polluted[0] = 0x5555555555555555
    polluted[1] = 0x5555555555555555
    polluted[10] = 0x5555555555555555
    polluted[11] = 0x5555555555555555

    assert inner_median_distance(base, polluted) == 0.0


def test_inner_median_distance_empty_guard() -> None:
    assert inner_median_distance([], []) == 1.0
    assert inner_median_distance([1, 2, 3], [1, 2]) == 1.0


def test_plan_visual_sample_timestamps_uses_scene_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene_points = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
    monkeypatch.setattr(
        "video_duperz.fingerprint._scene_change_candidates",
        lambda *args, **kwargs: list(scene_points),
    )

    timestamps, algo_version = plan_visual_sample_timestamps(
        "D:/Videos/clip.mp4",
        20.0,
        scene_aware_sampling=True,
    )

    assert timestamps == scene_points
    assert algo_version == SCENE_AWARE_ALGO_VERSION


def test_plan_visual_sample_timestamps_falls_back_when_scene_data_is_sparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "video_duperz.fingerprint._scene_change_candidates",
        lambda *args, **kwargs: [1.0, 2.0, 3.0],
    )

    timestamps, algo_version = plan_visual_sample_timestamps(
        "D:/Videos/clip.mp4",
        20.0,
        scene_aware_sampling=True,
    )

    assert timestamps == sample_timestamps(20.0)
    assert algo_version == SCENE_AWARE_ALGO_VERSION


def test_plan_visual_sample_timestamps_keeps_fixed_mode_when_disabled() -> None:
    timestamps, algo_version = plan_visual_sample_timestamps(
        "D:/Videos/clip.mp4",
        20.0,
        scene_aware_sampling=False,
    )

    assert timestamps == sample_timestamps(20.0)
    assert algo_version == ALGO_VERSION


def test_ffmpeg_gray_samples_use_explicit_override_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, float, str, str]] = []

    monkeypatch.setattr(
        "video_duperz.fingerprint.ensure_ffmpeg_available",
        lambda ffmpeg_exe_path="": str(ffmpeg_exe_path),
    )
    monkeypatch.setattr(
        "video_duperz.fingerprint._ffmpeg_gray_frame",
        lambda ffmpeg_path, path, timestamp_s, **kwargs: (
            seen.append(
                (
                    ffmpeg_path,
                    path,
                    timestamp_s,
                    str(kwargs["scan_child_cpu_priority"]),
                    str(kwargs["scan_child_io_mode"]),
                )
            )
            or None
        ),
    )

    _ffmpeg_gray_samples(
        "D:/Videos/clip.mp4",
        10.0,
        ffmpeg_exe_path=r"C:\ffmpeg\bin\ffmpeg.exe",
        scan_child_cpu_priority="below_normal",
        scan_child_io_mode="background",
    )

    assert seen == [
        (
            r"C:\ffmpeg\bin\ffmpeg.exe",
            "D:/Videos/clip.mp4",
            timestamp_s,
            "below_normal",
            "background",
        )
        for timestamp_s in sample_timestamps(10.0)
    ]


def test_ensure_ffmpeg_available_raises_for_invalid_override_path() -> None:
    with pytest.raises(
        FingerprintError,
        match=r"ffmpeg executable override path is invalid: C:\\missing\\ffmpeg\.exe",
    ):
        ensure_ffmpeg_available(r"C:\missing\ffmpeg.exe")


def test_decoder_subprocess_payload_includes_ffmpeg_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.pid = 123

        def communicate(
            self,
            input: str | None = None,
            timeout: float | None = None,
        ) -> tuple[str, str]:
            captured["input"] = input
            captured["timeout"] = timeout
            return (json.dumps({"status": "success", "hashes": [1, 2, 3]}), "")

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(
        "video_duperz.fingerprint.windows_no_window_popen_kwargs",
        lambda: {},
    )
    monkeypatch.setattr(
        "video_duperz.fingerprint.subprocess.Popen",
        lambda command, **kwargs: _FakeProcess(),
    )

    result = build_fingerprint_record_with_fallback(
        file_id=1,
        duration_s=10.0,
        path="D:/Videos/risky.asf",
        ffmpeg_exe_path=r"C:\ffmpeg\bin\ffmpeg.exe",
        scan_child_cpu_priority="above_normal",
        scan_child_io_mode="background",
    )

    payload = json.loads(str(captured["input"]))
    assert payload["ffmpeg_exe_path"] == r"C:\ffmpeg\bin\ffmpeg.exe"
    assert payload["timestamps_s"] == sample_timestamps(10.0)
    assert payload["scan_child_cpu_priority"] == "above_normal"
    assert payload["scan_child_io_mode"] == "background"
    assert result.decoder_backend == "ffmpeg"


def test_problematic_formats_bypass_opencv() -> None:
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
        path="D:/Videos/archive.avi",
        attempt_runner=_runner,
    )

    provenance = json.loads(result.provenance_json)
    assert seen_backends == ["ffmpeg"]
    assert result.decoder_backend == "ffmpeg"
    assert provenance["risky_format_bypass"] is True


def test_problematic_formats_get_extended_decoder_timeout() -> None:
    assert _decoder_timeout_for_path("D:/Videos/stuck.wmv", 15.0) == 60.0
    assert _decoder_timeout_for_path("D:/Videos/archive.avi", 15.0) == 60.0
    assert _decoder_timeout_for_path("D:/Videos/sample.flv", 15.0) == 60.0


def test_normal_formats_keep_default_decoder_timeout() -> None:
    assert _decoder_timeout_for_path("D:/Videos/ok.mp4", 15.0) == 15.0
    assert _decoder_timeout_for_path("D:/Videos/ok.mkv", 15.0) == 15.0


def test_normal_formats_pass_configured_timeout_to_attempt_runner() -> None:
    seen_timeouts: list[float] = []

    def _runner(
        path: str,
        duration_s: float,
        decoder_backend: str,
        timeout_s: float,
    ) -> _DecoderAttemptResult:
        _ = path, duration_s, decoder_backend
        seen_timeouts.append(timeout_s)
        return _DecoderAttemptResult(status="success", hashes=[1, 2, 3])

    build_fingerprint_record_with_fallback(
        file_id=16,
        duration_s=12.0,
        path="D:/Videos/steady.mp4",
        attempt_runner=_runner,
        timeout_s=45.0,
    )

    assert seen_timeouts == [45.0]


def test_problematic_formats_pass_extended_timeout_to_attempt_runner() -> None:
    seen_timeouts: list[float] = []

    def _runner(
        path: str,
        duration_s: float,
        decoder_backend: str,
        timeout_s: float,
    ) -> _DecoderAttemptResult:
        _ = path, duration_s, decoder_backend
        seen_timeouts.append(timeout_s)
        return _DecoderAttemptResult(status="success", hashes=[1, 2, 3])

    build_fingerprint_record_with_fallback(
        file_id=17,
        duration_s=12.0,
        path="D:/Videos/tricky.mpeg",
        attempt_runner=_runner,
        timeout_s=30.0,
    )

    assert seen_timeouts == [120.0]


def test_problematic_mov_formats_use_ffmpeg_first_then_pyav() -> None:
    seen_backends: list[str] = []

    def _runner(
        path: str,
        duration_s: float,
        decoder_backend: str,
        timeout_s: float,
    ) -> _DecoderAttemptResult:
        _ = path, duration_s, timeout_s
        seen_backends.append(decoder_backend)
        if decoder_backend == "ffmpeg":
            return _DecoderAttemptResult(status="error", message="ffmpeg failed")
        return _DecoderAttemptResult(status="success", hashes=[8, 9, 10])

    result = build_fingerprint_record_with_fallback(
        file_id=18,
        duration_s=13.0,
        path="D:/Videos/tricky.mov",
        attempt_runner=_runner,
    )

    assert seen_backends == ["ffmpeg", "pyav"]
    assert result.decoder_backend == "pyav"


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


def test_ffmpeg_failure_falls_back_to_pyav() -> None:
    seen_backends: list[str] = []

    def _runner(
        path: str,
        duration_s: float,
        decoder_backend: str,
        timeout_s: float,
    ) -> _DecoderAttemptResult:
        _ = path, duration_s, timeout_s
        seen_backends.append(decoder_backend)
        if decoder_backend == "ffmpeg":
            return _DecoderAttemptResult(status="error", message="ffmpeg failed")
        return _DecoderAttemptResult(status="success", hashes=[7, 8, 9])

    result = build_fingerprint_record_with_fallback(
        file_id=9,
        duration_s=44.0,
        path="D:/Videos/risky.asf",
        attempt_runner=_runner,
    )

    assert seen_backends == ["ffmpeg", "pyav"]
    assert result.decoder_backend == "pyav"
    assert result.fallback_decoder == "pyav"


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
        lambda path, duration_s, decoder_backend, **_kwargs: [11, 22, 33],
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
    def _raise(
        path: str,
        duration_s: float,
        decoder_backend: str,
        **_kwargs: object,
    ) -> list[int]:
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
