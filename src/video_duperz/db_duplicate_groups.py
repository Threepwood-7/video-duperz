"""Persistence helpers for duplicate-group rows and actionable file state."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .db_shared import normalize_action_kind, require_lastrowid
from .models import DuplicateGroup, DuplicateItem, normalize_match_reason, utc_now_iso

if TYPE_CHECKING:
    import sqlite3


class DatabaseDuplicateGroupMixin:
    """Duplicate-group persistence and result-loading helpers."""

    conn: sqlite3.Connection

    def _commit_if_needed(self) -> None:
        """Commit immediately when the connection is not in a scan transaction."""
        raise NotImplementedError

    def insert_duplicate_group(
        self,
        scan_id: int,
        profile: str,
        total_size_bytes: int,
    ) -> int:
        """Insert one duplicate-group header row and return its id."""
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
        """Insert or update one duplicate-group item row."""
        self.conn.execute(
            """
            INSERT INTO duplicate_group_items(
              group_id, file_id, similarity_score, keep_default,
              match_reason, match_duration_delta_s, selected_action
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, file_id) DO UPDATE SET
              similarity_score = excluded.similarity_score,
              keep_default = excluded.keep_default,
              match_reason = excluded.match_reason,
              match_duration_delta_s = excluded.match_duration_delta_s,
              selected_action = excluded.selected_action
            """,
            (
                group_id,
                item.file_id,
                item.similarity_score,
                1 if item.keep_default else 0,
                item.match_reason,
                item.match_duration_delta_s,
                item.selected_action,
            ),
        )
        self._commit_if_needed()

    def insert_duplicate_groups_batch(
        self,
        scan_id: int,
        profile: str,
        groups: list[DuplicateGroup],
    ) -> list[int]:
        """Insert duplicate groups and their member rows in one batch."""
        if not groups:
            return []
        created_at = utc_now_iso()
        group_ids: list[int] = []
        item_rows: list[tuple[int, int, float, int, str, float, str]] = []
        for group in groups:
            cursor = self.conn.execute(
                """
                INSERT INTO duplicate_groups(
                  scan_id, profile, created_at, total_size_bytes
                )
                VALUES(?, ?, ?, ?)
                """,
                (int(scan_id), str(profile), created_at, int(group.total_size_bytes)),
            )
            group_id = require_lastrowid(cursor)
            group_ids.append(group_id)
            for item in group.items:
                item_rows.append(
                    (
                        group_id,
                        int(item.file_id),
                        float(item.similarity_score),
                        1 if item.keep_default else 0,
                        str(item.match_reason),
                        float(item.match_duration_delta_s),
                        str(item.selected_action),
                    )
                )
        if item_rows:
            self.conn.executemany(
                """
                INSERT INTO duplicate_group_items(
                  group_id, file_id, similarity_score, keep_default,
                  match_reason, match_duration_delta_s, selected_action
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, file_id) DO UPDATE SET
                  similarity_score = excluded.similarity_score,
                  keep_default = excluded.keep_default,
                  match_reason = excluded.match_reason,
                  match_duration_delta_s = excluded.match_duration_delta_s,
                  selected_action = excluded.selected_action
                """,
                item_rows,
            )
        self._commit_if_needed()
        return group_ids

    def update_group_item_actions(self, group_id: int, actions: dict[int, str]) -> None:
        """Update selected actions for all matching group items."""
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
        """Return file ids to current paths for one duplicate group."""
        rows = self.conn.execute(
            """
            SELECT gi.file_id, f.path
            FROM duplicate_group_items gi
            JOIN files f ON f.id = gi.file_id
            WHERE gi.group_id = ?
            """,
            (group_id,),
        ).fetchall()
        return {int(row["file_id"]): str(row["path"]) for row in rows}

    def refresh_file_after_rename(self, file_id: int, new_path: str) -> None:
        """Refresh stored file stats after a successful rename action."""
        target = Path(new_path)
        stat_result = target.stat()
        ext = target.suffix.lower().lstrip(".")
        self.conn.execute(
            """
            UPDATE files
            SET path = ?, size = ?, mtime_ns = ?, ext = ?, exists_flag = 1
            WHERE id = ?
            """,
            (
                str(target),
                int(stat_result.st_size),
                int(stat_result.st_mtime_ns),
                ext,
                file_id,
            ),
        )
        self._commit_if_needed()

    def mark_file_missing(self, file_id: int) -> None:
        """Mark one file row as missing on disk."""
        self.conn.execute("UPDATE files SET exists_flag = 0 WHERE id = ?", (file_id,))
        self._commit_if_needed()

    def remove_file_from_duplicate_groups(self, file_id: int) -> None:
        """Delete duplicate-group membership rows for one file."""
        self.conn.execute(
            "DELETE FROM duplicate_group_items WHERE file_id = ?",
            (file_id,),
        )
        self._commit_if_needed()

    def prune_duplicate_groups(self, scan_id: int) -> list[int]:
        """Delete groups that no longer contain at least two existing files."""
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
        group_ids = [int(row["group_id"]) for row in rows]
        for group_id in group_ids:
            self.conn.execute("DELETE FROM duplicate_groups WHERE id = ?", (group_id,))
        self._commit_if_needed()
        return group_ids

    def load_duplicate_groups(self, scan_id: int) -> list[DuplicateGroup]:
        """Load duplicate groups with their current live member rows."""
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
        for group_row in group_rows:
            item_rows = self.conn.execute(
                """
                SELECT gi.file_id, gi.similarity_score,
                       gi.keep_default, gi.match_reason,
                       gi.match_duration_delta_s, gi.selected_action,
                       f.path, f.size, f.mtime_ns, f.ctime_ns,
                       vm.duration_s, vm.width, vm.height, vm.fps,
                       vm.bit_depth, vm.hdr_format, vm.container,
                       vm.codec_profile, vm.codec_level, vm.is_interlaced,
                       vm.bitrate, vm.codec, vm.audio_stream_count,
                       vm.audio_codec, vm.audio_bitrate,
                       vm.audio_languages, vm.subtitle_languages
                FROM duplicate_group_items gi
                JOIN files f ON f.id = gi.file_id
                JOIN scans s ON s.id = ?
                JOIN video_meta vm
                  ON vm.file_id = f.id AND vm.probe_backend = s.probe_backend
                WHERE gi.group_id = ? AND f.exists_flag = 1
                ORDER BY gi.keep_default DESC,
                         vm.width * vm.height DESC,
                         vm.bitrate DESC
                """,
                (int(group_row["scan_id"]), int(group_row["id"])),
            ).fetchall()
            items = [
                DuplicateItem(
                    file_id=int(item_row["file_id"]),
                    path=str(item_row["path"]),
                    size=int(item_row["size"]),
                    mtime_ns=int(item_row["mtime_ns"]),
                    ctime_ns=int(item_row["ctime_ns"]),
                    duration_s=float(item_row["duration_s"]),
                    width=int(item_row["width"]),
                    height=int(item_row["height"]),
                    fps=float(item_row["fps"]),
                    bit_depth=int(item_row["bit_depth"] or 8),
                    hdr_format=str(item_row["hdr_format"] or ""),
                    container=str(item_row["container"] or ""),
                    codec_profile=str(item_row["codec_profile"] or ""),
                    codec_level=str(item_row["codec_level"] or ""),
                    is_interlaced=bool(item_row["is_interlaced"]),
                    bitrate=int(item_row["bitrate"]),
                    codec=str(item_row["codec"]),
                    audio_stream_count=int(item_row["audio_stream_count"] or 0),
                    audio_codec=str(item_row["audio_codec"] or ""),
                    audio_bitrate=int(item_row["audio_bitrate"] or 0),
                    audio_languages=str(item_row["audio_languages"] or ""),
                    subtitle_languages=str(item_row["subtitle_languages"] or ""),
                    similarity_score=float(item_row["similarity_score"]),
                    keep_default=bool(item_row["keep_default"]),
                    match_reason=normalize_match_reason(item_row["match_reason"]),
                    match_duration_delta_s=float(
                        item_row["match_duration_delta_s"] or 0.0
                    ),
                    selected_action=normalize_action_kind(item_row["selected_action"]),
                )
                for item_row in item_rows
            ]
            if len(items) < 2:
                continue
            groups.append(
                DuplicateGroup(
                    scan_id=int(group_row["scan_id"]),
                    profile=str(group_row["profile"]),
                    created_at=str(group_row["created_at"]),
                    items=items,
                    total_size_bytes=int(group_row["total_size_bytes"]),
                    group_id=int(group_row["id"]),
                )
            )
        return groups
