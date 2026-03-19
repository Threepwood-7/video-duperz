"""Query helpers for scans, scan sets, and rescan cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from threep_commons.fs_paths import is_path_under_root, path_key

from .db_shared import decode_hashes, decode_string_list_json
from .models import MatchItem, ScanLinkRecord
from .scan_sets import (
    normalize_cross_resolution_mode,
    normalize_custom_similarity_threshold,
    normalize_extensions,
    normalize_roots_for_display,
    normalize_similarity_profile,
)

if TYPE_CHECKING:
    import sqlite3


class DatabaseScanQueryMixin:
    """Scan-query and rescan-cleanup helpers backed by the database."""

    conn: sqlite3.Connection

    def _commit_if_needed(self) -> None:
        """Commit immediately when the connection is not in a scan transaction."""
        raise NotImplementedError

    def list_match_items_for_scan(
        self,
        scan_id: int,
        algo_version: int,
    ) -> list[MatchItem]:
        """Load fingerprint-ready match items for one scan."""
        rows = self.conn.execute(
            """
            SELECT f.id AS file_id, f.path, f.size, f.mtime_ns, f.ctime_ns,
                   vm.duration_s, vm.width, vm.height, vm.fps, vm.bit_depth,
                   vm.hdr_format,
                   vm.codec, vm.bitrate,
                   vm.audio_codec, vm.audio_bitrate,
                   vm.audio_languages, vm.subtitle_languages,
                   fp.hash_blob,
                   af.fingerprint_text
            FROM files f
            JOIN scans s ON s.id = f.scan_id
            JOIN video_meta vm
              ON vm.file_id = f.id AND vm.probe_backend = s.probe_backend
            JOIN fingerprints fp
              ON fp.file_id = f.id AND fp.probe_backend = s.probe_backend
             AND fp.algo_version = ?
            LEFT JOIN audio_fingerprints af
              ON af.file_id = f.id
            WHERE f.scan_id = ? AND f.exists_flag = 1
              AND vm.duration_s > 0
              AND NOT EXISTS(
                SELECT 1
                FROM scan_failed_files sff
                WHERE sff.scan_id = f.scan_id
                  AND sff.display_path = f.path COLLATE NOCASE
              )
            ORDER BY f.path
            """,
            (algo_version, scan_id),
        ).fetchall()
        return [
            MatchItem(
                file_id=int(row["file_id"]),
                path=str(row["path"]),
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                ctime_ns=int(row["ctime_ns"]),
                duration_s=float(row["duration_s"]),
                width=int(row["width"]),
                height=int(row["height"]),
                fps=float(row["fps"]),
                bit_depth=int(row["bit_depth"] or 8),
                hdr_format=str(row["hdr_format"] or ""),
                codec=str(row["codec"]),
                bitrate=int(row["bitrate"]),
                audio_codec=str(row["audio_codec"] or ""),
                audio_bitrate=int(row["audio_bitrate"] or 0),
                audio_languages=str(row["audio_languages"] or ""),
                subtitle_languages=str(row["subtitle_languages"] or ""),
                hashes=decode_hashes(row["hash_blob"]),
                audio_fingerprint=str(row["fingerprint_text"] or ""),
            )
            for row in rows
        ]

    def clear_duplicate_groups(self, scan_id: int) -> None:
        """Delete all duplicate groups for one scan."""
        self.conn.execute("DELETE FROM duplicate_groups WHERE scan_id = ?", (scan_id,))
        self._commit_if_needed()

    def clear_all_scans(self) -> None:
        """Delete all scans and their cascaded child rows."""
        self.conn.execute("DELETE FROM scans")
        self._commit_if_needed()

    @staticmethod
    def _canonical_path_match_key(path: str) -> str:
        """Normalize one path for case-insensitive path-root matching."""
        return path_key(str(path))

    @classmethod
    def _path_is_under_root(cls, path: str, root: str) -> bool:
        """Return whether a path is contained by a candidate scan root."""
        path_match_key = cls._canonical_path_match_key(path)
        root_match_key = cls._canonical_path_match_key(root)
        if not path_match_key or not root_match_key:
            return False
        return is_path_under_root(path, root)

    def purge_for_fresh_rescan(
        self,
        scan_set_key: str,
        roots: list[str],
    ) -> dict[str, int]:
        """Delete prior scan artifacts for the given scan set or matching roots."""
        key = str(scan_set_key or "").strip()
        normalized_roots = [
            str(root) for root in roots if self._canonical_path_match_key(root)
        ]
        scan_ids: set[int] = set()
        if key:
            scan_rows = self.conn.execute(
                "SELECT id FROM scans WHERE scan_set_key = ?",
                (key,),
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
                f"SELECT COUNT(*) AS c FROM scans WHERE id IN ({placeholders})",
                params,
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
                "SELECT COUNT(*) AS c FROM duplicate_groups "
                f"WHERE scan_id IN ({placeholders})",
                params,
            ).fetchone()["c"]
        )
        deleted_actions = int(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM action_runs "
                f"WHERE scan_id IN ({placeholders})",
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

    def get_scan_info(self, scan_id: int) -> dict[str, Any]:
        """Load one scan row as the UI/export payload shape."""
        row = self.conn.execute(
            "SELECT * FROM scans WHERE id = ?",
            (scan_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"scan_id {scan_id} not found")
        roots = decode_string_list_json(row["roots_json"])
        extensions = decode_string_list_json(row["extensions_json"])
        return {
            "id": int(row["id"]),
            "created_at": str(row["created_at"]),
            "profile": str(row["profile"]),
            "roots": normalize_roots_for_display(roots),
            "extensions": normalize_extensions(extensions),
            "custom_similarity_threshold": normalize_custom_similarity_threshold(
                row["custom_similarity_threshold"]
            ),
            "scene_aware_sampling": bool(row["scene_aware_sampling"]),
            "audio_fingerprint_enabled": bool(row["audio_fingerprint_enabled"]),
            "cross_resolution_mode": normalize_cross_resolution_mode(
                row["cross_resolution_mode"]
            ),
            "probe_backend": str(row["probe_backend"] or "pyav"),
            "scan_set_key": str(row["scan_set_key"] or ""),
            "status": str(row["status"]),
        }

    def load_scan_links(self, scan_id: int) -> list[ScanLinkRecord]:
        """Load all persisted tracked-link rows for one scan."""
        rows = self.conn.execute(
            """
            SELECT scan_id, link_kind, link_path, target_original_path,
                   target_exists_flag, source_root
            FROM scan_links
            WHERE scan_id = ?
            ORDER BY link_path
            """,
            (int(scan_id),),
        ).fetchall()
        return [
            ScanLinkRecord(
                scan_id=int(row["scan_id"]),
                link_kind=(
                    "symlink"
                    if str(row["link_kind"] or "").strip().lower() == "symlink"
                    else "hardlink"
                ),
                link_path=str(row["link_path"]),
                target_original_path=str(row["target_original_path"]),
                target_exists=bool(row["target_exists_flag"]),
                source_root=str(row["source_root"] or ""),
            )
            for row in rows
        ]

    def latest_scan_id(self) -> int | None:
        """Return the most recent scan id, if any."""
        row = self.conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def latest_scan_id_for_set(self, scan_set_key: str) -> int | None:
        """Return the newest scan id for one scan set key."""
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
        """Return the newest completed scan id for one scan set key."""
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
        """Return the newest scan row for every non-empty scan set key."""
        rows = self.conn.execute(
            """
            SELECT s.id, s.created_at, s.profile, s.roots_json,
                   s.extensions_json, s.custom_similarity_threshold,
                   s.scene_aware_sampling, s.audio_fingerprint_enabled,
                   s.cross_resolution_mode, s.scan_set_key, s.status
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
        """Return the newest completed scan row for every non-empty scan set key."""
        rows = self.conn.execute(
            """
            SELECT s.id, s.created_at, s.profile, s.roots_json,
                   s.extensions_json, s.custom_similarity_threshold,
                   s.scene_aware_sampling, s.audio_fingerprint_enabled,
                   s.cross_resolution_mode, s.scan_set_key, s.status
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

    def _scan_set_rows_to_payload(
        self,
        rows: list[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        """Convert scan rows into the shared scan-set payload shape."""
        output: list[dict[str, Any]] = []
        for row in rows:
            roots = decode_string_list_json(row["roots_json"])
            extensions = decode_string_list_json(row["extensions_json"])
            output.append(
                {
                    "scan_id": int(row["id"]),
                    "created_at": str(row["created_at"]),
                    "profile": normalize_similarity_profile(
                        str(row["profile"] or "balanced")
                    ),
                    "roots": normalize_roots_for_display(roots),
                    "extensions": normalize_extensions(extensions),
                    "custom_similarity_threshold": (
                        normalize_custom_similarity_threshold(
                            row["custom_similarity_threshold"]
                        )
                    ),
                    "scene_aware_sampling": bool(row["scene_aware_sampling"]),
                    "audio_fingerprint_enabled": bool(row["audio_fingerprint_enabled"]),
                    "cross_resolution_mode": normalize_cross_resolution_mode(
                        row["cross_resolution_mode"]
                    ),
                    "scan_set_key": str(row["scan_set_key"] or ""),
                    "status": str(row["status"] or ""),
                }
            )
        return output

    def scan_summary(self, scan_id: int) -> dict[str, Any]:
        """Return duplicate-group summary counters for one scan."""
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
