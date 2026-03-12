from __future__ import annotations

import subprocess
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from threep_commons.desktop import open_path_in_default_app, reveal_path_in_file_manager

from ..quality import codec_rank
from .thumbnails import (
    normalize_frame_pair,
    normalize_thumbnail_size_key,
    opencv_available,
    thumbnail_dimensions,
    thumbnail_pair_cache_paths,
)
from .workers import ExactMatchGroupWorker, ThumbnailPairWorker

if TYPE_CHECKING:
    from ..models import DuplicateGroup, DuplicateItem

RESULTS_HEADERS = [
    "Group ID",
    "Checkbox",
    "=",
    "Thumbnail",
    "File Name",
    "Size (Bytes)",
    "Resolution",
    "Duration",
    "Video Codec",
    "Audio Codec",
    "Audio Bitrate",
    "Audio Lang(s)",
    "Sub Lang(s)",
    "HDR",
    "Bitrate",
    "Similarity",
    "Last Modified",
    "Parent Dir",
    "Full Path",
]

COL_GROUP_ID = 0
COL_CHECK = 1
COL_IDENTICAL = 2
COL_THUMB = 3
COL_FILE_NAME = 4
COL_SIZE = 5
COL_RESOLUTION = 6
COL_DURATION = 7
COL_VIDEO_CODEC = 8
COL_AUDIO_CODEC = 9
COL_AUDIO_BITRATE = 10
COL_AUDIO_LANGS = 11
COL_SUB_LANGS = 12
COL_HDR = 13
COL_BITRATE = 14
COL_SIMILARITY = 15
COL_LAST_MODIFIED = 16
COL_PARENT_DIR = 17
COL_FULL_PATH = 18

META_ROLE = Qt.ItemDataRole.UserRole
THUMB_GAP = 6
SORT_NONE = "none"
SORT_GROUP_SIZE_DESC = "group_size_desc"
SORT_GROUP_SIZE_ASC = "group_size_asc"
SORT_GROUP_COUNT_DESC = "group_count_desc"
SORT_GROUP_COUNT_ASC = "group_count_asc"
SORT_ROW_SIZE_DESC = "row_size_desc"
SORT_ROW_SIZE_ASC = "row_size_asc"
SORT_GROUP_SPREAD_DESC = "group_spread_desc"
SORT_GROUP_SPREAD_ASC = "group_spread_asc"
VALID_SORT_MODES = {
    SORT_NONE,
    SORT_GROUP_SIZE_DESC,
    SORT_GROUP_SIZE_ASC,
    SORT_GROUP_COUNT_DESC,
    SORT_GROUP_COUNT_ASC,
    SORT_ROW_SIZE_DESC,
    SORT_ROW_SIZE_ASC,
    SORT_GROUP_SPREAD_DESC,
    SORT_GROUP_SPREAD_ASC,
}


@dataclass(slots=True)
class RowMeta:
    group_db_id: int
    file_id: int
    path: str
    size: int
    mtime_ns: int
    width: int
    height: int
    codec: str
    bitrate: int
    similarity: float
    keep_default: bool


class ResultsView(QWidget):
    delete_requested = Signal(str, object)  # mode, list[dict]
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._groups: list[DuplicateGroup] = []
        self._sort_mode = SORT_NONE
        self._filter_include_name = ""
        self._filter_include_path = ""
        self._filter_exclude_name = ""
        self._filter_exclude_path = ""
        self._thumbnail_size_key = "96x54"
        self._thumbnail_w, self._thumbnail_h = thumbnail_dimensions(
            self._thumbnail_size_key
        )
        self._frame_a_pct, self._frame_b_pct = normalize_frame_pair(23, 77)
        self._thumbnail_serial = 0
        self._thumbnail_token = "rows-0"
        self._thumbnail_rows: dict[int, int] = {}
        self._thumbnail_workers: dict[int, ThumbnailPairWorker] = {}
        self._thumbnails_enabled = opencv_available()
        self._thread_pool = QThreadPool.globalInstance()
        self._column_widths: list[int] = []
        self._applying_column_widths = False
        self._mediainfo_missing_notified = False
        self._checked_file_ids: set[int] = set()
        self._rebuilding_table = False
        self._scan_context_note = ""
        self._identical_block_mib = 1
        self._identical_sample_a_pct, self._identical_sample_b_pct = (
            self._normalize_identical_sample_pair(23, 78)
        )
        self._dataset_serial = 0
        self._dataset_token = "groups-0"
        self._group_compare_payloads: dict[str, list[dict[str, object]]] = {}
        self._row_group_keys: dict[int, str] = {}
        self._group_rows_visible: dict[str, list[int]] = {}
        self._group_compare_pending: deque[str] = deque()
        self._group_compare_pending_set: set[str] = set()
        self._group_compare_running_key: str | None = None
        self._group_compare_cached_labels: dict[str, dict[int, str]] = {}
        self._group_compare_cached_errors: dict[str, dict[int, str]] = {}
        self._group_compare_cached_group_error: dict[str, str] = {}
        self._group_compare_workers: dict[int, ExactMatchGroupWorker] = {}

        self.info_label = QLabel("No scan loaded", self)
        self.thumbnail_note = QLabel("", self)
        self.thumbnail_note.setVisible(False)
        if not self._thumbnails_enabled:
            self.thumbnail_note.setText(
                "Thumbnail previews disabled: opencv-python is not installed."
            )
            self.thumbnail_note.setVisible(True)

        self.filter_toolbar = QWidget(self)
        include_row = QHBoxLayout()
        include_row.setContentsMargins(0, 0, 0, 0)
        include_row.addWidget(QLabel("Filter (Include)", self.filter_toolbar))
        include_row.addWidget(QLabel("File Name Contains", self.filter_toolbar))
        self.filter_include_name_edit = QLineEdit(self.filter_toolbar)
        include_row.addWidget(self.filter_include_name_edit, stretch=1)
        include_row.addWidget(QLabel("Path Contains", self.filter_toolbar))
        self.filter_include_path_edit = QLineEdit(self.filter_toolbar)
        include_row.addWidget(self.filter_include_path_edit, stretch=1)

        exclude_row = QHBoxLayout()
        exclude_row.setContentsMargins(0, 0, 0, 0)
        exclude_row.addWidget(QLabel("Filter (Exclude)", self.filter_toolbar))
        exclude_row.addWidget(QLabel("File Name Contains", self.filter_toolbar))
        self.filter_exclude_name_edit = QLineEdit(self.filter_toolbar)
        exclude_row.addWidget(self.filter_exclude_name_edit, stretch=1)
        exclude_row.addWidget(QLabel("Path Contains", self.filter_toolbar))
        self.filter_exclude_path_edit = QLineEdit(self.filter_toolbar)
        exclude_row.addWidget(self.filter_exclude_path_edit, stretch=1)

        filter_layout = QVBoxLayout(self.filter_toolbar)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.addLayout(include_row)
        filter_layout.addLayout(exclude_row)

        self.results_table = QTableWidget(0, len(RESULTS_HEADERS), self)
        self.results_table.setHorizontalHeaderLabels(RESULTS_HEADERS)
        self.results_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.results_table.setSelectionMode(
            QTableWidget.SelectionMode.ExtendedSelection
        )
        self.results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results_table.setIconSize(
            QSize(self._combined_thumbnail_width(), self._thumbnail_h)
        )
        self.results_table.horizontalHeader().sectionResized.connect(
            self._on_column_resized
        )
        self.results_table.verticalScrollBar().valueChanged.connect(
            self._on_results_scrolled
        )
        self.results_table.itemChanged.connect(self._on_item_changed)

        self.filter_include_name_edit.textChanged.connect(self._on_filter_changed)
        self.filter_include_path_edit.textChanged.connect(self._on_filter_changed)
        self.filter_exclude_name_edit.textChanged.connect(self._on_filter_changed)
        self.filter_exclude_path_edit.textChanged.connect(self._on_filter_changed)

        self._install_shortcuts()

        layout = QVBoxLayout(self)
        layout.addWidget(self.info_label)
        layout.addWidget(self.thumbnail_note)
        layout.addWidget(self.filter_toolbar)
        layout.addWidget(self.results_table, stretch=1)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._schedule_visible_groups_for_compare)

    def _install_shortcuts(self) -> None:
        self._shortcut_delete = QShortcut(QKeySequence("Delete"), self.results_table)
        self._shortcut_delete.activated.connect(self.request_soft_delete_selected)
        self._shortcut_shift_delete = QShortcut(
            QKeySequence("Shift+Delete"), self.results_table
        )
        self._shortcut_shift_delete.activated.connect(
            self.request_permanent_delete_selected
        )
        self._shortcut_enter = QShortcut(QKeySequence("Return"), self.results_table)
        self._shortcut_enter.activated.connect(self.open_current_in_default_player)
        self._shortcut_enter_num = QShortcut(QKeySequence("Enter"), self.results_table)
        self._shortcut_enter_num.activated.connect(self.open_current_in_default_player)
        self._shortcut_explore = QShortcut(QKeySequence("E"), self.results_table)
        self._shortcut_explore.activated.connect(self.explore_current_file)
        self._shortcut_mediainfo = QShortcut(QKeySequence("M"), self.results_table)
        self._shortcut_mediainfo.activated.connect(self.launch_mediainfo)

    def set_column_widths(self, widths: list[int]) -> None:
        self._column_widths = self._normalize_column_widths(
            widths, expected_count=self.results_table.columnCount()
        )
        if self._column_widths:
            self._set_table_column_widths(self._column_widths)

    def column_widths(self) -> list[int]:
        return self._column_widths or self._capture_column_widths()

    def fit_columns_to_contents(self) -> None:
        self.results_table.resizeColumnsToContents()
        self._column_widths = self._capture_column_widths()

    def column_labels(self) -> list[str]:
        return list(RESULTS_HEADERS)

    def set_column_visibility(self, visibility: list[bool]) -> None:
        normalized = self._normalize_column_visibility(
            visibility, expected_count=self.results_table.columnCount()
        )
        if not normalized:
            return
        for index, visible in enumerate(normalized):
            self.results_table.setColumnHidden(index, not visible)

    def column_visibility(self) -> list[bool]:
        return [
            not self.results_table.isColumnHidden(index)
            for index in range(self.results_table.columnCount())
        ]

    def set_column_visible(self, index: int, visible: bool) -> None:
        if index < 0 or index >= self.results_table.columnCount():
            return
        if not visible:
            current = self.column_visibility()
            if sum(1 for x in current if x) <= 1 and current[index]:
                return
        self.results_table.setColumnHidden(index, not visible)

    def set_thumbnail_size(self, size_key: str) -> None:
        normalized = normalize_thumbnail_size_key(size_key)
        if normalized == self._thumbnail_size_key:
            return
        self._thumbnail_size_key = normalized
        self._thumbnail_w, self._thumbnail_h = thumbnail_dimensions(normalized)
        self.results_table.setIconSize(
            QSize(self._combined_thumbnail_width(), self._thumbnail_h)
        )
        if self._groups:
            self._rebuild_results_table()

    def set_thumbnail_frame_positions(self, frame_a_pct: int, frame_b_pct: int) -> None:
        a, b = normalize_frame_pair(frame_a_pct, frame_b_pct)
        if a == self._frame_a_pct and b == self._frame_b_pct:
            return
        self._frame_a_pct, self._frame_b_pct = a, b
        if self._groups:
            self._rebuild_results_table()

    def set_identical_compare_config(
        self, block_mib: int, sample_a_pct: int, sample_b_pct: int
    ) -> None:
        normalized_block = self._normalize_identical_block_mib(block_mib)
        normalized_a, normalized_b = self._normalize_identical_sample_pair(
            sample_a_pct, sample_b_pct
        )
        if (
            normalized_block == self._identical_block_mib
            and normalized_a == self._identical_sample_a_pct
            and normalized_b == self._identical_sample_b_pct
        ):
            return
        self._identical_block_mib = normalized_block
        self._identical_sample_a_pct = normalized_a
        self._identical_sample_b_pct = normalized_b
        self._invalidate_group_compare_dataset()
        self._group_compare_payloads = self._build_group_compare_payloads(self._groups)
        if self._groups:
            self._rebuild_results_table()

    @staticmethod
    def _normalize_identical_block_mib(value: object) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, min(64, parsed))

    @staticmethod
    def _normalize_identical_sample_pair(
        a_value: object, b_value: object
    ) -> tuple[int, int]:
        def _normalize_percent(value: object, default: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                parsed = default
            return max(0, min(100, parsed))

        a = _normalize_percent(a_value, 23)
        b = _normalize_percent(b_value, 78)
        if a == b:
            if b < 100:
                b += 1
            else:
                a = max(0, a - 1)
        if a > b:
            a, b = b, a
        return a, b

    def set_sort_mode(self, mode: str) -> None:
        normalized = mode if mode in VALID_SORT_MODES else SORT_NONE
        if normalized == self._sort_mode:
            return
        self._sort_mode = normalized
        self._rebuild_results_table()

    def sort_mode(self) -> str:
        return self._sort_mode

    def load_groups(self, groups: list[DuplicateGroup]) -> None:
        self._groups = list(groups)
        self._checked_file_ids = set()
        self._invalidate_group_compare_dataset()
        self._group_compare_payloads = self._build_group_compare_payloads(self._groups)
        self._rebuild_results_table()

    def set_scan_context_note(self, note: str) -> None:
        self._scan_context_note = str(note or "").strip()
        self._update_info_label()

    def _on_filter_changed(self, _text: str) -> None:
        self._filter_include_name = (
            self.filter_include_name_edit.text().strip().casefold()
        )
        self._filter_include_path = (
            self.filter_include_path_edit.text().strip().casefold()
        )
        self._filter_exclude_name = (
            self.filter_exclude_name_edit.text().strip().casefold()
        )
        self._filter_exclude_path = (
            self.filter_exclude_path_edit.text().strip().casefold()
        )
        self._rebuild_results_table()

    def _rebuild_results_table(self) -> None:
        self._invalidate_thumbnail_token()
        self._clear_group_compare_row_state()
        self._clear_group_compare_pending()
        self.results_table.setRowCount(0)
        self._rebuilding_table = True
        try:
            display_groups = self._display_groups()
            for group_index, group in enumerate(display_groups, start=1):
                group_db_id = int(group.group_id or 0)
                display_group_id = f"G{group_index:04d}"
                group_key = self._group_key(group)
                cached_labels = self._group_compare_cached_labels.get(group_key, {})
                cached_errors = self._group_compare_cached_errors.get(group_key, {})
                cached_group_error = self._group_compare_cached_group_error.get(
                    group_key, ""
                )
                self._group_rows_visible.setdefault(group_key, [])
                for item in group.items:
                    row = self.results_table.rowCount()
                    self.results_table.insertRow(row)
                    self.results_table.setRowHeight(row, self._thumbnail_h + 8)

                    meta = RowMeta(
                        group_db_id=group_db_id,
                        file_id=item.file_id,
                        path=item.path,
                        size=item.size,
                        mtime_ns=item.mtime_ns,
                        width=item.width,
                        height=item.height,
                        codec=item.codec,
                        bitrate=item.bitrate,
                        similarity=item.similarity_score,
                        keep_default=item.keep_default,
                    )

                    group_item = QTableWidgetItem(display_group_id)
                    group_item.setData(META_ROLE, meta)
                    self.results_table.setItem(row, COL_GROUP_ID, group_item)

                    check_item = QTableWidgetItem("")
                    check_item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    check_item.setCheckState(
                        Qt.CheckState.Checked
                        if item.file_id in self._checked_file_ids
                        else Qt.CheckState.Unchecked
                    )
                    self.results_table.setItem(row, COL_CHECK, check_item)

                    identical_text = cached_labels.get(item.file_id, "")
                    identical_item = QTableWidgetItem(identical_text)
                    identical_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    tooltip = cached_errors.get(item.file_id, "") or cached_group_error
                    if tooltip:
                        identical_item.setToolTip(tooltip)
                    self.results_table.setItem(row, COL_IDENTICAL, identical_item)

                    thumb_item = QTableWidgetItem(
                        "Loading..." if self._thumbnails_enabled else "N/A"
                    )
                    thumb_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.results_table.setItem(row, COL_THUMB, thumb_item)

                    file_path = Path(item.path)
                    self.results_table.setItem(
                        row, COL_FILE_NAME, QTableWidgetItem(file_path.name)
                    )
                    self.results_table.setItem(
                        row, COL_SIZE, QTableWidgetItem(f"{item.size:,}")
                    )
                    self.results_table.setItem(
                        row,
                        COL_RESOLUTION,
                        QTableWidgetItem(f"{item.width}x{item.height}"),
                    )
                    self.results_table.setItem(
                        row, COL_DURATION, QTableWidgetItem(f"{item.duration_s:.1f}s")
                    )
                    self.results_table.setItem(
                        row, COL_VIDEO_CODEC, QTableWidgetItem(item.codec)
                    )
                    self.results_table.setItem(
                        row, COL_AUDIO_CODEC, QTableWidgetItem(item.audio_codec or "")
                    )
                    self.results_table.setItem(
                        row,
                        COL_AUDIO_BITRATE,
                        QTableWidgetItem(str(item.audio_bitrate)),
                    )
                    self.results_table.setItem(
                        row,
                        COL_AUDIO_LANGS,
                        QTableWidgetItem(item.audio_languages or ""),
                    )
                    self.results_table.setItem(
                        row,
                        COL_SUB_LANGS,
                        QTableWidgetItem(item.subtitle_languages or ""),
                    )
                    self.results_table.setItem(
                        row, COL_HDR, QTableWidgetItem("Yes" if item.is_hdr else "No")
                    )
                    self.results_table.setItem(
                        row, COL_BITRATE, QTableWidgetItem(str(item.bitrate))
                    )
                    self.results_table.setItem(
                        row,
                        COL_SIMILARITY,
                        QTableWidgetItem(f"{item.similarity_score:.3f}"),
                    )
                    self.results_table.setItem(
                        row,
                        COL_LAST_MODIFIED,
                        QTableWidgetItem(self._fmt_mtime(item.mtime_ns)),
                    )
                    self.results_table.setItem(
                        row, COL_PARENT_DIR, QTableWidgetItem(str(file_path.parent))
                    )
                    self.results_table.setItem(
                        row, COL_FULL_PATH, QTableWidgetItem(item.path)
                    )

                    self._row_group_keys[row] = group_key
                    self._group_rows_visible[group_key].append(row)

                    self._apply_row_style(
                        row=row, group_index=group_index, bold=item.keep_default
                    )
                    self._thumbnail_rows[item.file_id] = row
                    self._queue_thumbnail(
                        row_token=self._thumbnail_token,
                        row=row,
                        file_id=item.file_id,
                        path=item.path,
                        size=item.size,
                        mtime_ns=item.mtime_ns,
                    )
        finally:
            self._rebuilding_table = False
        if self._column_widths:
            self._set_table_column_widths(self._column_widths)
        else:
            self.results_table.resizeColumnsToContents()
            self._column_widths = self._capture_column_widths()

        self._update_info_label()
        QTimer.singleShot(0, self._schedule_visible_groups_for_compare)

    def _display_groups(self) -> list[DuplicateGroup]:
        filtered = self._filtered_groups(self._groups)
        if self._sort_mode == SORT_GROUP_SIZE_DESC:
            return sorted(
                filtered,
                key=lambda group: (
                    -self._group_total_size(group),
                    -len(group.items),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_GROUP_SIZE_ASC:
            return sorted(
                filtered,
                key=lambda group: (
                    self._group_total_size(group),
                    len(group.items),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_GROUP_COUNT_DESC:
            return sorted(
                filtered,
                key=lambda group: (
                    -len(group.items),
                    -self._group_total_size(group),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_GROUP_COUNT_ASC:
            return sorted(
                filtered,
                key=lambda group: (
                    len(group.items),
                    self._group_total_size(group),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_ROW_SIZE_DESC:
            return [
                replace(
                    group,
                    items=self._sorted_group_items(group.items, larger_first=True),
                )
                for group in filtered
            ]
        if self._sort_mode == SORT_ROW_SIZE_ASC:
            return [
                replace(
                    group,
                    items=self._sorted_group_items(group.items, larger_first=False),
                )
                for group in filtered
            ]
        if self._sort_mode == SORT_GROUP_SPREAD_DESC:
            return sorted(
                filtered,
                key=lambda group: (
                    -self._group_size_spread(group),
                    -len(group.items),
                    -self._group_total_size(group),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_GROUP_SPREAD_ASC:
            return sorted(
                filtered,
                key=lambda group: (
                    self._group_size_spread(group),
                    len(group.items),
                    self._group_total_size(group),
                    self._group_tiebreak(group),
                ),
            )
        return filtered

    def _filtered_groups(self, groups: list[DuplicateGroup]) -> list[DuplicateGroup]:
        filtered_groups: list[DuplicateGroup] = []
        for group in groups:
            items = [item for item in group.items if self._item_matches_filters(item)]
            if not items:
                continue
            filtered_groups.append(replace(group, items=items))
        return filtered_groups

    def _item_matches_filters(self, item: DuplicateItem) -> bool:
        file_name = Path(item.path).name.casefold()
        full_path = item.path.casefold()

        if self._filter_include_name and self._filter_include_name not in file_name:
            return False
        if self._filter_include_path and self._filter_include_path not in full_path:
            return False
        if self._filter_exclude_name and self._filter_exclude_name in file_name:
            return False
        return not (
            self._filter_exclude_path and self._filter_exclude_path in full_path
        )

    def _sorted_group_items(
        self, items: list[DuplicateItem], larger_first: bool
    ) -> list[DuplicateItem]:
        if larger_first:
            return sorted(
                items,
                key=lambda item: (
                    -item.size,
                    -self._quality_score_for_item(item),
                    item.mtime_ns,
                    item.path.lower(),
                ),
            )
        return sorted(
            items,
            key=lambda item: (
                item.size,
                -self._quality_score_for_item(item),
                item.mtime_ns,
                item.path.lower(),
            ),
        )

    @staticmethod
    def _group_total_size(group: DuplicateGroup) -> int:
        return sum(item.size for item in group.items)

    @staticmethod
    def _group_size_spread(group: DuplicateGroup) -> int:
        if not group.items:
            return 0
        sizes = [item.size for item in group.items]
        return max(sizes) - min(sizes)

    @staticmethod
    def _group_tiebreak(group: DuplicateGroup) -> str:
        if not group.items:
            return ""
        return min(item.path.casefold() for item in group.items)

    def _group_key(self, group: DuplicateGroup) -> str:
        group_db_id = int(group.group_id or 0)
        if group_db_id > 0:
            return f"id:{group_db_id}"
        file_ids = sorted(int(item.file_id) for item in group.items)
        return "files:" + ",".join(str(file_id) for file_id in file_ids)

    def _build_group_compare_payloads(
        self, groups: list[DuplicateGroup]
    ) -> dict[str, list[dict[str, object]]]:
        payloads: dict[str, list[dict[str, object]]] = {}
        for group in groups:
            group_key = self._group_key(group)
            payloads[group_key] = [
                {
                    "file_id": int(item.file_id),
                    "path": str(item.path),
                    "size": int(item.size),
                }
                for item in group.items
            ]
        return payloads

    def _invalidate_group_compare_dataset(self) -> None:
        self._dataset_serial += 1
        self._dataset_token = f"groups-{self._dataset_serial}"
        self._reset_group_compare_cache()
        self._clear_group_compare_row_state()
        self._clear_group_compare_pending()
        self._group_compare_running_key = None

    def _reset_group_compare_cache(self) -> None:
        self._group_compare_cached_labels = {}
        self._group_compare_cached_errors = {}
        self._group_compare_cached_group_error = {}

    def _clear_group_compare_row_state(self) -> None:
        self._row_group_keys = {}
        self._group_rows_visible = {}

    def _clear_group_compare_pending(self) -> None:
        self._group_compare_pending = deque()
        self._group_compare_pending_set = set()

    def _cancel_group_compare_queue(self) -> None:
        self._clear_group_compare_pending()
        self._group_compare_running_key = None

    def _on_results_scrolled(self, _value: int) -> None:
        self._schedule_visible_groups_for_compare()

    def _schedule_visible_groups_for_compare(self) -> None:
        if self.results_table.rowCount() <= 0:
            return
        viewport = self.results_table.viewport()
        if viewport.height() <= 0:
            return
        first_row = self.results_table.rowAt(0)
        if first_row < 0:
            first_row = 0
        last_row = self.results_table.rowAt(max(0, viewport.height() - 1))
        if last_row < 0:
            last_row = self.results_table.rowCount() - 1

        for row in range(first_row, last_row + 1):
            group_key = self._row_group_keys.get(row)
            if not group_key:
                continue
            if (
                group_key in self._group_compare_cached_labels
                or group_key in self._group_compare_cached_group_error
            ):
                continue
            if (
                group_key == self._group_compare_running_key
                or group_key in self._group_compare_pending_set
            ):
                continue
            payload = self._group_compare_payloads.get(group_key, [])
            if len(payload) < 2:
                self._group_compare_cached_labels[group_key] = {}
                self._group_compare_cached_errors[group_key] = {}
                continue
            self._group_compare_pending.append(group_key)
            self._group_compare_pending_set.add(group_key)

        self._start_next_group_compare()

    def _start_next_group_compare(self) -> None:
        if self._group_compare_running_key is not None:
            return
        while self._group_compare_pending:
            group_key = self._group_compare_pending.popleft()
            self._group_compare_pending_set.discard(group_key)
            if (
                group_key in self._group_compare_cached_labels
                or group_key in self._group_compare_cached_group_error
            ):
                continue
            payload = self._group_compare_payloads.get(group_key, [])
            if len(payload) < 2:
                self._group_compare_cached_labels[group_key] = {}
                self._group_compare_cached_errors[group_key] = {}
                continue

            worker = ExactMatchGroupWorker(
                row_token=self._dataset_token,
                group_key=group_key,
                files=payload,
                block_mib=self._identical_block_mib,
                sample_a_pct=self._identical_sample_a_pct,
                sample_b_pct=self._identical_sample_b_pct,
            )
            worker.signals.ready.connect(self._on_group_compare_ready)
            worker.signals.error.connect(self._on_group_compare_error)
            self._group_compare_workers[worker.worker_id] = worker
            self._group_compare_running_key = group_key
            self._thread_pool.start(worker)
            return

    def _on_group_compare_ready(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        worker_id = int(payload.get("worker_id", 0))
        self._group_compare_workers.pop(worker_id, None)

        token = str(payload.get("token", ""))
        if token != self._dataset_token:
            return

        group_key = str(payload.get("group_key", ""))
        raw_labels = payload.get("labels", {})
        raw_errors = payload.get("errors", {})
        labels: dict[int, str] = {}
        errors: dict[int, str] = {}
        if isinstance(raw_labels, dict):
            for raw_file_id, raw_label in raw_labels.items():
                try:
                    file_id = int(raw_file_id)
                except (TypeError, ValueError):
                    continue
                labels[file_id] = str(raw_label or "")
        if isinstance(raw_errors, dict):
            for raw_file_id, raw_error in raw_errors.items():
                try:
                    file_id = int(raw_file_id)
                except (TypeError, ValueError):
                    continue
                error_text = str(raw_error or "").strip()
                if error_text:
                    errors[file_id] = error_text

        self._group_compare_cached_labels[group_key] = labels
        self._group_compare_cached_errors[group_key] = errors
        self._group_compare_cached_group_error.pop(group_key, None)
        self._group_compare_running_key = None
        self._apply_group_compare_result(group_key)
        self._start_next_group_compare()

    def _on_group_compare_error(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        worker_id = int(payload.get("worker_id", 0))
        self._group_compare_workers.pop(worker_id, None)

        token = str(payload.get("token", ""))
        if token != self._dataset_token:
            return

        group_key = str(payload.get("group_key", ""))
        self._group_compare_cached_labels[group_key] = {}
        self._group_compare_cached_errors[group_key] = {}
        self._group_compare_cached_group_error[group_key] = str(
            payload.get("message", "")
        ).strip()
        self._group_compare_running_key = None
        self._apply_group_compare_result(group_key)
        self._start_next_group_compare()

    def _apply_group_compare_result(self, group_key: str) -> None:
        rows = self._group_rows_visible.get(group_key, [])
        if not rows:
            return
        labels = self._group_compare_cached_labels.get(group_key, {})
        errors = self._group_compare_cached_errors.get(group_key, {})
        group_error = self._group_compare_cached_group_error.get(group_key, "")
        for row in rows:
            meta = self._row_meta(row)
            if meta is None:
                continue
            item = self.results_table.item(row, COL_IDENTICAL)
            if item is None:
                item = QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.results_table.setItem(row, COL_IDENTICAL, item)
            item.setText(labels.get(meta.file_id, ""))
            tooltip = errors.get(meta.file_id, "") or group_error
            item.setToolTip(tooltip)

    def _invalidate_thumbnail_token(self) -> None:
        self._thumbnail_serial += 1
        self._thumbnail_token = f"rows-{self._thumbnail_serial}"
        self._thumbnail_rows = {}

    def _combined_thumbnail_width(self) -> int:
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
        if not isinstance(payload, dict):
            return
        worker_id = int(payload.get("worker_id", 0))
        self._thumbnail_workers.pop(worker_id, None)

        token = str(payload.get("token", ""))
        if token != self._thumbnail_token:
            return
        file_id = int(payload.get("file_id", 0))
        row = self._thumbnail_rows.get(file_id)
        if row is None or row < 0 or row >= self.results_table.rowCount():
            return
        item = self.results_table.item(row, COL_THUMB)
        if item is None:
            return

        cache_a = str(payload.get("cache_path_a", ""))
        cache_b = str(payload.get("cache_path_b", ""))
        composed = self._compose_thumbnail_pair(cache_a, cache_b)
        if composed is None:
            self._set_thumbnail_text(row, "Error", "failed to load thumbnails")
            return

        item.setText("")
        item.setData(Qt.ItemDataRole.DecorationRole, composed)
        item.setToolTip(f"{cache_a}\n{cache_b}")
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _on_thumbnail_error(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        worker_id = int(payload.get("worker_id", 0))
        self._thumbnail_workers.pop(worker_id, None)

        token = str(payload.get("token", ""))
        if token != self._thumbnail_token:
            return
        file_id = int(payload.get("file_id", 0))
        row = self._thumbnail_rows.get(file_id)
        if row is None or row < 0 or row >= self.results_table.rowCount():
            return
        self._set_thumbnail_text(row, "Error", str(payload.get("message", "")))

    def _compose_thumbnail_pair(self, cache_a: str, cache_b: str) -> QPixmap | None:
        pix_a = QPixmap(cache_a)
        pix_b = QPixmap(cache_b)
        if pix_a.isNull() or pix_b.isNull():
            return None
        w = self._combined_thumbnail_width()
        h = self._thumbnail_h
        canvas = QPixmap(w, h)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        try:
            scaled_a = pix_a.scaled(
                self._thumbnail_w,
                h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled_b = pix_b.scaled(
                self._thumbnail_w,
                h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            off_a_x = (self._thumbnail_w - scaled_a.width()) // 2
            off_a_y = (h - scaled_a.height()) // 2
            base_b_x = self._thumbnail_w + THUMB_GAP
            off_b_x = base_b_x + (self._thumbnail_w - scaled_b.width()) // 2
            off_b_y = (h - scaled_b.height()) // 2
            painter.drawPixmap(off_a_x, off_a_y, scaled_a)
            painter.drawPixmap(off_b_x, off_b_y, scaled_b)
        finally:
            painter.end()
        return canvas

    def _set_thumbnail_text(self, row: int, text: str, tooltip: str = "") -> None:
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
        bg_color = QColor("#f2f7fc") if group_index % 2 == 1 else QColor("#ecf2e7")
        brush = QBrush(bg_color)
        for col in range(self.results_table.columnCount()):
            item = self.results_table.item(row, col)
            if item is None:
                continue
            item.setBackground(brush)
            font = item.font() if item.font() is not None else QFont()
            font.setBold(bold)
            item.setFont(font)

    def _row_meta(self, row: int) -> RowMeta | None:
        if row < 0 or row >= self.results_table.rowCount():
            return None
        item = self.results_table.item(row, COL_GROUP_ID)
        if item is None:
            return None
        payload = item.data(META_ROLE)
        if isinstance(payload, RowMeta):
            return payload
        return None

    def _group_rows(self) -> dict[int, list[int]]:
        grouped: dict[int, list[int]] = {}
        for row in range(self.results_table.rowCount()):
            meta = self._row_meta(row)
            if meta is None:
                continue
            grouped.setdefault(meta.group_db_id, []).append(row)
        return grouped

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
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
        return self._quality_score_for_dimensions(
            meta.width, meta.height, meta.bitrate, meta.codec
        )

    def _quality_score_for_item(self, item: DuplicateItem) -> float:
        return self._quality_score_for_dimensions(
            item.width, item.height, item.bitrate, item.codec
        )

    @staticmethod
    def _quality_score_for_dimensions(
        width: int, height: int, bitrate: int, codec: str
    ) -> float:
        # Same scoring model used in duplicate "keep best" decision.
        pixels = float(width * height)
        return 0.65 * pixels + 0.25 * float(bitrate) + 0.10 * codec_rank(codec)

    def apply_keep_strategy(self, strategy: str) -> None:
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
        self._emit_delete_request(mode="rename")

    def request_permanent_delete_selected(self) -> None:
        self._emit_delete_request(mode="permanent")

    def _emit_delete_request(self, mode: str) -> None:
        targets: list[dict[str, Any]] = []
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
        row = self.results_table.currentRow()
        if row < 0:
            return None
        item = self.results_table.item(row, COL_FULL_PATH)
        if item is None:
            return None
        return item.text()

    def open_current_in_default_player(self) -> None:
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
        path = self.current_file_path()
        if not path:
            return
        try:
            subprocess.Popen(["mediainfo", path])
            self.status_message.emit("Launched MediaInfo.")
        except FileNotFoundError:
            if not self._mediainfo_missing_notified:
                QMessageBox.warning(
                    self, "MediaInfo Missing", "mediainfo executable not found on PATH."
                )
                self._mediainfo_missing_notified = True
            self.status_message.emit("mediainfo is not installed or not on PATH.")
        except Exception as exc:
            QMessageBox.warning(self, "MediaInfo Failed", str(exc))
            self.status_message.emit("Failed to launch mediainfo.")

    def remove_file_by_id(self, file_id: int) -> None:
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
        if self._applying_column_widths:
            return
        self._column_widths = self._capture_column_widths()

    def _set_table_column_widths(self, widths: list[int]) -> None:
        if len(widths) != self.results_table.columnCount():
            return
        self._applying_column_widths = True
        try:
            for index, width in enumerate(widths):
                self.results_table.setColumnWidth(index, width)
        finally:
            self._applying_column_widths = False

    def _capture_column_widths(self) -> list[int]:
        return [
            self.results_table.columnWidth(index)
            for index in range(self.results_table.columnCount())
        ]

    @staticmethod
    def _normalize_column_widths(widths: list[int], expected_count: int) -> list[int]:
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
        visibility: list[bool], expected_count: int
    ) -> list[bool]:
        if len(visibility) != expected_count:
            return []
        normalized = [bool(v) for v in visibility]
        if not any(normalized):
            return [True] * expected_count
        return normalized

    @staticmethod
    def _fmt_mtime(mtime_ns: int) -> str:
        if mtime_ns <= 0:
            return ""
        dt = datetime.fromtimestamp(float(mtime_ns) / 1_000_000_000.0)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
