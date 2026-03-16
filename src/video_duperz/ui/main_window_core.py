"""Core widget construction and source-tab setup for the main window."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config_video_presets import (
    DEFAULT_VIDEO_EXTENSION_PRESET,
    VIDEO_EXTENSION_PRESET_NAMES,
)
from .results_view import (
    SORT_GROUP_COUNT_ASC,
    SORT_GROUP_COUNT_DESC,
    SORT_GROUP_SIZE_ASC,
    SORT_GROUP_SIZE_DESC,
    SORT_GROUP_SPREAD_ASC,
    SORT_GROUP_SPREAD_DESC,
    SORT_ROW_SIZE_ASC,
    SORT_ROW_SIZE_DESC,
    ResultsView,
)
from .scan_view import ScanView

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..db import Database
    from ..models import SavedScanProfilePayload, Settings
    from .workers import ScanWorker

THUMBNAIL_SIZE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Small (80x45)", "80x45"),
    ("Default (96x54)", "96x54"),
    ("Medium (128x72)", "128x72"),
    ("Large (160x90)", "160x90"),
)
MAX_DRIVE_WORKERS = 64


class DeleteTarget(TypedDict):
    """Selected duplicate row metadata used when dispatching delete actions."""

    row: int
    file_id: int
    group_db_id: int
    path: str


def payload_dict(value: object) -> dict[str, object]:
    """Normalize arbitrary worker payloads into string-key dictionaries."""
    if not isinstance(value, dict):
        return {}
    raw_map = cast("dict[object, object]", value)
    normalized: dict[str, object] = {}
    for key, raw in raw_map.items():
        if isinstance(key, str | int | float | bool):
            normalized[str(key)] = raw
    return normalized


def payload_strings(value: object) -> list[str]:
    """Normalize an arbitrary list payload into trimmed strings."""
    if not isinstance(value, list):
        return []
    raw_items = cast("list[object]", value)
    return [text for item in raw_items if (text := str(item).strip())]


def metric_float(metrics: dict[str, object], key: str, default: float = 0.0) -> float:
    """Read one float-like value from a metrics payload."""
    value = metrics.get(key, default)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def metric_int(metrics: dict[str, object], key: str, default: int = 0) -> int:
    """Read one int-like value from a metrics payload."""
    value = metrics.get(key, default)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


class MainWindowBase(QMainWindow):
    """Base main-window class that owns widget construction and shared state."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Video Duperz")
        self.resize(1600, 920)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

        self.db = db
        self.db_file = str(db.path)
        self.settings = settings
        self.current_scan_id: int | None = None
        self.thread_pool = QThreadPool(self)
        self.scan_worker: ScanWorker | None = None
        self._scan_tab_locked = False
        self._recent_roots: list[str] = list(settings.recent_scan_roots)
        self._recent_roots_menu: QMenu | None = None
        self._saved_column_views: dict[str, dict[str, list[int] | list[bool]]] = dict(
            settings.saved_column_views
        )
        self._saved_scan_profiles: dict[str, SavedScanProfilePayload] = dict(
            settings.saved_scan_profiles
        )
        self._column_toggle_actions: list[QAction] = []
        self._saved_views_menu: QMenu | None = None
        self._sort_action_group: QActionGroup | None = None
        self._sort_actions: dict[str, QAction] = {}
        self._saved_scans_menu: QMenu | None = None
        self._drive_worker_overrides: dict[str, int] = {
            str(key): max(1, int(value))
            for key, value in settings.drive_worker_overrides.items()
        }
        self._drive_workers_editing = False
        self._full_reset_requested = False

        self.tabs = QTabWidget(self)
        self.setCentralWidget(self.tabs)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.sources_tab = QWidget(self)
        self.scan_view = ScanView(self)
        self.results_view = ResultsView(self)
        self.results_view.set_thumbnail_size(settings.thumbnail_size)
        self.results_view.set_thumbnail_frame_positions(
            settings.thumbnail_frame_a_pct,
            settings.thumbnail_frame_b_pct,
        )
        self.results_view.set_identical_compare_config(
            block_mib=settings.identical_block_mib,
            sample_a_pct=settings.identical_sample_a_pct,
            sample_b_pct=settings.identical_sample_b_pct,
        )
        self.results_view.set_mediainfo_exe_path(settings.mediainfo_exe_path)

        self._build_sources_tab()
        self._build_menus()
        self.tabs.addTab(self.sources_tab, "Sources")
        self.tabs.addTab(self.scan_view, "Scan")
        self.tabs.addTab(self.results_view, "Results")

        self.scan_view.start_requested.connect(self._start_scan)
        self.scan_view.rescan_requested.connect(self._rescan_scan)
        self.scan_view.cancel_requested.connect(self._cancel_scan)
        self.results_view.delete_requested.connect(self._handle_delete_requested)
        self.results_view.status_message.connect(self.statusBar().showMessage)

        self.statusBar().showMessage("Ready")
        self._load_settings_to_widgets()

    def _on_tab_changed(self, index: int) -> None: ...

    def _build_sources_tab(self) -> None: ...

    def _build_menus(self) -> None: ...

    def _start_scan(self) -> None: ...

    def _rescan_scan(self) -> None: ...

    def _cancel_scan(self) -> None: ...

    def _handle_delete_requested(
        self,
        mode: str,
        targets: list[DeleteTarget],
    ) -> None: ...

    def _load_settings_to_widgets(self) -> None: ...


class MainWindowMenuMixin(MainWindowBase):
    """Window menu construction helpers."""

    def _export_current_scan(self) -> None: ...

    def _clear_recent_roots(self) -> None: ...

    def _clear_saved_scans(self) -> None: ...

    def _clear_cached_thumbnails(self) -> None: ...

    def _request_full_reset(self) -> None: ...

    def _fit_columns(self) -> None: ...

    def _save_current_view(self) -> None: ...

    def _refresh_saved_views_menu(self) -> None: ...

    def _column_toggle_slot(self, column_index: int) -> Callable[[bool], None]: ...

    def _edit_ini_file(self) -> None: ...

    def _show_about(self) -> None: ...

    def _build_menus(self) -> None:
        """Build all top-level menus for the main window."""
        self._build_file_menu()
        self._build_view_menu()
        self._build_sort_menu()
        self._build_actions_menu()
        self._build_tools_menu()
        self._build_help_menu()

    def _build_file_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.export_action = QAction("&Export Current Scan...", self)
        self.export_action.triggered.connect(self._export_current_scan)
        file_menu.addAction(self.export_action)

        file_menu.addSeparator()
        self.clear_recent_folders_action = QAction("C&lear Recent Folders", self)
        self.clear_recent_folders_action.triggered.connect(self._clear_recent_roots)
        file_menu.addAction(self.clear_recent_folders_action)

        self.clear_saved_scans_action = QAction("Clear Sa&ved Scans", self)
        self.clear_saved_scans_action.triggered.connect(self._clear_saved_scans)
        file_menu.addAction(self.clear_saved_scans_action)

        self.clear_cached_thumbnails_action = QAction("Clear Cached T&humbnails", self)
        self.clear_cached_thumbnails_action.triggered.connect(
            self._clear_cached_thumbnails
        )
        file_menu.addAction(self.clear_cached_thumbnails_action)

        file_menu.addSeparator()
        self.full_reset_action = QAction("&Full Reset", self)
        self.full_reset_action.triggered.connect(self._request_full_reset)
        file_menu.addAction(self.full_reset_action)

        file_menu.addSeparator()
        self.exit_action = QAction("E&xit", self)
        self.exit_action.setShortcuts([QKeySequence("Ctrl+Q"), QKeySequence("Alt+X")])
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

    def _build_view_menu(self) -> None:
        view_menu = self.menuBar().addMenu("&View")
        columns_menu = view_menu.addMenu("&Columns")

        self.fit_columns_action = QAction("&Fit Columns", self)
        self.fit_columns_action.triggered.connect(self._fit_columns)
        columns_menu.addAction(self.fit_columns_action)

        self.save_current_view_action = QAction("&Save Current View", self)
        self.save_current_view_action.triggered.connect(self._save_current_view)
        columns_menu.addAction(self.save_current_view_action)

        self._saved_views_menu = columns_menu.addMenu("Sa&ved Views")
        self._refresh_saved_views_menu()
        columns_menu.addSeparator()

        self._column_toggle_actions = []
        for index, label in enumerate(self.results_view.column_labels()):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(self._column_toggle_slot(index))
            columns_menu.addAction(action)
            self._column_toggle_actions.append(action)

    def _build_sort_menu(self) -> None:
        sort_menu = self.menuBar().addMenu("&Sort")
        self._sort_action_group = QActionGroup(self)
        self._sort_action_group.setExclusive(True)
        self._sort_actions = {}
        sort_specs = [
            ("&1 Larger size groups first", SORT_GROUP_SIZE_DESC),
            ("&2 Smaller size groups first", SORT_GROUP_SIZE_ASC),
            ("&3 Most duplicates first", SORT_GROUP_COUNT_DESC),
            ("&4 Least duplicates first", SORT_GROUP_COUNT_ASC),
            ("&5 Larger files in group first", SORT_ROW_SIZE_DESC),
            ("&6 Smaller files in group first", SORT_ROW_SIZE_ASC),
            (
                "&7 Size difference between largest and smallest in group first",
                SORT_GROUP_SPREAD_DESC,
            ),
            (
                "&8 Size difference between largest and smallest in group last",
                SORT_GROUP_SPREAD_ASC,
            ),
        ]
        for label, mode in sort_specs:
            action = QAction(label, self)
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, value=mode: self.results_view.set_sort_mode(
                    value
                )
            )
            self._sort_action_group.addAction(action)
            sort_menu.addAction(action)
            self._sort_actions[mode] = action

    def _build_actions_menu(self) -> None:
        actions_menu = self.menuBar().addMenu("&Actions")
        self.keep_best_action = QAction("&Select all, keep best", self)
        self.keep_best_action.triggered.connect(
            lambda: self.results_view.apply_keep_strategy("best")
        )
        actions_menu.addAction(self.keep_best_action)

        self.keep_worst_action = QAction("Select all, keep &worst", self)
        self.keep_worst_action.triggered.connect(
            lambda: self.results_view.apply_keep_strategy("worst")
        )
        actions_menu.addAction(self.keep_worst_action)

        self.keep_larger_action = QAction("Select all, keep &larger", self)
        self.keep_larger_action.triggered.connect(
            lambda: self.results_view.apply_keep_strategy("larger")
        )
        actions_menu.addAction(self.keep_larger_action)

        self.keep_smaller_action = QAction("Select all, keep s&maller", self)
        self.keep_smaller_action.triggered.connect(
            lambda: self.results_view.apply_keep_strategy("smaller")
        )
        actions_menu.addAction(self.keep_smaller_action)

        self.keep_newer_action = QAction("Select all, keep &newer", self)
        self.keep_newer_action.triggered.connect(
            lambda: self.results_view.apply_keep_strategy("newer")
        )
        actions_menu.addAction(self.keep_newer_action)

        self.keep_older_action = QAction("Select all, keep &older", self)
        self.keep_older_action.triggered.connect(
            lambda: self.results_view.apply_keep_strategy("older")
        )
        actions_menu.addAction(self.keep_older_action)

        actions_menu.addSeparator()
        self.delete_selected_action = QAction("&Delete Selected", self)
        self.delete_selected_action.triggered.connect(
            self.results_view.request_soft_delete_selected
        )
        actions_menu.addAction(self.delete_selected_action)

        self.delete_selected_permanent_action = QAction(
            "&Permanently Delete Selected",
            self,
        )
        self.delete_selected_permanent_action.triggered.connect(
            self.results_view.request_permanent_delete_selected
        )
        actions_menu.addAction(self.delete_selected_permanent_action)

    def _build_tools_menu(self) -> None:
        tools_menu = self.menuBar().addMenu("&Tools")
        self.edit_ini_action = QAction("Edit &.ini File", self)
        self.edit_ini_action.triggered.connect(self._edit_ini_file)
        tools_menu.addAction(self.edit_ini_action)

    def _build_help_menu(self) -> None:
        help_menu = self.menuBar().addMenu("&Help")
        self.about_action = QAction("&Help", self)
        self.about_action.setShortcut("F1")
        self.about_action.triggered.connect(self._show_about)
        help_menu.addAction(self.about_action)


class MainWindowSourceSetupMixin(MainWindowMenuMixin):
    """Source-tab widget construction helpers."""

    def _refresh_recent_roots_menu(self) -> None: ...

    def _refresh_saved_scans_menu(self) -> None: ...

    def _update_root_buttons_state(self) -> None: ...

    def _add_root(self) -> None: ...

    def _remove_selected_root(self) -> None: ...

    def _remove_all_roots(self) -> None: ...

    def _show_recent_roots_menu(self) -> None: ...

    def _save_current_scan_set_as(self) -> None: ...

    def _show_saved_scans_menu(self) -> None: ...

    def _extensions_preset_changed(self, preset_name: str) -> None: ...

    def _extensions_text_edited(self, _text: str) -> None: ...

    def _refresh_sources_physical_drive_view(self) -> None: ...

    def _thumbnail_size_changed(self) -> None: ...

    def _browse_executable_path(
        self,
        target_edit: QLineEdit,
        tool_name: str,
    ) -> None: ...

    def _build_sources_tab(self) -> None:
        """Build the entire Sources tab and its child controls."""
        roots_actions = self._build_sources_root_controls()
        self._build_sources_option_controls()
        self._build_sources_drive_widgets()
        self._build_sources_layout(roots_actions)
        self._recent_roots_menu = QMenu(self)
        self._refresh_recent_roots_menu()
        self._saved_scans_menu = QMenu(self)
        self._refresh_saved_scans_menu()
        self._update_root_buttons_state()

    def _build_sources_root_controls(self) -> QHBoxLayout:
        self.roots_list = QListWidget(self.sources_tab)
        self.add_root_btn = QPushButton("Add Folder", self.sources_tab)
        self.remove_root_btn = QPushButton("Remove Folder", self.sources_tab)
        self.remove_all_roots_btn = QPushButton("Remove All", self.sources_tab)
        self.add_recent_root_btn = QPushButton("Add Recent Folder", self.sources_tab)
        self.save_scan_set_btn = QPushButton("Save Scan Set", self.sources_tab)
        self.load_saved_scan_btn = QPushButton("Load Saved Scan", self.sources_tab)
        self.add_root_btn.clicked.connect(self._add_root)
        self.remove_root_btn.clicked.connect(self._remove_selected_root)
        self.remove_all_roots_btn.clicked.connect(self._remove_all_roots)
        self.add_recent_root_btn.clicked.connect(self._show_recent_roots_menu)
        self.save_scan_set_btn.clicked.connect(self._save_current_scan_set_as)
        self.load_saved_scan_btn.clicked.connect(self._show_saved_scans_menu)
        self.roots_list.currentRowChanged.connect(self._update_root_buttons_state)

        roots_actions = QHBoxLayout()
        roots_actions.addWidget(self.add_root_btn)
        roots_actions.addWidget(self.remove_root_btn)
        roots_actions.addWidget(self.remove_all_roots_btn)
        roots_actions.addWidget(self.add_recent_root_btn)
        roots_actions.addWidget(self.save_scan_set_btn)
        roots_actions.addWidget(self.load_saved_scan_btn)
        roots_actions.addStretch(1)
        return roots_actions

    def _build_sources_option_controls(self) -> None:
        self.extensions_preset_combo = QComboBox(self.sources_tab)
        self.extensions_preset_combo.addItems(VIDEO_EXTENSION_PRESET_NAMES)
        default_preset_index = self.extensions_preset_combo.findText(
            DEFAULT_VIDEO_EXTENSION_PRESET
        )
        self.extensions_preset_combo.setCurrentIndex(max(0, default_preset_index))
        self.extensions_preset_combo.currentTextChanged.connect(
            self._extensions_preset_changed
        )
        self.extensions_edit = QLineEdit(self.sources_tab)
        self.extensions_edit.textEdited.connect(self._extensions_text_edited)
        self.profile_combo = QComboBox(self.sources_tab)
        self.profile_combo.addItems(["balanced", "conservative", "aggressive"])

        self.max_workers_spin = QSpinBox(self.sources_tab)
        self.max_workers_spin.setRange(1, 16)
        self.max_workers_spin.valueChanged.connect(
            self._refresh_sources_physical_drive_view
        )
        self.probe_backend_combo = QComboBox(self.sources_tab)
        self.probe_backend_combo.addItems(["pyav", "ffprobe"])
        self.probe_mode_combo = QComboBox(self.sources_tab)
        self.probe_mode_combo.addItems(["balanced", "burst"])
        self.probe_backend_combo.currentTextChanged.connect(
            self._refresh_sources_physical_drive_view
        )
        self.probe_mode_combo.currentTextChanged.connect(
            self._refresh_sources_physical_drive_view
        )
        self.ffmpeg_exe_path_edit = QLineEdit(self.sources_tab)
        self.ffmpeg_exe_path_browse_btn = QPushButton("Browse...", self.sources_tab)
        self.ffmpeg_exe_path_browse_btn.clicked.connect(
            lambda: self._browse_executable_path(
                self.ffmpeg_exe_path_edit,
                "ffmpeg",
            )
        )
        self.ffprobe_exe_path_edit = QLineEdit(self.sources_tab)
        self.ffprobe_exe_path_browse_btn = QPushButton("Browse...", self.sources_tab)
        self.ffprobe_exe_path_browse_btn.clicked.connect(
            lambda: self._browse_executable_path(
                self.ffprobe_exe_path_edit,
                "ffprobe",
            )
        )
        self.mediainfo_exe_path_edit = QLineEdit(self.sources_tab)
        self.mediainfo_exe_path_browse_btn = QPushButton("Browse...", self.sources_tab)
        self.mediainfo_exe_path_browse_btn.clicked.connect(
            lambda: self._browse_executable_path(
                self.mediainfo_exe_path_edit,
                "mediainfo",
            )
        )
        self.thumbnail_size_combo = QComboBox(self.sources_tab)
        for label, size_key in THUMBNAIL_SIZE_OPTIONS:
            self.thumbnail_size_combo.addItem(label, size_key)
        self.thumbnail_size_combo.currentIndexChanged.connect(
            self._thumbnail_size_changed
        )

    def _build_executable_override_row(
        self,
        label_text: str,
        path_edit: QLineEdit,
        browse_button: QPushButton,
    ) -> QWidget:
        """Build one labeled row for an executable override field."""
        row_widget = QWidget(self.sources_tab)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(QLabel(label_text, row_widget))
        row_layout.addWidget(path_edit, stretch=1)
        row_layout.addWidget(browse_button)
        return row_widget

    def _build_sources_drive_widgets(self) -> None:
        self.sources_drive_summary_label = QLabel(
            "Matched physical drives: 0 | Requested workers: 1 | Effective workers: 0",
            self.sources_tab,
        )
        self.sources_drive_table = QTableWidget(0, 9, self.sources_tab)
        self.sources_drive_table.setHorizontalHeaderLabels(
            [
                "Root",
                "Disk token(s)",
                "Volume identity",
                "Total",
                "Free",
                "Used %",
                "Workers",
                "Matched",
                "Lookup note",
            ]
        )
        self.sources_drive_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.sources_drive_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.sources_drive_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.sources_drive_table.setAlternatingRowColors(True)
        self.sources_drive_table.verticalHeader().setVisible(False)
        drives_header = self.sources_drive_table.horizontalHeader()
        drives_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        drives_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        drives_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        drives_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        drives_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        drives_header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        drives_header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        drives_header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        drives_header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)

    def _build_sources_layout(self, roots_actions: QHBoxLayout) -> None:
        layout = QVBoxLayout(self.sources_tab)
        layout.addWidget(QLabel("Scan Folders"))
        layout.addWidget(self.roots_list, stretch=1)
        layout.addLayout(roots_actions)
        layout.addWidget(QLabel("Extensions preset"))
        layout.addWidget(self.extensions_preset_combo)
        layout.addWidget(QLabel("Extensions (comma-separated, no dots required)"))
        layout.addWidget(self.extensions_edit)
        layout.addWidget(QLabel("Similarity profile"))
        layout.addWidget(self.profile_combo)
        layout.addWidget(QLabel("Max Workers total"))
        layout.addWidget(self.max_workers_spin)
        layout.addWidget(QLabel("Probe backend"))
        layout.addWidget(self.probe_backend_combo)
        layout.addWidget(QLabel("Probe mode"))
        layout.addWidget(self.probe_mode_combo)
        layout.addWidget(QLabel("Executable overrides (blank = use PATH)"))
        layout.addWidget(
            self._build_executable_override_row(
                "ffmpeg",
                self.ffmpeg_exe_path_edit,
                self.ffmpeg_exe_path_browse_btn,
            )
        )
        layout.addWidget(
            self._build_executable_override_row(
                "ffprobe",
                self.ffprobe_exe_path_edit,
                self.ffprobe_exe_path_browse_btn,
            )
        )
        layout.addWidget(
            self._build_executable_override_row(
                "mediainfo",
                self.mediainfo_exe_path_edit,
                self.mediainfo_exe_path_browse_btn,
            )
        )
        layout.addWidget(QLabel("Physical drives"))
        layout.addWidget(self.sources_drive_summary_label)
        layout.addWidget(self.sources_drive_table, stretch=1)
        layout.addWidget(QLabel("Thumbnail preview size"))
        layout.addWidget(self.thumbnail_size_combo)
        layout.addStretch(1)
