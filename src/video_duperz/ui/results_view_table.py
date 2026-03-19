"""Table rendering and group ordering helpers for the results view."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QTableWidgetItem

from .results_view_base import ResultsViewBase
from .results_view_shared import (
    COL_AUDIO_BITRATE,
    COL_AUDIO_CODEC,
    COL_AUDIO_LANGS,
    COL_BITRATE,
    COL_CHECK,
    COL_DURATION,
    COL_EXTENSION,
    COL_FILE_NAME,
    COL_FULL_PATH,
    COL_GROUP_ID,
    COL_HDR,
    COL_IDENTICAL,
    COL_LAST_MODIFIED,
    COL_MATCH,
    COL_PARENT_DIR,
    COL_RESOLUTION,
    COL_SIMILARITY,
    COL_SIZE,
    COL_SUB_LANGS,
    COL_THUMB,
    COL_VIDEO_CODEC,
    HDR_FILTER_EXCLUDE,
    HDR_FILTER_ONLY,
    META_ROLE,
    SORT_GROUP_COUNT_ASC,
    SORT_GROUP_COUNT_DESC,
    SORT_GROUP_SIZE_ASC,
    SORT_GROUP_SIZE_DESC,
    SORT_GROUP_SPREAD_ASC,
    SORT_GROUP_SPREAD_DESC,
    SORT_ROW_SIZE_ASC,
    SORT_ROW_SIZE_DESC,
    GroupRenderContext,
    RowMeta,
)

if TYPE_CHECKING:
    from ..models import DuplicateGroup, DuplicateItem


class ResultsViewTableMixin(ResultsViewBase):
    """Results-table rendering, filtering, and sort helper methods."""

    def _invalidate_thumbnail_token(self) -> None: ...

    def _clear_group_compare_row_state(self) -> None: ...

    def _clear_group_compare_pending(self) -> None: ...

    @staticmethod
    def _fmt_mtime(mtime_ns: int) -> str: ...

    def _apply_row_style(self, row: int, group_index: int, bold: bool) -> None: ...

    def _queue_thumbnail(
        self,
        row_token: str,
        row: int,
        file_id: int,
        path: str,
        size: int,
        mtime_ns: int,
    ) -> None: ...

    def _quality_score_for_item(self, item: DuplicateItem) -> float: ...

    def _rebuild_results_table(self) -> None:
        """Rebuild all currently visible result rows from the active groups."""
        self._invalidate_thumbnail_token()
        self._clear_group_compare_row_state()
        self._clear_group_compare_pending()
        self.results_table.setRowCount(0)
        self._rebuilding_table = True
        try:
            display_groups = self._display_groups()
            for group_index, group in enumerate(display_groups, start=1):
                render_ctx = self._build_group_render_context(group, group_index)
                for item in group.items:
                    self._populate_results_row(render_ctx, item)
        finally:
            self._rebuilding_table = False
        if self._column_widths:
            self._set_table_column_widths(self._column_widths)
        else:
            self.results_table.resizeColumnsToContents()
            self._column_widths = self._capture_column_widths()

        self._update_info_label()
        QTimer.singleShot(0, self._schedule_visible_groups_for_compare)

    def _build_group_render_context(
        self,
        group: DuplicateGroup,
        group_index: int,
    ) -> GroupRenderContext:
        """Build cached render state for one displayed duplicate group."""
        group_key = self._group_key(group)
        self._group_rows_visible.setdefault(group_key, [])
        return GroupRenderContext(
            group_index=group_index,
            group_db_id=int(group.group_id or 0),
            display_group_id=f"G{group_index:04d}",
            group_key=group_key,
            cached_labels=self._group_compare_cached_labels.get(group_key, {}),
            cached_errors=self._group_compare_cached_errors.get(group_key, {}),
            cached_group_error=self._group_compare_cached_group_error.get(
                group_key,
                "",
            ),
        )

    def _build_row_meta(
        self,
        render_ctx: GroupRenderContext,
        item: DuplicateItem,
    ) -> RowMeta:
        """Build the metadata payload attached to one rendered results row."""
        return RowMeta(
            group_db_id=render_ctx.group_db_id,
            file_id=item.file_id,
            path=item.path,
            size=item.size,
            mtime_ns=item.mtime_ns,
            width=item.width,
            height=item.height,
            codec=item.codec,
            bitrate=item.bitrate,
            similarity=item.similarity_score,
            keep_default=item.keep_default,
        )

    def _populate_results_row(
        self,
        render_ctx: GroupRenderContext,
        item: DuplicateItem,
    ) -> None:
        """Append one duplicate item as a fully populated table row."""
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        self.results_table.setRowHeight(row, self._thumbnail_h + 8)

        meta = self._build_row_meta(render_ctx, item)
        group_item = QTableWidgetItem(render_ctx.display_group_id)
        group_item.setData(META_ROLE, meta)
        self.results_table.setItem(row, COL_GROUP_ID, group_item)

        check_item = QTableWidgetItem("")
        check_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        check_item.setCheckState(
            Qt.CheckState.Checked
            if item.file_id in self._checked_file_ids
            else Qt.CheckState.Unchecked
        )
        self.results_table.setItem(row, COL_CHECK, check_item)

        identical_item = QTableWidgetItem(
            render_ctx.cached_labels.get(item.file_id, "")
        )
        identical_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        tooltip = (
            render_ctx.cached_errors.get(item.file_id, "")
            or render_ctx.cached_group_error
        )
        if tooltip:
            identical_item.setToolTip(tooltip)
        self.results_table.setItem(row, COL_IDENTICAL, identical_item)

        thumb_item = QTableWidgetItem(
            "Loading..." if self._thumbnails_enabled else "N/A"
        )
        thumb_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results_table.setItem(row, COL_THUMB, thumb_item)

        file_path = Path(item.path)
        if item.match_reason == "trimmed_match":
            match_label = "Trimmed"
        elif item.match_reason == "audio_match":
            match_label = "Audio"
        else:
            match_label = "Perceptual"
        row_items = {
            COL_FILE_NAME: QTableWidgetItem(file_path.name),
            COL_EXTENSION: QTableWidgetItem(
                self._normalized_extension_value(item.path)
            ),
            COL_SIZE: QTableWidgetItem(f"{item.size:,}"),
            COL_RESOLUTION: QTableWidgetItem(f"{item.width}x{item.height}"),
            COL_DURATION: QTableWidgetItem(f"{item.duration_s:.1f}s"),
            COL_VIDEO_CODEC: QTableWidgetItem(item.codec),
            COL_AUDIO_CODEC: QTableWidgetItem(item.audio_codec or ""),
            COL_AUDIO_BITRATE: QTableWidgetItem(str(item.audio_bitrate)),
            COL_AUDIO_LANGS: QTableWidgetItem(item.audio_languages or ""),
            COL_SUB_LANGS: QTableWidgetItem(item.subtitle_languages or ""),
            COL_HDR: QTableWidgetItem("Yes" if item.is_hdr else "No"),
            COL_BITRATE: QTableWidgetItem(str(item.bitrate)),
            COL_SIMILARITY: QTableWidgetItem(f"{item.similarity_score:.3f}"),
            COL_MATCH: QTableWidgetItem(match_label),
            COL_LAST_MODIFIED: QTableWidgetItem(self._fmt_mtime(item.mtime_ns)),
            COL_PARENT_DIR: QTableWidgetItem(str(file_path.parent)),
            COL_FULL_PATH: QTableWidgetItem(item.path),
        }
        for column, table_item in row_items.items():
            if column == COL_MATCH:
                table_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if item.match_reason == "trimmed_match":
                    table_item.setToolTip(
                        f"Trimmed match. Delta t {item.match_duration_delta_s:.1f}s"
                    )
                elif item.match_reason == "audio_match":
                    table_item.setToolTip("Audio fingerprint rescue match.")
                else:
                    table_item.setToolTip("Perceptual match.")
            self.results_table.setItem(row, column, table_item)

        self._row_group_keys[row] = render_ctx.group_key
        self._group_rows_visible[render_ctx.group_key].append(row)
        self._apply_row_style(
            row=row,
            group_index=render_ctx.group_index,
            bold=item.keep_default,
        )
        self._thumbnail_rows[item.file_id] = row
        self._queue_thumbnail(
            row_token=self._thumbnail_token,
            row=row,
            file_id=item.file_id,
            path=item.path,
            size=item.size,
            mtime_ns=item.mtime_ns,
        )

    def _display_groups(self) -> list[DuplicateGroup]:
        """Return groups after applying the active filters and sort mode."""
        filtered = self._filtered_groups(self._groups)
        if self._sort_mode == SORT_GROUP_SIZE_DESC:
            return sorted(
                filtered,
                key=lambda group: (
                    -self._group_total_size(group),
                    -len(group.items),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_GROUP_SIZE_ASC:
            return sorted(
                filtered,
                key=lambda group: (
                    self._group_total_size(group),
                    len(group.items),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_GROUP_COUNT_DESC:
            return sorted(
                filtered,
                key=lambda group: (
                    -len(group.items),
                    -self._group_total_size(group),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_GROUP_COUNT_ASC:
            return sorted(
                filtered,
                key=lambda group: (
                    len(group.items),
                    self._group_total_size(group),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_ROW_SIZE_DESC:
            return [
                replace(
                    group,
                    items=self._sorted_group_items(group.items, larger_first=True),
                )
                for group in filtered
            ]
        if self._sort_mode == SORT_ROW_SIZE_ASC:
            return [
                replace(
                    group,
                    items=self._sorted_group_items(group.items, larger_first=False),
                )
                for group in filtered
            ]
        if self._sort_mode == SORT_GROUP_SPREAD_DESC:
            return sorted(
                filtered,
                key=lambda group: (
                    -self._group_size_spread(group),
                    -len(group.items),
                    -self._group_total_size(group),
                    self._group_tiebreak(group),
                ),
            )
        if self._sort_mode == SORT_GROUP_SPREAD_ASC:
            return sorted(
                filtered,
                key=lambda group: (
                    self._group_size_spread(group),
                    len(group.items),
                    self._group_total_size(group),
                    self._group_tiebreak(group),
                ),
            )
        return filtered

    def _filtered_groups(self, groups: list[DuplicateGroup]) -> list[DuplicateGroup]:
        """Return visible groups after applying group-aware filter semantics."""
        filtered_groups: list[DuplicateGroup] = []
        for group in groups:
            items = self._visible_group_items(group)
            if len(items) < 2:
                continue
            filtered_groups.append(replace(group, items=items))
        return filtered_groups

    def _visible_group_items(self, group: DuplicateGroup) -> list[DuplicateItem]:
        """Return the visible items for one group under the active filter state."""
        candidate_items = [
            item for item in group.items if self._item_matches_exclude_filters(item)
        ]
        if len(candidate_items) < 2:
            return []
        filter_state = self._filter_state
        if not filter_state.has_include_filters():
            return candidate_items
        include_matches = [
            item for item in candidate_items if self._item_matches_include_filters(item)
        ]
        if filter_state.include_match_all:
            if len(include_matches) != len(candidate_items):
                return []
            return candidate_items
        return candidate_items if include_matches else []

    def _item_matches_include_filters(self, item: DuplicateItem) -> bool:
        """Return whether one duplicate item satisfies all include-style filters."""
        file_name = Path(item.path).name.casefold()
        full_path = item.path.casefold()
        return self._matches_include_text_filters(
            file_name=file_name,
            full_path=full_path,
        ) and self._matches_include_structured_filters(item)

    def _item_matches_exclude_filters(self, item: DuplicateItem) -> bool:
        """Return whether one duplicate item survives the exclude-only filters."""
        file_name = Path(item.path).name.casefold()
        full_path = item.path.casefold()
        return self._matches_exclude_text_filters(
            file_name=file_name,
            full_path=full_path,
        )

    def _matches_include_text_filters(self, *, file_name: str, full_path: str) -> bool:
        """Return whether include text filters accept one result item."""
        filter_state = self._filter_state
        return not (
            (
                filter_state.include_name_terms
                and not any(
                    term in file_name for term in filter_state.include_name_terms
                )
            )
            or (
                filter_state.include_path_terms
                and not any(
                    term in full_path for term in filter_state.include_path_terms
                )
            )
        )

    def _matches_exclude_text_filters(self, *, file_name: str, full_path: str) -> bool:
        """Return whether exclude text filters keep one result item visible."""
        filter_state = self._filter_state
        if filter_state.exclude_name_terms and any(
            term in file_name for term in filter_state.exclude_name_terms
        ):
            return False
        return not any(term in full_path for term in filter_state.exclude_path_terms)

    def _matches_include_structured_filters(self, item: DuplicateItem) -> bool:
        """Return whether include-style structured filters accept one result item."""
        filter_state = self._filter_state
        if filter_state.min_size_mib is not None and (
            item.size < int(filter_state.min_size_mib * 1024.0 * 1024.0)
        ):
            return False
        if filter_state.max_size_mib is not None and (
            item.size > int(filter_state.max_size_mib * 1024.0 * 1024.0)
        ):
            return False
        if (
            filter_state.min_duration_s is not None
            and item.duration_s < filter_state.min_duration_s
        ):
            return False
        if (
            filter_state.max_duration_s is not None
            and item.duration_s > filter_state.max_duration_s
        ):
            return False
        if (
            filter_state.min_similarity is not None
            and item.similarity_score < filter_state.min_similarity
        ):
            return False
        if filter_state.min_width is not None and item.width < filter_state.min_width:
            return False
        if (
            filter_state.min_height is not None
            and item.height < filter_state.min_height
        ):
            return False
        if (
            filter_state.extension
            and self._normalized_extension_value(item.path) != filter_state.extension
        ):
            return False
        if (
            filter_state.video_codec
            and self._normalized_codec_value(item.codec) != filter_state.video_codec
        ):
            return False
        if filter_state.hdr_mode == HDR_FILTER_ONLY and not item.is_hdr:
            return False
        return not (filter_state.hdr_mode == HDR_FILTER_EXCLUDE and item.is_hdr)

    def _sorted_group_items(
        self,
        items: list[DuplicateItem],
        larger_first: bool,
    ) -> list[DuplicateItem]:
        """Return group items sorted by size and fallback quality heuristics."""
        if larger_first:
            return sorted(
                items,
                key=lambda item: (
                    -item.size,
                    -self._quality_score_for_item(item),
                    item.mtime_ns,
                    item.path.lower(),
                ),
            )
        return sorted(
            items,
            key=lambda item: (
                item.size,
                -self._quality_score_for_item(item),
                item.mtime_ns,
                item.path.lower(),
            ),
        )

    @staticmethod
    def _group_total_size(group: DuplicateGroup) -> int:
        """Return the total size in bytes for one duplicate group."""
        return sum(item.size for item in group.items)

    @staticmethod
    def _group_size_spread(group: DuplicateGroup) -> int:
        """Return the size delta between the largest and smallest group member."""
        if not group.items:
            return 0
        sizes = [item.size for item in group.items]
        return max(sizes) - min(sizes)

    @staticmethod
    def _group_tiebreak(group: DuplicateGroup) -> str:
        """Return the stable lexical path tie-break key for one group."""
        if not group.items:
            return ""
        return min(item.path.casefold() for item in group.items)

    def _group_key(self, group: DuplicateGroup) -> str:
        """Return a stable key for a group even before it has a DB id."""
        group_db_id = int(group.group_id or 0)
        if group_db_id > 0:
            return f"id:{group_db_id}"
        file_ids = sorted(int(item.file_id) for item in group.items)
        return "files:" + ",".join(str(file_id) for file_id in file_ids)

    def _build_group_compare_payloads(
        self,
        groups: list[DuplicateGroup],
    ) -> dict[str, list[dict[str, object]]]:
        """Build the worker payloads used for deferred identical compares."""
        payloads: dict[str, list[dict[str, object]]] = {}
        for group in groups:
            group_key = self._group_key(group)
            payloads[group_key] = [
                {
                    "file_id": int(item.file_id),
                    "path": str(item.path),
                    "size": int(item.size),
                }
                for item in group.items
            ]
        return payloads
