"""Scan lifecycle and duplicate-action helpers for the main window."""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..db import Database
from ..exporters import export_scan
from ..models import ScanIssue
from ..scanner import build_physical_drive_scan_plan
from .main_window_core import DeleteTarget, metric_float, metric_int, payload_dict
from .main_window_profiles import MainWindowProfilesMixin
from .workers import ScanWorker

if TYPE_CHECKING:
    from PySide6.QtGui import QCloseEvent

    from ..models import ScanResult


class MainWindowScanActionMixin(MainWindowProfilesMixin):
    """Active scan lifecycle and duplicate-action helper methods."""

    def _start_scan(self) -> None:
        """Create and launch a new background scan worker from current settings."""
        self._launch_scan()

    def _launch_scan(self, *, resume_scan_id: int | None = None) -> None:
        """Create and launch one background scan worker from the current UI state."""
        self._persist_settings()
        if not self.settings.scan_roots:
            QMessageBox.warning(
                self,
                "Missing Sources",
                "Add at least one scan root in the Sources tab.",
            )
            self.tabs.setCurrentWidget(self.sources_tab)
            return

        self._clear_loaded_paused_scan()
        lane_plan = build_physical_drive_scan_plan(
            roots=list(self.settings.scan_roots),
            max_workers=int(self.settings.max_workers),
            drive_worker_overrides=self.settings.drive_worker_overrides,
        )
        self.scan_view.reset()
        if resume_scan_id is not None:
            self.scan_view.set_issues(self.db.list_scan_issues(resume_scan_id))
            self.scan_view.append_progress_note(
                "paused",
                f"Resuming paused scan #{resume_scan_id}",
            )
        self.scan_view.initialize_lane_plan(
            lane_plan.root_groups,
            lane_plan.effective_total_workers
            if lane_plan.effective_total_workers > 0
            else int(self.settings.max_workers),
        )
        self._set_scan_tab_lock(True)
        self.scan_view.set_running(True)
        if resume_scan_id is None:
            self.statusBar().showMessage("Scan started")
        else:
            self.statusBar().showMessage(f"Resuming scan #{resume_scan_id}...")
        self.tabs.setCurrentWidget(self.scan_view)

        self.scan_worker = ScanWorker(
            db_file=self.db_file,
            roots=self.settings.scan_roots,
            extensions=self.settings.normalized_extensions(),
            profile=self.settings.similarity_profile,
            max_workers=self.settings.max_workers,
            drive_worker_overrides=self.settings.drive_worker_overrides,
            probe_backend=self.settings.probe_backend,
            probe_worker_mode=self.settings.probe_worker_mode,
            ffmpeg_exe_path=self.settings.ffmpeg_exe_path,
            ffprobe_exe_path=self.settings.ffprobe_exe_path,
            db_batch_size=self.settings.scan_db_batch_size,
            db_flush_interval_ms=self.settings.scan_db_flush_interval_ms,
            enum_queue_max=self.settings.scan_enum_queue_max,
            progress_emit_interval_ms=self.settings.scan_progress_emit_interval_ms,
            progress_emit_every_files=self.settings.scan_progress_emit_every_files,
            resume_scan_id=resume_scan_id,
        )
        self.scan_worker.signals.progress.connect(self.scan_view.update_progress)
        self.scan_worker.signals.issue.connect(self._scan_issue)
        self.scan_worker.signals.finished.connect(self._scan_finished)
        self.scan_worker.signals.error.connect(self._scan_error)
        self.thread_pool.start(self.scan_worker)

    def _pause_scan(self) -> None:
        """Request a graceful pause of the active scan worker."""
        if self.scan_worker is None:
            return
        self.scan_worker.pause()
        self.statusBar().showMessage("Pausing scan...")

    def _resume_scan(self) -> None:
        """Resume the currently loaded paused scan when edits are still safe."""
        paused_scan_id = self._loaded_paused_scan_id
        if paused_scan_id is None:
            return
        if self._paused_resume_requires_new_scan():
            choice = self._prompt_risky_paused_scan_edit()
            if choice == "new":
                self._clear_loaded_paused_scan()
                self._launch_scan()
                return
            self._reload_loaded_paused_scan_widgets()
            self.tabs.setCurrentWidget(self.scan_view)
            self.statusBar().showMessage("Paused scan kept unchanged.")
            return
        self._launch_scan(resume_scan_id=paused_scan_id)

    def _cancel_scan(self) -> None:
        """Request cancellation of the active scan worker."""
        if self.scan_worker is None:
            return
        confirm = QMessageBox.question(
            self,
            "Cancel Scan",
            "Cancel the current scan?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.scan_worker.cancel()
        self.statusBar().showMessage("Cancelling scan...")

    def _scan_issue(self, issue: object) -> None:
        """Append one live scan issue to the Scan tab as it arrives."""
        if not isinstance(issue, ScanIssue):
            return
        self.scan_view.append_issue(issue)
        self.statusBar().showMessage(f"Scan issue [{issue.stage}]: {issue.message}")

    def _scan_finished(self, result: ScanResult) -> None:
        """Refresh persisted results after a scan worker completes."""
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
            if status_text == "paused":
                scan_info = self.db.get_scan_info(finished_scan_id)
                self._set_loaded_paused_scan(
                    scan_id=finished_scan_id,
                    roots=list(scan_info.get("roots", [])),
                    profile=str(scan_info.get("profile", "balanced")),
                    extensions=list(scan_info.get("extensions", [])),
                    probe_backend=str(scan_info.get("probe_backend", "pyav")),
                )
                self.scan_view.set_paused_loaded(True)
                self.scan_view.append_progress_note(
                    "paused",
                    (
                        f"Scan #{finished_scan_id} paused. "
                        "You can edit sources, then resume."
                    ),
                )
                self.statusBar().showMessage(
                    f"Scan {finished_scan_id} paused: {len(result.issues)} issues."
                )
                return
            self._clear_loaded_paused_scan()
            self.statusBar().showMessage(
                f"Scan {finished_scan_id} {status_text}: {len(result.issues)} issues."
            )
            return

        self._clear_loaded_paused_scan()
        groups = self.db.load_duplicate_groups(finished_scan_id)
        self.results_view.load_groups(groups)
        self.results_view.set_scan_context_note("")
        self.tabs.setCurrentWidget(self.results_view)
        metrics = payload_dict(result.metrics)
        flush_count = metric_int(metrics, "flush_count")
        max_queue_depth = metric_int(metrics, "max_queue_depth")
        timing_summary = ""
        stage_seconds = payload_dict(metrics.get("stage_seconds", {}))
        if stage_seconds:
            matching_s = metric_float(stage_seconds, "matching")
            timing_summary = f", matching {matching_s:.2f}s"
        self.statusBar().showMessage(
            f"Scan {finished_scan_id} complete: {len(groups)} groups, "
            f"{len(result.issues)} issues"
            f" (flushes {flush_count}, queue {max_queue_depth}{timing_summary})."
        )

    def _scan_error(self, details: str) -> None:
        """Handle a failed background scan worker."""
        self.scan_worker = None
        self._set_scan_tab_lock(False)
        self.scan_view.set_running(False)
        self.statusBar().showMessage("Scan failed")
        QMessageBox.critical(self, "Scan Error", details)

    def _open_scan_subject_in_explorer(self, path: str) -> None:
        """Open Explorer on the selected progress or issue path."""
        target = Path(path) if path.strip() else None
        if target is None:
            self.statusBar().showMessage("No file is associated with that row.")
            return
        try:
            if not target.exists():
                self.statusBar().showMessage("The selected path no longer exists.")
                return
            command = (
                ["explorer.exe", str(target)]
                if target.is_dir()
                else ["explorer.exe", "/select,", str(target)]
            )
            subprocess.Popen(command)
        except Exception as exc:
            QMessageBox.warning(self, "Explorer Launch Failed", str(exc))

    def _next_zdele_path(self, source: Path) -> Path:
        """Return the next available `.z_dele` rename target for a file."""
        candidate = source.with_name(f"{source.name}.z_dele")
        if not candidate.exists():
            return candidate
        index = 1
        while True:
            alt = source.with_name(f"{source.name}.z_dele.{index}")
            if not alt.exists():
                return alt
            index += 1

    def _handle_delete_requested(self, mode: str, targets: list[DeleteTarget]) -> None:
        """Execute rename or permanent delete actions for selected result rows."""
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
                    self.db.refresh_file_after_rename(
                        file_id=file_id,
                        new_path=str(destination),
                    )
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
            QMessageBox.warning(
                self,
                "Delete Completed with Errors",
                "\n".join(failures[:20]),
            )
        else:
            self.statusBar().showMessage(f"Processed {success_count} file(s).")

    def _export_current_scan(self) -> None:
        """Export the currently loaded scan to CSV and JSON."""
        if self.current_scan_id is None:
            QMessageBox.warning(self, "No Scan", "Run a scan first.")
            return
        out_dir = QFileDialog.getExistingDirectory(self, "Choose export directory")
        if not out_dir:
            return
        csv_path, json_path = export_scan(
            self.db,
            scan_id=self.current_scan_id,
            out_dir=out_dir,
        )
        QMessageBox.information(
            self,
            "Export Complete",
            f"CSV: {csv_path}\nJSON: {json_path}",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        """Persist settings and close the DB connection on window shutdown."""
        if not self._full_reset_requested:
            self._persist_settings()
        with contextlib.suppress(Exception):
            self.db.close()
        super().closeEvent(event)
