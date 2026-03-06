from __future__ import annotations

import numpy as np

from video_duperz.fingerprint import dhash_from_gray, normalized_median_distance


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
