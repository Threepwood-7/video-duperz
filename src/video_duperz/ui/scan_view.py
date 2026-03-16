"""Scan progress widget that presents runtime telemetry and issues."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from ..models import ScanIssue, ScanLaneSnapshot, ScanProgress


class ScanView(QWidget):
    """UI panel for scan controls, progress events, lane stats, and issues."""

    _PATH_ROLE = int(Qt.ItemDataRole.UserRole)

    start_requested = Signal()
    rescan_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()
    cancel_requested = Signal()
    path_activation_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.status_label = QLabel("Idle", self)
        self.stage_progress = QProgressBar(self)
        self.stage_progress.setRange(0, 100)
        self.stage_progress.setValue(0)
        self.eta_label = QLabel("ETA: --", self)
        self.worker_progress = QProgressBar(self)
        self.worker_progress.setRange(0, 1)
        self.worker_progress.setValue(0)
        self.worker_progress.setFormat("Workers 0/0")
        self.worker_progress.setVisible(False)
        self.io_stats_label = QLabel(
            "I/O Stats: discovered 0 @ 0.00/s, 0.00 MiB/s | analyzed 0 @ "
            "0.00/s, 0.00 MiB/s | cache hit 0.0%",
            self,
        )
        self.lane_table = QTableWidget(0, 12, self)
        self.lane_table.setHorizontalHeaderLabels(
            [
                "Lane",
                "Roots",
                "State",
                "Discovered",
                "Queued",
                "Completed",
                "Active File",
                "Workers",
                "Disc/s",
                "Disc MiB/s",
                "Anal/s",
                "Anal MiB/s",
            ]
        )
        self.lane_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.lane_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.lane_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.lane_table.verticalHeader().setVisible(False)
        lane_header = self.lane_table.horizontalHeader()
        lane_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        lane_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lane_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        lane_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        lane_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        lane_header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        lane_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        lane_header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        lane_header.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
        lane_header.setSectionResizeMode(9, QHeaderView.ResizeMode.ResizeToContents)
        lane_header.setSectionResizeMode(10, QHeaderView.ResizeMode.ResizeToContents)
        lane_header.setSectionResizeMode(11, QHeaderView.ResizeMode.ResizeToContents)
        self.progress_list = QListWidget(self)
        self.progress_list.setUniformItemSizes(True)
        self.progress_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.progress_list.itemDoubleClicked.connect(self._emit_item_path)
        self.issues_list = QListWidget(self)
        self.issues_list.setUniformItemSizes(True)
        self.issues_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.issues_list.itemDoubleClicked.connect(self._emit_item_path)
        self._max_progress_rows = 5000
        self._last_progress_row = ""
        self._lane_rows: dict[int, int] = {}
        self._worker_limit = 0
        self._paused_loaded = False

        self.start_btn = QPushButton("Start Scan", self)
        self.rescan_btn = QPushButton("Rescan", self)
        self.pause_btn = QPushButton("Pause Scan", self)
        self.resume_btn = QPushButton("Resume Scan", self)
        self.cancel_btn = QPushButton("Cancel Scan", self)
        self._apply_mode("idle")

        self.start_btn.clicked.connect(self.start_requested.emit)
        self.rescan_btn.clicked.connect(self.rescan_requested.emit)
        self.pause_btn.clicked.connect(self.pause_requested.emit)
        self.resume_btn.clicked.connect(self.resume_requested.emit)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)

        actions = QHBoxLayout()
        actions.addWidget(self.start_btn)
        actions.addWidget(self.rescan_btn)
        actions.addWidget(self.pause_btn)
        actions.addWidget(self.resume_btn)
        actions.addWidget(self.cancel_btn)
        actions.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(QLabel("Stage Progress", self))
        layout.addWidget(self.stage_progress)
        layout.addWidget(self.eta_label)
        layout.addWidget(self.io_stats_label)
        layout.addLayout(actions)
        layout.addWidget(QLabel("Parallel Lanes", self))
        layout.addWidget(self.lane_table, stretch=1)
        layout.addWidget(QLabel("Detailed Scan Progress", self))
        layout.addWidget(self.progress_list, stretch=2)
        layout.addWidget(QLabel("Scan Issues", self))
        layout.addWidget(self.issues_list, stretch=1)

    def _apply_mode(self, mode: str) -> None:
        """Apply the scan-action button state for one high-level mode."""
        is_running = mode == "running"
        is_paused = mode == "paused"
        self.start_btn.setEnabled(not is_running and not is_paused)
        self.rescan_btn.setEnabled(not is_running and not is_paused)
        self.pause_btn.setEnabled(is_running)
        self.resume_btn.setEnabled(is_paused)
        self.cancel_btn.setEnabled(is_running)

    def _format_counter(self, current: int, total: int) -> str:
        """Format one padded file-progress counter."""
        display_total = max(1, int(total))
        display_current = max(0, int(current))
        width = len(str(display_total))
        return f"{display_current:>{width}} / {display_total}"

    def _format_eta(self, progress: ScanProgress) -> str:
        """Format the ETA label from the current analysis throughput."""
        analyzed = int(progress.analyzed_files or 0)
        total = int(progress.total_analyze_files or 0)
        elapsed_s = float(progress.elapsed_s or 0.0)
        if analyzed < 3 or elapsed_s < 5.0 or total <= analyzed:
            return "ETA: --"
        files_per_s = float(analyzed) / elapsed_s if elapsed_s > 0.0 else 0.0
        if files_per_s <= 0.0:
            return "ETA: --"
        remaining_files = max(0, total - analyzed)
        remaining_s = remaining_files / files_per_s
        remaining_minutes = max(0.0, remaining_s / 60.0)
        remaining_text = (
            "<1m" if remaining_minutes < 1.0 else f"{round(remaining_minutes)}m"
        )
        done_by = datetime.now() + timedelta(seconds=remaining_s)
        return f"ETA: {remaining_text} | Done by {done_by:%H:%M}"

    def _progress_row_text(self, progress: ScanProgress) -> str:
        """Render one stable detailed-progress row string."""
        message = progress.message.strip() or progress.stage
        return (
            f"[{progress.stage}] "
            f"{self._format_counter(progress.current, progress.total)} -> {message}"
        )

    def _issue_row_text(self, issue: ScanIssue) -> str:
        """Render one stable issue row string."""
        if issue.path.strip():
            return f"[{issue.stage}] {issue.path} -> {issue.message}"
        return f"[{issue.stage}] {issue.message}"

    def _append_list_item(
        self,
        target: QListWidget,
        text: str,
        subject_path: str = "",
    ) -> None:
        """Append one list item with its structured path payload."""
        item = QListWidgetItem(text)
        item.setData(self._PATH_ROLE, subject_path)
        target.addItem(item)
        if target is self.progress_list and target.count() > self._max_progress_rows:
            target.takeItem(0)
        target.scrollToBottom()

    def _emit_item_path(self, item: QListWidgetItem) -> None:
        """Emit the structured path associated with one double-clicked row."""
        subject_path = str(item.data(self._PATH_ROLE) or "")
        self.path_activation_requested.emit(subject_path)

    def reset(self) -> None:
        self.status_label.setText("Idle")
        self.stage_progress.setValue(0)
        self.worker_progress.setRange(0, 1)
        self.worker_progress.setValue(0)
        self.worker_progress.setFormat("Workers 0/0")
        self.eta_label.setText("ETA: --")
        self.io_stats_label.setText(
            "I/O Stats: discovered 0 @ 0.00/s, 0.00 MiB/s | analyzed 0 @ "
            "0.00/s, 0.00 MiB/s | cache hit 0.0%"
        )
        self.lane_table.setRowCount(0)
        self.progress_list.clear()
        self.issues_list.clear()
        self._last_progress_row = ""
        self._lane_rows = {}
        self._worker_limit = 0
        self._paused_loaded = False
        self._apply_mode("idle")

    def initialize_lane_plan(
        self, root_groups: list[list[str]], worker_limit: int
    ) -> None:
        self.lane_table.setRowCount(0)
        self._lane_rows = {}
        for lane, roots in enumerate(root_groups):
            row = self.lane_table.rowCount()
            self.lane_table.insertRow(row)
            self._lane_rows[lane] = row
            self.lane_table.setItem(row, 0, QTableWidgetItem(str(lane + 1)))
            self.lane_table.setItem(
                row, 1, QTableWidgetItem(", ".join(roots) if roots else "")
            )
            self.lane_table.setItem(row, 2, QTableWidgetItem("pending"))
            self.lane_table.setItem(row, 3, QTableWidgetItem("0"))
            self.lane_table.setItem(row, 4, QTableWidgetItem("0"))
            self.lane_table.setItem(row, 5, QTableWidgetItem("0"))
            self.lane_table.setItem(row, 6, QTableWidgetItem(""))
            self.lane_table.setItem(row, 7, QTableWidgetItem("0/0"))
            self.lane_table.setItem(row, 8, QTableWidgetItem("0.00"))
            self.lane_table.setItem(row, 9, QTableWidgetItem("0.00"))
            self.lane_table.setItem(row, 10, QTableWidgetItem("0.00"))
            self.lane_table.setItem(row, 11, QTableWidgetItem("0.00"))
            self._apply_row_background(row, "pending")
        self._set_worker_progress(0, worker_limit)

    def _set_worker_progress(self, active_workers: int, worker_limit: int) -> None:
        bounded_limit = max(0, int(worker_limit))
        self._worker_limit = bounded_limit
        self.worker_progress.setRange(0, max(1, bounded_limit))
        self.worker_progress.setValue(
            max(0, min(int(active_workers), max(1, bounded_limit)))
        )
        self.worker_progress.setFormat(f"Workers {int(active_workers)}/{bounded_limit}")

    def _row_background_for_state(self, state: str) -> QColor:
        normalized = str(state or "").strip().lower()
        if normalized == "idle":
            return QColor("WhiteSmoke")
        if normalized == "done":
            return QColor("HoneyDew")
        if normalized in {"pending", "queued"}:
            return QColor("Beige")
        if normalized == "running":
            return QColor("AliceBlue")
        if normalized.startswith("error"):
            return QColor("MistyRose")
        return QColor("White")

    def _apply_row_background(self, row: int, state: str) -> None:
        color = self._row_background_for_state(state)
        for col in range(self.lane_table.columnCount()):
            cell = self.lane_table.item(row, col)
            if cell is not None:
                cell.setBackground(color)

    def _upsert_lane_snapshot(self, snapshot: ScanLaneSnapshot) -> None:
        lane = int(snapshot.lane)
        row = self._lane_rows.get(lane)
        if row is None:
            row = self.lane_table.rowCount()
            self.lane_table.insertRow(row)
            self._lane_rows[lane] = row
        roots_text = ", ".join(snapshot.roots) if snapshot.roots else ""
        workers_text = (
            f"{int(snapshot.workers)}/{self._worker_limit}"
            if self._worker_limit
            else str(int(snapshot.workers))
        )
        self.lane_table.setItem(row, 0, QTableWidgetItem(str(lane + 1)))
        self.lane_table.setItem(row, 1, QTableWidgetItem(roots_text))
        self.lane_table.setItem(row, 2, QTableWidgetItem(snapshot.state))
        self.lane_table.setItem(row, 3, QTableWidgetItem(str(int(snapshot.discovered))))
        self.lane_table.setItem(row, 4, QTableWidgetItem(str(int(snapshot.queued))))
        self.lane_table.setItem(row, 5, QTableWidgetItem(str(int(snapshot.completed))))
        self.lane_table.setItem(row, 6, QTableWidgetItem(snapshot.active_file))
        self.lane_table.setItem(row, 7, QTableWidgetItem(workers_text))
        self.lane_table.setItem(
            row, 8, QTableWidgetItem(f"{float(snapshot.discovered_files_per_s):.2f}")
        )
        self.lane_table.setItem(
            row, 9, QTableWidgetItem(f"{float(snapshot.discovered_mib_per_s):.2f}")
        )
        self.lane_table.setItem(
            row, 10, QTableWidgetItem(f"{float(snapshot.analyzed_files_per_s):.2f}")
        )
        self.lane_table.setItem(
            row, 11, QTableWidgetItem(f"{float(snapshot.analyzed_mib_per_s):.2f}")
        )
        self._apply_row_background(row, snapshot.state)

    def _update_io_stats(self, progress: ScanProgress) -> None:
        discovered_files = int(progress.discovered_files or 0)
        discovered_mib = float(progress.discovered_bytes or 0) / (1024.0 * 1024.0)
        analyzed_files = int(progress.analyzed_files or 0)
        analyzed_mib = float(progress.analyzed_bytes or 0) / (1024.0 * 1024.0)
        discovered_fps = float(progress.discovered_files_per_s or 0.0)
        discovered_mibps = float(progress.discovered_mib_per_s or 0.0)
        analyzed_fps = float(progress.analyzed_files_per_s or 0.0)
        analyzed_mibps = float(progress.analyzed_mib_per_s or 0.0)
        cache_hit_ratio = float(progress.cache_hit_ratio or 0.0) * 100.0
        self.io_stats_label.setText(
            "I/O Stats: "
            f"discovered {discovered_files} ({discovered_mib:.2f} MiB) @ "
            f"{discovered_fps:.2f}/s, {discovered_mibps:.2f} MiB/s | "
            f"analyzed {analyzed_files} ({analyzed_mib:.2f} MiB) @ "
            f"{analyzed_fps:.2f}/s, {analyzed_mibps:.2f} MiB/s | "
            f"cache hit {cache_hit_ratio:.1f}%"
        )

    def set_running(self, running: bool) -> None:
        if running:
            self._paused_loaded = False
            self._apply_mode("running")
            self.status_label.setText("Running...")
            return
        if not self._paused_loaded:
            self._apply_mode("idle")

    def set_paused_loaded(self, paused_loaded: bool) -> None:
        """Toggle the explicit paused-scan action state."""
        self._paused_loaded = bool(paused_loaded)
        if self._paused_loaded:
            self._apply_mode("paused")
            if not self.status_label.text().strip():
                self.status_label.setText("Paused")
            return
        self._apply_mode("idle")

    def append_progress_note(
        self,
        stage: str,
        message: str,
        subject_path: str = "",
    ) -> None:
        """Append one synthetic progress row without requiring a full snapshot."""
        self._append_list_item(
            self.progress_list,
            f"[{stage}] {message}",
            subject_path,
        )
        self._last_progress_row = ""

    def update_progress(self, progress: ScanProgress) -> None:
        total = max(1, progress.total)
        value = int((progress.current / total) * 100)
        self.stage_progress.setValue(max(0, min(100, value)))
        self.eta_label.setText(self._format_eta(progress))
        if progress.worker_limit is not None or progress.active_workers is not None:
            self._set_worker_progress(
                int(progress.active_workers or 0),
                int(progress.worker_limit or self._worker_limit),
            )
        if progress.lane_snapshots:
            for snapshot in progress.lane_snapshots:
                self._upsert_lane_snapshot(snapshot)
        self._update_io_stats(progress)
        message = progress.message.strip() or progress.stage
        counter_text = self._format_counter(progress.current, progress.total)
        self.status_label.setText(f"{progress.stage}: {message} ({counter_text})")
        progress_row = self._progress_row_text(progress)
        if progress_row != self._last_progress_row:
            self._append_list_item(
                self.progress_list,
                progress_row,
                progress.subject_path,
            )
            self._last_progress_row = progress_row

    def append_issue(self, issue: ScanIssue) -> None:
        """Append one live issue row."""
        self._append_list_item(
            self.issues_list,
            self._issue_row_text(issue),
            issue.path,
        )

    def set_issues(self, issues: list[ScanIssue]) -> None:
        self.issues_list.clear()
        for issue in issues:
            self.append_issue(issue)
