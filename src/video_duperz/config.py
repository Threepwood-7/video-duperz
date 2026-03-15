"""Settings defaults, normalization helpers, and QSettings persistence."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import cast

from PySide6.QtCore import QSettings
from threep_commons.paths import resolve_app_data_dir
from threep_commons.settings import QSettingsValueStore

from .config_video_presets import COMMON_VIDEO_EXTENSIONS
from .constants import APP_IDENTITY, SETTINGS_APP_NAME
from .models import (
    DEFAULT_THUMBNAIL_SIZE,
    THUMBNAIL_SIZE_CHOICES,
    KeepRule,
    ProbeBackendId,
    ProbeWorkerMode,
    SavedScanProfilePayload,
    Settings,
    utc_now_iso,
)
from .scan_sets import (
    build_scan_set_key,
    normalize_extensions,
    normalize_roots_for_display,
    normalize_similarity_profile,
)

RESULTS_TABLE_COLUMN_COUNT = 19
MAX_RECENT_ROOTS = 20
MAX_SAVED_SCAN_PROFILES = 200
SETTINGS_FILE_NAME = f"{SETTINGS_APP_NAME}.ini"
MAX_DRIVE_WORKERS = 64
PROBE_WORKER_MODES: tuple[str, str] = ("balanced", "burst")
PROBE_BACKEND_IDS: tuple[str, str] = ("ffprobe", "pyav")
DEFAULT_SCAN_DB_BATCH_SIZE = 512
DEFAULT_SCAN_DB_FLUSH_INTERVAL_MS = 200
DEFAULT_SCAN_ENUM_QUEUE_MAX = 4096
DEFAULT_SCAN_PROGRESS_EMIT_INTERVAL_MS = 200
DEFAULT_SCAN_PROGRESS_EMIT_EVERY_FILES = 100


def _object_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast("list[object]", value)
    return []


def _object_dict(value: object) -> dict[object, object]:
    if isinstance(value, dict):
        return cast("dict[object, object]", value)
    return {}


def _string_list(value: object) -> list[str]:
    normalized: list[str] = []
    for item in _object_list(value):
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_keep_rule(value: object, default: KeepRule = "best_quality") -> KeepRule:
    if str(value).strip().lower() == "best_quality":
        return "best_quality"
    return default


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _normalize_column_widths(value: object, expected_count: int) -> list[int]:
    raw_values = _object_list(value)
    if len(raw_values) != expected_count:
        return []
    widths: list[int] = []
    for raw in raw_values:
        width = _coerce_int(raw, -1)
        if width < 0:
            return []
        widths.append(width)
    return widths


def _normalize_column_visibility(value: object, expected_count: int) -> list[bool]:
    raw_values = _object_list(value)
    if len(raw_values) != expected_count:
        return []
    visibility: list[bool] = []
    for raw in raw_values:
        visibility.append(bool(raw))
    if not any(visibility):
        # Ensure at least one visible column.
        return [True] * expected_count
    return visibility


def _normalize_saved_column_views(
    value: object, expected_count: int
) -> dict[str, dict[str, list[int] | list[bool]]]:
    normalized: dict[str, dict[str, list[int] | list[bool]]] = {}
    for raw_name, raw_payload in _object_dict(value).items():
        name = str(raw_name).strip()
        if not name or len(name) > 80:
            continue
        payload_map = _object_dict(raw_payload)
        if not payload_map:
            continue
        widths = _normalize_column_widths(payload_map.get("widths"), expected_count)
        visibility = _normalize_column_visibility(
            payload_map.get("visibility"), expected_count
        )
        if not widths or not visibility:
            continue
        normalized[name] = {"widths": widths, "visibility": visibility}
    return normalized


def _normalize_recent_roots(value: object, limit: int = MAX_RECENT_ROOTS) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for text in _string_list(value):
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _normalize_saved_scan_profiles(value: object) -> dict[str, SavedScanProfilePayload]:
    normalized: dict[str, SavedScanProfilePayload] = {}
    for raw_name, raw_payload in _object_dict(value).items():
        name = str(raw_name).strip()
        if not name or len(name) > 80:
            continue
        payload_map = _object_dict(raw_payload)
        if not payload_map:
            continue
        roots_raw = _string_list(payload_map.get("roots", []))
        if not roots_raw:
            continue
        roots = normalize_roots_for_display(roots_raw)
        if not roots:
            continue
        profile = normalize_similarity_profile(
            str(payload_map.get("similarity_profile", "balanced"))
        )
        ext_raw = _string_list(payload_map.get("extensions", []))
        extensions = normalize_extensions(ext_raw)
        scan_set_key = str(payload_map.get("scan_set_key", "")).strip()
        if not scan_set_key:
            scan_set_key = build_scan_set_key(
                roots=roots, similarity_profile=profile, extensions=extensions
            )
        updated_at = str(payload_map.get("updated_at", "")).strip() or utc_now_iso()
        normalized[name] = SavedScanProfilePayload(
            scan_set_key=scan_set_key,
            roots=roots,
            similarity_profile=profile,
            extensions=extensions,
            updated_at=updated_at,
        )
        if len(normalized) >= MAX_SAVED_SCAN_PROFILES:
            break
    return normalized


def _normalize_percent(value: object, default: int) -> int:
    parsed = _coerce_int(value, default)
    return max(0, min(100, parsed))


def _normalize_frame_pair(
    a_value: object, b_value: object, default_a: int, default_b: int
) -> tuple[int, int]:
    a = _normalize_percent(a_value, default_a)
    b = _normalize_percent(b_value, default_b)
    if a == b:
        b = b + 1 if b < 100 else b - 1
    return a, b


def _normalize_identical_block_mib(value: object, default: int = 1) -> int:
    parsed = _coerce_int(value, default)
    return max(1, min(64, parsed))


def _normalize_identical_sample_pair(
    a_value: object, b_value: object, default_a: int, default_b: int
) -> tuple[int, int]:
    a = _normalize_percent(a_value, default_a)
    b = _normalize_percent(b_value, default_b)
    if a == b:
        if b < 100:
            b += 1
        else:
            a = max(0, a - 1)
    if a > b:
        a, b = b, a
    return a, b


def _normalize_drive_worker_overrides(
    value: object, max_workers: int = MAX_DRIVE_WORKERS
) -> dict[str, int]:
    normalized: dict[str, int] = {}
    limit = max(1, int(max_workers))
    for raw_key, raw_value in _object_dict(value).items():
        key = str(raw_key).strip()
        if not key:
            continue
        if isinstance(raw_value, bool):
            workers = int(raw_value)
        elif isinstance(raw_value, int):
            workers = raw_value
        elif isinstance(raw_value, str):
            workers_text = raw_value.strip()
            if not workers_text:
                continue
            try:
                workers = int(workers_text)
            except ValueError:
                continue
        else:
            continue
        normalized[key] = max(1, min(limit, workers))
    return normalized


def _normalize_probe_worker_mode(
    value: object, default: ProbeWorkerMode = "balanced"
) -> ProbeWorkerMode:
    text = str(value or "").strip().lower()
    if text in PROBE_WORKER_MODES:
        return cast("ProbeWorkerMode", text)
    return default


def _normalize_probe_backend(
    value: object, default: ProbeBackendId = "pyav"
) -> ProbeBackendId:
    text = str(value or "").strip().lower()
    if text in PROBE_BACKEND_IDS:
        return cast("ProbeBackendId", text)
    return default


def _normalize_int_range(
    value: object, default: int, minimum: int, maximum: int
) -> int:
    parsed = _coerce_int(value, default)
    return max(minimum, min(maximum, parsed))


def default_max_workers() -> int:
    """Choose a conservative default worker count for desktop scans."""
    cpus = os.cpu_count() or 4
    return min(6, max(2, cpus - 1))


def app_data_dir() -> Path:
    """Return the resolved application data directory for the current user."""
    return resolve_app_data_dir(APP_IDENTITY)


def settings_path() -> Path:
    """Return the backing INI path used by the QSettings store."""
    settings = _settings_store()
    settings.sync()
    return Path(settings.file_name())


def db_path() -> Path:
    """Return the SQLite database path inside the app data directory."""
    return app_data_dir() / "app.db"


def _settings_store(path: Path | None = None) -> QSettingsValueStore:
    if path is not None:
        return QSettingsValueStore(QSettings(str(path), QSettings.Format.IniFormat))
    return QSettingsValueStore.from_identity(APP_IDENTITY)


def _decode_json_value(value: object, fallback: object) -> object:
    if value is None:
        return fallback
    if isinstance(value, list):
        return cast("list[object]", value)
    if isinstance(value, dict):
        return cast("dict[object, object]", value)
    text = str(value).strip()
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _read_qsettings_payload(
    qs: QSettingsValueStore, defaults: Settings
) -> dict[str, object]:
    payload: dict[str, object] = {
        "scan_roots": _decode_json_value(qs.value("scan_roots"), defaults.scan_roots),
        "recent_scan_roots": _decode_json_value(
            qs.value("recent_scan_roots"), defaults.recent_scan_roots
        ),
        "extensions": _decode_json_value(qs.value("extensions"), defaults.extensions),
        "similarity_profile": qs.value(
            "similarity_profile", defaults.similarity_profile
        ),
        "max_workers": qs.value("max_workers", defaults.max_workers),
        "preview_autoplay": _coerce_bool(
            qs.value("preview_autoplay"), defaults.preview_autoplay
        ),
        "thumbnail_size": qs.value("thumbnail_size", defaults.thumbnail_size),
        "thumbnail_frame_a_pct": qs.value(
            "thumbnail_frame_a_pct", defaults.thumbnail_frame_a_pct
        ),
        "thumbnail_frame_b_pct": qs.value(
            "thumbnail_frame_b_pct", defaults.thumbnail_frame_b_pct
        ),
        "identical_block_mib": qs.value(
            "identical_block_mib", defaults.identical_block_mib
        ),
        "identical_sample_a_pct": qs.value(
            "identical_sample_a_pct", defaults.identical_sample_a_pct
        ),
        "identical_sample_b_pct": qs.value(
            "identical_sample_b_pct", defaults.identical_sample_b_pct
        ),
        "results_table_column_widths": _decode_json_value(
            qs.value("results_table_column_widths"),
            defaults.results_table_column_widths,
        ),
        "results_table_column_visibility": _decode_json_value(
            qs.value("results_table_column_visibility"),
            defaults.results_table_column_visibility,
        ),
        "saved_column_views": _decode_json_value(
            qs.value("saved_column_views"), defaults.saved_column_views
        ),
        "saved_scan_profiles": _decode_json_value(
            qs.value("saved_scan_profiles"), defaults.saved_scan_profiles
        ),
        "keep_rule": qs.value("keep_rule", defaults.keep_rule),
        "drive_worker_overrides": _decode_json_value(
            qs.value("drive_worker_overrides"),
            defaults.drive_worker_overrides,
        ),
        "probe_backend": qs.value("probe_backend", defaults.probe_backend),
        "probe_worker_mode": qs.value("probe_worker_mode", defaults.probe_worker_mode),
        "scan_db_batch_size": qs.value(
            "scan_db_batch_size", defaults.scan_db_batch_size
        ),
        "scan_db_flush_interval_ms": qs.value(
            "scan_db_flush_interval_ms", defaults.scan_db_flush_interval_ms
        ),
        "scan_enum_queue_max": qs.value(
            "scan_enum_queue_max", defaults.scan_enum_queue_max
        ),
        "scan_progress_emit_interval_ms": qs.value(
            "scan_progress_emit_interval_ms",
            defaults.scan_progress_emit_interval_ms,
        ),
        "scan_progress_emit_every_files": qs.value(
            "scan_progress_emit_every_files",
            defaults.scan_progress_emit_every_files,
        ),
    }
    return payload


def _settings_from_raw(raw: dict[str, object], defaults: Settings) -> Settings:
    frame_a, frame_b = _normalize_frame_pair(
        raw.get("thumbnail_frame_a_pct", defaults.thumbnail_frame_a_pct),
        raw.get("thumbnail_frame_b_pct", defaults.thumbnail_frame_b_pct),
        defaults.thumbnail_frame_a_pct,
        defaults.thumbnail_frame_b_pct,
    )
    identical_a, identical_b = _normalize_identical_sample_pair(
        raw.get("identical_sample_a_pct", defaults.identical_sample_a_pct),
        raw.get("identical_sample_b_pct", defaults.identical_sample_b_pct),
        defaults.identical_sample_a_pct,
        defaults.identical_sample_b_pct,
    )
    settings = Settings(
        scan_roots=_string_list(raw.get("scan_roots", defaults.scan_roots)),
        recent_scan_roots=_normalize_recent_roots(
            raw.get("recent_scan_roots", defaults.recent_scan_roots)
        ),
        extensions=_string_list(raw.get("extensions", defaults.extensions)),
        similarity_profile=normalize_similarity_profile(
            str(raw.get("similarity_profile", defaults.similarity_profile))
        ),
        max_workers=_coerce_int(raw.get("max_workers", defaults.max_workers), 0),
        preview_autoplay=_coerce_bool(
            raw.get("preview_autoplay", defaults.preview_autoplay),
            defaults.preview_autoplay,
        ),
        thumbnail_size=normalize_thumbnail_size(
            str(raw.get("thumbnail_size", defaults.thumbnail_size))
        ),
        thumbnail_frame_a_pct=frame_a,
        thumbnail_frame_b_pct=frame_b,
        identical_block_mib=_normalize_identical_block_mib(
            raw.get("identical_block_mib", defaults.identical_block_mib),
            defaults.identical_block_mib,
        ),
        identical_sample_a_pct=identical_a,
        identical_sample_b_pct=identical_b,
        results_table_column_widths=_normalize_column_widths(
            raw.get("results_table_column_widths", []),
            expected_count=RESULTS_TABLE_COLUMN_COUNT,
        ),
        results_table_column_visibility=_normalize_column_visibility(
            raw.get("results_table_column_visibility", []),
            expected_count=RESULTS_TABLE_COLUMN_COUNT,
        ),
        saved_column_views=_normalize_saved_column_views(
            raw.get("saved_column_views", {}),
            expected_count=RESULTS_TABLE_COLUMN_COUNT,
        ),
        saved_scan_profiles=_normalize_saved_scan_profiles(
            raw.get("saved_scan_profiles", {})
        ),
        keep_rule=_normalize_keep_rule(raw.get("keep_rule", defaults.keep_rule)),
        drive_worker_overrides=_normalize_drive_worker_overrides(
            raw.get("drive_worker_overrides", defaults.drive_worker_overrides),
        ),
        probe_backend=_normalize_probe_backend(
            raw.get("probe_backend", defaults.probe_backend),
            default=defaults.probe_backend,
        ),
        probe_worker_mode=_normalize_probe_worker_mode(
            raw.get("probe_worker_mode", defaults.probe_worker_mode),
            default=defaults.probe_worker_mode,
        ),
        scan_db_batch_size=_normalize_int_range(
            raw.get("scan_db_batch_size", defaults.scan_db_batch_size),
            defaults.scan_db_batch_size,
            32,
            4096,
        ),
        scan_db_flush_interval_ms=_normalize_int_range(
            raw.get("scan_db_flush_interval_ms", defaults.scan_db_flush_interval_ms),
            defaults.scan_db_flush_interval_ms,
            50,
            2000,
        ),
        scan_enum_queue_max=_normalize_int_range(
            raw.get("scan_enum_queue_max", defaults.scan_enum_queue_max),
            defaults.scan_enum_queue_max,
            256,
            32768,
        ),
        scan_progress_emit_interval_ms=_normalize_int_range(
            raw.get(
                "scan_progress_emit_interval_ms",
                defaults.scan_progress_emit_interval_ms,
            ),
            defaults.scan_progress_emit_interval_ms,
            50,
            2000,
        ),
        scan_progress_emit_every_files=_normalize_int_range(
            raw.get(
                "scan_progress_emit_every_files",
                defaults.scan_progress_emit_every_files,
            ),
            defaults.scan_progress_emit_every_files,
            10,
            5000,
        ),
    )
    if settings.max_workers < 1:
        settings.max_workers = defaults.max_workers
    settings.thumbnail_size = normalize_thumbnail_size(settings.thumbnail_size)
    return settings


def normalize_thumbnail_size(value: str | None) -> str:
    """Normalize a thumbnail size key to one of the supported options."""
    if not value:
        return DEFAULT_THUMBNAIL_SIZE
    cleaned = str(value).strip().lower()
    if cleaned in THUMBNAIL_SIZE_CHOICES:
        return cleaned
    return DEFAULT_THUMBNAIL_SIZE


def default_settings() -> Settings:
    """Build the default in-memory settings payload for a first launch."""
    return Settings(
        scan_roots=[],
        recent_scan_roots=[],
        extensions=COMMON_VIDEO_EXTENSIONS.copy(),
        similarity_profile="balanced",
        max_workers=default_max_workers(),
        preview_autoplay=False,
        thumbnail_size=DEFAULT_THUMBNAIL_SIZE,
        thumbnail_frame_a_pct=23,
        thumbnail_frame_b_pct=77,
        identical_block_mib=1,
        identical_sample_a_pct=23,
        identical_sample_b_pct=78,
        results_table_column_widths=[],
        results_table_column_visibility=[],
        saved_column_views={},
        saved_scan_profiles={},
        keep_rule="best_quality",
        drive_worker_overrides={},
        probe_backend="pyav",
        probe_worker_mode="balanced",
        scan_db_batch_size=DEFAULT_SCAN_DB_BATCH_SIZE,
        scan_db_flush_interval_ms=DEFAULT_SCAN_DB_FLUSH_INTERVAL_MS,
        scan_enum_queue_max=DEFAULT_SCAN_ENUM_QUEUE_MAX,
        scan_progress_emit_interval_ms=DEFAULT_SCAN_PROGRESS_EMIT_INTERVAL_MS,
        scan_progress_emit_every_files=DEFAULT_SCAN_PROGRESS_EMIT_EVERY_FILES,
    )


def load_settings() -> Settings:
    """Load persisted settings, creating and saving defaults on first run."""
    defaults = default_settings()
    qs = _settings_store()
    if qs.qsettings.allKeys():
        raw = _read_qsettings_payload(qs, defaults)
        return _settings_from_raw(raw, defaults)

    settings = default_settings()
    save_settings(settings)
    return settings


def save_settings(settings: Settings) -> None:
    """Persist the provided settings object to the QSettings store."""
    payload = asdict(settings)
    qs = _settings_store()
    qs.clear_all()
    for key, value in payload.items():
        if isinstance(value, (list, dict)):
            qs.set_value(key, json.dumps(value))
        else:
            qs.set_value(key, value)
    qs.sync()
