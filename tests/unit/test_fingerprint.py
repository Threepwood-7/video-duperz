from __future__ import annotations

import numpy as np

from video_duperz.fingerprint import (
    FingerprintError,
    build_fingerprint_record_with_fallback,
    dhash_from_gray,
    normalized_median_distance,
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


def _gray_samples() -> list[np.ndarray]:
    base = np.arange(32 * 32, dtype=np.uint8).reshape(32, 32)
    return [base.copy() for _ in range(12)]


def test_build_fingerprint_record_with_fallback_uses_pyav(monkeypatch) -> None:
    monkeypatch.setattr(
        "video_duperz.fingerprint.compute_video_hashes",
        lambda path, duration_s: (_ for _ in ()).throw(
            FingerprintError("opencv failed")
        ),
    )
    monkeypatch.setattr(
        "video_duperz.fingerprint._pyav_gray_samples",
        lambda path, duration_s: _gray_samples(),
    )

    result = build_fingerprint_record_with_fallback(1, 10.0, "sample.mp4")

    assert result.fallback_decoder == "pyav"
    assert result.record.file_id == 1
    assert result.record.frame_count == 12
    assert any(result.record.hashes)


def test_build_fingerprint_record_with_fallback_uses_ffmpeg_after_pyav(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "video_duperz.fingerprint.compute_video_hashes",
        lambda path, duration_s: (_ for _ in ()).throw(
            FingerprintError("opencv failed")
        ),
    )
    monkeypatch.setattr(
        "video_duperz.fingerprint._pyav_gray_samples",
        lambda path, duration_s: (_ for _ in ()).throw(FingerprintError("pyav failed")),
    )
    monkeypatch.setattr(
        "video_duperz.fingerprint._ffmpeg_gray_samples",
        lambda path, duration_s: _gray_samples(),
    )

    result = build_fingerprint_record_with_fallback(2, 10.0, "sample.mp4")

    assert result.fallback_decoder == "ffmpeg"
    assert result.record.file_id == 2
    assert result.record.frame_count == 12
    assert any(result.record.hashes)
