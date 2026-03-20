"""Artifact persistence helpers for scans, metadata, and fingerprints."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, cast

from threep_commons.fs_paths import path_key

from .db_shared import (
    coerce_int,
    decode_hashes,
    encode_hashes,
    require_lastrowid,
)
from .models import (
    FrameDecodeBackendId,
    ProbeBackendId,
    ScanIssue,
    ScanLinkRecord,
    VideoMeta,
    utc_now_iso,
)
from .scan_sets import (
    build_scan_set_key,
    normalize_cross_resolution_mode,
    normalize_custom_similarity_threshold,
    normalize_extensions,
    normalize_roots_for_display,
    normalize_similarity_profile,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping, Sequence


class CachedFingerprintPayload(TypedDict):
    """Serialized fingerprint payload returned by cache lookups."""

    algo_version: int
    frame_count: int
    hashes: list[int]
    created_at: str


class CachedArtifacts(TypedDict, total=False):
    """Cached probe and fingerprint data returned for one file path."""

    file_id: int
    meta: VideoMeta
    meta_probed_at: str
    fingerprint: CachedFingerprintPayload
    fingerprint_created_at: str
    audio_fingerprint: str
    audio_fingerprint_created_at: str


class FailedFileRow(TypedDict):
    """Persisted failed-file payload for paused-scan resume decisions."""

    normalized_path: str
    display_path: str
    stage: str
    message: str
    created_at: str
    updated_at: str


class ScanFileSnapshotRow(TypedDict):
    """Persisted file snapshot row for one completed scan."""

    file_id: int
    path: str
    normalized_path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    ext: str


class DatabaseArtifactMixin:
    """Scan-row, cached-artifact, and fingerprint persistence helpers."""

    conn: sqlite3.Connection

    @staticmethod
    def _iter_chunks(values: list[str], chunk_size: int = 300) -> Iterable[list[str]]:
        """Yield fixed-size string chunks for SQLite ``IN`` queries."""
        raise NotImplementedError

    def _commit_if_needed(self) -> None:
        """Commit immediately when the connection is not in a scan transaction."""
        raise NotImplementedError

    def _lookup_file_stat_snapshot(self, file_id: int) -> tuple[int, int]:
        """Load the current file-size and mtime snapshot for one file row."""
        row = self.conn.execute(
            "SELECT size, mtime_ns FROM files WHERE id = ?",
            (int(file_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"file_id {file_id} not found")
        return (int(row["size"]), int(row["mtime_ns"]))

    def create_scan(
        self,
        profile: str,
        roots: list[str],
        extensions: list[str] | None = None,
        custom_similarity_threshold: float = 0.18,
        scene_aware_sampling: bool = False,
        audio_fingerprint_enabled: bool = False,
        cross_resolution_mode: str = "off",
        probe_backend: ProbeBackendId = "pyav",
    ) -> int:
        """Insert a new scan row and return its id."""
        normalized_roots = normalize_roots_for_display(roots)
        normalized_profile = normalize_similarity_profile(profile)
        normalized_extensions = normalize_extensions(extensions or [])
        scan_set_key = build_scan_set_key(
            roots=normalized_roots,
            similarity_profile=normalized_profile,
            extensions=normalized_extensions,
            custom_similarity_threshold=custom_similarity_threshold,
            scene_aware_sampling=scene_aware_sampling,
            audio_fingerprint_enabled=audio_fingerprint_enabled,
            cross_resolution_mode=cross_resolution_mode,
        )
        cursor = self.conn.execute(
            """
            INSERT INTO scans(
              created_at, profile, roots_json, extensions_json,
              custom_similarity_threshold, scene_aware_sampling,
              audio_fingerprint_enabled, cross_resolution_mode,
              probe_backend, scan_set_key, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                normalized_profile,
                json.dumps(normalized_roots),
                json.dumps(normalized_extensions),
                normalize_custom_similarity_threshold(custom_similarity_threshold),
                1 if scene_aware_sampling else 0,
                1 if audio_fingerprint_enabled else 0,
                normalize_cross_resolution_mode(cross_resolution_mode),
                str(probe_backend),
                scan_set_key,
                "running",
            ),
        )
        self._commit_if_needed()
        return require_lastrowid(cursor)

    def complete_scan(self, scan_id: int, status: str = "done") -> None:
        """Mark a scan row as completed with the given status."""
        self.conn.execute("UPDATE scans SET status = ? WHERE id = ?", (status, scan_id))
        self._commit_if_needed()

    def update_scan_definition(
        self,
        scan_id: int,
        *,
        profile: str,
        roots: list[str],
        extensions: list[str] | None = None,
        custom_similarity_threshold: float = 0.18,
        scene_aware_sampling: bool = False,
        audio_fingerprint_enabled: bool = False,
        cross_resolution_mode: str = "off",
        probe_backend: ProbeBackendId = "pyav",
        status: str | None = None,
    ) -> None:
        """Update one scan row so a paused scan can be resumed in place."""
        normalized_roots = normalize_roots_for_display(roots)
        normalized_profile = normalize_similarity_profile(profile)
        normalized_extensions = normalize_extensions(extensions or [])
        scan_set_key = build_scan_set_key(
            roots=normalized_roots,
            similarity_profile=normalized_profile,
            extensions=normalized_extensions,
            custom_similarity_threshold=custom_similarity_threshold,
            scene_aware_sampling=scene_aware_sampling,
            audio_fingerprint_enabled=audio_fingerprint_enabled,
            cross_resolution_mode=cross_resolution_mode,
        )
        if status is None:
            self.conn.execute(
                """
                UPDATE scans
                SET profile = ?, roots_json = ?, extensions_json = ?,
                    custom_similarity_threshold = ?, scene_aware_sampling = ?,
                    audio_fingerprint_enabled = ?, cross_resolution_mode = ?,
                    probe_backend = ?, scan_set_key = ?
                WHERE id = ?
                """,
                (
                    normalized_profile,
                    json.dumps(normalized_roots),
                    json.dumps(normalized_extensions),
                    normalize_custom_similarity_threshold(custom_similarity_threshold),
                    1 if scene_aware_sampling else 0,
                    1 if audio_fingerprint_enabled else 0,
                    normalize_cross_resolution_mode(cross_resolution_mode),
                    str(probe_backend),
                    scan_set_key,
                    scan_id,
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE scans
                SET profile = ?, roots_json = ?, extensions_json = ?,
                    custom_similarity_threshold = ?, scene_aware_sampling = ?,
                    audio_fingerprint_enabled = ?, cross_resolution_mode = ?,
                    probe_backend = ?, scan_set_key = ?, status = ?
                WHERE id = ?
                """,
                (
                    normalized_profile,
                    json.dumps(normalized_roots),
                    json.dumps(normalized_extensions),
                    normalize_custom_similarity_threshold(custom_similarity_threshold),
                    1 if scene_aware_sampling else 0,
                    1 if audio_fingerprint_enabled else 0,
                    normalize_cross_resolution_mode(cross_resolution_mode),
                    str(probe_backend),
                    scan_set_key,
                    str(status),
                    scan_id,
                ),
            )
        self._commit_if_needed()

    def insert_scan_issue(self, scan_id: int, issue: ScanIssue) -> None:
        """Persist one scan issue row for the given scan."""
        self.insert_scan_issues_batch(scan_id, [issue])

    def insert_scan_issues_batch(self, scan_id: int, issues: list[ScanIssue]) -> None:
        """Persist multiple scan issue rows for the given scan."""
        if not issues:
            return
        created_at = utc_now_iso()
        payload = [
            (
                int(scan_id),
                created_at,
                str(issue.stage),
                str(issue.path),
                str(issue.message),
            )
            for issue in issues
        ]
        self.conn.executemany(
            """
            INSERT INTO scan_issues(scan_id, created_at, stage, path, message)
            VALUES(?, ?, ?, ?, ?)
            """,
            payload,
        )
        self._commit_if_needed()

    def list_scan_issues(self, scan_id: int) -> list[ScanIssue]:
        """Load all persisted issue rows for one scan."""
        rows = self.conn.execute(
            """
            SELECT stage, path, message
            FROM scan_issues
            WHERE scan_id = ?
            ORDER BY id
            """,
            (scan_id,),
        ).fetchall()
        return [
            ScanIssue(
                stage=str(row["stage"] or ""),
                path=str(row["path"] or ""),
                message=str(row["message"] or ""),
            )
            for row in rows
        ]

    def delete_scan_issues_for_scan(self, scan_id: int) -> None:
        """Delete all persisted issue rows for one scan."""
        self.conn.execute("DELETE FROM scan_issues WHERE scan_id = ?", (scan_id,))
        self._commit_if_needed()

    def delete_scan_links_for_scan(self, scan_id: int) -> None:
        """Delete all persisted link rows for one scan."""
        self.conn.execute("DELETE FROM scan_links WHERE scan_id = ?", (int(scan_id),))
        self._commit_if_needed()

    def upsert_scan_links_batch(self, links: list[ScanLinkRecord]) -> None:
        """Persist multiple tracked link rows for one or more scans."""
        if not links:
            return
        payload = [
            (
                int(link.scan_id),
                str(link.link_kind),
                str(link.link_path),
                str(link.target_original_path),
                1 if link.target_exists else 0,
                str(link.source_root),
            )
            for link in links
        ]
        self.conn.executemany(
            """
            INSERT INTO scan_links(
              scan_id, link_kind, link_path, target_original_path,
              target_exists_flag, source_root
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id, link_path) DO UPDATE SET
              link_kind = excluded.link_kind,
              target_original_path = excluded.target_original_path,
              target_exists_flag = excluded.target_exists_flag,
              source_root = excluded.source_root
            """,
            payload,
        )
        self._commit_if_needed()

    @staticmethod
    def _is_retryable_failed_issue(issue: ScanIssue) -> bool:
        """Return whether one issue should count as a resumable failed file."""
        stage = str(issue.stage).strip().lower()
        path = str(issue.path).strip()
        return stage in {"probe", "fingerprint", "analyze"} and bool(path)

    def upsert_failed_file(self, scan_id: int, issue: ScanIssue) -> None:
        """Persist one retryable failed-file row for a paused or running scan."""
        if not self._is_retryable_failed_issue(issue):
            return
        now = utc_now_iso()
        normalized_path = path_key(issue.path)
        self.conn.execute(
            """
            INSERT INTO scan_failed_files(
              scan_id, normalized_path, display_path, stage, message,
              created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id, normalized_path) DO UPDATE SET
              display_path = excluded.display_path,
              stage = excluded.stage,
              message = excluded.message,
              updated_at = excluded.updated_at
            """,
            (
                int(scan_id),
                normalized_path,
                str(issue.path),
                str(issue.stage),
                str(issue.message),
                now,
                now,
            ),
        )
        self._commit_if_needed()

    def clear_failed_file(self, scan_id: int, path: str) -> None:
        """Delete one retryable failed-file marker after a successful retry."""
        normalized_path = path_key(path)
        if not normalized_path:
            return
        self.conn.execute(
            """
            DELETE FROM scan_failed_files
            WHERE scan_id = ? AND normalized_path = ?
            """,
            (int(scan_id), normalized_path),
        )
        self._commit_if_needed()

    def list_failed_files(self, scan_id: int) -> list[FailedFileRow]:
        """Load retryable failed-file rows for one scan."""
        rows = self.conn.execute(
            """
            SELECT normalized_path, display_path, stage, message, created_at, updated_at
            FROM scan_failed_files
            WHERE scan_id = ?
            ORDER BY normalized_path
            """,
            (int(scan_id),),
        ).fetchall()
        return [
            FailedFileRow(
                normalized_path=str(row["normalized_path"] or ""),
                display_path=str(row["display_path"] or ""),
                stage=str(row["stage"] or ""),
                message=str(row["message"] or ""),
                created_at=str(row["created_at"] or ""),
                updated_at=str(row["updated_at"] or ""),
            )
            for row in rows
        ]

    def count_failed_files(self, scan_id: int) -> int:
        """Return the number of retryable failed files for one scan."""
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM scan_failed_files
            WHERE scan_id = ?
            """,
            (int(scan_id),),
        ).fetchone()
        return int(row["c"] if row is not None else 0)

    def failed_file_path_keys(self, scan_id: int) -> set[str]:
        """Return normalized failed-file path keys for one scan."""
        rows = self.conn.execute(
            """
            SELECT normalized_path
            FROM scan_failed_files
            WHERE scan_id = ?
            """,
            (int(scan_id),),
        ).fetchall()
        return {
            str(row["normalized_path"] or "")
            for row in rows
            if str(row["normalized_path"] or "")
        }

    def load_scan_file_snapshot(
        self,
        scan_id: int,
    ) -> dict[str, ScanFileSnapshotRow]:
        """Load live file snapshot rows for one completed scan keyed by path key."""
        rows = self.conn.execute(
            """
            SELECT id, path, normalized_path, size, mtime_ns, ctime_ns, ext
            FROM files
            WHERE scan_id = ? AND exists_flag = 1
            ORDER BY normalized_path
            """,
            (int(scan_id),),
        ).fetchall()
        return {
            str(row["normalized_path"]): ScanFileSnapshotRow(
                file_id=int(row["id"]),
                path=str(row["path"]),
                normalized_path=str(row["normalized_path"]),
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                ctime_ns=int(row["ctime_ns"]),
                ext=str(row["ext"] or ""),
            )
            for row in rows
        }

    def clone_failed_files_for_paths(
        self,
        *,
        source_scan_id: int,
        target_scan_id: int,
        paths: set[str],
    ) -> int:
        """Clone persisted failed-file markers for unchanged paths."""
        if not paths:
            return 0
        normalized_paths = sorted({path_key(path) for path in paths if path_key(path)})
        if not normalized_paths:
            return 0
        copied = 0
        for chunk in self._iter_chunks(normalized_paths):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"""
                SELECT normalized_path, display_path, stage, message,
                       created_at, updated_at
                FROM scan_failed_files
                WHERE scan_id = ? AND normalized_path IN ({placeholders})
                """,
                (int(source_scan_id), *chunk),
            ).fetchall()
            if not rows:
                continue
            payload = [
                (
                    int(target_scan_id),
                    str(row["normalized_path"]),
                    str(row["display_path"]),
                    str(row["stage"]),
                    str(row["message"]),
                    str(row["created_at"]),
                    str(row["updated_at"]),
                )
                for row in rows
            ]
            self.conn.executemany(
                """
                INSERT INTO scan_failed_files(
                  scan_id, normalized_path, display_path, stage, message,
                  created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scan_id, normalized_path) DO UPDATE SET
                  display_path = excluded.display_path,
                  stage = excluded.stage,
                  message = excluded.message,
                  updated_at = excluded.updated_at
                """,
                payload,
            )
            copied += len(payload)
        self._commit_if_needed()
        return copied

    def clone_scan_files_with_artifacts(
        self,
        *,
        source_scan_id: int,
        target_scan_id: int,
        paths: set[str],
        probe_backend: ProbeBackendId = "pyav",
        algo_version: int,
        include_audio_fingerprint: bool = False,
    ) -> dict[str, int]:
        """Clone file rows and cached artifacts from one scan into another."""
        if not paths:
            return {}
        requested_paths_by_key: dict[str, list[str]] = {}
        for raw_path in sorted(str(path) for path in paths if str(path).strip()):
            normalized_path = path_key(raw_path)
            if not normalized_path:
                continue
            requested_paths_by_key.setdefault(normalized_path, []).append(raw_path)
        if not requested_paths_by_key:
            return {}
        source_rows: list[sqlite3.Row] = []
        for chunk in self._iter_chunks(sorted(requested_paths_by_key)):
            placeholders = ",".join("?" for _ in chunk)
            source_rows.extend(
                self.conn.execute(
                    f"""
                    SELECT id, path, normalized_path, size, mtime_ns, ctime_ns, ext
                    FROM files
                    WHERE scan_id = ? AND exists_flag = 1
                      AND normalized_path IN ({placeholders})
                    ORDER BY normalized_path
                    """,
                    (int(source_scan_id), *chunk),
                ).fetchall()
            )
        if not source_rows:
            return {}
        file_payload = [
            (
                str(row["path"]),
                str(row["normalized_path"]),
                int(row["size"]),
                int(row["mtime_ns"]),
                int(row["ctime_ns"]),
                str(row["ext"] or ""),
                int(target_scan_id),
            )
            for row in source_rows
        ]
        self.conn.executemany(
            """
            INSERT INTO files(
              path, normalized_path, size, mtime_ns, ctime_ns, ext, scan_id, exists_flag
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(scan_id, normalized_path) DO UPDATE SET
              path = excluded.path,
              size = excluded.size,
              mtime_ns = excluded.mtime_ns,
              ctime_ns = excluded.ctime_ns,
              ext = excluded.ext,
              exists_flag = 1
            """,
            file_payload,
        )
        inserted = self.conn.execute(
            """
            SELECT id, normalized_path
            FROM files
            WHERE scan_id = ?
            """,
            (int(target_scan_id),),
        ).fetchall()
        target_file_ids = {
            str(row["normalized_path"]): int(row["id"]) for row in inserted
        }
        source_to_target = {
            int(row["id"]): target_file_ids[str(row["normalized_path"])]
            for row in source_rows
        }
        source_file_ids = sorted(source_to_target)
        meta_rows: list[sqlite3.Row] = []
        fingerprint_rows: list[sqlite3.Row] = []
        provenance_rows: list[sqlite3.Row] = []
        audio_rows: list[sqlite3.Row] = []
        for chunk in self._iter_chunks([str(file_id) for file_id in source_file_ids]):
            placeholders = ",".join("?" for _ in chunk)
            meta_rows.extend(
                self.conn.execute(
                    f"""
                    SELECT file_id, probed_at, source_size, source_mtime_ns, duration_s,
                           width, height, fps, bit_depth, hdr_format, container,
                           codec_profile, codec_level, is_interlaced,
                           codec, bitrate, audio_stream_count, audio_codec,
                           audio_bitrate, audio_languages, subtitle_languages,
                           probe_error
                    FROM video_meta
                    WHERE probe_backend = ? AND file_id IN ({placeholders})
                    """,
                    (str(probe_backend), *chunk),
                ).fetchall()
            )
            fingerprint_rows.extend(
                self.conn.execute(
                    f"""
                    SELECT file_id, source_size, source_mtime_ns,
                           frame_count, hash_blob, created_at
                    FROM fingerprints
                    WHERE probe_backend = ? AND algo_version = ?
                      AND file_id IN ({placeholders})
                    """,
                    (str(probe_backend), int(algo_version), *chunk),
                ).fetchall()
            )
            provenance_rows.extend(
                self.conn.execute(
                    f"""
                    SELECT file_id, decoder_backend, attempts_json, created_at
                    FROM fingerprint_decoder_provenance
                    WHERE probe_backend = ? AND algo_version = ?
                      AND file_id IN ({placeholders})
                    """,
                    (str(probe_backend), int(algo_version), *chunk),
                ).fetchall()
            )
            if include_audio_fingerprint:
                audio_rows.extend(
                    self.conn.execute(
                        f"""
                        SELECT file_id, source_size, source_mtime_ns, fingerprint_text,
                               created_at
                        FROM audio_fingerprints
                        WHERE file_id IN ({placeholders})
                        """,
                        tuple(chunk),
                    ).fetchall()
                )
        if meta_rows:
            self.conn.executemany(
                """
                INSERT INTO video_meta(
                  file_id, probe_backend, probed_at, source_size, source_mtime_ns,
                  duration_s, width, height, fps, bit_depth, hdr_format,
                  container, codec_profile, codec_level, is_interlaced,
                  codec, bitrate, audio_stream_count, audio_codec,
                  audio_bitrate, audio_languages, subtitle_languages, probe_error
                )
                VALUES(
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(file_id, probe_backend) DO UPDATE SET
                  probed_at = excluded.probed_at,
                  source_size = excluded.source_size,
                  source_mtime_ns = excluded.source_mtime_ns,
                  duration_s = excluded.duration_s,
                  width = excluded.width,
                  height = excluded.height,
                  fps = excluded.fps,
                  bit_depth = excluded.bit_depth,
                  hdr_format = excluded.hdr_format,
                  container = excluded.container,
                  codec_profile = excluded.codec_profile,
                  codec_level = excluded.codec_level,
                  is_interlaced = excluded.is_interlaced,
                  codec = excluded.codec,
                  bitrate = excluded.bitrate,
                  audio_stream_count = excluded.audio_stream_count,
                  audio_codec = excluded.audio_codec,
                  audio_bitrate = excluded.audio_bitrate,
                  audio_languages = excluded.audio_languages,
                  subtitle_languages = excluded.subtitle_languages,
                  probe_error = excluded.probe_error
                """,
                [
                    (
                        source_to_target[int(row["file_id"])],
                        str(probe_backend),
                        str(row["probed_at"]),
                        int(row["source_size"]),
                        int(row["source_mtime_ns"]),
                        float(row["duration_s"]),
                        int(row["width"]),
                        int(row["height"]),
                        float(row["fps"]),
                        int(row["bit_depth"] or 8),
                        str(row["hdr_format"] or ""),
                        str(row["container"] or ""),
                        str(row["codec_profile"] or ""),
                        str(row["codec_level"] or ""),
                        int(row["is_interlaced"] or 0),
                        str(row["codec"] or ""),
                        int(row["bitrate"]),
                        int(row["audio_stream_count"] or 0),
                        str(row["audio_codec"] or ""),
                        int(row["audio_bitrate"] or 0),
                        str(row["audio_languages"] or ""),
                        str(row["subtitle_languages"] or ""),
                        row["probe_error"],
                    )
                    for row in meta_rows
                    if int(row["file_id"]) in source_to_target
                ],
            )
        if fingerprint_rows:
            self.conn.executemany(
                """
                INSERT INTO fingerprints(
                  file_id, probe_backend, algo_version, source_size, source_mtime_ns,
                  frame_count, hash_blob, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id, probe_backend, algo_version) DO UPDATE SET
                  source_size = excluded.source_size,
                  source_mtime_ns = excluded.source_mtime_ns,
                  frame_count = excluded.frame_count,
                  hash_blob = excluded.hash_blob,
                  created_at = excluded.created_at
                """,
                [
                    (
                        source_to_target[int(row["file_id"])],
                        str(probe_backend),
                        int(algo_version),
                        int(row["source_size"]),
                        int(row["source_mtime_ns"]),
                        int(row["frame_count"]),
                        row["hash_blob"],
                        str(row["created_at"]),
                    )
                    for row in fingerprint_rows
                    if int(row["file_id"]) in source_to_target
                ],
            )
        if provenance_rows:
            self.conn.executemany(
                """
                INSERT INTO fingerprint_decoder_provenance(
                  file_id, probe_backend, algo_version, decoder_backend,
                  attempts_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id, probe_backend, algo_version) DO UPDATE SET
                  decoder_backend = excluded.decoder_backend,
                  attempts_json = excluded.attempts_json,
                  created_at = excluded.created_at
                """,
                [
                    (
                        source_to_target[int(row["file_id"])],
                        str(probe_backend),
                        int(algo_version),
                        str(row["decoder_backend"] or ""),
                        str(row["attempts_json"] or "[]"),
                        str(row["created_at"]),
                    )
                    for row in provenance_rows
                    if int(row["file_id"]) in source_to_target
                ],
            )
        if audio_rows:
            self.conn.executemany(
                """
                INSERT INTO audio_fingerprints(
                  file_id, source_size, source_mtime_ns, fingerprint_text,
                  created_at
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                  source_size = excluded.source_size,
                  source_mtime_ns = excluded.source_mtime_ns,
                  fingerprint_text = excluded.fingerprint_text,
                  created_at = excluded.created_at
                """,
                [
                    (
                        source_to_target[int(row["file_id"])],
                        int(row["source_size"]),
                        int(row["source_mtime_ns"]),
                        str(row["fingerprint_text"] or ""),
                        str(row["created_at"]),
                    )
                    for row in audio_rows
                    if int(row["file_id"]) in source_to_target
                ],
            )
        self._commit_if_needed()
        cloned_by_requested_path: dict[str, int] = {}
        for row in source_rows:
            normalized_path = str(row["normalized_path"] or "")
            target_file_id = source_to_target.get(int(row["id"]))
            if target_file_id is None:
                continue
            for raw_path in requested_paths_by_key.get(normalized_path, []):
                cloned_by_requested_path[raw_path] = target_file_id
        return cloned_by_requested_path

    def upsert_file(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        ctime_ns: int,
        ext: str,
        scan_id: int,
    ) -> int:
        """Insert or update one scan file row and return its id."""
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

    def upsert_files_batch(
        self,
        files: Sequence[Mapping[str, object]],
    ) -> dict[str, int]:
        """Insert or update multiple file rows and map paths back to ids."""
        if not files:
            return {}
        rows: list[tuple[str, str, int, int, int, str, int]] = []
        ordered_by_scan: dict[int, list[tuple[str, str]]] = {}
        for file in files:
            path = str(file.get("path", "")).strip()
            if not path:
                continue
            normalized_path = path_key(path)
            if not normalized_path:
                continue
            scan_id = coerce_int(file.get("scan_id", 0))
            rows.append(
                (
                    path,
                    normalized_path,
                    coerce_int(file.get("size", 0)),
                    coerce_int(file.get("mtime_ns", 0)),
                    coerce_int(file.get("ctime_ns", 0)),
                    str(file.get("ext", "")),
                    scan_id,
                )
            )
            ordered_by_scan.setdefault(scan_id, []).append((path, normalized_path))
        if not rows:
            return {}
        self.conn.executemany(
            """
            INSERT INTO files(
              path, normalized_path, size, mtime_ns, ctime_ns, ext, scan_id, exists_flag
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(scan_id, normalized_path) DO UPDATE SET
              path = excluded.path,
              size = excluded.size,
              mtime_ns = excluded.mtime_ns,
              ctime_ns = excluded.ctime_ns,
              ext = excluded.ext,
              exists_flag = 1
            """,
            rows,
        )
        path_to_id: dict[str, int] = {}
        for scan_id, requested_rows in ordered_by_scan.items():
            unique_paths = sorted({normalized for _, normalized in requested_rows})
            normalized_to_id: dict[str, int] = {}
            for chunk in self._iter_chunks(unique_paths):
                placeholders = ",".join("?" for _ in chunk)
                fetched = self.conn.execute(
                    f"""
                    SELECT id, normalized_path
                    FROM files
                    WHERE scan_id = ? AND normalized_path IN ({placeholders})
                    """,
                    (int(scan_id), *chunk),
                ).fetchall()
                for row in fetched:
                    normalized_to_id[str(row["normalized_path"])] = int(row["id"])
            for raw_path, normalized_path in requested_rows:
                file_id = normalized_to_id.get(normalized_path)
                if file_id is not None:
                    path_to_id[raw_path] = file_id
        self._commit_if_needed()
        return path_to_id

    def mark_missing_for_scan(self, scan_id: int, present_paths: set[str]) -> None:
        """Mark file rows absent when they were not seen during the current scan."""
        normalized_present_paths = {
            normalized_path
            for path in present_paths
            if (normalized_path := path_key(path))
        }
        rows = self.conn.execute(
            """
            SELECT id, normalized_path
            FROM files
            WHERE scan_id = ? AND exists_flag = 1
            """,
            (scan_id,),
        ).fetchall()
        for row in rows:
            normalized_path = str(row["normalized_path"] or "")
            if normalized_path not in normalized_present_paths:
                self.conn.execute(
                    "UPDATE files SET exists_flag = 0 WHERE id = ?",
                    (int(row["id"]),),
                )
        self._commit_if_needed()

    @staticmethod
    def _row_to_cached_artifacts(row: sqlite3.Row) -> CachedArtifacts:
        """Convert a joined file row into the cached-artifact payload shape."""
        out: CachedArtifacts = {"file_id": int(row["file_id"])}
        probe_error = str(row["probe_error"] or "").strip()
        if (
            row["duration_s"] is not None
            and not probe_error
            and int(row["meta_source_size"] or 0) == int(row["size"])
            and int(row["meta_source_mtime_ns"] or 0) == int(row["mtime_ns"])
        ):
            out["meta"] = VideoMeta(
                duration_s=float(row["duration_s"]),
                width=int(row["width"]),
                height=int(row["height"]),
                fps=float(row["fps"]),
                bit_depth=int(row["bit_depth"] or 8),
                hdr_format=str(row["hdr_format"] or ""),
                container=str(row["container"] or ""),
                codec_profile=str(row["codec_profile"] or ""),
                codec_level=str(row["codec_level"] or ""),
                is_interlaced=bool(row["is_interlaced"]),
                codec=str(row["codec"]),
                bitrate=int(row["bitrate"]),
                audio_stream_count=int(row["audio_stream_count"] or 0),
                audio_codec=str(row["audio_codec"] or ""),
                audio_bitrate=int(row["audio_bitrate"] or 0),
                audio_languages=str(row["audio_languages"] or ""),
                subtitle_languages=str(row["subtitle_languages"] or ""),
            )
            out["meta_probed_at"] = str(row["probed_at"] or "")
        if (
            row["algo_version"] is not None
            and int(row["fp_source_size"] or 0) == int(row["size"])
            and int(row["fp_source_mtime_ns"] or 0) == int(row["mtime_ns"])
        ):
            out["fingerprint"] = {
                "algo_version": int(row["algo_version"]),
                "frame_count": int(row["frame_count"]),
                "hashes": decode_hashes(row["hash_blob"]),
                "created_at": str(row["created_at"] or ""),
            }
            out["fingerprint_created_at"] = str(row["created_at"] or "")
        if (
            row["audio_fingerprint_text"] is not None
            and int(row["audio_source_size"] or 0) == int(row["size"])
            and int(row["audio_source_mtime_ns"] or 0) == int(row["mtime_ns"])
        ):
            out["audio_fingerprint"] = str(row["audio_fingerprint_text"] or "")
            out["audio_fingerprint_created_at"] = str(row["audio_created_at"] or "")
        return out

    def get_cached_artifacts(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        *,
        algo_version: int = 1,
        include_audio_fingerprint: bool = False,
        probe_backend: ProbeBackendId = "pyav",
    ) -> CachedArtifacts | None:
        """Load cached artifacts for one file stat tuple."""
        cached = self.load_cached_artifacts_batch(
            [{"path": path, "size": int(size), "mtime_ns": int(mtime_ns)}],
            algo_version=algo_version,
            include_audio_fingerprint=include_audio_fingerprint,
            probe_backend=probe_backend,
        )
        normalized_path = path_key(path)
        if not normalized_path:
            return None
        return cached.get(normalized_path)

    def load_cached_artifacts_batch(
        self,
        files: Sequence[Mapping[str, object]],
        *,
        algo_version: int,
        include_audio_fingerprint: bool = False,
        probe_backend: ProbeBackendId = "pyav",
    ) -> dict[str, CachedArtifacts]:
        """Load cached metadata and fingerprints keyed by normalized path."""
        if not files:
            return {}
        requested: dict[str, tuple[int, int]] = {}
        for file in files:
            path = str(file.get("path", "")).strip()
            if not path:
                continue
            normalized_path = path_key(path)
            if not normalized_path:
                continue
            requested[normalized_path] = (
                coerce_int(file.get("size", 0)),
                coerce_int(file.get("mtime_ns", 0)),
            )
        if not requested:
            return {}

        out: dict[str, CachedArtifacts] = {}
        request_rows = [
            (normalized_path, values[0], values[1])
            for normalized_path, values in sorted(requested.items())
        ]
        chunk_size = 100
        for offset in range(0, len(request_rows), chunk_size):
            chunk = request_rows[offset : offset + chunk_size]
            placeholders = ", ".join("(?, ?, ?)" for _ in chunk)
            values_clause_params: list[object] = []
            for normalized_path, size, mtime_ns in chunk:
                values_clause_params.extend((normalized_path, size, mtime_ns))
            rows = self.conn.execute(
                f"""
                WITH requested(normalized_path, size, mtime_ns) AS (
                  VALUES {placeholders}
                )
                SELECT f.id AS file_id, f.path, f.size, f.mtime_ns,
                       f.normalized_path,
                       vm.probed_at, vm.probe_error,
                       vm.source_size AS meta_source_size,
                       vm.source_mtime_ns AS meta_source_mtime_ns,
                       vm.duration_s, vm.width, vm.height, vm.fps,
                       vm.bit_depth, vm.hdr_format, vm.container,
                       vm.codec_profile, vm.codec_level, vm.is_interlaced,
                       vm.codec, vm.bitrate, vm.audio_stream_count,
                       vm.audio_codec, vm.audio_bitrate,
                       vm.audio_languages, vm.subtitle_languages,
                       fp.source_size AS fp_source_size,
                       fp.source_mtime_ns AS fp_source_mtime_ns,
                       fp.algo_version, fp.frame_count, fp.hash_blob, fp.created_at,
                       af.source_size AS audio_source_size,
                       af.source_mtime_ns AS audio_source_mtime_ns,
                       af.fingerprint_text AS audio_fingerprint_text,
                       af.created_at AS audio_created_at
                FROM requested req
                JOIN files f
                  ON f.normalized_path = req.normalized_path
                 AND f.size = req.size
                 AND f.mtime_ns = req.mtime_ns
                 AND f.exists_flag = 1
                LEFT JOIN video_meta vm
                  ON vm.file_id = f.id AND vm.probe_backend = ?
                LEFT JOIN fingerprints fp
                  ON fp.file_id = f.id AND fp.probe_backend = ?
                 AND fp.algo_version = ?
                LEFT JOIN audio_fingerprints af
                  ON af.file_id = f.id
                ORDER BY req.normalized_path, f.scan_id DESC, f.id DESC
                """,
                (
                    *values_clause_params,
                    str(probe_backend),
                    str(probe_backend),
                    int(algo_version),
                ),
            ).fetchall()
            for row in rows:
                normalized_path = str(row["normalized_path"] or "")
                if normalized_path in out:
                    continue
                expected = requested.get(normalized_path)
                if expected is None:
                    continue
                if (
                    int(row["size"]) != expected[0]
                    or int(row["mtime_ns"]) != expected[1]
                ):
                    continue
                artifacts = self._row_to_cached_artifacts(cast("sqlite3.Row", row))
                if not include_audio_fingerprint:
                    artifacts.pop("audio_fingerprint", None)
                    artifacts.pop("audio_fingerprint_created_at", None)
                if len(artifacts) <= 1:
                    continue
                out[normalized_path] = artifacts
        return out

    def save_video_meta(
        self,
        file_id: int,
        meta: VideoMeta,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist video metadata for one file row."""
        source_size, source_mtime_ns = self._lookup_file_stat_snapshot(file_id)
        self.save_video_meta_batch(
            [(int(file_id), source_size, source_mtime_ns, meta)],
            probe_backend=probe_backend,
        )

    def save_probe_error(
        self,
        file_id: int,
        error: str,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist one probe error row for a file."""
        source_size, source_mtime_ns = self._lookup_file_stat_snapshot(file_id)
        self.save_probe_errors_batch(
            [(int(file_id), source_size, source_mtime_ns, str(error))],
            probe_backend=probe_backend,
        )

    def save_fingerprint(
        self,
        file_id: int,
        algo_version: int,
        hashes: list[int],
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist one fingerprint row for a file."""
        source_size, source_mtime_ns = self._lookup_file_stat_snapshot(file_id)
        self.save_fingerprints_batch(
            [
                (
                    int(file_id),
                    source_size,
                    source_mtime_ns,
                    int(algo_version),
                    list(hashes),
                )
            ],
            probe_backend=probe_backend,
        )

    def save_video_meta_batch(
        self,
        rows: list[tuple[int, int, int, VideoMeta]],
        *,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist multiple video metadata rows."""
        if not rows:
            return
        probed_at = utc_now_iso()
        payload: list[tuple[object, ...]] = []
        for file_id, source_size, source_mtime_ns, meta in rows:
            payload.append(
                (
                    int(file_id),
                    str(probe_backend),
                    probed_at,
                    int(source_size),
                    int(source_mtime_ns),
                    float(meta.duration_s),
                    int(meta.width),
                    int(meta.height),
                    float(meta.fps),
                    int(meta.bit_depth),
                    str(meta.hdr_format),
                    str(meta.container),
                    str(meta.codec_profile),
                    str(meta.codec_level),
                    1 if meta.is_interlaced else 0,
                    str(meta.codec),
                    int(meta.bitrate),
                    int(meta.audio_stream_count),
                    str(meta.audio_codec),
                    int(meta.audio_bitrate),
                    str(meta.audio_languages),
                    str(meta.subtitle_languages),
                )
            )
        self.conn.executemany(
            """
            INSERT INTO video_meta(
              file_id, probe_backend, probed_at, source_size, source_mtime_ns,
              duration_s, width, height, fps, bit_depth, hdr_format,
              container, codec_profile, codec_level, is_interlaced,
              codec, bitrate, audio_stream_count, audio_codec, audio_bitrate,
              audio_languages, subtitle_languages, probe_error
            )
            VALUES(
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL
            )
            ON CONFLICT(file_id, probe_backend) DO UPDATE SET
              probed_at = excluded.probed_at,
              source_size = excluded.source_size,
              source_mtime_ns = excluded.source_mtime_ns,
              duration_s = excluded.duration_s,
              width = excluded.width,
              height = excluded.height,
              fps = excluded.fps,
              bit_depth = excluded.bit_depth,
              hdr_format = excluded.hdr_format,
              container = excluded.container,
              codec_profile = excluded.codec_profile,
              codec_level = excluded.codec_level,
              is_interlaced = excluded.is_interlaced,
              codec = excluded.codec,
              bitrate = excluded.bitrate,
              audio_stream_count = excluded.audio_stream_count,
              audio_codec = excluded.audio_codec,
              audio_bitrate = excluded.audio_bitrate,
              audio_languages = excluded.audio_languages,
              subtitle_languages = excluded.subtitle_languages,
              probe_error = NULL
            """,
            payload,
        )
        self._commit_if_needed()

    def save_probe_errors_batch(
        self,
        rows: list[tuple[int, int, int, str]],
        *,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist multiple probe error rows."""
        if not rows:
            return
        probed_at = utc_now_iso()
        payload = [
            (
                int(file_id),
                str(probe_backend),
                probed_at,
                int(source_size),
                int(source_mtime_ns),
                str(error)[:500],
            )
            for file_id, source_size, source_mtime_ns, error in rows
        ]
        self.conn.executemany(
            """
            INSERT INTO video_meta(
              file_id, probe_backend, probed_at, source_size, source_mtime_ns,
              duration_s, width, height, fps, bit_depth, hdr_format,
              container, codec_profile, codec_level, is_interlaced,
              codec, bitrate, audio_stream_count, audio_codec, audio_bitrate,
              audio_languages, subtitle_languages, probe_error
            )
            VALUES(
              ?, ?, ?, ?, ?,
              0, 0, 0, 0, 8, '', '', '', '', 0, '', 0, 0, '', 0, '', '', ?
            )
            ON CONFLICT(file_id, probe_backend) DO UPDATE SET
              probed_at = excluded.probed_at,
              source_size = excluded.source_size,
              source_mtime_ns = excluded.source_mtime_ns,
              probe_error = excluded.probe_error
            """,
            payload,
        )
        self._commit_if_needed()

    def save_fingerprints_batch(
        self,
        rows: list[tuple[int, int, int, int, list[int]]],
        *,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist multiple fingerprint rows."""
        if not rows:
            return
        created_at = utc_now_iso()
        payload: list[tuple[int, str, int, int, int, int, bytes, str]] = []
        for file_id, source_size, source_mtime_ns, algo_version, hashes in rows:
            normalized_hashes = list(hashes)
            payload.append(
                (
                    int(file_id),
                    str(probe_backend),
                    int(source_size),
                    int(source_mtime_ns),
                    int(algo_version),
                    len(normalized_hashes),
                    encode_hashes(normalized_hashes),
                    created_at,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO fingerprints(
              file_id, probe_backend, source_size, source_mtime_ns,
              algo_version, frame_count, hash_blob, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id, probe_backend, algo_version) DO UPDATE SET
              source_size = excluded.source_size,
              source_mtime_ns = excluded.source_mtime_ns,
              frame_count = excluded.frame_count,
              hash_blob = excluded.hash_blob,
              created_at = excluded.created_at
            """,
            payload,
        )
        self._commit_if_needed()

    def save_fingerprint_provenance_batch(
        self,
        rows: list[tuple[int, int, FrameDecodeBackendId, str]],
        *,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist quiet decoder provenance for multiple fingerprint rows."""
        if not rows:
            return
        created_at = utc_now_iso()
        payload = [
            (
                int(file_id),
                str(probe_backend),
                int(algo_version),
                str(decoder_backend),
                str(attempts_json),
                created_at,
            )
            for file_id, algo_version, decoder_backend, attempts_json in rows
        ]
        self.conn.executemany(
            """
            INSERT INTO fingerprint_decoder_provenance(
              file_id, probe_backend, algo_version, decoder_backend,
              attempts_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id, probe_backend, algo_version) DO UPDATE SET
              decoder_backend = excluded.decoder_backend,
              attempts_json = excluded.attempts_json,
              created_at = excluded.created_at
            """,
            payload,
        )
        self._commit_if_needed()

    def save_audio_fingerprints_batch(
        self,
        rows: list[tuple[int, int, int, str]],
    ) -> None:
        """Persist multiple audio-fingerprint rows."""
        if not rows:
            return
        created_at = utc_now_iso()
        payload = [
            (
                int(file_id),
                int(source_size),
                int(source_mtime_ns),
                str(fingerprint_text),
                created_at,
            )
            for file_id, source_size, source_mtime_ns, fingerprint_text in rows
        ]
        self.conn.executemany(
            """
            INSERT INTO audio_fingerprints(
              file_id, source_size, source_mtime_ns, fingerprint_text, created_at
            )
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
              source_size = excluded.source_size,
              source_mtime_ns = excluded.source_mtime_ns,
              fingerprint_text = excluded.fingerprint_text,
              created_at = excluded.created_at
            """,
            payload,
        )
        self._commit_if_needed()
