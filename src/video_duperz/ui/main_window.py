from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from threep_commons.desktop import open_path_in_default_app

from .. import __version__
from ..config import (
    DEFAULT_VIDEO_EXTENSION_PRESET,
    MAX_RECENT_ROOTS,
    VIDEO_EXTENSION_PRESET_NAMES,
    detect_video_extension_preset,
    normalize_thumbnail_size,
    save_settings,
    settings_path,
    video_extensions_csv_for_preset,
)
from ..db import Database
from ..exporters import export_scan
from ..models import SavedScanProfilePayload, Settings, utc_now_iso
from ..scan_sets import (
    build_scan_set_key,
    normalize_extensions,
    normalize_roots_for_display,
    normalize_similarity_profile,
)
from ..scanner import build_physical_drive_scan_plan, list_physical_drives
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
from .thumbnails import thumbnail_cache_dir
from .workers import ScanWorker

THUMBNAIL_SIZE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Small (80x45)", "80x45"),
    ("Default (96x54)", "96x54"),
    ("Medium (128x72)", "128x72"),
    ("Large (160x90)", "160x90"),
)
MAX_DRIVE_WORKERS = 64


class MainWindow(QMainWindow):
    def __init__(self, db: Database, settings: Settings, parent: QWidget | None = None) -> None:
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
        self._saved_column_views: dict[str, dict[str, list[int] | list[bool]]] = dict(settings.saved_column_views)
        self._saved_scan_profiles: dict[str, SavedScanProfilePayload] = dict(settings.saved_scan_profiles)
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
        self.results_view.set_thumbnail_frame_positions(settings.thumbnail_frame_a_pct, settings.thumbnail_frame_b_pct)
        self.results_view.set_identical_compare_config(
            block_mib=settings.identical_block_mib,
            sample_a_pct=settings.identical_sample_a_pct,
            sample_b_pct=settings.identical_sample_b_pct,
        )

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

    def _build_menus(self) -> None:
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
        self.clear_cached_thumbnails_action.triggered.connect(self._clear_cached_thumbnails)
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
            action.toggled.connect(lambda checked, col=index: self._set_column_visibility_from_menu(col, checked))
            columns_menu.addAction(action)
            self._column_toggle_actions.append(action)

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
            ("&7 Size difference between largest and smallest in group first", SORT_GROUP_SPREAD_DESC),
            ("&8 Size difference between largest and smallest in group last", SORT_GROUP_SPREAD_ASC),
        ]
        for label, mode in sort_specs:
            action = QAction(label, self)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, value=mode: self.results_view.set_sort_mode(value))
            self._sort_action_group.addAction(action)
            sort_menu.addAction(action)
            self._sort_actions[mode] = action

        actions_menu = self.menuBar().addMenu("&Actions")
        self.keep_best_action = QAction("&Select all, keep best", self)
        self.keep_best_action.triggered.connect(lambda: self.results_view.apply_keep_strategy("best"))
        actions_menu.addAction(self.keep_best_action)

        self.keep_worst_action = QAction("Select all, keep &worst", self)
        self.keep_worst_action.triggered.connect(lambda: self.results_view.apply_keep_strategy("worst"))
        actions_menu.addAction(self.keep_worst_action)

        self.keep_larger_action = QAction("Select all, keep &larger", self)
        self.keep_larger_action.triggered.connect(lambda: self.results_view.apply_keep_strategy("larger"))
        actions_menu.addAction(self.keep_larger_action)

        self.keep_smaller_action = QAction("Select all, keep s&maller", self)
        self.keep_smaller_action.triggered.connect(lambda: self.results_view.apply_keep_strategy("smaller"))
        actions_menu.addAction(self.keep_smaller_action)

        self.keep_newer_action = QAction("Select all, keep &newer", self)
        self.keep_newer_action.triggered.connect(lambda: self.results_view.apply_keep_strategy("newer"))
        actions_menu.addAction(self.keep_newer_action)

        self.keep_older_action = QAction("Select all, keep &older", self)
        self.keep_older_action.triggered.connect(lambda: self.results_view.apply_keep_strategy("older"))
        actions_menu.addAction(self.keep_older_action)

        actions_menu.addSeparator()
        self.delete_selected_action = QAction("&Delete Selected", self)
        self.delete_selected_action.triggered.connect(self.results_view.request_soft_delete_selected)
        actions_menu.addAction(self.delete_selected_action)

        self.delete_selected_permanent_action = QAction("&Permanently Delete Selected", self)
        self.delete_selected_permanent_action.triggered.connect(self.results_view.request_permanent_delete_selected)
        actions_menu.addAction(self.delete_selected_permanent_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        self.edit_ini_action = QAction("Edit &.ini File", self)
        self.edit_ini_action.triggered.connect(self._edit_ini_file)
        tools_menu.addAction(self.edit_ini_action)

        help_menu = self.menuBar().addMenu("&Help")
        self.about_action = QAction("&Help", self)
        self.about_action.setShortcut("F1")
        self.about_action.triggered.connect(self._show_about)
        help_menu.addAction(self.about_action)

    def _build_sources_tab(self) -> None:
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

        self.extensions_preset_combo = QComboBox(self.sources_tab)
        self.extensions_preset_combo.addItems(VIDEO_EXTENSION_PRESET_NAMES)
        default_preset_index = self.extensions_preset_combo.findText(DEFAULT_VIDEO_EXTENSION_PRESET)
        self.extensions_preset_combo.setCurrentIndex(max(0, default_preset_index))
        self.extensions_preset_combo.currentTextChanged.connect(self._extensions_preset_changed)
        self.extensions_edit = QLineEdit(self.sources_tab)
        self.extensions_edit.textEdited.connect(self._extensions_text_edited)
        self.profile_combo = QComboBox(self.sources_tab)
        self.profile_combo.addItems(["balanced", "conservative", "aggressive"])

        self.max_workers_spin = QSpinBox(self.sources_tab)
        self.max_workers_spin.setRange(1, 16)
        self.max_workers_spin.valueChanged.connect(self._refresh_sources_physical_drive_view)
        self.probe_mode_combo = QComboBox(self.sources_tab)
        self.probe_mode_combo.addItems(["balanced", "burst"])
        self.probe_mode_combo.currentTextChanged.connect(self._refresh_sources_physical_drive_view)
        self.thumbnail_size_combo = QComboBox(self.sources_tab)
        for label, size_key in THUMBNAIL_SIZE_OPTIONS:
            self.thumbnail_size_combo.addItem(label, size_key)
        self.thumbnail_size_combo.currentIndexChanged.connect(self._thumbnail_size_changed)
        self.sources_drive_summary_label = QLabel(
            "Matched physical drives: 0 | Requested workers: 1 | Effective workers: 0",
            self.sources_tab,
        )
        self.sources_drive_table = QTableWidget(0, 9, self.sources_tab)
        self.sources_drive_table.setHorizontalHeaderLabels(
            ["Root", "Disk token(s)", "Volume identity", "Total", "Free", "Used %", "Workers", "Matched", "Lookup note"]
        )
        self.sources_drive_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.sources_drive_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sources_drive_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
        layout.addWidget(QLabel("Probe mode"))
        layout.addWidget(self.probe_mode_combo)
        layout.addWidget(QLabel("Physical drives"))
        layout.addWidget(self.sources_drive_summary_label)
        layout.addWidget(self.sources_drive_table, stretch=1)
        layout.addWidget(QLabel("Thumbnail preview size"))
        layout.addWidget(self.thumbnail_size_combo)
        layout.addStretch(1)

        self._recent_roots_menu = QMenu(self)
        self._refresh_recent_roots_menu()
        self._saved_scans_menu = QMenu(self)
        self._refresh_saved_scans_menu()
        self._update_root_buttons_state()

    def _load_settings_to_widgets(self) -> None:
        self.roots_list.clear()
        for root in self.settings.scan_roots:
            self.roots_list.addItem(root)
        self.roots_list.setCurrentRow(-1)
        self._recent_roots = list(self.settings.recent_scan_roots)
        self._refresh_recent_roots_menu()
        normalized_extensions = normalize_extensions(list(self.settings.extensions))
        self.extensions_edit.setText(", ".join(normalized_extensions))
        preset_name = detect_video_extension_preset(normalized_extensions) or DEFAULT_VIDEO_EXTENSION_PRESET
        preset_index = self.extensions_preset_combo.findText(preset_name)
        self.extensions_preset_combo.blockSignals(True)
        self.extensions_preset_combo.setCurrentIndex(max(0, preset_index))
        self.extensions_preset_combo.blockSignals(False)
        idx = self.profile_combo.findText(self.settings.similarity_profile)
        self.profile_combo.setCurrentIndex(max(0, idx))
        self.max_workers_spin.setValue(max(1, int(self.settings.max_workers)))
        probe_index = self.probe_mode_combo.findText(self.settings.probe_worker_mode)
        self.probe_mode_combo.setCurrentIndex(max(0, probe_index))
        size_key = normalize_thumbnail_size(self.settings.thumbnail_size)
        self.thumbnail_size_combo.blockSignals(True)
        for i in range(self.thumbnail_size_combo.count()):
            if str(self.thumbnail_size_combo.itemData(i)) == size_key:
                self.thumbnail_size_combo.setCurrentIndex(i)
                break
        self.thumbnail_size_combo.blockSignals(False)
        self.results_view.set_thumbnail_size(size_key)
        self.results_view.set_thumbnail_frame_positions(
            self.settings.thumbnail_frame_a_pct,
            self.settings.thumbnail_frame_b_pct,
        )
        self.results_view.set_identical_compare_config(
            block_mib=self.settings.identical_block_mib,
            sample_a_pct=self.settings.identical_sample_a_pct,
            sample_b_pct=self.settings.identical_sample_b_pct,
        )
        visibility = self.settings.results_table_column_visibility
        if visibility:
            self.results_view.set_column_visibility(visibility)
        self.results_view.set_column_widths(self.settings.results_table_column_widths)
        self._saved_column_views = dict(self.settings.saved_column_views)
        self._refresh_saved_views_menu()
        self._saved_scan_profiles = dict(self.settings.saved_scan_profiles)
        self._refresh_saved_scans_menu()
        self._drive_worker_overrides = {
            str(key): max(1, int(value))
            for key, value in self.settings.drive_worker_overrides.items()
        }
        self._update_root_buttons_state()
        self._sync_column_toggle_actions()
        self._refresh_sources_physical_drive_view()

    def _settings_from_widgets(self) -> Settings:
        roots = [self.roots_list.item(i).text() for i in range(self.roots_list.count())]
        exts = [e.strip().lower().lstrip(".") for e in self.extensions_edit.text().split(",")]
        exts = [e for e in exts if e]
        drive_worker_overrides = self._normalized_drive_worker_overrides()
        return Settings(
            scan_roots=roots,
            recent_scan_roots=list(self._recent_roots),
            extensions=exts,
            similarity_profile=self.profile_combo.currentText(),
            max_workers=self.max_workers_spin.value(),
            preview_autoplay=self.settings.preview_autoplay,  # backward-compat only
            thumbnail_size=normalize_thumbnail_size(str(self.thumbnail_size_combo.currentData())),
            thumbnail_frame_a_pct=self.settings.thumbnail_frame_a_pct,
            thumbnail_frame_b_pct=self.settings.thumbnail_frame_b_pct,
            identical_block_mib=self.settings.identical_block_mib,
            identical_sample_a_pct=self.settings.identical_sample_a_pct,
            identical_sample_b_pct=self.settings.identical_sample_b_pct,
            results_table_column_widths=self.results_view.column_widths(),
            results_table_column_visibility=self.results_view.column_visibility(),
            saved_column_views=self._normalized_saved_column_views(),
            saved_scan_profiles=self._normalized_saved_scan_profiles(),
            keep_rule="best_quality",
            drive_worker_overrides=drive_worker_overrides,
            probe_worker_mode=self.probe_mode_combo.currentText().strip().lower() or "balanced",
            scan_db_batch_size=self.settings.scan_db_batch_size,
            scan_db_flush_interval_ms=self.settings.scan_db_flush_interval_ms,
            scan_enum_queue_max=self.settings.scan_enum_queue_max,
            scan_progress_emit_interval_ms=self.settings.scan_progress_emit_interval_ms,
            scan_progress_emit_every_files=self.settings.scan_progress_emit_every_files,
        )

    def _persist_settings(self) -> None:
        self.settings = self._settings_from_widgets()
        save_settings(self.settings)
        self.results_view.set_thumbnail_size(self.settings.thumbnail_size)
        self.results_view.set_thumbnail_frame_positions(
            self.settings.thumbnail_frame_a_pct,
            self.settings.thumbnail_frame_b_pct,
        )
        self.results_view.set_identical_compare_config(
            block_mib=self.settings.identical_block_mib,
            sample_a_pct=self.settings.identical_sample_a_pct,
            sample_b_pct=self.settings.identical_sample_b_pct,
        )

    def _on_tab_changed(self, index: int) -> None:
        if not self._scan_tab_locked:
            return
        scan_index = self.tabs.indexOf(self.scan_view)
        if scan_index < 0 or index == scan_index:
            return
        self.tabs.blockSignals(True)
        try:
            self.tabs.setCurrentIndex(scan_index)
        finally:
            self.tabs.blockSignals(False)

    def _set_scan_tab_lock(self, locked: bool) -> None:
        self._scan_tab_locked = bool(locked)
        sources_index = self.tabs.indexOf(self.sources_tab)
        scan_index = self.tabs.indexOf(self.scan_view)
        results_index = self.tabs.indexOf(self.results_view)
        if self._scan_tab_locked:
            if sources_index >= 0:
                self.tabs.setTabEnabled(sources_index, False)
            if results_index >= 0:
                self.tabs.setTabEnabled(results_index, False)
            if scan_index >= 0:
                self.tabs.setTabEnabled(scan_index, True)
                self.tabs.setCurrentIndex(scan_index)
            return
        for idx in (sources_index, scan_index, results_index):
            if idx >= 0:
                self.tabs.setTabEnabled(idx, True)

    def _thumbnail_size_changed(self) -> None:
        size_key = normalize_thumbnail_size(str(self.thumbnail_size_combo.currentData()))
        self.results_view.set_thumbnail_size(size_key)

    def _extensions_preset_changed(self, preset_name: str) -> None:
        self.extensions_edit.setText(video_extensions_csv_for_preset(preset_name))

    def _extensions_text_edited(self, _text: str) -> None:
        matched_preset = detect_video_extension_preset(self._current_sources_extensions())
        if not matched_preset:
            return
        target_index = self.extensions_preset_combo.findText(matched_preset)
        if target_index < 0 or target_index == self.extensions_preset_combo.currentIndex():
            return
        self.extensions_preset_combo.blockSignals(True)
        self.extensions_preset_combo.setCurrentIndex(target_index)
        self.extensions_preset_combo.blockSignals(False)

    def _normalized_saved_column_views(self) -> dict[str, dict[str, list[int] | list[bool]]]:
        normalized: dict[str, dict[str, list[int] | list[bool]]] = {}
        for name, payload in self._saved_column_views.items():
            cleaned_name = str(name).strip()
            if not cleaned_name:
                continue
            widths_raw = payload.get("widths", []) if isinstance(payload, dict) else []
            visibility_raw = payload.get("visibility", []) if isinstance(payload, dict) else []
            widths = [int(w) for w in widths_raw] if isinstance(widths_raw, list) else []
            visibility = [bool(v) for v in visibility_raw] if isinstance(visibility_raw, list) else []
            if len(widths) != len(self.results_view.column_labels()):
                continue
            if len(visibility) != len(self.results_view.column_labels()):
                continue
            if not any(visibility):
                continue
            normalized[cleaned_name] = {"widths": widths, "visibility": visibility}
        return normalized

    def _normalized_saved_scan_profiles(self) -> dict[str, SavedScanProfilePayload]:
        normalized: dict[str, SavedScanProfilePayload] = {}
        for name, payload in self._saved_scan_profiles.items():
            cleaned_name = str(name).strip()
            if not cleaned_name or len(cleaned_name) > 80:
                continue
            roots = normalize_roots_for_display(list(payload.roots))
            if not roots:
                continue
            profile = normalize_similarity_profile(payload.similarity_profile)
            extensions = normalize_extensions(list(payload.extensions))
            scan_set_key = str(payload.scan_set_key).strip() or build_scan_set_key(
                roots=roots,
                similarity_profile=profile,
                extensions=extensions,
            )
            updated_at = str(payload.updated_at).strip() or utc_now_iso()
            normalized[cleaned_name] = SavedScanProfilePayload(
                scan_set_key=scan_set_key,
                roots=roots,
                similarity_profile=profile,
                extensions=extensions,
                updated_at=updated_at,
            )
        return normalized

    def _fit_columns(self) -> None:
        self.results_view.fit_columns_to_contents()
        self._sync_column_toggle_actions()
        self.statusBar().showMessage("Columns fitted to contents.")

    def _save_current_view(self) -> None:
        name, ok = QInputDialog.getText(self, "Save Current View", "View name:")
        if not ok:
            return
        cleaned = name.strip()
        if not cleaned:
            return
        if cleaned in self._saved_column_views:
            replace = QMessageBox.question(
                self,
                "Overwrite View",
                f"A saved view named '{cleaned}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if replace != QMessageBox.StandardButton.Yes:
                return
        self._saved_column_views[cleaned] = {
            "widths": self.results_view.column_widths(),
            "visibility": self.results_view.column_visibility(),
        }
        self._refresh_saved_views_menu()
        self._persist_settings()
        self.statusBar().showMessage(f"Saved view '{cleaned}'.")

    def _refresh_saved_views_menu(self) -> None:
        if self._saved_views_menu is None:
            return
        self._saved_views_menu.clear()
        if not self._saved_column_views:
            empty_action = QAction("(No saved views)", self)
            empty_action.setEnabled(False)
            self._saved_views_menu.addAction(empty_action)
            return
        for name in sorted(self._saved_column_views):
            action = QAction(name, self)
            action.triggered.connect(lambda _checked=False, view_name=name: self._apply_saved_view(view_name))
            self._saved_views_menu.addAction(action)

    def _apply_saved_view(self, name: str) -> None:
        payload = self._saved_column_views.get(name)
        if not isinstance(payload, dict):
            return
        widths_raw = payload.get("widths", [])
        visibility_raw = payload.get("visibility", [])
        widths = [int(w) for w in widths_raw] if isinstance(widths_raw, list) else []
        visibility = [bool(v) for v in visibility_raw] if isinstance(visibility_raw, list) else []
        self.results_view.set_column_visibility(visibility)
        self.results_view.set_column_widths(widths)
        self._sync_column_toggle_actions()
        self.statusBar().showMessage(f"Applied view '{name}'.")

    def _set_column_visibility_from_menu(self, column_index: int, checked: bool) -> None:
        self.results_view.set_column_visible(column_index, checked)
        self._sync_column_toggle_actions()

    def _sync_column_toggle_actions(self) -> None:
        visibility = self.results_view.column_visibility()
        for index, action in enumerate(self._column_toggle_actions):
            if index >= len(visibility):
                break
            action.blockSignals(True)
            action.setChecked(bool(visibility[index]))
            action.blockSignals(False)

    def _add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select scan folder")
        if folder:
            self._add_root_path(folder)
            self._remember_recent_root(folder)

    def _remove_selected_root(self) -> None:
        row = self.roots_list.currentRow()
        if row >= 0:
            self.roots_list.takeItem(row)
            self._refresh_sources_physical_drive_view()
        self._update_root_buttons_state()

    def _remove_all_roots(self) -> None:
        if self.roots_list.count() <= 0:
            self._update_root_buttons_state()
            return
        self.roots_list.clear()
        self._refresh_sources_physical_drive_view()
        self._update_root_buttons_state()

    def _update_root_buttons_state(self, _row: int = -1) -> None:
        has_roots = self.roots_list.count() > 0
        has_selection = self.roots_list.currentRow() >= 0
        self.remove_root_btn.setEnabled(has_selection)
        self.remove_all_roots_btn.setEnabled(has_roots)

    def _normalize_root_path(self, path: str) -> str:
        return str(Path(path).expanduser()).strip()

    def _find_root_row(self, path: str) -> int:
        needle = path.casefold()
        for i in range(self.roots_list.count()):
            if self.roots_list.item(i).text().casefold() == needle:
                return i
        return -1

    def _add_root_path(self, path: str) -> None:
        normalized = self._normalize_root_path(path)
        if not normalized:
            return
        existing = self._find_root_row(normalized)
        if existing >= 0:
            self.roots_list.setCurrentRow(existing)
            self._update_root_buttons_state()
            return
        self.roots_list.addItem(normalized)
        self.roots_list.setCurrentRow(self.roots_list.count() - 1)
        self._refresh_sources_physical_drive_view()
        self._update_root_buttons_state()

    def _remember_recent_root(self, path: str) -> None:
        normalized = self._normalize_root_path(path)
        if not normalized:
            return
        deduped = [p for p in self._recent_roots if p.casefold() != normalized.casefold()]
        self._recent_roots = [normalized, *deduped][:MAX_RECENT_ROOTS]
        self._refresh_recent_roots_menu()

    def _refresh_recent_roots_menu(self) -> None:
        if self._recent_roots_menu is None:
            return
        self._recent_roots_menu.clear()
        if not self._recent_roots:
            empty = QAction("(No recent folders)", self)
            empty.setEnabled(False)
            self._recent_roots_menu.addAction(empty)
            self.add_recent_root_btn.setEnabled(False)
            return
        self.add_recent_root_btn.setEnabled(True)
        for folder in self._recent_roots:
            action = QAction(folder, self)
            action.triggered.connect(lambda _checked=False, value=folder: self._add_recent_root_selected(value))
            self._recent_roots_menu.addAction(action)
        self._recent_roots_menu.addSeparator()
        clear_action = QAction("Clear Recent Folders", self)
        clear_action.triggered.connect(self._clear_recent_roots)
        self._recent_roots_menu.addAction(clear_action)

    def _show_recent_roots_menu(self) -> None:
        if self._recent_roots_menu is None:
            return
        self._recent_roots_menu.exec(self.add_recent_root_btn.mapToGlobal(self.add_recent_root_btn.rect().bottomLeft()))

    def _add_recent_root_selected(self, path: str) -> None:
        self._add_root_path(path)
        self._remember_recent_root(path)

    def _clear_recent_roots(self) -> None:
        self._recent_roots = []
        self._refresh_recent_roots_menu()
        self._persist_settings()
        self.statusBar().showMessage("Recent folder history cleared.")

    def _clear_saved_scans(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Clear Saved Scans",
            "Clear all saved scan profiles and scan history? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._saved_scan_profiles = {}
        self.db.clear_all_scans()
        self.current_scan_id = None
        self.results_view.load_groups([])
        self.results_view.set_scan_context_note("")
        self._refresh_saved_scans_menu()
        self._persist_settings()
        self.statusBar().showMessage("Saved scans and scan history cleared.")

    def _clear_cached_thumbnails_internal(self) -> tuple[int, int]:
        cache_dir = thumbnail_cache_dir()
        removed = 0
        failed = 0
        if not cache_dir.exists():
            return removed, failed
        for child in cache_dir.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                    removed += 1
                else:
                    child.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                failed += 1
        return removed, failed

    def _clear_cached_thumbnails(self) -> None:
        removed, failed = self._clear_cached_thumbnails_internal()
        if failed > 0:
            self.statusBar().showMessage(f"Cleared cached thumbnails ({removed} item(s), {failed} failed).")
            return
        self.statusBar().showMessage(f"Cleared cached thumbnails ({removed} item(s)).")

    def _edit_ini_file(self) -> None:
        self._persist_settings()
        target = settings_path()
        try:
            if not open_path_in_default_app(target):
                raise RuntimeError("No default opener available on this platform")
            self.statusBar().showMessage(f"Opened settings file: {target}")
        except Exception as exc:
            QMessageBox.warning(self, "Open Settings Failed", str(exc))

    def _normalized_drive_worker_overrides(self) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for raw_identity, raw_value in self._drive_worker_overrides.items():
            identity = str(raw_identity).strip()
            if not identity:
                continue
            try:
                workers = int(raw_value)
            except (TypeError, ValueError):
                continue
            normalized[identity] = max(1, min(MAX_DRIVE_WORKERS, workers))
        return normalized

    def _on_drive_worker_override_changed(self, volume_identity: str, workers: int) -> None:
        identity = str(volume_identity).strip()
        if not identity:
            return
        self._drive_worker_overrides[identity] = max(1, min(MAX_DRIVE_WORKERS, int(workers)))
        if self._drive_workers_editing:
            return
        self._refresh_sources_physical_drive_view()

    def _format_byte_count(self, value: int | None) -> str:
        if value is None:
            return "n/a"
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        size = float(value)
        unit_idx = 0
        while size >= 1024.0 and unit_idx < len(units) - 1:
            size /= 1024.0
            unit_idx += 1
        return f"{size:.1f} {units[unit_idx]}"

    def _refresh_sources_physical_drive_view(self) -> None:
        roots = self._current_sources_roots()
        max_workers = max(1, int(self.max_workers_spin.value()))
        drive_worker_overrides = self._normalized_drive_worker_overrides()
        self._drive_worker_overrides = dict(drive_worker_overrides)
        drives = list_physical_drives()
        plan = build_physical_drive_scan_plan(
            roots=roots,
            max_workers=max_workers,
            drive_worker_overrides=drive_worker_overrides,
        )
        matched_identities = set(plan.matched_volume_identities)

        self.sources_drive_table.setRowCount(0)
        if not drives:
            self.sources_drive_table.setRowCount(1)
            self.sources_drive_table.setItem(0, 0, QTableWidgetItem("(No local drives detected)"))
            for col in range(1, self.sources_drive_table.columnCount()):
                self.sources_drive_table.setItem(0, col, QTableWidgetItem(""))
        else:
            self.sources_drive_table.setRowCount(len(drives))
            self._drive_workers_editing = True
            try:
                for row, drive in enumerate(drives):
                    tokens = ", ".join(drive.disk_tokens) if drive.disk_tokens else "(none)"
                    matched = drive.volume_identity in matched_identities
                    row_values = [
                        drive.root,
                        tokens,
                        drive.volume_identity,
                        self._format_byte_count(drive.total_bytes),
                        self._format_byte_count(drive.free_bytes),
                        f"{drive.used_percent:.1f}%" if drive.used_percent is not None else "n/a",
                        "Yes" if matched else "No",
                        drive.lookup_error or "",
                    ]
                    for col, value in enumerate(row_values):
                        target_col = col if col < 6 else col + 1
                        item = QTableWidgetItem(value)
                        if matched:
                            item.setBackground(QColor("#d9f7d9"))
                        if matched and target_col == 0:
                            font = item.font()
                            font.setBold(True)
                            item.setFont(font)
                        self.sources_drive_table.setItem(row, target_col, item)
                    if matched:
                        workers = drive_worker_overrides.get(drive.volume_identity, 1)
                        spin = QSpinBox(self.sources_drive_table)
                        spin.setRange(1, MAX_DRIVE_WORKERS)
                        spin.setValue(max(1, int(workers)))
                        spin.valueChanged.connect(
                            lambda value, volume=drive.volume_identity: self._on_drive_worker_override_changed(
                                volume,
                                value,
                            )
                        )
                        self.sources_drive_table.setCellWidget(row, 6, spin)
                    else:
                        self.sources_drive_table.setItem(row, 6, QTableWidgetItem("-"))
            finally:
                self._drive_workers_editing = False

        summary = (
            f"Matched physical drives: {len(matched_identities)} | "
            f"Requested workers: {plan.requested_worker_target} | "
            f"Effective workers: {plan.effective_total_workers}"
        )
        if plan.requested_worker_target > plan.effective_total_workers:
            summary = f"{summary} | Caps applied"
        self.sources_drive_summary_label.setText(summary)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Video Duperz",
            f"Video Duperz {__version__}\nWindows-first duplicate video finder.\nSettings: {settings_path()}",
        )

    def _request_full_reset(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Full Reset",
            "This will close Video Duperz, erase all app data (saved scans, settings, thumbnails), and relaunch.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._full_reset_requested = True
        self.close()

    def consume_full_reset_requested(self) -> bool:
        pending = bool(self._full_reset_requested)
        self._full_reset_requested = False
        return pending

    def _current_sources_roots(self) -> list[str]:
        return [self.roots_list.item(i).text() for i in range(self.roots_list.count())]

    def _current_sources_extensions(self) -> list[str]:
        raw = [e.strip().lower().lstrip(".") for e in self.extensions_edit.text().split(",")]
        return normalize_extensions([e for e in raw if e])

    def _current_sources_profile(self) -> str:
        return normalize_similarity_profile(self.profile_combo.currentText())

    def _build_profile_payload_from_sources(self) -> SavedScanProfilePayload | None:
        roots = normalize_roots_for_display(self._current_sources_roots())
        if not roots:
            return None
        profile = self._current_sources_profile()
        extensions = self._current_sources_extensions()
        return SavedScanProfilePayload(
            scan_set_key=build_scan_set_key(roots=roots, similarity_profile=profile, extensions=extensions),
            roots=roots,
            similarity_profile=profile,
            extensions=extensions,
            updated_at=utc_now_iso(),
        )

    def _save_current_scan_set_as(self) -> None:
        payload = self._build_profile_payload_from_sources()
        if payload is None:
            QMessageBox.warning(self, "Missing Sources", "Add at least one scan root before saving a scan set.")
            return
        name, ok = QInputDialog.getText(self, "Save Scan Set", "Profile name:")
        if not ok:
            return
        cleaned = str(name).strip()
        if not cleaned:
            return
        if len(cleaned) > 80:
            QMessageBox.warning(self, "Name Too Long", "Profile name must be 80 characters or fewer.")
            return
        if cleaned in self._saved_scan_profiles:
            replace = QMessageBox.question(
                self,
                "Overwrite Profile",
                f"A saved scan profile named '{cleaned}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if replace != QMessageBox.StandardButton.Yes:
                return
        self._saved_scan_profiles[cleaned] = payload
        self._refresh_saved_scans_menu()
        self._persist_settings()
        self.statusBar().showMessage(f"Saved scan set '{cleaned}'.")

    def _delete_named_scan_profile(self) -> None:
        names = sorted(self._saved_scan_profiles.keys(), key=str.casefold)
        if not names:
            self.statusBar().showMessage("No named scan profiles to delete.")
            return
        chosen, ok = QInputDialog.getItem(self, "Delete Named Profile", "Profile:", names, 0, False)
        if not ok:
            return
        name = str(chosen).strip()
        if not name:
            return
        confirm = QMessageBox.question(
            self,
            "Delete Named Profile",
            f"Delete saved profile '{name}'? This does not delete scan history.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._saved_scan_profiles.pop(name, None)
        self._refresh_saved_scans_menu()
        self._persist_settings()
        self.statusBar().showMessage(f"Deleted named profile '{name}'.")

    def _scan_root_summary(self, roots: list[str]) -> str:
        if not roots:
            return "(no roots)"
        first = roots[0]
        if len(roots) == 1:
            return first
        return f"{first} (+{len(roots) - 1})"

    def _format_scan_created_at(self, value: str) -> str:
        text = str(value).strip()
        if not text:
            return ""
        try:
            return text.replace("T", " ")[:19]
        except Exception:
            return text

    def _scan_set_key_for_profile(self, profile: SavedScanProfilePayload) -> str:
        roots = normalize_roots_for_display(list(profile.roots))
        similarity_profile = normalize_similarity_profile(profile.similarity_profile)
        extensions = normalize_extensions(list(profile.extensions))
        raw_key = str(profile.scan_set_key).strip()
        if raw_key:
            return raw_key
        return build_scan_set_key(roots=roots, similarity_profile=similarity_profile, extensions=extensions)

    def _format_scan_status(self, status: str | None) -> str:
        cleaned = str(status or "").strip().lower()
        if cleaned in {"done", "cancelled", "running"}:
            return cleaned
        if not cleaned:
            return "not started"
        return cleaned

    def _show_saved_scans_menu(self) -> None:
        if self._saved_scans_menu is None:
            return
        self._refresh_saved_scans_menu()
        self._saved_scans_menu.exec(self.load_saved_scan_btn.mapToGlobal(self.load_saved_scan_btn.rect().bottomLeft()))

    def _refresh_saved_scans_menu(self) -> None:
        if self._saved_scans_menu is None:
            return
        self._saved_scans_menu.clear()
        named_items = sorted(self._saved_scan_profiles.items(), key=lambda pair: pair[0].casefold())
        represented_keys = {self._scan_set_key_for_profile(payload) for _, payload in named_items}
        represented_keys.discard("")
        has_entries = False

        if named_items:
            has_entries = True
            named_header = QAction("Named Profiles", self)
            named_header.setEnabled(False)
            self._saved_scans_menu.addAction(named_header)
            for name, payload in named_items:
                scan_set_key = self._scan_set_key_for_profile(payload)
                latest_scan_id = self.db.latest_scan_id_for_set(scan_set_key)
                if latest_scan_id is None:
                    status_text = self._format_scan_status(None)
                    action = QAction(f"{name} | {status_text}", self)
                    action.setToolTip("Profile saved; scan has not started yet.")
                    self._saved_scans_menu.addAction(action)
                else:
                    summary = self.db.scan_summary(latest_scan_id)
                    stamp = self._format_scan_created_at(summary["created_at"])
                    status_text = self._format_scan_status(summary.get("status"))
                    action = QAction(f"{name} | #{latest_scan_id} | {stamp} | {status_text}", self)
                action.triggered.connect(
                    lambda _checked=False, profile=payload, source_name=name: self._load_saved_scan_profile(
                        profile=profile,
                        source_name=source_name,
                    )
                )
                self._saved_scans_menu.addAction(action)
            self._saved_scans_menu.addSeparator()

        auto_scans = [item for item in self.db.list_latest_scans_by_set() if item["scan_set_key"] not in represented_keys]
        if auto_scans:
            auto_header = QAction("Auto Profiles", self)
            auto_header.setEnabled(False)
            self._saved_scans_menu.addAction(auto_header)
            for scan in auto_scans:
                roots = normalize_roots_for_display(list(scan.get("roots", [])))
                profile = normalize_similarity_profile(str(scan.get("profile", "balanced")))
                extensions = normalize_extensions(list(scan.get("extensions", [])))
                payload = SavedScanProfilePayload(
                    scan_set_key=str(scan.get("scan_set_key", "")),
                    roots=roots,
                    similarity_profile=profile,
                    extensions=extensions,
                    updated_at=str(scan.get("created_at", "")),
                )
                scan_id = int(scan.get("scan_id", 0))
                status_text = self._format_scan_status(str(scan.get("status", "")))
                label = f"Auto: {self._scan_root_summary(roots)} | {profile} | #{scan_id} | {status_text}"
                action = QAction(label, self)
                action.triggered.connect(
                    lambda _checked=False, profile_payload=payload: self._load_saved_scan_profile(
                        profile=profile_payload,
                        source_name="auto profile",
                    )
                )
                self._saved_scans_menu.addAction(action)
                has_entries = True
            self._saved_scans_menu.addSeparator()

        if not has_entries:
            empty = QAction("(No saved scans)", self)
            empty.setEnabled(False)
            self._saved_scans_menu.addAction(empty)
            self._saved_scans_menu.addSeparator()

        save_action = QAction("Save Current Scan Set As...", self)
        save_action.triggered.connect(self._save_current_scan_set_as)
        self._saved_scans_menu.addAction(save_action)

        delete_action = QAction("Delete Named Profile...", self)
        delete_action.setEnabled(bool(named_items))
        delete_action.triggered.connect(self._delete_named_scan_profile)
        self._saved_scans_menu.addAction(delete_action)

    def _load_saved_scan_profile(self, profile: SavedScanProfilePayload, source_name: str) -> None:
        roots = normalize_roots_for_display(list(profile.roots))
        normalized_profile = normalize_similarity_profile(profile.similarity_profile)
        extensions = normalize_extensions(list(profile.extensions))
        scan_set_key = self._scan_set_key_for_profile(profile)

        self.roots_list.clear()
        for root in roots:
            self.roots_list.addItem(root)
            self._remember_recent_root(root)
        self.roots_list.setCurrentRow(-1)
        self._update_root_buttons_state()
        profile_index = self.profile_combo.findText(normalized_profile)
        self.profile_combo.setCurrentIndex(max(0, profile_index))
        self.extensions_edit.setText(", ".join(extensions))
        preset_name = detect_video_extension_preset(extensions) or DEFAULT_VIDEO_EXTENSION_PRESET
        preset_index = self.extensions_preset_combo.findText(preset_name)
        self.extensions_preset_combo.blockSignals(True)
        self.extensions_preset_combo.setCurrentIndex(max(0, preset_index))
        self.extensions_preset_combo.blockSignals(False)
        self._refresh_sources_physical_drive_view()

        latest_scan_id = self.db.latest_scan_id_for_set(scan_set_key)
        if latest_scan_id is None:
            self.current_scan_id = None
            self.results_view.load_groups([])
            self.results_view.set_scan_context_note("")
            self.tabs.setCurrentWidget(self.sources_tab)
            self.statusBar().showMessage(
                f"Loaded saved scan profile '{source_name}' ({self._format_scan_status(None)}). Start scan to continue."
            )
            return

        summary = self.db.scan_summary(latest_scan_id)
        status_text = self._format_scan_status(summary.get("status"))
        if status_text != "done":
            self.current_scan_id = None
            self.results_view.load_groups([])
            self.results_view.set_scan_context_note("")
            stamp = self._format_scan_created_at(summary["created_at"])
            when = f" from {stamp}" if stamp else ""
            note = (
                f"Loaded saved scan profile '{source_name}'. Latest scan #{latest_scan_id}{when} is {status_text}; "
                "start scan to continue."
            )
            self.tabs.setCurrentWidget(self.sources_tab)
            self.statusBar().showMessage(note)
            return

        groups = self.db.load_duplicate_groups(latest_scan_id)
        self.current_scan_id = latest_scan_id
        self.results_view.load_groups(groups)
        stamp = self._format_scan_created_at(summary["created_at"])
        note = f"Loaded saved scan #{latest_scan_id} from {stamp}; filesystem may have changed."
        self.results_view.set_scan_context_note(note)
        self.tabs.setCurrentWidget(self.results_view)
        self.statusBar().showMessage(note)

    def _rescan_scan(self) -> None:
        if self._scan_tab_locked:
            return
        self._persist_settings()
        roots = normalize_roots_for_display(list(self.settings.scan_roots))
        if not roots:
            QMessageBox.warning(self, "Missing Sources", "Add at least one scan root in the Sources tab.")
            self.tabs.setCurrentWidget(self.sources_tab)
            return
        profile = normalize_similarity_profile(self.settings.similarity_profile)
        extensions = normalize_extensions(list(self.settings.extensions))
        scan_set_key = build_scan_set_key(roots=roots, similarity_profile=profile, extensions=extensions)
        confirm = QMessageBox.question(
            self,
            "Rescan (Fresh)",
            (
                "This will permanently delete scan history/artifacts for this scan set and any cached file artifacts "
                "under the selected folders.\n\n"
                "All cached thumbnails will also be cleared.\n\n"
                "Continue with fresh rescan?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            purge_counts = self.db.purge_for_fresh_rescan(scan_set_key=scan_set_key, roots=roots)
            thumbs_removed, thumbs_failed = self._clear_cached_thumbnails_internal()
        except Exception as exc:
            QMessageBox.critical(self, "Rescan Failed", str(exc))
            return

        self.current_scan_id = None
        self.results_view.load_groups([])
        self.results_view.set_scan_context_note("")
        self._refresh_saved_scans_menu()
        cleanup_summary = (
            "Fresh rescan cleanup complete: "
            f"scans={int(purge_counts.get('deleted_scans', 0))}, "
            f"files={int(purge_counts.get('deleted_files', 0))}, "
            f"groups={int(purge_counts.get('deleted_groups', 0))}, "
            f"actions={int(purge_counts.get('deleted_actions', 0))}, "
            f"thumbnails={thumbs_removed}, thumb_failures={thumbs_failed}."
        )
        self.statusBar().showMessage(cleanup_summary)
        self._start_scan()
        self.statusBar().showMessage(f"{cleanup_summary} Scan started.")

    def _start_scan(self) -> None:
        self._persist_settings()
        if not self.settings.scan_roots:
            QMessageBox.warning(self, "Missing Sources", "Add at least one scan root in the Sources tab.")
            self.tabs.setCurrentWidget(self.sources_tab)
            return

        lane_plan = build_physical_drive_scan_plan(
            roots=list(self.settings.scan_roots),
            max_workers=int(self.settings.max_workers),
            drive_worker_overrides=self.settings.drive_worker_overrides,
        )
        self.scan_view.reset()
        self.scan_view.initialize_lane_plan(
            lane_plan.root_groups,
            lane_plan.effective_total_workers
            if lane_plan.effective_total_workers > 0
            else int(self.settings.max_workers),
        )
        self._set_scan_tab_lock(True)
        self.scan_view.set_running(True)
        self.statusBar().showMessage("Scan started")
        self.tabs.setCurrentWidget(self.scan_view)

        self.scan_worker = ScanWorker(
            db_file=self.db_file,
            roots=self.settings.scan_roots,
            extensions=self.settings.normalized_extensions(),
            profile=self.settings.similarity_profile,
            max_workers=self.settings.max_workers,
            drive_worker_overrides=self.settings.drive_worker_overrides,
            probe_worker_mode=self.settings.probe_worker_mode,
            db_batch_size=self.settings.scan_db_batch_size,
            db_flush_interval_ms=self.settings.scan_db_flush_interval_ms,
            enum_queue_max=self.settings.scan_enum_queue_max,
            progress_emit_interval_ms=self.settings.scan_progress_emit_interval_ms,
            progress_emit_every_files=self.settings.scan_progress_emit_every_files,
        )
        self.scan_worker.signals.progress.connect(self.scan_view.update_progress)
        self.scan_worker.signals.finished.connect(self._scan_finished)
        self.scan_worker.signals.error.connect(self._scan_error)
        self.thread_pool.start(self.scan_worker)

    def _cancel_scan(self) -> None:
        if self.scan_worker:
            self.scan_worker.cancel()
            self.statusBar().showMessage("Cancelling scan...")

    def _scan_finished(self, result) -> None:
        self.scan_worker = None
        self._set_scan_tab_lock(False)
        self.scan_view.set_running(False)
        self.scan_view.set_issues(result.issues)
        finished_scan_id = int(result.scan_id)
        self.current_scan_id = finished_scan_id

        self.db.close()
        self.db = Database(self.db_file)
        summary = self.db.scan_summary(finished_scan_id)
        status_text = self._format_scan_status(summary.get("status"))
        self._refresh_saved_scans_menu()
        if status_text != "done":
            self.current_scan_id = None
            self.tabs.setCurrentWidget(self.scan_view)
            self.statusBar().showMessage(f"Scan {finished_scan_id} {status_text}: {len(result.issues)} issues.")
            return

        groups = self.db.load_duplicate_groups(finished_scan_id)
        self.results_view.load_groups(groups)
        self.results_view.set_scan_context_note("")
        self.tabs.setCurrentWidget(self.results_view)
        metrics = getattr(result, "metrics", {}) or {}
        flush_count = int(metrics.get("flush_count", 0))
        max_queue_depth = int(metrics.get("max_queue_depth", 0))
        timing_summary = ""
        stage_seconds = metrics.get("stage_seconds", {})
        if isinstance(stage_seconds, dict):
            matching_s = float(stage_seconds.get("matching", 0.0))
            timing_summary = f", matching {matching_s:.2f}s"
        self.statusBar().showMessage(
            f"Scan {finished_scan_id} complete: {len(groups)} groups, {len(result.issues)} issues"
            f" (flushes {flush_count}, queue {max_queue_depth}{timing_summary})."
        )

    def _scan_error(self, details: str) -> None:
        self.scan_worker = None
        self._set_scan_tab_lock(False)
        self.scan_view.set_running(False)
        self.statusBar().showMessage("Scan failed")
        QMessageBox.critical(self, "Scan Error", details)

    def _next_zdele_path(self, source: Path) -> Path:
        candidate = source.with_name(f"{source.name}.z_dele")
        if not candidate.exists():
            return candidate
        index = 1
        while True:
            alt = source.with_name(f"{source.name}.z_dele.{index}")
            if not alt.exists():
                return alt
            index += 1

    def _handle_delete_requested(self, mode: str, targets: list[dict]) -> None:
        if self.current_scan_id is None:
            QMessageBox.warning(self, "No Scan", "Run a scan first.")
            return
        if not targets:
            self.statusBar().showMessage("No rows selected.")
            return

        failures: list[str] = []
        success_count = 0
        for target in targets:
            file_id = int(target.get("file_id", 0))
            if file_id <= 0:
                continue
            source = Path(self.db.fetch_file_path(file_id))
            try:
                if mode == "rename":
                    if not source.exists():
                        raise FileNotFoundError(f"{source} does not exist")
                    destination = self._next_zdele_path(source)
                    source.rename(destination)
                    self.db.refresh_file_after_rename(file_id=file_id, new_path=str(destination))
                elif mode == "permanent":
                    if source.exists():
                        source.unlink()
                    self.db.mark_file_missing(file_id=file_id)
                else:
                    continue
                self.db.remove_file_from_duplicate_groups(file_id=file_id)
                self.results_view.remove_file_by_id(file_id)
                success_count += 1
            except Exception as exc:
                failures.append(f"{source}: {exc}")

        self.db.prune_duplicate_groups(self.current_scan_id)
        groups = self.db.load_duplicate_groups(self.current_scan_id)
        self.results_view.load_groups(groups)

        if failures:
            self.statusBar().showMessage(f"Completed with {len(failures)} errors.")
            QMessageBox.warning(self, "Delete Completed with Errors", "\n".join(failures[:20]))
        else:
            self.statusBar().showMessage(f"Processed {success_count} file(s).")

    def _export_current_scan(self) -> None:
        if self.current_scan_id is None:
            QMessageBox.warning(self, "No Scan", "Run a scan first.")
            return
        out_dir = QFileDialog.getExistingDirectory(self, "Choose export directory")
        if not out_dir:
            return
        csv_path, json_path = export_scan(self.db, scan_id=self.current_scan_id, out_dir=out_dir)
        QMessageBox.information(self, "Export Complete", f"CSV: {csv_path}\nJSON: {json_path}")

    def closeEvent(self, event) -> None:
        if not self._full_reset_requested:
            self._persist_settings()
        with contextlib.suppress(Exception):
            self.db.close()
        super().closeEvent(event)
