"""Shared dataclasses and literal types used across the application."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

ActionKind = Literal["keep", "rename", "delete"]
SimilarityProfile = Literal["balanced", "conservative", "aggressive"]
KeepRule = Literal["best_quality"]
ProbeWorkerMode = Literal["balanced", "burst"]
ProbeBackendId = Literal["ffprobe", "pyav"]
FrameDecodeBackendId = Literal["opencv", "pyav", "ffmpeg"]
ScanWorkKind = Literal["cache_hit", "fingerprint_only", "probe_and_fingerprint"]
ScanLinkKind = Literal["symlink", "hardlink"]
ScanProcessCpuPriority = Literal[
    "idle",
    "below_normal",
    "normal",
    "above_normal",
    "high",
]
ScanProcessIoMode = Literal["normal", "background"]
THUMBNAIL_SIZE_CHOICES = ("80x45", "96x54", "128x72", "160x90")
DEFAULT_THUMBNAIL_SIZE = "96x54"


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class SavedScanProfilePayload:
    """Saved source/profile/extension selections for quickly reloading scans."""

    scan_set_key: str
    roots: list[str]
    similarity_profile: SimilarityProfile
    extensions: list[str]
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class Settings:
    """Persisted user settings for scan behavior, UI state, and runtime tuning."""

    scan_roots: list[str] = field(default_factory=list)
    recent_scan_roots: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    scan_size_mib_min: int = 50
    scan_size_mib_max: int = 0
    similarity_profile: SimilarityProfile = "balanced"
    max_workers: int = 2
    preview_autoplay: bool = False
    thumbnail_size: str = DEFAULT_THUMBNAIL_SIZE
    thumbnail_frame_a_pct: int = 23
    thumbnail_frame_b_pct: int = 77
    identical_block_mib: int = 1
    identical_sample_a_pct: int = 23
    identical_sample_b_pct: int = 78
    sources_drive_table_column_widths: list[int] = field(default_factory=list)
    scan_lane_table_column_widths: list[int] = field(default_factory=list)
    scan_log_table_column_widths: list[int] = field(default_factory=list)
    results_table_column_widths: list[int] = field(default_factory=list)
    results_table_column_visibility: list[bool] = field(default_factory=list)
    saved_column_views: dict[str, dict[str, list[int] | list[bool]]] = field(
        default_factory=dict
    )
    saved_scan_profiles: dict[str, SavedScanProfilePayload] = field(
        default_factory=dict
    )
    keep_rule: KeepRule = "best_quality"
    drive_worker_overrides: dict[str, int] = field(default_factory=dict)
    probe_backend: ProbeBackendId = "pyav"
    probe_worker_mode: ProbeWorkerMode = "balanced"
    ffmpeg_exe_path: str = ""
    ffprobe_exe_path: str = ""
    mediainfo_exe_path: str = ""
    everything_exe_path: str = ""
    custom_command_f2: str = ""
    custom_command_f3: str = ""
    custom_command_f4: str = ""
    scan_parent_cpu_priority: ScanProcessCpuPriority = "normal"
    scan_parent_io_mode: ScanProcessIoMode = "normal"
    scan_child_cpu_priority: ScanProcessCpuPriority = "normal"
    scan_child_io_mode: ScanProcessIoMode = "normal"
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
    """Discovered file-system record tracked during enumeration and scanning."""

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
class ScanLinkRecord:
    """Tracked filesystem link excluded from duplicate-candidate matching."""

    scan_id: int
    link_kind: ScanLinkKind
    link_path: str
    target_original_path: str
    target_exists: bool
    source_root: str = ""


@dataclass(slots=True)
class ScanEnumerationResult:
    """Completed enumeration payload with candidates, links, and issues."""

    files: list[VideoRecord] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)
    links: list[ScanLinkRecord] = field(default_factory=list)

    def __iter__(self) -> Iterator[list[VideoRecord] | list[ScanIssue]]:
        """Yield backward-compatible tuple-style values for older callers."""
        yield self.files
        yield self.issues


@dataclass(slots=True)
class VideoMeta:
    """Probe metadata captured for a scanned video file."""

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
    """Persisted fingerprint payload used during duplicate matching."""

    file_id: int
    algo_version: int
    frame_count: int
    hashes: list[int]
    created_at: str


@dataclass(slots=True)
class ScanIssue:
    """Non-fatal issue reported during scan preparation or execution."""

    stage: str
    path: str
    message: str


@dataclass(slots=True)
class ScanProgress:
    """Progress snapshot emitted while the scan pipeline advances."""

    stage: str
    current: int
    total: int
    message: str = ""
    subject_path: str = ""
    work_kind: ScanWorkKind | None = None
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
    completed_files: int | None = None
    total_work_files: int | None = None
    skipped_failed_files: int | None = None
    fingerprint_only_files: int | None = None
    probe_and_fingerprint_files: int | None = None
    lane_snapshots: list[ScanLaneSnapshot] | None = None


@dataclass(slots=True)
class ScanLaneSnapshot:
    """Per-lane telemetry snapshot emitted alongside scan progress updates."""

    lane: int
    roots: list[str] = field(default_factory=list)
    state: str = "pending"
    discovered: int = 0
    discovered_bytes: int = 0
    queued: int = 0
    analyzed: int = 0
    analyzed_bytes: int = 0
    completed: int = 0
    discovery_complete: bool = False
    cache_hits: int = 0
    fingerprint_only: int = 0
    probe_and_fingerprint: int = 0
    discovered_files_per_s: float = 0.0
    discovered_mib_per_s: float = 0.0
    analyzed_files_per_s: float = 0.0
    analyzed_mib_per_s: float = 0.0
    active_file: str = ""
    workers: int = 0


@dataclass(slots=True)
class DuplicateEdge:
    """Weighted duplicate relationship between two matched file ids."""

    file_a: int
    file_b: int
    score: float


@dataclass(slots=True)
class MatchStats:
    """Counters collected while candidate pairs are filtered and compared."""

    total_items: int = 0
    bucket_count: int = 0
    candidate_pairs: int = 0
    prefilter_pairs: int = 0
    prefilter_rejected_pairs: int = 0
    full_distance_pairs: int = 0
    accepted_pairs: int = 0


@dataclass(slots=True)
class MatchItem:
    """Normalized match input assembled from persisted metadata and fingerprints."""

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
    """UI-ready duplicate item enriched with similarity and chosen action state."""

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
    """A persisted set of duplicate items that belong to the same cluster."""

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
    """Requested action choice for a single duplicate item."""

    file_id: int
    action: ActionKind


@dataclass(slots=True)
class ActionItemResult:
    """Execution result for one file action within a batch run."""

    file_id: int
    source_path: str
    target_path: str | None
    result: str
    error_text: str | None = None


@dataclass(slots=True)
class ActionRunResult:
    """Recorded outcome of a batch rename/delete action run."""

    run_id: int
    scan_id: int
    mode: str
    status: str
    items: list[ActionItemResult]
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class ScanResult:
    """Final scan payload returned to the CLI and UI layers."""

    scan_id: int
    groups: list[DuplicateGroup]
    issues: list[ScanIssue]
    scanned_files: int
    cached_files: int
    fingerprinted_files: int
    metrics: dict[str, Any] = field(default_factory=dict)
