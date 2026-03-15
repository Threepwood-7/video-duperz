"""SQLite connection and migration helpers for the application database."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .config import db_path
from .db_shared import decode_string_list_json
from .scan_sets import (
    build_scan_set_key,
    normalize_extensions,
    normalize_roots_for_display,
    normalize_similarity_profile,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

SCHEMA_VERSION = 7


class DatabaseConnectionMixin:
    """Core SQLite connection, migration, and transaction lifecycle helpers."""

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
        """Close the underlying SQLite connection."""
        self.conn.close()

    def __enter__(self) -> Self:
        """Enter the context manager using this database instance."""
        return self

    def __exit__(self, _exc_type: object, exc: object, _tb: object) -> None:
        """Close the connection when the context manager exits."""
        self.close()

    def _apply_scan_pragmas(self) -> None:
        """Apply best-effort SQLite tuning pragmas for scan-heavy workloads."""
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
                # Keep the connection usable on limited filesystems or runtimes.
                continue

    @staticmethod
    def _iter_chunks(values: list[str], chunk_size: int = 300) -> Iterable[list[str]]:
        """Yield fixed-size chunks from a list of string values."""
        if chunk_size <= 0:
            chunk_size = 300
        for idx in range(0, len(values), chunk_size):
            yield values[idx : idx + chunk_size]

    def _commit_if_needed(self) -> None:
        """Commit immediately when no scan transaction is open."""
        if not self._scan_tx_active:
            self.conn.commit()

    def begin_scan_transaction(self) -> None:
        """Open the long-lived scan transaction when it is not already active."""
        if self._scan_tx_active:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._scan_tx_active = True

    def flush_scan_transaction(self) -> None:
        """Flush the current scan transaction while keeping it open."""
        if not self._scan_tx_active:
            return
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")

    def end_scan_transaction(self) -> None:
        """Commit and close the active scan transaction."""
        if not self._scan_tx_active:
            return
        self.conn.commit()
        self._scan_tx_active = False

    def migrate(self) -> None:
        """Create or migrate the SQLite schema to the current version."""
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
              probe_backend TEXT NOT NULL DEFAULT 'pyav',
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
            CREATE INDEX IF NOT EXISTS idx_files_path_stat
              ON files(path, size, mtime_ns);
            CREATE INDEX IF NOT EXISTS idx_files_scan_exists
              ON files(scan_id, exists_flag);
            CREATE INDEX IF NOT EXISTS idx_files_path_stat_exists
              ON files(path, size, mtime_ns, exists_flag);

            CREATE TABLE IF NOT EXISTS video_meta(
              file_id INTEGER NOT NULL,
              probe_backend TEXT NOT NULL DEFAULT 'pyav',
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
              PRIMARY KEY(file_id, probe_backend),
              FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_video_meta_file_backend
              ON video_meta(file_id, probe_backend);

            CREATE TABLE IF NOT EXISTS fingerprints(
              file_id INTEGER NOT NULL,
              probe_backend TEXT NOT NULL DEFAULT 'pyav',
              algo_version INTEGER NOT NULL,
              frame_count INTEGER NOT NULL,
              hash_blob BLOB NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(file_id, probe_backend),
              FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_fingerprints_algo
              ON fingerprints(algo_version);
            CREATE INDEX IF NOT EXISTS idx_fingerprints_file_backend
              ON fingerprints(file_id, probe_backend);

            CREATE TABLE IF NOT EXISTS analysis_issues(
              file_id INTEGER NOT NULL,
              probe_backend TEXT NOT NULL DEFAULT 'pyav',
              stage TEXT NOT NULL,
              message TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(file_id, probe_backend, stage),
              FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_issues_scan_backend
              ON analysis_issues(file_id, probe_backend);

            CREATE TABLE IF NOT EXISTS duplicate_groups(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scan_id INTEGER NOT NULL,
              profile TEXT NOT NULL,
              created_at TEXT NOT NULL,
              total_size_bytes INTEGER NOT NULL,
              FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_duplicate_groups_scan
              ON duplicate_groups(scan_id);

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
            CREATE INDEX IF NOT EXISTS idx_group_items_file
              ON duplicate_group_items(file_id);

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
        self._ensure_backend_scoped_cache_tables()
        self._ensure_analysis_issue_table()
        self._ensure_scan_columns()
        self._backfill_scan_set_keys()
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _ensure_backend_scoped_cache_tables(self) -> None:
        """Rebuild cache tables so every probe backend can store rows per file."""
        video_meta_pk = {
            str(row["name"]): int(row["pk"])
            for row in self.conn.execute("PRAGMA table_info(video_meta)").fetchall()
        }
        if video_meta_pk.get("file_id") != 1 or video_meta_pk.get("probe_backend") != 2:
            self.conn.executescript(
                """
                CREATE TABLE video_meta_new(
                  file_id INTEGER NOT NULL,
                  probe_backend TEXT NOT NULL DEFAULT 'pyav',
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
                  PRIMARY KEY(file_id, probe_backend),
                  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
                );
                INSERT INTO video_meta_new(
                  file_id, probe_backend, duration_s, width, height, fps,
                  codec, bitrate, has_audio, audio_codec, audio_bitrate,
                  audio_languages,
                  subtitle_languages, is_hdr, probe_error
                )
                SELECT
                  file_id,
                  CASE
                    WHEN TRIM(COALESCE(probe_backend, '')) = '' THEN 'ffprobe'
                    ELSE probe_backend
                  END,
                  duration_s,
                  width,
                  height,
                  fps,
                  codec,
                  bitrate,
                  has_audio,
                  audio_codec,
                  audio_bitrate,
                  audio_languages,
                  subtitle_languages,
                  is_hdr,
                  probe_error
                FROM video_meta;
                DROP TABLE video_meta;
                ALTER TABLE video_meta_new RENAME TO video_meta;
                CREATE INDEX idx_video_meta_file_backend
                  ON video_meta(file_id, probe_backend);
                """
            )

        fingerprints_pk = {
            str(row["name"]): int(row["pk"])
            for row in self.conn.execute("PRAGMA table_info(fingerprints)").fetchall()
        }
        if (
            fingerprints_pk.get("file_id") != 1
            or fingerprints_pk.get("probe_backend") != 2
        ):
            self.conn.executescript(
                """
                CREATE TABLE fingerprints_new(
                  file_id INTEGER NOT NULL,
                  probe_backend TEXT NOT NULL DEFAULT 'pyav',
                  algo_version INTEGER NOT NULL,
                  frame_count INTEGER NOT NULL,
                  hash_blob BLOB NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(file_id, probe_backend),
                  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
                );
                INSERT INTO fingerprints_new(
                  file_id, probe_backend, algo_version, frame_count,
                  hash_blob, created_at
                )
                SELECT
                  file_id,
                  CASE
                    WHEN TRIM(COALESCE(probe_backend, '')) = '' THEN 'ffprobe'
                    ELSE probe_backend
                  END,
                  algo_version,
                  frame_count,
                  hash_blob,
                  created_at
                FROM fingerprints;
                DROP TABLE fingerprints;
                ALTER TABLE fingerprints_new RENAME TO fingerprints;
                CREATE INDEX idx_fingerprints_algo
                  ON fingerprints(algo_version);
                CREATE INDEX idx_fingerprints_file_backend
                  ON fingerprints(file_id, probe_backend);
                """
            )

    def _ensure_analysis_issue_table(self) -> None:
        """Create or repair the persisted analysis-issue table."""
        columns = {
            str(row["name"]): int(row["pk"])
            for row in self.conn.execute(
                "PRAGMA table_info(analysis_issues)"
            ).fetchall()
        }
        if not columns:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_issues(
                  file_id INTEGER NOT NULL,
                  probe_backend TEXT NOT NULL DEFAULT 'pyav',
                  stage TEXT NOT NULL,
                  message TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(file_id, probe_backend, stage),
                  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_issues_scan_backend
                  ON analysis_issues(file_id, probe_backend);
                """
            )
            return
        if (
            columns.get("file_id") == 1
            and columns.get("probe_backend") == 2
            and columns.get("stage") == 3
        ):
            return
        self.conn.executescript(
            """
            CREATE TABLE analysis_issues_new(
              file_id INTEGER NOT NULL,
              probe_backend TEXT NOT NULL DEFAULT 'pyav',
              stage TEXT NOT NULL,
              message TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(file_id, probe_backend, stage),
              FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
            );
            INSERT INTO analysis_issues_new(
              file_id, probe_backend, stage, message, created_at
            )
            SELECT
              file_id,
              CASE
                WHEN TRIM(COALESCE(probe_backend, '')) = '' THEN 'pyav'
                ELSE probe_backend
              END,
              stage,
              message,
              created_at
            FROM analysis_issues;
            DROP TABLE analysis_issues;
            ALTER TABLE analysis_issues_new RENAME TO analysis_issues;
            CREATE INDEX idx_analysis_issues_scan_backend
              ON analysis_issues(file_id, probe_backend);
            """
        )

    def _ensure_video_meta_columns(self) -> None:
        """Add newly introduced video metadata columns to legacy databases."""
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(video_meta)").fetchall()
        }
        if "probe_backend" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN probe_backend TEXT NOT NULL "
                "DEFAULT 'ffprobe'"
            )
        self.conn.execute(
            "UPDATE video_meta SET probe_backend = 'ffprobe' "
            "WHERE TRIM(COALESCE(probe_backend, '')) = ''"
        )
        if "audio_codec" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN audio_codec TEXT NOT NULL DEFAULT ''"
            )
        if "audio_bitrate" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN audio_bitrate INTEGER "
                "NOT NULL DEFAULT 0"
            )
        if "audio_languages" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN audio_languages TEXT "
                "NOT NULL DEFAULT ''"
            )
        if "subtitle_languages" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN subtitle_languages TEXT "
                "NOT NULL DEFAULT ''"
            )
        if "is_hdr" not in columns:
            self.conn.execute(
                "ALTER TABLE video_meta ADD COLUMN is_hdr INTEGER NOT NULL DEFAULT 0"
            )
        fp_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(fingerprints)").fetchall()
        }
        if "probe_backend" not in fp_columns:
            self.conn.execute(
                "ALTER TABLE fingerprints "
                "ADD COLUMN probe_backend TEXT NOT NULL DEFAULT 'ffprobe'"
            )
        self.conn.execute(
            "UPDATE fingerprints SET probe_backend = 'ffprobe' "
            "WHERE TRIM(COALESCE(probe_backend, '')) = ''"
        )

    def _ensure_scan_columns(self) -> None:
        """Add newly introduced scan columns and indexes to legacy databases."""
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(scans)").fetchall()
        }
        if "extensions_json" not in columns:
            self.conn.execute(
                "ALTER TABLE scans ADD COLUMN extensions_json TEXT "
                "NOT NULL DEFAULT '[]'"
            )
        if "probe_backend" not in columns:
            self.conn.execute(
                "ALTER TABLE scans ADD COLUMN probe_backend TEXT NOT NULL "
                "DEFAULT 'ffprobe'"
            )
        if "scan_set_key" not in columns:
            self.conn.execute(
                "ALTER TABLE scans ADD COLUMN scan_set_key TEXT NOT NULL DEFAULT ''"
            )
        self.conn.execute(
            "UPDATE scans SET probe_backend = 'ffprobe' "
            "WHERE TRIM(COALESCE(probe_backend, '')) = ''"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scans_set_status "
            "ON scans(scan_set_key, status, id DESC)"
        )

    def _backfill_scan_set_keys(self) -> None:
        """Populate scan-set keys for legacy rows that predate that column."""
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
            roots = normalize_roots_for_display(decode_string_list_json(raw_roots))
            extensions = normalize_extensions(decode_string_list_json(raw_extensions))
            profile = normalize_similarity_profile(str(row["profile"] or "balanced"))
            scan_set_key = build_scan_set_key(
                roots=roots,
                similarity_profile=profile,
                extensions=extensions,
            )
            self.conn.execute(
                "UPDATE scans SET scan_set_key = ? WHERE id = ?",
                (scan_set_key, int(row["id"])),
            )
