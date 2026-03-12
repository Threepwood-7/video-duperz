from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
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
    start_requested = Signal()
    rescan_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.status_label = QLabel("Idle", self)
        self.stage_progress = QProgressBar(self)
        self.stage_progress.setRange(0, 100)
        self.stage_progress.setValue(0)
        self.worker_progress = QProgressBar(self)
        self.worker_progress.setRange(0, 1)
        self.worker_progress.setValue(0)
        self.worker_progress.setFormat("Workers 0/0")
        self.worker_progress.setVisible(False)
        self.worker_hint_label = QLabel(
            "Worker status is shown per lane in the Parallel Lanes table.", self
        )
        self.io_stats_label = QLabel(
            "I/O Stats: discovered 0 @ 0.00/s, 0.00 MiB/s | analyzed 0 @ 0.00/s, 0.00 MiB/s | cache hit 0.0%",
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
        self.progress_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.issues_list = QListWidget(self)
        self.issues_list.setUniformItemSizes(True)
        self.issues_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._max_progress_rows = 5000
        self._last_progress_row = ""
        self._lane_rows: dict[int, int] = {}
        self._worker_limit = 0

        self.start_btn = QPushButton("Start Scan", self)
        self.rescan_btn = QPushButton("Rescan", self)
        self.cancel_btn = QPushButton("Cancel Scan", self)
        self.cancel_btn.setEnabled(False)

        self.start_btn.clicked.connect(self.start_requested.emit)
        self.rescan_btn.clicked.connect(self.rescan_requested.emit)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)

        actions = QHBoxLayout()
        actions.addWidget(self.start_btn)
        actions.addWidget(self.rescan_btn)
        actions.addWidget(self.cancel_btn)
        actions.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(QLabel("Stage Progress", self))
        layout.addWidget(self.stage_progress)
        layout.addWidget(self.worker_hint_label)
        layout.addWidget(self.io_stats_label)
        layout.addLayout(actions)
        layout.addWidget(QLabel("Parallel Lanes", self))
        layout.addWidget(self.lane_table, stretch=1)
        layout.addWidget(QLabel("Detailed Scan Progress", self))
        layout.addWidget(self.progress_list, stretch=2)
        layout.addWidget(QLabel("Scan Issues", self))
        layout.addWidget(self.issues_list, stretch=1)

    def reset(self) -> None:
        self.status_label.setText("Idle")
        self.stage_progress.setValue(0)
        self.worker_progress.setRange(0, 1)
        self.worker_progress.setValue(0)
        self.worker_progress.setFormat("Workers 0/0")
        self.io_stats_label.setText(
            "I/O Stats: discovered 0 @ 0.00/s, 0.00 MiB/s | analyzed 0 @ 0.00/s, 0.00 MiB/s | cache hit 0.0%"
        )
        self.lane_table.setRowCount(0)
        self.progress_list.clear()
        self.issues_list.clear()
        self._last_progress_row = ""
        self._lane_rows = {}
        self._worker_limit = 0

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
            f"discovered {discovered_files} ({discovered_mib:.2f} MiB) @ {discovered_fps:.2f}/s, {discovered_mibps:.2f} MiB/s | "
            f"analyzed {analyzed_files} ({analyzed_mib:.2f} MiB) @ {analyzed_fps:.2f}/s, {analyzed_mibps:.2f} MiB/s | "
            f"cache hit {cache_hit_ratio:.1f}%"
        )

    def set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.rescan_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        if running:
            self.status_label.setText("Running...")

    def update_progress(self, progress: ScanProgress) -> None:
        total = max(1, progress.total)
        value = int((progress.current / total) * 100)
        self.stage_progress.setValue(max(0, min(100, value)))
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
        self.status_label.setText(
            f"{progress.stage}: {message} ({progress.current}/{progress.total})"
        )
        progress_row = (
            f"[{progress.stage}] {progress.current}/{progress.total} -> {message}"
        )
        if progress_row != self._last_progress_row:
            self.progress_list.addItem(progress_row)
            if self.progress_list.count() > self._max_progress_rows:
                self.progress_list.takeItem(0)
            self.progress_list.scrollToBottom()
            self._last_progress_row = progress_row

    def set_issues(self, issues: list[ScanIssue]) -> None:
        self.issues_list.clear()
        if not issues:
            self.issues_list.addItem("No issues.")
            return
        self.issues_list.addItems(
            [f"[{i.stage}] {i.path} -> {i.message}" for i in issues]
        )
