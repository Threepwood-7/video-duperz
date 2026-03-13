"""Selection, launch, and table-state helpers for the results view."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QTableWidgetItem
from threep_commons.desktop import open_path_in_default_app, reveal_path_in_file_manager

from ..quality import codec_rank
from .results_view_shared import COL_CHECK, COL_FULL_PATH, DeleteTarget, RowMeta
from .results_view_thumbnail import ResultsViewThumbnailMixin

if TYPE_CHECKING:
    from ..models import DuplicateGroup, DuplicateItem


class ResultsViewActionMixin(ResultsViewThumbnailMixin):
    """Selection, launch, and table-state helper methods."""

    def _group_rows(self) -> dict[int, list[int]]:
        """Group visible row indexes by duplicate group id."""
        grouped: dict[int, list[int]] = {}
        for row in range(self.results_table.rowCount()):
            meta = self._row_meta(row)
            if meta is None:
                continue
            grouped.setdefault(meta.group_db_id, []).append(row)
        return grouped

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Track checkbox changes in the selected-file id set."""
        if self._rebuilding_table:
            return
        if item.column() != COL_CHECK:
            return
        meta = self._row_meta(item.row())
        if meta is None:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._checked_file_ids.add(meta.file_id)
        else:
            self._checked_file_ids.discard(meta.file_id)

    def _quality_score_for_row(self, meta: RowMeta) -> float:
        """Return the keep-best heuristic score for one rendered row."""
        return self._quality_score_for_dimensions(
            meta.width,
            meta.height,
            meta.bitrate,
            meta.codec,
        )

    def _quality_score_for_item(self, item: DuplicateItem) -> float:
        """Return the keep-best heuristic score for one duplicate item."""
        return self._quality_score_for_dimensions(
            item.width,
            item.height,
            item.bitrate,
            item.codec,
        )

    @staticmethod
    def _quality_score_for_dimensions(
        width: int,
        height: int,
        bitrate: int,
        codec: str,
    ) -> float:
        """Score one video using the same heuristic as the keep-best action."""
        pixels = float(width * height)
        return 0.65 * pixels + 0.25 * float(bitrate) + 0.10 * codec_rank(codec)

    def apply_keep_strategy(self, strategy: str) -> None:
        """Check every row except the chosen keeper in each visible group."""
        grouped = self._group_rows()
        if not grouped:
            return
        keep_rows: set[int] = set()

        for rows in grouped.values():
            if not rows:
                continue
            metas = [(row, self._row_meta(row)) for row in rows]
            metas = [(row, meta) for row, meta in metas if meta is not None]
            if not metas:
                continue

            if strategy == "best":
                keeper = sorted(
                    metas,
                    key=lambda pair: (
                        -self._quality_score_for_row(pair[1]),
                        pair[1].mtime_ns,
                        pair[1].path.lower(),
                    ),
                )[0][0]
            elif strategy == "worst":
                keeper = sorted(
                    metas,
                    key=lambda pair: (
                        self._quality_score_for_row(pair[1]),
                        pair[1].mtime_ns,
                        pair[1].path.lower(),
                    ),
                )[0][0]
            elif strategy == "larger":
                keeper = sorted(
                    metas,
                    key=lambda pair: (
                        -pair[1].size,
                        -self._quality_score_for_row(pair[1]),
                        pair[1].path.lower(),
                    ),
                )[0][0]
            elif strategy == "smaller":
                keeper = sorted(
                    metas,
                    key=lambda pair: (
                        pair[1].size,
                        -self._quality_score_for_row(pair[1]),
                        pair[1].path.lower(),
                    ),
                )[0][0]
            elif strategy == "newer":
                keeper = sorted(
                    metas,
                    key=lambda pair: (
                        -pair[1].mtime_ns,
                        -self._quality_score_for_row(pair[1]),
                        pair[1].path.lower(),
                    ),
                )[0][0]
            elif strategy == "older":
                keeper = sorted(
                    metas,
                    key=lambda pair: (
                        pair[1].mtime_ns,
                        -self._quality_score_for_row(pair[1]),
                        pair[1].path.lower(),
                    ),
                )[0][0]
            else:
                return
            keep_rows.add(keeper)

        for row in range(self.results_table.rowCount()):
            check_item = self.results_table.item(row, COL_CHECK)
            if check_item is None:
                continue
            check_item.setCheckState(
                Qt.CheckState.Unchecked if row in keep_rows else Qt.CheckState.Checked
            )

    def request_soft_delete_selected(self) -> None:
        """Emit a rename-to-delete-bin style request for selected rows."""
        self._emit_delete_request(mode="rename")

    def request_permanent_delete_selected(self) -> None:
        """Emit a permanent-delete request for selected rows."""
        self._emit_delete_request(mode="permanent")

    def _emit_delete_request(self, mode: str) -> None:
        """Collect selected rows and emit the delete request signal."""
        targets: list[DeleteTarget] = []
        for row in self._target_rows_for_delete():
            meta = self._row_meta(row)
            if meta is None:
                continue
            targets.append(
                {
                    "row": row,
                    "file_id": meta.file_id,
                    "group_db_id": meta.group_db_id,
                    "path": meta.path,
                }
            )
        if not targets:
            self.status_message.emit("No rows selected for delete.")
            return
        self.delete_requested.emit(mode, targets)

    def _target_rows_for_delete(self) -> list[int]:
        """Return checked rows, or fall back to the current row when needed."""
        checked_rows: list[int] = []
        for row in range(self.results_table.rowCount()):
            check_item = self.results_table.item(row, COL_CHECK)
            if (
                check_item is not None
                and check_item.checkState() == Qt.CheckState.Checked
            ):
                checked_rows.append(row)
        if checked_rows:
            return checked_rows
        current = self.results_table.currentRow()
        return [current] if current >= 0 else []

    def current_file_path(self) -> str | None:
        """Return the file path of the current row, if any."""
        row = self.results_table.currentRow()
        if row < 0:
            return None
        item = self.results_table.item(row, COL_FULL_PATH)
        if item is None:
            return None
        return item.text()

    def open_current_in_default_player(self) -> None:
        """Open the current file with the system default handler."""
        path = self.current_file_path()
        if not path:
            return
        target = Path(path)
        if not target.exists():
            self.status_message.emit("Selected file does not exist.")
            return
        try:
            if not open_path_in_default_app(target):
                raise RuntimeError("No default opener available on this platform")
            self.status_message.emit(f"Opened {target.name}")
        except Exception as exc:
            QMessageBox.warning(self, "Open Failed", str(exc))
            self.status_message.emit("Failed to open file.")

    def explore_current_file(self) -> None:
        """Reveal the current file in the system file manager."""
        path = self.current_file_path()
        if not path:
            return
        target = Path(path)
        if not target.exists():
            self.status_message.emit("Selected file does not exist.")
            return
        try:
            if not reveal_path_in_file_manager(target):
                raise RuntimeError("No file manager available on this platform")
            self.status_message.emit(f"Exploring {target.parent}")
        except Exception as exc:
            QMessageBox.warning(self, "Explore Failed", str(exc))
            self.status_message.emit("Failed to open explorer.")

    def launch_mediainfo(self) -> None:
        """Launch the current file in MediaInfo when available."""
        path = self.current_file_path()
        if not path:
            return
        try:
            subprocess.Popen(["mediainfo", path])
            self.status_message.emit("Launched MediaInfo.")
        except FileNotFoundError:
            if not self._mediainfo_missing_notified:
                QMessageBox.warning(
                    self,
                    "MediaInfo Missing",
                    "mediainfo executable not found on PATH.",
                )
                self._mediainfo_missing_notified = True
            self.status_message.emit("mediainfo is not installed or not on PATH.")
        except Exception as exc:
            QMessageBox.warning(self, "MediaInfo Failed", str(exc))
            self.status_message.emit("Failed to launch mediainfo.")

    def remove_file_by_id(self, file_id: int) -> None:
        """Remove one file from the loaded groups and rebuild if anything changed."""
        if file_id <= 0:
            return
        self._checked_file_ids.discard(file_id)
        changed = False
        updated_groups: list[DuplicateGroup] = []
        for group in self._groups:
            original_count = len(group.items)
            kept_items = [item for item in group.items if item.file_id != file_id]
            if len(kept_items) == original_count:
                updated_groups.append(group)
                continue
            changed = True
            if len(kept_items) >= 2:
                updated_groups.append(replace(group, items=kept_items))
        if not changed:
            return
        self._groups = updated_groups
        self._invalidate_group_compare_dataset()
        self._group_compare_payloads = self._build_group_compare_payloads(self._groups)
        self._rebuild_results_table()

    def _update_info_label(self) -> None:
        """Refresh the summary label above the results table."""
        grouped = self._group_rows()
        group_count = len(grouped)
        row_count = self.results_table.rowCount()
        if row_count == 0:
            base = "No duplicate groups for this scan"
        else:
            base = f"Loaded {group_count} groups / {row_count} files"
        if self._scan_context_note:
            base = f"{base} | {self._scan_context_note}"
        self.info_label.setText(base)

    def _on_column_resized(self, _section: int, _old_size: int, _new_size: int) -> None:
        """Persist live column widths when the user resizes the table."""
        if self._applying_column_widths:
            return
        self._column_widths = self._capture_column_widths()

    def _set_table_column_widths(self, widths: list[int]) -> None:
        """Apply a complete column-width payload to the results table."""
        if len(widths) != self.results_table.columnCount():
            return
        self._applying_column_widths = True
        try:
            for index, width in enumerate(widths):
                self.results_table.setColumnWidth(index, width)
        finally:
            self._applying_column_widths = False

    def _capture_column_widths(self) -> list[int]:
        """Capture the current live column widths from the table."""
        return [
            self.results_table.columnWidth(index)
            for index in range(self.results_table.columnCount())
        ]

    @staticmethod
    def _normalize_column_widths(widths: list[int], expected_count: int) -> list[int]:
        """Validate and normalize a persisted column-width payload."""
        if len(widths) != expected_count:
            return []
        normalized: list[int] = []
        for raw in widths:
            try:
                width = int(raw)
            except (TypeError, ValueError):
                return []
            if width < 0:
                return []
            normalized.append(width)
        return normalized

    @staticmethod
    def _normalize_column_visibility(
        visibility: list[bool],
        expected_count: int,
    ) -> list[bool]:
        """Validate and normalize a persisted column-visibility payload."""
        if len(visibility) != expected_count:
            return []
        normalized = [bool(value) for value in visibility]
        if not any(normalized):
            return [True] * expected_count
        return normalized

    @staticmethod
    def _fmt_mtime(mtime_ns: int) -> str:
        """Format nanosecond mtime values for display."""
        if mtime_ns <= 0:
            return ""
        dt = datetime.fromtimestamp(float(mtime_ns) / 1_000_000_000.0)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
