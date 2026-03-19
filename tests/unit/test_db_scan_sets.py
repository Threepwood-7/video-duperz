from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from video_duperz.db import Database
from video_duperz.fingerprint import ALGO_VERSION
from video_duperz.models import DuplicateGroup, DuplicateItem, ScanIssue, VideoMeta
from video_duperz.scan_sets import build_scan_set_key

if TYPE_CHECKING:
    from pathlib import Path


def test_db_migration_resets_legacy_schema_to_snapshot_model(tmp_path: Path) -> None:
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE scans(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          profile TEXT NOT NULL,
          roots_json TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE files(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          path TEXT NOT NULL UNIQUE,
          size INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          ctime_ns INTEGER NOT NULL,
          ext TEXT NOT NULL,
          scan_id INTEGER NOT NULL
        );
        INSERT INTO scans(created_at, profile, roots_json, status)
        VALUES('2026-01-01T10:00:00+00:00', 'balanced', '["D:/Videos"]', 'done');
        PRAGMA user_version = 2;
        """
    )
    conn.commit()
    conn.close()

    with Database(db_file) as db:
        indexes = db.conn.execute("PRAGMA index_list(files)").fetchall()
        unique_indexes = [row for row in indexes if int(row["unique"]) == 1]

        assert db.latest_scan_id() is None
        assert "idx_files_scan_exists" in {str(row["name"]) for row in indexes}
        assert len(unique_indexes) == 1
        unique_columns = [
            str(row["name"])
            for row in db.conn.execute(
                f"PRAGMA index_info({unique_indexes[0]['name']})"
            ).fetchall()
        ]
        assert unique_columns == ["scan_id", "path"]
        assert int(db.conn.execute("PRAGMA user_version").fetchone()[0]) >= 15


def test_latest_scan_queries_by_scan_set() -> None:
    with Database(":memory:") as db:
        roots = ["D:/Videos"]
        ext = ["mp4", "mkv"]
        balanced_key = build_scan_set_key(
            roots=roots, similarity_profile="balanced", extensions=ext
        )
        aggressive_key = build_scan_set_key(
            roots=roots, similarity_profile="aggressive", extensions=ext
        )
        conservative_key = build_scan_set_key(
            roots=roots, similarity_profile="conservative", extensions=ext
        )

        balanced_done_a = db.create_scan(
            profile="balanced", roots=roots, extensions=ext
        )
        db.complete_scan(balanced_done_a, status="done")

        db.complete_scan(
            db.create_scan(profile="balanced", roots=roots, extensions=ext),
            status="cancelled",
        )

        balanced_done_b = db.create_scan(
            profile="balanced", roots=roots, extensions=ext
        )
        db.complete_scan(balanced_done_b, status="done")

        aggressive_done = db.create_scan(
            profile="aggressive", roots=roots, extensions=ext
        )
        db.complete_scan(aggressive_done, status="done")

        aggressive_cancelled = db.create_scan(
            profile="aggressive", roots=roots, extensions=ext
        )
        db.complete_scan(aggressive_cancelled, status="cancelled")

        conservative_running = db.create_scan(
            profile="conservative", roots=roots, extensions=ext
        )

        assert db.latest_scan_id_for_set(balanced_key) == balanced_done_b
        assert db.latest_scan_id_for_set(aggressive_key) == aggressive_cancelled
        assert db.latest_scan_id_for_set(conservative_key) == conservative_running

        assert db.latest_completed_scan_id_for_set(balanced_key) == balanced_done_b
        assert db.latest_completed_scan_id_for_set(aggressive_key) == aggressive_done
        assert db.latest_completed_scan_id_for_set(conservative_key) is None

        latest_by_set = db.list_latest_scans_by_set()
        by_key = {str(item["scan_set_key"]): item for item in latest_by_set}
        assert int(by_key[balanced_key]["scan_id"]) == balanced_done_b
        assert str(by_key[balanced_key]["status"]) == "done"
        assert int(by_key[aggressive_key]["scan_id"]) == aggressive_cancelled
        assert str(by_key[aggressive_key]["status"]) == "cancelled"
        assert int(by_key[conservative_key]["scan_id"]) == conservative_running
        assert str(by_key[conservative_key]["status"]) == "running"

        latest_completed_by_set = db.list_latest_completed_scans_by_set()
        completed_by_key = {
            str(item["scan_set_key"]): item for item in latest_completed_by_set
        }
        assert int(completed_by_key[balanced_key]["scan_id"]) == balanced_done_b
        assert int(completed_by_key[aggressive_key]["scan_id"]) == aggressive_done
        assert conservative_key not in completed_by_key

        summary = db.scan_summary(balanced_done_b)
        assert summary["scan_id"] == balanced_done_b
        assert summary["status"] == "done"
        assert "created_at" in summary
        assert "profile" in summary
        assert isinstance(summary["group_count"], int)
        assert isinstance(summary["file_count"], int)


def test_list_match_items_for_scan_orders_by_path() -> None:
    with Database(":memory:") as db:
        scan_id = db.create_scan(
            profile="balanced", roots=["D:/Videos"], extensions=["mp4"]
        )
        file_b = db.upsert_file(
            path="D:/Videos/b.mp4",
            size=2,
            mtime_ns=2,
            ctime_ns=2,
            ext="mp4",
            scan_id=scan_id,
        )
        file_a = db.upsert_file(
            path="D:/Videos/a.mp4",
            size=1,
            mtime_ns=1,
            ctime_ns=1,
            ext="mp4",
            scan_id=scan_id,
        )
        meta = VideoMeta(
            duration_s=1.0,
            width=320,
            height=240,
            fps=24.0,
            codec="h264",
            bitrate=1000,
            has_audio=True,
            audio_codec="aac",
            audio_bitrate=128000,
            audio_languages="eng",
            subtitle_languages="",
            hdr_format="",
        )
        db.save_video_meta(file_b, meta)
        db.save_video_meta(file_a, meta)
        db.save_fingerprint(file_b, algo_version=ALGO_VERSION, hashes=[1, 2, 3])
        db.save_fingerprint(file_a, algo_version=ALGO_VERSION, hashes=[4, 5, 6])

        items = db.list_match_items_for_scan(scan_id=scan_id, algo_version=ALGO_VERSION)

        assert [item.path for item in items] == ["D:/Videos/a.mp4", "D:/Videos/b.mp4"]


def test_scan_issue_rows_persist_and_list_for_paused_scans() -> None:
    with Database(":memory:") as db:
        scan_id = db.create_scan(
            profile="balanced",
            roots=["D:/Videos"],
            extensions=["mp4"],
        )
        db.insert_scan_issue(
            scan_id,
            ScanIssue(
                stage="probe",
                path="D:/Videos/bad.mp4",
                message="invalid stream metadata",
            ),
        )
        db.insert_scan_issue(
            scan_id,
            ScanIssue(stage="enumerate", path="", message="worker cap reduced"),
        )
        db.complete_scan(scan_id, status="paused")

        issues = db.list_scan_issues(scan_id)
        summary = db.scan_summary(scan_id)

        assert summary["status"] == "paused"
        assert [issue.stage for issue in issues] == ["probe", "enumerate"]
        assert issues[0].path == "D:/Videos/bad.mp4"
        assert issues[1].message == "worker cap reduced"


def test_scan_info_roundtrips_new_detection_settings() -> None:
    with Database(":memory:") as db:
        scan_id = db.create_scan(
            profile="custom",
            roots=["D:/Videos"],
            extensions=["mp4"],
            custom_similarity_threshold=0.22,
            scene_aware_sampling=True,
            audio_fingerprint_enabled=True,
            cross_resolution_mode="same_aspect",
            probe_backend="ffprobe",
        )

        scan_info = db.get_scan_info(scan_id)

        assert scan_info["profile"] == "custom"
        assert scan_info["custom_similarity_threshold"] == 0.22
        assert scan_info["scene_aware_sampling"] is True
        assert scan_info["audio_fingerprint_enabled"] is True
        assert scan_info["cross_resolution_mode"] == "same_aspect"
        assert scan_info["probe_backend"] == "ffprobe"


def test_cached_artifacts_can_include_audio_fingerprints() -> None:
    with Database(":memory:") as db:
        scan_id = db.create_scan(
            profile="balanced",
            roots=["D:/Videos"],
            extensions=["mp4"],
        )
        file_id = db.upsert_file(
            path="D:/Videos/a.mp4",
            size=123,
            mtime_ns=456,
            ctime_ns=456,
            ext="mp4",
            scan_id=scan_id,
        )
        db.save_video_meta(
            file_id,
            VideoMeta(
                duration_s=10.0,
                width=320,
                height=240,
                fps=24.0,
                codec="h264",
                bitrate=1000,
                has_audio=True,
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="",
                hdr_format="",
            ),
        )
        db.save_fingerprint(file_id, algo_version=ALGO_VERSION, hashes=[1, 2, 3])
        db.save_audio_fingerprints_batch([(file_id, 123, 456, "audio:abc")])

        cached = db.load_cached_artifacts_batch(
            [{"path": "D:/Videos/a.mp4", "size": 123, "mtime_ns": 456}],
            algo_version=ALGO_VERSION,
            include_audio_fingerprint=True,
        )

        assert cached["D:/Videos/a.mp4"]["audio_fingerprint"] == "audio:abc"


def test_failed_file_rows_persist_clear_and_ignore_non_file_issues() -> None:
    with Database(":memory:") as db:
        scan_id = db.create_scan(
            profile="balanced",
            roots=["D:/Videos"],
            extensions=["mp4"],
        )

        db.upsert_failed_file(
            scan_id,
            ScanIssue(
                stage="probe",
                path="D:/Videos/bad.mp4",
                message="invalid stream metadata",
            ),
        )
        db.upsert_failed_file(
            scan_id,
            ScanIssue(
                stage="fingerprint",
                path="d:/videos/BAD.mp4",
                message="decoder timeout",
            ),
        )
        db.upsert_failed_file(
            scan_id,
            ScanIssue(stage="enumerate", path="", message="worker cap reduced"),
        )

        failed_rows = db.list_failed_files(scan_id)

        assert db.count_failed_files(scan_id) == 1
        assert len(failed_rows) == 1
        assert len(db.failed_file_path_keys(scan_id)) == 1
        assert failed_rows[0]["normalized_path"]
        assert failed_rows[0]["display_path"] == "d:/videos/BAD.mp4"
        assert failed_rows[0]["stage"] == "fingerprint"
        assert failed_rows[0]["message"] == "decoder timeout"

        db.clear_failed_file(scan_id, "D:/Videos/bad.mp4")

        assert db.count_failed_files(scan_id) == 0
        assert db.list_failed_files(scan_id) == []


def test_purge_for_fresh_rescan_deletes_scan_set_and_selected_root_artifacts() -> None:
    with Database(":memory:") as db:
        roots_a = ["D:/Videos"]
        ext = ["mp4"]
        key_a = build_scan_set_key(
            roots=roots_a, similarity_profile="balanced", extensions=ext
        )

        scan_a1 = db.create_scan(profile="balanced", roots=roots_a, extensions=ext)
        db.upsert_file(
            path="D:/Videos/a1.mp4",
            size=1,
            mtime_ns=1,
            ctime_ns=1,
            ext="mp4",
            scan_id=scan_a1,
        )
        db.insert_duplicate_group(
            scan_id=scan_a1, profile="balanced", total_size_bytes=1
        )
        db.insert_action_run(scan_id=scan_a1, mode="rename", status="done")
        db.complete_scan(scan_a1, status="done")

        scan_a2 = db.create_scan(profile="balanced", roots=roots_a, extensions=ext)
        db.upsert_file(
            path="D:/Videos/a2.mp4",
            size=2,
            mtime_ns=2,
            ctime_ns=2,
            ext="mp4",
            scan_id=scan_a2,
        )
        db.insert_duplicate_group(
            scan_id=scan_a2, profile="balanced", total_size_bytes=2
        )
        db.insert_action_run(scan_id=scan_a2, mode="rename", status="done")
        db.complete_scan(scan_a2, status="done")

        scan_root_match = db.create_scan(
            profile="aggressive", roots=["Z:/Other"], extensions=ext
        )
        db.upsert_file(
            path="D:/Videos/strict.mp4",
            size=3,
            mtime_ns=3,
            ctime_ns=3,
            ext="mp4",
            scan_id=scan_root_match,
        )
        db.insert_duplicate_group(
            scan_id=scan_root_match, profile="aggressive", total_size_bytes=3
        )
        db.insert_action_run(scan_id=scan_root_match, mode="rename", status="done")
        db.complete_scan(scan_root_match, status="done")

        scan_keep = db.create_scan(
            profile="balanced", roots=["E:/Archive"], extensions=ext
        )
        db.upsert_file(
            path="E:/Archive/keep.mp4",
            size=4,
            mtime_ns=4,
            ctime_ns=4,
            ext="mp4",
            scan_id=scan_keep,
        )
        db.insert_duplicate_group(
            scan_id=scan_keep, profile="balanced", total_size_bytes=4
        )
        db.insert_action_run(scan_id=scan_keep, mode="rename", status="done")
        db.complete_scan(scan_keep, status="done")

        counts = db.purge_for_fresh_rescan(scan_set_key=key_a, roots=["d:\\videos\\"])

        assert counts["deleted_scans"] == 3
        assert counts["deleted_files"] == 3
        assert counts["deleted_groups"] == 3
        assert counts["deleted_actions"] == 3
        assert db.latest_scan_id_for_set(key_a) is None
        assert db.scan_summary(scan_keep)["scan_id"] == scan_keep
        remaining_paths = [
            str(row["path"])
            for row in db.conn.execute(
                "SELECT path FROM files ORDER BY path"
            ).fetchall()
        ]
        assert remaining_paths == ["E:/Archive/keep.mp4"]


def test_scan_batch_methods_roundtrip() -> None:
    with Database(":memory:") as db:
        scan_id = db.create_scan(
            profile="balanced", roots=["D:/Videos"], extensions=["mp4"]
        )
        db.begin_scan_transaction()
        by_path = db.upsert_files_batch(
            [
                {
                    "path": "D:/Videos/a.mp4",
                    "size": 10,
                    "mtime_ns": 11,
                    "ctime_ns": 11,
                    "ext": "mp4",
                    "scan_id": scan_id,
                },
                {
                    "path": "D:/Videos/b.mp4",
                    "size": 20,
                    "mtime_ns": 22,
                    "ctime_ns": 22,
                    "ext": "mp4",
                    "scan_id": scan_id,
                },
            ]
        )
        file_a = int(by_path["D:/Videos/a.mp4"])
        file_b = int(by_path["D:/Videos/b.mp4"])

        db.save_video_meta_batch(
            [
                (
                    file_a,
                    10,
                    11,
                    VideoMeta(
                        duration_s=12.0,
                        width=1920,
                        height=1080,
                        fps=30.0,
                        codec="h264",
                        bitrate=1000,
                        has_audio=True,
                        audio_codec="aac",
                        audio_bitrate=128000,
                        audio_languages="eng",
                        subtitle_languages="",
                        hdr_format="",
                    ),
                )
            ]
        )
        db.save_fingerprints_batch([(file_a, 10, 11, ALGO_VERSION, [1, 2, 3, 4])])
        db.save_fingerprint_provenance_batch(
            [(file_a, ALGO_VERSION, "pyav", '{"decoder_backend":"pyav"}')]
        )
        db.save_probe_errors_batch([(file_b, 20, 22, "probe failed")])

        group = DuplicateGroup(
            scan_id=scan_id,
            profile="balanced",
            created_at="2026-01-01T00:00:00+00:00",
            total_size_bytes=30,
            items=[
                DuplicateItem(
                    file_id=file_a,
                    path="D:/Videos/a.mp4",
                    size=10,
                    mtime_ns=11,
                    ctime_ns=11,
                    duration_s=12.0,
                    width=1920,
                    height=1080,
                    bitrate=1000,
                    codec="h264",
                    audio_codec="aac",
                    audio_bitrate=128000,
                    audio_languages="eng",
                    subtitle_languages="",
                    hdr_format="",
                    similarity_score=1.0,
                    keep_default=True,
                    match_reason="trimmed_match",
                    match_duration_delta_s=5.0,
                ),
                DuplicateItem(
                    file_id=file_b,
                    path="D:/Videos/b.mp4",
                    size=20,
                    mtime_ns=22,
                    ctime_ns=22,
                    duration_s=0.0,
                    width=0,
                    height=0,
                    bitrate=0,
                    codec="",
                    audio_codec="",
                    audio_bitrate=0,
                    audio_languages="",
                    subtitle_languages="",
                    hdr_format="",
                    similarity_score=0.8,
                    keep_default=False,
                    selected_action="rename",
                ),
            ],
        )
        inserted_ids = db.insert_duplicate_groups_batch(
            scan_id=scan_id, profile="balanced", groups=[group]
        )
        db.flush_scan_transaction()
        db.end_scan_transaction()

        assert len(inserted_ids) == 1
        cached = db.load_cached_artifacts_batch(
            [
                {"path": "D:/Videos/a.mp4", "size": 10, "mtime_ns": 11},
                {"path": "D:/Videos/b.mp4", "size": 20, "mtime_ns": 22},
            ],
            algo_version=ALGO_VERSION,
        )
        assert "D:/Videos/a.mp4" in cached
        assert "meta" in cached["D:/Videos/a.mp4"]
        assert cached["D:/Videos/a.mp4"]["meta_probed_at"]
        assert "fingerprint" in cached["D:/Videos/a.mp4"]
        assert cached["D:/Videos/a.mp4"]["fingerprint_created_at"]
        assert cached["D:/Videos/a.mp4"]["fingerprint"]["algo_version"] == ALGO_VERSION
        provenance_row = db.conn.execute(
            """
            SELECT decoder_backend, attempts_json
            FROM fingerprint_decoder_provenance
            WHERE file_id = ? AND probe_backend = 'pyav'
            """,
            (file_a,),
        ).fetchone()
        assert provenance_row is not None
        assert str(provenance_row["decoder_backend"]) == "pyav"
        assert str(provenance_row["attempts_json"]) == '{"decoder_backend":"pyav"}'
        groups = db.load_duplicate_groups(scan_id)
        assert len(groups) == 1
        assert groups[0].items[0].match_reason == "trimmed_match"
        assert groups[0].items[0].match_duration_delta_s == 5.0
        assert groups[0].items[1].match_reason == "perceptual"
        assert groups[0].items[1].match_duration_delta_s == 0.0


def test_files_are_snapshot_scoped_per_scan() -> None:
    with Database(":memory:") as db:
        scan_a = db.create_scan(
            profile="balanced",
            roots=["D:/Videos"],
            extensions=["mp4"],
        )
        scan_b = db.create_scan(
            profile="balanced",
            roots=["D:/Videos"],
            extensions=["mp4"],
        )

        file_a = db.upsert_file(
            path="D:/Videos/a.mp4",
            size=10,
            mtime_ns=11,
            ctime_ns=11,
            ext="mp4",
            scan_id=scan_a,
        )
        file_b = db.upsert_file(
            path="D:/Videos/a.mp4",
            size=10,
            mtime_ns=11,
            ctime_ns=11,
            ext="mp4",
            scan_id=scan_b,
        )

        assert file_a != file_b
        rows = db.conn.execute(
            "SELECT scan_id, path FROM files ORDER BY scan_id"
        ).fetchall()
        assert [(int(row["scan_id"]), str(row["path"])) for row in rows] == [
            (scan_a, "D:/Videos/a.mp4"),
            (scan_b, "D:/Videos/a.mp4"),
        ]


def test_clone_scan_files_with_artifacts_roundtrips_to_new_snapshot() -> None:
    with Database(":memory:") as db:
        source_scan_id = db.create_scan(
            profile="balanced",
            roots=["D:/Videos"],
            extensions=["mp4"],
            audio_fingerprint_enabled=True,
        )
        source_file_id = db.upsert_file(
            path="D:/Videos/a.mp4",
            size=123,
            mtime_ns=456,
            ctime_ns=456,
            ext="mp4",
            scan_id=source_scan_id,
        )
        meta = VideoMeta(
            duration_s=10.0,
            width=320,
            height=240,
            fps=24.0,
            codec="h264",
            bitrate=1000,
            has_audio=True,
            audio_codec="aac",
            audio_bitrate=128000,
            audio_languages="eng",
            subtitle_languages="",
            hdr_format="",
        )
        db.save_video_meta(source_file_id, meta)
        db.save_fingerprint(
            source_file_id,
            algo_version=ALGO_VERSION,
            hashes=[1, 2, 3],
        )
        db.save_fingerprint_provenance_batch(
            [(source_file_id, ALGO_VERSION, "pyav", '{"decoder_backend":"pyav"}')]
        )
        db.save_audio_fingerprints_batch([(source_file_id, 123, 456, "audio:abc")])
        db.upsert_failed_file(
            source_scan_id,
            ScanIssue(
                stage="probe",
                path="D:/Videos/a.mp4",
                message="transient failure",
            ),
        )
        db.complete_scan(source_scan_id, status="done")

        target_scan_id = db.create_scan(
            profile="balanced",
            roots=["D:/Videos"],
            extensions=["mp4"],
            audio_fingerprint_enabled=True,
        )
        cloned = db.clone_scan_files_with_artifacts(
            source_scan_id=source_scan_id,
            target_scan_id=target_scan_id,
            paths={"D:/Videos/a.mp4"},
            algo_version=ALGO_VERSION,
            include_audio_fingerprint=True,
        )
        copied_failed = db.clone_failed_files_for_paths(
            source_scan_id=source_scan_id,
            target_scan_id=target_scan_id,
            paths={"D:/Videos/a.mp4"},
        )

        assert copied_failed == 1
        cloned_file_id = int(cloned["D:/Videos/a.mp4"])
        assert cloned_file_id != source_file_id

        cached = db.get_cached_artifacts(
            "D:/Videos/a.mp4",
            size=123,
            mtime_ns=456,
            include_audio_fingerprint=True,
        )
        assert cached is not None
        assert cached["file_id"] == cloned_file_id
        assert cached["meta"].duration_s == 10.0
        assert cached["fingerprint"]["hashes"] == [1, 2, 3]
        assert cached["audio_fingerprint"] == "audio:abc"
        assert db.count_failed_files(target_scan_id) == 1


def test_cached_artifacts_are_probe_backend_specific() -> None:
    with Database(":memory:") as db:
        scan_id = db.create_scan(
            profile="balanced", roots=["D:/Videos"], extensions=["mp4"]
        )
        file_id = db.upsert_file(
            path="D:/Videos/a.mp4",
            size=10,
            mtime_ns=11,
            ctime_ns=11,
            ext="mp4",
            scan_id=scan_id,
        )
        meta = VideoMeta(
            duration_s=12.0,
            width=1920,
            height=1080,
            fps=30.0,
            codec="h264",
            bitrate=1000,
            has_audio=True,
            audio_codec="aac",
            audio_bitrate=128000,
            audio_languages="eng",
            subtitle_languages="",
            hdr_format="",
        )
        db.save_video_meta(file_id, meta, probe_backend="ffprobe")
        db.save_fingerprint(
            file_id,
            algo_version=ALGO_VERSION,
            hashes=[1, 2, 3, 4],
            probe_backend="ffprobe",
        )
        db.save_video_meta(
            file_id,
            VideoMeta(
                duration_s=24.0,
                width=1280,
                height=720,
                fps=60.0,
                codec="hevc",
                bitrate=2000,
                has_audio=False,
                audio_codec="",
                audio_bitrate=0,
                audio_languages="",
                subtitle_languages="",
                hdr_format="HDR10",
            ),
            probe_backend="pyav",
        )
        db.save_fingerprint(
            file_id,
            algo_version=ALGO_VERSION,
            hashes=[9, 8, 7, 6],
            probe_backend="pyav",
        )

        ffprobe_cache = db.get_cached_artifacts(
            "D:/Videos/a.mp4",
            size=10,
            mtime_ns=11,
            probe_backend="ffprobe",
        )
        pyav_cache = db.get_cached_artifacts(
            "D:/Videos/a.mp4",
            size=10,
            mtime_ns=11,
            probe_backend="pyav",
        )

        assert ffprobe_cache is not None
        assert pyav_cache is not None
        assert ffprobe_cache["meta"].codec == "h264"
        assert pyav_cache["meta"].codec == "hevc"
        assert ffprobe_cache["fingerprint"]["hashes"] == [1, 2, 3, 4]
        assert pyav_cache["fingerprint"]["hashes"] == [9, 8, 7, 6]


def test_probe_error_rows_do_not_count_as_cached_probe_metadata() -> None:
    with Database(":memory:") as db:
        scan_id = db.create_scan(
            profile="balanced", roots=["D:/Videos"], extensions=["mp4"]
        )
        file_id = db.upsert_file(
            path="D:/Videos/broken.mp4",
            size=10,
            mtime_ns=11,
            ctime_ns=11,
            ext="mp4",
            scan_id=scan_id,
        )

        db.save_probe_error(file_id, "broken container", probe_backend="ffprobe")

        cached = db.get_cached_artifacts(
            "D:/Videos/broken.mp4",
            size=10,
            mtime_ns=11,
            probe_backend="ffprobe",
        )

        assert cached is None
