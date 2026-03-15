"""Artifact persistence helpers for scans, metadata, and fingerprints."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from .db_shared import (
    coerce_int,
    decode_hashes,
    encode_hashes,
    require_lastrowid,
)
from .models import FrameDecodeBackendId, ProbeBackendId, VideoMeta, utc_now_iso
from .scan_sets import (
    build_scan_set_key,
    normalize_extensions,
    normalize_roots_for_display,
    normalize_similarity_profile,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping, Sequence


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

    def create_scan(
        self,
        profile: str,
        roots: list[str],
        extensions: list[str] | None = None,
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
        )
        cursor = self.conn.execute(
            """
            INSERT INTO scans(
              created_at, profile, roots_json, extensions_json,
              probe_backend, scan_set_key, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                normalized_profile,
                json.dumps(normalized_roots),
                json.dumps(normalized_extensions),
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
        rows: list[tuple[str, int, int, int, str, int]] = []
        ordered_paths: list[str] = []
        for file in files:
            path = str(file.get("path", "")).strip()
            if not path:
                continue
            rows.append(
                (
                    path,
                    coerce_int(file.get("size", 0)),
                    coerce_int(file.get("mtime_ns", 0)),
                    coerce_int(file.get("ctime_ns", 0)),
                    str(file.get("ext", "")),
                    coerce_int(file.get("scan_id", 0)),
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
        """Mark file rows absent when they were not seen during the current scan."""
        rows = self.conn.execute(
            "SELECT path FROM files WHERE scan_id = ?",
            (scan_id,),
        ).fetchall()
        for row in rows:
            path = row["path"]
            if path not in present_paths:
                self.conn.execute(
                    "UPDATE files SET exists_flag = 0 WHERE path = ?",
                    (path,),
                )
        self._commit_if_needed()

    @staticmethod
    def _row_to_cached_artifacts(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a joined file row into the cached-artifact payload shape."""
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
        self,
        path: str,
        size: int,
        mtime_ns: int,
        probe_backend: ProbeBackendId = "pyav",
    ) -> dict[str, Any] | None:
        """Load cached artifacts for one file stat tuple."""
        cached = self.load_cached_artifacts_batch(
            [{"path": path, "size": int(size), "mtime_ns": int(mtime_ns)}],
            probe_backend=probe_backend,
        )
        return cached.get(path)

    def load_cached_artifacts_batch(
        self,
        files: Sequence[Mapping[str, object]],
        probe_backend: ProbeBackendId = "pyav",
    ) -> dict[str, dict[str, Any]]:
        """Load cached metadata and fingerprints for matching file stat tuples."""
        if not files:
            return {}
        requested: dict[str, tuple[int, int]] = {}
        for file in files:
            path = str(file.get("path", "")).strip()
            if not path:
                continue
            requested[path] = (
                coerce_int(file.get("size", 0)),
                coerce_int(file.get("mtime_ns", 0)),
            )
        if not requested:
            return {}

        out: dict[str, dict[str, Any]] = {}
        for chunk in self._iter_chunks(sorted(requested)):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"""
                SELECT f.id AS file_id, f.path, f.size, f.mtime_ns,
                       vm.duration_s, vm.width, vm.height, vm.fps,
                       vm.codec, vm.bitrate, vm.has_audio,
                       vm.audio_codec, vm.audio_bitrate,
                       vm.audio_languages, vm.subtitle_languages,
                       vm.is_hdr,
                       fp.algo_version, fp.frame_count, fp.hash_blob
                FROM files f
                LEFT JOIN video_meta vm
                  ON vm.file_id = f.id AND vm.probe_backend = ?
                LEFT JOIN fingerprints fp
                  ON fp.file_id = f.id AND fp.probe_backend = ?
                WHERE f.path IN ({placeholders}) AND f.exists_flag = 1
                """,
                (str(probe_backend), str(probe_backend), *chunk),
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
                artifacts = self._row_to_cached_artifacts(cast("sqlite3.Row", row))
                if len(artifacts) <= 1:
                    continue
                out[path] = artifacts
        return out

    def save_video_meta(
        self,
        file_id: int,
        meta: VideoMeta,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist video metadata for one file row."""
        self.save_video_meta_batch([(int(file_id), meta)], probe_backend=probe_backend)

    def save_probe_error(
        self,
        file_id: int,
        error: str,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist one probe error row for a file."""
        self.save_probe_errors_batch(
            [(int(file_id), str(error))],
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
        self.save_fingerprints_batch(
            [(int(file_id), int(algo_version), list(hashes))],
            probe_backend=probe_backend,
        )

    def save_video_meta_batch(
        self,
        rows: list[tuple[int, VideoMeta]],
        *,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist multiple video metadata rows."""
        if not rows:
            return
        payload: list[
            tuple[
                int,
                str,
                float,
                int,
                int,
                float,
                str,
                int,
                int,
                str,
                int,
                str,
                str,
                int,
            ]
        ] = []
        for file_id, meta in rows:
            payload.append(
                (
                    int(file_id),
                    str(probe_backend),
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
              file_id, probe_backend, duration_s, width, height, fps,
              codec, bitrate, has_audio, audio_codec, audio_bitrate, audio_languages,
              subtitle_languages, is_hdr, probe_error
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(file_id, probe_backend) DO UPDATE SET
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

    def save_probe_errors_batch(
        self,
        rows: list[tuple[int, str]],
        *,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist multiple probe error rows."""
        if not rows:
            return
        payload = [
            (int(file_id), str(probe_backend), str(error)[:500])
            for file_id, error in rows
        ]
        self.conn.executemany(
            """
            INSERT INTO video_meta(
              file_id, probe_backend, duration_s, width, height, fps,
              codec, bitrate, has_audio, audio_codec, audio_bitrate, audio_languages,
              subtitle_languages, is_hdr, probe_error
            )
            VALUES(?, ?, 0, 0, 0, 0, '', 0, 0, '', 0, '', '', 0, ?)
            ON CONFLICT(file_id, probe_backend) DO UPDATE SET
              probe_error = excluded.probe_error
            """,
            payload,
        )
        self._commit_if_needed()

    def save_fingerprints_batch(
        self,
        rows: list[tuple[int, int, list[int]]],
        *,
        probe_backend: ProbeBackendId = "pyav",
    ) -> None:
        """Persist multiple fingerprint rows."""
        if not rows:
            return
        created_at = utc_now_iso()
        payload: list[tuple[int, str, int, int, bytes, str]] = []
        for file_id, algo_version, hashes in rows:
            normalized_hashes = list(hashes)
            payload.append(
                (
                    int(file_id),
                    str(probe_backend),
                    int(algo_version),
                    len(normalized_hashes),
                    encode_hashes(normalized_hashes),
                    created_at,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO fingerprints(
              file_id, probe_backend, algo_version, frame_count, hash_blob, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id, probe_backend) DO UPDATE SET
              algo_version = excluded.algo_version,
              frame_count = excluded.frame_count,
              hash_blob = excluded.hash_blob,
              created_at = excluded.created_at
            """,
            payload,
        )
        self._commit_if_needed()

    def save_fingerprint_provenance_batch(
        self,
        rows: list[tuple[int, FrameDecodeBackendId, str]],
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
                str(decoder_backend),
                str(attempts_json),
                created_at,
            )
            for file_id, decoder_backend, attempts_json in rows
        ]
        self.conn.executemany(
            """
            INSERT INTO fingerprint_decoder_provenance(
              file_id, probe_backend, decoder_backend, attempts_json, created_at
            )
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(file_id, probe_backend) DO UPDATE SET
              decoder_backend = excluded.decoder_backend,
              attempts_json = excluded.attempts_json,
              created_at = excluded.created_at
            """,
            payload,
        )
        self._commit_if_needed()
