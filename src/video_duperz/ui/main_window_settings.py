"""Settings, saved views, and root maintenance helpers for the main window."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMessageBox,
)
from threep_commons.desktop import open_path_in_default_app

from ..config import (
    MAX_RECENT_ROOTS,
    normalize_thumbnail_size,
    save_settings,
    settings_path,
)
from ..config_video_presets import (
    DEFAULT_VIDEO_EXTENSION_PRESET,
    detect_video_extension_preset,
    video_extensions_csv_for_preset,
)
from ..executable_paths import normalize_executable_override_path
from ..models import (
    ProbeBackendId,
    ProbeWorkerMode,
    SavedScanProfilePayload,
    Settings,
    utc_now_iso,
)
from ..process_priority import normalize_scan_cpu_priority, normalize_scan_io_mode
from ..scan_sets import (
    build_scan_set_key,
    normalize_cross_resolution_mode,
    normalize_custom_similarity_threshold,
    normalize_extensions,
    normalize_roots_for_display,
    normalize_similarity_profile,
)
from .main_window_core import MainWindowSourceSetupMixin
from .thumbnails import thumbnail_cache_dir

if TYPE_CHECKING:
    from collections.abc import Callable


class MainWindowSettingsMixin(MainWindowSourceSetupMixin):
    """Settings synchronization and tab-state helper methods."""

    def _refresh_recent_roots_menu(self) -> None: ...

    def _refresh_saved_views_menu(self) -> None: ...

    def _refresh_saved_scans_menu(self) -> None: ...

    def _update_root_buttons_state(self) -> None: ...

    def _sync_column_toggle_actions(self) -> None: ...

    def _refresh_sources_physical_drive_view(self) -> None: ...

    def _normalized_drive_worker_overrides(self) -> dict[str, int]: ...

    def _normalized_saved_column_views(
        self,
    ) -> dict[str, dict[str, list[int] | list[bool]]]: ...

    def _normalized_saved_scan_profiles(self) -> dict[str, SavedScanProfilePayload]: ...

    def _current_probe_backend(self) -> ProbeBackendId: ...

    def _current_probe_worker_mode(self) -> ProbeWorkerMode: ...

    def _current_sources_extensions(self) -> list[str]: ...

    def _clear_loaded_paused_scan(self) -> None: ...

    def _browse_executable_path(
        self,
        target_edit: QLineEdit,
        tool_name: str,
    ) -> None:
        """Prompt for one executable path and copy it into the target edit."""
        selected_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            f"Select {tool_name} executable",
            str(Path(target_edit.text()).expanduser()) if target_edit.text() else "",
            "Executable (*.exe);;All files (*)",
        )
        if selected_path:
            target_edit.setText(selected_path)

    def _load_settings_to_widgets(self) -> None:
        """Load persisted settings into all source and results controls."""
        self.roots_list.clear()
        for root in self.settings.scan_roots:
            self.roots_list.addItem(root)
        self.roots_list.setCurrentRow(-1)
        self._recent_roots = list(self.settings.recent_scan_roots)
        self._refresh_recent_roots_menu()
        normalized_extensions = normalize_extensions(list(self.settings.extensions))
        self.extensions_edit.setText(", ".join(normalized_extensions))
        self.scan_size_mib_min_spin.setValue(
            max(0, int(self.settings.scan_size_mib_min))
        )
        self.scan_size_mib_max_spin.setValue(
            max(0, int(self.settings.scan_size_mib_max))
        )
        preset_name = (
            detect_video_extension_preset(normalized_extensions)
            or DEFAULT_VIDEO_EXTENSION_PRESET
        )
        preset_index = self.extensions_preset_combo.findText(preset_name)
        self.extensions_preset_combo.blockSignals(True)
        self.extensions_preset_combo.setCurrentIndex(max(0, preset_index))
        self.extensions_preset_combo.blockSignals(False)
        idx = self.profile_combo.findText(self.settings.similarity_profile)
        self.profile_combo.setCurrentIndex(max(0, idx))
        self.custom_similarity_threshold_spin.setValue(
            normalize_custom_similarity_threshold(
                self.settings.custom_similarity_threshold
            )
        )
        self.duration_tolerance_spin.setValue(
            max(0.0, float(self.settings.duration_tolerance_s))
        )
        self.scene_aware_sampling_check.setChecked(
            bool(self.settings.scene_aware_sampling)
        )
        self.audio_fingerprint_enabled_check.setChecked(
            bool(self.settings.audio_fingerprint_enabled)
        )
        self._set_combo_by_data(
            self.cross_resolution_mode_combo,
            self.settings.cross_resolution_mode,
        )
        probe_backend_index = self.probe_backend_combo.findText(
            self.settings.probe_backend
        )
        self.probe_backend_combo.setCurrentIndex(max(0, probe_backend_index))
        self.max_workers_spin.setValue(max(1, int(self.settings.max_workers)))
        self.fingerprint_timeout_spin.setValue(
            max(0.1, float(self.settings.fingerprint_timeout_s))
        )
        probe_index = self.probe_mode_combo.findText(self.settings.probe_worker_mode)
        self.probe_mode_combo.setCurrentIndex(max(0, probe_index))
        self.ffmpeg_exe_path_edit.setText(self.settings.ffmpeg_exe_path)
        self.ffprobe_exe_path_edit.setText(self.settings.ffprobe_exe_path)
        self.fpcalc_exe_path_edit.setText(self.settings.fpcalc_exe_path)
        self.mediainfo_exe_path_edit.setText(self.settings.mediainfo_exe_path)
        self._set_combo_by_data(
            self.scan_parent_cpu_priority_combo,
            self.settings.scan_parent_cpu_priority,
        )
        self._set_combo_by_data(
            self.scan_parent_io_mode_combo,
            self.settings.scan_parent_io_mode,
        )
        self._set_combo_by_data(
            self.scan_child_cpu_priority_combo,
            self.settings.scan_child_cpu_priority,
        )
        self._set_combo_by_data(
            self.scan_child_io_mode_combo,
            self.settings.scan_child_io_mode,
        )
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
        self._sources_drive_table_column_widths = list(
            self.settings.sources_drive_table_column_widths
        )
        if self._sources_drive_table_column_widths:
            self._set_sources_drive_table_column_widths(
                self._sources_drive_table_column_widths
            )
        self.scan_view.set_lane_column_widths(
            self.settings.scan_lane_table_column_widths
        )
        self.scan_view.set_log_column_widths(self.settings.scan_log_table_column_widths)
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
        self.results_view.set_mediainfo_exe_path(self.settings.mediainfo_exe_path)
        self.results_view.set_everything_exe_path(self.settings.everything_exe_path)
        self.results_view.set_custom_command_overrides(
            command_f2=self.settings.custom_command_f2,
            command_f3=self.settings.custom_command_f3,
            command_f4=self.settings.custom_command_f4,
        )
        self._update_custom_similarity_controls_visibility()
        self._update_root_buttons_state()
        self._sync_column_toggle_actions()
        self._refresh_sources_physical_drive_view()

    def _settings_from_widgets(self) -> Settings:
        """Build the persisted settings payload from the current widget state."""
        roots = [self.roots_list.item(i).text() for i in range(self.roots_list.count())]
        exts = [
            ext.strip().lower().lstrip(".")
            for ext in self.extensions_edit.text().split(",")
        ]
        exts = [ext for ext in exts if ext]
        drive_worker_overrides = self._normalized_drive_worker_overrides()
        return Settings(
            scan_roots=roots,
            recent_scan_roots=list(self._recent_roots),
            extensions=exts,
            scan_size_mib_min=max(0, int(self.scan_size_mib_min_spin.value())),
            scan_size_mib_max=max(0, int(self.scan_size_mib_max_spin.value())),
            similarity_profile=normalize_similarity_profile(
                self.profile_combo.currentText()
            ),
            custom_similarity_threshold=normalize_custom_similarity_threshold(
                self.custom_similarity_threshold_spin.value()
            ),
            duration_tolerance_s=max(
                0.0,
                float(self.duration_tolerance_spin.value()),
            ),
            scene_aware_sampling=bool(self.scene_aware_sampling_check.isChecked()),
            audio_fingerprint_enabled=bool(
                self.audio_fingerprint_enabled_check.isChecked()
            ),
            cross_resolution_mode=normalize_cross_resolution_mode(
                self.cross_resolution_mode_combo.currentData()
            ),
            fingerprint_timeout_s=max(
                0.1,
                float(self.fingerprint_timeout_spin.value()),
            ),
            max_workers=self.max_workers_spin.value(),
            preview_autoplay=self.settings.preview_autoplay,
            thumbnail_size=normalize_thumbnail_size(
                str(self.thumbnail_size_combo.currentData())
            ),
            thumbnail_frame_a_pct=self.settings.thumbnail_frame_a_pct,
            thumbnail_frame_b_pct=self.settings.thumbnail_frame_b_pct,
            identical_block_mib=self.settings.identical_block_mib,
            identical_sample_a_pct=self.settings.identical_sample_a_pct,
            identical_sample_b_pct=self.settings.identical_sample_b_pct,
            sources_drive_table_column_widths=(
                self._capture_sources_drive_table_column_widths()
            ),
            scan_lane_table_column_widths=self.scan_view.lane_column_widths(),
            scan_log_table_column_widths=self.scan_view.log_column_widths(),
            results_table_column_widths=self.results_view.column_widths(),
            results_table_column_visibility=self.results_view.column_visibility(),
            saved_column_views=self._normalized_saved_column_views(),
            saved_scan_profiles=self._normalized_saved_scan_profiles(),
            keep_rule="best_quality",
            drive_worker_overrides=drive_worker_overrides,
            probe_backend=self._current_probe_backend(),
            probe_worker_mode=self._current_probe_worker_mode(),
            ffmpeg_exe_path=normalize_executable_override_path(
                self.ffmpeg_exe_path_edit.text()
            ),
            ffprobe_exe_path=normalize_executable_override_path(
                self.ffprobe_exe_path_edit.text()
            ),
            fpcalc_exe_path=normalize_executable_override_path(
                self.fpcalc_exe_path_edit.text()
            ),
            mediainfo_exe_path=normalize_executable_override_path(
                self.mediainfo_exe_path_edit.text()
            ),
            everything_exe_path=normalize_executable_override_path(
                self.settings.everything_exe_path
            ),
            custom_command_f2=str(self.settings.custom_command_f2 or "").strip(),
            custom_command_f3=str(self.settings.custom_command_f3 or "").strip(),
            custom_command_f4=str(self.settings.custom_command_f4 or "").strip(),
            scan_parent_cpu_priority=normalize_scan_cpu_priority(
                self.scan_parent_cpu_priority_combo.currentData()
            ),
            scan_parent_io_mode=normalize_scan_io_mode(
                self.scan_parent_io_mode_combo.currentData()
            ),
            scan_child_cpu_priority=normalize_scan_cpu_priority(
                self.scan_child_cpu_priority_combo.currentData()
            ),
            scan_child_io_mode=normalize_scan_io_mode(
                self.scan_child_io_mode_combo.currentData()
            ),
            scan_db_batch_size=self.settings.scan_db_batch_size,
            scan_db_flush_interval_ms=self.settings.scan_db_flush_interval_ms,
            scan_enum_queue_max=self.settings.scan_enum_queue_max,
            scan_progress_emit_interval_ms=self.settings.scan_progress_emit_interval_ms,
            scan_progress_emit_every_files=self.settings.scan_progress_emit_every_files,
        )

    def _persist_settings(self) -> None:
        """Persist current widget state and refresh dependent results settings."""
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
        self.results_view.set_mediainfo_exe_path(self.settings.mediainfo_exe_path)
        self.results_view.set_everything_exe_path(self.settings.everything_exe_path)
        self.results_view.set_custom_command_overrides(
            command_f2=self.settings.custom_command_f2,
            command_f3=self.settings.custom_command_f3,
            command_f4=self.settings.custom_command_f4,
        )

    @staticmethod
    def _set_combo_by_data(combo: QComboBox, value: object) -> None:
        """Set one combo box to the first item whose data matches the value."""
        for index in range(combo.count()):
            if str(combo.itemData(index)) == str(value):
                combo.setCurrentIndex(index)
                return

    def _on_tab_changed(self, index: int) -> None:
        """Keep the Scan tab selected while a scan is running."""
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
        """Toggle the Sources and Results tabs while a scan is active."""
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
        """Apply the currently selected thumbnail size to the results view."""
        size_key = normalize_thumbnail_size(
            str(self.thumbnail_size_combo.currentData())
        )
        self.results_view.set_thumbnail_size(size_key)

    def _extensions_preset_changed(self, preset_name: str) -> None:
        """Apply the selected extension preset to the custom extensions edit."""
        self.extensions_edit.setText(video_extensions_csv_for_preset(preset_name))

    def _extensions_text_edited(self, _text: str) -> None:
        """Sync the preset combo when the freeform extensions match a known preset."""
        matched_preset = detect_video_extension_preset(
            self._current_sources_extensions()
        )
        if not matched_preset:
            return
        target_index = self.extensions_preset_combo.findText(matched_preset)
        if (
            target_index < 0
            or target_index == self.extensions_preset_combo.currentIndex()
        ):
            return
        self.extensions_preset_combo.blockSignals(True)
        self.extensions_preset_combo.setCurrentIndex(target_index)
        self.extensions_preset_combo.blockSignals(False)


class MainWindowSavedViewsMixin(MainWindowSettingsMixin):
    """Column-view and saved-scan menu helper methods."""

    def _normalized_saved_column_views(
        self,
    ) -> dict[str, dict[str, list[int] | list[bool]]]:
        """Validate the persisted saved-column-view payloads."""
        normalized: dict[str, dict[str, list[int] | list[bool]]] = {}
        for name, payload in self._saved_column_views.items():
            cleaned_name = str(name).strip()
            if not cleaned_name:
                continue
            widths_raw = payload.get("widths", [])
            visibility_raw = payload.get("visibility", [])
            widths = [int(width) for width in widths_raw]
            visibility = [bool(value) for value in visibility_raw]
            if len(widths) != len(self.results_view.column_labels()):
                continue
            if len(visibility) != len(self.results_view.column_labels()):
                continue
            if not any(visibility):
                continue
            normalized[cleaned_name] = {"widths": widths, "visibility": visibility}
        return normalized

    def _normalized_saved_scan_profiles(self) -> dict[str, SavedScanProfilePayload]:
        """Validate the persisted saved scan profiles."""
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
            custom_similarity_threshold = normalize_custom_similarity_threshold(
                payload.custom_similarity_threshold
            )
            scene_aware_sampling = bool(payload.scene_aware_sampling)
            audio_fingerprint_enabled = bool(payload.audio_fingerprint_enabled)
            cross_resolution_mode = normalize_cross_resolution_mode(
                payload.cross_resolution_mode
            )
            scan_set_key = str(payload.scan_set_key).strip() or build_scan_set_key(
                roots=roots,
                similarity_profile=profile,
                extensions=extensions,
                custom_similarity_threshold=custom_similarity_threshold,
                scene_aware_sampling=scene_aware_sampling,
                audio_fingerprint_enabled=audio_fingerprint_enabled,
                cross_resolution_mode=cross_resolution_mode,
            )
            updated_at = str(payload.updated_at).strip() or utc_now_iso()
            normalized[cleaned_name] = SavedScanProfilePayload(
                scan_set_key=scan_set_key,
                roots=roots,
                similarity_profile=profile,
                extensions=extensions,
                custom_similarity_threshold=custom_similarity_threshold,
                scene_aware_sampling=scene_aware_sampling,
                audio_fingerprint_enabled=audio_fingerprint_enabled,
                cross_resolution_mode=cross_resolution_mode,
                updated_at=updated_at,
            )
        return normalized

    def _fit_columns(self) -> None:
        current_tab = self.tabs.currentWidget()
        if current_tab is self.sources_tab:
            self._fit_sources_drive_table_columns()
            self.statusBar().showMessage("Sources columns fitted to contents.")
            return
        if current_tab is self.scan_view:
            self.scan_view.fit_all_columns_to_contents()
            self.statusBar().showMessage("Scan columns fitted to contents.")
            return
        self.results_view.fit_columns_to_contents()
        self._sync_column_toggle_actions()
        self.statusBar().showMessage("Results columns fitted to contents.")

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
            action.triggered.connect(
                lambda _checked=False, view_name=name: self._apply_saved_view(view_name)
            )
            self._saved_views_menu.addAction(action)

    def _apply_saved_view(self, name: str) -> None:
        payload = self._saved_column_views.get(name)
        if payload is None:
            return
        widths_raw = payload.get("widths", [])
        visibility_raw = payload.get("visibility", [])
        widths = [int(width) for width in widths_raw]
        visibility = [bool(value) for value in visibility_raw]
        self.results_view.set_column_visibility(visibility)
        self.results_view.set_column_widths(widths)
        self._sync_column_toggle_actions()
        self.statusBar().showMessage(f"Applied view '{name}'.")

    def _column_toggle_slot(self, column_index: int) -> Callable[[bool], None]:
        def _toggle(checked: bool) -> None:
            self._set_column_visibility_from_menu(column_index, checked)

        return _toggle

    def _set_column_visibility_from_menu(
        self,
        column_index: int,
        checked: bool,
    ) -> None:
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


class MainWindowRootsMixin(MainWindowSavedViewsMixin):
    """Root-list, recent-root, and basic maintenance helper methods."""

    def _refresh_sources_physical_drive_view(self) -> None: ...

    def _refresh_saved_scans_menu(self) -> None: ...

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
        for index in range(self.roots_list.count()):
            if self.roots_list.item(index).text().casefold() == needle:
                return index
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
        deduped = [
            value
            for value in self._recent_roots
            if value.casefold() != normalized.casefold()
        ]
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
            action.triggered.connect(
                lambda _checked=False, value=folder: self._add_recent_root_selected(
                    value
                )
            )
            self._recent_roots_menu.addAction(action)
        self._recent_roots_menu.addSeparator()
        clear_action = QAction("Clear Recent Folders", self)
        clear_action.triggered.connect(self._clear_recent_roots)
        self._recent_roots_menu.addAction(clear_action)

    def _show_recent_roots_menu(self) -> None:
        if self._recent_roots_menu is None:
            return
        self._recent_roots_menu.exec(
            self.add_recent_root_btn.mapToGlobal(
                self.add_recent_root_btn.rect().bottomLeft()
            )
        )

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
        self._clear_loaded_paused_scan()
        self.current_scan_id = None
        self.results_view.load_groups([])
        self.results_view.set_scan_context_note("")
        self._refresh_saved_scans_menu()
        self._persist_settings()
        self.statusBar().showMessage("Saved scans and scan history cleared.")

    def _clear_cached_thumbnails_internal(self) -> tuple[int, int]:
        removed = 0
        failed = 0
        cache_dir = thumbnail_cache_dir()
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
            self.statusBar().showMessage(
                f"Cleared cached thumbnails ({removed} item(s), {failed} failed)."
            )
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
