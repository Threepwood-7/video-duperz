from __future__ import annotations

from statistics import median

import numpy as np

from .models import FingerprintRecord, utc_now_iso

try:
    import cv2  # type: ignore
except ImportError:
    cv2 = None  # type: ignore[assignment]

ALGO_VERSION = 1
SAMPLE_PERCENTS = [
    0.05,
    0.13,
    0.21,
    0.29,
    0.37,
    0.45,
    0.53,
    0.61,
    0.69,
    0.77,
    0.85,
    0.93,
]


class FingerprintError(RuntimeError):
    pass


def sample_timestamps(duration_s: float) -> list[float]:
    if duration_s <= 0:
        return [0.0] * len(SAMPLE_PERCENTS)
    return [duration_s * p for p in SAMPLE_PERCENTS]


def _resize_nearest(gray: np.ndarray, width: int, height: int) -> np.ndarray:
    src_h, src_w = gray.shape[:2]
    if src_h == 0 or src_w == 0:
        return np.zeros((height, width), dtype=np.uint8)
    y_idx = (np.linspace(0, src_h - 1, num=height)).astype(int)
    x_idx = (np.linspace(0, src_w - 1, num=width)).astype(int)
    return gray[np.ix_(y_idx, x_idx)]


def dhash_from_gray(gray_frame: np.ndarray) -> int:
    if cv2 is not None:
        gray32 = cv2.resize(gray_frame, (32, 32), interpolation=cv2.INTER_AREA)
        small = cv2.resize(gray32, (9, 8), interpolation=cv2.INTER_AREA)
    else:
        gray32 = _resize_nearest(gray_frame, 32, 32)
        small = _resize_nearest(gray32, 9, 8)
    diff = small[:, 1:] > small[:, :-1]
    value = 0
    for bit in diff.flatten():
        value = (value << 1) | int(bool(bit))
    return value


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def normalized_median_distance(hashes_a: list[int], hashes_b: list[int]) -> float:
    if len(hashes_a) != len(hashes_b) or not hashes_a:
        return 1.0
    distances = [
        hamming_distance(a, b) for a, b in zip(hashes_a, hashes_b, strict=True)
    ]
    return float(median(distances)) / 64.0


def compute_video_hashes(path: str, duration_s: float) -> list[int]:
    if cv2 is None:
        raise FingerprintError("opencv-python is not installed")
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FingerprintError(f"Unable to open video: {path}")

    timestamps = sample_timestamps(duration_s)
    hashes: list[int] = []
    try:
        for ts in timestamps:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, ts * 1000.0))
            ok, frame = cap.read()
            if not ok or frame is None:
                hashes.append(0)
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hashes.append(dhash_from_gray(gray))
    finally:
        cap.release()

    if not any(hashes):
        raise FingerprintError(f"Could not decode sample frames for {path}")
    return hashes


def build_fingerprint_record(
    file_id: int, duration_s: float, path: str
) -> FingerprintRecord:
    hashes = compute_video_hashes(path, duration_s)
    return FingerprintRecord(
        file_id=file_id,
        algo_version=ALGO_VERSION,
        frame_count=len(hashes),
        hashes=hashes,
        created_at=utc_now_iso(),
    )
