"""Duplicate matching heuristics and group-building helpers."""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from statistics import median

from .fingerprint import hamming_distance, normalized_median_distance
from .models import (
    DuplicateEdge,
    DuplicateGroup,
    DuplicateItem,
    MatchItem,
    MatchStats,
    utc_now_iso,
)
from .quality import choose_keep_file_id

PROFILE_THRESHOLD = {
    "aggressive": 0.24,
    "balanced": 0.18,
    "conservative": 0.12,
}


def aspect_bin(width: int, height: int) -> float:
    """Bucket aspect ratios into coarse bins for candidate generation."""
    if height <= 0:
        return 0.0
    return round((width / height) * 4) / 4.0


def resolution_bin(width: int, height: int) -> int:
    """Bucket resolutions by approximate quarter-megapixel increments."""
    pixels = max(0, int(width) * int(height))
    if pixels <= 0:
        return 0
    return round(pixels / 250_000)


def fps_bin(value: float) -> float:
    """Bucket frame rates into half-frame-per-second increments."""
    fps = float(value)
    if fps <= 0:
        return 0.0
    return round(fps * 2.0) / 2.0


def duration_half_sec(duration_s: float) -> int:
    """Bucket durations into half-second increments."""
    return round(max(0.0, float(duration_s)) * 2.0)


def _prefilter_hamming_median(a: list[int], b: list[int]) -> float:
    length = min(len(a), len(b))
    if length <= 0:
        return 64.0
    indices = sorted({0, length // 2, length - 1})
    distances = [hamming_distance(a[idx], b[idx]) for idx in indices]
    return float(median(distances)) if distances else 64.0


def _is_candidate(a: MatchItem, b: MatchItem) -> bool:
    if abs(a.duration_s - b.duration_s) > 2.0:
        return False
    shortest = min(a.duration_s, b.duration_s)
    longest = max(a.duration_s, b.duration_s)
    if longest <= 0:
        return False
    if shortest / longest < 0.96:
        return False
    ra = a.width / a.height if a.height else 0.0
    rb = b.width / b.height if b.height else 0.0
    if ra <= 0 or rb <= 0:
        return False
    ratio_delta = abs(math.log2(ra / rb))
    return ratio_delta <= 0.2


def find_duplicate_edges(
    items: list[MatchItem], profile: str = "balanced"
) -> tuple[list[DuplicateEdge], MatchStats]:
    """Find likely duplicate pairs by bucketing and comparing match items."""
    threshold = PROFILE_THRESHOLD.get(profile, PROFILE_THRESHOLD["balanced"])
    buckets: dict[tuple[int, float, int, float], list[MatchItem]] = defaultdict(list)
    for item in items:
        buckets[
            (
                duration_half_sec(item.duration_s),
                aspect_bin(item.width, item.height),
                resolution_bin(item.width, item.height),
                fps_bin(item.fps),
            )
        ].append(item)

    stats = MatchStats(total_items=len(items), bucket_count=len(buckets))
    edges: list[DuplicateEdge] = []
    for bucket_items in buckets.values():
        if len(bucket_items) < 2:
            continue
        for a, b in itertools.combinations(bucket_items, 2):
            stats.candidate_pairs += 1
            if not _is_candidate(a, b):
                continue
            stats.prefilter_pairs += 1
            if _prefilter_hamming_median(a.hashes, b.hashes) > 22.0:
                stats.prefilter_rejected_pairs += 1
                continue
            stats.full_distance_pairs += 1
            distance = normalized_median_distance(a.hashes, b.hashes)
            if distance <= threshold:
                edges.append(
                    DuplicateEdge(
                        file_a=a.file_id, file_b=b.file_id, score=1.0 - distance
                    )
                )
                stats.accepted_pairs += 1
    return edges, stats


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        if x not in self.parent:
            self.parent[x] = x
            return x
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_duplicate_groups(
    items: list[MatchItem], edges: list[DuplicateEdge], profile: str
) -> list[DuplicateGroup]:
    """Convert duplicate edges into persisted duplicate-group payloads."""
    by_id = {i.file_id: i for i in items}
    uf = _UnionFind()
    for edge in edges:
        uf.union(edge.file_a, edge.file_b)

    components: dict[int, list[int]] = defaultdict(list)
    for edge in edges:
        components[uf.find(edge.file_a)].append(edge.file_a)
        components[uf.find(edge.file_b)].append(edge.file_b)

    unique_components: list[list[int]] = []
    for ids in components.values():
        uniq = sorted(set(ids))
        if len(uniq) >= 2:
            unique_components.append(uniq)

    score_map: dict[tuple[int, int], float] = {}
    for edge in edges:
        score_map[(edge.file_a, edge.file_b)] = edge.score
        score_map[(edge.file_b, edge.file_a)] = edge.score

    groups: list[DuplicateGroup] = []
    created_at = utc_now_iso()
    for comp_ids in unique_components:
        comp_items = [by_id[file_id] for file_id in comp_ids if file_id in by_id]
        if len(comp_items) < 2:
            continue
        keep_id = choose_keep_file_id(comp_items)

        group_items: list[DuplicateItem] = []
        total_size = 0
        for item in comp_items:
            total_size += item.size
            pair_scores: list[float] = []
            for other in comp_ids:
                if other == item.file_id:
                    continue
                score = score_map.get((item.file_id, other))
                if score is not None:
                    pair_scores.append(score)
            similarity = sum(pair_scores) / len(pair_scores) if pair_scores else 1.0
            is_keep = item.file_id == keep_id
            group_items.append(
                DuplicateItem(
                    file_id=item.file_id,
                    path=item.path,
                    size=item.size,
                    mtime_ns=item.mtime_ns,
                    ctime_ns=item.ctime_ns,
                    duration_s=item.duration_s,
                    width=item.width,
                    height=item.height,
                    bitrate=item.bitrate,
                    codec=item.codec,
                    audio_codec=item.audio_codec,
                    audio_bitrate=item.audio_bitrate,
                    audio_languages=item.audio_languages,
                    subtitle_languages=item.subtitle_languages,
                    is_hdr=item.is_hdr,
                    similarity_score=similarity,
                    keep_default=is_keep,
                    selected_action="keep" if is_keep else "rename",
                )
            )
        groups.append(
            DuplicateGroup(
                scan_id=0,  # filled by caller when persisted/reloaded
                profile=profile,
                created_at=created_at,
                items=group_items,
                total_size_bytes=total_size,
            )
        )

    groups.sort(key=lambda g: (len(g.items), g.reclaimable_bytes), reverse=True)
    return groups
