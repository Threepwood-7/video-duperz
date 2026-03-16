"""Base widget and shared state for the results view."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, QThreadPool, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .results_view_shared import (
    RESULTS_HEADERS,
    SORT_NONE,
    VALID_SORT_MODES,
    coerce_int,
)
from .thumbnails import (
    normalize_frame_pair,
    normalize_thumbnail_size_key,
    opencv_available,
    thumbnail_dimensions,
)

if TYPE_CHECKING:
    from ..models import DuplicateGroup


class ResultsViewBase(QWidget):
    """Base results widget that owns shared state and top-level controls."""

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
        self._frame_a_pct: int
        self._frame_b_pct: int
        self._frame_a_pct, self._frame_b_pct = normalize_frame_pair(23, 77)
        self._thumbnail_serial = 0
        self._thumbnail_token = "rows-0"
        self._thumbnail_rows: dict[int, int] = {}
        self._thumbnail_workers: dict[int, object] = {}
        self._thumbnails_enabled = opencv_available()
        self._thread_pool = QThreadPool.globalInstance()
        self._column_widths: list[int] = []
        self._applying_column_widths = False
        self._mediainfo_missing_notified = False
        self._mediainfo_exe_path = ""
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
        self._group_compare_workers: dict[int, object] = {}

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

    def _combined_thumbnail_width(self) -> int: ...

    def _on_column_resized(
        self,
        _section: int,
        _old_size: int,
        _new_size: int,
    ) -> None: ...

    def _on_results_scrolled(self, _value: int) -> None: ...

    def _on_item_changed(self, item: QTableWidgetItem) -> None: ...

    def _schedule_visible_groups_for_compare(self) -> None: ...

    def request_soft_delete_selected(self) -> None: ...

    def request_permanent_delete_selected(self) -> None: ...

    def open_current_in_default_player(self) -> None: ...

    def explore_current_file(self) -> None: ...

    def launch_mediainfo(self) -> None: ...

    @staticmethod
    def _normalize_column_widths(
        widths: list[int],
        expected_count: int,
    ) -> list[int]: ...

    def _set_table_column_widths(self, widths: list[int]) -> None: ...

    def _capture_column_widths(self) -> list[int]: ...

    @staticmethod
    def _normalize_column_visibility(
        visibility: list[bool],
        expected_count: int,
    ) -> list[bool]: ...

    def _rebuild_results_table(self) -> None: ...

    def _invalidate_group_compare_dataset(self) -> None: ...

    def _build_group_compare_payloads(
        self,
        groups: list[DuplicateGroup],
    ) -> dict[str, list[dict[str, object]]]: ...

    def _update_info_label(self) -> None: ...

    def showEvent(self, event: QShowEvent) -> None:
        """Kick deferred compare scheduling once the widget is shown."""
        super().showEvent(event)
        QTimer.singleShot(0, self._schedule_visible_groups_for_compare)

    def _install_shortcuts(self) -> None:
        """Register keyboard shortcuts for the results table actions."""
        self._shortcut_delete = QShortcut(QKeySequence("Delete"), self.results_table)
        self._shortcut_delete.activated.connect(self.request_soft_delete_selected)
        self._shortcut_shift_delete = QShortcut(
            QKeySequence("Shift+Delete"),
            self.results_table,
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
        """Apply persisted column widths when the payload is well formed."""
        self._column_widths = self._normalize_column_widths(
            widths,
            expected_count=self.results_table.columnCount(),
        )
        if self._column_widths:
            self._set_table_column_widths(self._column_widths)

    def column_widths(self) -> list[int]:
        """Return stored column widths or capture them live from the table."""
        return self._column_widths or self._capture_column_widths()

    def fit_columns_to_contents(self) -> None:
        """Resize all visible columns to their current contents."""
        self.results_table.resizeColumnsToContents()
        self._column_widths = self._capture_column_widths()

    def column_labels(self) -> list[str]:
        """Return the stable set of results table column labels."""
        return list(RESULTS_HEADERS)

    def set_column_visibility(self, visibility: list[bool]) -> None:
        """Apply persisted column visibility when the payload is well formed."""
        normalized = self._normalize_column_visibility(
            visibility,
            expected_count=self.results_table.columnCount(),
        )
        if not normalized:
            return
        for index, visible in enumerate(normalized):
            self.results_table.setColumnHidden(index, not visible)

    def column_visibility(self) -> list[bool]:
        """Return whether each table column is currently visible."""
        return [
            not self.results_table.isColumnHidden(index)
            for index in range(self.results_table.columnCount())
        ]

    def set_column_visible(self, index: int, visible: bool) -> None:
        """Toggle one column while always keeping at least one column visible."""
        if index < 0 or index >= self.results_table.columnCount():
            return
        if not visible:
            current = self.column_visibility()
            if sum(1 for value in current if value) <= 1 and current[index]:
                return
        self.results_table.setColumnHidden(index, not visible)

    def set_thumbnail_size(self, size_key: str) -> None:
        """Update the thumbnail preview size and rebuild visible rows when needed."""
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
        """Update the two frame sample positions used for thumbnail previews."""
        frame_a, frame_b = normalize_frame_pair(frame_a_pct, frame_b_pct)
        if frame_a == self._frame_a_pct and frame_b == self._frame_b_pct:
            return
        self._frame_a_pct, self._frame_b_pct = frame_a, frame_b
        if self._groups:
            self._rebuild_results_table()

    def set_identical_compare_config(
        self,
        block_mib: int,
        sample_a_pct: int,
        sample_b_pct: int,
    ) -> None:
        """Update exact-match sampling parameters and rebuild when they change."""
        normalized_block = self._normalize_identical_block_mib(block_mib)
        normalized_a, normalized_b = self._normalize_identical_sample_pair(
            sample_a_pct,
            sample_b_pct,
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

    def set_mediainfo_exe_path(self, path: str) -> None:
        """Store the configured MediaInfo executable override path."""
        self._mediainfo_exe_path = str(path or "").strip()

    @staticmethod
    def _normalize_identical_block_mib(value: object) -> int:
        """Clamp the identical-compare block size to a safe range."""
        parsed = coerce_int(value, 1)
        return max(1, min(64, parsed))

    @staticmethod
    def _normalize_identical_sample_pair(
        a_value: object,
        b_value: object,
    ) -> tuple[int, int]:
        """Clamp and order the two identical-compare sample percentages."""

        def _normalize_percent(value: object, default: int) -> int:
            parsed = coerce_int(value, default)
            return max(0, min(100, parsed))

        sample_a = _normalize_percent(a_value, 23)
        sample_b = _normalize_percent(b_value, 78)
        if sample_a == sample_b:
            if sample_b < 100:
                sample_b += 1
            else:
                sample_a = max(0, sample_a - 1)
        if sample_a > sample_b:
            sample_a, sample_b = sample_b, sample_a
        return sample_a, sample_b

    def set_sort_mode(self, mode: str) -> None:
        """Update the sort mode and rebuild when it changes."""
        normalized = mode if mode in VALID_SORT_MODES else SORT_NONE
        if normalized == self._sort_mode:
            return
        self._sort_mode = normalized
        self._rebuild_results_table()

    def sort_mode(self) -> str:
        """Return the active sort mode."""
        return self._sort_mode

    def load_groups(self, groups: list[DuplicateGroup]) -> None:
        """Replace the current duplicate groups and rebuild the table."""
        self._groups = list(groups)
        self._checked_file_ids = set()
        self._invalidate_group_compare_dataset()
        self._group_compare_payloads = self._build_group_compare_payloads(self._groups)
        self._rebuild_results_table()

    def set_scan_context_note(self, note: str) -> None:
        """Show extra scan context next to the row/group count summary."""
        self._scan_context_note = str(note or "").strip()
        self._update_info_label()

    def _on_filter_changed(self, _text: str) -> None:
        """Update active text filters and rebuild the table."""
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
