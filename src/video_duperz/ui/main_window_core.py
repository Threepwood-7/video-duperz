"""Core widget construction and source-tab setup for the main window."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QAction, QActionGroup, QKeyEvent, QKeySequence, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QPushButton,
    QSlider,
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
from ..process_priority import CPU_PRIORITY_OPTIONS, IO_MODE_OPTIONS
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
    from ..process_priority import CurrentProcessPriorityState
    from .workers import ScanWorker

THUMBNAIL_SIZE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Small (80x45)", "80x45"),
    ("Default (96x54)", "96x54"),
    ("Medium (128x72)", "128x72"),
    ("Large (160x90)", "160x90"),
)
MAX_DRIVE_WORKERS = 64
SOURCES_DRIVE_DEFAULT_WIDTHS: list[int] = [240, 220, 160, 96, 96, 76, 96, 76, 240]


class PersistentCheckMenu(QMenu):
    """Keep checkable actions open while the user toggles them."""

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Toggle checkable actions without closing the menu."""
        action = self.activeAction()
        if self._should_keep_open(action):
            action.trigger()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Toggle checkable actions from the keyboard without closing the menu."""
        action = self.activeAction()
        if self._should_keep_open(action) and event.key() in {
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
            Qt.Key.Key_Select,
        }:
            action.trigger()
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _should_keep_open(action: QAction | None) -> bool:
        """Return whether one action should toggle without closing the menu."""
        return bool(action is not None and action.isEnabled() and action.isCheckable())


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
        self._loaded_paused_scan_id: int | None = None
        self._loaded_paused_roots: list[str] = []
        self._loaded_paused_profile: str = ""
        self._loaded_paused_extensions: list[str] = []
        self._loaded_paused_probe_backend: str = ""
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
        self._columns_menu: QMenu | None = None
        self._saved_views_menu: QMenu | None = None
        self._sort_action_group: QActionGroup | None = None
        self._sort_actions: dict[str, QAction] = {}
        self._saved_scans_menu: QMenu | None = None
        self.actions_menu: QMenu | None = None
        self._drive_worker_overrides: dict[str, int] = {
            str(key): max(1, int(value))
            for key, value in settings.drive_worker_overrides.items()
        }
        self._drive_workers_editing = False
        self._full_reset_requested = False
        self._scan_priority_state: CurrentProcessPriorityState | None = None
        self._sources_drive_table_column_widths: list[int] = []
        self._applying_sources_drive_table_column_widths = False

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
        self.results_view.set_everything_exe_path(settings.everything_exe_path)
        self.results_view.set_custom_command_overrides(
            command_f2=settings.custom_command_f2,
            command_f3=settings.custom_command_f3,
            command_f4=settings.custom_command_f4,
        )

        self._build_sources_tab()
        self._build_menus()
        self.tabs.addTab(self.sources_tab, "Sources")
        self.tabs.addTab(self.scan_view, "Scan")
        self.tabs.addTab(self.results_view, "Results")

        self.scan_view.start_requested.connect(self._start_scan)
        self.scan_view.rescan_requested.connect(self._rescan_scan)
        self.scan_view.pause_requested.connect(self._pause_scan)
        self.scan_view.resume_requested.connect(self._resume_scan)
        self.scan_view.cancel_requested.connect(self._cancel_scan)
        self.scan_view.path_activation_requested.connect(
            self._open_scan_subject_in_explorer
        )
        self.results_view.delete_requested.connect(self._handle_delete_requested)
        self.results_view.status_message.connect(self.statusBar().showMessage)

        self.statusBar().showMessage("Ready")
        self._load_settings_to_widgets()

    def _on_tab_changed(self, index: int) -> None: ...

    def _build_sources_tab(self) -> None: ...

    def _build_menus(self) -> None: ...

    def _start_scan(self) -> None: ...

    def _rescan_scan(self) -> None: ...

    def _pause_scan(self) -> None: ...

    def _resume_scan(self) -> None: ...

    def _cancel_scan(self) -> None: ...

    def _open_scan_subject_in_explorer(self, path: str) -> None: ...

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
        view_menu = PersistentCheckMenu("&View", self)
        self.menuBar().addMenu(view_menu)
        self._columns_menu = view_menu

        self.fit_columns_action = QAction("&Fit Columns", self)
        self.fit_columns_action.triggered.connect(self._fit_columns)
        view_menu.addAction(self.fit_columns_action)

        self.save_current_view_action = QAction("&Save Current View", self)
        self.save_current_view_action.triggered.connect(self._save_current_view)
        view_menu.addAction(self.save_current_view_action)

        self._saved_views_menu = view_menu.addMenu("Sa&ved Views")
        self._refresh_saved_views_menu()
        view_menu.addSeparator()

        self._column_toggle_actions = []
        for index, label in enumerate(self.results_view.column_labels()):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(self._column_toggle_slot(index))
            view_menu.addAction(action)
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
        self.actions_menu = actions_menu
        self.open_current_file_action = self.results_view.open_current_file_action
        actions_menu.addAction(self.open_current_file_action)

        self.explore_current_file_action = self.results_view.explore_current_file_action
        actions_menu.addAction(self.explore_current_file_action)

        self.copy_full_path_action = self.results_view.copy_full_path_action
        actions_menu.addAction(self.copy_full_path_action)

        self.search_everything_action = self.results_view.search_everything_action
        actions_menu.addAction(self.search_everything_action)

        self.open_web_search_action = self.results_view.open_web_search_action
        actions_menu.addAction(self.open_web_search_action)

        self.launch_mediainfo_action = self.results_view.launch_mediainfo_action
        actions_menu.addAction(self.launch_mediainfo_action)

        actions_menu.addSeparator()
        self.custom_command_f2_action = self.results_view.custom_command_f2_action
        actions_menu.addAction(self.custom_command_f2_action)

        self.custom_command_f3_action = self.results_view.custom_command_f3_action
        actions_menu.addAction(self.custom_command_f3_action)

        self.custom_command_f4_action = self.results_view.custom_command_f4_action
        actions_menu.addAction(self.custom_command_f4_action)

        actions_menu.addSeparator()
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
        self.delete_selected_action = self.results_view.delete_selected_action
        actions_menu.addAction(self.delete_selected_action)

        self.delete_selected_recycle_bin_action = (
            self.results_view.delete_selected_recycle_bin_action
        )
        actions_menu.addAction(self.delete_selected_recycle_bin_action)

        self.delete_selected_permanent_action = (
            self.results_view.delete_selected_permanent_action
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
        self.roots_list.setMinimumHeight(250)
        self._configure_named_widget(
            self.roots_list,
            object_name="sources_roots_list",
            widget_alias="Scan Folders List",
        )
        self.add_root_btn = QPushButton("Add Folder", self.sources_tab)
        self.remove_root_btn = QPushButton("Remove Folder", self.sources_tab)
        self.remove_all_roots_btn = QPushButton("Remove All", self.sources_tab)
        self.add_recent_root_btn = QPushButton("Add Recent Folder", self.sources_tab)
        self.save_scan_set_btn = QPushButton("Save Scan Set", self.sources_tab)
        self.load_saved_scan_btn = QPushButton("Load Saved Scan", self.sources_tab)
        self.sources_scan_btn = QPushButton("Scan", self.sources_tab)
        self.add_root_btn.clicked.connect(self._add_root)
        self.remove_root_btn.clicked.connect(self._remove_selected_root)
        self.remove_all_roots_btn.clicked.connect(self._remove_all_roots)
        self.add_recent_root_btn.clicked.connect(self._show_recent_roots_menu)
        self.save_scan_set_btn.clicked.connect(self._save_current_scan_set_as)
        self.load_saved_scan_btn.clicked.connect(self._show_saved_scans_menu)
        self.sources_scan_btn.clicked.connect(
            lambda: self.tabs.setCurrentWidget(self.scan_view)
        )
        self.roots_list.currentRowChanged.connect(self._update_root_buttons_state)
        self._configure_named_widget(
            self.add_root_btn,
            object_name="sources_add_root_btn",
            widget_alias="Add Folder",
        )
        self._configure_named_widget(
            self.remove_root_btn,
            object_name="sources_remove_root_btn",
            widget_alias="Remove Folder",
        )
        self._configure_named_widget(
            self.remove_all_roots_btn,
            object_name="sources_remove_all_roots_btn",
            widget_alias="Remove All Folders",
        )
        self._configure_named_widget(
            self.add_recent_root_btn,
            object_name="sources_add_recent_root_btn",
            widget_alias="Add Recent Folder",
        )
        self._configure_named_widget(
            self.save_scan_set_btn,
            object_name="sources_save_scan_set_btn",
            widget_alias="Save Scan Set",
        )
        self._configure_named_widget(
            self.load_saved_scan_btn,
            object_name="sources_load_saved_scan_btn",
            widget_alias="Load Saved Scan",
        )
        self._configure_named_widget(
            self.sources_scan_btn,
            object_name="sources_scan_btn",
            widget_alias="Open Scan Tab",
        )
        self._set_sources_tooltip(
            self.roots_list,
            (
                "Choose the top-level folders that this scan will walk.\n\n"
                "Every matching video file found under these roots is eligible for "
                "duplicate analysis. The selected roots also drive the physical-drive "
                "planning shown below."
            ),
        )
        self._set_sources_tooltip(
            self.add_root_btn,
            (
                "Browse for one more folder and add it to the scan list.\n\n"
                "Use this when you want to include another library, drive, or "
                "archive location in the same scan."
            ),
        )
        self._set_sources_tooltip(
            self.remove_root_btn,
            (
                "Remove only the currently selected folder from the scan list.\n\n"
                "This does not delete anything from disk. It only changes which "
                "roots will be scanned next."
            ),
        )
        self._set_sources_tooltip(
            self.remove_all_roots_btn,
            (
                "Clear the entire scan-folder list.\n\n"
                "Use this when you want to rebuild the source selection from scratch."
            ),
        )
        self._set_sources_tooltip(
            self.add_recent_root_btn,
            (
                "Open the recent-folder menu and add a previously used source root.\n\n"
                "This is a fast way to restore common scan locations without "
                "browsing again."
            ),
        )
        self._set_sources_tooltip(
            self.save_scan_set_btn,
            (
                "Save the current combination of scan folders, extensions, and "
                "profile as a reusable scan set.\n\n"
                "Saved scan sets help you switch between recurring jobs quickly."
            ),
        )
        self._set_sources_tooltip(
            self.load_saved_scan_btn,
            (
                "Load a previously saved scan set back into the Sources tab.\n\n"
                "This restores roots, extensions, and similarity profile for a "
                "known scanning configuration."
            ),
        )
        self._set_sources_tooltip(
            self.sources_scan_btn,
            (
                "Open the Scan tab to review progress controls and start the scan.\n\n"
                "Use this as a quick handoff after you finish choosing source "
                "folders and scan options."
            ),
        )

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
        self._configure_named_widget(
            self.extensions_preset_combo,
            object_name="extensions_preset_combo",
            widget_alias="Extensions Preset",
        )
        self.extensions_edit = QLineEdit(self.sources_tab)
        self.extensions_edit.textEdited.connect(self._extensions_text_edited)
        self._configure_named_widget(
            self.extensions_edit,
            object_name="sources_extensions_edit",
            widget_alias="Extensions",
        )
        self.scan_size_mib_min_spin = QSpinBox(self.sources_tab)
        self.scan_size_mib_min_spin.setRange(0, 1024 * 1024)
        self.scan_size_mib_min_spin.setSpecialValueText("Any")
        self.scan_size_mib_min_spin.setSuffix(" MiB")
        self._configure_named_widget(
            self.scan_size_mib_min_spin,
            object_name="sources_scan_size_mib_min_spin",
            widget_alias="Scan Size MiB Min",
        )
        self.scan_size_mib_max_spin = QSpinBox(self.sources_tab)
        self.scan_size_mib_max_spin.setRange(0, 1024 * 1024)
        self.scan_size_mib_max_spin.setSpecialValueText("Any")
        self.scan_size_mib_max_spin.setSuffix(" MiB")
        self._configure_named_widget(
            self.scan_size_mib_max_spin,
            object_name="sources_scan_size_mib_max_spin",
            widget_alias="Scan Size MiB Max",
        )
        self.profile_combo = QComboBox(self.sources_tab)
        self.profile_combo.addItems(
            ["balanced", "conservative", "aggressive", "custom"]
        )
        self._configure_named_widget(
            self.profile_combo,
            object_name="sources_profile_combo",
            widget_alias="Similarity Profile",
        )
        self.profile_combo.currentTextChanged.connect(
            self._update_custom_similarity_controls_visibility
        )
        self.custom_similarity_threshold_slider = QSlider(
            Qt.Orientation.Horizontal,
            self.sources_tab,
        )
        self.custom_similarity_threshold_slider.setRange(1, 30)
        self._configure_named_widget(
            self.custom_similarity_threshold_slider,
            object_name="sources_custom_similarity_threshold_slider",
            widget_alias="Custom Similarity Threshold Slider",
        )
        self.custom_similarity_threshold_spin = QDoubleSpinBox(self.sources_tab)
        self.custom_similarity_threshold_spin.setRange(0.01, 0.30)
        self.custom_similarity_threshold_spin.setDecimals(2)
        self.custom_similarity_threshold_spin.setSingleStep(0.01)
        self._configure_named_widget(
            self.custom_similarity_threshold_spin,
            object_name="sources_custom_similarity_threshold_spin",
            widget_alias="Custom Similarity Threshold Spin",
        )
        self.custom_similarity_threshold_slider.valueChanged.connect(
            self._sync_custom_similarity_threshold_from_slider
        )
        self.custom_similarity_threshold_spin.valueChanged.connect(
            self._sync_custom_similarity_threshold_from_spin
        )
        self.duration_tolerance_spin = QDoubleSpinBox(self.sources_tab)
        self.duration_tolerance_spin.setRange(0.0, 30.0)
        self.duration_tolerance_spin.setDecimals(1)
        self.duration_tolerance_spin.setSingleStep(0.5)
        self.duration_tolerance_spin.setSuffix(" s")
        self._configure_named_widget(
            self.duration_tolerance_spin,
            object_name="sources_duration_tolerance_spin",
            widget_alias="Duration Tolerance Seconds",
        )
        self.scene_aware_sampling_check = QCheckBox("Enabled", self.sources_tab)
        self._configure_named_widget(
            self.scene_aware_sampling_check,
            object_name="sources_scene_aware_sampling_check",
            widget_alias="Scene Aware Sampling",
        )
        self.audio_fingerprint_enabled_check = QCheckBox(
            "Enabled",
            self.sources_tab,
        )
        self._configure_named_widget(
            self.audio_fingerprint_enabled_check,
            object_name="sources_audio_fingerprint_enabled_check",
            widget_alias="Audio Fingerprinting",
        )
        self.cross_resolution_mode_combo = QComboBox(self.sources_tab)
        self.cross_resolution_mode_combo.addItem("Off", "off")
        self.cross_resolution_mode_combo.addItem("Same aspect", "same_aspect")
        self.cross_resolution_mode_combo.addItem("Any aspect", "any_aspect")
        self._configure_named_widget(
            self.cross_resolution_mode_combo,
            object_name="sources_cross_resolution_mode_combo",
            widget_alias="Cross Resolution Mode",
        )

        self.max_workers_spin = QSpinBox(self.sources_tab)
        self.max_workers_spin.setRange(1, 16)
        self.max_workers_spin.valueChanged.connect(
            self._refresh_sources_physical_drive_view
        )
        self._configure_named_widget(
            self.max_workers_spin,
            object_name="sources_max_workers_spin",
            widget_alias="Max Workers",
        )
        self.fingerprint_timeout_spin = QDoubleSpinBox(self.sources_tab)
        self.fingerprint_timeout_spin.setRange(0.1, 3600.0)
        self.fingerprint_timeout_spin.setDecimals(1)
        self.fingerprint_timeout_spin.setSingleStep(0.5)
        self.fingerprint_timeout_spin.setSuffix(" s")
        self._configure_named_widget(
            self.fingerprint_timeout_spin,
            object_name="sources_fingerprint_timeout_spin",
            widget_alias="Fingerprint Decode Timeout",
        )
        self.probe_backend_combo = QComboBox(self.sources_tab)
        self.probe_backend_combo.addItems(["pyav", "ffprobe"])
        self._configure_named_widget(
            self.probe_backend_combo,
            object_name="probe_backend_combo",
            widget_alias="Probe Backend",
        )
        self.probe_mode_combo = QComboBox(self.sources_tab)
        self.probe_mode_combo.addItems(["balanced", "burst"])
        self._configure_named_widget(
            self.probe_mode_combo,
            object_name="sources_probe_mode_combo",
            widget_alias="Probe Mode",
        )
        self.probe_backend_combo.currentTextChanged.connect(
            self._refresh_sources_physical_drive_view
        )
        self.probe_mode_combo.currentTextChanged.connect(
            self._refresh_sources_physical_drive_view
        )
        self.ffmpeg_exe_path_edit = QLineEdit(self.sources_tab)
        self.ffmpeg_exe_path_browse_btn = QPushButton("Browse...", self.sources_tab)
        self._configure_named_widget(
            self.ffmpeg_exe_path_edit,
            object_name="sources_ffmpeg_exe_path_edit",
            widget_alias="ffmpeg Path Override",
        )
        self._configure_named_widget(
            self.ffmpeg_exe_path_browse_btn,
            object_name="sources_ffmpeg_exe_path_browse_btn",
            widget_alias="Browse ffmpeg Path",
        )
        self.ffmpeg_exe_path_browse_btn.clicked.connect(
            lambda: self._browse_executable_path(
                self.ffmpeg_exe_path_edit,
                "ffmpeg",
            )
        )
        self.ffprobe_exe_path_edit = QLineEdit(self.sources_tab)
        self.ffprobe_exe_path_browse_btn = QPushButton("Browse...", self.sources_tab)
        self._configure_named_widget(
            self.ffprobe_exe_path_edit,
            object_name="sources_ffprobe_exe_path_edit",
            widget_alias="ffprobe Path Override",
        )
        self._configure_named_widget(
            self.ffprobe_exe_path_browse_btn,
            object_name="sources_ffprobe_exe_path_browse_btn",
            widget_alias="Browse ffprobe Path",
        )
        self.ffprobe_exe_path_browse_btn.clicked.connect(
            lambda: self._browse_executable_path(
                self.ffprobe_exe_path_edit,
                "ffprobe",
            )
        )
        self.fpcalc_exe_path_edit = QLineEdit(self.sources_tab)
        self.fpcalc_exe_path_browse_btn = QPushButton("Browse...", self.sources_tab)
        self._configure_named_widget(
            self.fpcalc_exe_path_edit,
            object_name="sources_fpcalc_exe_path_edit",
            widget_alias="fpcalc Path Override",
        )
        self._configure_named_widget(
            self.fpcalc_exe_path_browse_btn,
            object_name="sources_fpcalc_exe_path_browse_btn",
            widget_alias="Browse fpcalc Path",
        )
        self.fpcalc_exe_path_browse_btn.clicked.connect(
            lambda: self._browse_executable_path(
                self.fpcalc_exe_path_edit,
                "fpcalc",
            )
        )
        self.mediainfo_exe_path_edit = QLineEdit(self.sources_tab)
        self.mediainfo_exe_path_browse_btn = QPushButton("Browse...", self.sources_tab)
        self._configure_named_widget(
            self.mediainfo_exe_path_edit,
            object_name="sources_mediainfo_exe_path_edit",
            widget_alias="MediaInfo Path Override",
        )
        self._configure_named_widget(
            self.mediainfo_exe_path_browse_btn,
            object_name="sources_mediainfo_exe_path_browse_btn",
            widget_alias="Browse MediaInfo Path",
        )
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
        self._configure_named_widget(
            self.thumbnail_size_combo,
            object_name="sources_thumbnail_size_combo",
            widget_alias="Thumbnail Preview Size",
        )
        self.scan_parent_cpu_priority_combo = QComboBox(self.sources_tab)
        self._configure_named_widget(
            self.scan_parent_cpu_priority_combo,
            object_name="scan_parent_cpu_priority_combo",
            widget_alias="Scan Parent CPU Priority",
        )
        self.scan_parent_io_mode_combo = QComboBox(self.sources_tab)
        self._configure_named_widget(
            self.scan_parent_io_mode_combo,
            object_name="scan_parent_io_mode_combo",
            widget_alias="Scan Parent IO Mode",
        )
        self.scan_child_cpu_priority_combo = QComboBox(self.sources_tab)
        self._configure_named_widget(
            self.scan_child_cpu_priority_combo,
            object_name="scan_child_cpu_priority_combo",
            widget_alias="Scan Child CPU Priority",
        )
        self.scan_child_io_mode_combo = QComboBox(self.sources_tab)
        self._configure_named_widget(
            self.scan_child_io_mode_combo,
            object_name="scan_child_io_mode_combo",
            widget_alias="Scan Child IO Mode",
        )
        for label, value in CPU_PRIORITY_OPTIONS:
            self.scan_parent_cpu_priority_combo.addItem(label, value)
            self.scan_child_cpu_priority_combo.addItem(label, value)
        for label, value in IO_MODE_OPTIONS:
            self.scan_parent_io_mode_combo.addItem(label, value)
            self.scan_child_io_mode_combo.addItem(label, value)
        self._set_sources_tooltip(
            self.extensions_preset_combo,
            (
                "Choose a prepared extension list for the scan.\n\n"
                "Use this when you want to switch quickly between narrower or "
                "broader media coverage. The preset fills the custom extensions "
                "field below."
            ),
        )
        self._set_sources_tooltip(
            self.extensions_edit,
            (
                "Enter the video file extensions that should be scanned.\n\n"
                "Use commas, and dots are optional. Only files whose suffix matches "
                "this list are enumerated."
            ),
        )
        self._set_sources_tooltip(
            self.scan_size_mib_min_spin,
            (
                "Exclude files smaller than this size from scan discovery.\n\n"
                "This filter is applied during enumeration, so files below the "
                "threshold never enter duplicate analysis. Set 0 for Any."
            ),
        )
        self._set_sources_tooltip(
            self.scan_size_mib_max_spin,
            (
                "Exclude files larger than this size from scan discovery.\n\n"
                "Use this to cap very large files during enumeration. Set 0 for Any, "
                "which means no upper size limit is enforced."
            ),
        )
        self._set_sources_tooltip(
            self.profile_combo,
            (
                "Choose how strict duplicate matching should be.\n\n"
                "Balanced is the everyday default. Conservative reduces false "
                "positives, aggressive is more willing to group near-matches, and "
                "Custom lets you choose an exact numeric threshold."
            ),
        )
        custom_similarity_tooltip = (
            "Set the exact similarity threshold used when the Custom profile is "
            "selected.\n\n"
            "Lower values are stricter and higher values are more permissive. "
            "This control appears only while the Custom profile is active."
        )
        self._set_sources_tooltip(
            self.custom_similarity_threshold_slider,
            custom_similarity_tooltip,
        )
        self._set_sources_tooltip(
            self.custom_similarity_threshold_spin,
            custom_similarity_tooltip,
        )
        self._set_sources_tooltip(
            self.duration_tolerance_spin,
            (
                "Allow duplicates to match even when their durations differ "
                "slightly.\n\n"
                "Use this for intro/outro trims, tiny remux timestamp drift, or "
                "near-identical copies with a few seconds added or removed."
            ),
        )
        self._set_sources_tooltip(
            self.scene_aware_sampling_check,
            (
                "Use ffmpeg scene detection to choose fingerprint timestamps from "
                "scene boundaries instead of fixed percentages.\n\n"
                "This can improve matching when intros or outros vary in length."
            ),
        )
        self._set_sources_tooltip(
            self.audio_fingerprint_enabled_check,
            (
                "Enable optional audio fingerprint rescue matching.\n\n"
                "When visual hashes miss a near-identical copy, matching audio can "
                "still group the pair if duration and aspect checks remain plausible."
            ),
        )
        self._set_sources_tooltip(
            self.cross_resolution_mode_combo,
            (
                "Control how strictly aspect ratio must match before files can be "
                "compared.\n\n"
                "Off keeps the original strict gate, Same aspect allows mild crop "
                "or remux differences, and Any aspect disables the aspect gate."
            ),
        )
        self._set_sources_tooltip(
            self.max_workers_spin,
            (
                "Set the total worker budget for the scan.\n\n"
                "Higher values can improve throughput on fast storage, but they can "
                "also increase disk contention. The physical-drive planner below "
                "shows how this budget is distributed."
            ),
        )
        self._set_sources_tooltip(
            self.fingerprint_timeout_spin,
            (
                "Set the base timeout for one fingerprint decoder attempt.\n\n"
                "Increase this for slow disks or stubborn media files that need "
                "more time to yield sampled frames. Risky legacy formats still "
                "apply their separate timeout multiplier on top of this base value."
            ),
        )
        self._set_sources_tooltip(
            self.probe_backend_combo,
            (
                "Choose how video metadata is extracted before matching.\n\n"
                "PyAV stays inside the Python process, while ffprobe shells out to "
                "the ffprobe executable and parses JSON output."
            ),
        )
        self._set_sources_tooltip(
            self.probe_mode_combo,
            (
                "Choose how aggressively probe work is dispatched.\n\n"
                "Balanced is safer for slower disks. Burst is better suited to fast "
                "SSDs when you want more concurrency."
            ),
        )
        self._set_sources_tooltip(
            self.scan_parent_cpu_priority_combo,
            (
                "Choose the CPU priority for the Video Duperz process while a scan is "
                "active.\n\n"
                "This affects the main app process only during scans and is restored "
                "after the scan finishes, pauses, fails, or is cancelled."
            ),
        )
        self._set_sources_tooltip(
            self.scan_parent_io_mode_combo,
            (
                "Choose the scan-time I/O mode for the Video Duperz process.\n\n"
                "Background mode asks Windows to deprioritize this process's scan "
                "I/O so foreground work stays responsive."
            ),
        )
        self._set_sources_tooltip(
            self.scan_child_cpu_priority_combo,
            (
                "Choose the CPU priority for scan-related child processes.\n\n"
                "This affects heavy probe and fingerprint subprocesses only. It does "
                "not affect Explorer, MediaInfo, Everything, or custom commands."
            ),
        )
        self._set_sources_tooltip(
            self.scan_child_io_mode_combo,
            (
                "Choose the I/O mode for scan-related child processes.\n\n"
                "Background mode lowers the I/O impact of ffprobe, ffmpeg, and "
                "fingerprint-child subprocesses during scanning."
            ),
        )
        ffmpeg_tooltip = (
            "Optional override path for the ffmpeg executable.\n\n"
            "Leave this blank to use ffmpeg from PATH. Set it when you want the app "
            "to use a specific ffmpeg build for fingerprint frame extraction."
        )
        ffprobe_tooltip = (
            "Optional override path for the ffprobe executable.\n\n"
            "Leave this blank to use ffprobe from PATH. Set it when you want to pin "
            "metadata extraction to a specific ffprobe build."
        )
        fpcalc_tooltip = (
            "Optional override path for the fpcalc executable used for audio "
            "fingerprints.\n\n"
            "Leave this blank to use fpcalc from PATH. This is only needed when "
            "audio fingerprinting is enabled."
        )
        mediainfo_tooltip = (
            "Optional override path for the MediaInfo executable used from the "
            "Results tab.\n\n"
            "Leave this blank to use MediaInfo from PATH."
        )
        browse_tooltip = (
            "Browse for an executable path and copy it into the override field.\n\n"
            "Use this when the tool is not on PATH or when you want to pin a "
            "specific installed binary."
        )
        self._set_sources_tooltip(self.ffmpeg_exe_path_edit, ffmpeg_tooltip)
        self._set_sources_tooltip(self.ffprobe_exe_path_edit, ffprobe_tooltip)
        self._set_sources_tooltip(self.fpcalc_exe_path_edit, fpcalc_tooltip)
        self._set_sources_tooltip(self.mediainfo_exe_path_edit, mediainfo_tooltip)
        self._set_sources_tooltip(self.ffmpeg_exe_path_browse_btn, browse_tooltip)
        self._set_sources_tooltip(self.ffprobe_exe_path_browse_btn, browse_tooltip)
        self._set_sources_tooltip(self.fpcalc_exe_path_browse_btn, browse_tooltip)
        self._set_sources_tooltip(self.mediainfo_exe_path_browse_btn, browse_tooltip)
        self._set_sources_tooltip(
            self.thumbnail_size_combo,
            (
                "Choose the thumbnail preview size used in the Results tab.\n\n"
                "Larger previews are easier to inspect but take more space and can "
                "make the table denser."
            ),
        )

    def _create_sources_group_box(
        self,
        title: str,
        *,
        object_name: str,
        widget_alias: str,
        tooltip: str,
    ) -> tuple[QGroupBox, QVBoxLayout]:
        """Build one named Sources-tab group box with shared spacing and tooltip."""
        group_box = QGroupBox(title, self.sources_tab)
        self._configure_named_widget(
            group_box,
            object_name=object_name,
            widget_alias=widget_alias,
        )
        self._set_sources_tooltip(group_box, tooltip)
        layout = QVBoxLayout(group_box)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(10)
        return group_box, layout

    @staticmethod
    def _configure_named_widget(
        widget: QWidget,
        *,
        object_name: str,
        widget_alias: str,
    ) -> None:
        """Attach stable widget naming metadata for tests and diagnostics."""
        widget.setObjectName(object_name)
        widget.setProperty("widget_id", object_name)
        widget.setProperty("widget_alias", widget_alias)

    @staticmethod
    def _set_sources_tooltip(widget: QWidget, tooltip: str) -> None:
        """Attach one detailed tooltip to a Sources-tab widget."""
        widget.setToolTip(tooltip)
        widget.setWhatsThis(tooltip)

    def _create_sources_form_table(
        self,
        *,
        object_name: str,
        widget_alias: str,
    ) -> tuple[QWidget, QGridLayout]:
        """Build one two-column Sources-tab form container."""
        table_widget = QWidget(self.sources_tab)
        self._configure_named_widget(
            table_widget,
            object_name=object_name,
            widget_alias=widget_alias,
        )
        table_layout = QGridLayout(table_widget)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setHorizontalSpacing(12)
        table_layout.setVerticalSpacing(8)
        table_layout.setColumnStretch(1, 1)
        return table_widget, table_layout

    def _add_sources_form_row(
        self,
        table_layout: QGridLayout,
        row: int,
        table_widget: QWidget,
        label_text: str,
        control: QWidget,
        *,
        tooltip: str,
    ) -> QLabel:
        """Add one left-label row to a Sources-tab form table."""
        label = QLabel(label_text, table_widget)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._set_sources_tooltip(label, tooltip)
        self._set_sources_tooltip(control, tooltip)
        table_layout.addWidget(label, row, 0)
        table_layout.addWidget(control, row, 1)
        return label

    def _build_custom_similarity_threshold_control(self) -> QWidget:
        """Build one inline slider-plus-spin control for the custom threshold."""
        row = QWidget(self.sources_tab)
        self._configure_named_widget(
            row,
            object_name="sources_custom_similarity_threshold_row",
            widget_alias="Custom Similarity Threshold Row",
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.custom_similarity_threshold_slider, stretch=1)
        layout.addWidget(self.custom_similarity_threshold_spin)
        return row

    def _sync_custom_similarity_threshold_from_slider(self, value: int) -> None:
        """Keep the custom-threshold spin box aligned with the slider."""
        target = float(max(1, min(30, int(value)))) / 100.0
        self.custom_similarity_threshold_spin.blockSignals(True)
        self.custom_similarity_threshold_spin.setValue(target)
        self.custom_similarity_threshold_spin.blockSignals(False)

    def _sync_custom_similarity_threshold_from_spin(self, value: float) -> None:
        """Keep the custom-threshold slider aligned with the spin box."""
        target = max(1, min(30, round(float(value) * 100.0)))
        self.custom_similarity_threshold_slider.blockSignals(True)
        self.custom_similarity_threshold_slider.setValue(target)
        self.custom_similarity_threshold_slider.blockSignals(False)

    def _update_custom_similarity_controls_visibility(self) -> None:
        """Show the numeric threshold row only while the Custom profile is active."""
        visible = self.profile_combo.currentText().strip().lower() == "custom"
        label = getattr(self, "custom_similarity_threshold_label", None)
        row = getattr(self, "custom_similarity_threshold_row", None)
        if isinstance(label, QLabel):
            label.setVisible(visible)
        if isinstance(row, QWidget):
            row.setVisible(visible)

    def _build_executable_override_control(
        self,
        path_edit: QLineEdit,
        browse_button: QPushButton,
    ) -> QWidget:
        """Build one inline executable override control row."""
        path_row = QWidget(self.sources_tab)
        path_row_layout = QHBoxLayout(path_row)
        path_row_layout.setContentsMargins(0, 0, 0, 0)
        path_row_layout.setSpacing(8)
        path_row_layout.addWidget(path_edit, stretch=1)
        path_row_layout.addWidget(browse_button)
        return path_row

    def _build_sources_drive_widgets(self) -> None:
        self.sources_drive_summary_label = QLabel(
            "Matched physical drives: 0 | Requested workers: 1 | Effective workers: 0",
            self.sources_tab,
        )
        self._configure_named_widget(
            self.sources_drive_summary_label,
            object_name="sources_drive_summary_label",
            widget_alias="Physical Drives Summary",
        )
        self._set_sources_tooltip(
            self.sources_drive_summary_label,
            (
                "Summarizes how the current scan roots map to local physical "
                "drives.\n\n"
                "Requested workers is your global worker target. Effective workers "
                "reflect the per-drive plan after root matching and any overrides."
            ),
        )
        self.sources_drive_table = QTableWidget(0, 9, self.sources_tab)
        self._configure_named_widget(
            self.sources_drive_table,
            object_name="sources_drive_table",
            widget_alias="Physical Drives Table",
        )
        self._set_sources_tooltip(
            self.sources_drive_table,
            (
                "Shows the local-drive planning data used to spread scan work.\n\n"
                "Matched rows belong to the selected scan roots. The Workers column "
                "lets you tune per-drive concurrency without changing unrelated drives."
            ),
        )
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
        self.sources_drive_table.setMinimumHeight(250)
        self.sources_drive_table.verticalHeader().setVisible(False)
        drives_header = self.sources_drive_table.horizontalHeader()
        drives_header.setStretchLastSection(False)
        drives_header.sectionResized.connect(
            self._on_sources_drive_table_column_resized
        )
        for index, width in enumerate(SOURCES_DRIVE_DEFAULT_WIDTHS):
            drives_header.setSectionResizeMode(
                index,
                QHeaderView.ResizeMode.Interactive,
            )
            self.sources_drive_table.setColumnWidth(index, width)
        header_tooltips = [
            (
                "The scan root or local-drive row this entry represents.\n\n"
                "Matched roots are emphasized because they participate in the active "
                "scan plan."
            ),
            (
                "The disk identifiers reported for the underlying physical drive.\n\n"
                "These help show which roots are sharing the same hardware."
            ),
            (
                "The normalized volume identity used by the planner.\n\n"
                "Per-drive worker overrides are keyed by this identity."
            ),
            ("Total capacity reported for the matched drive or volume."),
            ("Currently free capacity reported for the matched drive or volume."),
            ("Approximate percentage of the drive that is already used."),
            (
                "Worker count assigned to this drive.\n\n"
                "Editable rows let you override concurrency for matched drives."
            ),
            ("Whether this row belongs to the roots currently selected for scanning."),
            (
                "Additional lookup or planning notes.\n\n"
                "This can explain missing drive metadata or why a row is "
                "informational only."
            ),
        ]
        for index, tooltip in enumerate(header_tooltips):
            header_item = self.sources_drive_table.horizontalHeaderItem(index)
            if header_item is not None:
                header_item.setToolTip(tooltip)

    def _on_sources_drive_table_column_resized(
        self,
        _section: int,
        _old_size: int,
        _new_size: int,
    ) -> None:
        """Persist live Sources drive-table widths when the user resizes them."""
        if self._applying_sources_drive_table_column_widths:
            return
        self._sources_drive_table_column_widths = (
            self._capture_sources_drive_table_column_widths()
        )

    def _capture_sources_drive_table_column_widths(self) -> list[int]:
        """Capture the current live Sources drive-table widths."""
        return [
            self.sources_drive_table.columnWidth(index)
            for index in range(self.sources_drive_table.columnCount())
        ]

    def _set_sources_drive_table_column_widths(self, widths: list[int]) -> None:
        """Apply one complete width payload to the Sources drive table."""
        if len(widths) != self.sources_drive_table.columnCount():
            return
        self._applying_sources_drive_table_column_widths = True
        try:
            for index, width in enumerate(widths):
                self.sources_drive_table.setColumnWidth(index, int(width))
        finally:
            self._applying_sources_drive_table_column_widths = False
        self._sources_drive_table_column_widths = (
            self._capture_sources_drive_table_column_widths()
        )

    def _fit_sources_drive_table_columns(self) -> None:
        """Auto-fit the Sources drive-table columns and keep the result."""
        self.sources_drive_table.resizeColumnsToContents()
        fitted = self._capture_sources_drive_table_column_widths()
        widened = [
            max(width, default_width)
            for width, default_width in zip(
                fitted,
                SOURCES_DRIVE_DEFAULT_WIDTHS,
                strict=True,
            )
        ]
        self._set_sources_drive_table_column_widths(widened)

    def _build_sources_layout(self, roots_actions: QHBoxLayout) -> None:
        scan_folders_group, scan_folders_layout = self._create_sources_group_box(
            "Scan Folders",
            object_name="sources_scan_folders_group",
            widget_alias="Scan Folders Group",
            tooltip=(
                "Define which folders belong to the next scan.\n\n"
                "These roots control file discovery, saved scan sets, and the "
                "physical-drive planning shown just below."
            ),
        )
        scan_folders_layout.addWidget(self.roots_list, stretch=1)
        scan_folders_layout.addLayout(roots_actions)

        physical_drives_group, physical_drives_layout = self._create_sources_group_box(
            "Physical Drives",
            object_name="sources_physical_drives_group",
            widget_alias="Physical Drives Group",
            tooltip=(
                "Review how the selected scan roots map to local physical drives.\n\n"
                "This section explains the drive-aware worker plan and lets you tune "
                "per-drive worker counts when a specific disk needs gentler or more "
                "aggressive scheduling."
            ),
        )
        physical_drives_layout.addWidget(self.sources_drive_summary_label)
        physical_drives_layout.addWidget(self.sources_drive_table, stretch=1)

        scan_content_group, scan_content_layout = self._create_sources_group_box(
            "Scan Content",
            object_name="sources_scan_content_group",
            widget_alias="Scan Content Group",
            tooltip=(
                "Choose what kinds of files are scanned and how strict matching "
                "should be.\n\n"
                "Use these controls to define scan coverage and duplicate sensitivity."
            ),
        )
        scan_content_table, scan_content_table_layout = self._create_sources_form_table(
            object_name="sources_scan_content_form_table",
            widget_alias="Scan Content Form Table",
        )
        self._add_sources_form_row(
            scan_content_table_layout,
            0,
            scan_content_table,
            "Extensions preset",
            self.extensions_preset_combo,
            tooltip=self.extensions_preset_combo.toolTip(),
        )
        self._add_sources_form_row(
            scan_content_table_layout,
            1,
            scan_content_table,
            "Extensions (comma-separated, no dots required)",
            self.extensions_edit,
            tooltip=self.extensions_edit.toolTip(),
        )
        self._add_sources_form_row(
            scan_content_table_layout,
            2,
            scan_content_table,
            "Size MiB Min",
            self.scan_size_mib_min_spin,
            tooltip=self.scan_size_mib_min_spin.toolTip(),
        )
        self._add_sources_form_row(
            scan_content_table_layout,
            3,
            scan_content_table,
            "Size MiB Max",
            self.scan_size_mib_max_spin,
            tooltip=self.scan_size_mib_max_spin.toolTip(),
        )
        self._add_sources_form_row(
            scan_content_table_layout,
            4,
            scan_content_table,
            "Similarity profile",
            self.profile_combo,
            tooltip=self.profile_combo.toolTip(),
        )
        self.custom_similarity_threshold_row = (
            self._build_custom_similarity_threshold_control()
        )
        self.custom_similarity_threshold_label = self._add_sources_form_row(
            scan_content_table_layout,
            5,
            scan_content_table,
            "Custom threshold",
            self.custom_similarity_threshold_row,
            tooltip=self.custom_similarity_threshold_spin.toolTip(),
        )
        self._add_sources_form_row(
            scan_content_table_layout,
            6,
            scan_content_table,
            "Duration tolerance (s)",
            self.duration_tolerance_spin,
            tooltip=self.duration_tolerance_spin.toolTip(),
        )
        self._add_sources_form_row(
            scan_content_table_layout,
            7,
            scan_content_table,
            "Scene-aware sampling",
            self.scene_aware_sampling_check,
            tooltip=self.scene_aware_sampling_check.toolTip(),
        )
        self._add_sources_form_row(
            scan_content_table_layout,
            8,
            scan_content_table,
            "Audio fingerprinting",
            self.audio_fingerprint_enabled_check,
            tooltip=self.audio_fingerprint_enabled_check.toolTip(),
        )
        self._add_sources_form_row(
            scan_content_table_layout,
            9,
            scan_content_table,
            "Cross-resolution matching",
            self.cross_resolution_mode_combo,
            tooltip=self.cross_resolution_mode_combo.toolTip(),
        )
        scan_content_layout.addWidget(scan_content_table)
        scan_content_layout.addStretch(1)

        scan_performance_group, scan_performance_layout = (
            self._create_sources_group_box(
                "Scan Performance",
                object_name="sources_scan_performance_group",
                widget_alias="Scan Performance Group",
                tooltip=(
                    "Tune scan concurrency, probe behavior, and scan-time process "
                    "priority.\n\n"
                    "These settings affect how aggressively the app uses CPU and disk "
                    "resources while scanning."
                ),
            )
        )
        scan_performance_table, scan_performance_table_layout = (
            self._create_sources_form_table(
                object_name="sources_scan_performance_form_table",
                widget_alias="Scan Performance Form Table",
            )
        )
        for scan_performance_row, (label_text, control) in enumerate(
            (
                ("Max workers total", self.max_workers_spin),
                ("Fingerprint decode timeout", self.fingerprint_timeout_spin),
                ("Probe backend", self.probe_backend_combo),
                ("Probe mode", self.probe_mode_combo),
                (
                    "Parent CPU priority during scan",
                    self.scan_parent_cpu_priority_combo,
                ),
                ("Parent I/O mode during scan", self.scan_parent_io_mode_combo),
                (
                    "Child CPU priority during scan",
                    self.scan_child_cpu_priority_combo,
                ),
                ("Child I/O mode during scan", self.scan_child_io_mode_combo),
            )
        ):
            self._add_sources_form_row(
                scan_performance_table_layout,
                scan_performance_row,
                scan_performance_table,
                label_text,
                control,
                tooltip=control.toolTip(),
            )
        scan_performance_layout.addWidget(scan_performance_table)
        scan_performance_layout.addStretch(1)

        tools_group, tools_layout = self._create_sources_group_box(
            "Tool Paths & Preview",
            object_name="sources_tool_paths_preview_group",
            widget_alias="Tool Paths And Preview Group",
            tooltip=(
                "Override external tool paths when needed and choose how preview "
                "thumbnails should be shown in Results.\n\n"
                "Blank tool-path fields fall back to PATH lookup."
            ),
        )
        tools_table, tools_table_layout = self._create_sources_form_table(
            object_name="sources_tool_paths_form_table",
            widget_alias="Tool Paths Form Table",
        )
        self._add_sources_form_row(
            tools_table_layout,
            0,
            tools_table,
            "ffmpeg executable override",
            self._build_executable_override_control(
                self.ffmpeg_exe_path_edit,
                self.ffmpeg_exe_path_browse_btn,
            ),
            tooltip=self.ffmpeg_exe_path_edit.toolTip(),
        )
        self._add_sources_form_row(
            tools_table_layout,
            1,
            tools_table,
            "ffprobe executable override",
            self._build_executable_override_control(
                self.ffprobe_exe_path_edit,
                self.ffprobe_exe_path_browse_btn,
            ),
            tooltip=self.ffprobe_exe_path_edit.toolTip(),
        )
        self._add_sources_form_row(
            tools_table_layout,
            2,
            tools_table,
            "fpcalc executable override",
            self._build_executable_override_control(
                self.fpcalc_exe_path_edit,
                self.fpcalc_exe_path_browse_btn,
            ),
            tooltip=self.fpcalc_exe_path_edit.toolTip(),
        )
        self._add_sources_form_row(
            tools_table_layout,
            3,
            tools_table,
            "MediaInfo executable override",
            self._build_executable_override_control(
                self.mediainfo_exe_path_edit,
                self.mediainfo_exe_path_browse_btn,
            ),
            tooltip=self.mediainfo_exe_path_edit.toolTip(),
        )
        self._add_sources_form_row(
            tools_table_layout,
            4,
            tools_table,
            "Thumbnail preview size",
            self.thumbnail_size_combo,
            tooltip=self.thumbnail_size_combo.toolTip(),
        )
        tools_layout.addWidget(tools_table)
        tools_layout.addStretch(1)

        options_container = QWidget(self.sources_tab)
        self._configure_named_widget(
            options_container,
            object_name="sources_options_container",
            widget_alias="Sources Options Container",
        )
        options_layout = QGridLayout(options_container)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setHorizontalSpacing(12)
        options_layout.setVerticalSpacing(12)
        options_layout.addWidget(scan_content_group, 0, 0)
        options_layout.addWidget(scan_performance_group, 0, 1)
        options_layout.addWidget(tools_group, 1, 0, 1, 2)
        options_layout.setColumnStretch(0, 1)
        options_layout.setColumnStretch(1, 1)

        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.sources_scan_btn)

        layout = QVBoxLayout(self.sources_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        layout.addWidget(scan_folders_group, stretch=1)
        layout.addWidget(physical_drives_group, stretch=2)
        layout.addWidget(options_container)
        layout.addLayout(footer_layout)
        self._update_custom_similarity_controls_visibility()
