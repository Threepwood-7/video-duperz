"""Generate deterministic README screenshots from mocked demo data only."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from video_duperz.config import default_settings
from video_duperz.db import Database
from video_duperz.models import (
    DuplicateGroup,
    DuplicateItem,
    ScanIssue,
    ScanLaneSnapshot,
    ScanProgress,
)
from video_duperz.ui.main_window import MainWindow
from video_duperz.ui.results_view import SORT_GROUP_COUNT_DESC
from video_duperz.ui.results_view_shared import (
    COL_BIT_DEPTH,
    COL_DURATION,
    COL_FILE_NAME,
    COL_GROUP_ID,
    COL_HDR_FORMAT,
    COL_MATCH,
    COL_RESOLUTION,
    COL_SIMILARITY,
    COL_SIZE,
)
from video_duperz.ui.scan_view import (
    SCAN_LANE_COL_COMPLETED,
    SCAN_LANE_COL_ETA,
    SCAN_LANE_COL_LANE,
    SCAN_LANE_COL_PROGRESS,
    SCAN_LANE_COL_ROOTS,
    SCAN_LANE_COL_STATE,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _demo_path(*parts: str) -> str:
    return "\\".join(parts)


def _build_settings() -> object:
    settings = default_settings()
    settings.scan_roots = [
        "Demo Library Movies",
        "Demo Library Series",
    ]
    settings.recent_scan_roots = ["Demo Library Movies"]
    settings.extensions = ["mp4", "mkv", "avi", "mov"]
    settings.fingerprint_timeout_s = 45.0
    settings.thumbnail_size = "128x72"
    settings.ffmpeg_exe_path = "ffmpeg"
    settings.ffprobe_exe_path = "ffprobe"
    settings.fpcalc_exe_path = "fpcalc"
    settings.mediainfo_exe_path = "MediaInfo"
    settings.everything_exe_path = "Everything"
    return settings


def _duplicate_item(
    *,
    file_id: int,
    path: str,
    width: int,
    height: int,
    bitrate: int,
    similarity: float,
    size: int,
    duration_s: float,
    codec: str,
    keep_default: bool,
    hdr_format: str = "",
) -> DuplicateItem:
    return DuplicateItem(
        file_id=file_id,
        path=path,
        size=size,
        mtime_ns=1_700_000_000_000_000_000 + file_id,
        ctime_ns=1_700_000_000_000_000_000 + file_id,
        duration_s=duration_s,
        width=width,
        height=height,
        bitrate=bitrate,
        codec=codec,
        audio_stream_count=1,
        audio_codec="aac",
        audio_bitrate=128_000,
        audio_languages="eng",
        subtitle_languages="eng",
        similarity_score=similarity,
        keep_default=keep_default,
        fps=23.976,
        bit_depth=10 if hdr_format else 8,
        hdr_format=hdr_format,
        container=Path(path).suffix.lstrip("."),
        codec_profile="High",
        codec_level="4.1",
        is_interlaced=False,
        match_reason="perceptual",
        match_duration_delta_s=0.2,
        selected_action="keep" if keep_default else "delete",
    )


def _configure_sources(window: MainWindow) -> None:
    window.tabs.setCurrentWidget(window.sources_tab)
    window.roots_list.setCurrentRow(0)
    window.roots_list.setFixedHeight(180)
    for edit in (
        window.ffmpeg_exe_path_edit,
        window.ffprobe_exe_path_edit,
        window.fpcalc_exe_path_edit,
        window.mediainfo_exe_path_edit,
        window.everything_exe_path_edit,
    ):
        edit.setCursorPosition(0)
    window.sources_drive_summary_label.setText(
        "Matched physical drives: 2 | Requested workers: 2 | Effective workers: 2"
    )
    window.sources_drive_table.setRowCount(2)
    mock_rows = [
        (
            "Drive Alpha",
            "disk-a",
            "drive:alpha",
            "4.0 TB",
            "1.6 TB",
            "60",
            1,
            "Yes",
            "SSD lane",
        ),
        (
            "Drive Beta",
            "disk-b",
            "drive:beta",
            "8.0 TB",
            "5.1 TB",
            "36",
            1,
            "Yes",
            "Archive lane",
        ),
    ]
    for row_index, row_values in enumerate(mock_rows):
        for column_index, value in enumerate(row_values):
            if column_index == 6:
                spin = QSpinBox(window.sources_drive_table)
                spin.setRange(1, 8)
                spin.setValue(int(value))
                spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
                spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
                window.sources_drive_table.setCellWidget(row_index, column_index, spin)
                continue
            item = QTableWidgetItem(str(value))
            if column_index == 0:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            window.sources_drive_table.setItem(row_index, column_index, item)
    window.sources_drive_table.setColumnWidth(0, 180)
    window.sources_drive_table.setColumnWidth(1, 100)
    window.sources_drive_table.setColumnWidth(2, 120)
    window.sources_drive_table.setColumnWidth(3, 90)
    window.sources_drive_table.setColumnWidth(4, 90)
    window.sources_drive_table.setColumnWidth(5, 70)
    window.sources_drive_table.setColumnWidth(6, 80)
    window.sources_drive_table.setColumnWidth(7, 80)
    window.sources_drive_table.setColumnWidth(8, 120)
    scan_folders_group = window.findChild(QGroupBox, "sources_scan_folders_group")
    if scan_folders_group is not None:
        scan_folders_group.setFixedHeight(320)


def _configure_scan(window: MainWindow) -> None:
    window.scan_view.reset()
    window.scan_view.initialize_lane_plan(
        [
            ["Movies Lane"],
            ["Series Lane"],
        ],
        worker_limit=3,
    )
    window.scan_view.set_running(True)
    window.scan_view.set_retry_failed_file_count(4)
    window.scan_view.append_progress_note(
        "enumerate",
        "Assigned scan roots across two physical-drive lanes.",
    )
    window.scan_view.append_progress_note(
        "prepare",
        "Reused cached metadata for 18 unchanged files.",
    )
    window.scan_view.append_issue(
        ScanIssue(
            stage="probe",
            path=_demo_path("E:", "Demo Media", "Series", "broken_sample.mkv"),
            message="ffprobe timed out once and was queued for retry.",
        )
    )
    window.scan_view.update_progress(
        ScanProgress(
            stage="fingerprint",
            current=48,
            total=72,
            message="Fingerprinting representative frames for likely matches.",
            subject_path="Feature Cut 1080p.mkv",
            active_workers=3,
            worker_limit=3,
            discovered_files=72,
            discovered_bytes=93 * 1024 * 1024 * 1024,
            analyzed_files=48,
            analyzed_bytes=61 * 1024 * 1024 * 1024,
            cached_files=18,
            discovered_files_per_s=5.8,
            discovered_mib_per_s=912.4,
            analyzed_files_per_s=1.9,
            analyzed_mib_per_s=744.6,
            cache_hit_ratio=0.25,
            elapsed_s=1530.0,
            total_analyze_files=72,
            completed_files=48,
            total_work_files=72,
            skipped_failed_files=4,
            fingerprint_only_files=12,
            probe_and_fingerprint_files=36,
            lane_snapshots=[
                ScanLaneSnapshot(
                    lane=0,
                    roots=["Movies Lane"],
                    state="running",
                    discovered=39,
                    discovered_bytes=51 * 1024 * 1024 * 1024,
                    queued=5,
                    analyzed=28,
                    analyzed_bytes=36 * 1024 * 1024 * 1024,
                    completed=28,
                    discovery_complete=True,
                    cache_hits=11,
                    fingerprint_only=6,
                    probe_and_fingerprint=22,
                    discovered_files_per_s=3.1,
                    discovered_mib_per_s=488.2,
                    analyzed_files_per_s=1.2,
                    analyzed_mib_per_s=301.5,
                    active_file="Feature Cut 1080p.mkv",
                    workers=2,
                ),
                ScanLaneSnapshot(
                    lane=1,
                    roots=["Series Lane"],
                    state="running",
                    discovered=33,
                    discovered_bytes=42 * 1024 * 1024 * 1024,
                    queued=4,
                    analyzed=20,
                    analyzed_bytes=25 * 1024 * 1024 * 1024,
                    completed=20,
                    discovery_complete=True,
                    cache_hits=7,
                    fingerprint_only=6,
                    probe_and_fingerprint=14,
                    discovered_files_per_s=2.7,
                    discovered_mib_per_s=424.2,
                    analyzed_files_per_s=0.8,
                    analyzed_mib_per_s=221.4,
                    active_file="Concert Cut 4K.mp4",
                    workers=1,
                ),
            ],
        )
    )
    visible_lane_columns = {
        SCAN_LANE_COL_LANE,
        SCAN_LANE_COL_ROOTS,
        SCAN_LANE_COL_STATE,
        SCAN_LANE_COL_COMPLETED,
        SCAN_LANE_COL_ETA,
        SCAN_LANE_COL_PROGRESS,
    }
    for column in range(window.scan_view.lane_table.columnCount()):
        window.scan_view.lane_table.setColumnHidden(
            column,
            column not in visible_lane_columns,
        )
    window.scan_view.io_stats_label.setText(
        "I/O Stats: 72 discovered | 48 analyzed | 25.0% cache hit | 4 skipped failed"
    )
    window.scan_view.lane_table.resizeColumnsToContents()
    window.scan_view.progress_table.resizeColumnsToContents()
    window.scan_view.issues_table.resizeColumnsToContents()
    window.tabs.setCurrentWidget(window.scan_view)


def _configure_results(window: MainWindow) -> None:
    window.results_view._queue_thumbnail = lambda *args, **kwargs: None
    window.results_view.load_groups(
        [
            DuplicateGroup(
                scan_id=7,
                profile="balanced",
                created_at="now",
                total_size_bytes=8_500_000_000,
                group_id=101,
                items=[
                    _duplicate_item(
                        file_id=101,
                        path="Demo Library\\Season 01\\Episode 01 1080p.mkv",
                        width=1920,
                        height=1080,
                        bitrate=6_400_000,
                        similarity=0.992,
                        size=3_200_000_000,
                        duration_s=2680.0,
                        codec="h264",
                        keep_default=True,
                    ),
                    _duplicate_item(
                        file_id=102,
                        path="Demo Library\\Season 01\\Episode 01 WEB-DL.mp4",
                        width=1920,
                        height=1080,
                        bitrate=5_200_000,
                        similarity=0.986,
                        size=2_900_000_000,
                        duration_s=2681.0,
                        codec="h264",
                        keep_default=False,
                    ),
                    _duplicate_item(
                        file_id=103,
                        path="Demo Clips\\Promos\\Episode 01 Trailer.mp4",
                        width=1280,
                        height=720,
                        bitrate=1_400_000,
                        similarity=0.941,
                        size=650_000_000,
                        duration_s=2679.0,
                        codec="h264",
                        keep_default=False,
                    ),
                ],
            ),
            DuplicateGroup(
                scan_id=7,
                profile="balanced",
                created_at="now",
                total_size_bytes=14_200_000_000,
                group_id=102,
                items=[
                    _duplicate_item(
                        file_id=104,
                        path="Demo Features\\Feature 4K HDR10.mkv",
                        width=3840,
                        height=2160,
                        bitrate=15_000_000,
                        similarity=0.995,
                        size=7_100_000_000,
                        duration_s=2710.0,
                        codec="hevc",
                        keep_default=True,
                        hdr_format="HDR10",
                    ),
                    _duplicate_item(
                        file_id=105,
                        path="Demo Archive\\Feature 4K HDR10 Remux.mkv",
                        width=3840,
                        height=2160,
                        bitrate=12_300_000,
                        similarity=0.989,
                        size=7_100_000_000,
                        duration_s=2711.0,
                        codec="hevc",
                        keep_default=False,
                        hdr_format="HDR10",
                    ),
                ],
            ),
        ]
    )
    window.results_view.set_scan_context_note(
        "Balanced profile | 2 groups visible | 5 files loaded"
    )
    window.results_view.set_sort_mode(SORT_GROUP_COUNT_DESC)
    window.results_view.filter_include_name_edit.setText("episode|hdr")
    window.results_view.advanced_filters_toggle.setChecked(True)
    window.results_view.filter_min_similarity_spin.setValue(0.94)
    visible_columns = [False] * window.results_view.results_table.columnCount()
    for column in (
        COL_GROUP_ID,
        COL_FILE_NAME,
        COL_SIZE,
        COL_RESOLUTION,
        COL_BIT_DEPTH,
        COL_HDR_FORMAT,
        COL_DURATION,
        COL_SIMILARITY,
        COL_MATCH,
    ):
        visible_columns[column] = True
    window.results_view.set_column_visibility(visible_columns)
    window.results_view.fit_columns_to_contents()
    window.tabs.setCurrentWidget(window.results_view)


def _fit_table_height(table: QTableWidget, *, rows: int | None = None) -> None:
    """Clamp one table to a compact height for screenshot composition."""
    visible_rows = table.rowCount() if rows is None else min(rows, table.rowCount())
    content_height = table.horizontalHeader().height() + (table.frameWidth() * 2)
    for row in range(visible_rows):
        content_height += table.rowHeight(row)
    content_height += table.horizontalScrollBar().sizeHint().height()
    table.setFixedHeight(max(80, content_height + 6))


def _fit_table_width(table: QTableWidget, *, minimum_width: int = 320) -> None:
    """Clamp one table to its visible column width for compact screenshots."""
    content_width = table.verticalHeader().width() + (table.frameWidth() * 2)
    for column in range(table.columnCount()):
        if table.isColumnHidden(column):
            continue
        content_width += table.columnWidth(column)
    content_width += table.verticalScrollBar().sizeHint().width()
    table.setFixedWidth(max(minimum_width, content_width + 6))


def _render_widget(widget: QWidget) -> QPixmap:
    """Render one widget directly to a pixmap without parent-window chrome."""
    widget.ensurePolished()
    widget.adjustSize()
    size = widget.size()
    if size.isEmpty():
        size = widget.sizeHint()
    pixmap = QPixmap(size)
    pixmap.fill(QColor("#ffffff"))
    painter = QPainter(pixmap)
    try:
        widget.render(painter, QPoint(0, 0))
    finally:
        painter.end()
    return pixmap


def _compose_section_grabs(target: Path, sections: list[QWidget]) -> None:
    """Render selected widgets into one stacked high-readability screenshot."""
    pixmaps = [_render_widget(section) for section in sections if section.isVisible()]
    if not pixmaps:
        raise RuntimeError("No visible sections available for screenshot capture")
    padding = 28
    spacing = 22
    width = max(pixmap.width() for pixmap in pixmaps) + (padding * 2)
    height = sum(pixmap.height() for pixmap in pixmaps)
    height += padding * 2 + (spacing * (len(pixmaps) - 1))
    canvas = QPixmap(width, height)
    canvas.fill(QColor("#ffffff"))
    painter = QPainter(canvas)
    try:
        y = padding
        for pixmap in pixmaps:
            painter.drawPixmap(padding, y, pixmap)
            y += pixmap.height() + spacing
    finally:
        painter.end()
    if not canvas.save(str(target)):
        raise RuntimeError(f"Failed to save screenshot to {target}")


def _save_sources_screenshot(window: MainWindow, target: Path) -> None:
    scan_folders_group = window.findChild(QGroupBox, "sources_scan_folders_group")
    options_container = window.findChild(QWidget, "sources_options_container")
    if scan_folders_group is None or options_container is None:
        raise RuntimeError("Sources screenshot widgets were not found")
    _compose_section_grabs(
        target,
        [
            scan_folders_group,
            window.sources_drive_summary_label,
            options_container,
        ],
    )


def _save_scan_screenshot(window: MainWindow, target: Path) -> None:
    _fit_table_height(window.scan_view.lane_table)
    _fit_table_height(window.scan_view.progress_table, rows=3)
    _fit_table_height(window.scan_view.issues_table, rows=1)
    _fit_table_width(window.scan_view.lane_table, minimum_width=1000)
    _fit_table_width(window.scan_view.progress_table, minimum_width=1100)
    _fit_table_width(window.scan_view.issues_table, minimum_width=1100)
    _compose_section_grabs(
        target,
        [
            window.scan_view.stage_progress,
            window.scan_view.eta_label,
            window.scan_view.io_stats_label,
            window.scan_view.parallel_lanes_label,
            window.scan_view.lane_table,
            window.scan_view.detailed_progress_label,
            window.scan_view.progress_table,
            window.scan_view.scan_issues_label,
            window.scan_view.issues_table,
        ],
    )


def _save_results_screenshot(window: MainWindow, target: Path) -> None:
    basic_card = window.findChild(QGroupBox, "results_filter_basic_card")
    ranges_card = window.findChild(QGroupBox, "results_filter_ranges_card")
    attributes_card = window.findChild(QGroupBox, "results_filter_attributes_card")
    if basic_card is None or ranges_card is None or attributes_card is None:
        raise RuntimeError("Results screenshot filter cards were not found")
    _fit_table_height(window.results_view.results_table, rows=4)
    _fit_table_width(window.results_view.results_table, minimum_width=1200)
    _compose_section_grabs(
        target,
        [
            window.results_view.info_label,
            basic_card,
            window.results_view.advanced_filters_toggle,
            ranges_card,
            attributes_card,
            window.results_view.results_table,
        ],
    )


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

    app = QApplication.instance() or QApplication([])
    app_font = QFont(app.font())
    app_font.setPointSize(15)
    app.setFont(app_font)
    app.setStyleSheet(
        """
        QWidget {
            font-size: 15px;
        }
        QHeaderView::section {
            font-size: 14px;
        }
        """
    )
    images_dir = _repo_root() / "docs" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "app.db"
        with Database(db_path) as db:
            window = MainWindow(db=db, settings=_build_settings())
            window.resize(2400, 1500)
            window.show()
            app.processEvents()

            _configure_sources(window)
            app.processEvents()
            _save_sources_screenshot(window, images_dir / "ui-01-overview.png")

            _configure_scan(window)
            app.processEvents()
            _save_scan_screenshot(window, images_dir / "ui-02-workflow.png")

            _configure_results(window)
            app.processEvents()
            _save_results_screenshot(window, images_dir / "ui-03-details.png")

            window.close()
            app.processEvents()

    print(f"Refreshed mocked screenshots in {images_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
