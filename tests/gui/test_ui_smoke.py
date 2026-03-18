from __future__ import annotations

import os
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from video_duperz.config import default_settings, load_settings
from video_duperz.db import Database

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
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
    COL_CHECK,
    COL_FILE_NAME,
    COL_FULL_PATH,
    COL_PARENT_DIR,
    COL_SIZE,
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
    is_hdr: bool = False,
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
        audio_codec="aac",
        audio_bitrate=128000,
        audio_languages="eng",
        subtitle_languages="eng",
        is_hdr=is_hdr,
        similarity_score=sim,
        keep_default=file_id % 2 == 1,
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
                    is_hdr=False,
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
                    is_hdr=True,
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
                    is_hdr=False,
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
                    is_hdr=False,
                ),
            ],
            total_size_bytes=(12 + 6) * 1024 * 1024,
            group_id=62,
        ),
    ]


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
        assert window.results_view.results_table.columnCount() == 20
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
            window.results_view.results_table.horizontalHeaderItem(10).text()
            == "Audio Codec"
        )
        assert (
            window.results_view.results_table.horizontalHeaderItem(14).text() == "HDR"
        )

        group = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(11, str(tmp_path / "missing.mp4"), 320, 240, 1000, 1.0),
                _dup_item(
                    12,
                    str(tmp_path / "missing_copy.mp4"),
                    320,
                    240,
                    900,
                    0.98,
                ),
            ],
            total_size_bytes=200,
            group_id=42,
        )
        window.results_view.load_groups([group])
        app.processEvents()
        initial_height = window.results_view.results_table.rowHeight(0)

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
        scan_folders_label = sources_layout.itemAt(0).widget()
        assert scan_folders_label is not None
        assert scan_folders_label.text() == "Scan Folders"
        labels = []
        for i in range(sources_layout.count()):
            widget = sources_layout.itemAt(i).widget()
            if widget is not None and hasattr(widget, "text"):
                labels.append(str(widget.text()))
        assert "Max Workers total" in labels

        roots_actions_layout = sources_layout.itemAt(2).layout()
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

        window.close()


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
                            is_hdr=False,
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
                            is_hdr=True,
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
                            is_hdr=False,
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
                            is_hdr=False,
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
        assert window.delete_selected_permanent_action in menu_actions

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
            for sequence in window.delete_selected_permanent_action.shortcuts()
        } == {"Shift+Del"}

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


def test_saved_scan_profiles_save_load_and_delete(tmp_path: Path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        roots = [str(tmp_path / "library")]
        scan_id = db.create_scan(profile="balanced", roots=roots, extensions=["mp4"])
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
                has_audio=True,
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="eng",
                is_hdr=False,
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
                has_audio=True,
                audio_codec="aac",
                audio_bitrate=128000,
                audio_languages="eng",
                subtitle_languages="eng",
                is_hdr=False,
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
        settings.similarity_profile = "balanced"
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        assert window.save_scan_set_btn.text() == "Save Scan Set"
        assert window.load_saved_scan_btn.text() == "Load Saved Scan"

        monkeypatch.setattr(
            "video_duperz.ui.main_window_profiles.QInputDialog.getText",
            lambda *a, **k: ("My Library", True),
        )
        window._save_current_scan_set_as()
        app.processEvents()
        assert "My Library" in window._saved_scan_profiles

        payload = window._saved_scan_profiles["My Library"]
        window._load_saved_scan_profile(payload, "My Library")
        app.processEvents()
        assert window.current_scan_id == scan_id
        assert window.tabs.currentWidget() == window.results_view
        assert window.results_view.results_table.rowCount() == 2
        roots_in_widget = [
            window.roots_list.item(i).text() for i in range(window.roots_list.count())
        ]
        assert roots_in_widget == roots
        assert window.profile_combo.currentText() == "balanced"
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
            profile="balanced",
            roots=roots,
            extensions=["mp4"],
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
            similarity_profile="balanced",
            extensions=["mp4"],
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
        assert window.scan_view.issues_list.count() == 1
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
        window.scan_view.retry_failed_checkbox.setChecked(False)

        captured: dict[str, object] = {}

        def _capture_launch(**kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(window, "_launch_scan", _capture_launch)

        window._resume_scan()

        assert captured["resume_scan_id"] == paused_id
        assert captured["retry_failed_files"] is False
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
    assert loaded.ffmpeg_exe_path == r"C:\tools\ffmpeg.exe"
    assert loaded.ffprobe_exe_path == r"C:\tools\ffprobe.exe"
    assert loaded.mediainfo_exe_path == r"C:\tools\mediainfo.exe"

    with Database(tmp_path / "app.db") as db:
        reloaded_window = MainWindow(db=db, settings=loaded)
        reloaded_window.show()
        app.processEvents()

        assert reloaded_window.probe_backend_combo.currentText() == "pyav"
        assert reloaded_window.probe_mode_combo.currentText() == "burst"
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

        assert "[workers 2/3]" in window.scan_view.status_label.text()
        assert "[workers 2/3]" in window.scan_view.progress_list.item(0).text()
        window.close()


def test_scan_view_progress_uses_padded_counters(tmp_path: Path) -> None:
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

        assert "(  12 / 1001)" in window.scan_view.status_label.text()
        assert "  12 / 1001" in window.scan_view.progress_list.item(0).text()
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
        assert window.scan_view.eta_label.text().startswith("ETA: 3m | Done by ")
        assert window.scan_view.lane_table.rowCount() == 2
        assert window.scan_view.lane_table.columnCount() == 13
        assert window.scan_view.lane_table.item(0, 2).text() == "running"
        lane0_progress_cell = window.scan_view.lane_table.cellWidget(0, 6)
        assert lane0_progress_cell is not None
        lane0_progress = _lane_progress_bar(lane0_progress_cell)
        assert lane0_progress.format() == "1 / 3+"
        lane0_layout = lane0_progress_cell.layout()
        assert lane0_layout is not None
        assert bool(lane0_layout.alignment() & Qt.AlignmentFlag.AlignVCenter)
        assert window.scan_view.lane_table.item(0, 7).text() == "R:/Videos/a.mp4"
        assert window.scan_view.lane_table.item(0, 9).text() == "1.20"
        assert window.scan_view.lane_table.item(0, 10).text() == "2.40"
        assert window.scan_view.lane_table.item(0, 11).text() == "0.40"
        assert window.scan_view.lane_table.item(0, 12).text() == "0.80"
        assert (
            window.scan_view.lane_table.item(0, 0).background().color().name().lower()
            == "#f0f8ff"
        )
        assert window.scan_view.lane_table.item(1, 2).text() == "idle"
        assert window.scan_view.lane_table.item(1, 5).text() == "2"
        lane1_progress = _lane_progress_bar(
            window.scan_view.lane_table.cellWidget(1, 6)
        )
        assert lane1_progress.format() == "2 / 2"
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
            window.scan_view.progress_list.item(index).text()
            for index in range(window.scan_view.progress_list.count())
        ]
        assert any("Reused cached analysis a.mp4" in row for row in rows)
        assert any("Reused probe, fingerprinted b.mp4" in row for row in rows)
        assert any("Probed and fingerprinted c.mp4" in row for row in rows)
        window.close()
