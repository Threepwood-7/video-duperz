from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

ActionKind = Literal["keep", "rename", "delete"]
SimilarityProfile = Literal["balanced", "conservative", "aggressive"]
KeepRule = Literal["best_quality"]
THUMBNAIL_SIZE_CHOICES = ("80x45", "96x54", "128x72", "160x90")
DEFAULT_THUMBNAIL_SIZE = "96x54"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class SavedScanProfilePayload:
    scan_set_key: str
    roots: list[str]
    similarity_profile: str
    extensions: list[str]
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class Settings:
    scan_roots: list[str] = field(default_factory=list)
    recent_scan_roots: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    similarity_profile: SimilarityProfile = "balanced"
    max_workers: int = 2
    preview_autoplay: bool = False
    thumbnail_size: str = DEFAULT_THUMBNAIL_SIZE
    thumbnail_frame_a_pct: int = 23
    thumbnail_frame_b_pct: int = 77
    identical_block_mib: int = 1
    identical_sample_a_pct: int = 23
    identical_sample_b_pct: int = 78
    results_table_column_widths: list[int] = field(default_factory=list)
    results_table_column_visibility: list[bool] = field(default_factory=list)
    saved_column_views: dict[str, dict[str, list[int] | list[bool]]] = field(default_factory=dict)
    saved_scan_profiles: dict[str, SavedScanProfilePayload] = field(default_factory=dict)
    keep_rule: KeepRule = "best_quality"
    drive_worker_overrides: dict[str, int] = field(default_factory=dict)
    probe_worker_mode: Literal["balanced", "burst"] = "balanced"
    scan_db_batch_size: int = 512
    scan_db_flush_interval_ms: int = 200
    scan_enum_queue_max: int = 4096
    scan_progress_emit_interval_ms: int = 200
    scan_progress_emit_every_files: int = 100

    def normalized_extensions(self) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for ext in self.extensions:
            e = ext.lower().strip().lstrip(".")
            if not e or e in seen:
                continue
            seen.add(e)
            normalized.append(e)
        return normalized


@dataclass(slots=True)
class VideoRecord:
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    ext: str
    scan_id: int
    source_root: str = ""
    parallel_lane: int = 0
    file_id: int | None = None


@dataclass(slots=True)
class VideoMeta:
    duration_s: float
    width: int
    height: int
    fps: float
    codec: str
    bitrate: int
    has_audio: bool
    audio_codec: str
    audio_bitrate: int
    audio_languages: str
    subtitle_languages: str
    is_hdr: bool


@dataclass(slots=True)
class FingerprintRecord:
    file_id: int
    algo_version: int
    frame_count: int
    hashes: list[int]
    created_at: str


@dataclass(slots=True)
class ScanIssue:
    stage: str
    path: str
    message: str


@dataclass(slots=True)
class ScanProgress:
    stage: str
    current: int
    total: int
    message: str = ""
    active_workers: int | None = None
    worker_limit: int | None = None
    enumerated_roots: int | None = None
    total_roots: int | None = None
    prepared_files: int | None = None
    discovered_files: int | None = None
    discovered_bytes: int | None = None
    analyzed_files: int | None = None
    analyzed_bytes: int | None = None
    cached_files: int | None = None
    discovered_files_per_s: float | None = None
    discovered_mib_per_s: float | None = None
    analyzed_files_per_s: float | None = None
    analyzed_mib_per_s: float | None = None
    cache_hit_ratio: float | None = None
    elapsed_s: float | None = None
    total_analyze_files: int | None = None
    lane_snapshots: list[ScanLaneSnapshot] | None = None


@dataclass(slots=True)
class ScanLaneSnapshot:
    lane: int
    roots: list[str] = field(default_factory=list)
    state: str = "pending"
    discovered: int = 0
    discovered_bytes: int = 0
    queued: int = 0
    analyzed: int = 0
    analyzed_bytes: int = 0
    completed: int = 0
    discovered_files_per_s: float = 0.0
    discovered_mib_per_s: float = 0.0
    analyzed_files_per_s: float = 0.0
    analyzed_mib_per_s: float = 0.0
    active_file: str = ""
    workers: int = 0


@dataclass(slots=True)
class DuplicateEdge:
    file_a: int
    file_b: int
    score: float


@dataclass(slots=True)
class MatchStats:
    total_items: int = 0
    bucket_count: int = 0
    candidate_pairs: int = 0
    prefilter_pairs: int = 0
    prefilter_rejected_pairs: int = 0
    full_distance_pairs: int = 0
    accepted_pairs: int = 0


@dataclass(slots=True)
class MatchItem:
    file_id: int
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    duration_s: float
    width: int
    height: int
    fps: float
    codec: str
    bitrate: int
    audio_codec: str
    audio_bitrate: int
    audio_languages: str
    subtitle_languages: str
    is_hdr: bool
    hashes: list[int]


@dataclass(slots=True)
class DuplicateItem:
    file_id: int
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    duration_s: float
    width: int
    height: int
    bitrate: int
    codec: str
    audio_codec: str
    audio_bitrate: int
    audio_languages: str
    subtitle_languages: str
    is_hdr: bool
    similarity_score: float
    keep_default: bool
    selected_action: ActionKind = "keep"


@dataclass(slots=True)
class DuplicateGroup:
    scan_id: int
    profile: str
    created_at: str
    items: list[DuplicateItem]
    total_size_bytes: int
    group_id: int | None = None

    @property
    def average_confidence(self) -> float:
        if not self.items:
            return 0.0
        return sum(i.similarity_score for i in self.items) / len(self.items)

    @property
    def reclaimable_bytes(self) -> int:
        keep_ids = {i.file_id for i in self.items if i.keep_default}
        keep_size = sum(i.size for i in self.items if i.file_id in keep_ids)
        return max(0, self.total_size_bytes - keep_size)


@dataclass(slots=True)
class ActionSelection:
    file_id: int
    action: ActionKind


@dataclass(slots=True)
class ActionItemResult:
    file_id: int
    source_path: str
    target_path: str | None
    result: str
    error_text: str | None = None


@dataclass(slots=True)
class ActionRunResult:
    run_id: int
    scan_id: int
    mode: str
    status: str
    items: list[ActionItemResult]
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class ScanResult:
    scan_id: int
    groups: list[DuplicateGroup]
    issues: list[ScanIssue]
    scanned_files: int
    cached_files: int
    fingerprinted_files: int
    metrics: dict[str, Any] = field(default_factory=dict)
