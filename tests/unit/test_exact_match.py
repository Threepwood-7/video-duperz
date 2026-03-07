from __future__ import annotations

from typing import TYPE_CHECKING

from video_duperz.exact_match import ExactMatchFile, compare_group_files, sample_offsets

if TYPE_CHECKING:
    from pathlib import Path


def test_compare_group_files_labels_identical_samples(tmp_path: Path) -> None:
    payload = (b"A" * 1024) + (b"B" * 2048) + (b"C" * 1024)
    file_a = tmp_path / "a.mp4"
    file_b = tmp_path / "b.mp4"
    file_a.write_bytes(payload)
    file_b.write_bytes(payload)

    result = compare_group_files(
        files=[
            ExactMatchFile(file_id=1, path=str(file_a), size=len(payload)),
            ExactMatchFile(file_id=2, path=str(file_b), size=len(payload)),
        ],
        block_mib=1,
        sample_a_pct=23,
        sample_b_pct=78,
    )
    assert result.errors == {}
    assert result.labels[1] == result.labels[2]


def test_compare_group_files_ignores_non_identical_samples(tmp_path: Path) -> None:
    file_a = tmp_path / "a.mp4"
    file_b = tmp_path / "b.mp4"
    file_a.write_bytes(b"A" * 8192)
    file_b.write_bytes(b"B" * 8192)

    result = compare_group_files(
        files=[
            ExactMatchFile(file_id=1, path=str(file_a), size=file_a.stat().st_size),
            ExactMatchFile(file_id=2, path=str(file_b), size=file_b.stat().st_size),
        ]
    )
    assert result.labels == {}
    assert result.errors == {}


def test_compare_group_files_handles_small_files(tmp_path: Path) -> None:
    file_a = tmp_path / "a.mp4"
    file_b = tmp_path / "b.mp4"
    data = b"tiny"
    file_a.write_bytes(data)
    file_b.write_bytes(data)

    result = compare_group_files(
        files=[
            ExactMatchFile(file_id=1, path=str(file_a), size=len(data)),
            ExactMatchFile(file_id=2, path=str(file_b), size=len(data)),
        ],
        block_mib=4,
    )
    assert result.errors == {}
    assert result.labels[1] == result.labels[2]


def test_sample_offsets_first_last_and_percent_positions() -> None:
    size = 10 * 1024 * 1024
    block = 1 * 1024 * 1024
    offsets = sample_offsets(
        file_size=size,
        block_bytes=block,
        sample_a_pct=23,
        sample_b_pct=78,
    )
    span = size - block
    assert offsets == sorted({0, span, round(span * 0.23), round(span * 0.78)})


def test_compare_group_files_label_overflow_reaches_aa(tmp_path: Path) -> None:
    files: list[ExactMatchFile] = []
    for cluster in range(31):
        payload = bytes([cluster]) * 512
        a_path = tmp_path / f"c{cluster:02d}_a.mp4"
        b_path = tmp_path / f"c{cluster:02d}_b.mp4"
        a_path.write_bytes(payload)
        b_path.write_bytes(payload)
        files.append(ExactMatchFile(file_id=(cluster * 2) + 1, path=str(a_path), size=len(payload)))
        files.append(ExactMatchFile(file_id=(cluster * 2) + 2, path=str(b_path), size=len(payload)))

    result = compare_group_files(files=files, block_mib=1, sample_a_pct=23, sample_b_pct=78)
    labels = set(result.labels.values())
    assert "●" in labels
    assert "■" in labels
    assert "♥" in labels
    assert "♦" in labels
    assert "a" in labels
    assert "z" in labels
    assert "aa" in labels
