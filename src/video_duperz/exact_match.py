from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_MIB = 1024 * 1024
_BASE_LABELS: tuple[str, str, str, str] = ("●", "■", "♥", "♦")


@dataclass(slots=True, frozen=True)
class ExactMatchFile:
    file_id: int
    path: str
    size: int


@dataclass(slots=True, frozen=True)
class ExactMatchResult:
    labels: dict[int, str]
    errors: dict[int, str]


def normalize_exact_sample_pair(
    sample_a_pct: int | object,
    sample_b_pct: int | object,
    default_a: int = 23,
    default_b: int = 78,
) -> tuple[int, int]:
    def _normalize_percent(value: int | object, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(0, min(100, parsed))

    a = _normalize_percent(sample_a_pct, default_a)
    b = _normalize_percent(sample_b_pct, default_b)
    if a == b:
        if b < 100:
            b += 1
        else:
            a = max(0, a - 1)
    if a > b:
        a, b = b, a
    return a, b


def sample_offsets(file_size: int, block_bytes: int, sample_a_pct: int, sample_b_pct: int) -> list[int]:
    size = max(0, int(file_size))
    block = max(1, int(block_bytes))
    a, b = normalize_exact_sample_pair(sample_a_pct, sample_b_pct)
    span = max(0, size - block)
    raw_offsets = [
        0,
        span,
        round(span * (a / 100.0)),
        round(span * (b / 100.0)),
    ]
    normalized = {
        max(0, min(span, int(offset)))
        for offset in raw_offsets
    }
    return sorted(normalized)


def _alpha_label(index: int) -> str:
    n = int(index) + 1
    chars: list[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(ord("a") + rem))
    return "".join(reversed(chars))


def _cluster_label(cluster_index: int) -> str:
    if cluster_index < len(_BASE_LABELS):
        return _BASE_LABELS[cluster_index]
    return _alpha_label(cluster_index - len(_BASE_LABELS))


def _sampled_file_digest(file: ExactMatchFile, block_bytes: int, sample_a_pct: int, sample_b_pct: int) -> str:
    path = Path(file.path)
    if not path.exists():
        raise FileNotFoundError("source file missing")
    if not path.is_file():
        raise OSError("source path is not a file")

    expected_size = max(0, int(file.size))
    actual_size = int(path.stat().st_size)
    if actual_size != expected_size:
        raise OSError(f"file size changed (scan={expected_size}, actual={actual_size})")

    digest = hashlib.sha256()
    digest.update(struct.pack(">Q", expected_size))

    offsets = sample_offsets(expected_size, block_bytes, sample_a_pct, sample_b_pct)
    with path.open("rb") as handle:
        for offset in offsets:
            length = max(0, min(block_bytes, expected_size - offset))
            digest.update(struct.pack(">QQ", int(offset), int(length)))
            if length <= 0:
                continue
            handle.seek(int(offset))
            payload = handle.read(int(length))
            if len(payload) != int(length):
                raise OSError("short read while sampling file")
            digest.update(payload)

    return digest.hexdigest()


def compare_group_files(
    files: Iterable[ExactMatchFile],
    block_mib: int = 1,
    sample_a_pct: int = 23,
    sample_b_pct: int = 78,
) -> ExactMatchResult:
    block_bytes = max(1, int(block_mib)) * _MIB
    a, b = normalize_exact_sample_pair(sample_a_pct, sample_b_pct)
    files_list = list(files)

    signatures: dict[int, str] = {}
    errors: dict[int, str] = {}
    for file in files_list:
        try:
            signatures[file.file_id] = _sampled_file_digest(
                file=file,
                block_bytes=block_bytes,
                sample_a_pct=a,
                sample_b_pct=b,
            )
        except Exception as exc:
            errors[file.file_id] = str(exc)

    buckets: dict[tuple[int, str], list[ExactMatchFile]] = {}
    for file in files_list:
        signature = signatures.get(file.file_id)
        if signature is None:
            continue
        key = (max(0, int(file.size)), signature)
        buckets.setdefault(key, []).append(file)

    clusters = [
        sorted(bucket, key=lambda item: item.path.casefold())
        for bucket in buckets.values()
        if len(bucket) >= 2
    ]
    clusters.sort(key=lambda bucket: bucket[0].path.casefold())

    labels: dict[int, str] = {}
    for idx, cluster in enumerate(clusters):
        label = _cluster_label(idx)
        for file in cluster:
            labels[file.file_id] = label

    return ExactMatchResult(labels=labels, errors=errors)
