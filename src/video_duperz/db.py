from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any

from threep_commons.fs_paths import is_path_under_root, path_key

from .config import db_path
from .models import DuplicateGroup, DuplicateItem, MatchItem, VideoMeta, utc_now_iso
from .scan_sets import (
    build_scan_set_key,
    normalize_extensions,
    normalize_roots_for_display,
    normalize_similarity_profile,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

SCHEMA_VERSION = 3


def encode_hashes(hashes: list[int]) -> bytes:
    if not hashes:
        return b""
    return struct.pack(f">{len(hashes)}Q", *hashes)


def decode_hashes(blob: bytes | None) -> list[int]:
    if not blob:
        return []
    if len(blob) % 8 != 0:
        return []
    count = len(blob) // 8
    return list(struct.unpack(f">{count}Q", blob))


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            target = db_path()
        elif str(path) == ":memory:":
            target = Path(":memory:")
        else:
            target = Path(path)
        self.path = target
        if str(target) != ":memory:":
            target.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(target))
        self.conn.row_factory = sqlite3.Row
        self._scan_tx_active = False
        self._apply_scan_pragmas()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _apply_scan_pragmas(self) -> None:
        pragmas = (
            ("journal_mode", "WAL"),
            ("synchronous", "NORMAL"),
            ("temp_store", "MEMORY"),
            ("cache_size", "-131072"),
            ("mmap_size", "268435456"),
        )
        for key, value in pragmas:
            try:
                self.conn.execute(f"PRAGMA {key} = {value}")
            except sqlite3.DatabaseError:
                # Keep the connection usable even on filesystems/platforms
                # that do not support one of the tuning pragmas.
                continue

    @staticmethod
    def _iter_chunks(values: list[str], chunk_size: int = 300) -> Iterable[list[str]]:
        if chunk_size <= 0:
            chunk_size = 300
        for idx in range(0, len(values), chunk_size):
            yield values[idx : idx + chunk_size]

    def _commit_if_needed(self) -> None:
        if not self._scan_tx_active:
            self.conn.commit()

    def begin_scan_transaction(self) -> None:
        if self._scan_tx_active:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._scan_tx_active = True

    def flush_scan_transaction(self) -> None:
        if not self._scan_tx_active:
            return
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")

    def end_scan_transaction(self) -> None:
        if not self._scan_tx_active:
            return
        self.conn.commit()
        self._scan_tx_active = False

    def migrate(self) -> None:
        current = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if current >= SCHEMA_VERSION:
            return

        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scans(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              profile TEXT NOT NULL,
              roots_json TEXT NOT NULL,
              extensions_json TEXT NOT NULL DEFAULT '[]',
              scan_set_key TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS files(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              path TEXT NOT NULL UNIQUE,
              size INTEGER NOT NULL,
              mtime_ns INTEGER NOT NULL,
              ctime_ns INTEGER NOT NULL,
              ext TEXT NOT NULL,
              scan_id INTEGER NOT NULL,
              exists_flag INTEGER NOT NULL DEFAULT 1,
              FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_files_scan_id ON files(scan_id);
            CREATE INDEX IF NOT EXISTS idx_files_path_stat ON files(path, size, mtime_ns);
            CREATE INDEX IF NOT EXISTS idx_files_scan_exists ON files(scan_id, exists_flag);
            CREATE INDEX IF NOT EXISTS idx_files_path_stat_exists ON files(path, size, mtime_ns, exists_flag);

            CREATE TABLE IF NOT EXISTS video_meta(
              file_id INTEGER PRIMARY KEY,
              duration_s REAL NOT NULL,
              width INTEGER NOT NULL,
              height INTEGER NOT NULL,
              fps REAL NOT NULL,
              codec TEXT NOT NULL,
              bitrate INTEGER NOT NULL,
              has_audio INTEGER NOT NULL,
              audio_codec TEXT NOT NULL DEFAULT '',
              audio_bitrate INTEGER NOT NULL DEFAULT 0,
              audio_languages TEXT NOT NULL DEFAULT '',
              subtitle_languages TEXT NOT NULL DEFAULT '',
              is_hdr INTEGER NOT NULL DEFAULT 0,
              probe_error TEXT,
              FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS fingerprints(
              file_id INTEGER PRIMARY KEY,
              algo_version INTEGER NOT NULL,
              frame_count INTEGER NOT NULL,
              hash_blob BLOB NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_fingerprints_algo ON fingerprints(algo_version);

            CREATE TABLE IF NOT EXISTS duplicate_groups(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scan_id INTEGER NOT NULL,
              profile TEXT NOT NULL,
              created_at TEXT NOT NULL,
              total_size_bytes INTEGER NOT NULL,
              FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_duplicate_groups_scan ON duplicate_groups(scan_id);

            CREATE TABLE IF NOT EXISTS duplicate_group_items(
              group_id INTEGER NOT NULL,
              file_id INTEGER NOT NULL,
              similarity_score REAL NOT NULL,
              keep_default INTEGER NOT NULL,
              selected_action TEXT NOT NULL,
              PRIMARY KEY(group_id, file_id),
              FOREIGN KEY(group_id) REFERENCES duplicate_groups(id) ON DELETE CASCADE,
              FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_group_items_file ON duplicate_group_items(file_id);

            CREATE TABLE IF NOT EXISTS action_runs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scan_id INTEGER NOT NULL,
              mode TEXT NOT NULL,
              created_at TEXT NOT NULL,
              status TEXT NOT NULL,
              FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS action_items(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL,
              file_id INTEGER NOT NULL,
              source_path TEXT NOT NULL,
              target_path TEXT,
              result TEXT NOT NULL,
              error_text TEXT,
              FOREIGN KEY(run_id) REFERENCES action_runs(id) ON DELETE CASCADE,
              FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
            );
            """
        )
        self._ensure_video_meta_columns()
        self._ensure_scan_columns()
        self._backfill_scan_set_keys()
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _ensure_video_meta_columns(self) -> None:
        columns = {
            str(r["name"])
            for r in self.conn.execute("PRAGMA table_info(video_meta)").fetchall()
        }
        if "audio_codec" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN audio_codec TEXT NOT NULL DEFAULT ''"
            )
        if "audio_bitrate" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN audio_bitrate INTEGER NOT NULL DEFAULT 0"
            )
        if "audio_languages" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN audio_languages TEXT NOT NULL DEFAULT ''"
            )
        if "subtitle_languages" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN subtitle_languages TEXT NOT NULL DEFAULT ''"
            )
        if "is_hdr" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN is_hdr INTEGER NOT NULL DEFAULT 0"
            )

    def _ensure_scan_columns(self) -> None:
        columns = {
            str(r["name"])
            for r in self.conn.execute("PRAGMA table_info(scans)").fetchall()
        }
        if "extensions_json" not in columns:
            self.conn.execute(
                "ALTER TABLE scans ADD COLUMN extensions_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "scan_set_key" not in columns:
            self.conn.execute(
                "ALTER TABLE scans ADD COLUMN scan_set_key TEXT NOT NULL DEFAULT ''"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scans_set_status ON scans(scan_set_key, status, id DESC)"
        )

    def _backfill_scan_set_keys(self) -> None:
        rows = self.conn.execute(
            """
            SELECT id, roots_json, profile, extensions_json, scan_set_key
            FROM scans
            WHERE scan_set_key = '' OR scan_set_key IS NULL
            """
        ).fetchall()
        for row in rows:
            raw_roots = row["roots_json"]
            raw_extensions = row["extensions_json"]
            try:
                roots_payload = json.loads(raw_roots) if raw_roots else []
            except (TypeError, ValueError, json.JSONDecodeError):
                roots_payload = []
            if not isinstance(roots_payload, list):
                roots_payload = []
            roots = normalize_roots_for_display(roots_payload)
            try:
                ext_payload = json.loads(raw_extensions) if raw_extensions else []
            except (TypeError, ValueError, json.JSONDecodeError):
                ext_payload = []
            if not isinstance(ext_payload, list):
                ext_payload = []
            extensions = normalize_extensions(ext_payload)
            profile = normalize_similarity_profile(str(row["profile"] or "balanced"))
            scan_set_key = build_scan_set_key(
                roots=roots, similarity_profile=profile, extensions=extensions
            )
            self.conn.execute(
                "UPDATE scans SET scan_set_key = ? WHERE id = ?",
                (scan_set_key, int(row["id"])),
            )

    def create_scan(
        self, profile: str, roots: list[str], extensions: list[str] | None = None
    ) -> int:
        normalized_roots = normalize_roots_for_display(roots)
        normalized_profile = normalize_similarity_profile(profile)
        normalized_extensions = normalize_extensions(extensions or [])
        scan_set_key = build_scan_set_key(
            roots=normalized_roots,
            similarity_profile=normalized_profile,
            extensions=normalized_extensions,
        )
        cur = self.conn.execute(
            """
            INSERT INTO scans(created_at, profile, roots_json, extensions_json, scan_set_key, status)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                normalized_profile,
                json.dumps(normalized_roots),
                json.dumps(normalized_extensions),
                scan_set_key,
                "running",
            ),
        )
        self._commit_if_needed()
        return int(cur.lastrowid)

    def complete_scan(self, scan_id: int, status: str = "done") -> None:
        self.conn.execute("UPDATE scans SET status = ? WHERE id = ?", (status, scan_id))
        self._commit_if_needed()

    def upsert_file(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        ctime_ns: int,
        ext: str,
        scan_id: int,
    ) -> int:
        payload = [
            {
                "path": path,
                "size": int(size),
                "mtime_ns": int(mtime_ns),
                "ctime_ns": int(ctime_ns),
                "ext": str(ext),
                "scan_id": int(scan_id),
            }
        ]
        by_path = self.upsert_files_batch(payload)
        return int(by_path.get(path, 0))

    def upsert_files_batch(self, files: list[dict[str, object]]) -> dict[str, int]:
        if not files:
            return {}
        rows: list[tuple[str, int, int, int, str, int]] = []
        ordered_paths: list[str] = []
        for file in files:
            path = str(file.get("path", "")).strip()
            if not path:
                continue
            rows.append(
                (
                    path,
                    int(file.get("size", 0)),
                    int(file.get("mtime_ns", 0)),
                    int(file.get("ctime_ns", 0)),
                    str(file.get("ext", "")),
                    int(file.get("scan_id", 0)),
                )
            )
            ordered_paths.append(path)
        if not rows:
            return {}
        self.conn.executemany(
            """
            INSERT INTO files(path, size, mtime_ns, ctime_ns, ext, scan_id, exists_flag)
            VALUES(?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(path) DO UPDATE SET
              size = excluded.size,
              mtime_ns = excluded.mtime_ns,
              ctime_ns = excluded.ctime_ns,
              ext = excluded.ext,
              scan_id = excluded.scan_id,
              exists_flag = 1
            """,
            rows,
        )
        path_to_id: dict[str, int] = {}
        unique_paths = sorted(set(ordered_paths))
        for chunk in self._iter_chunks(unique_paths):
            placeholders = ",".join("?" for _ in chunk)
            fetched = self.conn.execute(
                f"SELECT id, path FROM files WHERE path IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in fetched:
                path_to_id[str(row["path"])] = int(row["id"])
        self._commit_if_needed()
        return path_to_id

    def mark_missing_for_scan(self, scan_id: int, present_paths: set[str]) -> None:
        rows = self.conn.execute(
            "SELECT path FROM files WHERE scan_id = ?", (scan_id,)
        ).fetchall()
        for row in rows:
            path = row["path"]
            if path not in present_paths:
                self.conn.execute(
                    "UPDATE files SET exists_flag = 0 WHERE path = ?", (path,)
                )
        self._commit_if_needed()

    @staticmethod
    def _row_to_cached_artifacts(row: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"file_id": int(row["file_id"])}
        if row["duration_s"] is not None:
            out["meta"] = VideoMeta(
                duration_s=float(row["duration_s"]),
                width=int(row["width"]),
                height=int(row["height"]),
                fps=float(row["fps"]),
                codec=str(row["codec"]),
                bitrate=int(row["bitrate"]),
                has_audio=bool(row["has_audio"]),
                audio_codec=str(row["audio_codec"] or ""),
                audio_bitrate=int(row["audio_bitrate"] or 0),
                audio_languages=str(row["audio_languages"] or ""),
                subtitle_languages=str(row["subtitle_languages"] or ""),
                is_hdr=bool(row["is_hdr"]),
            )
        if row["algo_version"] is not None:
            out["fingerprint"] = {
                "algo_version": int(row["algo_version"]),
                "frame_count": int(row["frame_count"]),
                "hashes": decode_hashes(row["hash_blob"]),
            }
        return out

    def get_cached_artifacts(
        self, path: str, size: int, mtime_ns: int
    ) -> dict[str, Any] | None:
        cached = self.load_cached_artifacts_batch(
            [
                {
                    "path": path,
                    "size": int(size),
                    "mtime_ns": int(mtime_ns),
                }
            ]
        )
        return cached.get(path)

    def load_cached_artifacts_batch(
        self, files: list[dict[str, object]]
    ) -> dict[str, dict[str, Any]]:
        if not files:
            return {}
        requested: dict[str, tuple[int, int]] = {}
        for file in files:
            path = str(file.get("path", "")).strip()
            if not path:
                continue
            requested[path] = (int(file.get("size", 0)), int(file.get("mtime_ns", 0)))
        if not requested:
            return {}

        out: dict[str, dict[str, Any]] = {}
        for chunk in self._iter_chunks(sorted(requested)):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"""
                SELECT f.id AS file_id, f.path, f.size, f.mtime_ns,
                       vm.duration_s, vm.width, vm.height, vm.fps, vm.codec, vm.bitrate, vm.has_audio,
                       vm.audio_codec, vm.audio_bitrate, vm.audio_languages, vm.subtitle_languages, vm.is_hdr,
                       fp.algo_version, fp.frame_count, fp.hash_blob
                FROM files f
                LEFT JOIN video_meta vm ON vm.file_id = f.id
                LEFT JOIN fingerprints fp ON fp.file_id = f.id
                WHERE f.path IN ({placeholders}) AND f.exists_flag = 1
                """,
                tuple(chunk),
            ).fetchall()
            for row in rows:
                path = str(row["path"])
                expected = requested.get(path)
                if expected is None:
                    continue
                if (
                    int(row["size"]) != expected[0]
                    or int(row["mtime_ns"]) != expected[1]
                ):
                    continue
                out[path] = self._row_to_cached_artifacts(row)
        return out

    def save_video_meta(self, file_id: int, meta: VideoMeta) -> None:
        self.save_video_meta_batch([(int(file_id), meta)])

    def save_probe_error(self, file_id: int, error: str) -> None:
        self.save_probe_errors_batch([(int(file_id), str(error))])

    def save_fingerprint(
        self, file_id: int, algo_version: int, hashes: list[int]
    ) -> None:
        self.save_fingerprints_batch([(int(file_id), int(algo_version), list(hashes))])

    def save_video_meta_batch(self, rows: list[tuple[int, VideoMeta]]) -> None:
        if not rows:
            return
        payload: list[
            tuple[int, float, int, int, float, str, int, int, str, int, str, str, int]
        ] = []
        for file_id, meta in rows:
            payload.append(
                (
                    int(file_id),
                    float(meta.duration_s),
                    int(meta.width),
                    int(meta.height),
                    float(meta.fps),
                    str(meta.codec),
                    int(meta.bitrate),
                    1 if meta.has_audio else 0,
                    str(meta.audio_codec),
                    int(meta.audio_bitrate),
                    str(meta.audio_languages),
                    str(meta.subtitle_languages),
                    1 if meta.is_hdr else 0,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO video_meta(
              file_id, duration_s, width, height, fps, codec, bitrate, has_audio,
              audio_codec, audio_bitrate, audio_languages, subtitle_languages, is_hdr, probe_error
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(file_id) DO UPDATE SET
              duration_s = excluded.duration_s,
              width = excluded.width,
              height = excluded.height,
              fps = excluded.fps,
              codec = excluded.codec,
              bitrate = excluded.bitrate,
              has_audio = excluded.has_audio,
              audio_codec = excluded.audio_codec,
              audio_bitrate = excluded.audio_bitrate,
              audio_languages = excluded.audio_languages,
              subtitle_languages = excluded.subtitle_languages,
              is_hdr = excluded.is_hdr,
              probe_error = NULL
            """,
            payload,
        )
        self._commit_if_needed()

    def save_probe_errors_batch(self, rows: list[tuple[int, str]]) -> None:
        if not rows:
            return
        payload = [(int(file_id), str(error)[:500]) for file_id, error in rows]
        self.conn.executemany(
            """
            INSERT INTO video_meta(
              file_id, duration_s, width, height, fps, codec, bitrate, has_audio,
              audio_codec, audio_bitrate, audio_languages, subtitle_languages, is_hdr, probe_error
            )
            VALUES(?, 0, 0, 0, 0, '', 0, 0, '', 0, '', '', 0, ?)
            ON CONFLICT(file_id) DO UPDATE SET probe_error = excluded.probe_error
            """,
            payload,
        )
        self._commit_if_needed()

    def save_fingerprints_batch(self, rows: list[tuple[int, int, list[int]]]) -> None:
        if not rows:
            return
        created_at = utc_now_iso()
        payload: list[tuple[int, int, int, bytes, str]] = []
        for file_id, algo_version, hashes in rows:
            normalized_hashes = list(hashes)
            payload.append(
                (
                    int(file_id),
                    int(algo_version),
                    len(normalized_hashes),
                    encode_hashes(normalized_hashes),
                    created_at,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO fingerprints(file_id, algo_version, frame_count, hash_blob, created_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
              algo_version = excluded.algo_version,
              frame_count = excluded.frame_count,
              hash_blob = excluded.hash_blob,
              created_at = excluded.created_at
            """,
            payload,
        )
        self._commit_if_needed()

    def list_match_items_for_scan(
        self, scan_id: int, algo_version: int
    ) -> list[MatchItem]:
        rows = self.conn.execute(
            """
            SELECT f.id AS file_id, f.path, f.size, f.mtime_ns, f.ctime_ns,
                   vm.duration_s, vm.width, vm.height, vm.fps, vm.codec, vm.bitrate,
                   vm.audio_codec, vm.audio_bitrate, vm.audio_languages, vm.subtitle_languages, vm.is_hdr,
                   fp.hash_blob
            FROM files f
            JOIN video_meta vm ON vm.file_id = f.id
            JOIN fingerprints fp ON fp.file_id = f.id
            WHERE f.scan_id = ? AND f.exists_flag = 1 AND fp.algo_version = ?
              AND vm.duration_s > 0
            ORDER BY f.path
            """,
            (scan_id, algo_version),
        ).fetchall()
        return [
            MatchItem(
                file_id=int(r["file_id"]),
                path=str(r["path"]),
                size=int(r["size"]),
                mtime_ns=int(r["mtime_ns"]),
                ctime_ns=int(r["ctime_ns"]),
                duration_s=float(r["duration_s"]),
                width=int(r["width"]),
                height=int(r["height"]),
                fps=float(r["fps"]),
                codec=str(r["codec"]),
                bitrate=int(r["bitrate"]),
                audio_codec=str(r["audio_codec"] or ""),
                audio_bitrate=int(r["audio_bitrate"] or 0),
                audio_languages=str(r["audio_languages"] or ""),
                subtitle_languages=str(r["subtitle_languages"] or ""),
                is_hdr=bool(r["is_hdr"]),
                hashes=decode_hashes(r["hash_blob"]),
            )
            for r in rows
        ]

    def clear_duplicate_groups(self, scan_id: int) -> None:
        self.conn.execute("DELETE FROM duplicate_groups WHERE scan_id = ?", (scan_id,))
        self._commit_if_needed()

    def clear_all_scans(self) -> None:
        self.conn.execute("DELETE FROM scans")
        self._commit_if_needed()

    @staticmethod
    def _canonical_path_match_key(path: str) -> str:
        return path_key(str(path))

    @classmethod
    def _path_is_under_root(cls, path: str, root: str) -> bool:
        path_match_key = cls._canonical_path_match_key(path)
        root_match_key = cls._canonical_path_match_key(root)
        if not path_match_key or not root_match_key:
            return False
        return is_path_under_root(path, root)

    def purge_for_fresh_rescan(
        self, scan_set_key: str, roots: list[str]
    ) -> dict[str, int]:
        key = str(scan_set_key or "").strip()
        normalized_roots = [
            str(root) for root in roots if self._canonical_path_match_key(root)
        ]
        scan_ids: set[int] = set()
        if key:
            scan_rows = self.conn.execute(
                "SELECT id FROM scans WHERE scan_set_key = ?", (key,)
            ).fetchall()
            scan_ids.update(int(row["id"]) for row in scan_rows)
        if normalized_roots:
            file_rows = self.conn.execute("SELECT scan_id, path FROM files").fetchall()
            for row in file_rows:
                file_path = str(row["path"] or "")
                if any(
                    self._path_is_under_root(file_path, root)
                    for root in normalized_roots
                ):
                    scan_ids.add(int(row["scan_id"]))

        if not scan_ids:
            return {
                "deleted_scans": 0,
                "deleted_files": 0,
                "deleted_groups": 0,
                "deleted_actions": 0,
            }

        ordered_ids = sorted(scan_ids)
        placeholders = ",".join("?" for _ in ordered_ids)
        params = tuple(ordered_ids)
        deleted_scans = int(
            self.conn.execute(
                f"SELECT COUNT(*) AS c FROM scans WHERE id IN ({placeholders})", params
            ).fetchone()["c"]
        )
        deleted_files = int(
            self.conn.execute(
                f"SELECT COUNT(*) AS c FROM files WHERE scan_id IN ({placeholders})",
                params,
            ).fetchone()["c"]
        )
        deleted_groups = int(
            self.conn.execute(
                f"SELECT COUNT(*) AS c FROM duplicate_groups WHERE scan_id IN ({placeholders})",
                params,
            ).fetchone()["c"]
        )
        deleted_actions = int(
            self.conn.execute(
                f"SELECT COUNT(*) AS c FROM action_runs WHERE scan_id IN ({placeholders})",
                params,
            ).fetchone()["c"]
        )
        self.conn.execute(f"DELETE FROM scans WHERE id IN ({placeholders})", params)
        self._commit_if_needed()
        return {
            "deleted_scans": deleted_scans,
            "deleted_files": deleted_files,
            "deleted_groups": deleted_groups,
            "deleted_actions": deleted_actions,
        }

    def insert_duplicate_group(
        self, scan_id: int, profile: str, total_size_bytes: int
    ) -> int:
        group_ids = self.insert_duplicate_groups_batch(
            scan_id=scan_id,
            profile=profile,
            groups=[
                DuplicateGroup(
                    scan_id=scan_id,
                    profile=profile,
                    created_at=utc_now_iso(),
                    items=[],
                    total_size_bytes=int(total_size_bytes),
                )
            ],
        )
        return int(group_ids[0])

    def insert_duplicate_item(self, group_id: int, item: DuplicateItem) -> None:
        self.conn.execute(
            """
            INSERT INTO duplicate_group_items(group_id, file_id, similarity_score, keep_default, selected_action)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(group_id, file_id) DO UPDATE SET
              similarity_score = excluded.similarity_score,
              keep_default = excluded.keep_default,
              selected_action = excluded.selected_action
            """,
            (
                group_id,
                item.file_id,
                item.similarity_score,
                1 if item.keep_default else 0,
                item.selected_action,
            ),
        )
        self._commit_if_needed()

    def insert_duplicate_groups_batch(
        self, scan_id: int, profile: str, groups: list[DuplicateGroup]
    ) -> list[int]:
        if not groups:
            return []
        created_at = utc_now_iso()
        group_ids: list[int] = []
        item_rows: list[tuple[int, int, float, int, str]] = []
        for group in groups:
            cur = self.conn.execute(
                """
                INSERT INTO duplicate_groups(scan_id, profile, created_at, total_size_bytes)
                VALUES(?, ?, ?, ?)
                """,
                (int(scan_id), str(profile), created_at, int(group.total_size_bytes)),
            )
            group_id = int(cur.lastrowid)
            group_ids.append(group_id)
            for item in group.items:
                item_rows.append(
                    (
                        group_id,
                        int(item.file_id),
                        float(item.similarity_score),
                        1 if item.keep_default else 0,
                        str(item.selected_action),
                    )
                )
        if item_rows:
            self.conn.executemany(
                """
                INSERT INTO duplicate_group_items(group_id, file_id, similarity_score, keep_default, selected_action)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(group_id, file_id) DO UPDATE SET
                  similarity_score = excluded.similarity_score,
                  keep_default = excluded.keep_default,
                  selected_action = excluded.selected_action
                """,
                item_rows,
            )
        self._commit_if_needed()
        return group_ids

    def update_group_item_actions(self, group_id: int, actions: dict[int, str]) -> None:
        for file_id, action in actions.items():
            self.conn.execute(
                """
                UPDATE duplicate_group_items
                SET selected_action = ?
                WHERE group_id = ? AND file_id = ?
                """,
                (action, group_id, file_id),
            )
        self._commit_if_needed()

    def get_group_file_paths(self, group_id: int) -> dict[int, str]:
        rows = self.conn.execute(
            """
            SELECT gi.file_id, f.path
            FROM duplicate_group_items gi
            JOIN files f ON f.id = gi.file_id
            WHERE gi.group_id = ?
            """,
            (group_id,),
        ).fetchall()
        return {int(r["file_id"]): str(r["path"]) for r in rows}

    def refresh_file_after_rename(self, file_id: int, new_path: str) -> None:
        target = Path(new_path)
        st = target.stat()
        ext = target.suffix.lower().lstrip(".")
        self.conn.execute(
            """
            UPDATE files
            SET path = ?, size = ?, mtime_ns = ?, ext = ?, exists_flag = 1
            WHERE id = ?
            """,
            (str(target), int(st.st_size), int(st.st_mtime_ns), ext, file_id),
        )
        self._commit_if_needed()

    def mark_file_missing(self, file_id: int) -> None:
        self.conn.execute("UPDATE files SET exists_flag = 0 WHERE id = ?", (file_id,))
        self._commit_if_needed()

    def remove_file_from_duplicate_groups(self, file_id: int) -> None:
        self.conn.execute(
            "DELETE FROM duplicate_group_items WHERE file_id = ?", (file_id,)
        )
        self._commit_if_needed()

    def prune_duplicate_groups(self, scan_id: int) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT g.id AS group_id,
                   SUM(CASE WHEN f.exists_flag = 1 THEN 1 ELSE 0 END) AS alive_count
            FROM duplicate_groups g
            LEFT JOIN duplicate_group_items gi ON gi.group_id = g.id
            LEFT JOIN files f ON f.id = gi.file_id
            WHERE g.scan_id = ?
            GROUP BY g.id
            HAVING SUM(CASE WHEN f.exists_flag = 1 THEN 1 ELSE 0 END) < 2
            """,
            (scan_id,),
        ).fetchall()
        group_ids = [int(r["group_id"]) for r in rows]
        for group_id in group_ids:
            self.conn.execute("DELETE FROM duplicate_groups WHERE id = ?", (group_id,))
        self._commit_if_needed()
        return group_ids

    def load_duplicate_groups(self, scan_id: int) -> list[DuplicateGroup]:
        group_rows = self.conn.execute(
            """
            SELECT id, scan_id, profile, created_at, total_size_bytes
            FROM duplicate_groups
            WHERE scan_id = ?
            ORDER BY id
            """,
            (scan_id,),
        ).fetchall()
        groups: list[DuplicateGroup] = []
        for gr in group_rows:
            item_rows = self.conn.execute(
                """
                SELECT gi.file_id, gi.similarity_score, gi.keep_default, gi.selected_action,
                       f.path, f.size, f.mtime_ns, f.ctime_ns,
                       vm.duration_s, vm.width, vm.height, vm.bitrate, vm.codec,
                       vm.audio_codec, vm.audio_bitrate, vm.audio_languages, vm.subtitle_languages, vm.is_hdr
                FROM duplicate_group_items gi
                JOIN files f ON f.id = gi.file_id
                JOIN video_meta vm ON vm.file_id = f.id
                WHERE gi.group_id = ? AND f.exists_flag = 1
                ORDER BY gi.keep_default DESC, vm.width * vm.height DESC, vm.bitrate DESC
                """,
                (int(gr["id"]),),
            ).fetchall()
            items = [
                DuplicateItem(
                    file_id=int(ir["file_id"]),
                    path=str(ir["path"]),
                    size=int(ir["size"]),
                    mtime_ns=int(ir["mtime_ns"]),
                    ctime_ns=int(ir["ctime_ns"]),
                    duration_s=float(ir["duration_s"]),
                    width=int(ir["width"]),
                    height=int(ir["height"]),
                    bitrate=int(ir["bitrate"]),
                    codec=str(ir["codec"]),
                    audio_codec=str(ir["audio_codec"] or ""),
                    audio_bitrate=int(ir["audio_bitrate"] or 0),
                    audio_languages=str(ir["audio_languages"] or ""),
                    subtitle_languages=str(ir["subtitle_languages"] or ""),
                    is_hdr=bool(ir["is_hdr"]),
                    similarity_score=float(ir["similarity_score"]),
                    keep_default=bool(ir["keep_default"]),
                    selected_action=str(ir["selected_action"]),
                )
                for ir in item_rows
            ]
            if len(items) < 2:
                continue
            groups.append(
                DuplicateGroup(
                    scan_id=int(gr["scan_id"]),
                    profile=str(gr["profile"]),
                    created_at=str(gr["created_at"]),
                    items=items,
                    total_size_bytes=int(gr["total_size_bytes"]),
                    group_id=int(gr["id"]),
                )
            )
        return groups

    def get_scan_info(self, scan_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"scan_id {scan_id} not found")
        try:
            roots = json.loads(row["roots_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            roots = []
        if not isinstance(roots, list):
            roots = []
        try:
            extensions = json.loads(row["extensions_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            extensions = []
        if not isinstance(extensions, list):
            extensions = []
        return {
            "id": int(row["id"]),
            "created_at": str(row["created_at"]),
            "profile": str(row["profile"]),
            "roots": normalize_roots_for_display(roots),
            "extensions": normalize_extensions(extensions),
            "scan_set_key": str(row["scan_set_key"] or ""),
            "status": str(row["status"]),
        }

    def latest_scan_id(self) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def latest_scan_id_for_set(self, scan_set_key: str) -> int | None:
        key = str(scan_set_key or "").strip()
        if not key:
            return None
        row = self.conn.execute(
            """
            SELECT id
            FROM scans
            WHERE scan_set_key = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def latest_completed_scan_id_for_set(self, scan_set_key: str) -> int | None:
        key = str(scan_set_key or "").strip()
        if not key:
            return None
        row = self.conn.execute(
            """
            SELECT id
            FROM scans
            WHERE scan_set_key = ? AND status = 'done'
            ORDER BY id DESC
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def list_latest_scans_by_set(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT s.id, s.created_at, s.profile, s.roots_json, s.extensions_json, s.scan_set_key, s.status
            FROM scans s
            JOIN (
              SELECT scan_set_key, MAX(id) AS latest_id
              FROM scans
              WHERE scan_set_key <> ''
              GROUP BY scan_set_key
            ) latest ON latest.latest_id = s.id
            ORDER BY s.id DESC
            """
        ).fetchall()
        return self._scan_set_rows_to_payload(rows)

    def list_latest_completed_scans_by_set(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT s.id, s.created_at, s.profile, s.roots_json, s.extensions_json, s.scan_set_key, s.status
            FROM scans s
            JOIN (
              SELECT scan_set_key, MAX(id) AS latest_id
              FROM scans
              WHERE status = 'done' AND scan_set_key <> ''
              GROUP BY scan_set_key
            ) latest ON latest.latest_id = s.id
            ORDER BY s.id DESC
            """
        ).fetchall()
        return self._scan_set_rows_to_payload(rows)

    def _scan_set_rows_to_payload(self, rows: list[Any]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for row in rows:
            try:
                roots = json.loads(row["roots_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                roots = []
            if not isinstance(roots, list):
                roots = []
            try:
                extensions = json.loads(row["extensions_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                extensions = []
            if not isinstance(extensions, list):
                extensions = []
            output.append(
                {
                    "scan_id": int(row["id"]),
                    "created_at": str(row["created_at"]),
                    "profile": normalize_similarity_profile(
                        str(row["profile"] or "balanced")
                    ),
                    "roots": normalize_roots_for_display(roots),
                    "extensions": normalize_extensions(extensions),
                    "scan_set_key": str(row["scan_set_key"] or ""),
                    "status": str(row["status"] or ""),
                }
            )
        return output

    def scan_summary(self, scan_id: int) -> dict[str, Any]:
        scan = self.conn.execute(
            """
            SELECT id, created_at, profile, status
            FROM scans
            WHERE id = ?
            """,
            (scan_id,),
        ).fetchone()
        if scan is None:
            raise ValueError(f"scan_id {scan_id} not found")
        group_count_row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM duplicate_groups WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        file_count_row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM duplicate_group_items gi
            JOIN duplicate_groups g ON g.id = gi.group_id
            JOIN files f ON f.id = gi.file_id
            WHERE g.scan_id = ? AND f.exists_flag = 1
            """,
            (scan_id,),
        ).fetchone()
        return {
            "scan_id": int(scan["id"]),
            "created_at": str(scan["created_at"]),
            "profile": str(scan["profile"]),
            "status": str(scan["status"]),
            "group_count": int(
                group_count_row["c"] if group_count_row is not None else 0
            ),
            "file_count": int(file_count_row["c"] if file_count_row is not None else 0),
        }

    def insert_action_run(
        self, scan_id: int, mode: str, status: str = "running"
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO action_runs(scan_id, mode, created_at, status) VALUES(?, ?, ?, ?)",
            (scan_id, mode, utc_now_iso(), status),
        )
        self._commit_if_needed()
        return int(cur.lastrowid)

    def insert_action_item(
        self,
        run_id: int,
        file_id: int,
        source_path: str,
        target_path: str | None,
        result: str,
        error_text: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO action_items(run_id, file_id, source_path, target_path, result, error_text)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (run_id, file_id, source_path, target_path, result, error_text),
        )
        self._commit_if_needed()

    def finalize_action_run(self, run_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE action_runs SET status = ? WHERE id = ?", (status, run_id)
        )
        self._commit_if_needed()

    def fetch_file_path(self, file_id: int) -> str:
        row = self.conn.execute(
            "SELECT path FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"file_id {file_id} not found")
        return str(row["path"])
