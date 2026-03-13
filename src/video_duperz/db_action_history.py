"""Persistence helpers for rename/delete action-run history."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .db_shared import require_lastrowid
from .models import utc_now_iso

if TYPE_CHECKING:
    import sqlite3


class DatabaseActionHistoryMixin:
    """Action-run persistence helpers for rename and delete workflows."""

    conn: sqlite3.Connection

    def _commit_if_needed(self) -> None:
        """Commit immediately when the connection is not in a scan transaction."""
        raise NotImplementedError

    def insert_action_run(
        self,
        scan_id: int,
        mode: str,
        status: str = "running",
    ) -> int:
        """Insert one action-run row and return its id."""
        cursor = self.conn.execute(
            "INSERT INTO action_runs(scan_id, mode, created_at, status) "
            "VALUES(?, ?, ?, ?)",
            (scan_id, mode, utc_now_iso(), status),
        )
        self._commit_if_needed()
        return require_lastrowid(cursor)

    def insert_action_item(
        self,
        run_id: int,
        file_id: int,
        source_path: str,
        target_path: str | None,
        result: str,
        error_text: str | None = None,
    ) -> None:
        """Insert one action result row."""
        self.conn.execute(
            """
            INSERT INTO action_items(
              run_id, file_id, source_path, target_path, result, error_text
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (run_id, file_id, source_path, target_path, result, error_text),
        )
        self._commit_if_needed()

    def finalize_action_run(self, run_id: int, status: str) -> None:
        """Persist the final status for an action run."""
        self.conn.execute(
            "UPDATE action_runs SET status = ? WHERE id = ?",
            (status, run_id),
        )
        self._commit_if_needed()

    def fetch_file_path(self, file_id: int) -> str:
        """Load the current persisted path for a file row."""
        row = self.conn.execute(
            "SELECT path FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"file_id {file_id} not found")
        return str(row["path"])
