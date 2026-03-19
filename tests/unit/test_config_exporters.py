from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings

from video_duperz.config import (
    MAX_DRIVE_WORKERS,
    RESULTS_TABLE_COLUMN_COUNT,
    SCAN_LANE_TABLE_COLUMN_COUNT,
    SCAN_LOG_TABLE_COLUMN_COUNT,
    SOURCES_DRIVE_TABLE_COLUMN_COUNT,
    default_settings,
    load_settings,
    save_settings,
    settings_path,
)
from video_duperz.constants import SETTINGS_APP_NAME, SETTINGS_ORG_NAME
from video_duperz.db import Database
from video_duperz.exporters import export_scan
from video_duperz.models import (
    DuplicateItem,
    SavedScanProfilePayload,
    ScanLinkRecord,
    VideoMeta,
)


def _set_qsettings_value(path: Path, key: str, value: object) -> None:
    qs = QSettings(str(path), QSettings.Format.IniFormat)
    if isinstance(value, (list, dict)):
        qs.setValue(key, json.dumps(value))
    else:
        qs.setValue(key, value)
    qs.sync()


def test_settings_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    settings.scan_roots = ["D:/Videos"]
    settings.recent_scan_roots = ["D:/Videos", "E:/Archive"]
    settings.thumbnail_size = "128x72"
    settings.scan_size_mib_min = 75
    settings.scan_size_mib_max = 640
    settings.custom_similarity_threshold = 0.21
    settings.duration_tolerance_s = 9.5
    settings.scene_aware_sampling = True
    settings.audio_fingerprint_enabled = True
    settings.cross_resolution_mode = "same_aspect"
    settings.thumbnail_frame_a_pct = 25
    settings.thumbnail_frame_b_pct = 75
    settings.identical_block_mib = 4
    settings.identical_sample_a_pct = 12
    settings.identical_sample_b_pct = 91
    settings.sources_drive_table_column_widths = [
        110
    ] * SOURCES_DRIVE_TABLE_COLUMN_COUNT
    settings.scan_lane_table_column_widths = [90] * SCAN_LANE_TABLE_COLUMN_COUNT
    settings.scan_log_table_column_widths = [120] * SCAN_LOG_TABLE_COLUMN_COUNT
    settings.results_table_column_widths = [80] * RESULTS_TABLE_COLUMN_COUNT
    settings.drive_worker_overrides = {"volume:a": 3}
    settings.probe_backend = "pyav"
    settings.probe_worker_mode = "burst"
    settings.ffmpeg_exe_path = "~/bin/ffmpeg.exe"
    settings.ffprobe_exe_path = "~/bin/ffprobe.exe"
    settings.fpcalc_exe_path = "~/bin/fpcalc.exe"
    settings.mediainfo_exe_path = "~/bin/mediainfo.exe"
    settings.everything_exe_path = "~/bin/Everything.exe"
    settings.custom_command_f2 = '"C:/Tools/F2 Runner.exe" --flag'
    settings.custom_command_f3 = '"C:/Tools/F3 Runner.exe"'
    settings.custom_command_f4 = ""
    settings.scan_parent_cpu_priority = "below_normal"
    settings.scan_parent_io_mode = "background"
    settings.scan_child_cpu_priority = "high"
    settings.scan_child_io_mode = "background"
    settings.scan_db_batch_size = 2048
    settings.scan_db_flush_interval_ms = 450
    settings.scan_enum_queue_max = 8192
    settings.scan_progress_emit_interval_ms = 500
    settings.scan_progress_emit_every_files = 300
    settings.saved_scan_profiles = {
        "My Set": SavedScanProfilePayload(
            scan_set_key='{"audio_fingerprint_enabled":true,"cross_resolution_mode":"same_aspect","custom_similarity_threshold":0.21,"extensions":["mp4"],"roots":["d:/videos"],"scene_aware_sampling":true,"similarity_profile":"custom"}',
            roots=["D:/Videos"],
            similarity_profile="custom",
            extensions=["mp4"],
            custom_similarity_threshold=0.21,
            scene_aware_sampling=True,
            audio_fingerprint_enabled=True,
            cross_resolution_mode="same_aspect",
            updated_at="2026-01-01T12:00:00+00:00",
        )
    }
    save_settings(settings)
    loaded = load_settings()
    assert loaded.scan_roots == ["D:/Videos"]
    assert loaded.recent_scan_roots == ["D:/Videos", "E:/Archive"]
    assert "mp4" in loaded.extensions
    assert loaded.thumbnail_size == "128x72"
    assert loaded.scan_size_mib_min == 75
    assert loaded.scan_size_mib_max == 640
    assert loaded.custom_similarity_threshold == 0.21
    assert loaded.duration_tolerance_s == 9.5
    assert loaded.scene_aware_sampling is True
    assert loaded.audio_fingerprint_enabled is True
    assert loaded.cross_resolution_mode == "same_aspect"
    assert loaded.thumbnail_frame_a_pct == 25
    assert loaded.thumbnail_frame_b_pct == 75
    assert loaded.identical_block_mib == 4
    assert loaded.identical_sample_a_pct == 12
    assert loaded.identical_sample_b_pct == 91
    assert (
        loaded.sources_drive_table_column_widths
        == [110] * SOURCES_DRIVE_TABLE_COLUMN_COUNT
    )
    assert loaded.scan_lane_table_column_widths == [90] * SCAN_LANE_TABLE_COLUMN_COUNT
    assert loaded.scan_log_table_column_widths == [120] * SCAN_LOG_TABLE_COLUMN_COUNT
    assert loaded.results_table_column_widths == [80] * RESULTS_TABLE_COLUMN_COUNT
    assert loaded.drive_worker_overrides == {"volume:a": 3}
    assert loaded.probe_backend == "pyav"
    assert loaded.probe_worker_mode == "burst"
    assert loaded.ffmpeg_exe_path == str(Path("~/bin/ffmpeg.exe").expanduser())
    assert loaded.ffprobe_exe_path == str(Path("~/bin/ffprobe.exe").expanduser())
    assert loaded.fpcalc_exe_path == str(Path("~/bin/fpcalc.exe").expanduser())
    assert loaded.mediainfo_exe_path == str(Path("~/bin/mediainfo.exe").expanduser())
    assert loaded.everything_exe_path == str(Path("~/bin/Everything.exe").expanduser())
    assert loaded.custom_command_f2 == '"C:/Tools/F2 Runner.exe" --flag'
    assert loaded.custom_command_f3 == '"C:/Tools/F3 Runner.exe"'
    assert loaded.custom_command_f4 == ""
    assert loaded.scan_parent_cpu_priority == "below_normal"
    assert loaded.scan_parent_io_mode == "background"
    assert loaded.scan_child_cpu_priority == "high"
    assert loaded.scan_child_io_mode == "background"
    assert loaded.scan_db_batch_size == 2048
    assert loaded.scan_db_flush_interval_ms == 450
    assert loaded.scan_enum_queue_max == 8192
    assert loaded.scan_progress_emit_interval_ms == 500
    assert loaded.scan_progress_emit_every_files == 300
    assert "My Set" in loaded.saved_scan_profiles
    assert loaded.saved_scan_profiles["My Set"].roots == [str(Path("D:/Videos"))]
    assert loaded.saved_scan_profiles["My Set"].similarity_profile == "custom"
    assert loaded.saved_scan_profiles["My Set"].custom_similarity_threshold == 0.21
    assert loaded.saved_scan_profiles["My Set"].scene_aware_sampling is True
    assert loaded.saved_scan_profiles["My Set"].audio_fingerprint_enabled is True
    assert (
        loaded.saved_scan_profiles["My Set"].cross_resolution_mode
        == "same_aspect"
    )


def test_settings_path_uses_app_name_ini_under_appdata(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = settings_path()
    assert path.parent == tmp_path / SETTINGS_ORG_NAME
    assert path.name == f"{SETTINGS_APP_NAME}.ini"


def test_settings_invalid_thumbnail_size_falls_back_to_default(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "thumbnail_size", "999x999")

    loaded = load_settings()
    assert loaded.thumbnail_size == "96x54"


def test_settings_invalid_scan_size_filters_fall_back_to_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "scan_size_mib_min", "bogus")
    _set_qsettings_value(path, "scan_size_mib_max", "bogus")

    loaded = load_settings()
    assert loaded.scan_size_mib_min == 50
    assert loaded.scan_size_mib_max == 0


def test_settings_duration_tolerance_is_clamped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "duration_tolerance_s", 99.0)
    loaded_high = load_settings()
    assert loaded_high.duration_tolerance_s == 30.0

    _set_qsettings_value(path, "duration_tolerance_s", -5.0)
    loaded_low = load_settings()
    assert loaded_low.duration_tolerance_s == 0.0


def test_settings_custom_similarity_threshold_is_clamped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "custom_similarity_threshold", 9.0)
    loaded_high = load_settings()
    assert loaded_high.custom_similarity_threshold == 0.30

    _set_qsettings_value(path, "custom_similarity_threshold", -1.0)
    loaded_low = load_settings()
    assert loaded_low.custom_similarity_threshold == 0.01


def test_settings_recent_roots_are_normalized(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(
        path, "recent_scan_roots", ["  D:/Videos ", "", "d:/videos", "E:/Archive"]
    )

    loaded = load_settings()
    assert loaded.recent_scan_roots == ["D:/Videos", "E:/Archive"]


def test_settings_invalid_column_widths_fall_back_to_empty(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(
        path, "results_table_column_widths", [100, -2, 80, 90, 70, 60, 50, 40]
    )

    loaded = load_settings()
    assert loaded.results_table_column_widths == []


def test_settings_invalid_scan_lane_column_widths_fall_back_to_empty(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "scan_lane_table_column_widths", [100, -2, 80])

    loaded = load_settings()
    assert loaded.scan_lane_table_column_widths == []


def test_settings_invalid_scan_log_column_widths_fall_back_to_empty(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "scan_log_table_column_widths", [100, -2, 80])

    loaded = load_settings()
    assert loaded.scan_log_table_column_widths == []


def test_settings_invalid_sources_drive_column_widths_fall_back_to_empty(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "sources_drive_table_column_widths", [100, -2, 80])

    loaded = load_settings()
    assert loaded.sources_drive_table_column_widths == []


def test_settings_thumbnail_frame_pair_normalization(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "thumbnail_frame_a_pct", 300)
    _set_qsettings_value(path, "thumbnail_frame_b_pct", 300)

    loaded = load_settings()
    assert loaded.thumbnail_frame_a_pct == 100
    assert loaded.thumbnail_frame_b_pct == 99


def test_settings_identical_compare_normalization(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "identical_block_mib", 0)
    _set_qsettings_value(path, "identical_sample_a_pct", 100)
    _set_qsettings_value(path, "identical_sample_b_pct", 100)

    loaded = load_settings()
    assert loaded.identical_block_mib == 1
    assert loaded.identical_sample_a_pct == 99
    assert loaded.identical_sample_b_pct == 100

    _set_qsettings_value(path, "identical_block_mib", 1000)
    _set_qsettings_value(path, "identical_sample_a_pct", 80)
    _set_qsettings_value(path, "identical_sample_b_pct", 10)

    loaded_swapped = load_settings()
    assert loaded_swapped.identical_block_mib == 64
    assert loaded_swapped.identical_sample_a_pct == 10
    assert loaded_swapped.identical_sample_b_pct == 80


def test_settings_old_column_payloads_are_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(
        path,
        "results_table_column_widths",
        [80] * (RESULTS_TABLE_COLUMN_COUNT - 1),
    )
    _set_qsettings_value(
        path,
        "results_table_column_visibility",
        [False] * (RESULTS_TABLE_COLUMN_COUNT - 1),
    )

    loaded = load_settings()
    assert loaded.results_table_column_widths == []
    assert loaded.results_table_column_visibility == []


def test_settings_saved_views_normalization(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(
        path,
        "saved_column_views",
        {
            "good": {
                "widths": [80] * RESULTS_TABLE_COLUMN_COUNT,
                "visibility": [True] * RESULTS_TABLE_COLUMN_COUNT,
            },
            "bad_short": {
                "widths": [80] * 3,
                "visibility": [True] * RESULTS_TABLE_COLUMN_COUNT,
            },
            "bad_all_hidden": {
                "widths": [80] * RESULTS_TABLE_COLUMN_COUNT,
                "visibility": [False] * RESULTS_TABLE_COLUMN_COUNT,
            },
        },
    )

    loaded = load_settings()
    assert "good" in loaded.saved_column_views
    assert "bad_short" not in loaded.saved_column_views
    assert "bad_all_hidden" in loaded.saved_column_views
    assert all(loaded.saved_column_views["bad_all_hidden"]["visibility"])


def test_settings_saved_views_with_old_column_counts_are_ignored(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(
        path,
        "saved_column_views",
        {
            "legacy": {
                "widths": [90] * (RESULTS_TABLE_COLUMN_COUNT - 1),
                "visibility": [True] * (RESULTS_TABLE_COLUMN_COUNT - 1),
            }
        },
    )

    loaded = load_settings()
    assert "legacy" not in loaded.saved_column_views


def test_settings_saved_scan_profiles_normalization(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(
        path,
        "saved_scan_profiles",
        {
            "  Good  ": {
                "scan_set_key": "",
                "roots": [" D:/Videos ", "d:/videos"],
                "similarity_profile": "BALANCED",
                "extensions": ["MP4", ".mkv", "mp4"],
                "updated_at": "",
            },
            "": {
                "roots": ["D:/Videos"],
                "similarity_profile": "balanced",
                "extensions": ["mp4"],
            },
            "BadRootsType": {
                "roots": "D:/Videos",
                "similarity_profile": "balanced",
                "extensions": ["mp4"],
            },
        },
    )

    loaded = load_settings()
    assert "Good" in loaded.saved_scan_profiles
    profile = loaded.saved_scan_profiles["Good"]
    assert profile.roots == [str(Path("D:/Videos"))]
    assert profile.similarity_profile == "balanced"
    assert profile.extensions == ["mp4", "mkv"]
    assert profile.scan_set_key
    assert "BadRootsType" not in loaded.saved_scan_profiles


def test_settings_drive_worker_overrides_and_probe_mode_normalization(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(
        path,
        "drive_worker_overrides",
        {
            " volume:a ": 0,
            "": 5,
            "volume:b": "oops",
            "volume:c": MAX_DRIVE_WORKERS + 999,
        },
    )
    _set_qsettings_value(path, "probe_backend", "INVALID")
    _set_qsettings_value(path, "probe_worker_mode", "INVALID")
    _set_qsettings_value(path, "ffmpeg_exe_path", "  ~/tools/ffmpeg.exe  ")
    _set_qsettings_value(path, "ffprobe_exe_path", "")
    _set_qsettings_value(path, "mediainfo_exe_path", "  ")

    loaded = load_settings()
    assert loaded.drive_worker_overrides == {
        "volume:a": 1,
        "volume:c": MAX_DRIVE_WORKERS,
    }
    assert loaded.probe_backend == "pyav"
    assert loaded.probe_worker_mode == "balanced"
    assert loaded.ffmpeg_exe_path == str(Path("~/tools/ffmpeg.exe").expanduser())
    assert loaded.ffprobe_exe_path == ""
    assert loaded.mediainfo_exe_path == ""

    _set_qsettings_value(path, "probe_backend", "pyav")
    _set_qsettings_value(path, "probe_worker_mode", "burst")
    loaded_burst = load_settings()
    assert loaded_burst.probe_backend == "pyav"
    assert loaded_burst.probe_worker_mode == "burst"


def test_settings_scan_tuning_normalization(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = default_settings()
    save_settings(settings)

    path = settings_path()
    _set_qsettings_value(path, "scan_db_batch_size", 1)
    _set_qsettings_value(path, "scan_db_flush_interval_ms", 99999)
    _set_qsettings_value(path, "scan_enum_queue_max", 10)
    _set_qsettings_value(path, "scan_progress_emit_interval_ms", 0)
    _set_qsettings_value(path, "scan_progress_emit_every_files", "oops")

    loaded = load_settings()
    assert loaded.scan_db_batch_size == 32
    assert loaded.scan_db_flush_interval_ms == 2000
    assert loaded.scan_enum_queue_max == 256
    assert loaded.scan_progress_emit_interval_ms == 50
    assert (
        loaded.scan_progress_emit_every_files == settings.scan_progress_emit_every_files
    )


def test_scan_links_roundtrip(tmp_path: Path) -> None:
    db_file = tmp_path / "app.db"
    with Database(db_file) as db:
        scan_id = db.create_scan(profile="balanced", roots=[str(tmp_path)])
        db.upsert_scan_links_batch(
            [
                ScanLinkRecord(
                    scan_id=scan_id,
                    link_kind="symlink",
                    link_path=str(tmp_path / "shortcut.mp4"),
                    target_original_path=str(tmp_path / "real.mp4"),
                    target_exists=True,
                    source_root=str(tmp_path),
                ),
                ScanLinkRecord(
                    scan_id=scan_id,
                    link_kind="hardlink",
                    link_path=str(tmp_path / "copy.mp4"),
                    target_original_path=str(tmp_path / "real.mp4"),
                    target_exists=True,
                    source_root=str(tmp_path),
                ),
            ]
        )

        links = db.load_scan_links(scan_id)

    assert [link.link_kind for link in links] == ["hardlink", "symlink"]
    assert links[0].target_original_path == str(tmp_path / "real.mp4")


def test_export_scan_outputs_duplicates_and_links(tmp_path: Path) -> None:
    db_file = tmp_path / "app.db"
    with Database(db_file) as db:
        scan_id = db.create_scan(profile="balanced", roots=[str(tmp_path)])
        file_id = db.upsert_file(
            path=str(tmp_path / "a.mp4"),
            size=123,
            mtime_ns=1,
            ctime_ns=1,
            ext="mp4",
            scan_id=scan_id,
        )
        file_id_2 = db.upsert_file(
            path=str(tmp_path / "b.mp4"),
            size=124,
            mtime_ns=2,
            ctime_ns=2,
            ext="mp4",
            scan_id=scan_id,
        )
        db.save_video_meta(
            file_id,
            VideoMeta(
                duration_s=10.0,
                width=1920,
                height=1080,
                fps=30.0,
                codec="h264",
                bitrate=1000,
                has_audio=True,
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="eng",
                is_hdr=False,
            ),
        )
        db.save_video_meta(
            file_id_2,
            VideoMeta(
                duration_s=10.1,
                width=1920,
                height=1080,
                fps=30.0,
                codec="h264",
                bitrate=900,
                has_audio=True,
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="eng",
                is_hdr=False,
            ),
        )
        db.save_fingerprint(file_id=file_id, algo_version=1, hashes=[1] * 12)
        db.save_fingerprint(file_id=file_id_2, algo_version=1, hashes=[1] * 12)

        group_id = db.insert_duplicate_group(
            scan_id=scan_id, profile="balanced", total_size_bytes=123
        )
        db.insert_duplicate_item(
            group_id,
            DuplicateItem(
                file_id=file_id,
                path=str(tmp_path / "a.mp4"),
                size=123,
                mtime_ns=1,
                ctime_ns=1,
                duration_s=10.0,
                width=1920,
                height=1080,
                bitrate=1000,
                codec="h264",
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="eng",
                is_hdr=False,
                similarity_score=1.0,
                keep_default=True,
                selected_action="keep",
            ),
        )
        db.insert_duplicate_item(
            group_id,
            DuplicateItem(
                file_id=file_id_2,
                path=str(tmp_path / "b.mp4"),
                size=124,
                mtime_ns=2,
                ctime_ns=2,
                duration_s=10.1,
                width=1920,
                height=1080,
                bitrate=900,
                codec="h264",
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="eng",
                is_hdr=False,
                similarity_score=0.99,
                keep_default=False,
                selected_action="rename",
            ),
        )
        db.upsert_scan_links_batch(
            [
                ScanLinkRecord(
                    scan_id=scan_id,
                    link_kind="symlink",
                    link_path=str(tmp_path / "symlink-a.mp4"),
                    target_original_path=str(tmp_path / "a.mp4"),
                    target_exists=True,
                    source_root=str(tmp_path),
                )
            ]
        )

        out_dir = tmp_path / "out"
        export_paths = export_scan(db, scan_id=scan_id, out_dir=out_dir)
        assert export_paths.duplicates_csv.exists()
        assert export_paths.duplicates_json.exists()
        assert export_paths.links_csv.exists()
        assert export_paths.links_json.exists()

        data = json.loads(export_paths.duplicates_json.read_text(encoding="utf-8"))
        assert data["scan_id"] == scan_id
        assert data["profile"] == "balanced"
        assert len(data["groups"]) == 1
        links_data = json.loads(export_paths.links_json.read_text(encoding="utf-8"))
        assert links_data["scan_id"] == scan_id
        assert len(links_data["links"]) == 1
        assert links_data["links"][0]["link_kind"] == "symlink"
        assert Path(links_data["links"][0]["target_original_path"]).name == "a.mp4"
