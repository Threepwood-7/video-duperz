from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from video_duperz.db import Database
from video_duperz.fingerprint import ALGO_VERSION
from video_duperz.models import DuplicateGroup, DuplicateItem, VideoMeta
from video_duperz.scan_sets import build_scan_set_key

if TYPE_CHECKING:
    from pathlib import Path


def test_db_migration_backfills_scan_set_columns(tmp_path: Path) -> None:
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
        INSERT INTO scans(created_at, profile, roots_json, status)
        VALUES('2026-01-01T10:00:00+00:00', 'balanced', '["D:/Videos"]', 'done');
        PRAGMA user_version = 2;
        """
    )
    conn.commit()
    conn.close()

    with Database(db_file) as db:
        cols = {
            str(row["name"])
            for row in db.conn.execute("PRAGMA table_info(scans)").fetchall()
        }
        assert "extensions_json" in cols
        assert "probe_backend" in cols
        assert "scan_set_key" in cols
        row = db.conn.execute(
            "SELECT scan_set_key, extensions_json, probe_backend "
            "FROM scans WHERE id = 1"
        ).fetchone()
        assert str(row["scan_set_key"])
        assert str(row["extensions_json"]) == "[]"
        assert str(row["probe_backend"]) == "ffprobe"


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
            is_hdr=False,
        )
        db.save_video_meta(file_b, meta)
        db.save_video_meta(file_a, meta)
        db.save_fingerprint(file_b, algo_version=ALGO_VERSION, hashes=[1, 2, 3])
        db.save_fingerprint(file_a, algo_version=ALGO_VERSION, hashes=[4, 5, 6])

        items = db.list_match_items_for_scan(scan_id=scan_id, algo_version=ALGO_VERSION)

        assert [item.path for item in items] == ["D:/Videos/a.mp4", "D:/Videos/b.mp4"]


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
                        is_hdr=False,
                    ),
                )
            ]
        )
        db.save_fingerprints_batch([(file_a, ALGO_VERSION, [1, 2, 3, 4])])
        db.save_probe_errors_batch([(file_b, "probe failed")])

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
                    is_hdr=False,
                    similarity_score=1.0,
                    keep_default=True,
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
                    is_hdr=False,
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
            ]
        )
        assert "D:/Videos/a.mp4" in cached
        assert "meta" in cached["D:/Videos/a.mp4"]
        assert "fingerprint" in cached["D:/Videos/a.mp4"]
        assert cached["D:/Videos/a.mp4"]["fingerprint"]["algo_version"] == ALGO_VERSION


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
            is_hdr=False,
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
                is_hdr=True,
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


def test_analysis_issues_are_probe_backend_scoped_and_clearable() -> None:
    with Database(":memory:") as db:
        scan_id = db.create_scan(
            profile="balanced",
            roots=["D:/Videos"],
            extensions=["mp4"],
            probe_backend="pyav",
        )
        file_id = db.upsert_file(
            path="D:/Videos/a.mp4",
            size=10,
            mtime_ns=11,
            ctime_ns=11,
            ext="mp4",
            scan_id=scan_id,
        )

        db.save_analysis_issue(
            file_id,
            "analyze_timeout",
            "analysis timeout after 60s; manual review required",
            probe_backend="pyav",
        )
        db.save_analysis_issue(
            file_id,
            "fingerprint_fallback",
            "primary frame decoder opencv failed; ffmpeg fallback succeeded",
            probe_backend="pyav",
        )
        db.save_analysis_issue(
            file_id,
            "analyze_timeout",
            "ffprobe timeout placeholder",
            probe_backend="ffprobe",
        )

        persisted = db.list_analysis_issues_for_scan(scan_id)
        assert len(persisted) == 3
        assert {str(item["probe_backend"]) for item in persisted} == {"ffprobe", "pyav"}
        assert {
            str(item["stage"])
            for item in persisted
            if str(item["probe_backend"]) == "pyav"
        } == {"analyze_timeout", "fingerprint_fallback"}

        db.delete_analysis_issue(file_id, probe_backend="pyav")
        remaining = db.list_analysis_issues_for_scan(scan_id)
        assert len(remaining) == 1
        assert str(remaining[0]["probe_backend"]) == "ffprobe"
