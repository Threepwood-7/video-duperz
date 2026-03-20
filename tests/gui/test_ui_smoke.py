from __future__ import annotations

import os
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from video_duperz.config import (
    SCAN_LANE_TABLE_COLUMN_COUNT,
    default_settings,
    load_settings,
)
from video_duperz.db import Database

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QWidget,
)

if TYPE_CHECKING:
    from PySide6.QtGui import QAction

from video_duperz.config_video_presets import video_extensions_csv_for_preset
from video_duperz.models import (
    DuplicateGroup,
    DuplicateItem,
    SavedScanProfilePayload,
    ScanIssue,
    ScanLaneSnapshot,
    ScanProgress,
    VideoMeta,
)
from video_duperz.scan_sets import build_scan_set_key, normalize_roots_for_display
from video_duperz.scanner import PhysicalDriveInfo
from video_duperz.ui.main_window import MainWindow
from video_duperz.ui.results_view import (
    SORT_GROUP_COUNT_DESC,
    SORT_GROUP_SIZE_DESC,
    SORT_ROW_SIZE_DESC,
)
from video_duperz.ui.results_view_shared import (
    COL_BIT_DEPTH,
    COL_CHECK,
    COL_CODEC_LEVEL,
    COL_CODEC_PROFILE,
    COL_CONTAINER,
    COL_FILE_NAME,
    COL_FPS,
    COL_FULL_PATH,
    COL_HDR_FORMAT,
    COL_INTERLACED,
    COL_MATCH,
    COL_PARENT_DIR,
    COL_SIZE,
)
from video_duperz.ui.scan_view import (
    SCAN_ISSUE_COL_FILE,
    SCAN_ISSUE_COL_MESSAGE,
    SCAN_ISSUE_HEADERS,
    SCAN_LANE_COL_ACTIVE_FILE,
    SCAN_LANE_COL_ANAL_MIB_PER_S,
    SCAN_LANE_COL_ANAL_PER_S,
    SCAN_LANE_COL_DISC_MIB_PER_S,
    SCAN_LANE_COL_DISC_PER_S,
    SCAN_LANE_COL_PROGRESS,
    SCAN_LANE_HEADERS,
    SCAN_LOG_DEFAULT_WIDTHS,
    SCAN_PROGRESS_COL_FILE,
    SCAN_PROGRESS_COL_MESSAGE,
    SCAN_PROGRESS_COL_PROGRESS,
    SCAN_PROGRESS_HEADERS,
)
from video_duperz.ui.thumbnails import thumbnail_cache_dir


def _dup_item(
    file_id: int,
    path: str,
    width: int,
    height: int,
    bitrate: int,
    sim: float,
    *,
    size: int = 100,
    duration_s: float = 1.0,
    codec: str = "h264",
    fps: float = 30.0,
    bit_depth: int = 8,
    hdr_format: str = "",
    container: str = "",
    codec_profile: str = "",
    codec_level: str = "",
    is_interlaced: bool = False,
    match_reason: str = "perceptual",
    match_duration_delta_s: float = 0.0,
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
        fps=fps,
        bit_depth=bit_depth,
        hdr_format=hdr_format,
        container=container,
        codec_profile=codec_profile,
        codec_level=codec_level,
        is_interlaced=is_interlaced,
        bitrate=bitrate,
        codec=codec,
        audio_stream_count=1,
        audio_codec="aac",
        audio_bitrate=128000,
        audio_languages="eng",
        subtitle_languages="eng",
        similarity_score=sim,
        keep_default=file_id % 2 == 1,
        match_reason=match_reason,
        match_duration_delta_s=match_duration_delta_s,
        selected_action="keep",
    )


def _build_results_filter_groups(tmp_path: Path) -> list[DuplicateGroup]:
    """Return sample duplicate groups used by results filter tests."""
    return [
        DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(
                    11,
                    str(tmp_path / "alpha" / "alpha_keep_big.mp4"),
                    320,
                    240,
                    1000,
                    1.0,
                    size=220,
                ),
                _dup_item(
                    12,
                    str(tmp_path / "alpha" / "alpha_skip_small.mp4"),
                    320,
                    240,
                    900,
                    0.98,
                    size=40,
                ),
            ],
            total_size_bytes=260,
            group_id=42,
        ),
        DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(
                    13,
                    str(tmp_path / "beta" / "beta_keep_1.mp4"),
                    640,
                    360,
                    1200,
                    0.97,
                    size=90,
                ),
                _dup_item(
                    14,
                    str(tmp_path / "beta" / "beta_keep_2.mp4"),
                    640,
                    360,
                    1100,
                    0.96,
                    size=80,
                ),
                _dup_item(
                    15,
                    str(tmp_path / "beta" / "beta_skip_3.mp4"),
                    640,
                    360,
                    1000,
                    0.95,
                    size=70,
                ),
            ],
            total_size_bytes=240,
            group_id=43,
        ),
        DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(
                    16,
                    str(tmp_path / "gamma" / "gamma_skip_huge.mp4"),
                    800,
                    450,
                    1500,
                    0.94,
                    size=300,
                ),
                _dup_item(
                    17,
                    str(tmp_path / "gamma" / "gamma_keep_tiny.mp4"),
                    800,
                    450,
                    1400,
                    0.93,
                    size=20,
                ),
            ],
            total_size_bytes=320,
            group_id=44,
        ),
    ]


def _build_results_structured_filter_groups(tmp_path: Path) -> list[DuplicateGroup]:
    """Return duplicate groups with varied metadata for structured filters."""
    return [
        DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(
                    31,
                    str(tmp_path / "core" / "feature_cut_h264.mp4"),
                    1920,
                    1080,
                    4_500_000,
                    0.991,
                    size=25 * 1024 * 1024,
                    duration_s=180.0,
                    codec="h264",
                    hdr_format="",
                ),
                _dup_item(
                    32,
                    str(tmp_path / "core" / "feature_cut_hevc_hdr.mkv"),
                    3840,
                    2160,
                    8_200_000,
                    0.997,
                    size=80 * 1024 * 1024,
                    duration_s=240.0,
                    codec="hevc",
                    hdr_format="HDR10",
                ),
            ],
            total_size_bytes=(25 + 80) * 1024 * 1024,
            group_id=61,
        ),
        DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(
                    33,
                    str(tmp_path / "extras" / "extras_vp9_low.webm"),
                    1280,
                    720,
                    1_700_000,
                    0.945,
                    size=12 * 1024 * 1024,
                    duration_s=95.0,
                    codec="vp9",
                    hdr_format="",
                ),
                _dup_item(
                    34,
                    str(tmp_path / "extras" / "extras_h264_short.mp4"),
                    854,
                    480,
                    900_000,
                    0.905,
                    size=6 * 1024 * 1024,
                    duration_s=40.0,
                    codec="h264",
                    hdr_format="",
                ),
            ],
            total_size_bytes=(12 + 6) * 1024 * 1024,
            group_id=62,
        ),
    ]


def _load_delete_test_group(
    window: MainWindow,
    db: Database,
    *,
    file_paths: list[Path],
) -> tuple[int, list[dict[str, object]]]:
    """Persist one duplicate group for delete-action tests and load it into the UI."""
    scan_id = db.create_scan(
        profile="balanced",
        roots=[str(file_paths[0].parent)],
        extensions=sorted({path.suffix.lstrip(".") for path in file_paths}),
    )
    items: list[DuplicateItem] = []
    total_size_bytes = 0
    for index, path in enumerate(file_paths):
        if not path.exists():
            path.write_bytes(f"payload-{index}".encode())
        stat_result = path.stat()
        file_id = db.upsert_file(
            path=str(path),
            size=int(stat_result.st_size),
            mtime_ns=int(stat_result.st_mtime_ns),
            ctime_ns=int(stat_result.st_ctime_ns),
            ext=path.suffix.lstrip("."),
            scan_id=scan_id,
        )
        db.save_video_meta(
            file_id,
            VideoMeta(
                duration_s=10.0 + index,
                width=320,
                height=240,
                fps=24.0,
                codec="h264",
                bitrate=900_000 - (index * 10_000),
                audio_stream_count=1,
                audio_codec="aac",
                audio_bitrate=128_000,
                audio_languages="eng",
                subtitle_languages="eng",
                hdr_format="",
            ),
        )
        item = _dup_item(
            file_id,
            str(path),
            320,
            240,
            900_000 - (index * 10_000),
            1.0 - (index * 0.01),
            size=int(stat_result.st_size),
            duration_s=10.0 + index,
        )
        items.append(item)
        total_size_bytes += int(stat_result.st_size)
    group_db_id = db.insert_duplicate_group(
        scan_id=scan_id,
        profile="balanced",
        total_size_bytes=total_size_bytes,
    )
    for item in items:
        db.insert_duplicate_item(group_db_id, item)
    db.complete_scan(scan_id, status="done")
    window.current_scan_id = scan_id
    window.results_view._thumbnails_enabled = False
    window.results_view.load_groups(db.load_duplicate_groups(scan_id))
    targets = [
        {
            "row": row,
            "file_id": item.file_id,
            "group_db_id": group_db_id,
            "path": item.path,
        }
        for row, item in enumerate(items)
    ]
    return scan_id, targets


def _results_menu_actions(window: MainWindow) -> list[QAction]:
    """Return the actions currently exposed by the top-level Actions menu."""
    assert window.actions_menu is not None
    return list(window.actions_menu.actions())


def _visible_result_paths(window: MainWindow) -> list[str]:
    """Return the currently visible full-result paths from the results table."""
    return [
        window.results_view.results_table.item(row, COL_FULL_PATH).text()
        for row in range(window.results_view.results_table.rowCount())
    ]


def _wait_until_table_text(
    app: QApplication,
    table,
    row: int,
    column: int,
    *,
    timeout_s: float = 2.0,
) -> str:
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        app.processEvents()
        item = table.item(row, column)
        if item is not None and item.text():
            return item.text()
        QTest.qWait(10)
    item = table.item(row, column)
    return "" if item is None else item.text()


def _lane_progress_bar(cell_widget: QWidget | None) -> QProgressBar:
    """Return the inner lane progress bar from one table cell widget."""
    assert cell_widget is not None
    progress_bar = cell_widget.findChild(QProgressBar, "scan_lane_progress_bar")
    assert progress_bar is not None
    return progress_bar


def _table_row_texts(table: QTableWidget, row: int) -> list[str]:
    """Return the visible text for one table row."""
    values: list[str] = []
    for column in range(table.columnCount()):
        item = table.item(row, column)
        values.append("" if item is None else item.text())
    return values


def _table_cell_background_name(
    table: QTableWidget,
    row: int,
    column: int = 0,
) -> str:
    """Return the normalized background color name for one table cell."""
    item = table.item(row, column)
    assert item is not None
    return item.background().color().name().lower()


def test_main_window_launches_with_new_results_table(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert window.thumbnail_size_combo.count() == 4
        assert window.add_recent_root_btn.text() == "Add Recent Folder"
        assert window.results_view.results_table.columnCount() == 28
        assert window.results_view.results_table.horizontalHeaderItem(2).text() == "="
        assert (
            window.results_view.results_table.horizontalHeaderItem(3).text()
            == "Thumbnail"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(5).text()
            == "Extension"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(COL_FPS).text()
            == "FPS"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(
                COL_INTERLACED
            ).text()
            == "Interlaced"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(COL_BIT_DEPTH).text()
            == "Bit Depth"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(
                COL_HDR_FORMAT
            ).text()
            == "HDR Format"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(
                COL_CODEC_PROFILE
            ).text()
            == "Codec Profile"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(
                COL_CODEC_LEVEL
            ).text()
            == "Codec Level"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(COL_CONTAINER).text()
            == "Container"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(18).text()
            == "Audio Codec"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(COL_MATCH).text()
            == "Match"
        )

        group = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(
                    11,
                    str(tmp_path / "missing.mp4"),
                    320,
                    240,
                    1000,
                    1.0,
                    fps=23.976,
                    bit_depth=10,
                    hdr_format="HDR10",
                    container="mkv",
                    codec_profile="High",
                    codec_level="4.1",
                    is_interlaced=True,
                    match_reason="audio_match",
                ),
                _dup_item(
                    12,
                    str(tmp_path / "missing_copy.mp4"),
                    320,
                    240,
                    900,
                    0.98,
                    duration_s=6.0,
                ),
            ],
            total_size_bytes=200,
            group_id=42,
        )
        group.items[1].match_reason = "trimmed_match"
        group.items[1].match_duration_delta_s = 5.0
        window.results_view.load_groups([group])
        app.processEvents()
        initial_height = window.results_view.results_table.rowHeight(0)
        assert window.results_view.results_table.item(0, COL_MATCH).text() == "Audio"
        assert window.results_view.results_table.item(0, COL_FPS).text() == "23.976"
        assert (
            window.results_view.results_table.item(0, COL_BIT_DEPTH).text() == "10-bit"
        )
        assert (
            window.results_view.results_table.item(0, COL_HDR_FORMAT).text() == "HDR10"
        )
        assert window.results_view.results_table.item(0, COL_CONTAINER).text() == "mkv"
        assert window.results_view.results_table.item(0, COL_INTERLACED).text() == "Yes"
        assert (
            window.results_view.results_table.item(0, COL_CODEC_PROFILE).text()
            == "High"
        )
        assert (
            window.results_view.results_table.item(0, COL_CODEC_LEVEL).text() == "4.1"
        )
        assert "Scan Type: Interlaced" in (
            window.results_view.results_table.item(0, COL_CODEC_PROFILE).toolTip()
        )
        assert "Audio fingerprint rescue match" in (
            window.results_view.results_table.item(0, COL_MATCH).toolTip()
        )
        assert window.results_view.results_table.item(1, COL_MATCH).text() == "Trimmed"
        assert "Delta t 5.0s" in (
            window.results_view.results_table.item(1, COL_MATCH).toolTip()
        )

        for i in range(window.thumbnail_size_combo.count()):
            if str(window.thumbnail_size_combo.itemData(i)) == "160x90":
                window.thumbnail_size_combo.setCurrentIndex(i)
                break
        app.processEvents()
        resized_height = window.results_view.results_table.rowHeight(0)

        assert resized_height > initial_height
        window.close()


def test_sources_root_buttons_labels_order_and_state(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path / "one"), str(tmp_path / "two")]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert window.remove_root_btn.text() == "Remove Folder"
        assert window.remove_all_roots_btn.text() == "Remove All"
        sources_layout = window.sources_tab.layout()
        assert sources_layout is not None
        scan_folders_group = window.findChild(QGroupBox, "sources_scan_folders_group")
        physical_drives_group = window.findChild(
            QGroupBox,
            "sources_physical_drives_group",
        )
        scan_content_group = window.findChild(QGroupBox, "sources_scan_content_group")
        scan_performance_group = window.findChild(
            QGroupBox,
            "sources_scan_performance_group",
        )
        tools_group = window.findChild(
            QGroupBox,
            "sources_tool_paths_preview_group",
        )
        options_container = window.findChild(QWidget, "sources_options_container")
        assert scan_folders_group is not None
        assert physical_drives_group is not None
        assert scan_content_group is not None
        assert scan_performance_group is not None
        assert tools_group is not None
        assert options_container is not None
        assert sources_layout.itemAt(0).widget() is scan_folders_group
        assert sources_layout.itemAt(1).widget() is physical_drives_group
        assert sources_layout.itemAt(2).widget() is options_container
        assert scan_folders_group.title() == "Scan Folders"
        assert physical_drives_group.title() == "Physical Drives"
        content_labels = {
            label.text() for label in scan_content_group.findChildren(QLabel)
        }
        performance_labels = {
            label.text() for label in scan_performance_group.findChildren(QLabel)
        }
        tools_labels = {label.text() for label in tools_group.findChildren(QLabel)}
        assert "Extensions preset" in content_labels
        assert "Extensions (comma-separated, no dots required)" in content_labels
        assert "Similarity profile" in content_labels
        assert "Max workers total" in performance_labels
        assert "Parent CPU priority during scan" in performance_labels
        assert "Child I/O mode during scan" in performance_labels
        assert "ffmpeg executable override" in tools_labels
        assert "Thumbnail preview size" in tools_labels
        content_form_table = window.findChild(
            QWidget,
            "sources_scan_content_form_table",
        )
        performance_form_table = window.findChild(
            QWidget,
            "sources_scan_performance_form_table",
        )
        tools_form_table = window.findChild(
            QWidget,
            "sources_tool_paths_form_table",
        )
        assert content_form_table is not None
        assert performance_form_table is not None
        assert tools_form_table is not None
        assert isinstance(content_form_table.layout(), QGridLayout)
        assert isinstance(performance_form_table.layout(), QGridLayout)
        assert isinstance(tools_form_table.layout(), QGridLayout)

        scan_folders_layout = scan_folders_group.layout()
        assert scan_folders_layout is not None
        roots_actions_layout = scan_folders_layout.itemAt(1).layout()
        assert roots_actions_layout is not None
        button_texts: list[str] = []
        for i in range(roots_actions_layout.count()):
            widget = roots_actions_layout.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                button_texts.append(widget.text())
        assert button_texts[:6] == [
            "Add Folder",
            "Remove Folder",
            "Remove All",
            "Add Recent Folder",
            "Save Scan Set",
            "Load Saved Scan",
        ]

        window.roots_list.setCurrentRow(-1)
        app.processEvents()
        assert not window.remove_root_btn.isEnabled()
        assert window.remove_all_roots_btn.isEnabled()

        window.roots_list.setCurrentRow(0)
        app.processEvents()
        assert window.remove_root_btn.isEnabled()

        window.remove_root_btn.click()
        app.processEvents()
        assert window.roots_list.count() == 1

        window.remove_all_roots_btn.click()
        app.processEvents()
        assert window.roots_list.count() == 0
        assert not window.remove_root_btn.isEnabled()
        assert not window.remove_all_roots_btn.isEnabled()
        window.close()


def test_sources_tab_scan_shortcut_button_opens_scan_tab(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        scan_button = window.findChild(QPushButton, "sources_scan_btn")
        assert scan_button is window.sources_scan_btn
        assert scan_button.text() == "Scan"
        assert window.roots_list.minimumHeight() == 250
        assert window.roots_list.maximumHeight() > 250

        window.tabs.setCurrentWidget(window.sources_tab)
        app.processEvents()
        assert window.tabs.currentWidget() is window.sources_tab

        QTest.mouseClick(scan_button, Qt.MouseButton.LeftButton)
        app.processEvents()

        assert window.tabs.currentWidget() is window.scan_view
        window.close()


def test_recent_folder_history_button_and_persistence(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert not window.add_recent_root_btn.isEnabled()
        window._remember_recent_root("D:/Videos")
        window._remember_recent_root("E:/Archive")
        app.processEvents()
        assert window.add_recent_root_btn.isEnabled()
        assert window._recent_roots == [str(Path("E:/Archive")), str(Path("D:/Videos"))]

        window._add_recent_root_selected("D:/Videos")
        app.processEvents()
        roots = [
            window.roots_list.item(i).text() for i in range(window.roots_list.count())
        ]
        assert str(Path("D:/Videos")) in roots
        window.close()

    loaded = load_settings()
    assert loaded.recent_scan_roots[:2] == [
        str(Path("D:/Videos")),
        str(Path("E:/Archive")),
    ]


def test_sources_tab_defaults_to_broad_extensions_preset(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        window = MainWindow(db=db, settings=default_settings())
        window.show()
        app.processEvents()

        assert window.extensions_preset_combo.currentText() == "broad"
        assert window.extensions_edit.text() == video_extensions_csv_for_preset("broad")
        assert window.scan_size_mib_min_spin.value() == 50
        assert window.scan_size_mib_max_spin.value() == 0
        assert window.duration_tolerance_spin.value() == 8.0

        window.close()


def test_sources_tab_scan_size_filters_exist_and_persist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert (
            window.findChild(QSpinBox, "sources_scan_size_mib_min_spin")
            is window.scan_size_mib_min_spin
        )
        assert (
            window.findChild(QSpinBox, "sources_scan_size_mib_max_spin")
            is window.scan_size_mib_max_spin
        )
        assert window.scan_size_mib_min_spin.value() == 50
        assert window.scan_size_mib_max_spin.value() == 0
        assert "never enter duplicate analysis" in (
            window.scan_size_mib_min_spin.toolTip().lower()
        )
        assert "no upper size limit" in (
            window.scan_size_mib_max_spin.toolTip().lower()
        )

        window.scan_size_mib_min_spin.setValue(120)
        window.scan_size_mib_max_spin.setValue(700)
        app.processEvents()
        window._persist_settings()
        window.close()

    loaded = load_settings()
    assert loaded.scan_size_mib_min == 120
    assert loaded.scan_size_mib_max == 700


def test_sources_tab_duration_tolerance_exists_and_persists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert (
            window.findChild(QDoubleSpinBox, "sources_duration_tolerance_spin")
            is window.duration_tolerance_spin
        )
        assert window.duration_tolerance_spin.value() == 8.0
        assert "durations differ slightly" in (
            window.duration_tolerance_spin.toolTip().lower()
        )

        window.duration_tolerance_spin.setValue(9.5)
        app.processEvents()
        window._persist_settings()
        window.close()

    loaded = load_settings()
    assert loaded.duration_tolerance_s == 9.5


def test_sources_tab_duplicate_detection_controls_exist_and_persist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert window.profile_combo.findText("custom") >= 0
        assert isinstance(
            window.findChild(
                QDoubleSpinBox,
                "sources_custom_similarity_threshold_spin",
            ),
            QDoubleSpinBox,
        )
        assert isinstance(
            window.findChild(
                QSlider,
                "sources_custom_similarity_threshold_slider",
            ),
            QSlider,
        )
        assert isinstance(
            window.findChild(
                QCheckBox,
                "sources_scene_aware_sampling_check",
            ),
            QCheckBox,
        )
        assert isinstance(
            window.findChild(
                QCheckBox,
                "sources_audio_fingerprint_enabled_check",
            ),
            QCheckBox,
        )
        assert isinstance(
            window.findChild(
                QComboBox,
                "sources_cross_resolution_mode_combo",
            ),
            QComboBox,
        )
        assert isinstance(
            window.findChild(
                QLineEdit,
                "sources_fpcalc_exe_path_edit",
            ),
            QLineEdit,
        )
        assert not window.custom_similarity_threshold_row.isVisible()

        window.profile_combo.setCurrentText("custom")
        app.processEvents()
        assert window.custom_similarity_threshold_row.isVisible()
        window.custom_similarity_threshold_spin.setValue(0.22)
        window.scene_aware_sampling_check.setChecked(True)
        window.audio_fingerprint_enabled_check.setChecked(True)
        for index in range(window.cross_resolution_mode_combo.count()):
            if str(window.cross_resolution_mode_combo.itemData(index)) == "same_aspect":
                window.cross_resolution_mode_combo.setCurrentIndex(index)
                break
        window.fpcalc_exe_path_edit.setText(str(tmp_path / "tools" / "fpcalc.exe"))
        window._persist_settings()
        window.close()

    loaded = load_settings()
    assert loaded.similarity_profile == "custom"
    assert loaded.custom_similarity_threshold == 0.22
    assert loaded.scene_aware_sampling is True
    assert loaded.audio_fingerprint_enabled is True
    assert loaded.cross_resolution_mode == "same_aspect"
    assert loaded.fpcalc_exe_path == str(tmp_path / "tools" / "fpcalc.exe")


def test_results_column_widths_persist(tmp_path: Path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()

        group_a = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(11, str(tmp_path / "a.mp4"), 320, 240, 1000, 1.0),
                _dup_item(12, str(tmp_path / "a_copy.mp4"), 320, 240, 900, 0.98),
            ],
            total_size_bytes=200,
            group_id=42,
        )
        group_b = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(13, str(tmp_path / "b.mp4"), 640, 360, 1200, 0.97),
                _dup_item(14, str(tmp_path / "b_copy.mp4"), 640, 360, 1100, 0.96),
            ],
            total_size_bytes=200,
            group_id=43,
        )

        window.results_view.load_groups([group_a, group_b])
        app.processEvents()

        window.results_view.results_table.setColumnWidth(3, 280)
        window.results_view.results_table.setColumnWidth(COL_FULL_PATH, 520)
        app.processEvents()
        window.close()

    loaded = load_settings()
    assert loaded.results_table_column_widths[3] == 280
    assert loaded.results_table_column_widths[COL_FULL_PATH] == 520


def test_scan_lane_column_widths_auto_fit_and_persist(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])

    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.scan_view.initialize_lane_plan(
            [[str(tmp_path / "alpha")], [str(tmp_path / "beta")]],
            worker_limit=2,
        )
        app.processEvents()

        assert len(window.scan_view.column_widths()) == SCAN_LANE_TABLE_COLUMN_COUNT
        assert window.scan_view.lane_table.columnWidth(
            SCAN_LANE_COL_ACTIVE_FILE
        ) > window.scan_view.lane_table.columnWidth(SCAN_LANE_COL_PROGRESS)

        window.scan_view.lane_table.setColumnWidth(SCAN_LANE_COL_ACTIVE_FILE, 520)
        window.scan_view.lane_table.setColumnWidth(SCAN_LANE_COL_PROGRESS, 210)
        app.processEvents()
        window.close()

    loaded = load_settings()
    assert len(loaded.scan_lane_table_column_widths) == SCAN_LANE_TABLE_COLUMN_COUNT
    assert loaded.scan_lane_table_column_widths[SCAN_LANE_COL_ACTIVE_FILE] == 520
    assert loaded.scan_lane_table_column_widths[SCAN_LANE_COL_PROGRESS] == 210

    with Database(tmp_path / "app-second.db") as db:
        window = MainWindow(db=db, settings=loaded)
        window.show()
        app.processEvents()

        window.scan_view.initialize_lane_plan(
            [[str(tmp_path / "alpha")], [str(tmp_path / "beta")]],
            worker_limit=2,
        )
        app.processEvents()

        assert window.scan_view.lane_table.columnWidth(SCAN_LANE_COL_ACTIVE_FILE) == 520
        assert window.scan_view.lane_table.columnWidth(SCAN_LANE_COL_PROGRESS) == 210
        window.close()


def test_scan_log_column_widths_persist_and_stay_mirrored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])

    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        for index, default_width in enumerate(SCAN_LOG_DEFAULT_WIDTHS):
            assert window.scan_view.progress_table.columnWidth(index) == default_width
            assert window.scan_view.issues_table.columnWidth(index) == default_width

        window.scan_view.progress_table.setColumnWidth(SCAN_PROGRESS_COL_FILE, 333)
        window.scan_view.progress_table.setColumnWidth(SCAN_PROGRESS_COL_MESSAGE, 444)
        app.processEvents()

        assert window.scan_view.issues_table.columnWidth(SCAN_ISSUE_COL_FILE) == 333
        assert window.scan_view.issues_table.columnWidth(SCAN_ISSUE_COL_MESSAGE) == 444
        window.close()

    loaded = load_settings()
    assert loaded.scan_log_table_column_widths[SCAN_PROGRESS_COL_FILE] == 333
    assert loaded.scan_log_table_column_widths[SCAN_PROGRESS_COL_MESSAGE] == 444

    with Database(tmp_path / "app-second.db") as db:
        window = MainWindow(db=db, settings=loaded)
        window.show()
        app.processEvents()

        assert (
            window.scan_view.progress_table.columnWidth(SCAN_PROGRESS_COL_FILE) == 333
        )
        assert (
            window.scan_view.progress_table.columnWidth(SCAN_PROGRESS_COL_MESSAGE)
            == 444
        )
        assert window.scan_view.issues_table.columnWidth(SCAN_ISSUE_COL_FILE) == 333
        assert window.scan_view.issues_table.columnWidth(SCAN_ISSUE_COL_MESSAGE) == 444
        window.close()


def test_sources_drive_table_column_widths_persist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.list_physical_drives",
        lambda roots=None: [
            PhysicalDriveInfo(
                root="R:\\",
                volume_identity="volume:a",
                disk_tokens=["disk:0"],
                total_bytes=1_000,
                free_bytes=250,
                used_percent=75.0,
            ),
        ],
    )
    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: SimpleNamespace(
            matched_volume_identities={"volume:a"} if roots else set(),
            requested_worker_target=max_workers,
            effective_total_workers=1 if roots else 0,
        ),
    )

    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = ["R:/Videos"]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.sources_drive_table.setColumnWidth(0, 321)
        window.sources_drive_table.setColumnWidth(8, 456)
        app.processEvents()
        window._persist_settings()
        window.close()

    loaded = load_settings()
    assert loaded.sources_drive_table_column_widths[0] == 321
    assert loaded.sources_drive_table_column_widths[8] == 456

    with Database(tmp_path / "app-second.db") as db:
        window = MainWindow(db=db, settings=loaded)
        window.show()
        app.processEvents()

        assert window.sources_drive_table.columnWidth(0) == 321
        assert window.sources_drive_table.columnWidth(8) == 456
        window.close()


def test_fit_columns_targets_the_current_tab_view(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.list_physical_drives",
        lambda roots=None: [
            PhysicalDriveInfo(
                root="R:\\Very Long Root Name\\Videos",
                volume_identity="volume:a",
                disk_tokens=["disk:0"],
                total_bytes=1_000,
                free_bytes=250,
                used_percent=75.0,
            ),
        ],
    )
    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: SimpleNamespace(
            matched_volume_identities={"volume:a"} if roots else set(),
            requested_worker_target=max_workers,
            effective_total_workers=1 if roots else 0,
        ),
    )

    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = ["R:/Videos"]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        long_result_path = str(
            tmp_path
            / "very_long_results_folder_name"
            / "nested"
            / "result_clip_subject.mp4"
        )
        window.results_view.load_groups(
            [
                DuplicateGroup(
                    scan_id=1,
                    profile="balanced",
                    created_at="now",
                    items=[
                        _dup_item(1, long_result_path, 320, 240, 1000, 1.0),
                        _dup_item(
                            2,
                            str(tmp_path / "copy" / "result_clip_subject_copy.mp4"),
                            320,
                            240,
                            900,
                            0.98,
                        ),
                    ],
                    total_size_bytes=200,
                    group_id=42,
                )
            ]
        )
        window.scan_view.initialize_lane_plan(
            [[str(tmp_path / "lane_alpha")]],
            worker_limit=1,
        )
        window.scan_view.update_progress(
            ScanProgress(
                stage="probe",
                current=1,
                total=3,
                message="Analyzing a long-path scan item",
                lane_snapshots=[
                    ScanLaneSnapshot(
                        lane=0,
                        roots=[str(tmp_path / "lane_alpha")],
                        state="running",
                        discovered=3,
                        queued=2,
                        completed=1,
                        active_file=str(
                            tmp_path
                            / "lane_alpha"
                            / "nested"
                            / "a_very_long_active_file_name.mp4"
                        ),
                        workers=1,
                        discovered_files_per_s=1.0,
                        discovered_mib_per_s=2.0,
                        analyzed_files_per_s=1.0,
                        analyzed_mib_per_s=1.5,
                    )
                ],
            )
        )
        window.scan_view.append_progress_note(
            "probe",
            "Progress message",
            str(tmp_path / "scan_progress_subject_file_name.mp4"),
        )
        window.scan_view.append_issue(
            ScanIssue(
                stage="warning",
                path=str(tmp_path / "scan_issue_subject_file_name.mp4"),
                message="Issue message",
            )
        )
        app.processEvents()

        window.tabs.setCurrentWidget(window.sources_tab)
        window.sources_drive_table.setColumnWidth(0, 80)
        results_before_sources = window.results_view.results_table.columnWidth(
            COL_FULL_PATH
        )
        lane_before_sources = window.scan_view.lane_table.columnWidth(
            SCAN_LANE_COL_ACTIVE_FILE
        )
        window._fit_columns()
        app.processEvents()

        assert (
            window.statusBar().currentMessage() == "Sources columns fitted to contents."
        )
        assert window.sources_drive_table.columnWidth(0) > 80
        assert (
            window.results_view.results_table.columnWidth(COL_FULL_PATH)
            == results_before_sources
        )
        assert (
            window.scan_view.lane_table.columnWidth(SCAN_LANE_COL_ACTIVE_FILE)
            == lane_before_sources
        )

        window.tabs.setCurrentWidget(window.scan_view)
        window.scan_view.lane_table.setColumnWidth(SCAN_LANE_COL_ACTIVE_FILE, 90)
        window.scan_view.progress_table.setColumnWidth(SCAN_PROGRESS_COL_FILE, 60)
        window.scan_view.issues_table.setColumnWidth(SCAN_ISSUE_COL_FILE, 60)
        results_before_scan = window.results_view.results_table.columnWidth(
            COL_FULL_PATH
        )
        window._fit_columns()
        app.processEvents()

        assert window.statusBar().currentMessage() == "Scan columns fitted to contents."
        assert window.scan_view.lane_table.columnWidth(SCAN_LANE_COL_ACTIVE_FILE) > 90
        assert window.scan_view.progress_table.columnWidth(SCAN_PROGRESS_COL_FILE) > 60
        assert window.scan_view.progress_table.columnWidth(
            SCAN_PROGRESS_COL_FILE
        ) == window.scan_view.issues_table.columnWidth(SCAN_ISSUE_COL_FILE)
        assert (
            window.results_view.results_table.columnWidth(COL_FULL_PATH)
            == results_before_scan
        )

        window.tabs.setCurrentWidget(window.results_view)
        window.results_view.results_table.setColumnWidth(COL_FULL_PATH, 120)
        sources_before_results = window.sources_drive_table.columnWidth(0)
        scan_before_results = window.scan_view.progress_table.columnWidth(
            SCAN_PROGRESS_COL_FILE
        )
        window._fit_columns()
        app.processEvents()

        assert (
            window.statusBar().currentMessage() == "Results columns fitted to contents."
        )
        assert window.results_view.results_table.columnWidth(COL_FULL_PATH) > 120
        assert window.sources_drive_table.columnWidth(0) == sources_before_results
        assert (
            window.scan_view.progress_table.columnWidth(SCAN_PROGRESS_COL_FILE)
            == scan_before_results
        )
        window.close()


def test_group_formatting_and_keep_strategy(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()

        group_a = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(11, str(tmp_path / "a.mp4"), 320, 240, 1000, 1.0),
                _dup_item(12, str(tmp_path / "a_copy.mp4"), 320, 240, 900, 0.98),
            ],
            total_size_bytes=200,
            group_id=42,
        )
        group_b = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(13, str(tmp_path / "b.mp4"), 640, 360, 1200, 0.97),
                _dup_item(14, str(tmp_path / "b_copy.mp4"), 640, 360, 1100, 0.96),
            ],
            total_size_bytes=200,
            group_id=43,
        )
        window.results_view.load_groups([group_a, group_b])
        app.processEvents()

        assert window.results_view.results_table.item(0, 0).text() == "G0001"
        assert window.results_view.results_table.item(2, 0).text() == "G0002"
        assert window.results_view.results_table.item(0, 0).font().bold()
        assert window.results_view.results_table.item(1, 0).font().bold() is False

        color_a = (
            window.results_view.results_table.item(0, 0).background().color().name()
        )
        color_b = (
            window.results_view.results_table.item(2, 0).background().color().name()
        )
        assert color_a != color_b

        window.results_view.apply_keep_strategy("larger")
        app.processEvents()
        # In group A both sizes are equal. Tie breaks on quality/mtime/path,
        # so one row must remain unchecked.
        group_a_checks = [
            window.results_view.results_table.item(r, 1).checkState()
            for r in range(0, 2)
        ]
        assert group_a_checks.count(Qt.CheckState.Unchecked) == 1
        assert group_a_checks.count(Qt.CheckState.Checked) == 1
        window.close()


def test_results_identical_column_lazy_compare_and_cache(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view._thumbnails_enabled = False

        group_a_items: list[DuplicateItem] = []
        group_a_payload = b"A" * 8192
        for idx in range(16):
            path = tmp_path / f"g1_{idx:02d}.mp4"
            path.write_bytes(group_a_payload)
            group_a_items.append(
                _dup_item(
                    100 + idx,
                    str(path),
                    320,
                    240,
                    1000,
                    0.99,
                    size=path.stat().st_size,
                )
            )

        g2_a = tmp_path / "g2_a.mp4"
        g2_b = tmp_path / "g2_b.mp4"
        group_b_payload = b"B" * 6144
        g2_a.write_bytes(group_b_payload)
        g2_b.write_bytes(group_b_payload)
        group_b_items = [
            _dup_item(300, str(g2_a), 320, 240, 900, 0.98, size=g2_a.stat().st_size),
            _dup_item(301, str(g2_b), 320, 240, 900, 0.98, size=g2_b.stat().st_size),
        ]

        group_a = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=group_a_items,
            total_size_bytes=sum(item.size for item in group_a_items),
            group_id=42,
        )
        group_b = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=group_b_items,
            total_size_bytes=sum(item.size for item in group_b_items),
            group_id=43,
        )
        window.results_view.load_groups([group_a, group_b])
        window.tabs.setCurrentWidget(window.results_view)
        app.processEvents()

        table = window.results_view.results_table
        assert table.item(0, 2).text() == ""

        assert _wait_until_table_text(app, table, 0, 2) != ""

        group_b_row = next(
            row
            for row in range(table.rowCount())
            if table.item(row, 0).text() == "G0002"
        )
        assert table.item(group_b_row, 2).text() == ""

        table.verticalScrollBar().setValue(table.verticalScrollBar().maximum())
        group_b_symbol = _wait_until_table_text(app, table, group_b_row, 2)
        assert group_b_symbol != ""

        window.results_view.filter_include_path_edit.setText("g2_")
        window.results_view._apply_filter_inputs()
        for _ in range(50):
            app.processEvents()
        assert table.rowCount() == 2
        assert table.item(0, 2).text() == group_b_symbol
        assert table.item(1, 2).text() == group_b_symbol
        window.close()


def test_view_columns_menu_toggle_and_saved_view(tmp_path: Path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()

        group = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(11, str(tmp_path / "a.mp4"), 320, 240, 1000, 1.0),
                _dup_item(12, str(tmp_path / "a_copy.mp4"), 320, 240, 900, 0.98),
            ],
            total_size_bytes=200,
            group_id=42,
        )
        window.results_view.load_groups([group])
        app.processEvents()

        assert (
            len(window._column_toggle_actions)
            == window.results_view.results_table.columnCount()
        )
        assert window._columns_menu is not None
        top_level_view_menu = next(
            action.menu()
            for action in window.menuBar().actions()
            if action.text().replace("&", "") == "View"
        )
        assert top_level_view_menu is window._columns_menu
        assert all(
            action.text().replace("&", "") != "Columns"
            for action in window._columns_menu.actions()
        )
        window._columns_menu.popup(window.mapToGlobal(QPoint(32, 32)))
        app.processEvents()
        assert window._columns_menu.isVisible()
        toggle_rect = window._columns_menu.actionGeometry(
            window._column_toggle_actions[COL_FULL_PATH]
        )
        QTest.mouseClick(
            window._columns_menu,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            toggle_rect.center(),
        )
        app.processEvents()
        assert window._columns_menu.isVisible()
        assert window.results_view.results_table.isColumnHidden(COL_FULL_PATH)
        window._columns_menu.close()
        app.processEvents()

        window._column_toggle_actions[COL_FULL_PATH].setChecked(False)
        app.processEvents()
        assert window.results_view.results_table.isColumnHidden(COL_FULL_PATH)

        monkeypatch.setattr(
            "video_duperz.ui.main_window_settings.QInputDialog.getText",
            lambda *a, **k: ("Compact", True),
        )
        window._save_current_view()
        assert "Compact" in window._saved_column_views

        window._column_toggle_actions[COL_FULL_PATH].setChecked(True)
        app.processEvents()
        assert not window.results_view.results_table.isColumnHidden(COL_FULL_PATH)

        window._apply_saved_view("Compact")
        app.processEvents()
        assert window.results_view.results_table.isColumnHidden(COL_FULL_PATH)
        window.close()

    loaded = load_settings()
    assert "Compact" in loaded.saved_column_views


def test_view_sort_menu_and_results_filters(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(_build_results_filter_groups(tmp_path))
        app.processEvents()

        assert len(window._sort_actions) == 8
        assert window.results_view.filter_include_name_edit is not None
        assert window.results_view.filter_include_path_edit is not None
        assert window.results_view.filter_exclude_name_edit is not None
        assert window.results_view.filter_exclude_path_edit is not None

        window._sort_actions[SORT_GROUP_SIZE_DESC].trigger()
        app.processEvents()
        assert (
            "gamma"
            in window.results_view.results_table.item(0, COL_FULL_PATH).text().lower()
        )

        window._sort_actions[SORT_GROUP_COUNT_DESC].trigger()
        app.processEvents()
        assert (
            "beta"
            in window.results_view.results_table.item(0, COL_FULL_PATH).text().lower()
        )

        window._sort_actions[SORT_ROW_SIZE_DESC].trigger()
        app.processEvents()
        first_group = window.results_view.results_table.item(0, 0).text()
        first_group_rows = [
            row
            for row in range(window.results_view.results_table.rowCount())
            if window.results_view.results_table.item(row, 0).text() == first_group
        ]
        first_group_sizes = [
            int(
                window.results_view.results_table.item(row, COL_SIZE)
                .text()
                .replace(",", "")
            )
            for row in first_group_rows
        ]
        assert first_group_sizes == sorted(first_group_sizes, reverse=True)

        window.results_view.filter_include_name_edit.setText("KEEP")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 7
        assert any(
            "skip"
            in Path(
                window.results_view.results_table.item(row, COL_FULL_PATH).text()
            ).name.lower()
            for row in range(window.results_view.results_table.rowCount())
        )

        window.results_view.filter_exclude_path_edit.setText("gamma")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 5
        assert all(
            "gamma"
            not in window.results_view.results_table.item(row, COL_FULL_PATH)
            .text()
            .lower()
            for row in range(window.results_view.results_table.rowCount())
        )

        window.results_view.filter_include_path_edit.setText("beta")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 3
        assert all(
            "beta"
            in window.results_view.results_table.item(row, COL_FULL_PATH).text().lower()
            for row in range(window.results_view.results_table.rowCount())
        )
        window.close()


def test_results_filters_debounce_multi_value_and_enter_apply(tmp_path: Path) -> None:
    """Apply results filters after debounce or Enter using OR-matching terms."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(_build_results_filter_groups(tmp_path))
        app.processEvents()

        assert window.results_view.results_table.rowCount() == 7

        window.results_view.filter_include_name_edit.setText("KEEP|tiny")
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 7

        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 7

        window.results_view.filter_exclude_path_edit.setText("gamma|beta")
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 7

        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2
        assert all(
            "alpha"
            in window.results_view.results_table.item(row, COL_FULL_PATH).text().lower()
            for row in range(window.results_view.results_table.rowCount())
        )

        window.results_view.filter_include_name_edit.setText("")
        window.results_view.filter_exclude_path_edit.setText("")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 7

        window.results_view.filter_include_path_edit.setFocus()
        window.results_view.filter_include_path_edit.setText("BETA")
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 7

        QTest.keyClick(window.results_view.filter_include_path_edit, Qt.Key.Key_Return)
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 3
        assert all(
            "beta"
            in window.results_view.results_table.item(row, COL_FULL_PATH).text().lower()
            for row in range(window.results_view.results_table.rowCount())
        )
        window.close()


def test_results_structured_filters_and_clear_button(tmp_path: Path) -> None:
    """Debounce structured filters and reset them with one button."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(
            _build_results_structured_filter_groups(tmp_path)
        )
        window.tabs.setCurrentWidget(window.results_view)
        app.processEvents()

        assert isinstance(window.results_view.filter_toolbar, QWidget)
        assert (
            window.results_view.filter_toolbar.property("widget_alias")
            == "Results Filters"
        )
        basic_card = window.results_view.filter_toolbar.findChild(
            QGroupBox,
            "results_filter_basic_card",
        )
        advanced_toggle = window.results_view.filter_toolbar.findChild(
            QCheckBox,
            "results_filter_advanced_toggle",
        )
        advanced_container = window.results_view.filter_toolbar.findChild(
            QWidget,
            "results_filter_advanced_container",
        )
        ranges_card = window.results_view.filter_toolbar.findChild(
            QGroupBox,
            "results_filter_ranges_card",
        )
        attributes_card = window.results_view.filter_toolbar.findChild(
            QGroupBox,
            "results_filter_attributes_card",
        )
        assert basic_card is not None
        assert advanced_toggle is not None
        assert advanced_container is not None
        assert ranges_card is not None
        assert attributes_card is not None
        assert basic_card.isVisible()
        assert advanced_toggle.isVisible()
        assert advanced_toggle.text() == "Advanced Filters"
        assert advanced_toggle.isCheckable()
        assert advanced_toggle.isChecked() is False
        assert advanced_container.isVisible() is False
        assert isinstance(window.results_view.filter_text_hint_label, QLabel)
        assert window.results_view.filter_text_hint_label.text() == (
            "Case-insensitive, | means OR."
        )
        assert isinstance(
            window.results_view.filter_include_match_all_checkbox,
            QCheckBox,
        )
        assert window.results_view.filter_include_match_all_checkbox.toolTip() == (
            "Off: one matching file keeps the whole group visible. "
            "On: every surviving file must match, or the group is hidden."
        )
        assert isinstance(window.results_view.filter_min_size_spin, QDoubleSpinBox)
        assert isinstance(window.results_view.filter_max_duration_spin, QDoubleSpinBox)
        assert isinstance(window.results_view.filter_min_width_spin, QSpinBox)
        assert isinstance(window.results_view.filter_extension_combo, QComboBox)
        assert isinstance(window.results_view.filter_video_codec_combo, QComboBox)
        assert isinstance(window.results_view.clear_filters_button, QPushButton)
        assert (
            window.results_view.filter_min_size_spin.objectName()
            == "results_filter_min_size_spin"
        )
        assert (
            window.results_view.filter_extension_combo.property("widget_alias")
            == "Extension Filter"
        )
        assert (
            window.results_view.filter_video_codec_combo.property("widget_alias")
            == "Video Codec Filter"
        )
        assert window.results_view.results_table.rowCount() == 4
        assert window.results_view.clear_filters_button.isVisible()

        window.results_view.filter_include_name_edit.setText("feature")
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 4
        window.results_view.clear_filters_button.click()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 4
        assert window.results_view.filter_include_name_edit.text() == ""

        advanced_toggle.click()
        app.processEvents()
        assert advanced_toggle.isChecked()
        assert advanced_container.isVisible()

        window.results_view.filter_min_size_spin.setValue(20.0)
        app.processEvents()
        assert window.results_view._filter_apply_timer.isActive()
        assert window.results_view.results_table.rowCount() == 4
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        window.results_view.clear_filters_button.click()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 4

        window.results_view.filter_min_duration_spin.setValue(100.0)
        app.processEvents()
        assert window.results_view._filter_apply_timer.isActive()
        assert window.results_view.results_table.rowCount() == 4
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        window.results_view.clear_filters_button.click()
        app.processEvents()
        window.results_view.filter_min_similarity_spin.setValue(0.95)
        app.processEvents()
        assert window.results_view._filter_apply_timer.isActive()
        assert window.results_view.results_table.rowCount() == 4
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        window.results_view.clear_filters_button.click()
        app.processEvents()
        window.results_view.filter_min_width_spin.setValue(1900)
        app.processEvents()
        assert window.results_view._filter_apply_timer.isActive()
        assert window.results_view.results_table.rowCount() == 4
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        window.results_view.filter_min_height_spin.setValue(2000)
        app.processEvents()
        assert window.results_view._filter_apply_timer.isActive()
        assert window.results_view.results_table.rowCount() == 2
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        window.results_view.clear_filters_button.click()
        app.processEvents()
        window.results_view.filter_include_path_edit.setText("core")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2
        assert sorted(_visible_result_paths(window)) == sorted(
            [
                str(tmp_path / "core" / "feature_cut_h264.mp4"),
                str(tmp_path / "core" / "feature_cut_hevc_hdr.mkv"),
            ]
        )

        window.results_view.filter_video_codec_combo.setCurrentText("h264")
        app.processEvents()
        assert window.results_view._filter_apply_timer.isActive()
        assert window.results_view.results_table.rowCount() == 2
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2
        assert sorted(_visible_result_paths(window)) == sorted(
            [
                str(tmp_path / "core" / "feature_cut_h264.mp4"),
                str(tmp_path / "core" / "feature_cut_hevc_hdr.mkv"),
            ]
        )

        window.results_view.clear_filters_button.click()
        app.processEvents()
        window.results_view.filter_hdr_combo.setCurrentText("HDR only")
        app.processEvents()
        assert window.results_view._filter_apply_timer.isActive()
        assert window.results_view.results_table.rowCount() == 4
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2
        assert sorted(_visible_result_paths(window)) == sorted(
            [
                str(tmp_path / "core" / "feature_cut_h264.mp4"),
                str(tmp_path / "core" / "feature_cut_hevc_hdr.mkv"),
            ]
        )

        window.results_view.filter_include_match_all_checkbox.setChecked(True)
        app.processEvents()
        assert window.results_view._filter_apply_timer.isActive()
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 0

        window.results_view.clear_filters_button.click()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 4
        assert window.results_view.filter_include_name_edit.text() == ""
        assert window.results_view.filter_include_path_edit.text() == ""
        assert window.results_view.filter_exclude_name_edit.text() == ""
        assert window.results_view.filter_exclude_path_edit.text() == ""
        assert not window.results_view.filter_include_match_all_checkbox.isChecked()
        assert (
            window.results_view.filter_min_size_spin.value()
            == window.results_view.filter_min_size_spin.minimum()
        )
        assert (
            window.results_view.filter_min_width_spin.value()
            == window.results_view.filter_min_width_spin.minimum()
        )
        assert window.results_view.filter_extension_combo.currentText() == "Any"
        assert window.results_view.filter_video_codec_combo.currentText() == "Any"
        assert window.results_view.filter_hdr_combo.currentText() == "Any"
        advanced_toggle.click()
        app.processEvents()
        assert advanced_toggle.isChecked() is False
        assert advanced_container.isVisible() is False
        window.close()


def test_results_filter_attribute_options_refresh_and_fallback(tmp_path: Path) -> None:
    """Refresh extension options from loaded results and preserve valid choices."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(
            _build_results_structured_filter_groups(tmp_path)
        )
        app.processEvents()

        codec_items = [
            window.results_view.filter_video_codec_combo.itemText(index)
            for index in range(window.results_view.filter_video_codec_combo.count())
        ]
        assert codec_items == ["Any", "h264", "hevc", "vp9"]
        extension_items = [
            window.results_view.filter_extension_combo.itemText(index)
            for index in range(window.results_view.filter_extension_combo.count())
        ]
        assert extension_items == ["Any", "mkv", "mp4", "webm"]

        window.results_view.filter_extension_combo.setCurrentText("webm")
        app.processEvents()
        assert window.results_view._filter_apply_timer.isActive()
        assert window.results_view.results_table.rowCount() == 4
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2
        assert all(
            "extras"
            in window.results_view.results_table.item(row, COL_FULL_PATH).text().lower()
            for row in range(window.results_view.results_table.rowCount())
        )

        replacement_groups_keep_selection = [
            DuplicateGroup(
                scan_id=1,
                profile="balanced",
                created_at="now",
                items=[
                    _dup_item(
                        41,
                        str(tmp_path / "refresh" / "refresh_av1.webm"),
                        1920,
                        1080,
                        2_500_000,
                        0.97,
                        size=18 * 1024 * 1024,
                        duration_s=120.0,
                        codec="av1",
                    ),
                    _dup_item(
                        42,
                        str(tmp_path / "refresh" / "refresh_av1_copy.mp4"),
                        1920,
                        1080,
                        2_400_000,
                        0.965,
                        size=17 * 1024 * 1024,
                        duration_s=118.0,
                        codec="av1",
                    ),
                ],
                total_size_bytes=(18 + 17) * 1024 * 1024,
                group_id=63,
            )
        ]
        window.results_view.load_groups(replacement_groups_keep_selection)
        app.processEvents()

        refreshed_codec_items = [
            window.results_view.filter_video_codec_combo.itemText(index)
            for index in range(window.results_view.filter_video_codec_combo.count())
        ]
        refreshed_extension_items = [
            window.results_view.filter_extension_combo.itemText(index)
            for index in range(window.results_view.filter_extension_combo.count())
        ]
        assert refreshed_codec_items == ["Any", "av1"]
        assert refreshed_extension_items == ["Any", "mp4", "webm"]
        assert window.results_view.filter_video_codec_combo.currentText() == "Any"
        assert window.results_view.filter_extension_combo.currentText() == "webm"
        assert window.results_view.results_table.rowCount() == 2

        replacement_groups_reset_selection = [
            DuplicateGroup(
                scan_id=1,
                profile="balanced",
                created_at="now",
                items=[
                    _dup_item(
                        51,
                        str(tmp_path / "refresh2" / "refresh2_av1.mp4"),
                        1920,
                        1080,
                        2_500_000,
                        0.97,
                        size=18 * 1024 * 1024,
                        duration_s=120.0,
                        codec="av1",
                    ),
                    _dup_item(
                        52,
                        str(tmp_path / "refresh2" / "refresh2_av1_copy.mp4"),
                        1920,
                        1080,
                        2_400_000,
                        0.965,
                        size=17 * 1024 * 1024,
                        duration_s=118.0,
                        codec="av1",
                    ),
                ],
                total_size_bytes=(18 + 17) * 1024 * 1024,
                group_id=64,
            )
        ]
        window.results_view.load_groups(replacement_groups_reset_selection)
        app.processEvents()

        reset_extension_items = [
            window.results_view.filter_extension_combo.itemText(index)
            for index in range(window.results_view.filter_extension_combo.count())
        ]
        assert reset_extension_items == ["Any", "mp4"]
        assert window.results_view.filter_extension_combo.currentText() == "Any"
        assert window.results_view.results_table.rowCount() == 2
        window.close()


def test_results_filters_must_match_all_and_hide_singletons(tmp_path: Path) -> None:
    """Hide groups that do not keep at least two visible files after filtering."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(_build_results_filter_groups(tmp_path))
        app.processEvents()

        window.results_view.filter_include_path_edit.setText("alpha_keep_big")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        window.results_view.filter_include_match_all_checkbox.setChecked(True)
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 0

        window.results_view.clear_filters_button.click()
        app.processEvents()
        window.results_view.filter_include_path_edit.setText("beta")
        window.results_view.filter_include_match_all_checkbox.setChecked(True)
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 3
        assert all(
            "beta"
            in window.results_view.results_table.item(row, COL_FULL_PATH).text().lower()
            for row in range(window.results_view.results_table.rowCount())
        )
        window.close()


def test_results_advanced_min_size_keeps_group_until_must_match_all(
    tmp_path: Path,
) -> None:
    """Keep a mixed-size group visible until all visible files must qualify."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(
            [
                DuplicateGroup(
                    scan_id=1,
                    profile="balanced",
                    created_at="now",
                    items=[
                        _dup_item(
                            61,
                            str(tmp_path / "sizes" / "big_match.mp4"),
                            1920,
                            1080,
                            2_000_000,
                            0.99,
                            size=80 * 1024 * 1024,
                        ),
                        _dup_item(
                            62,
                            str(tmp_path / "sizes" / "small_miss.mkv"),
                            1920,
                            1080,
                            1_900_000,
                            0.98,
                            size=5 * 1024 * 1024,
                        ),
                    ],
                    total_size_bytes=85 * 1024 * 1024,
                    group_id=65,
                )
            ]
        )
        app.processEvents()

        window.results_view.filter_min_size_spin.setValue(20.0)
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        window.results_view.filter_include_match_all_checkbox.setChecked(True)
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 0
        window.close()


def test_results_extension_filter_respects_must_match_all(tmp_path: Path) -> None:
    """Extension-only filtering follows the Must match all checkbox rule."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(
            [
                DuplicateGroup(
                    scan_id=1,
                    profile="balanced",
                    created_at="now",
                    items=[
                        _dup_item(
                            71,
                            str(tmp_path / "ext" / "match.mp4"),
                            1920,
                            1080,
                            2_000_000,
                            0.99,
                        ),
                        _dup_item(
                            72,
                            str(tmp_path / "ext" / "other.mkv"),
                            1920,
                            1080,
                            1_900_000,
                            0.98,
                        ),
                    ],
                    total_size_bytes=200,
                    group_id=66,
                )
            ]
        )
        app.processEvents()

        window.results_view.filter_extension_combo.setCurrentText("mp4")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        window.results_view.filter_include_match_all_checkbox.setChecked(True)
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 0
        window.close()


def test_results_combined_include_filters_require_one_file_to_match_all_conditions(
    tmp_path: Path,
) -> None:
    """Do not qualify a group when different files satisfy different filters."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(
            [
                DuplicateGroup(
                    scan_id=1,
                    profile="balanced",
                    created_at="now",
                    items=[
                        _dup_item(
                            81,
                            str(tmp_path / "combo" / "codec_match.mkv"),
                            1920,
                            1080,
                            2_000_000,
                            0.99,
                            codec="hevc",
                        ),
                        _dup_item(
                            82,
                            str(tmp_path / "combo" / "ext_match.webm"),
                            1920,
                            1080,
                            1_900_000,
                            0.98,
                            codec="vp9",
                        ),
                    ],
                    total_size_bytes=200,
                    group_id=67,
                )
            ]
        )
        app.processEvents()

        window.results_view.filter_video_codec_combo.setCurrentText("hevc")
        window.results_view.filter_extension_combo.setCurrentText("webm")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 0
        window.close()


def test_results_attribute_filters_and_include_text_share_one_item_predicate(
    tmp_path: Path,
) -> None:
    """Require one file to satisfy both text and attribute includes together."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(
            [
                DuplicateGroup(
                    scan_id=1,
                    profile="balanced",
                    created_at="now",
                    items=[
                        _dup_item(
                            91,
                            str(tmp_path / "core" / "feature_cut_h264.mp4"),
                            1920,
                            1080,
                            4_500_000,
                            0.991,
                            size=25 * 1024 * 1024,
                            duration_s=180.0,
                            codec=" h264 ",
                            hdr_format="",
                        ),
                        _dup_item(
                            92,
                            str(tmp_path / "core" / "feature_cut_hevc_hdr.mkv"),
                            3840,
                            2160,
                            8_200_000,
                            0.997,
                            size=80 * 1024 * 1024,
                            duration_s=240.0,
                            codec="hevc",
                            hdr_format="HDR10",
                        ),
                    ],
                    total_size_bytes=(25 + 80) * 1024 * 1024,
                    group_id=68,
                ),
                DuplicateGroup(
                    scan_id=1,
                    profile="balanced",
                    created_at="now",
                    items=[
                        _dup_item(
                            93,
                            str(tmp_path / "extras" / "extras_vp9_low.webm"),
                            1280,
                            720,
                            1_700_000,
                            0.945,
                            size=12 * 1024 * 1024,
                            duration_s=95.0,
                            codec="vp9",
                            hdr_format="",
                        ),
                        _dup_item(
                            94,
                            str(tmp_path / "extras" / "extras_h264_short.mp4"),
                            854,
                            480,
                            900_000,
                            0.905,
                            size=6 * 1024 * 1024,
                            duration_s=40.0,
                            codec="h264",
                            hdr_format="",
                        ),
                    ],
                    total_size_bytes=(12 + 6) * 1024 * 1024,
                    group_id=69,
                ),
            ]
        )
        app.processEvents()

        expected_core_paths = sorted(
            [
                str(tmp_path / "core" / "feature_cut_h264.mp4"),
                str(tmp_path / "core" / "feature_cut_hevc_hdr.mkv"),
            ]
        )

        window.results_view.filter_include_path_edit.setText("core")
        window.results_view.filter_video_codec_combo.setCurrentText("h264")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert sorted(_visible_result_paths(window)) == expected_core_paths

        window.results_view.filter_include_match_all_checkbox.setChecked(True)
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 0

        window.results_view.clear_filters_button.click()
        app.processEvents()
        window.results_view.filter_include_path_edit.setText("core")
        window.results_view.filter_extension_combo.setCurrentText("mkv")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert sorted(_visible_result_paths(window)) == expected_core_paths

        window.results_view.filter_include_match_all_checkbox.setChecked(True)
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 0

        window.results_view.clear_filters_button.click()
        app.processEvents()
        window.results_view.filter_include_path_edit.setText("core")
        window.results_view.filter_hdr_combo.setCurrentText("HDR only")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert sorted(_visible_result_paths(window)) == expected_core_paths

        window.results_view.filter_include_match_all_checkbox.setChecked(True)
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 0

        window.results_view.clear_filters_button.click()
        app.processEvents()
        window.results_view.filter_include_name_edit.setText("hdr")
        window.results_view.filter_video_codec_combo.setCurrentText("h264")
        window.results_view._apply_filter_inputs()
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 0
        window.close()


def test_results_summary_tooltip_reports_visible_and_loaded_stats(
    tmp_path: Path,
) -> None:
    """Show detailed duplicate stats for both visible and loaded results."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        window.results_view.load_groups(_build_results_filter_groups(tmp_path))
        app.processEvents()

        tooltip = window.results_view.info_label.toolTip()
        assert "Visible\nGroups: 3\nFiles: 7" in tooltip
        assert "Total size: 820.0 B" in tooltip
        assert "Potential save (max): 690.0 B" in tooltip
        assert "Potential save (min): 210.0 B" in tooltip
        assert "Largest group: 3 files" in tooltip
        assert "Average files/group: 2.33" in tooltip
        assert "Median files/group: 2" in tooltip
        assert tooltip.count("Extra duplicates: 4") == 2

        window.results_view.filter_include_path_edit.setText("beta")
        window.results_view._apply_filter_inputs()
        app.processEvents()

        filtered_tooltip = window.results_view.info_label.toolTip()
        assert window.results_view.info_label.text() == "Loaded 1 groups / 3 files"
        assert "Visible\nGroups: 1\nFiles: 3" in filtered_tooltip
        assert "Total size: 240.0 B" in filtered_tooltip
        assert "Potential save (max): 170.0 B" in filtered_tooltip
        assert "Potential save (min): 150.0 B" in filtered_tooltip
        assert "Average files/group: 3" in filtered_tooltip
        assert "Median files/group: 3" in filtered_tooltip
        assert "Extra duplicates: 2" in filtered_tooltip
        assert "Loaded\nGroups: 3\nFiles: 7" in filtered_tooltip
        assert "Total size: 820.0 B" in filtered_tooltip

        window.results_view.filter_include_path_edit.setText("zzz")
        window.results_view._apply_filter_inputs()
        app.processEvents()

        empty_visible_tooltip = window.results_view.info_label.toolTip()
        assert (
            window.results_view.info_label.text() == "No duplicate groups for this scan"
        )
        assert "Visible\nGroups: 0\nFiles: 0" in empty_visible_tooltip
        assert "Total size: 0.0 B" in empty_visible_tooltip
        assert "Potential save (max): 0.0 B" in empty_visible_tooltip
        assert "Potential save (min): 0.0 B" in empty_visible_tooltip
        assert "Largest group: 0 files" in empty_visible_tooltip
        assert "Average files/group: 0" in empty_visible_tooltip
        assert "Median files/group: 0" in empty_visible_tooltip
        assert "Extra duplicates: 0" in empty_visible_tooltip
        assert "Loaded\nGroups: 3\nFiles: 7" in empty_visible_tooltip
        window.close()


def test_results_summary_tooltip_empty_state(tmp_path: Path) -> None:
    """Expose a fallback tooltip when no duplicate stats exist yet."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert (
            window.results_view.info_label.toolTip() == "No duplicate stats available."
        )

        window.results_view.load_groups([])
        app.processEvents()

        assert (
            window.results_view.info_label.toolTip() == "No duplicate stats available."
        )
        window.close()


def test_results_actions_menu_shortcuts_and_row_double_click(
    tmp_path: Path, monkeypatch
) -> None:
    """Expose row actions in the menu and open rows on double click."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "alpha.mp4"
    target.write_bytes(b"")
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        group = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(21, str(target), 320, 240, 1000, 1.0),
                _dup_item(22, str(tmp_path / "alpha_copy.mp4"), 320, 240, 900, 0.98),
            ],
            total_size_bytes=200,
            group_id=55,
        )
        window.results_view.load_groups([group])
        window.results_view.results_table.setCurrentCell(0, 0)
        app.processEvents()

        actions_menu = _results_menu_actions(window)
        menu_actions = [action for action in actions_menu if not action.isSeparator()]
        assert window.open_current_file_action in menu_actions
        assert window.explore_current_file_action in menu_actions
        assert window.copy_full_path_action in menu_actions
        assert window.search_everything_action in menu_actions
        assert window.open_web_search_action in menu_actions
        assert window.launch_mediainfo_action in menu_actions
        assert window.custom_command_f2_action in menu_actions
        assert window.custom_command_f3_action in menu_actions
        assert window.custom_command_f4_action in menu_actions
        assert window.delete_selected_action in menu_actions
        assert window.delete_selected_recycle_bin_action in menu_actions
        assert window.delete_selected_permanent_action in menu_actions
        assert window.delete_selected_action.text() == "&Soft Delete Selected"
        assert (
            window.delete_selected_recycle_bin_action.text() == "Delete to &Recycle Bin"
        )
        assert window.delete_selected_permanent_action.text() == "&Permanently Delete"

        assert {
            sequence.toString()
            for sequence in window.open_current_file_action.shortcuts()
        } == {"Return", "Enter"}
        assert {
            sequence.toString()
            for sequence in window.explore_current_file_action.shortcuts()
        } == {"E"}
        assert {
            sequence.toString() for sequence in window.copy_full_path_action.shortcuts()
        } == {"C"}
        assert {
            sequence.toString()
            for sequence in window.search_everything_action.shortcuts()
        } == {"S"}
        assert {
            sequence.toString()
            for sequence in window.open_web_search_action.shortcuts()
        } == {"G"}
        assert {
            sequence.toString()
            for sequence in window.launch_mediainfo_action.shortcuts()
        } == {"M"}
        assert {
            sequence.toString()
            for sequence in window.custom_command_f2_action.shortcuts()
        } == {"F2"}
        assert {
            sequence.toString()
            for sequence in window.custom_command_f3_action.shortcuts()
        } == {"F3"}
        assert {
            sequence.toString()
            for sequence in window.custom_command_f4_action.shortcuts()
        } == {"F4"}
        assert {
            sequence.toString()
            for sequence in window.delete_selected_action.shortcuts()
        } == {"Del"}
        assert {
            sequence.toString()
            for sequence in window.delete_selected_recycle_bin_action.shortcuts()
        } == {"Shift+Del"}
        assert {
            sequence.toString()
            for sequence in window.delete_selected_permanent_action.shortcuts()
        } == {"Ctrl+Shift+Del"}

        opened: list[Path] = []
        explored: list[Path] = []
        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.open_path_in_default_app",
            lambda path: opened.append(Path(path)) or True,
        )
        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.reveal_path_in_file_manager",
            lambda path: explored.append(Path(path)) or True,
        )

        window.results_view.results_table.itemDoubleClicked.emit(
            window.results_view.results_table.item(0, COL_FILE_NAME)
        )
        window.results_view.results_table.itemDoubleClicked.emit(
            window.results_view.results_table.item(0, COL_PARENT_DIR)
        )
        window.results_view.results_table.itemDoubleClicked.emit(
            window.results_view.results_table.item(0, COL_FULL_PATH)
        )
        app.processEvents()

        assert opened == [target]
        assert explored == [target, target]
        window.close()


def test_results_table_keyboard_navigation_and_extra_actions(
    tmp_path: Path, monkeypatch
) -> None:
    """Support table-space toggles, group tabbing, and extra row actions."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    target_a = tmp_path / "alpha.mp4"
    target_b = tmp_path / "beta.mkv"
    target_a.write_bytes(b"")
    target_b.write_bytes(b"")
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        settings.everything_exe_path = r"C:\tools\Everything.exe"
        settings.custom_command_f2 = '"C:\\Tools\\Runner F2.exe" --first'
        settings.custom_command_f3 = '"C:\\Tools\\Runner F3.exe"'
        settings.custom_command_f4 = '"C:\\Tools\\Runner F4.exe" --tail'
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        group_a = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(31, str(target_a), 320, 240, 1000, 1.0),
                _dup_item(32, str(tmp_path / "alpha_copy.mp4"), 320, 240, 900, 0.98),
            ],
            total_size_bytes=200,
            group_id=56,
        )
        group_b = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(33, str(target_b), 320, 240, 800, 0.97),
                _dup_item(34, str(tmp_path / "beta_copy.mkv"), 320, 240, 780, 0.96),
            ],
            total_size_bytes=200,
            group_id=57,
        )
        window.results_view.load_groups([group_a, group_b])
        window.results_view.results_table.setCurrentCell(0, COL_FILE_NAME)
        window.results_view.results_table.setFocus()
        app.processEvents()

        check_item = window.results_view.results_table.item(0, COL_CHECK)
        assert check_item is not None
        assert check_item.checkState() == Qt.CheckState.Unchecked

        QTest.keyClick(window.results_view.results_table, Qt.Key.Key_Space)
        app.processEvents()
        assert check_item.checkState() == Qt.CheckState.Checked

        QTest.keyClick(window.results_view.results_table, Qt.Key.Key_Tab)
        app.processEvents()
        assert window.results_view.results_table.currentRow() == 2

        QTest.keyClick(
            window.results_view.results_table,
            Qt.Key.Key_Backtab,
            Qt.KeyboardModifier.ShiftModifier,
        )
        app.processEvents()
        assert window.results_view.results_table.currentRow() == 0

        launched_commands: list[list[str]] = []
        opened_urls: list[str] = []

        def _resolve_executable_path(
            tool_name: str,
            override_path: str = "",
            *,
            not_found_message: str,
            fallback_paths: tuple[str, ...] = (),
        ) -> str:
            _ = not_found_message, fallback_paths
            return str(override_path or tool_name)

        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.resolve_executable_path",
            _resolve_executable_path,
        )
        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.subprocess.Popen",
            lambda command: launched_commands.append(list(command)),
        )
        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.webbrowser.open",
            lambda url: opened_urls.append(str(url)) or True,
        )

        window.results_view.copy_full_path_action.trigger()
        assert QApplication.clipboard().text() == str(target_a)

        window.results_view.search_everything_action.trigger()
        window.results_view.open_web_search_action.trigger()
        window.results_view.custom_command_f2_action.trigger()
        window.results_view.custom_command_f3_action.trigger()
        window.results_view.custom_command_f4_action.trigger()

        assert launched_commands == [
            [r"C:\tools\Everything.exe", "-search", "alpha.mp4"],
            [
                r"C:\Tools\Runner F2.exe",
                "--first",
                str(target_a),
                str(target_a.parent),
            ],
            [
                r"C:\Tools\Runner F3.exe",
                str(target_a),
                str(target_a.parent),
            ],
            [
                r"C:\Tools\Runner F4.exe",
                "--tail",
                str(target_a),
                str(target_a.parent),
            ],
        ]
        assert opened_urls == ["https://www.google.com/search?q=alpha"]
        window.close()


def test_results_delete_actions_emit_expected_modes(tmp_path: Path) -> None:
    """Map the three delete actions to the expected request modes."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        group = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(41, str(tmp_path / "delete_me.mp4"), 320, 240, 1000, 1.0),
                _dup_item(
                    42,
                    str(tmp_path / "delete_me_copy.mp4"),
                    320,
                    240,
                    900,
                    0.98,
                ),
            ],
            total_size_bytes=200,
            group_id=77,
        )
        window.results_view.load_groups([group])
        window.results_view.results_table.setCurrentCell(0, COL_FILE_NAME)
        window.results_view.results_table.setFocus()
        app.processEvents()

        emitted: list[tuple[str, int]] = []
        window.results_view.delete_requested.disconnect(window._handle_delete_requested)
        window.results_view.delete_requested.connect(
            lambda mode, targets: emitted.append((str(mode), len(list(targets))))
        )

        window.results_view.delete_selected_action.trigger()
        window.results_view.delete_selected_recycle_bin_action.trigger()
        window.results_view.delete_selected_permanent_action.trigger()
        app.processEvents()

        assert emitted == [
            ("rename", 1),
            ("recycle_bin", 1),
            ("permanent", 1),
        ]
        window.close()


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        ("rename", "Processed 1 file(s)."),
        ("recycle_bin", "Processed 1 file(s)."),
        ("permanent", "Processed 1 file(s)."),
    ],
)
def test_handle_delete_requested_supports_all_delete_modes_without_popups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_status: str,
) -> None:
    """Execute each delete mode and report only through the status bar."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        file_a = tmp_path / f"{mode}_a.mp4"
        file_b = tmp_path / f"{mode}_b.mp4"
        _, targets = _load_delete_test_group(
            window,
            db,
            file_paths=[file_a, file_b],
        )

        warnings: list[tuple[str, str]] = []
        recycle_bin_calls: list[str] = []
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.QMessageBox.warning",
            lambda _parent, title, text: warnings.append((str(title), str(text))),
        )
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.send2trash",
            lambda path: recycle_bin_calls.append(str(path)),
        )

        window._handle_delete_requested(mode, [targets[0]])
        app.processEvents()

        assert warnings == []
        assert window.statusBar().currentMessage() == expected_status
        assert window.results_view.results_table.rowCount() == 0

        if mode == "rename":
            assert not file_a.exists()
            assert (tmp_path / f"{file_a.name}.z_dele").exists()
            assert db.fetch_file_path(int(targets[0]["file_id"])) == str(
                tmp_path / f"{file_a.name}.z_dele"
            )
            assert recycle_bin_calls == []
        elif mode == "recycle_bin":
            assert recycle_bin_calls == [str(file_a)]
            assert file_a.exists()
        else:
            assert recycle_bin_calls == []
            assert not file_a.exists()

        window.close()


def test_handle_delete_requested_partial_failure_uses_status_bar_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep delete failures non-blocking and avoid warning popups."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        file_a = tmp_path / "partial_ok.mp4"
        file_b = tmp_path / "partial_missing.mp4"
        _, targets = _load_delete_test_group(
            window,
            db,
            file_paths=[file_a, file_b],
        )
        file_b.unlink()

        warnings: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.QMessageBox.warning",
            lambda _parent, title, text: warnings.append((str(title), str(text))),
        )

        window._handle_delete_requested("rename", targets)
        app.processEvents()

        assert warnings == []
        assert window.statusBar().currentMessage() == (
            "Processed 1 file(s) with 1 error(s)."
        )
        assert not file_a.exists()
        assert (tmp_path / f"{file_a.name}.z_dele").exists()
        assert window.results_view.results_table.rowCount() == 0
        window.close()


def test_saved_scan_profiles_save_load_and_delete(tmp_path: Path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        roots = [str(tmp_path / "library")]
        scan_id = db.create_scan(
            profile="custom",
            roots=roots,
            extensions=["mp4"],
            custom_similarity_threshold=0.23,
            scene_aware_sampling=True,
            audio_fingerprint_enabled=True,
            cross_resolution_mode="same_aspect",
        )
        file_a = db.upsert_file(
            path=str(tmp_path / "library" / "a.mp4"),
            size=111,
            mtime_ns=1,
            ctime_ns=1,
            ext="mp4",
            scan_id=scan_id,
        )
        file_b = db.upsert_file(
            path=str(tmp_path / "library" / "b.mp4"),
            size=112,
            mtime_ns=2,
            ctime_ns=2,
            ext="mp4",
            scan_id=scan_id,
        )
        db.save_video_meta(
            file_a,
            VideoMeta(
                duration_s=10.0,
                width=1920,
                height=1080,
                fps=30.0,
                codec="h264",
                bitrate=1000,
                audio_stream_count=1,
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="eng",
                hdr_format="",
            ),
        )
        db.save_video_meta(
            file_b,
            VideoMeta(
                duration_s=10.0,
                width=1920,
                height=1080,
                fps=30.0,
                codec="h264",
                bitrate=900,
                audio_stream_count=1,
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="eng",
                hdr_format="",
            ),
        )
        group_id = db.insert_duplicate_group(
            scan_id=scan_id, profile="balanced", total_size_bytes=223
        )
        db.insert_duplicate_item(
            group_id,
            _dup_item(
                file_a,
                str(tmp_path / "library" / "a.mp4"),
                1920,
                1080,
                1000,
                1.0,
                size=111,
            ),
        )
        db.insert_duplicate_item(
            group_id,
            _dup_item(
                file_b,
                str(tmp_path / "library" / "b.mp4"),
                1920,
                1080,
                900,
                0.99,
                size=112,
            ),
        )
        db.complete_scan(scan_id, status="done")

        settings = default_settings()
        settings.scan_roots = roots
        settings.extensions = ["mp4"]
        settings.similarity_profile = "custom"
        settings.custom_similarity_threshold = 0.23
        settings.scene_aware_sampling = True
        settings.audio_fingerprint_enabled = True
        settings.cross_resolution_mode = "same_aspect"
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert window.save_scan_set_btn.text() == "Save Scan Set"
        assert window.load_saved_scan_btn.text() == "Load Saved Scan"
        window.profile_combo.setCurrentText("custom")
        window.custom_similarity_threshold_spin.setValue(0.23)
        window.scene_aware_sampling_check.setChecked(True)
        window.audio_fingerprint_enabled_check.setChecked(True)
        for index in range(window.cross_resolution_mode_combo.count()):
            if str(window.cross_resolution_mode_combo.itemData(index)) == "same_aspect":
                window.cross_resolution_mode_combo.setCurrentIndex(index)
                break

        monkeypatch.setattr(
            "video_duperz.ui.main_window_profiles.QInputDialog.getText",
            lambda *a, **k: ("My Library", True),
        )
        window._save_current_scan_set_as()
        app.processEvents()
        assert "My Library" in window._saved_scan_profiles

        payload = window._saved_scan_profiles["My Library"]
        assert payload.similarity_profile == "custom"
        assert payload.custom_similarity_threshold == pytest.approx(0.23)
        assert payload.scene_aware_sampling is True
        assert payload.audio_fingerprint_enabled is True
        assert payload.cross_resolution_mode == "same_aspect"
        window._load_saved_scan_profile(payload, "My Library")
        app.processEvents()
        assert window.current_scan_id == scan_id
        assert window.tabs.currentWidget() == window.results_view
        assert window.results_view.results_table.rowCount() == 2
        roots_in_widget = [
            window.roots_list.item(i).text() for i in range(window.roots_list.count())
        ]
        assert roots_in_widget == roots
        assert window.profile_combo.currentText() == "custom"
        assert window.custom_similarity_threshold_spin.value() == pytest.approx(0.23)
        assert window.scene_aware_sampling_check.isChecked() is True
        assert window.audio_fingerprint_enabled_check.isChecked() is True
        assert str(window.cross_resolution_mode_combo.currentData()) == "same_aspect"
        assert window.extensions_edit.text() == "mp4"
        assert (
            "filesystem may have changed"
            in window.results_view.info_label.text().lower()
        )

        window._refresh_saved_scans_menu()
        action_texts = [action.text() for action in window._saved_scans_menu.actions()]
        assert any(text.startswith("My Library | #") for text in action_texts)
        assert any(
            text.startswith("My Library | #") and text.endswith("| done")
            for text in action_texts
        )

        monkeypatch.setattr(
            "video_duperz.ui.main_window_profiles.QInputDialog.getItem",
            lambda *a, **k: ("My Library", True),
        )
        monkeypatch.setattr(
            "video_duperz.ui.main_window_profiles.QMessageBox.question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        window._delete_named_scan_profile()
        app.processEvents()
        assert "My Library" not in window._saved_scan_profiles

        window._refresh_saved_scans_menu()
        action_texts = [action.text() for action in window._saved_scans_menu.actions()]
        assert any(text.startswith("Auto:") for text in action_texts)
        window.close()


def test_load_saved_scan_profile_not_started_restores_sources_and_clears_results(
    tmp_path: Path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path / "old_root")]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        stale_group = DuplicateGroup(
            scan_id=999,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(11, str(tmp_path / "stale_a.mp4"), 320, 240, 1000, 1.0),
                _dup_item(12, str(tmp_path / "stale_b.mp4"), 320, 240, 900, 0.99),
            ],
            total_size_bytes=200,
            group_id=77,
        )
        window.current_scan_id = 999
        window.results_view.load_groups([stale_group])
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        pending_roots = [str(tmp_path / "pending_root")]
        payload = SavedScanProfilePayload(
            scan_set_key="",
            roots=pending_roots,
            similarity_profile="aggressive",
            extensions=["mkv", ".mp4"],
        )
        window._load_saved_scan_profile(payload, "Pending Profile")
        app.processEvents()

        assert window.tabs.currentWidget() == window.sources_tab
        assert window.current_scan_id is None
        assert window.results_view.results_table.rowCount() == 0
        roots_in_widget = [
            window.roots_list.item(i).text() for i in range(window.roots_list.count())
        ]
        assert roots_in_widget == pending_roots
        assert window.profile_combo.currentText() == "aggressive"
        assert window.extensions_edit.text() == "mkv, mp4"
        assert "not started" in window.statusBar().currentMessage().lower()
        window.close()


def test_load_saved_scan_profile_cancelled_latest_routes_to_sources(
    tmp_path: Path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        roots = [str(tmp_path / "library")]
        done_id = db.create_scan(profile="balanced", roots=roots, extensions=["mp4"])
        db.complete_scan(done_id, status="done")
        cancelled_id = db.create_scan(
            profile="balanced", roots=roots, extensions=["mp4"]
        )
        db.complete_scan(cancelled_id, status="cancelled")

        settings = default_settings()
        settings.scan_roots = [str(tmp_path / "old_root")]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        stale_group = DuplicateGroup(
            scan_id=done_id,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(21, str(tmp_path / "old_a.mp4"), 320, 240, 1000, 1.0),
                _dup_item(22, str(tmp_path / "old_b.mp4"), 320, 240, 900, 0.99),
            ],
            total_size_bytes=200,
            group_id=88,
        )
        window.current_scan_id = done_id
        window.results_view.load_groups([stale_group])
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        payload = SavedScanProfilePayload(
            scan_set_key="",
            roots=roots,
            similarity_profile="balanced",
            extensions=["mp4"],
        )
        window._load_saved_scan_profile(payload, "Cancelled Profile")
        app.processEvents()

        assert window.tabs.currentWidget() == window.sources_tab
        assert window.current_scan_id is None
        assert window.results_view.results_table.rowCount() == 0
        assert "cancelled" in window.statusBar().currentMessage().lower()
        assert f"#{cancelled_id}" in window.statusBar().currentMessage()
        window.close()


def test_load_saved_scan_profile_paused_loads_scan_tab_and_issues(
    tmp_path: Path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        roots = [str(tmp_path / "library")]
        paused_id = db.create_scan(
            profile="custom",
            roots=roots,
            extensions=["mp4"],
            custom_similarity_threshold=0.24,
            scene_aware_sampling=True,
            audio_fingerprint_enabled=True,
            cross_resolution_mode="same_aspect",
            probe_backend="ffprobe",
        )
        db.insert_scan_issue(
            paused_id,
            ScanIssue(
                stage="probe",
                path=str(tmp_path / "library" / "clip.mp4"),
                message="bad metadata",
            ),
        )
        db.upsert_failed_file(
            paused_id,
            ScanIssue(
                stage="probe",
                path=str(tmp_path / "library" / "clip.mp4"),
                message="bad metadata",
            ),
        )
        db.complete_scan(paused_id, status="paused")

        settings = default_settings()
        settings.scan_roots = [str(tmp_path / "old_root")]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        payload = SavedScanProfilePayload(
            scan_set_key="",
            roots=roots,
            similarity_profile="custom",
            extensions=["mp4"],
            custom_similarity_threshold=0.24,
            scene_aware_sampling=True,
            audio_fingerprint_enabled=True,
            cross_resolution_mode="same_aspect",
        )
        window._load_saved_scan_profile(payload, "Paused Profile")
        app.processEvents()

        assert window.tabs.currentWidget() == window.scan_view
        assert window._loaded_paused_scan_id == paused_id
        assert window.current_scan_id is None
        assert window.scan_view.resume_btn.isEnabled()
        assert not window.scan_view.start_btn.isEnabled()
        assert window.scan_view.retry_failed_checkbox.isVisible()
        assert window.scan_view.retry_failed_checkbox.isEnabled()
        assert window.scan_view.retry_failed_checkbox.isChecked()
        assert window.scan_view.retry_failed_checkbox.text().endswith("(1)")
        assert window.scan_view.issues_table.rowCount() == 1
        assert "paused" in window.statusBar().currentMessage().lower()
        assert window.probe_backend_combo.currentText() == "ffprobe"
        window.close()


def test_resume_scan_passes_retry_failed_checkbox_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        roots = [str(tmp_path / "library")]
        paused_id = db.create_scan(
            profile="balanced",
            roots=roots,
            extensions=["mp4"],
            probe_backend="ffprobe",
        )
        db.upsert_failed_file(
            paused_id,
            ScanIssue(
                stage="fingerprint",
                path=str(tmp_path / "library" / "clip.mp4"),
                message="decoder timeout",
            ),
        )
        db.complete_scan(paused_id, status="paused")

        settings = default_settings()
        settings.scan_roots = roots
        settings.extensions = ["mp4"]
        settings.probe_backend = "ffprobe"
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        scan_info = db.get_scan_info(paused_id)
        window._load_paused_scan(
            scan_id=paused_id,
            source_name="Paused Profile",
            scan_info=scan_info,
        )
        app.processEvents()
        assert window.profile_combo.currentText() == "balanced"
        assert window.custom_similarity_threshold_spin.value() == pytest.approx(0.18)
        assert window.scene_aware_sampling_check.isChecked() is False
        assert window.audio_fingerprint_enabled_check.isChecked() is False
        assert str(window.cross_resolution_mode_combo.currentData()) == "off"
        window.scan_view.retry_failed_checkbox.setChecked(False)

        captured: dict[str, object] = {}

        def _capture_launch(**kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(window, "_launch_scan", _capture_launch)

        window._resume_scan()

        assert captured["resume_scan_id"] == paused_id
        assert captured["retry_failed_files"] is False
        window.close()


def test_resume_launch_preserves_unchecked_retry_failed_checkbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        roots = [str(tmp_path / "library")]
        paused_id = db.create_scan(
            profile="balanced",
            roots=roots,
            extensions=["mp4"],
            probe_backend="ffprobe",
        )
        db.upsert_failed_file(
            paused_id,
            ScanIssue(
                stage="fingerprint",
                path=str(tmp_path / "library" / "clip.mp4"),
                message="decoder timeout",
            ),
        )
        db.complete_scan(paused_id, status="paused")

        settings = default_settings()
        settings.scan_roots = roots
        settings.extensions = ["mp4"]
        settings.probe_backend = "ffprobe"
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        scan_info = db.get_scan_info(paused_id)
        window._load_paused_scan(
            scan_id=paused_id,
            source_name="Paused Profile",
            scan_info=scan_info,
        )
        app.processEvents()
        window.scan_view.retry_failed_checkbox.setChecked(False)

        class _DummySignal:
            def connect(self, _callback: object) -> None:
                return None

        class _DummySignals:
            def __init__(self) -> None:
                self.progress = _DummySignal()
                self.issue = _DummySignal()
                self.finished = _DummySignal()
                self.error = _DummySignal()

        class _DummyScanWorker:
            def __init__(self, **_kwargs: object) -> None:
                self.signals = _DummySignals()

        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.ScanWorker",
            _DummyScanWorker,
        )
        monkeypatch.setattr(window.thread_pool, "start", lambda _worker: None)
        monkeypatch.setattr(window, "_apply_scan_parent_priority", lambda: None)

        window._resume_scan()
        app.processEvents()

        assert window.scan_view.retry_failed_checkbox.isVisible()
        assert not window.scan_view.retry_failed_checkbox.isChecked()
        window.close()


def test_saved_scans_menu_lists_not_started_named_and_cancelled_auto(
    tmp_path: Path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        auto_roots = [str(tmp_path / "auto_library")]
        db.complete_scan(
            db.create_scan(profile="balanced", roots=auto_roots, extensions=["mp4"]),
            status="cancelled",
        )

        settings = default_settings()
        settings.scan_roots = [str(tmp_path / "root")]
        settings.saved_scan_profiles = {
            "Pending Named": SavedScanProfilePayload(
                scan_set_key="",
                roots=[str(tmp_path / "named_library")],
                similarity_profile="balanced",
                extensions=["mp4"],
            )
        }
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window._refresh_saved_scans_menu()
        actions = window._saved_scans_menu.actions()
        action_texts = [action.text() for action in actions]

        pending_action = next(
            action for action in actions if action.text().startswith("Pending Named | ")
        )
        assert pending_action.isEnabled()
        assert pending_action.text().endswith("not started")
        assert any(
            text.startswith("Auto:") and text.endswith("| cancelled")
            for text in action_texts
        )
        window.close()


def test_file_tools_help_menu_actions(tmp_path: Path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        settings.recent_scan_roots = ["D:/Videos"]
        settings.saved_scan_profiles = {
            "Demo": SavedScanProfilePayload(
                scan_set_key='{"extensions":["mp4"],"roots":["d:/videos"],"similarity_profile":"balanced"}',
                roots=["D:/Videos"],
                similarity_profile="balanced",
                extensions=["mp4"],
            )
        }
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        menu_titles = [
            action.text().replace("&", "") for action in window.menuBar().actions()
        ]
        assert menu_titles[:6] == ["File", "View", "Sort", "Actions", "Tools", "Help"]

        assert window.clear_recent_folders_action.text() == "C&lear Recent Folders"
        assert window.clear_saved_scans_action.text() == "Clear Sa&ved Scans"
        assert (
            window.clear_cached_thumbnails_action.text() == "Clear Cached T&humbnails"
        )
        assert window.full_reset_action.text() == "&Full Reset"
        assert window.edit_ini_action.text() == "Edit &.ini File"
        assert window.about_action.text() == "&Help"
        shortcuts = {seq.toString() for seq in window.exit_action.shortcuts()}
        assert {"Ctrl+Q", "Alt+X"} <= shortcuts
        tools_menu_action = next(
            action
            for action in window.menuBar().actions()
            if action.text().replace("&", "") == "Tools"
        )
        tools_menu = tools_menu_action.menu()
        assert tools_menu is not None
        tools_actions = [action.text() for action in tools_menu.actions()]
        assert "Edit &.ini File" in tools_actions
        assert "List physical drives" not in tools_actions

        cache_file = thumbnail_cache_dir() / "dummy.jpg"
        cache_file.write_bytes(b"123")
        assert cache_file.exists()
        window._clear_cached_thumbnails()
        assert not cache_file.exists()

        window._clear_recent_roots()
        app.processEvents()
        assert window._recent_roots == []
        assert not window.add_recent_root_btn.isEnabled()

        scan_id = window.db.create_scan(
            profile="balanced", roots=[str(tmp_path)], extensions=["mp4"]
        )
        window.db.complete_scan(scan_id, status="done")
        monkeypatch.setattr(
            "video_duperz.ui.main_window_profiles.QMessageBox.question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        close_called = {"value": False}
        monkeypatch.setattr(
            window, "close", lambda: close_called.__setitem__("value", True)
        )
        window._request_full_reset()
        app.processEvents()
        assert close_called["value"] is True
        assert window.consume_full_reset_requested() is True

        monkeypatch.setattr(window, "close", lambda: None)
        window._clear_saved_scans()
        app.processEvents()
        assert window._saved_scan_profiles == {}
        assert window.db.latest_scan_id() is None
        window.close()


def test_sources_tab_drive_table_highlights_matches_and_tracks_parallel_total(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    def _fake_plan(
        roots: list[str],
        max_workers: int,
        drive_worker_overrides: dict[str, int] | None = None,
    ):
        _ = drive_worker_overrides
        matched = {"volume:a", "volume:b"} if roots else set()
        effective_workers = len(matched)
        return SimpleNamespace(
            matched_volume_identities=matched,
            requested_worker_target=max(max_workers, effective_workers),
            effective_total_workers=effective_workers,
        )

    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.list_physical_drives",
        lambda roots=None: [
            PhysicalDriveInfo(
                root="R:\\",
                volume_identity="volume:a",
                disk_tokens=["disk:0"],
                total_bytes=1_000,
                free_bytes=250,
                used_percent=75.0,
            ),
            PhysicalDriveInfo(
                root="S:\\",
                volume_identity="volume:b",
                disk_tokens=["disk:1"],
                total_bytes=2_000,
                free_bytes=1_000,
                used_percent=50.0,
            ),
            PhysicalDriveInfo(
                root="T:\\",
                volume_identity="volume:c",
                disk_tokens=["disk:2"],
                total_bytes=3_000,
                free_bytes=2_000,
                used_percent=33.3,
                lookup_error="disk extent lookup failed",
            ),
        ],
    )
    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.build_physical_drive_scan_plan",
        _fake_plan,
    )

    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = ["R:/Videos", "S:/Archive"]
        settings.max_workers = 1
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert window.sources_drive_table.rowCount() == 3
        headers = [
            window.sources_drive_table.horizontalHeaderItem(i).text()
            for i in range(window.sources_drive_table.columnCount())
        ]
        assert headers == [
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
        assert "Matched physical drives: 2" in window.sources_drive_summary_label.text()
        assert "Requested workers: 2" in window.sources_drive_summary_label.text()
        assert "Effective workers: 2" in window.sources_drive_summary_label.text()
        assert isinstance(window.sources_drive_table.cellWidget(0, 6), QSpinBox)
        assert isinstance(window.sources_drive_table.cellWidget(1, 6), QSpinBox)
        assert window.sources_drive_table.item(2, 6).text() == "-"
        assert window.sources_drive_table.item(0, 7).text() == "Yes"
        assert window.sources_drive_table.item(1, 7).text() == "Yes"
        assert window.sources_drive_table.item(2, 7).text() == "No"
        assert window.sources_drive_table.item(0, 0).font().bold() is True
        assert window.sources_drive_table.item(1, 0).font().bold() is True
        assert window.sources_drive_table.item(2, 0).font().bold() is False
        assert window.sources_drive_table.item(2, 2).text() == "volume:c"
        assert (
            window.sources_drive_table.item(2, 8).text() == "disk extent lookup failed"
        )

        window.max_workers_spin.setValue(3)
        app.processEvents()
        assert "Requested workers: 3" in window.sources_drive_summary_label.text()
        assert "Effective workers: 2" in window.sources_drive_summary_label.text()
        assert "Caps applied" in window.sources_drive_summary_label.text()
        window.close()


def test_sources_tab_drive_table_shows_placeholder_when_no_local_drives(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.list_physical_drives",
        lambda roots=None: [],
    )
    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.build_physical_drive_scan_plan",
        lambda roots, max_workers, drive_worker_overrides=None: SimpleNamespace(
            matched_volume_identities=set(),
            requested_worker_target=max_workers,
            effective_total_workers=0,
        ),
    )

    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = []
        settings.max_workers = 2
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert window.sources_drive_table.rowCount() == 1
        assert (
            window.sources_drive_table.item(0, 0).text() == "(No local drives detected)"
        )
        assert window.sources_drive_table.item(0, 8).text() == ""
        assert "Matched physical drives: 0" in window.sources_drive_summary_label.text()
        assert "Requested workers: 2" in window.sources_drive_summary_label.text()
        assert "Effective workers: 0" in window.sources_drive_summary_label.text()
        window.close()


def test_sources_tab_drive_workers_and_probe_mode_persist_across_reload(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.list_physical_drives",
        lambda roots=None: [
            PhysicalDriveInfo(
                root="R:\\",
                volume_identity="volume:a",
                disk_tokens=["disk:0"],
                total_bytes=1_000,
                free_bytes=250,
                used_percent=75.0,
            ),
            PhysicalDriveInfo(
                root="S:\\",
                volume_identity="volume:b",
                disk_tokens=["disk:1"],
                total_bytes=2_000,
                free_bytes=1_000,
                used_percent=50.0,
            ),
        ],
    )

    def _fake_plan(
        roots: list[str],
        max_workers: int,
        drive_worker_overrides: dict[str, int] | None = None,
    ):
        matched = {"volume:a", "volume:b"} if roots else set()
        overrides = drive_worker_overrides or {}
        effective = 0
        if roots:
            effective = max(1, int(overrides.get("volume:a", 1))) + max(
                1, int(overrides.get("volume:b", 1))
            )
        return SimpleNamespace(
            matched_volume_identities=matched,
            requested_worker_target=max(max_workers, effective),
            effective_total_workers=effective,
        )

    monkeypatch.setattr(
        "video_duperz.ui.main_window_profiles.build_physical_drive_scan_plan",
        _fake_plan,
    )

    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = ["R:/Videos", "S:/Archive"]
        settings.max_workers = 2
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        spin_a = window.sources_drive_table.cellWidget(0, 6)
        spin_b = window.sources_drive_table.cellWidget(1, 6)
        assert isinstance(spin_a, QSpinBox)
        assert isinstance(spin_b, QSpinBox)
        spin_a.setValue(4)
        spin_b.setValue(2)
        window.probe_backend_combo.setCurrentText("pyav")
        window.probe_mode_combo.setCurrentText("burst")
        window.scan_parent_cpu_priority_combo.setCurrentIndex(1)
        window.scan_parent_io_mode_combo.setCurrentIndex(1)
        window.scan_child_cpu_priority_combo.setCurrentIndex(4)
        window.scan_child_io_mode_combo.setCurrentIndex(1)
        window.ffmpeg_exe_path_edit.setText(r"C:\tools\ffmpeg.exe")
        window.ffprobe_exe_path_edit.setText(r"C:\tools\ffprobe.exe")
        window.mediainfo_exe_path_edit.setText(r"C:\tools\mediainfo.exe")
        app.processEvents()

        assert "Requested workers: 6" in window.sources_drive_summary_label.text()
        assert "Effective workers: 6" in window.sources_drive_summary_label.text()
        window.close()

    loaded = load_settings()
    assert loaded.drive_worker_overrides == {"volume:a": 4, "volume:b": 2}
    assert loaded.probe_backend == "pyav"
    assert loaded.probe_worker_mode == "burst"
    assert loaded.scan_parent_cpu_priority == "below_normal"
    assert loaded.scan_parent_io_mode == "background"
    assert loaded.scan_child_cpu_priority == "high"
    assert loaded.scan_child_io_mode == "background"
    assert loaded.ffmpeg_exe_path == r"C:\tools\ffmpeg.exe"
    assert loaded.ffprobe_exe_path == r"C:\tools\ffprobe.exe"
    assert loaded.mediainfo_exe_path == r"C:\tools\mediainfo.exe"

    with Database(tmp_path / "app.db") as db:
        reloaded_window = MainWindow(db=db, settings=loaded)
        reloaded_window.show()
        app.processEvents()

        assert reloaded_window.probe_backend_combo.currentText() == "pyav"
        assert reloaded_window.probe_mode_combo.currentText() == "burst"
        assert str(reloaded_window.scan_parent_cpu_priority_combo.currentData()) == (
            "below_normal"
        )
        assert str(reloaded_window.scan_parent_io_mode_combo.currentData()) == (
            "background"
        )
        assert str(reloaded_window.scan_child_cpu_priority_combo.currentData()) == (
            "high"
        )
        assert str(reloaded_window.scan_child_io_mode_combo.currentData()) == (
            "background"
        )
        assert reloaded_window.ffmpeg_exe_path_edit.text() == r"C:\tools\ffmpeg.exe"
        assert reloaded_window.ffprobe_exe_path_edit.text() == r"C:\tools\ffprobe.exe"
        assert (
            reloaded_window.mediainfo_exe_path_edit.text() == r"C:\tools\mediainfo.exe"
        )
        reloaded_spin_a = reloaded_window.sources_drive_table.cellWidget(0, 6)
        reloaded_spin_b = reloaded_window.sources_drive_table.cellWidget(1, 6)
        assert isinstance(reloaded_spin_a, QSpinBox)
        assert isinstance(reloaded_spin_b, QSpinBox)
        assert reloaded_spin_a.value() == 4
        assert reloaded_spin_b.value() == 2
        reloaded_window.close()


def test_sources_tab_executable_browse_populates_target_edit(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        monkeypatch.setattr(
            "video_duperz.ui.main_window_settings.QFileDialog.getOpenFileName",
            lambda *args, **kwargs: (r"C:\tools\ffmpeg.exe", "Executable (*.exe)"),
        )

        window.ffmpeg_exe_path_browse_btn.click()
        app.processEvents()

        assert window.ffmpeg_exe_path_edit.text() == r"C:\tools\ffmpeg.exe"
        window.close()


def test_sources_tab_scan_priority_controls_exist(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert (
            window.findChild(QComboBox, "scan_parent_cpu_priority_combo")
            is window.scan_parent_cpu_priority_combo
        )
        assert (
            window.findChild(QComboBox, "scan_parent_io_mode_combo")
            is window.scan_parent_io_mode_combo
        )
        assert (
            window.findChild(QComboBox, "scan_child_cpu_priority_combo")
            is window.scan_child_cpu_priority_combo
        )
        assert (
            window.findChild(QComboBox, "scan_child_io_mode_combo")
            is window.scan_child_io_mode_combo
        )
        assert str(window.scan_parent_cpu_priority_combo.currentData()) == "normal"
        assert str(window.scan_parent_io_mode_combo.currentData()) == "normal"
        assert str(window.scan_child_cpu_priority_combo.currentData()) == "normal"
        assert str(window.scan_child_io_mode_combo.currentData()) == "normal"
        window.close()


def test_sources_tab_grouped_layout_has_detailed_tooltips(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        scan_folders_group = window.findChild(QGroupBox, "sources_scan_folders_group")
        physical_drives_group = window.findChild(
            QGroupBox,
            "sources_physical_drives_group",
        )
        scan_content_group = window.findChild(QGroupBox, "sources_scan_content_group")
        assert scan_folders_group is not None
        assert physical_drives_group is not None
        assert scan_content_group is not None
        assert "define which folders belong" in scan_folders_group.toolTip().lower()
        assert "physical-drive planning" in scan_folders_group.toolTip().lower()
        assert "worker plan" in physical_drives_group.toolTip().lower()
        assert "scan coverage" in scan_content_group.toolTip().lower()
        assert "eligible for duplicate analysis" in (
            window.roots_list.toolTip().lower()
        )
        assert "previously used source root" in (
            window.add_recent_root_btn.toolTip().lower()
        )
        assert "only files whose suffix matches" in (
            window.extensions_edit.toolTip().lower()
        )
        assert "shells out to the ffprobe executable" in (
            window.probe_backend_combo.toolTip().lower()
        )
        assert "restored after the scan finishes" in (
            window.scan_parent_cpu_priority_combo.toolTip().lower()
        )
        assert "heavy probe and fingerprint subprocesses" in (
            window.scan_child_cpu_priority_combo.toolTip().lower()
        )
        assert "leave this blank to use ffmpeg from path" in (
            window.ffmpeg_exe_path_edit.toolTip().lower()
        )
        assert "current scan roots map to local physical drives" in (
            window.sources_drive_summary_label.toolTip().lower()
        )
        assert window.sources_drive_table.minimumHeight() == 250
        assert window.sources_drive_table.maximumHeight() > 250
        content_form_table = window.findChild(
            QWidget,
            "sources_scan_content_form_table",
        )
        performance_form_table = window.findChild(
            QWidget,
            "sources_scan_performance_form_table",
        )
        tools_form_table = window.findChild(QWidget, "sources_tool_paths_form_table")
        assert content_form_table is not None
        assert performance_form_table is not None
        assert tools_form_table is not None
        assert isinstance(content_form_table.layout(), QGridLayout)
        assert isinstance(performance_form_table.layout(), QGridLayout)
        assert isinstance(tools_form_table.layout(), QGridLayout)
        sources_layout = window.sources_tab.layout()
        assert sources_layout is not None
        assert sources_layout.stretch(1) > sources_layout.stretch(0)
        workers_header = window.sources_drive_table.horizontalHeaderItem(6)
        assert workers_header is not None
        assert "worker count assigned to this drive" in (
            workers_header.toolTip().lower()
        )
        window.close()


def test_results_view_launch_mediainfo_uses_configured_override(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()
        (tmp_path / "a.mp4").write_bytes(b"")

        launched: list[list[str]] = []
        group = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(11, str(tmp_path / "a.mp4"), 320, 240, 1000, 1.0),
                _dup_item(12, str(tmp_path / "a_copy.mp4"), 320, 240, 900, 0.98),
            ],
            total_size_bytes=200,
            group_id=42,
        )
        window.results_view.load_groups([group])
        window.results_view.results_table.setCurrentCell(0, 0)
        window.results_view.set_mediainfo_exe_path(r"C:\tools\mediainfo.exe")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.resolve_executable_path",
            lambda tool_name, override_path="", *, not_found_message: str(
                override_path or tool_name
            ),
        )
        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.subprocess.Popen",
            lambda command: launched.append(list(command)),
        )

        window.results_view.launch_mediainfo()

        assert launched == [[r"C:\tools\mediainfo.exe", str(tmp_path / "a.mp4")]]
        monkeypatch.undo()
        window.close()


def test_results_view_launch_mediainfo_blank_override_falls_back_to_path(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()
        (tmp_path / "b.mp4").write_bytes(b"")

        launched: list[list[str]] = []
        group = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(12, str(tmp_path / "b.mp4"), 320, 240, 900, 0.98),
                _dup_item(13, str(tmp_path / "b_copy.mp4"), 320, 240, 880, 0.97),
            ],
            total_size_bytes=200,
            group_id=43,
        )
        window.results_view.load_groups([group])
        window.results_view.results_table.setCurrentCell(0, 0)
        window.results_view.set_mediainfo_exe_path("")

        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.resolve_executable_path",
            lambda tool_name, override_path="", *, not_found_message: str(
                override_path or tool_name
            ),
        )
        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.subprocess.Popen",
            lambda command: launched.append(list(command)),
        )

        window.results_view.launch_mediainfo()

        assert launched == [["mediainfo", str(tmp_path / "b.mp4")]]
        window.close()


def test_results_view_launch_mediainfo_invalid_override_warns(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()
        (tmp_path / "c.mp4").write_bytes(b"")

        warnings: list[tuple[str, str]] = []
        statuses: list[str] = []
        group = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(13, str(tmp_path / "c.mp4"), 320, 240, 800, 0.97),
                _dup_item(14, str(tmp_path / "c_copy.mp4"), 320, 240, 780, 0.96),
            ],
            total_size_bytes=200,
            group_id=44,
        )
        window.results_view.load_groups([group])
        window.results_view.results_table.setCurrentCell(0, 0)
        window.results_view.set_mediainfo_exe_path(r"C:\missing\mediainfo.exe")
        window.results_view.status_message.connect(statuses.append)

        def _raise_missing(
            tool_name: str,
            override_path: str = "",
            *,
            not_found_message: str,
        ) -> str:
            _ = not_found_message
            raise FileNotFoundError(
                f"{tool_name} executable override path is invalid: {override_path}"
            )

        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.resolve_executable_path",
            _raise_missing,
        )
        monkeypatch.setattr(
            "video_duperz.ui.results_view_actions.QMessageBox.warning",
            lambda _parent, title, text: warnings.append((str(title), str(text))),
        )

        window.results_view.launch_mediainfo()

        assert warnings == [
            (
                "MediaInfo Missing",
                (
                    r"mediainfo executable override path is invalid: "
                    r"C:\missing\mediainfo.exe"
                ),
            )
        ]
        assert statuses == [
            "mediainfo is not installed, not on PATH, or has an invalid override path."
        ]
        window.close()


def test_scan_running_locks_ui_to_scan_tab_until_finished(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        settings.extensions = ["mp4"]
        settings.max_workers = 2
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.build_physical_drive_scan_plan",
            lambda roots, max_workers, drive_worker_overrides=None: SimpleNamespace(
                root_groups=[[str(root)] for root in roots],
                effective_total_workers=max_workers,
            ),
        )
        priority_calls: list[str] = []
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.apply_scan_priority_to_current_process",
            lambda cpu_priority, io_mode: (
                priority_calls.append(f"apply:{cpu_priority}:{io_mode}") or object()
            ),
        )
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.restore_scan_priority_to_current_process",
            lambda state: priority_calls.append(f"restore:{state is not None}"),
        )
        monkeypatch.setattr(window.thread_pool, "start", lambda worker: None)

        window.tabs.setCurrentWidget(window.sources_tab)
        app.processEvents()
        assert window.tabs.currentWidget() == window.sources_tab

        window._start_scan()
        app.processEvents()
        scan_idx = window.tabs.indexOf(window.scan_view)
        src_idx = window.tabs.indexOf(window.sources_tab)
        res_idx = window.tabs.indexOf(window.results_view)
        assert window.tabs.currentWidget() == window.scan_view
        assert window.tabs.isTabEnabled(scan_idx)
        assert not window.tabs.isTabEnabled(src_idx)
        assert not window.tabs.isTabEnabled(res_idx)
        assert not window.scan_view.start_btn.isEnabled()
        assert not window.scan_view.rescan_btn.isEnabled()
        assert window.scan_view.cancel_btn.isEnabled()
        assert priority_calls == ["apply:normal:normal"]

        window.tabs.setCurrentWidget(window.sources_tab)
        app.processEvents()
        assert window.tabs.currentWidget() == window.scan_view

        finished_scan_id = db.create_scan(
            profile="balanced", roots=[str(tmp_path)], extensions=["mp4"]
        )
        db.complete_scan(finished_scan_id, status="done")
        window._scan_finished(
            SimpleNamespace(scan_id=finished_scan_id, issues=[], metrics={})
        )
        app.processEvents()

        assert window.tabs.isTabEnabled(scan_idx)
        assert window.tabs.isTabEnabled(src_idx)
        assert window.tabs.isTabEnabled(res_idx)
        assert window.scan_view.start_btn.isEnabled()
        assert window.scan_view.rescan_btn.isEnabled()
        assert not window.scan_view.cancel_btn.isEnabled()
        assert priority_calls == ["apply:normal:normal", "restore:True"]
        window.close()


def test_scan_finished_cancelled_does_not_switch_to_results_tab(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        settings.extensions = ["mp4"]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()
        restore_calls: list[bool] = []
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.restore_scan_priority_to_current_process",
            lambda state: restore_calls.append(state is not None),
        )
        window._scan_priority_state = object()

        cancelled_scan_id = db.create_scan(
            profile="balanced", roots=[str(tmp_path)], extensions=["mp4"]
        )
        db.complete_scan(cancelled_scan_id, status="cancelled")
        window.tabs.setCurrentWidget(window.scan_view)
        app.processEvents()

        window._scan_finished(SimpleNamespace(scan_id=cancelled_scan_id, issues=[]))
        app.processEvents()

        assert window.tabs.currentWidget() == window.scan_view
        assert window.current_scan_id is None
        assert "cancelled" in window.statusBar().currentMessage().lower()
        assert restore_calls == [True]
        monkeypatch.undo()
        window.close()


def test_scan_finished_paused_restores_scan_process_priority(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        settings.extensions = ["mp4"]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        paused_scan_id = db.create_scan(
            profile="balanced", roots=[str(tmp_path)], extensions=["mp4"]
        )
        db.complete_scan(paused_scan_id, status="paused")
        restore_calls: list[bool] = []
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.restore_scan_priority_to_current_process",
            lambda state: restore_calls.append(state is not None),
        )
        window._scan_priority_state = object()

        window._scan_finished(SimpleNamespace(scan_id=paused_scan_id, issues=[]))
        app.processEvents()

        assert restore_calls == [True]
        assert (
            window.scan_view.isVisible()
            or window.tabs.currentWidget() == window.scan_view
        )
        monkeypatch.undo()
        window.close()


def test_cancel_scan_confirmation_decline_does_not_cancel(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        calls = {"cancel": 0}
        window.scan_worker = SimpleNamespace(
            cancel=lambda: calls.__setitem__("cancel", int(calls["cancel"]) + 1)
        )
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.StandardButton.No,
        )

        window._cancel_scan()
        app.processEvents()

        assert calls["cancel"] == 0
        window.close()


def test_scan_error_restores_scan_process_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        restore_calls: list[bool] = []
        critical_calls: list[tuple[str, str]] = []
        window._scan_priority_state = object()
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.restore_scan_priority_to_current_process",
            lambda state: restore_calls.append(state is not None),
        )
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.QMessageBox.critical",
            lambda _parent, title, text: critical_calls.append((str(title), str(text))),
        )

        window._scan_error("boom")
        app.processEvents()

        assert restore_calls == [True]
        assert critical_calls == [("Scan Error", "boom")]
        window.close()


def test_cancel_scan_confirmation_accepts_and_cancels(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        calls = {"cancel": 0}
        window.scan_worker = SimpleNamespace(
            cancel=lambda: calls.__setitem__("cancel", int(calls["cancel"]) + 1)
        )
        monkeypatch.setattr(
            "video_duperz.ui.main_window_scan_actions.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )

        window._cancel_scan()
        app.processEvents()

        assert calls["cancel"] == 1
        window.close()


def test_rescan_uses_current_sources_and_runs_cleanup_then_start(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path / "old")]
        settings.extensions = ["avi"]
        settings.similarity_profile = "balanced"
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        stale = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(11, str(tmp_path / "stale_a.mp4"), 320, 240, 1000, 1.0),
                _dup_item(12, str(tmp_path / "stale_b.mp4"), 320, 240, 900, 0.99),
            ],
            total_size_bytes=200,
            group_id=99,
        )
        window.current_scan_id = 1
        window.results_view.load_groups([stale])
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2

        root_a = str(tmp_path / "source_a")
        root_b = str(tmp_path / "source_b")
        window.roots_list.clear()
        window.roots_list.addItem(root_a)
        window.roots_list.addItem(root_b)
        window.extensions_edit.setText("mp4,mkv")
        window.profile_combo.setCurrentText("aggressive")
        app.processEvents()

        called: dict[str, object] = {}

        def _fake_purge(scan_set_key: str, roots: list[str]) -> dict[str, int]:
            called["scan_set_key"] = scan_set_key
            called["roots"] = list(roots)
            return {
                "deleted_scans": 3,
                "deleted_files": 8,
                "deleted_groups": 2,
                "deleted_actions": 1,
            }

        starts = {"count": 0}
        monkeypatch.setattr(
            "video_duperz.ui.main_window_profiles.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(window.db, "purge_for_fresh_rescan", _fake_purge)
        monkeypatch.setattr(window, "_clear_cached_thumbnails_internal", lambda: (5, 1))
        monkeypatch.setattr(
            window,
            "_start_scan",
            lambda: starts.__setitem__("count", int(starts["count"]) + 1),
        )

        window._rescan_scan()
        app.processEvents()

        expected_roots = normalize_roots_for_display([root_a, root_b])
        expected_key = build_scan_set_key(
            roots=expected_roots,
            similarity_profile="aggressive",
            extensions=["mp4", "mkv"],
        )
        assert called["roots"] == expected_roots
        assert called["scan_set_key"] == expected_key
        assert starts["count"] == 1
        assert window.current_scan_id is None
        assert window.results_view.results_table.rowCount() == 0
        window.close()


def test_rescan_cancelled_confirmation_does_not_purge_or_start(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path / "src")]
        settings.extensions = ["mp4"]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        calls = {"purge": 0, "start": 0}
        monkeypatch.setattr(
            "video_duperz.ui.main_window_profiles.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.StandardButton.No,
        )
        monkeypatch.setattr(
            window.db,
            "purge_for_fresh_rescan",
            lambda *args, **kwargs: calls.__setitem__("purge", int(calls["purge"]) + 1),
        )
        monkeypatch.setattr(
            window,
            "_start_scan",
            lambda: calls.__setitem__("start", int(calls["start"]) + 1),
        )

        window._rescan_scan()
        app.processEvents()

        assert calls["purge"] == 0
        assert calls["start"] == 0
        window.close()


def test_scan_view_progress_keeps_parallel_worker_tokens(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.scan_view.update_progress(
            ScanProgress(
                stage="enumerate",
                current=1,
                total=2,
                message=f"Enumerated {tmp_path} [workers 2/3]",
            )
        )
        app.processEvents()

        assert (
            window.scan_view.progress_table.item(0, SCAN_PROGRESS_COL_MESSAGE)
            .text()
            .endswith(f"{tmp_path} [workers 2/3]")
        )
        window.close()


def test_scan_view_progress_uses_compact_counters(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.scan_view.update_progress(
            ScanProgress(
                stage="probe",
                current=12,
                total=1001,
                message="Analyzed file.mp4",
            )
        )
        app.processEvents()

        assert (
            window.scan_view.progress_table.item(0, SCAN_PROGRESS_COL_PROGRESS).text()
            == "12/1001"
        )
        window.close()


def test_scan_view_resume_eta_uses_only_real_remaining_work(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.scan_view.update_progress(
            ScanProgress(
                stage="probe",
                current=950,
                total=1000,
                message="Resumed and analyzing tail files",
                cached_files=945,
                analyzed_files=5,
                elapsed_s=100.0,
                completed_files=950,
                total_work_files=1000,
                total_analyze_files=20,
            )
        )
        app.processEvents()

        assert window.scan_view.eta_label.text().startswith("ETA: 5m | Done by ")
        window.close()


def test_scan_view_resume_eta_stays_hidden_without_enough_real_work(
    tmp_path: Path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.scan_view.update_progress(
            ScanProgress(
                stage="probe",
                current=902,
                total=1000,
                message="Resumed but still warming up",
                cached_files=900,
                analyzed_files=2,
                elapsed_s=120.0,
                completed_files=902,
                total_work_files=1000,
                total_analyze_files=20,
            )
        )
        app.processEvents()

        assert window.scan_view.eta_label.text() == "ETA: --"
        window.close()


def test_scan_view_removes_redundant_top_progress_labels(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert not hasattr(window.scan_view, "status_label")
        scan_labels = {
            label.text()
            for label in window.scan_view.findChildren(QLabel)
            if label.text()
        }
        assert "Stage Progress" not in scan_labels
        assert "Detailed Scan Progress" in scan_labels
        assert "Scan Issues" in scan_labels
        assert window.scan_view.stage_progress is not None
        assert [
            window.scan_view.progress_table.horizontalHeaderItem(index).text()
            for index in range(window.scan_view.progress_table.columnCount())
        ] == SCAN_PROGRESS_HEADERS
        assert [
            window.scan_view.issues_table.horizontalHeaderItem(index).text()
            for index in range(window.scan_view.issues_table.columnCount())
        ] == SCAN_ISSUE_HEADERS
        assert [
            window.scan_view.progress_table.columnWidth(index)
            for index in range(window.scan_view.progress_table.columnCount())
        ] == [
            window.scan_view.issues_table.columnWidth(index)
            for index in range(window.scan_view.issues_table.columnCount())
        ]
        window.close()


def test_scan_issue_rows_do_not_echo_to_status_bar(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.statusBar().showMessage("Scan started")
        window._scan_issue(
            ScanIssue(
                stage="probe",
                path=str(tmp_path / "bad.mp4"),
                message="Could not probe file",
            )
        )
        app.processEvents()

        assert window.scan_view.issues_table.rowCount() == 1
        assert _table_row_texts(window.scan_view.issues_table, 0) == [
            "probe",
            "",
            "bad.mp4",
            "Could not probe file",
        ]
        issue_file_item = window.scan_view.issues_table.item(0, SCAN_ISSUE_COL_FILE)
        assert issue_file_item is not None
        assert issue_file_item.toolTip() == str(tmp_path / "bad.mp4")
        assert window.statusBar().currentMessage() == "Scan started"
        window.close()


def test_scan_log_tables_use_shared_columns_and_filename_only_values(
    tmp_path: Path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        progress_path = str(tmp_path / "nested" / "progress_subject.mp4")
        issue_path = str(tmp_path / "nested" / "issue_subject.mp4")
        window.scan_view.append_progress_note(
            "probe",
            "Progress details",
            progress_path,
        )
        window.scan_view.append_issue(
            ScanIssue(
                stage="warning",
                path=issue_path,
                message="Issue details",
            )
        )
        app.processEvents()

        assert _table_row_texts(window.scan_view.progress_table, 0) == [
            "probe",
            "",
            "progress_subject.mp4",
            "Progress details",
        ]
        assert _table_row_texts(window.scan_view.issues_table, 0) == [
            "warning",
            "",
            "issue_subject.mp4",
            "Issue details",
        ]
        progress_file_item = window.scan_view.progress_table.item(
            0, SCAN_PROGRESS_COL_FILE
        )
        issue_file_item = window.scan_view.issues_table.item(0, SCAN_ISSUE_COL_FILE)
        assert progress_file_item is not None
        assert issue_file_item is not None
        assert progress_file_item.toolTip() == progress_path
        assert issue_file_item.toolTip() == issue_path
        window.close()


def test_scan_log_tables_use_light_state_tints(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.scan_view.append_progress_note(
            "cache",
            "Reused cached analysis clip.mp4",
        )
        window.scan_view.append_progress_note(
            "probe",
            "Analyzing clip.mp4",
        )
        window.scan_view.append_issue(
            ScanIssue(
                stage="warning",
                path=str(tmp_path / "warn.mp4"),
                message="Skipped because metadata was incomplete",
            )
        )
        window.scan_view.append_issue(
            ScanIssue(
                stage="probe",
                path=str(tmp_path / "bad.mp4"),
                message="Failed to probe file",
            )
        )
        app.processEvents()

        assert (
            _table_cell_background_name(window.scan_view.progress_table, 0) == "#e9f8ea"
        )
        assert (
            _table_cell_background_name(window.scan_view.progress_table, 1) == "#eaf4ff"
        )
        assert (
            _table_cell_background_name(window.scan_view.issues_table, 0) == "#fff4db"
        )
        assert (
            _table_cell_background_name(window.scan_view.issues_table, 1) == "#fde7e7"
        )
        window.close()


def test_scan_progress_table_auto_scroll_pauses_until_bottom(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.resize(960, 520)
        window.show()
        window.tabs.setCurrentWidget(window.scan_view)
        window.scan_view.progress_table.setFixedHeight(140)
        window.scan_view.progress_table.verticalHeader().setDefaultSectionSize(24)
        app.processEvents()

        for index in range(80):
            window.scan_view.append_progress_note(
                "probe",
                f"Progress row {index}",
                str(tmp_path / f"row_{index}.mp4"),
            )
        for row in range(window.scan_view.progress_table.rowCount()):
            window.scan_view.progress_table.setRowHeight(row, 24)
        app.processEvents()

        scroll_bar = window.scan_view.progress_table.verticalScrollBar()
        assert scroll_bar.maximum() > 0
        assert scroll_bar.value() >= scroll_bar.maximum() - 1

        scroll_bar.setValue(max(0, scroll_bar.maximum() // 2))
        app.processEvents()
        paused_value = scroll_bar.value()

        window.scan_view.append_progress_note(
            "probe",
            "Progress row while paused",
            str(tmp_path / "paused.mp4"),
        )
        app.processEvents()

        assert scroll_bar.value() >= paused_value
        assert scroll_bar.value() < scroll_bar.maximum()

        scroll_bar.setValue(scroll_bar.maximum())
        app.processEvents()
        window.scan_view.append_progress_note(
            "probe",
            "Progress row after resume",
            str(tmp_path / "resumed.mp4"),
        )
        app.processEvents()

        assert scroll_bar.value() >= scroll_bar.maximum() - 1
        window.close()


def test_scan_issues_table_auto_scroll_pauses_until_bottom(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.resize(960, 520)
        window.show()
        window.tabs.setCurrentWidget(window.scan_view)
        window.scan_view.issues_table.setFixedHeight(140)
        window.scan_view.issues_table.verticalHeader().setDefaultSectionSize(24)
        app.processEvents()

        for index in range(60):
            window.scan_view.append_issue(
                ScanIssue(
                    stage="probe",
                    path=str(tmp_path / f"issue_{index}.mp4"),
                    message=f"Issue row {index}",
                )
            )
        for row in range(window.scan_view.issues_table.rowCount()):
            window.scan_view.issues_table.setRowHeight(row, 24)
        app.processEvents()

        scroll_bar = window.scan_view.issues_table.verticalScrollBar()
        assert scroll_bar.maximum() > 0
        assert scroll_bar.value() >= scroll_bar.maximum() - 1

        scroll_bar.setValue(max(0, scroll_bar.maximum() // 2))
        app.processEvents()
        paused_value = scroll_bar.value()

        window.scan_view.append_issue(
            ScanIssue(
                stage="probe",
                path=str(tmp_path / "paused_issue.mp4"),
                message="Issue row while paused",
            )
        )
        app.processEvents()

        assert scroll_bar.value() >= paused_value
        assert scroll_bar.value() < scroll_bar.maximum()

        scroll_bar.setValue(scroll_bar.maximum())
        app.processEvents()
        window.scan_view.append_issue(
            ScanIssue(
                stage="probe",
                path=str(tmp_path / "resumed_issue.mp4"),
                message="Issue row after resume",
            )
        )
        app.processEvents()

        assert scroll_bar.value() >= scroll_bar.maximum() - 1
        window.close()


def test_scan_log_tables_double_click_emit_associated_path(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        captured_paths: list[str] = []
        window.scan_view.path_activation_requested.connect(captured_paths.append)

        progress_path = str(tmp_path / "progress_subject.mp4")
        issue_path = str(tmp_path / "issue_subject.mp4")
        window.scan_view.append_progress_note(
            "probe",
            "Open progress path",
            progress_path,
        )
        window.scan_view.append_issue(
            ScanIssue(
                stage="probe",
                path=issue_path,
                message="Open issue path",
            )
        )
        app.processEvents()

        window.scan_view.progress_table.itemDoubleClicked.emit(
            window.scan_view.progress_table.item(0, SCAN_PROGRESS_COL_FILE)
        )
        window.scan_view.issues_table.itemDoubleClicked.emit(
            window.scan_view.issues_table.item(0, SCAN_ISSUE_COL_FILE)
        )

        assert captured_paths == [progress_path, issue_path]
        window.close()


def test_scan_view_renders_lane_snapshots_and_worker_utilization(
    tmp_path: Path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.scan_view.initialize_lane_plan(
            [["R:/Videos"], ["S:/Archive"]], worker_limit=2
        )
        window.scan_view.update_progress(
            ScanProgress(
                stage="probe",
                current=1,
                total=3,
                message="Analyzed R:/Videos/a.mp4",
                discovered_files=5,
                discovered_bytes=8 * 1024 * 1024,
                discovered_files_per_s=2.5,
                discovered_mib_per_s=4.0,
                active_workers=1,
                worker_limit=2,
                analyzed_files=3,
                analyzed_bytes=4 * 1024 * 1024,
                analyzed_files_per_s=1.5,
                analyzed_mib_per_s=2.0,
                cached_files=2,
                cache_hit_ratio=0.4,
                elapsed_s=120.0,
                total_analyze_files=10,
                completed_files=4,
                total_work_files=10,
                fingerprint_only_files=1,
                probe_and_fingerprint_files=2,
                lane_snapshots=[
                    ScanLaneSnapshot(
                        lane=0,
                        roots=["R:/Videos"],
                        state="running",
                        discovered=3,
                        discovered_bytes=6 * 1024 * 1024,
                        queued=1,
                        analyzed=1,
                        analyzed_bytes=2 * 1024 * 1024,
                        completed=1,
                        discovery_complete=False,
                        cache_hits=1,
                        fingerprint_only=0,
                        probe_and_fingerprint=0,
                        discovered_files_per_s=1.2,
                        discovered_mib_per_s=2.4,
                        analyzed_files_per_s=0.4,
                        analyzed_mib_per_s=0.8,
                        active_file="R:/Videos/a.mp4",
                        workers=1,
                    ),
                    ScanLaneSnapshot(
                        lane=1,
                        roots=["S:/Archive"],
                        state="idle",
                        discovered=2,
                        discovered_bytes=2 * 1024 * 1024,
                        queued=0,
                        analyzed=2,
                        analyzed_bytes=2 * 1024 * 1024,
                        completed=2,
                        discovery_complete=True,
                        cache_hits=1,
                        fingerprint_only=1,
                        probe_and_fingerprint=1,
                        discovered_files_per_s=1.3,
                        discovered_mib_per_s=1.6,
                        analyzed_files_per_s=1.1,
                        analyzed_mib_per_s=1.2,
                        active_file="",
                        workers=0,
                    ),
                ],
            )
        )
        app.processEvents()

        assert not window.scan_view.worker_progress.isVisible()
        assert window.scan_view.eta_label.text().startswith("ETA: 5m | Done by ")
        assert window.scan_view.lane_table.rowCount() == 2
        assert window.scan_view.lane_table.columnCount() == 13
        assert [
            window.scan_view.lane_table.horizontalHeaderItem(index).text()
            for index in range(window.scan_view.lane_table.columnCount())
        ] == SCAN_LANE_HEADERS
        assert window.scan_view.lane_table.item(0, 2).text() == "running"
        lane0_progress_cell = window.scan_view.lane_table.cellWidget(
            0,
            SCAN_LANE_COL_PROGRESS,
        )
        assert lane0_progress_cell is not None
        lane0_progress = _lane_progress_bar(lane0_progress_cell)
        assert lane0_progress.format() == "1/3+ (33%)"
        lane0_layout = lane0_progress_cell.layout()
        assert lane0_layout is not None
        assert bool(lane0_layout.alignment() & Qt.AlignmentFlag.AlignVCenter)
        assert (
            window.scan_view.lane_table.item(0, SCAN_LANE_COL_ACTIVE_FILE).text()
            == "R:/Videos/a.mp4"
        )
        assert (
            window.scan_view.lane_table.item(0, SCAN_LANE_COL_DISC_PER_S).text()
            == "1.20"
        )
        assert (
            window.scan_view.lane_table.item(0, SCAN_LANE_COL_DISC_MIB_PER_S).text()
            == "2.40"
        )
        assert (
            window.scan_view.lane_table.item(0, SCAN_LANE_COL_ANAL_PER_S).text()
            == "0.40"
        )
        assert (
            window.scan_view.lane_table.item(0, SCAN_LANE_COL_ANAL_MIB_PER_S).text()
            == "0.80"
        )
        assert (
            window.scan_view.lane_table.item(0, 0).background().color().name().lower()
            == "#f0f8ff"
        )
        assert window.scan_view.lane_table.item(1, 2).text() == "idle"
        assert window.scan_view.lane_table.item(1, 5).text() == "2"
        lane1_progress = _lane_progress_bar(
            window.scan_view.lane_table.cellWidget(1, SCAN_LANE_COL_PROGRESS)
        )
        assert lane1_progress.format() == "2/2 (100%)"
        assert (
            window.scan_view.lane_table.item(1, 0).background().color().name().lower()
            == "#f5f5f5"
        )
        assert "cache hit 40.0%" in window.scan_view.io_stats_label.text()
        assert "reused 2" in window.scan_view.io_stats_label.text()
        assert "fp-only 1" in window.scan_view.io_stats_label.text()
        assert "reprobe 2" in window.scan_view.io_stats_label.text()
        assert window.scan_view.rescan_btn.text() == "Rescan"
        assert window.scan_view.pause_btn.text() == "Pause Scan"
        assert window.scan_view.resume_btn.text() == "Resume Scan"
        window.close()


def test_scan_view_lane_state_background_colors(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.scan_view.initialize_lane_plan(
            [["R:/0"], ["R:/1"], ["R:/2"], ["R:/3"], ["R:/4"], ["R:/5"]],
            worker_limit=2,
        )
        window.scan_view.update_progress(
            ScanProgress(
                stage="probe",
                current=1,
                total=6,
                message="lane states",
                lane_snapshots=[
                    ScanLaneSnapshot(lane=0, roots=["R:/0"], state="pending"),
                    ScanLaneSnapshot(lane=1, roots=["R:/1"], state="queued"),
                    ScanLaneSnapshot(lane=2, roots=["R:/2"], state="running"),
                    ScanLaneSnapshot(lane=3, roots=["R:/3"], state="idle"),
                    ScanLaneSnapshot(lane=4, roots=["R:/4"], state="done"),
                    ScanLaneSnapshot(lane=5, roots=["R:/5"], state="error"),
                ],
            )
        )
        app.processEvents()

        assert (
            window.scan_view.lane_table.item(0, 0).background().color().name().lower()
            == "#f5f5dc"
        )
        assert (
            window.scan_view.lane_table.item(1, 0).background().color().name().lower()
            == "#f5f5dc"
        )
        assert (
            window.scan_view.lane_table.item(2, 0).background().color().name().lower()
            == "#f0f8ff"
        )
        assert (
            window.scan_view.lane_table.item(3, 0).background().color().name().lower()
            == "#f5f5f5"
        )
        assert (
            window.scan_view.lane_table.item(4, 0).background().color().name().lower()
            == "#f0fff0"
        )
        assert (
            window.scan_view.lane_table.item(5, 0).background().color().name().lower()
            == "#ffe4e1"
        )
        window.close()


def test_scan_view_progress_rows_distinguish_resume_work_kinds(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        for stage, work_kind, message in [
            ("cache", "cache_hit", "Reused cached analysis a.mp4"),
            ("fingerprint", "fingerprint_only", "Reused probe, fingerprinted b.mp4"),
            ("probe", "probe_and_fingerprint", "Probed and fingerprinted c.mp4"),
        ]:
            window.scan_view.update_progress(
                ScanProgress(
                    stage=stage,
                    current=1,
                    total=3,
                    completed_files=1,
                    total_work_files=3,
                    work_kind=work_kind,
                    message=message,
                )
            )
        app.processEvents()

        rows = [
            _table_row_texts(window.scan_view.progress_table, index)
            for index in range(window.scan_view.progress_table.rowCount())
        ]
        assert any(
            row[SCAN_PROGRESS_COL_MESSAGE] == "Reused cached analysis a.mp4"
            for row in rows
        )
        assert any(
            row[SCAN_PROGRESS_COL_MESSAGE] == "Reused probe, fingerprinted b.mp4"
            for row in rows
        )
        assert any(
            row[SCAN_PROGRESS_COL_MESSAGE] == "Probed and fingerprinted c.mp4"
            for row in rows
        )
        window.close()
