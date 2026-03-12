from __future__ import annotations

import os
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest

from video_duperz.config import default_settings, load_settings
from video_duperz.db import Database

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QSpinBox

from video_duperz.models import (
    DuplicateGroup,
    DuplicateItem,
    SavedScanProfilePayload,
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
) -> DuplicateItem:
    return DuplicateItem(
        file_id=file_id,
        path=path,
        size=size,
        mtime_ns=1_700_000_000_000_000_000 + file_id,
        ctime_ns=1_700_000_000_000_000_000 + file_id,
        duration_s=1.0,
        width=width,
        height=height,
        bitrate=bitrate,
        codec="h264",
        audio_codec="aac",
        audio_bitrate=128000,
        audio_languages="eng",
        subtitle_languages="eng",
        is_hdr=False,
        similarity_score=sim,
        keep_default=file_id % 2 == 1,
        selected_action="keep",
    )


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
        assert window.results_view.results_table.columnCount() == 19
        assert window.results_view.results_table.horizontalHeaderItem(2).text() == "="
        assert window.results_view.results_table.horizontalHeaderItem(3).text() == "Thumbnail"
        assert window.results_view.results_table.horizontalHeaderItem(9).text() == "Audio Codec"
        assert window.results_view.results_table.horizontalHeaderItem(13).text() == "HDR"

        group = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[_dup_item(11, str(tmp_path / "missing.mp4"), 320, 240, 1000, 1.0)],
            total_size_bytes=100,
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


def test_recent_folder_history_button_and_persistence(tmp_path: Path, monkeypatch) -> None:
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
        roots = [window.roots_list.item(i).text() for i in range(window.roots_list.count())]
        assert str(Path("D:/Videos")) in roots
        window.close()

    loaded = load_settings()
    assert loaded.recent_scan_roots[:2] == [str(Path("D:/Videos")), str(Path("E:/Archive"))]


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
        window.results_view.results_table.setColumnWidth(18, 520)
        app.processEvents()
        window.close()

    loaded = load_settings()
    assert loaded.results_table_column_widths[3] == 280
    assert loaded.results_table_column_widths[18] == 520


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

        color_a = window.results_view.results_table.item(0, 0).background().color().name()
        color_b = window.results_view.results_table.item(2, 0).background().color().name()
        assert color_a != color_b

        window.results_view.apply_keep_strategy("larger")
        app.processEvents()
        # In group A both sizes are equal, tie breaks on quality/mtime/path; one row must remain unchecked.
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

        assert len(window._column_toggle_actions) == window.results_view.results_table.columnCount()
        window._column_toggle_actions[18].setChecked(False)
        app.processEvents()
        assert window.results_view.results_table.isColumnHidden(18)

        monkeypatch.setattr("video_duperz.ui.main_window.QInputDialog.getText", lambda *a, **k: ("Compact", True))
        window._save_current_view()
        assert "Compact" in window._saved_column_views

        window._column_toggle_actions[18].setChecked(True)
        app.processEvents()
        assert not window.results_view.results_table.isColumnHidden(18)

        window._apply_saved_view("Compact")
        app.processEvents()
        assert window.results_view.results_table.isColumnHidden(18)
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

        group_a = DuplicateGroup(
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
        )
        group_b = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(13, str(tmp_path / "beta" / "beta_keep_1.mp4"), 640, 360, 1200, 0.97, size=90),
                _dup_item(14, str(tmp_path / "beta" / "beta_keep_2.mp4"), 640, 360, 1100, 0.96, size=80),
                _dup_item(15, str(tmp_path / "beta" / "beta_skip_3.mp4"), 640, 360, 1000, 0.95, size=70),
            ],
            total_size_bytes=240,
            group_id=43,
        )
        group_c = DuplicateGroup(
            scan_id=1,
            profile="balanced",
            created_at="now",
            items=[
                _dup_item(16, str(tmp_path / "gamma" / "gamma_skip_huge.mp4"), 800, 450, 1500, 0.94, size=300),
                _dup_item(17, str(tmp_path / "gamma" / "gamma_keep_tiny.mp4"), 800, 450, 1400, 0.93, size=20),
            ],
            total_size_bytes=320,
            group_id=44,
        )

        window.results_view.load_groups([group_a, group_b, group_c])
        app.processEvents()

        assert len(window._sort_actions) == 8
        assert window.results_view.filter_include_name_edit is not None
        assert window.results_view.filter_include_path_edit is not None
        assert window.results_view.filter_exclude_name_edit is not None
        assert window.results_view.filter_exclude_path_edit is not None

        window._sort_actions[SORT_GROUP_SIZE_DESC].trigger()
        app.processEvents()
        assert "gamma" in window.results_view.results_table.item(0, 18).text().lower()

        window._sort_actions[SORT_GROUP_COUNT_DESC].trigger()
        app.processEvents()
        assert "beta" in window.results_view.results_table.item(0, 18).text().lower()

        window._sort_actions[SORT_ROW_SIZE_DESC].trigger()
        app.processEvents()
        first_group = window.results_view.results_table.item(0, 0).text()
        first_group_rows = [
            row
            for row in range(window.results_view.results_table.rowCount())
            if window.results_view.results_table.item(row, 0).text() == first_group
        ]
        first_group_sizes = [
            int(window.results_view.results_table.item(row, 5).text().replace(",", "")) for row in first_group_rows
        ]
        assert first_group_sizes == sorted(first_group_sizes, reverse=True)

        window.results_view.filter_include_name_edit.setText("KEEP")
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 4
        assert all(
            "keep" in Path(window.results_view.results_table.item(row, 18).text()).name.lower()
            for row in range(window.results_view.results_table.rowCount())
        )

        window.results_view.filter_exclude_path_edit.setText("gamma")
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 3
        assert all(
            "gamma" not in window.results_view.results_table.item(row, 18).text().lower()
            for row in range(window.results_view.results_table.rowCount())
        )

        window.results_view.filter_include_path_edit.setText("beta")
        app.processEvents()
        assert window.results_view.results_table.rowCount() == 2
        assert all(
            "beta" in window.results_view.results_table.item(row, 18).text().lower()
            for row in range(window.results_view.results_table.rowCount())
        )
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
        group_id = db.insert_duplicate_group(scan_id=scan_id, profile="balanced", total_size_bytes=223)
        db.insert_duplicate_item(group_id, _dup_item(file_a, str(tmp_path / "library" / "a.mp4"), 1920, 1080, 1000, 1.0, size=111))
        db.insert_duplicate_item(group_id, _dup_item(file_b, str(tmp_path / "library" / "b.mp4"), 1920, 1080, 900, 0.99, size=112))
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

        monkeypatch.setattr("video_duperz.ui.main_window.QInputDialog.getText", lambda *a, **k: ("My Library", True))
        window._save_current_scan_set_as()
        app.processEvents()
        assert "My Library" in window._saved_scan_profiles

        payload = window._saved_scan_profiles["My Library"]
        window._load_saved_scan_profile(payload, "My Library")
        app.processEvents()
        assert window.current_scan_id == scan_id
        assert window.tabs.currentWidget() == window.results_view
        assert window.results_view.results_table.rowCount() == 2
        roots_in_widget = [window.roots_list.item(i).text() for i in range(window.roots_list.count())]
        assert roots_in_widget == roots
        assert window.profile_combo.currentText() == "balanced"
        assert window.extensions_edit.text() == "mp4"
        assert "filesystem may have changed" in window.results_view.info_label.text().lower()

        window._refresh_saved_scans_menu()
        action_texts = [action.text() for action in window._saved_scans_menu.actions()]
        assert any(text.startswith("My Library | #") for text in action_texts)
        assert any(text.startswith("My Library | #") and text.endswith("| done") for text in action_texts)

        monkeypatch.setattr("video_duperz.ui.main_window.QInputDialog.getItem", lambda *a, **k: ("My Library", True))
        monkeypatch.setattr(
            "video_duperz.ui.main_window.QMessageBox.question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        window._delete_named_scan_profile()
        app.processEvents()
        assert "My Library" not in window._saved_scan_profiles

        window._refresh_saved_scans_menu()
        action_texts = [action.text() for action in window._saved_scans_menu.actions()]
        assert any(text.startswith("Auto:") for text in action_texts)
        window.close()


def test_load_saved_scan_profile_not_started_restores_sources_and_clears_results(tmp_path: Path) -> None:
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
        roots_in_widget = [window.roots_list.item(i).text() for i in range(window.roots_list.count())]
        assert roots_in_widget == pending_roots
        assert window.profile_combo.currentText() == "aggressive"
        assert window.extensions_edit.text() == "mkv, mp4"
        assert "not started" in window.statusBar().currentMessage().lower()
        window.close()


def test_load_saved_scan_profile_cancelled_latest_routes_to_sources(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        roots = [str(tmp_path / "library")]
        done_id = db.create_scan(profile="balanced", roots=roots, extensions=["mp4"])
        db.complete_scan(done_id, status="done")
        cancelled_id = db.create_scan(profile="balanced", roots=roots, extensions=["mp4"])
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


def test_saved_scans_menu_lists_not_started_named_and_cancelled_auto(tmp_path: Path) -> None:
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

        pending_action = next(action for action in actions if action.text().startswith("Pending Named | "))
        assert pending_action.isEnabled()
        assert pending_action.text().endswith("not started")
        assert any(text.startswith("Auto:") and text.endswith("| cancelled") for text in action_texts)
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

        menu_titles = [action.text().replace("&", "") for action in window.menuBar().actions()]
        assert menu_titles[:6] == ["File", "View", "Sort", "Actions", "Tools", "Help"]

        assert window.clear_recent_folders_action.text() == "C&lear Recent Folders"
        assert window.clear_saved_scans_action.text() == "Clear Sa&ved Scans"
        assert window.clear_cached_thumbnails_action.text() == "Clear Cached T&humbnails"
        assert window.full_reset_action.text() == "&Full Reset"
        assert window.edit_ini_action.text() == "Edit &.ini File"
        assert window.about_action.text() == "&Help"
        shortcuts = {seq.toString() for seq in window.exit_action.shortcuts()}
        assert {"Ctrl+Q", "Alt+X"} <= shortcuts
        tools_menu_action = next(
            action for action in window.menuBar().actions() if action.text().replace("&", "") == "Tools"
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

        scan_id = window.db.create_scan(profile="balanced", roots=[str(tmp_path)], extensions=["mp4"])
        window.db.complete_scan(scan_id, status="done")
        monkeypatch.setattr(
            "video_duperz.ui.main_window.QMessageBox.question",
            lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        close_called = {"value": False}
        monkeypatch.setattr(window, "close", lambda: close_called.__setitem__("value", True))
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


def test_sources_tab_drive_table_highlights_matches_and_tracks_parallel_total(tmp_path: Path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])

    def _fake_plan(roots: list[str], max_workers: int, drive_worker_overrides: dict[str, int] | None = None):
        _ = drive_worker_overrides
        matched = {"volume:a", "volume:b"} if roots else set()
        effective_workers = len(matched)
        return SimpleNamespace(
            matched_volume_identities=matched,
            requested_worker_target=max(max_workers, effective_workers),
            effective_total_workers=effective_workers,
        )

    monkeypatch.setattr(
        "video_duperz.ui.main_window.list_physical_drives",
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
    monkeypatch.setattr("video_duperz.ui.main_window.build_physical_drive_scan_plan", _fake_plan)

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
        assert window.sources_drive_table.item(2, 8).text() == "disk extent lookup failed"

        window.max_workers_spin.setValue(3)
        app.processEvents()
        assert "Requested workers: 3" in window.sources_drive_summary_label.text()
        assert "Effective workers: 2" in window.sources_drive_summary_label.text()
        assert "Caps applied" in window.sources_drive_summary_label.text()
        window.close()


def test_sources_tab_drive_table_shows_placeholder_when_no_local_drives(tmp_path: Path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("video_duperz.ui.main_window.list_physical_drives", lambda roots=None: [])
    monkeypatch.setattr(
        "video_duperz.ui.main_window.build_physical_drive_scan_plan",
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
        assert window.sources_drive_table.item(0, 0).text() == "(No local drives detected)"
        assert window.sources_drive_table.item(0, 8).text() == ""
        assert "Matched physical drives: 0" in window.sources_drive_summary_label.text()
        assert "Requested workers: 2" in window.sources_drive_summary_label.text()
        assert "Effective workers: 0" in window.sources_drive_summary_label.text()
        window.close()


def test_sources_tab_drive_workers_and_probe_mode_persist_across_reload(tmp_path: Path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(
        "video_duperz.ui.main_window.list_physical_drives",
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

    def _fake_plan(roots: list[str], max_workers: int, drive_worker_overrides: dict[str, int] | None = None):
        matched = {"volume:a", "volume:b"} if roots else set()
        overrides = drive_worker_overrides or {}
        effective = 0
        if roots:
            effective = max(1, int(overrides.get("volume:a", 1))) + max(1, int(overrides.get("volume:b", 1)))
        return SimpleNamespace(
            matched_volume_identities=matched,
            requested_worker_target=max(max_workers, effective),
            effective_total_workers=effective,
        )

    monkeypatch.setattr("video_duperz.ui.main_window.build_physical_drive_scan_plan", _fake_plan)

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
        window.probe_mode_combo.setCurrentText("burst")
        app.processEvents()

        assert "Requested workers: 6" in window.sources_drive_summary_label.text()
        assert "Effective workers: 6" in window.sources_drive_summary_label.text()
        window.close()

    loaded = load_settings()
    assert loaded.drive_worker_overrides == {"volume:a": 4, "volume:b": 2}
    assert loaded.probe_worker_mode == "burst"

    with Database(tmp_path / "app.db") as db:
        reloaded_window = MainWindow(db=db, settings=loaded)
        reloaded_window.show()
        app.processEvents()

        assert reloaded_window.probe_mode_combo.currentText() == "burst"
        reloaded_spin_a = reloaded_window.sources_drive_table.cellWidget(0, 6)
        reloaded_spin_b = reloaded_window.sources_drive_table.cellWidget(1, 6)
        assert isinstance(reloaded_spin_a, QSpinBox)
        assert isinstance(reloaded_spin_b, QSpinBox)
        assert reloaded_spin_a.value() == 4
        assert reloaded_spin_b.value() == 2
        reloaded_window.close()


def test_scan_running_locks_ui_to_scan_tab_until_finished(tmp_path: Path, monkeypatch) -> None:
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
            "video_duperz.ui.main_window.build_physical_drive_scan_plan",
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

        finished_scan_id = db.create_scan(profile="balanced", roots=[str(tmp_path)], extensions=["mp4"])
        db.complete_scan(finished_scan_id, status="done")
        window._scan_finished(SimpleNamespace(scan_id=finished_scan_id, issues=[]))
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

        cancelled_scan_id = db.create_scan(profile="balanced", roots=[str(tmp_path)], extensions=["mp4"])
        db.complete_scan(cancelled_scan_id, status="cancelled")
        window.tabs.setCurrentWidget(window.scan_view)
        app.processEvents()

        window._scan_finished(SimpleNamespace(scan_id=cancelled_scan_id, issues=[]))
        app.processEvents()

        assert window.tabs.currentWidget() == window.scan_view
        assert window.current_scan_id is None
        assert "cancelled" in window.statusBar().currentMessage().lower()
        window.close()


def test_rescan_uses_current_sources_and_runs_cleanup_then_start(tmp_path: Path, monkeypatch) -> None:
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
            return {"deleted_scans": 3, "deleted_files": 8, "deleted_groups": 2, "deleted_actions": 1}

        starts = {"count": 0}
        monkeypatch.setattr(
            "video_duperz.ui.main_window.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )
        monkeypatch.setattr(window.db, "purge_for_fresh_rescan", _fake_purge)
        monkeypatch.setattr(window, "_clear_cached_thumbnails_internal", lambda: (5, 1))
        monkeypatch.setattr(window, "_start_scan", lambda: starts.__setitem__("count", int(starts["count"]) + 1))

        window._rescan_scan()
        app.processEvents()

        expected_roots = normalize_roots_for_display([root_a, root_b])
        expected_key = build_scan_set_key(roots=expected_roots, similarity_profile="aggressive", extensions=["mp4", "mkv"])
        assert called["roots"] == expected_roots
        assert called["scan_set_key"] == expected_key
        assert starts["count"] == 1
        assert window.current_scan_id is None
        assert window.results_view.results_table.rowCount() == 0
        window.close()


def test_rescan_cancelled_confirmation_does_not_purge_or_start(tmp_path: Path, monkeypatch) -> None:
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
            "video_duperz.ui.main_window.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.StandardButton.No,
        )
        monkeypatch.setattr(
            window.db,
            "purge_for_fresh_rescan",
            lambda *args, **kwargs: calls.__setitem__("purge", int(calls["purge"]) + 1),
        )
        monkeypatch.setattr(window, "_start_scan", lambda: calls.__setitem__("start", int(calls["start"]) + 1))

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


def test_scan_view_renders_lane_snapshots_and_worker_utilization(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    with Database(tmp_path / "app.db") as db:
        settings = default_settings()
        settings.scan_roots = [str(tmp_path)]
        window = MainWindow(db=db, settings=settings)
        window.show()
        app.processEvents()

        window.scan_view.initialize_lane_plan([["R:/Videos"], ["S:/Archive"]], worker_limit=2)
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
                cache_hit_ratio=0.4,
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
        assert "Worker status is shown per lane" in window.scan_view.worker_hint_label.text()
        assert window.scan_view.lane_table.rowCount() == 2
        assert window.scan_view.lane_table.columnCount() == 12
        assert window.scan_view.lane_table.item(0, 2).text() == "running"
        assert window.scan_view.lane_table.item(0, 6).text() == "R:/Videos/a.mp4"
        assert window.scan_view.lane_table.item(0, 8).text() == "1.20"
        assert window.scan_view.lane_table.item(0, 9).text() == "2.40"
        assert window.scan_view.lane_table.item(0, 10).text() == "0.40"
        assert window.scan_view.lane_table.item(0, 11).text() == "0.80"
        assert window.scan_view.lane_table.item(0, 0).background().color().name().lower() == "#f0f8ff"
        assert window.scan_view.lane_table.item(1, 2).text() == "idle"
        assert window.scan_view.lane_table.item(1, 5).text() == "2"
        assert window.scan_view.lane_table.item(1, 0).background().color().name().lower() == "#f5f5f5"
        assert "cache hit 40.0%" in window.scan_view.io_stats_label.text()
        assert window.scan_view.rescan_btn.text() == "Rescan"
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

        assert window.scan_view.lane_table.item(0, 0).background().color().name().lower() == "#f5f5dc"
        assert window.scan_view.lane_table.item(1, 0).background().color().name().lower() == "#f5f5dc"
        assert window.scan_view.lane_table.item(2, 0).background().color().name().lower() == "#f0f8ff"
        assert window.scan_view.lane_table.item(3, 0).background().color().name().lower() == "#f5f5f5"
        assert window.scan_view.lane_table.item(4, 0).background().color().name().lower() == "#f0fff0"
        assert window.scan_view.lane_table.item(5, 0).background().color().name().lower() == "#ffe4e1"
        window.close()
