"""Base widget and shared state for the results view."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from PySide6.QtCore import QSignalBlocker, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..media_format_policy import normalize_media_suffix
from .results_view_shared import (
    HDR_FILTER_ANY,
    HDR_FILTER_OPTIONS,
    RESULTS_HEADERS,
    SORT_NONE,
    VALID_SORT_MODES,
    ResultsFilterState,
    coerce_int,
)
from .thumbnails import (
    normalize_frame_pair,
    normalize_thumbnail_size_key,
    opencv_available,
    thumbnail_dimensions,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..models import DuplicateGroup


class ResultsViewBase(QWidget):
    """Base results widget that owns shared state and top-level controls."""

    delete_requested = Signal(str, object)  # mode, list[dict]
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._groups: list[DuplicateGroup] = []
        self._sort_mode = SORT_NONE
        self._filter_state = ResultsFilterState()
        self._filter_debounce_ms = 5000
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
        self.open_current_file_action: QAction
        self.explore_current_file_action: QAction
        self.launch_mediainfo_action: QAction
        self.delete_selected_action: QAction
        self.delete_selected_permanent_action: QAction
        self._filter_apply_timer = QTimer(self)
        self._filter_apply_timer.setSingleShot(True)
        self._filter_apply_timer.timeout.connect(self._apply_filter_inputs)

        self.info_label = QLabel("No scan loaded", self)
        self.info_label.setToolTip("No duplicate stats available.")
        self.thumbnail_note = QLabel("", self)
        self.thumbnail_note.setVisible(False)
        if not self._thumbnails_enabled:
            self.thumbnail_note.setText(
                "Thumbnail previews disabled: opencv-python is not installed."
            )
            self.thumbnail_note.setVisible(True)

        self.filter_toolbar = QWidget(self)
        self._configure_filter_widget(
            self.filter_toolbar,
            object_name="results_filter_toolbar",
            widget_alias="Results Filters",
        )
        basic_card = self._create_filter_card(
            title="Basic Filters",
            object_name="results_filter_basic_card",
            widget_alias="Results Basic Filters",
        )
        basic_layout = QGridLayout(basic_card)
        basic_layout.setContentsMargins(12, 12, 12, 12)
        basic_layout.setHorizontalSpacing(16)
        basic_layout.setVerticalSpacing(8)

        text_include_form = QFormLayout()
        text_include_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        text_include_form.setHorizontalSpacing(10)
        text_include_form.setVerticalSpacing(8)

        self.filter_include_name_edit = QLineEdit(basic_card)
        self.filter_include_name_edit.setPlaceholderText("movie|clip")
        self.filter_include_name_edit.setMinimumWidth(180)
        self._configure_filter_widget(
            self.filter_include_name_edit,
            object_name="results_filter_include_name_edit",
            widget_alias="Include Name",
        )
        text_include_form.addRow("Include Name", self.filter_include_name_edit)

        self.filter_include_path_edit = QLineEdit(basic_card)
        self.filter_include_path_edit.setPlaceholderText("archive|season")
        self.filter_include_path_edit.setMinimumWidth(180)
        self._configure_filter_widget(
            self.filter_include_path_edit,
            object_name="results_filter_include_path_edit",
            widget_alias="Include Path",
        )
        text_include_form.addRow("Include Path", self.filter_include_path_edit)

        text_exclude_form = QFormLayout()
        text_exclude_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        text_exclude_form.setHorizontalSpacing(10)
        text_exclude_form.setVerticalSpacing(8)

        self.filter_exclude_name_edit = QLineEdit(basic_card)
        self.filter_exclude_name_edit.setPlaceholderText("sample|trailer")
        self.filter_exclude_name_edit.setMinimumWidth(180)
        self._configure_filter_widget(
            self.filter_exclude_name_edit,
            object_name="results_filter_exclude_name_edit",
            widget_alias="Exclude Name",
        )
        text_exclude_form.addRow("Exclude Name", self.filter_exclude_name_edit)

        self.filter_exclude_path_edit = QLineEdit(basic_card)
        self.filter_exclude_path_edit.setPlaceholderText("extras|temp")
        self.filter_exclude_path_edit.setMinimumWidth(180)
        self._configure_filter_widget(
            self.filter_exclude_path_edit,
            object_name="results_filter_exclude_path_edit",
            widget_alias="Exclude Path",
        )
        text_exclude_form.addRow("Exclude Path", self.filter_exclude_path_edit)

        self.filter_text_hint_label = QLabel(
            "Case-insensitive, | means OR.",
            basic_card,
        )
        self._configure_filter_widget(
            self.filter_text_hint_label,
            object_name="results_filter_text_hint_label",
            widget_alias="Results Filter Hint",
        )
        self.filter_text_hint_label.setWordWrap(True)
        basic_layout.addLayout(text_include_form, 0, 0)
        basic_layout.addLayout(text_exclude_form, 0, 1)

        self.filter_include_match_all_checkbox = QCheckBox(
            "Must match all",
            basic_card,
        )
        self._configure_filter_widget(
            self.filter_include_match_all_checkbox,
            object_name="results_filter_include_match_all_checkbox",
            widget_alias="Must Match All",
        )

        ranges_card = self._create_filter_card(
            title="Ranges",
            object_name="results_filter_ranges_card",
            widget_alias="Results Range Filters",
        )
        ranges_grid = QGridLayout(ranges_card)
        ranges_grid.setContentsMargins(12, 12, 12, 12)
        ranges_grid.setHorizontalSpacing(10)
        ranges_grid.setVerticalSpacing(8)

        ranges_grid.addWidget(QLabel("Size MiB Min", ranges_card), 0, 0)
        self.filter_min_size_spin = self._create_optional_double_spinbox(
            object_name="results_filter_min_size_spin",
            widget_alias="Minimum Size MiB",
            minimum=-1.0,
            maximum=10_000_000.0,
            decimals=1,
            step=10.0,
        )
        self.filter_min_size_spin.setMinimumWidth(120)
        ranges_grid.addWidget(self.filter_min_size_spin, 0, 1)

        ranges_grid.addWidget(QLabel("Size MiB Max", ranges_card), 0, 2)
        self.filter_max_size_spin = self._create_optional_double_spinbox(
            object_name="results_filter_max_size_spin",
            widget_alias="Maximum Size MiB",
            minimum=-1.0,
            maximum=10_000_000.0,
            decimals=1,
            step=10.0,
        )
        self.filter_max_size_spin.setMinimumWidth(120)
        ranges_grid.addWidget(self.filter_max_size_spin, 0, 3)

        ranges_grid.addWidget(QLabel("Duration s Min", ranges_card), 1, 0)
        self.filter_min_duration_spin = self._create_optional_double_spinbox(
            object_name="results_filter_min_duration_spin",
            widget_alias="Minimum Duration Seconds",
            minimum=-1.0,
            maximum=1_000_000.0,
            decimals=1,
            step=10.0,
        )
        self.filter_min_duration_spin.setMinimumWidth(120)
        ranges_grid.addWidget(self.filter_min_duration_spin, 1, 1)

        ranges_grid.addWidget(QLabel("Duration s Max", ranges_card), 1, 2)
        self.filter_max_duration_spin = self._create_optional_double_spinbox(
            object_name="results_filter_max_duration_spin",
            widget_alias="Maximum Duration Seconds",
            minimum=-1.0,
            maximum=1_000_000.0,
            decimals=1,
            step=10.0,
        )
        self.filter_max_duration_spin.setMinimumWidth(120)
        ranges_grid.addWidget(self.filter_max_duration_spin, 1, 3)

        ranges_grid.addWidget(QLabel("Similarity Min", ranges_card), 2, 0)
        self.filter_min_similarity_spin = self._create_optional_double_spinbox(
            object_name="results_filter_min_similarity_spin",
            widget_alias="Minimum Similarity",
            minimum=-1.0,
            maximum=1.0,
            decimals=3,
            step=0.01,
        )
        self.filter_min_similarity_spin.setMinimumWidth(120)
        ranges_grid.addWidget(self.filter_min_similarity_spin, 2, 1)

        ranges_grid.addWidget(QLabel("Width Min", ranges_card), 2, 2)
        self.filter_min_width_spin = self._create_optional_spinbox(
            object_name="results_filter_min_width_spin",
            widget_alias="Minimum Width",
            minimum=-1,
            maximum=100_000,
            step=10,
        )
        self.filter_min_width_spin.setMinimumWidth(120)
        ranges_grid.addWidget(self.filter_min_width_spin, 2, 3)

        ranges_grid.addWidget(QLabel("Height Min", ranges_card), 3, 0)
        self.filter_min_height_spin = self._create_optional_spinbox(
            object_name="results_filter_min_height_spin",
            widget_alias="Minimum Height",
            minimum=-1,
            maximum=100_000,
            step=10,
        )
        self.filter_min_height_spin.setMinimumWidth(120)
        ranges_grid.addWidget(self.filter_min_height_spin, 3, 1)

        attributes_card = self._create_filter_card(
            title="Attributes",
            object_name="results_filter_attributes_card",
            widget_alias="Results Attribute Filters",
        )
        attributes_form = QFormLayout(attributes_card)
        attributes_form.setContentsMargins(12, 12, 12, 12)
        attributes_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        attributes_form.setHorizontalSpacing(10)
        attributes_form.setVerticalSpacing(8)

        self.filter_extension_combo = QComboBox(attributes_card)
        self._configure_filter_widget(
            self.filter_extension_combo,
            object_name="results_filter_extension_combo",
            widget_alias="Extension Filter",
        )
        self.filter_extension_combo.setMinimumWidth(160)
        attributes_form.addRow("Extension", self.filter_extension_combo)

        self.filter_video_codec_combo = QComboBox(attributes_card)
        self._configure_filter_widget(
            self.filter_video_codec_combo,
            object_name="results_filter_video_codec_combo",
            widget_alias="Video Codec Filter",
        )
        self.filter_video_codec_combo.setMinimumWidth(160)
        attributes_form.addRow("Video Codec", self.filter_video_codec_combo)

        self.filter_hdr_combo = QComboBox(attributes_card)
        self._configure_filter_widget(
            self.filter_hdr_combo,
            object_name="results_filter_hdr_combo",
            widget_alias="HDR Filter",
        )
        for label, value in HDR_FILTER_OPTIONS:
            self.filter_hdr_combo.addItem(label, value)
        self.filter_hdr_combo.setMinimumWidth(160)
        attributes_form.addRow("HDR", self.filter_hdr_combo)

        self.clear_filters_button = QPushButton("Clear Filters", basic_card)
        self._configure_filter_widget(
            self.clear_filters_button,
            object_name="results_filter_clear_button",
            widget_alias="Clear Filters",
        )
        self.clear_filters_button.setMinimumWidth(140)
        basic_layout.addWidget(self.filter_include_match_all_checkbox, 1, 0)
        basic_layout.addWidget(self.filter_text_hint_label, 2, 0)
        basic_layout.addWidget(
            self.clear_filters_button,
            1,
            1,
            2,
            1,
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )

        self.advanced_filters_toggle = QCheckBox(
            "Advanced Filters",
            self.filter_toolbar,
        )
        self.advanced_filters_toggle.setText("Advanced Filters")
        self.advanced_filters_toggle.setChecked(False)
        self._configure_filter_widget(
            self.advanced_filters_toggle,
            object_name="results_filter_advanced_toggle",
            widget_alias="Advanced Filters Toggle",
        )

        self.advanced_filters_container = QWidget(self.filter_toolbar)
        self._configure_filter_widget(
            self.advanced_filters_container,
            object_name="results_filter_advanced_container",
            widget_alias="Advanced Filters Container",
        )
        advanced_layout = QGridLayout(self.advanced_filters_container)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setHorizontalSpacing(10)
        advanced_layout.setVerticalSpacing(10)
        advanced_layout.addWidget(ranges_card, 0, 0)
        advanced_layout.addWidget(attributes_card, 0, 1)
        advanced_layout.setColumnStretch(0, 3)
        advanced_layout.setColumnStretch(1, 2)

        filter_layout = QVBoxLayout(self.filter_toolbar)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(10)
        filter_layout.addWidget(basic_card)
        filter_layout.addWidget(self.advanced_filters_toggle)
        filter_layout.addWidget(self.advanced_filters_container)

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
        self.results_table.itemDoubleClicked.connect(
            self._on_results_table_item_double_clicked
        )

        self.filter_include_name_edit.textChanged.connect(self._schedule_filter_apply)
        self.filter_include_path_edit.textChanged.connect(self._schedule_filter_apply)
        self.filter_exclude_name_edit.textChanged.connect(self._schedule_filter_apply)
        self.filter_exclude_path_edit.textChanged.connect(self._schedule_filter_apply)
        self.filter_include_name_edit.returnPressed.connect(self._apply_filter_inputs)
        self.filter_include_path_edit.returnPressed.connect(self._apply_filter_inputs)
        self.filter_exclude_name_edit.returnPressed.connect(self._apply_filter_inputs)
        self.filter_exclude_path_edit.returnPressed.connect(self._apply_filter_inputs)
        self.filter_min_size_spin.valueChanged.connect(self._schedule_filter_apply)
        self.filter_max_size_spin.valueChanged.connect(self._schedule_filter_apply)
        self.filter_min_duration_spin.valueChanged.connect(self._schedule_filter_apply)
        self.filter_max_duration_spin.valueChanged.connect(self._schedule_filter_apply)
        self.filter_min_similarity_spin.valueChanged.connect(
            self._schedule_filter_apply
        )
        self.filter_min_width_spin.valueChanged.connect(self._schedule_filter_apply)
        self.filter_min_height_spin.valueChanged.connect(self._schedule_filter_apply)
        self.filter_include_match_all_checkbox.toggled.connect(
            self._schedule_filter_apply
        )
        self.filter_extension_combo.currentIndexChanged.connect(
            self._schedule_filter_apply
        )
        self.filter_video_codec_combo.currentIndexChanged.connect(
            self._schedule_filter_apply
        )
        self.filter_hdr_combo.currentIndexChanged.connect(self._schedule_filter_apply)
        self.clear_filters_button.clicked.connect(self._clear_filters)
        self.advanced_filters_toggle.toggled.connect(self._set_advanced_filters_visible)

        self._install_shortcuts()

        layout = QVBoxLayout(self)
        layout.addWidget(self.info_label)
        layout.addWidget(self.thumbnail_note)
        layout.addWidget(self.filter_toolbar)
        layout.addWidget(self.results_table, stretch=1)
        self._set_advanced_filters_visible(False)
        self._refresh_video_codec_filter_options()

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

    @staticmethod
    def _configure_filter_widget(
        widget: QWidget,
        *,
        object_name: str,
        widget_alias: str,
    ) -> None:
        """Assign stable widget metadata for tests and diagnostics."""
        widget.setObjectName(object_name)
        widget.setProperty("widget_id", object_name)
        widget.setProperty("widget_alias", widget_alias)

    def _create_filter_card(
        self,
        *,
        title: str,
        object_name: str,
        widget_alias: str,
    ) -> QGroupBox:
        """Create one grouped filter card container with a title."""
        card = QGroupBox(title, self.filter_toolbar)
        self._configure_filter_widget(
            card,
            object_name=object_name,
            widget_alias=widget_alias,
        )
        return card

    def _set_advanced_filters_visible(self, visible: bool) -> None:
        """Show or hide the advanced filter section and sync the checkbox state."""
        self.advanced_filters_toggle.blockSignals(True)
        self.advanced_filters_toggle.setChecked(visible)
        self.advanced_filters_toggle.blockSignals(False)
        self.advanced_filters_container.setVisible(visible)

    def _create_optional_double_spinbox(
        self,
        *,
        object_name: str,
        widget_alias: str,
        minimum: float,
        maximum: float,
        decimals: int,
        step: float,
    ) -> QDoubleSpinBox:
        """Create one optional floating-point filter spinbox."""
        spin = QDoubleSpinBox(self.filter_toolbar)
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setSpecialValueText("Any")
        spin.setValue(minimum)
        self._configure_filter_widget(
            spin,
            object_name=object_name,
            widget_alias=widget_alias,
        )
        return spin

    def _create_optional_spinbox(
        self,
        *,
        object_name: str,
        widget_alias: str,
        minimum: int,
        maximum: int,
        step: int,
    ) -> QSpinBox:
        """Create one optional integer filter spinbox."""
        spin = QSpinBox(self.filter_toolbar)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setSpecialValueText("Any")
        spin.setValue(minimum)
        self._configure_filter_widget(
            spin,
            object_name=object_name,
            widget_alias=widget_alias,
        )
        return spin

    def _create_results_action(
        self,
        text: str,
        shortcuts: list[QKeySequence],
        handler: Callable[[], None],
    ) -> QAction:
        """Create one canonical results action shared by shortcuts and menus."""
        action = QAction(text, self)
        action.setShortcuts(shortcuts)
        action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.triggered.connect(handler)
        self.results_table.addAction(action)
        return action

    def _install_shortcuts(self) -> None:
        """Register shared actions and shortcuts for the results table."""
        self.open_current_file_action = self._create_results_action(
            "&Open Current File",
            [QKeySequence("Return"), QKeySequence("Enter")],
            self.open_current_in_default_player,
        )
        self.explore_current_file_action = self._create_results_action(
            "E&xplore Current File",
            [QKeySequence("E")],
            self.explore_current_file,
        )
        self.launch_mediainfo_action = self._create_results_action(
            "Launch &MediaInfo",
            [QKeySequence("M")],
            self.launch_mediainfo,
        )
        self.delete_selected_action = self._create_results_action(
            "&Delete Selected",
            [QKeySequence("Delete")],
            self.request_soft_delete_selected,
        )
        self.delete_selected_permanent_action = self._create_results_action(
            "&Permanently Delete Selected",
            [QKeySequence("Shift+Delete")],
            self.request_permanent_delete_selected,
        )

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
        self._refresh_extension_filter_options()
        self._refresh_video_codec_filter_options()
        self._apply_filter_inputs()
        self._rebuild_results_table()

    def set_scan_context_note(self, note: str) -> None:
        """Show extra scan context next to the row/group count summary."""
        self._scan_context_note = str(note or "").strip()
        self._update_info_label()

    @staticmethod
    def _parse_filter_terms(raw_text: str) -> tuple[str, ...]:
        """Split one filter field into normalized OR-matching terms."""
        return tuple(
            term
            for term in (part.strip().casefold() for part in raw_text.split("|"))
            if term
        )

    @staticmethod
    def _optional_double_value(spin: QDoubleSpinBox) -> float | None:
        """Return the optional value for a floating-point filter control."""
        value = float(spin.value())
        return None if value == float(spin.minimum()) else value

    @staticmethod
    def _optional_int_value(spin: QSpinBox) -> int | None:
        """Return the optional value for an integer filter control."""
        value = int(spin.value())
        return None if value == int(spin.minimum()) else value

    def _current_filter_state(self) -> ResultsFilterState:
        """Read the live filter widgets into a normalized filter state."""
        extension_text = self.filter_extension_combo.currentText().strip().casefold()
        codec_text = self.filter_video_codec_combo.currentText().strip().casefold()
        hdr_mode = str(self.filter_hdr_combo.currentData() or HDR_FILTER_ANY)
        return ResultsFilterState(
            include_name_terms=self._parse_filter_terms(
                self.filter_include_name_edit.text()
            ),
            include_path_terms=self._parse_filter_terms(
                self.filter_include_path_edit.text()
            ),
            exclude_name_terms=self._parse_filter_terms(
                self.filter_exclude_name_edit.text()
            ),
            exclude_path_terms=self._parse_filter_terms(
                self.filter_exclude_path_edit.text()
            ),
            include_match_all=self.filter_include_match_all_checkbox.isChecked(),
            min_size_mib=self._optional_double_value(self.filter_min_size_spin),
            max_size_mib=self._optional_double_value(self.filter_max_size_spin),
            min_duration_s=self._optional_double_value(self.filter_min_duration_spin),
            max_duration_s=self._optional_double_value(self.filter_max_duration_spin),
            min_similarity=self._optional_double_value(self.filter_min_similarity_spin),
            min_width=self._optional_int_value(self.filter_min_width_spin),
            min_height=self._optional_int_value(self.filter_min_height_spin),
            extension="" if extension_text == "any" else extension_text,
            video_codec="" if codec_text == "any" else codec_text,
            hdr_mode=hdr_mode,
        )

    def _schedule_filter_apply(self, _value: object) -> None:
        """Delay rebuilding the table until the current filter change settles."""
        self._filter_apply_timer.start(self._filter_debounce_ms)

    def _apply_filter_inputs(self) -> None:
        """Normalize the live filter inputs and rebuild when they changed."""
        self._filter_apply_timer.stop()
        filter_state = self._current_filter_state()
        if filter_state == self._filter_state:
            return
        self._filter_state = filter_state
        self._rebuild_results_table()

    def _on_results_table_item_double_clicked(self, item: QTableWidgetItem) -> None:
        """Launch the double-clicked row using the default file opener."""
        self.results_table.setCurrentItem(item)
        self.open_current_in_default_player()

    def _clear_filters(self) -> None:
        """Reset every filter control and rebuild the full results set."""
        self._filter_apply_timer.stop()
        blockers = [
            QSignalBlocker(self.filter_include_name_edit),
            QSignalBlocker(self.filter_include_path_edit),
            QSignalBlocker(self.filter_exclude_name_edit),
            QSignalBlocker(self.filter_exclude_path_edit),
            QSignalBlocker(self.filter_min_size_spin),
            QSignalBlocker(self.filter_max_size_spin),
            QSignalBlocker(self.filter_min_duration_spin),
            QSignalBlocker(self.filter_max_duration_spin),
            QSignalBlocker(self.filter_min_similarity_spin),
            QSignalBlocker(self.filter_min_width_spin),
            QSignalBlocker(self.filter_min_height_spin),
            QSignalBlocker(self.filter_include_match_all_checkbox),
            QSignalBlocker(self.filter_extension_combo),
            QSignalBlocker(self.filter_video_codec_combo),
            QSignalBlocker(self.filter_hdr_combo),
        ]
        self.filter_include_name_edit.clear()
        self.filter_include_path_edit.clear()
        self.filter_exclude_name_edit.clear()
        self.filter_exclude_path_edit.clear()
        self.filter_include_match_all_checkbox.setChecked(False)
        self.filter_min_size_spin.setValue(self.filter_min_size_spin.minimum())
        self.filter_max_size_spin.setValue(self.filter_max_size_spin.minimum())
        self.filter_min_duration_spin.setValue(self.filter_min_duration_spin.minimum())
        self.filter_max_duration_spin.setValue(self.filter_max_duration_spin.minimum())
        self.filter_min_similarity_spin.setValue(
            self.filter_min_similarity_spin.minimum()
        )
        self.filter_min_width_spin.setValue(self.filter_min_width_spin.minimum())
        self.filter_min_height_spin.setValue(self.filter_min_height_spin.minimum())
        self.filter_extension_combo.setCurrentText("Any")
        self.filter_video_codec_combo.setCurrentText("Any")
        self.filter_hdr_combo.setCurrentIndex(0)
        del blockers
        self._apply_filter_inputs()

    @staticmethod
    def _normalized_extension_value(path: str) -> str:
        """Return one normalized lowercase extension without a leading dot."""
        return normalize_media_suffix(path).lstrip(".")

    def _refresh_attribute_filter_options(
        self,
        combo: QComboBox,
        values: list[str],
    ) -> None:
        """Refresh one attribute combo and preserve its current valid choice."""
        current_text = combo.currentText().strip().casefold()
        blocker = QSignalBlocker(combo)
        try:
            combo.clear()
            combo.addItem("Any")
            for value in values:
                combo.addItem(value)
            if current_text and current_text != "any":
                for index in range(combo.count()):
                    if combo.itemText(index).strip().casefold() == current_text:
                        combo.setCurrentIndex(index)
                        return
            combo.setCurrentIndex(0)
        finally:
            del blocker

    def _refresh_extension_filter_options(self) -> None:
        """Refresh extension filter choices from the currently loaded duplicate set."""
        extensions = sorted(
            {
                self._normalized_extension_value(item.path)
                for group in self._groups
                for item in group.items
                if self._normalized_extension_value(item.path)
            },
            key=str.casefold,
        )
        self._refresh_attribute_filter_options(self.filter_extension_combo, extensions)

    def _refresh_video_codec_filter_options(self) -> None:
        """Refresh codec filter choices from the currently loaded duplicate set."""
        codecs = sorted(
            {
                str(item.codec or "").strip()
                for group in self._groups
                for item in group.items
                if str(item.codec or "").strip()
            },
            key=str.casefold,
        )
        self._refresh_attribute_filter_options(self.filter_video_codec_combo, codecs)
