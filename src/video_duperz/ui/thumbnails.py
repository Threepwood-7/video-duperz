from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from ..config import app_data_dir

try:
    import cv2  # type: ignore
except ImportError:
    cv2 = None  # type: ignore[assignment]


THUMBNAIL_SIZES: dict[str, tuple[int, int]] = {
    "80x45": (80, 45),
    "96x54": (96, 54),
    "128x72": (128, 72),
    "160x90": (160, 90),
}
DEFAULT_THUMBNAIL_SIZE = "96x54"
THUMBNAIL_CACHE_VERSION = "v2"
FramePercentInput = int | float | str | None


def opencv_available() -> bool:
    return cv2 is not None


def normalize_thumbnail_size_key(size_key: str | None) -> str:
    if not size_key:
        return DEFAULT_THUMBNAIL_SIZE
    cleaned = str(size_key).strip().lower()
    if cleaned in THUMBNAIL_SIZES:
        return cleaned
    return DEFAULT_THUMBNAIL_SIZE


def thumbnail_dimensions(size_key: str) -> tuple[int, int]:
    return THUMBNAIL_SIZES.get(
        normalize_thumbnail_size_key(size_key), THUMBNAIL_SIZES[DEFAULT_THUMBNAIL_SIZE]
    )


def thumbnail_cache_dir() -> Path:
    path = app_data_dir() / "thumbnails"
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_frame_percent(value: FramePercentInput, fallback: int) -> int:
    try:
        parsed = int(float(value if value is not None else fallback))
    except (TypeError, ValueError):
        parsed = fallback
    return max(0, min(100, parsed))


def normalize_frame_pair(
    frame_a_pct: FramePercentInput, frame_b_pct: FramePercentInput
) -> tuple[int, int]:
    a = normalize_frame_percent(frame_a_pct, 23)
    b = normalize_frame_percent(frame_b_pct, 77)
    if a == b:
        b = b + 1 if b < 100 else b - 1
    return a, b


def build_thumbnail_cache_key(
    path: str,
    size: int,
    mtime_ns: int,
    size_key: str,
    frame_pct: int,
    slot: str,
) -> str:
    normalized_size = normalize_thumbnail_size_key(size_key)
    normalized_pct = normalize_frame_percent(frame_pct, 23)
    normalized_slot = str(slot).strip().lower() or "a"
    payload = f"{path}|{size}|{mtime_ns}|{normalized_size}|{normalized_pct}|{normalized_slot}|{THUMBNAIL_CACHE_VERSION}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def thumbnail_cache_path(
    path: str, size: int, mtime_ns: int, size_key: str, frame_pct: int, slot: str
) -> Path:
    cache_key = build_thumbnail_cache_key(
        path=path,
        size=size,
        mtime_ns=mtime_ns,
        size_key=size_key,
        frame_pct=frame_pct,
        slot=slot,
    )
    return thumbnail_cache_dir() / f"{cache_key}.jpg"


def thumbnail_pair_cache_paths(
    path: str,
    size: int,
    mtime_ns: int,
    size_key: str,
    frame_a_pct: int,
    frame_b_pct: int,
) -> tuple[Path, Path]:
    a, b = normalize_frame_pair(frame_a_pct, frame_b_pct)
    cache_a = thumbnail_cache_path(
        path, size=size, mtime_ns=mtime_ns, size_key=size_key, frame_pct=a, slot="a"
    )
    cache_b = thumbnail_cache_path(
        path, size=size, mtime_ns=mtime_ns, size_key=size_key, frame_pct=b, slot="b"
    )
    return cache_a, cache_b


def _read_preview_frame(
    path: str, frame_pct: int
) -> tuple[bool, np.ndarray | None, str | None]:
    if cv2 is None:
        return False, None, "opencv unavailable"

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return False, None, "unable to open video"

    try:
        frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        duration_s = frame_count / fps if fps > 0 else 0.0
        pct = normalize_frame_percent(frame_pct, 23) / 100.0
        target_ms = max(0.0, duration_s * pct * 1000.0)
        cap.set(cv2.CAP_PROP_POS_MSEC, target_ms)
        ok, frame = cap.read()
        if ok:
            return True, frame, None

        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        for _ in range(60):
            ok, frame = cap.read()
            if ok:
                return True, frame, None
        return False, None, "could not decode frame"
    finally:
        cap.release()


def _fit_with_letterbox(frame: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    src_h, src_w = frame.shape[:2]
    if src_w <= 0 or src_h <= 0:
        return np.zeros((target_h, target_w, 3), dtype=np.uint8)

    scale = min(target_w / float(src_w), target_h / float(src_h))
    fit_w = max(1, round(src_w * scale))
    fit_h = max(1, round(src_h * scale))
    interp = cv2.INTER_AREA if (cv2 is not None and scale < 1.0) else cv2.INTER_LINEAR  # type: ignore[union-attr]
    fitted = cv2.resize(frame, (fit_w, fit_h), interpolation=interp)  # type: ignore[union-attr]

    canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
    off_x = (target_w - fit_w) // 2
    off_y = (target_h - fit_h) // 2
    canvas[off_y : off_y + fit_h, off_x : off_x + fit_w] = fitted
    return canvas


def extract_thumbnail_at(
    path: str,
    output_path: str | Path,
    target_w: int,
    target_h: int,
    frame_pct: int,
) -> tuple[bool, str | None]:
    if target_w <= 0 or target_h <= 0:
        return False, "invalid target size"
    if cv2 is None:
        return False, "opencv unavailable"

    ok, frame, error = _read_preview_frame(path, frame_pct=frame_pct)
    if not ok or frame is None:
        return False, error or "could not decode frame"

    final_frame = _fit_with_letterbox(frame, target_w=target_w, target_h=target_h)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_ok = cv2.imwrite(str(out), final_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not write_ok:
        return False, "failed to write thumbnail"
    return True, None


def extract_thumbnail_pair(
    path: str,
    output_path_a: str | Path,
    output_path_b: str | Path,
    target_w: int,
    target_h: int,
    frame_a_pct: int,
    frame_b_pct: int,
) -> tuple[bool, str | None]:
    a, b = normalize_frame_pair(frame_a_pct, frame_b_pct)
    ok_a, err_a = extract_thumbnail_at(
        path, output_path_a, target_w=target_w, target_h=target_h, frame_pct=a
    )
    if not ok_a:
        return False, err_a
    ok_b, err_b = extract_thumbnail_at(
        path, output_path_b, target_w=target_w, target_h=target_h, frame_pct=b
    )
    if not ok_b:
        return False, err_b
    return True, None
