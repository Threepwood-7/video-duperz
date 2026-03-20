"""Thumbnail worker orchestration for the results view."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPixmap
from PySide6.QtWidgets import QTableWidgetItem

from .results_view_compare import ResultsViewCompareMixin
from .results_view_shared import (
    COL_GROUP_ID,
    COL_THUMB,
    META_ROLE,
    THUMB_GAP,
    RowMeta,
    coerce_int,
    payload_dict,
)
from .thumbnails import thumbnail_pair_cache_paths
from .workers import ThumbnailPairWorker


class ResultsViewThumbnailMixin(ResultsViewCompareMixin):
    """Thumbnail worker orchestration and row metadata helpers."""

    @staticmethod
    def _thumbnail_source_tooltip(path: str) -> str:
        """Return the compact source label shown for thumbnail hover tooltips."""
        file_path = Path(path)
        file_name = file_path.name or path
        parent_name = file_path.parent.name or str(file_path.parent)
        if not parent_name or parent_name == ".":
            return file_name
        return f"{file_name} | {parent_name}"

    def _invalidate_thumbnail_token(self) -> None:
        """Rotate the thumbnail token so stale worker results are ignored."""
        self._thumbnail_serial += 1
        self._thumbnail_token = f"rows-{self._thumbnail_serial}"
        self._thumbnail_rows = {}

    def _combined_thumbnail_width(self) -> int:
        """Return the total width reserved for the two preview thumbnails."""
        return (self._thumbnail_w * 2) + THUMB_GAP

    def _queue_thumbnail(
        self,
        row_token: str,
        row: int,
        file_id: int,
        path: str,
        size: int,
        mtime_ns: int,
    ) -> None:
        """Queue thumbnail generation for one table row."""
        if not self._thumbnails_enabled:
            self._set_thumbnail_text(row, "N/A")
            return
        source = Path(path)
        if not source.exists():
            self._set_thumbnail_text(row, "N/A", "source file missing")
            return
        cache_a, cache_b = thumbnail_pair_cache_paths(
            path=path,
            size=size,
            mtime_ns=mtime_ns,
            size_key=self._thumbnail_size_key,
            frame_a_pct=self._frame_a_pct,
            frame_b_pct=self._frame_b_pct,
        )

        worker = ThumbnailPairWorker(
            row_token=row_token,
            file_id=file_id,
            source_path=path,
            cache_path_a=str(cache_a),
            cache_path_b=str(cache_b),
            width=self._thumbnail_w,
            height=self._thumbnail_h,
            frame_a_pct=self._frame_a_pct,
            frame_b_pct=self._frame_b_pct,
        )
        worker.signals.ready.connect(self._on_thumbnail_ready)
        worker.signals.error.connect(self._on_thumbnail_error)
        self._thumbnail_workers[worker.worker_id] = worker
        self._thread_pool.start(worker)

    def _on_thumbnail_ready(self, payload: Any) -> None:
        """Apply a successful thumbnail worker result."""
        payload_map = payload_dict(payload)
        if not payload_map:
            return
        worker_id = coerce_int(payload_map.get("worker_id", 0))
        self._thumbnail_workers.pop(worker_id, None)

        token = str(payload_map.get("token", ""))
        if token != self._thumbnail_token:
            return
        file_id = coerce_int(payload_map.get("file_id", 0))
        row = self._thumbnail_rows.get(file_id)
        if row is None or row < 0 or row >= self.results_table.rowCount():
            return
        item = self.results_table.item(row, COL_THUMB)
        if item is None:
            return

        cache_a = str(payload_map.get("cache_path_a", ""))
        cache_b = str(payload_map.get("cache_path_b", ""))
        composed = self._compose_thumbnail_pair(cache_a, cache_b)
        if composed is None:
            self._set_thumbnail_text(row, "Error", "failed to load thumbnails")
            return

        item.setText("")
        item.setData(Qt.ItemDataRole.DecorationRole, composed)
        meta = self._row_meta(row)
        item.setToolTip(
            self._thumbnail_source_tooltip(meta.path) if meta is not None else ""
        )
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _on_thumbnail_error(self, payload: Any) -> None:
        """Apply a failed thumbnail worker result."""
        payload_map = payload_dict(payload)
        if not payload_map:
            return
        worker_id = coerce_int(payload_map.get("worker_id", 0))
        self._thumbnail_workers.pop(worker_id, None)

        token = str(payload_map.get("token", ""))
        if token != self._thumbnail_token:
            return
        file_id = coerce_int(payload_map.get("file_id", 0))
        row = self._thumbnail_rows.get(file_id)
        if row is None or row < 0 or row >= self.results_table.rowCount():
            return
        self._set_thumbnail_text(row, "Error", str(payload_map.get("message", "")))

    def _compose_thumbnail_pair(self, cache_a: str, cache_b: str) -> QPixmap | None:
        """Compose the two cached thumbnail frames into one side-by-side pixmap."""
        pix_a = QPixmap(cache_a)
        pix_b = QPixmap(cache_b)
        if pix_a.isNull() or pix_b.isNull():
            return None
        width = self._combined_thumbnail_width()
        height = self._thumbnail_h
        canvas = QPixmap(width, height)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        try:
            scaled_a = pix_a.scaled(
                self._thumbnail_w,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled_b = pix_b.scaled(
                self._thumbnail_w,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            off_a_x = (self._thumbnail_w - scaled_a.width()) // 2
            off_a_y = (height - scaled_a.height()) // 2
            base_b_x = self._thumbnail_w + THUMB_GAP
            off_b_x = base_b_x + (self._thumbnail_w - scaled_b.width()) // 2
            off_b_y = (height - scaled_b.height()) // 2
            painter.drawPixmap(off_a_x, off_a_y, scaled_a)
            painter.drawPixmap(off_b_x, off_b_y, scaled_b)
        finally:
            painter.end()
        return canvas

    def _set_thumbnail_text(self, row: int, text: str, tooltip: str = "") -> None:
        """Show a text placeholder in the thumbnail cell."""
        if row < 0 or row >= self.results_table.rowCount():
            return
        item = self.results_table.item(row, COL_THUMB)
        if item is None:
            item = QTableWidgetItem("")
            self.results_table.setItem(row, COL_THUMB, item)
        item.setData(Qt.ItemDataRole.DecorationRole, None)
        item.setText(text)
        item.setToolTip(tooltip)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _apply_row_style(self, row: int, group_index: int, bold: bool) -> None:
        """Apply alternating row color and default-keeper emphasis."""
        bg_color = QColor("#f2f7fc") if group_index % 2 == 1 else QColor("#ecf2e7")
        brush = QBrush(bg_color)
        for column in range(self.results_table.columnCount()):
            item = self.results_table.item(row, column)
            if item is None:
                continue
            item.setBackground(brush)
            font = item.font()
            font.setBold(bold)
            item.setFont(font)

    def _row_meta(self, row: int) -> RowMeta | None:
        """Return the row metadata payload stored on the group id cell."""
        if row < 0 or row >= self.results_table.rowCount():
            return None
        item = self.results_table.item(row, COL_GROUP_ID)
        if item is None:
            return None
        payload = item.data(META_ROLE)
        if isinstance(payload, RowMeta):
            return payload
        return None
