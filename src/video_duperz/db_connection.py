"""SQLite connection, schema setup, and transaction lifecycle helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Self

from .config import db_path

SCHEMA_VERSION = 16


_DROP_SCHEMA_SQL = """
DROP TABLE IF EXISTS action_items;
DROP TABLE IF EXISTS action_runs;
DROP TABLE IF EXISTS duplicate_group_items;
DROP TABLE IF EXISTS duplicate_groups;
DROP TABLE IF EXISTS fingerprint_decoder_provenance;
DROP TABLE IF EXISTS audio_fingerprints;
DROP TABLE IF EXISTS fingerprints;
DROP TABLE IF EXISTS video_meta;
DROP TABLE IF EXISTS scan_failed_files;
DROP TABLE IF EXISTS scan_issues;
DROP TABLE IF EXISTS scan_links;
DROP TABLE IF EXISTS files;
DROP TABLE IF EXISTS scans;
"""


_CREATE_SCHEMA_SQL = """
CREATE TABLE scans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  profile TEXT NOT NULL,
  roots_json TEXT NOT NULL,
  extensions_json TEXT NOT NULL DEFAULT '[]',
  custom_similarity_threshold REAL NOT NULL DEFAULT 0.18,
  scene_aware_sampling INTEGER NOT NULL DEFAULT 0,
  audio_fingerprint_enabled INTEGER NOT NULL DEFAULT 0,
  cross_resolution_mode TEXT NOT NULL DEFAULT 'off',
  probe_backend TEXT NOT NULL DEFAULT 'pyav',
  scan_set_key TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL
);
CREATE INDEX idx_scans_set_status ON scans(scan_set_key, status, id DESC);

CREATE TABLE files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  ctime_ns INTEGER NOT NULL,
  ext TEXT NOT NULL,
  scan_id INTEGER NOT NULL,
  exists_flag INTEGER NOT NULL DEFAULT 1,
  UNIQUE(scan_id, path),
  FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
);
CREATE INDEX idx_files_scan_id ON files(scan_id);
CREATE INDEX idx_files_scan_exists ON files(scan_id, exists_flag);
CREATE INDEX idx_files_path_stat ON files(path, size, mtime_ns);
CREATE INDEX idx_files_path_stat_exists ON files(path, size, mtime_ns, exists_flag);
CREATE INDEX idx_files_path_scan ON files(path, scan_id);

CREATE TABLE video_meta(
  file_id INTEGER NOT NULL,
  probe_backend TEXT NOT NULL DEFAULT 'pyav',
  probed_at TEXT NOT NULL,
  source_size INTEGER NOT NULL,
  source_mtime_ns INTEGER NOT NULL,
  duration_s REAL NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  fps REAL NOT NULL,
  bit_depth INTEGER NOT NULL DEFAULT 8,
  hdr_format TEXT NOT NULL DEFAULT '',
  container TEXT NOT NULL DEFAULT '',
  codec_profile TEXT NOT NULL DEFAULT '',
  codec_level TEXT NOT NULL DEFAULT '',
  is_interlaced INTEGER NOT NULL DEFAULT 0,
  codec TEXT NOT NULL,
  bitrate INTEGER NOT NULL,
  audio_stream_count INTEGER NOT NULL DEFAULT 0,
  audio_codec TEXT NOT NULL DEFAULT '',
  audio_bitrate INTEGER NOT NULL DEFAULT 0,
  audio_languages TEXT NOT NULL DEFAULT '',
  subtitle_languages TEXT NOT NULL DEFAULT '',
  probe_error TEXT,
  PRIMARY KEY(file_id, probe_backend),
  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);
CREATE INDEX idx_video_meta_file_backend
  ON video_meta(file_id, probe_backend);

CREATE TABLE fingerprints(
  file_id INTEGER NOT NULL,
  probe_backend TEXT NOT NULL DEFAULT 'pyav',
  algo_version INTEGER NOT NULL,
  source_size INTEGER NOT NULL,
  source_mtime_ns INTEGER NOT NULL,
  frame_count INTEGER NOT NULL,
  hash_blob BLOB NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(file_id, probe_backend, algo_version),
  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);
CREATE INDEX idx_fingerprints_algo ON fingerprints(algo_version);
CREATE INDEX idx_fingerprints_file_backend
  ON fingerprints(file_id, probe_backend, algo_version);

CREATE TABLE fingerprint_decoder_provenance(
  file_id INTEGER NOT NULL,
  probe_backend TEXT NOT NULL DEFAULT 'pyav',
  algo_version INTEGER NOT NULL,
  decoder_backend TEXT NOT NULL,
  attempts_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  PRIMARY KEY(file_id, probe_backend, algo_version),
  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);
CREATE INDEX idx_fingerprint_decoder_provenance_file_backend
  ON fingerprint_decoder_provenance(file_id, probe_backend, algo_version);

CREATE TABLE audio_fingerprints(
  file_id INTEGER NOT NULL,
  source_size INTEGER NOT NULL,
  source_mtime_ns INTEGER NOT NULL,
  fingerprint_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(file_id),
  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);
CREATE INDEX idx_audio_fingerprints_file ON audio_fingerprints(file_id);

CREATE TABLE duplicate_groups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL,
  profile TEXT NOT NULL,
  created_at TEXT NOT NULL,
  total_size_bytes INTEGER NOT NULL,
  FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
);
CREATE INDEX idx_duplicate_groups_scan ON duplicate_groups(scan_id);

CREATE TABLE duplicate_group_items(
  group_id INTEGER NOT NULL,
  file_id INTEGER NOT NULL,
  similarity_score REAL NOT NULL,
  keep_default INTEGER NOT NULL,
  match_reason TEXT NOT NULL DEFAULT 'perceptual',
  match_duration_delta_s REAL NOT NULL DEFAULT 0.0,
  selected_action TEXT NOT NULL,
  PRIMARY KEY(group_id, file_id),
  FOREIGN KEY(group_id) REFERENCES duplicate_groups(id) ON DELETE CASCADE,
  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);
CREATE INDEX idx_group_items_file ON duplicate_group_items(file_id);

CREATE TABLE action_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL,
  mode TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
);

CREATE TABLE action_items(
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

CREATE TABLE scan_issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  stage TEXT NOT NULL,
  path TEXT NOT NULL,
  message TEXT NOT NULL,
  FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
);
CREATE INDEX idx_scan_issues_scan_id ON scan_issues(scan_id, id);

CREATE TABLE scan_failed_files(
  scan_id INTEGER NOT NULL,
  normalized_path TEXT NOT NULL,
  display_path TEXT NOT NULL,
  stage TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(scan_id, normalized_path),
  FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
);
CREATE INDEX idx_scan_failed_files_scan_id
  ON scan_failed_files(scan_id, normalized_path);

CREATE TABLE scan_links(
  scan_id INTEGER NOT NULL,
  link_kind TEXT NOT NULL,
  link_path TEXT NOT NULL,
  target_original_path TEXT NOT NULL,
  target_exists_flag INTEGER NOT NULL DEFAULT 0,
  source_root TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(scan_id, link_path),
  FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
);
CREATE INDEX idx_scan_links_scan_kind_path
  ON scan_links(scan_id, link_kind, link_path);
"""


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
                continue

    @staticmethod
    def _iter_chunks(values: list[str], chunk_size: int = 300) -> list[list[str]]:
        """Yield fixed-size chunks from a list of string values."""
        if chunk_size <= 0:
            chunk_size = 300
        return [
            values[idx : idx + chunk_size] for idx in range(0, len(values), chunk_size)
        ]

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
        """Reset legacy databases and create the canonical snapshot schema."""
        current = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if current == SCHEMA_VERSION:
            return
        self.conn.executescript(_DROP_SCHEMA_SQL)
        self.conn.executescript(_CREATE_SCHEMA_SQL)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()
