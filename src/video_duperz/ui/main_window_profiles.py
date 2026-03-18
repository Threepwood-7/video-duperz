"""Drive planning and saved-profile helpers for the main window."""

from __future__ import annotations

from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import QInputDialog, QMessageBox, QSpinBox, QTableWidgetItem

from .. import __version__
from ..config import settings_path
from ..config_video_presets import (
    DEFAULT_VIDEO_EXTENSION_PRESET,
    detect_video_extension_preset,
)
from ..models import (
    ProbeBackendId,
    ProbeWorkerMode,
    SavedScanProfilePayload,
    SimilarityProfile,
    utc_now_iso,
)
from ..scan_sets import (
    build_scan_set_key,
    normalize_extensions,
    normalize_roots_for_display,
    normalize_similarity_profile,
)
from ..scanner import build_physical_drive_scan_plan, list_physical_drives
from .main_window_core import (
    MAX_DRIVE_WORKERS,
    metric_int,
    payload_dict,
    payload_strings,
)
from .main_window_settings import MainWindowRootsMixin


class MainWindowDriveViewMixin(MainWindowRootsMixin):
    """Physical-drive planning, diagnostics, and reset helper methods."""

    def _current_sources_roots(self) -> list[str]: ...

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

    def _on_drive_worker_override_changed(
        self,
        volume_identity: str,
        workers: int,
    ) -> None:
        identity = str(volume_identity).strip()
        if not identity:
            return
        self._drive_worker_overrides[identity] = max(
            1,
            min(MAX_DRIVE_WORKERS, int(workers)),
        )
        if self._drive_workers_editing:
            return
        self._refresh_sources_physical_drive_view()

    def _drive_worker_slot(self, volume_identity: str):
        def _update(workers: int) -> None:
            self._on_drive_worker_override_changed(volume_identity, workers)

        return _update

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
            self.sources_drive_table.setItem(
                0,
                0,
                QTableWidgetItem("(No local drives detected)"),
            )
            for col in range(1, self.sources_drive_table.columnCount()):
                self.sources_drive_table.setItem(0, col, QTableWidgetItem(""))
        else:
            self.sources_drive_table.setRowCount(len(drives))
            self._drive_workers_editing = True
            try:
                for row, drive in enumerate(drives):
                    tokens = (
                        ", ".join(drive.disk_tokens) if drive.disk_tokens else "(none)"
                    )
                    matched = drive.volume_identity in matched_identities
                    row_values = [
                        drive.root,
                        tokens,
                        drive.volume_identity,
                        self._format_byte_count(drive.total_bytes),
                        self._format_byte_count(drive.free_bytes),
                        f"{drive.used_percent:.1f}%"
                        if drive.used_percent is not None
                        else "n/a",
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
                            self._drive_worker_slot(drive.volume_identity)
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
            f"Video Duperz {__version__}\n"
            "Windows-first duplicate video finder.\n"
            f"Settings: {settings_path()}",
        )

    def _request_full_reset(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Full Reset",
            "This will close Video Duperz, erase all app data "
            "(saved scans, settings, thumbnails), and relaunch.\n\n"
            "Continue?",
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


class MainWindowProfilesMixin(MainWindowDriveViewMixin):
    """Saved-profile and scan-launch preparation helper methods."""

    def _start_scan(self) -> None: ...

    def _resume_scan(self) -> None: ...

    def _current_sources_roots(self) -> list[str]:
        return [self.roots_list.item(i).text() for i in range(self.roots_list.count())]

    def _current_sources_extensions(self) -> list[str]:
        raw = [
            ext.strip().lower().lstrip(".")
            for ext in self.extensions_edit.text().split(",")
        ]
        return normalize_extensions([ext for ext in raw if ext])

    def _current_sources_profile(self) -> SimilarityProfile:
        return normalize_similarity_profile(self.profile_combo.currentText())

    def _current_probe_worker_mode(self) -> ProbeWorkerMode:
        cleaned = self.probe_mode_combo.currentText().strip().lower()
        if cleaned == "burst":
            return "burst"
        return "balanced"

    def _current_probe_backend(self) -> ProbeBackendId:
        cleaned = self.probe_backend_combo.currentText().strip().lower()
        if cleaned == "pyav":
            return "pyav"
        return "ffprobe"

    def _build_profile_payload_from_sources(self) -> SavedScanProfilePayload | None:
        roots = normalize_roots_for_display(self._current_sources_roots())
        if not roots:
            return None
        profile = self._current_sources_profile()
        extensions = self._current_sources_extensions()
        return SavedScanProfilePayload(
            scan_set_key=build_scan_set_key(
                roots=roots,
                similarity_profile=profile,
                extensions=extensions,
            ),
            roots=roots,
            similarity_profile=profile,
            extensions=extensions,
            updated_at=utc_now_iso(),
        )

    def _save_current_scan_set_as(self) -> None:
        payload = self._build_profile_payload_from_sources()
        if payload is None:
            QMessageBox.warning(
                self,
                "Missing Sources",
                "Add at least one scan root before saving a scan set.",
            )
            return
        name, ok = QInputDialog.getText(self, "Save Scan Set", "Profile name:")
        if not ok:
            return
        cleaned = str(name).strip()
        if not cleaned:
            return
        if len(cleaned) > 80:
            QMessageBox.warning(
                self,
                "Name Too Long",
                "Profile name must be 80 characters or fewer.",
            )
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
        chosen, ok = QInputDialog.getItem(
            self,
            "Delete Named Profile",
            "Profile:",
            names,
            0,
            False,
        )
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
        return build_scan_set_key(
            roots=roots,
            similarity_profile=similarity_profile,
            extensions=extensions,
        )

    def _format_scan_status(self, status: str | None) -> str:
        cleaned = str(status or "").strip().lower()
        if cleaned in {"done", "cancelled", "running", "paused"}:
            return cleaned
        if not cleaned:
            return "not started"
        return cleaned

    def _clear_loaded_paused_scan(self) -> None:
        """Forget the currently loaded paused-scan context."""
        self._loaded_paused_scan_id = None
        self._loaded_paused_roots = []
        self._loaded_paused_profile = ""
        self._loaded_paused_extensions = []
        self._loaded_paused_probe_backend = ""
        self.scan_view.set_paused_loaded(False)

    def _set_loaded_paused_scan(
        self,
        *,
        scan_id: int,
        roots: list[str],
        profile: str,
        extensions: list[str],
        probe_backend: str,
    ) -> None:
        """Record the paused-scan definition that is currently loaded in the UI."""
        self._loaded_paused_scan_id = int(scan_id)
        self._loaded_paused_roots = normalize_roots_for_display(roots)
        self._loaded_paused_profile = normalize_similarity_profile(profile)
        self._loaded_paused_extensions = normalize_extensions(extensions)
        self._loaded_paused_probe_backend = str(probe_backend or "pyav")
        self.scan_view.set_paused_loaded(True)

    def _reload_loaded_paused_scan_widgets(self) -> None:
        """Restore the original paused-scan definition back into the Sources tab."""
        self.roots_list.clear()
        for root in self._loaded_paused_roots:
            self.roots_list.addItem(root)
        self.roots_list.setCurrentRow(-1)
        profile_index = self.profile_combo.findText(self._loaded_paused_profile)
        self.profile_combo.setCurrentIndex(max(0, profile_index))
        self.extensions_edit.setText(", ".join(self._loaded_paused_extensions))
        probe_backend_index = self.probe_backend_combo.findText(
            self._loaded_paused_probe_backend
        )
        self.probe_backend_combo.setCurrentIndex(max(0, probe_backend_index))
        preset_name = (
            detect_video_extension_preset(self._loaded_paused_extensions)
            or DEFAULT_VIDEO_EXTENSION_PRESET
        )
        preset_index = self.extensions_preset_combo.findText(preset_name)
        self.extensions_preset_combo.blockSignals(True)
        self.extensions_preset_combo.setCurrentIndex(max(0, preset_index))
        self.extensions_preset_combo.blockSignals(False)
        self._refresh_sources_physical_drive_view()
        self._update_root_buttons_state()

    def _paused_resume_requires_new_scan(self) -> bool:
        """Return whether the current paused-scan edits are too risky to resume."""
        if self._loaded_paused_scan_id is None:
            return False
        current_roots = normalize_roots_for_display(self._current_sources_roots())
        current_root_keys = {root.casefold() for root in current_roots}
        paused_root_keys = {root.casefold() for root in self._loaded_paused_roots}
        if not paused_root_keys.issubset(current_root_keys):
            return True
        if self._current_sources_profile() != self._loaded_paused_profile:
            return True
        if self._current_sources_extensions() != self._loaded_paused_extensions:
            return True
        return self._current_probe_backend() != self._loaded_paused_probe_backend

    def _prompt_risky_paused_scan_edit(self) -> str:
        """Ask whether risky paused-scan edits should start a fresh scan instead."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Paused Scan Changed")
        box.setText(
            "This paused scan can only be resumed after adding new folders.\n\n"
            "Removing folders or changing profile, extensions, or probe backend "
            "requires a new scan."
        )
        start_new = box.addButton("Start New Scan", QMessageBox.ButtonRole.AcceptRole)
        keep_paused = box.addButton(
            "Keep Paused Scan Unchanged",
            QMessageBox.ButtonRole.RejectRole,
        )
        box.setDefaultButton(start_new)
        box.exec()
        clicked = box.clickedButton()
        if clicked is start_new:
            return "new"
        if clicked is keep_paused:
            return "keep"
        return "keep"

    def _load_paused_scan(
        self,
        *,
        scan_id: int,
        source_name: str,
        scan_info: dict[str, object],
    ) -> None:
        """Load one paused scan into the Scan tab without resuming it."""
        roots = normalize_roots_for_display(payload_strings(scan_info.get("roots", [])))
        profile = normalize_similarity_profile(
            str(scan_info.get("profile", "balanced"))
        )
        extensions = normalize_extensions(
            payload_strings(scan_info.get("extensions", []))
        )
        probe_backend = str(scan_info.get("probe_backend", "pyav"))
        stamp = self._format_scan_created_at(str(scan_info.get("created_at", "")))

        self._set_loaded_paused_scan(
            scan_id=scan_id,
            roots=roots,
            profile=profile,
            extensions=extensions,
            probe_backend=probe_backend,
        )
        self.scan_view.reset()
        lane_plan = build_physical_drive_scan_plan(
            roots=roots,
            max_workers=max(1, int(self.max_workers_spin.value())),
            drive_worker_overrides=self._normalized_drive_worker_overrides(),
        )
        self.scan_view.initialize_lane_plan(
            lane_plan.root_groups,
            lane_plan.effective_total_workers
            if lane_plan.effective_total_workers > 0
            else max(1, int(self.max_workers_spin.value())),
        )
        self.scan_view.set_paused_loaded(True)
        failed_count = self.db.count_failed_files(scan_id)
        self.scan_view.set_retry_failed_file_count(failed_count)
        self.scan_view.append_progress_note(
            "paused",
            (
                f"Paused scan #{scan_id} loaded from {source_name}. "
                "Choose whether to retry prior failed files, then click Resume Scan."
            ),
        )
        self.scan_view.set_issues(self.db.list_scan_issues(scan_id))
        self.results_view.load_groups([])
        self.results_view.set_scan_context_note("")
        self.current_scan_id = None
        self.tabs.setCurrentWidget(self.scan_view)
        when = f" from {stamp}" if stamp else ""
        self.statusBar().showMessage(
            f"Loaded paused scan #{scan_id}{when}. "
            "You can add folders, choose whether to retry failed files, "
            "then click Resume Scan."
        )

    def _show_saved_scans_menu(self) -> None:
        if self._saved_scans_menu is None:
            return
        self._refresh_saved_scans_menu()
        self._saved_scans_menu.exec(
            self.load_saved_scan_btn.mapToGlobal(
                self.load_saved_scan_btn.rect().bottomLeft()
            )
        )

    def _refresh_saved_scans_menu(self) -> None:
        if self._saved_scans_menu is None:
            return
        self._saved_scans_menu.clear()
        named_items = sorted(
            self._saved_scan_profiles.items(),
            key=lambda pair: pair[0].casefold(),
        )
        represented_keys = {
            self._scan_set_key_for_profile(payload) for _, payload in named_items
        }
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
                    action = QAction(
                        f"{name} | #{latest_scan_id} | {stamp} | {status_text}",
                        self,
                    )
                action.triggered.connect(
                    lambda _checked=False, profile=payload, source_name=name: (
                        self._load_saved_scan_profile(
                            profile=profile,
                            source_name=source_name,
                        )
                    )
                )
                self._saved_scans_menu.addAction(action)
            self._saved_scans_menu.addSeparator()

        auto_scans = [
            item
            for item in self.db.list_latest_scans_by_set()
            if item["scan_set_key"] not in represented_keys
        ]
        if auto_scans:
            auto_header = QAction("Auto Profiles", self)
            auto_header.setEnabled(False)
            self._saved_scans_menu.addAction(auto_header)
            for scan in auto_scans:
                scan_payload = payload_dict(scan)
                roots = normalize_roots_for_display(
                    payload_strings(scan_payload.get("roots", []))
                )
                profile = normalize_similarity_profile(
                    str(scan_payload.get("profile", "balanced"))
                )
                extensions = normalize_extensions(
                    payload_strings(scan_payload.get("extensions", []))
                )
                payload = SavedScanProfilePayload(
                    scan_set_key=str(scan_payload.get("scan_set_key", "")),
                    roots=roots,
                    similarity_profile=profile,
                    extensions=extensions,
                    updated_at=str(scan_payload.get("created_at", "")),
                )
                scan_id = metric_int(scan_payload, "scan_id")
                status_text = self._format_scan_status(
                    str(scan_payload.get("status", ""))
                )
                label = (
                    f"Auto: {self._scan_root_summary(roots)} | {profile} | "
                    f"#{scan_id} | {status_text}"
                )
                action = QAction(label, self)
                action.triggered.connect(
                    lambda _checked=False, profile_payload=payload: (
                        self._load_saved_scan_profile(
                            profile=profile_payload,
                            source_name="auto profile",
                        )
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

    def _load_saved_scan_profile(
        self,
        profile: SavedScanProfilePayload,
        source_name: str,
    ) -> None:
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
        preset_name = (
            detect_video_extension_preset(extensions) or DEFAULT_VIDEO_EXTENSION_PRESET
        )
        preset_index = self.extensions_preset_combo.findText(preset_name)
        self.extensions_preset_combo.blockSignals(True)
        self.extensions_preset_combo.setCurrentIndex(max(0, preset_index))
        self.extensions_preset_combo.blockSignals(False)
        self._refresh_sources_physical_drive_view()

        latest_scan_id = self.db.latest_scan_id_for_set(scan_set_key)
        if latest_scan_id is None:
            self._clear_loaded_paused_scan()
            self.current_scan_id = None
            self.results_view.load_groups([])
            self.results_view.set_scan_context_note("")
            self.tabs.setCurrentWidget(self.sources_tab)
            self.statusBar().showMessage(
                f"Loaded saved scan profile '{source_name}' "
                f"({self._format_scan_status(None)}). Start scan to continue."
            )
            return

        scan_info = self.db.get_scan_info(latest_scan_id)
        probe_backend_index = self.probe_backend_combo.findText(
            str(scan_info.get("probe_backend", "pyav"))
        )
        self.probe_backend_combo.setCurrentIndex(max(0, probe_backend_index))
        summary = self.db.scan_summary(latest_scan_id)
        status_text = self._format_scan_status(summary.get("status"))
        if status_text == "paused":
            self._load_paused_scan(
                scan_id=latest_scan_id,
                source_name=source_name,
                scan_info=scan_info,
            )
            return
        if status_text != "done":
            self._clear_loaded_paused_scan()
            self.current_scan_id = None
            self.results_view.load_groups([])
            self.results_view.set_scan_context_note("")
            stamp = self._format_scan_created_at(summary["created_at"])
            when = f" from {stamp}" if stamp else ""
            note = (
                f"Loaded saved scan profile '{source_name}'. Latest scan "
                f"#{latest_scan_id}{when} is {status_text}; "
                "start scan to continue."
            )
            self.tabs.setCurrentWidget(self.sources_tab)
            self.statusBar().showMessage(note)
            return

        groups = self.db.load_duplicate_groups(latest_scan_id)
        self._clear_loaded_paused_scan()
        self.current_scan_id = latest_scan_id
        self.results_view.load_groups(groups)
        stamp = self._format_scan_created_at(summary["created_at"])
        note = (
            f"Loaded saved scan #{latest_scan_id} from {stamp}; "
            "filesystem may have changed."
        )
        self.results_view.set_scan_context_note(note)
        self.tabs.setCurrentWidget(self.results_view)
        self.statusBar().showMessage(note)

    def _rescan_scan(self) -> None:
        if self._scan_tab_locked:
            return
        self._persist_settings()
        roots = normalize_roots_for_display(list(self.settings.scan_roots))
        if not roots:
            QMessageBox.warning(
                self,
                "Missing Sources",
                "Add at least one scan root in the Sources tab.",
            )
            self.tabs.setCurrentWidget(self.sources_tab)
            return
        profile = normalize_similarity_profile(self.settings.similarity_profile)
        extensions = normalize_extensions(list(self.settings.extensions))
        scan_set_key = build_scan_set_key(
            roots=roots,
            similarity_profile=profile,
            extensions=extensions,
        )
        confirm = QMessageBox.question(
            self,
            "Rescan (Fresh)",
            (
                "This will permanently delete scan history/artifacts for this "
                "scan set and any cached file artifacts "
                "under the selected folders.\n\n"
                "All cached thumbnails will also be cleared.\n\n"
                "Continue with fresh rescan?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            purge_counts = self.db.purge_for_fresh_rescan(
                scan_set_key=scan_set_key,
                roots=roots,
            )
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
