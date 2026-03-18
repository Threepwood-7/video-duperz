"""Scan progress widget that presents runtime telemetry and issues."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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

SCAN_LANE_COL_LANE = 0
SCAN_LANE_COL_ROOTS = 1
SCAN_LANE_COL_STATE = 2
SCAN_LANE_COL_DISCOVERED = 3
SCAN_LANE_COL_QUEUED = 4
SCAN_LANE_COL_COMPLETED = 5
SCAN_LANE_COL_ACTIVE_FILE = 6
SCAN_LANE_COL_WORKERS = 7
SCAN_LANE_COL_DISC_PER_S = 8
SCAN_LANE_COL_DISC_MIB_PER_S = 9
SCAN_LANE_COL_ANAL_PER_S = 10
SCAN_LANE_COL_ANAL_MIB_PER_S = 11
SCAN_LANE_COL_PROGRESS = 12
SCAN_LANE_HEADERS = [
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
    "Progress",
]
SCAN_LANE_DEFAULT_WIDTHS = [
    56,
    240,
    90,
    84,
    72,
    84,
    420,
    80,
    72,
    86,
    72,
    86,
    180,
]


class _LaneProgressCell(QWidget):
    """Centered container widget for one lane progress bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setObjectName("scan_lane_progress_bar")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("0 / 0+ (--%)")
        self.progress_bar.setFixedHeight(12)
        self.progress_bar.setStyleSheet(
            """
            QProgressBar {
                min-height: 12px;
                max-height: 12px;
                border: 1px solid #c8c8c8;
                border-radius: 4px;
                background: #f6f6f6;
                text-align: center;
                padding: 0px;
            }
            QProgressBar::chunk {
                background-color: #6aa84f;
                border-radius: 3px;
            }
            """
        )
        layout.addWidget(self.progress_bar)


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
            "0.00/s, 0.00 MiB/s | cache hit 0.0% | reused 0 | fp-only 0 | reprobe 0"
            " | skipped failed 0",
            self,
        )
        self._column_widths: list[int] = []
        self._applying_column_widths = False
        self.lane_table = QTableWidget(0, len(SCAN_LANE_HEADERS), self)
        self.lane_table.setHorizontalHeaderLabels(SCAN_LANE_HEADERS)
        self.lane_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.lane_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.lane_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.lane_table.verticalHeader().setVisible(False)
        lane_header = self.lane_table.horizontalHeader()
        lane_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        lane_header.setStretchLastSection(False)
        lane_header.sectionResized.connect(self._on_column_resized)
        for index, width in enumerate(SCAN_LANE_DEFAULT_WIDTHS):
            self.lane_table.setColumnWidth(index, width)
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
        self.retry_failed_checkbox = QCheckBox(
            "Retry previously failed files (0)",
            self,
        )
        self.retry_failed_checkbox.setChecked(True)
        self.retry_failed_checkbox.setEnabled(False)
        self.retry_failed_checkbox.setVisible(False)
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
        actions.addWidget(self.retry_failed_checkbox)
        actions.addWidget(self.cancel_btn)
        actions.addStretch(1)

        layout = QVBoxLayout(self)
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
        analyzed = int(progress.completed_files or progress.analyzed_files or 0)
        total = int(progress.total_work_files or progress.total_analyze_files or 0)
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
        current, total = self._display_counter_values(progress)
        return f"[{progress.stage}] {self._format_counter(current, total)} -> {message}"

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
        self.stage_progress.setValue(0)
        self.worker_progress.setRange(0, 1)
        self.worker_progress.setValue(0)
        self.worker_progress.setFormat("Workers 0/0")
        self.eta_label.setText("ETA: --")
        self.io_stats_label.setText(
            "I/O Stats: discovered 0 @ 0.00/s, 0.00 MiB/s | analyzed 0 @ "
            "0.00/s, 0.00 MiB/s | cache hit 0.0% | reused 0 | fp-only 0 | reprobe 0"
            " | skipped failed 0"
        )
        self.lane_table.setRowCount(0)
        self.progress_list.clear()
        self.issues_list.clear()
        self._last_progress_row = ""
        self._lane_rows = {}
        self._worker_limit = 0
        self._paused_loaded = False
        self._set_retry_failed_state(visible=False, count=0, checked=True)
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
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_LANE,
                QTableWidgetItem(str(lane + 1)),
            )
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_ROOTS,
                QTableWidgetItem(", ".join(roots) if roots else ""),
            )
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_STATE,
                QTableWidgetItem("pending"),
            )
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_DISCOVERED,
                QTableWidgetItem("0"),
            )
            self.lane_table.setItem(row, SCAN_LANE_COL_QUEUED, QTableWidgetItem("0"))
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_COMPLETED,
                QTableWidgetItem("0"),
            )
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_ACTIVE_FILE,
                QTableWidgetItem(""),
            )
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_WORKERS,
                QTableWidgetItem("0/0"),
            )
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_DISC_PER_S,
                QTableWidgetItem("0.00"),
            )
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_DISC_MIB_PER_S,
                QTableWidgetItem("0.00"),
            )
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_ANAL_PER_S,
                QTableWidgetItem("0.00"),
            )
            self.lane_table.setItem(
                row,
                SCAN_LANE_COL_ANAL_MIB_PER_S,
                QTableWidgetItem("0.00"),
            )
            self.lane_table.setCellWidget(
                row,
                SCAN_LANE_COL_PROGRESS,
                self._create_lane_progress_cell(),
            )
            self._apply_row_background(row, "pending")
        if self._column_widths:
            self._set_table_column_widths(self._column_widths)
        else:
            self.fit_columns_to_contents()
        self._set_worker_progress(0, worker_limit)

    def _create_lane_progress_cell(self) -> _LaneProgressCell:
        """Create one vertically centered lane-progress cell widget."""
        return _LaneProgressCell(self.lane_table)

    def _lane_progress_bar(self, row: int) -> QProgressBar:
        """Return the lane progress-bar widget for one table row."""
        widget = self.lane_table.cellWidget(row, SCAN_LANE_COL_PROGRESS)
        if isinstance(widget, _LaneProgressCell):
            return widget.progress_bar
        progress_cell = self._create_lane_progress_cell()
        self.lane_table.setCellWidget(row, SCAN_LANE_COL_PROGRESS, progress_cell)
        return progress_cell.progress_bar

    def _on_column_resized(self, _section: int, _old_size: int, _new_size: int) -> None:
        """Persist live lane-table column widths when the user resizes them."""
        if self._applying_column_widths:
            return
        self._column_widths = self._capture_column_widths()

    def _set_table_column_widths(self, widths: list[int]) -> None:
        """Apply one complete width payload to the lane table."""
        if len(widths) != self.lane_table.columnCount():
            return
        self._applying_column_widths = True
        try:
            for index, width in enumerate(widths):
                self.lane_table.setColumnWidth(index, width)
        finally:
            self._applying_column_widths = False

    def _capture_column_widths(self) -> list[int]:
        """Capture the current live lane-table widths."""
        return [
            self.lane_table.columnWidth(index)
            for index in range(self.lane_table.columnCount())
        ]

    @staticmethod
    def _normalize_column_widths(widths: list[int], expected_count: int) -> list[int]:
        """Validate one persisted lane-table width payload."""
        if len(widths) != expected_count:
            return []
        normalized: list[int] = []
        for raw in widths:
            try:
                width = int(raw)
            except (TypeError, ValueError):
                return []
            if width < 0:
                return []
            normalized.append(width)
        return normalized

    def set_column_widths(self, widths: list[int]) -> None:
        """Apply persisted lane-table widths when the payload is well formed."""
        self._column_widths = self._normalize_column_widths(
            widths,
            expected_count=self.lane_table.columnCount(),
        )
        if self._column_widths:
            self._set_table_column_widths(self._column_widths)

    def column_widths(self) -> list[int]:
        """Return stored lane-table widths or capture them live from the table."""
        return self._column_widths or self._capture_column_widths()

    def fit_columns_to_contents(self) -> None:
        """Auto-fit lane columns once, then keep the resulting widths."""
        self.lane_table.resizeColumnsToContents()
        fitted = self._capture_column_widths()
        widened = [
            max(width, default_width)
            for width, default_width in zip(
                fitted,
                SCAN_LANE_DEFAULT_WIDTHS,
                strict=True,
            )
        ]
        self._set_table_column_widths(widened)
        self._column_widths = self._capture_column_widths()

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
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_LANE,
            QTableWidgetItem(str(lane + 1)),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_ROOTS,
            QTableWidgetItem(roots_text),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_STATE,
            QTableWidgetItem(snapshot.state),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_DISCOVERED,
            QTableWidgetItem(str(int(snapshot.discovered))),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_QUEUED,
            QTableWidgetItem(str(int(snapshot.queued))),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_COMPLETED,
            QTableWidgetItem(str(int(snapshot.completed))),
        )
        progress_bar = self._lane_progress_bar(row)
        self._update_lane_progress_bar(progress_bar, snapshot)
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_ACTIVE_FILE,
            QTableWidgetItem(snapshot.active_file),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_WORKERS,
            QTableWidgetItem(workers_text),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_DISC_PER_S,
            QTableWidgetItem(f"{float(snapshot.discovered_files_per_s):.2f}"),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_DISC_MIB_PER_S,
            QTableWidgetItem(f"{float(snapshot.discovered_mib_per_s):.2f}"),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_ANAL_PER_S,
            QTableWidgetItem(f"{float(snapshot.analyzed_files_per_s):.2f}"),
        )
        self.lane_table.setItem(
            row,
            SCAN_LANE_COL_ANAL_MIB_PER_S,
            QTableWidgetItem(f"{float(snapshot.analyzed_mib_per_s):.2f}"),
        )
        self._apply_row_background(row, snapshot.state)

    def _update_lane_progress_bar(
        self,
        progress_bar: QProgressBar,
        snapshot: ScanLaneSnapshot,
    ) -> None:
        """Render one lane-progress widget from the current snapshot."""
        discovered = max(0, int(snapshot.discovered))
        completed = max(0, int(snapshot.completed))
        percent_text = "--%"
        if not snapshot.discovery_complete and discovered <= 0:
            progress_bar.setRange(0, 0)
            progress_bar.setFormat("0 / 0+ (--%)")
        else:
            display_total = max(1, discovered)
            progress_bar.setRange(0, display_total)
            progress_bar.setValue(min(completed, display_total))
            suffix = "" if snapshot.discovery_complete else "+"
            percent = int((max(0, min(completed, display_total)) / display_total) * 100)
            percent_text = f"{percent}%"
            progress_bar.setFormat(
                f"{completed} / {discovered}{suffix} ({percent_text})"
            )
        progress_bar.setToolTip(
            "Completed {completed} of {discovered}{suffix} ({percent}) | "
            "cache {cache_hits}, fp-only {fingerprint_only}, reprobe {reprobe}".format(
                completed=completed,
                discovered=discovered,
                suffix="" if snapshot.discovery_complete else "+",
                percent=percent_text,
                cache_hits=int(snapshot.cache_hits),
                fingerprint_only=int(snapshot.fingerprint_only),
                reprobe=int(snapshot.probe_and_fingerprint),
            )
        )

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
        cache_hits = int(progress.cached_files or 0)
        fingerprint_only = int(progress.fingerprint_only_files or 0)
        reprobes = int(progress.probe_and_fingerprint_files or 0)
        skipped_failed = int(progress.skipped_failed_files or 0)
        self.io_stats_label.setText(
            "I/O Stats: "
            f"discovered {discovered_files} ({discovered_mib:.2f} MiB) @ "
            f"{discovered_fps:.2f}/s, {discovered_mibps:.2f} MiB/s | "
            f"analyzed {analyzed_files} ({analyzed_mib:.2f} MiB) @ "
            f"{analyzed_fps:.2f}/s, {analyzed_mibps:.2f} MiB/s | "
            f"cache hit {cache_hit_ratio:.1f}% | reused {cache_hits} | "
            f"fp-only {fingerprint_only} | reprobe {reprobes} | "
            f"skipped failed {skipped_failed}"
        )

    def _display_counter_values(self, progress: ScanProgress) -> tuple[int, int]:
        """Return the preferred visible counter pair for one progress frame."""
        if (
            progress.completed_files is not None
            and progress.total_work_files is not None
            and progress.stage in {"cache", "fingerprint", "probe", "error", "skip"}
        ):
            return (
                int(progress.completed_files),
                max(1, int(progress.total_work_files)),
            )
        return (int(progress.current), max(1, int(progress.total)))

    def set_running(self, running: bool) -> None:
        if running:
            self._paused_loaded = False
            self._apply_mode("running")
            return
        if not self._paused_loaded:
            self._apply_mode("idle")

    def set_paused_loaded(self, paused_loaded: bool) -> None:
        """Toggle the explicit paused-scan action state."""
        self._paused_loaded = bool(paused_loaded)
        if self._paused_loaded:
            self._apply_mode("paused")
            self.retry_failed_checkbox.setVisible(True)
            return
        self._set_retry_failed_state(visible=False, count=0, checked=True)
        self._apply_mode("idle")

    def _set_retry_failed_state(
        self,
        *,
        visible: bool,
        count: int,
        checked: bool,
    ) -> None:
        """Update the paused-only failed-file retry checkbox state."""
        retry_count = max(0, int(count))
        self.retry_failed_checkbox.setText(
            f"Retry previously failed files ({retry_count})"
        )
        self.retry_failed_checkbox.setChecked(bool(checked))
        self.retry_failed_checkbox.setEnabled(retry_count > 0)
        self.retry_failed_checkbox.setVisible(bool(visible))

    def set_retry_failed_file_count(self, count: int) -> None:
        """Show the failed-file retry checkbox for the loaded paused scan."""
        self._set_retry_failed_state(visible=True, count=count, checked=True)

    def retry_failed_files_enabled(self) -> bool:
        """Return whether the next resume should retry prior failed files."""
        if (
            not self.retry_failed_checkbox.isVisible()
            or not self.retry_failed_checkbox.isEnabled()
        ):
            return True
        return self.retry_failed_checkbox.isChecked()

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
        display_current, display_total = self._display_counter_values(progress)
        value = int((display_current / max(1, display_total)) * 100)
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
