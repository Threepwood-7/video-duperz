"""Duplicate matching heuristics and group-building helpers."""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from statistics import median

from .fingerprint import (
    hamming_distance,
    inner_median_distance,
    normalized_median_distance,
)
from .models import (
    CrossResolutionMode,
    DuplicateEdge,
    DuplicateGroup,
    DuplicateItem,
    MatchItem,
    MatchReason,
    MatchStats,
    utc_now_iso,
)
from .quality import choose_keep_file_id

PROFILE_THRESHOLD = {
    "aggressive": 0.24,
    "balanced": 0.18,
    "conservative": 0.12,
}
_DURATION_BUCKET_WINDOW_S = 5.0
_INNER_ONLY_THRESHOLD_S = 3.0
_INNER_MODE_THRESHOLD_FACTOR = 0.90
_SAME_ASPECT_RATIO_DELTA = 0.5


def _resolve_similarity_threshold(
    profile: str,
    *,
    custom_similarity_threshold: float,
) -> float:
    """Resolve one effective similarity threshold from profile settings."""
    if str(profile).strip().lower() == "custom":
        return max(0.01, min(0.30, float(custom_similarity_threshold)))
    return PROFILE_THRESHOLD.get(profile, PROFILE_THRESHOLD["balanced"])


def _audio_fingerprint_match(a: MatchItem, b: MatchItem) -> bool:
    """Return whether two items share a usable audio fingerprint."""
    return bool(
        a.audio_fingerprint
        and b.audio_fingerprint
        and a.audio_fingerprint == b.audio_fingerprint
    )


def aspect_bin(width: int, height: int) -> float:
    """Bucket aspect ratios into coarse bins for candidate generation."""
    if height <= 0:
        return 0.0
    return round((width / height) * 4) / 4.0


def duration_bucket(duration_s: float) -> int:
    """Bucket durations into coarse five-second windows for candidate lookup."""
    return round(max(0.0, float(duration_s)) / _DURATION_BUCKET_WINDOW_S)


def _prefilter_hamming_median(a: list[int], b: list[int]) -> float:
    """Estimate pair similarity from start, middle, and end hashes."""
    length = min(len(a), len(b))
    if length <= 0:
        return 64.0
    indices = sorted({0, length // 2, length - 1})
    distances = [hamming_distance(a[idx], b[idx]) for idx in indices]
    return float(median(distances)) if distances else 64.0


def _prefilter_hamming_inner(a: list[int], b: list[int]) -> float:
    """Estimate pair similarity from the stable inner hash window only."""
    inner_a = a[2:10]
    inner_b = b[2:10]
    length = min(len(inner_a), len(inner_b))
    if length <= 0:
        return 64.0
    indices = sorted({0, length // 2, length - 1})
    distances = [hamming_distance(inner_a[idx], inner_b[idx]) for idx in indices]
    return float(median(distances)) if distances else 64.0


def _is_candidate(
    a: MatchItem,
    b: MatchItem,
    *,
    duration_tolerance_s: float,
    cross_resolution_mode: CrossResolutionMode,
) -> bool:
    """Return whether one pair should advance to perceptual hash comparison."""
    if abs(a.duration_s - b.duration_s) > duration_tolerance_s:
        return False
    shortest = min(a.duration_s, b.duration_s)
    longest = max(a.duration_s, b.duration_s)
    if longest <= 0:
        return False
    ratio_floor = max(0.70, 1.0 - (duration_tolerance_s / max(1.0, longest)))
    if shortest / longest < ratio_floor:
        return False
    ra = a.width / a.height if a.height else 0.0
    rb = b.width / b.height if b.height else 0.0
    if ra <= 0 or rb <= 0:
        return False
    if cross_resolution_mode == "any_aspect":
        return True
    ratio_delta = abs(math.log2(ra / rb))
    if cross_resolution_mode == "same_aspect":
        return ratio_delta <= _SAME_ASPECT_RATIO_DELTA
    return ratio_delta <= 0.2


def _neighbor_items(
    buckets: dict[tuple[int, float], list[MatchItem]],
    *,
    duration_bin: int,
    aspect_ratio_bin: float,
    duration_tolerance_s: float,
    cross_resolution_mode: CrossResolutionMode,
) -> list[MatchItem]:
    """Collect items from the current and adjacent duration buckets."""
    extra_buckets = max(
        0,
        math.ceil(duration_tolerance_s / _DURATION_BUCKET_WINDOW_S) - 1,
    )
    seen_file_ids: set[int] = set()
    result: list[MatchItem] = []
    for offset in range(-extra_buckets, extra_buckets + 1):
        bucket_duration = duration_bin + offset
        if cross_resolution_mode == "off":
            aspect_bins: set[float] = {aspect_ratio_bin}
        else:
            aspect_bins = {
                bucket_aspect
                for candidate_duration, bucket_aspect in buckets
                if candidate_duration == bucket_duration
            }
        for candidate_aspect_bin in aspect_bins:
            for item in buckets.get(
                (bucket_duration, candidate_aspect_bin),
                [],
            ):
                if item.file_id in seen_file_ids:
                    continue
                seen_file_ids.add(item.file_id)
                result.append(item)
    return result


def find_duplicate_edges(
    items: list[MatchItem],
    profile: str = "balanced",
    *,
    custom_similarity_threshold: float = 0.18,
    duration_tolerance_s: float = 8.0,
    cross_resolution_mode: CrossResolutionMode = "off",
) -> tuple[list[DuplicateEdge], MatchStats]:
    """Find likely duplicate pairs by bucketing and comparing match items."""
    threshold = _resolve_similarity_threshold(
        profile,
        custom_similarity_threshold=custom_similarity_threshold,
    )
    buckets: dict[tuple[int, float], list[MatchItem]] = defaultdict(list)
    for item in items:
        buckets[
            (
                duration_bucket(item.duration_s),
                aspect_bin(item.width, item.height),
            )
        ].append(item)

    stats = MatchStats(total_items=len(items), bucket_count=len(buckets))
    edges: list[DuplicateEdge] = []
    seen_pairs: set[tuple[int, int]] = set()
    for (duration_bin, aspect_ratio_bin), _bucket_items in buckets.items():
        bucket_items = _neighbor_items(
            buckets,
            duration_bin=duration_bin,
            aspect_ratio_bin=aspect_ratio_bin,
            duration_tolerance_s=duration_tolerance_s,
            cross_resolution_mode=cross_resolution_mode,
        )
        if len(bucket_items) < 2:
            continue
        for a, b in itertools.combinations(bucket_items, 2):
            pair_key = (
                min(int(a.file_id), int(b.file_id)),
                max(int(a.file_id), int(b.file_id)),
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            stats.candidate_pairs += 1
            if not _is_candidate(
                a,
                b,
                duration_tolerance_s=duration_tolerance_s,
                cross_resolution_mode=cross_resolution_mode,
            ):
                continue
            duration_gap_s = abs(a.duration_s - b.duration_s)
            use_inner = duration_gap_s > _INNER_ONLY_THRESHOLD_S
            stats.prefilter_pairs += 1
            if use_inner:
                stats.inner_mode_pairs += 1
            prefilter_distance = (
                _prefilter_hamming_inner(a.hashes, b.hashes)
                if use_inner
                else _prefilter_hamming_median(a.hashes, b.hashes)
            )
            if prefilter_distance > 22.0:
                stats.prefilter_rejected_pairs += 1
                if _audio_fingerprint_match(a, b):
                    edges.append(
                        DuplicateEdge(
                            file_a=a.file_id,
                            file_b=b.file_id,
                            score=1.0,
                            match_reason="audio_match",
                        )
                    )
                    stats.accepted_pairs += 1
                continue
            stats.full_distance_pairs += 1
            distance = (
                inner_median_distance(a.hashes, b.hashes)
                if use_inner
                else normalized_median_distance(a.hashes, b.hashes)
            )
            effective_threshold = (
                threshold * _INNER_MODE_THRESHOLD_FACTOR if use_inner else threshold
            )
            if distance <= effective_threshold:
                edges.append(
                    DuplicateEdge(
                        file_a=a.file_id,
                        file_b=b.file_id,
                        score=1.0 - distance,
                        match_reason="trimmed_match" if use_inner else "perceptual",
                    )
                )
                if use_inner:
                    stats.inner_mode_accepted += 1
                stats.accepted_pairs += 1
                continue
            if _audio_fingerprint_match(a, b):
                edges.append(
                    DuplicateEdge(
                        file_a=a.file_id,
                        file_b=b.file_id,
                        score=1.0,
                        match_reason="audio_match",
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


def _collect_component_item_metrics(
    *,
    item: MatchItem,
    component_ids: list[int],
    score_map: dict[tuple[int, int], float],
    reason_map: dict[tuple[int, int], MatchReason],
    by_id: dict[int, MatchItem],
) -> tuple[float, MatchReason, float]:
    """Compute one item's similarity and persisted match metadata."""
    pair_scores: list[float] = []
    item_match_reason: MatchReason = "perceptual"
    item_match_duration_delta_s = 0.0
    for other in component_ids:
        if other == item.file_id:
            continue
        score = score_map.get((item.file_id, other))
        if score is not None:
            pair_scores.append(score)
        pair_reason = reason_map.get((item.file_id, other))
        if pair_reason == "trimmed_match":
            other_item = by_id.get(other)
            if other_item is None:
                continue
            duration_delta_s = abs(item.duration_s - other_item.duration_s)
            if item_match_reason != "trimmed_match":
                item_match_reason = "trimmed_match"
                item_match_duration_delta_s = duration_delta_s
                continue
            item_match_duration_delta_s = min(
                item_match_duration_delta_s,
                duration_delta_s,
            )
            continue
        if pair_reason == "audio_match" and item_match_reason == "perceptual":
            item_match_reason = "audio_match"
    similarity = sum(pair_scores) / len(pair_scores) if pair_scores else 1.0
    return (similarity, item_match_reason, item_match_duration_delta_s)


def _build_duplicate_group_item(
    *,
    item: MatchItem,
    similarity: float,
    keep_id: int,
    match_reason: MatchReason,
    match_duration_delta_s: float,
) -> DuplicateItem:
    """Build one UI-ready duplicate item from one match item."""
    is_keep = item.file_id == keep_id
    return DuplicateItem(
        file_id=item.file_id,
        path=item.path,
        size=item.size,
        mtime_ns=item.mtime_ns,
        ctime_ns=item.ctime_ns,
        duration_s=item.duration_s,
        width=item.width,
        height=item.height,
        fps=item.fps,
        bit_depth=item.bit_depth,
        hdr_format=item.hdr_format,
        container=item.container,
        codec_profile=item.codec_profile,
        codec_level=item.codec_level,
        is_interlaced=item.is_interlaced,
        bitrate=item.bitrate,
        codec=item.codec,
        audio_stream_count=item.audio_stream_count,
        audio_codec=item.audio_codec,
        audio_bitrate=item.audio_bitrate,
        audio_languages=item.audio_languages,
        subtitle_languages=item.subtitle_languages,
        similarity_score=similarity,
        keep_default=is_keep,
        match_reason=match_reason,
        match_duration_delta_s=match_duration_delta_s,
        selected_action="keep" if is_keep else "rename",
    )


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
    reason_map: dict[tuple[int, int], MatchReason] = {}
    for edge in edges:
        score_map[(edge.file_a, edge.file_b)] = edge.score
        score_map[(edge.file_b, edge.file_a)] = edge.score
        reason_map[(edge.file_a, edge.file_b)] = edge.match_reason
        reason_map[(edge.file_b, edge.file_a)] = edge.match_reason

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
            similarity, item_match_reason, item_match_duration_delta_s = (
                _collect_component_item_metrics(
                    item=item,
                    component_ids=comp_ids,
                    score_map=score_map,
                    reason_map=reason_map,
                    by_id=by_id,
                )
            )
            group_items.append(
                _build_duplicate_group_item(
                    item=item,
                    similarity=similarity,
                    keep_id=keep_id,
                    match_reason=item_match_reason,
                    match_duration_delta_s=item_match_duration_delta_s,
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
