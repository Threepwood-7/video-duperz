"""Deferred identical-compare queue helpers for the results view."""

from __future__ import annotations

from collections import deque
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidgetItem

from .results_view_shared import COL_IDENTICAL, RowMeta, coerce_int, payload_dict
from .results_view_table import ResultsViewTableMixin
from .workers import ExactMatchGroupWorker


class ResultsViewCompareMixin(ResultsViewTableMixin):
    """Deferred identical-compare queue and result application helpers."""

    def _row_meta(self, row: int) -> RowMeta | None: ...

    def _invalidate_group_compare_dataset(self) -> None:
        """Invalidate all compare-worker state for the current group dataset."""
        self._dataset_serial += 1
        self._dataset_token = f"groups-{self._dataset_serial}"
        self._reset_group_compare_cache()
        self._clear_group_compare_row_state()
        self._clear_group_compare_pending()
        self._group_compare_running_key = None

    def _reset_group_compare_cache(self) -> None:
        """Clear the cached compare labels and per-group error state."""
        self._group_compare_cached_labels = {}
        self._group_compare_cached_errors = {}
        self._group_compare_cached_group_error = {}

    def _clear_group_compare_row_state(self) -> None:
        """Forget row-to-group mappings for the current rendered table."""
        self._row_group_keys = {}
        self._group_rows_visible = {}

    def _clear_group_compare_pending(self) -> None:
        """Drop the queued groups waiting for compare workers."""
        self._group_compare_pending = deque()
        self._group_compare_pending_set = set()

    def _cancel_group_compare_queue(self) -> None:
        """Cancel any queued identical-compare work."""
        self._clear_group_compare_pending()
        self._group_compare_running_key = None

    def _on_results_scrolled(self, _value: int) -> None:
        """Schedule compare work for newly visible rows while scrolling."""
        self._schedule_visible_groups_for_compare()

    def _schedule_visible_groups_for_compare(self) -> None:
        """Queue compare workers for the groups visible in the viewport."""
        if self.results_table.rowCount() <= 0:
            return
        viewport = self.results_table.viewport()
        if viewport.height() <= 0:
            return
        first_row = self.results_table.rowAt(0)
        if first_row < 0:
            first_row = 0
        last_row = self.results_table.rowAt(max(0, viewport.height() - 1))
        if last_row < 0:
            last_row = self.results_table.rowCount() - 1

        for row in range(first_row, last_row + 1):
            group_key = self._row_group_keys.get(row)
            if not group_key:
                continue
            if (
                group_key in self._group_compare_cached_labels
                or group_key in self._group_compare_cached_group_error
            ):
                continue
            if (
                group_key == self._group_compare_running_key
                or group_key in self._group_compare_pending_set
            ):
                continue
            payload = self._group_compare_payloads.get(group_key, [])
            if len(payload) < 2:
                self._group_compare_cached_labels[group_key] = {}
                self._group_compare_cached_errors[group_key] = {}
                continue
            self._group_compare_pending.append(group_key)
            self._group_compare_pending_set.add(group_key)

        self._start_next_group_compare()

    def _start_next_group_compare(self) -> None:
        """Start the next queued identical-compare worker, if any."""
        if self._group_compare_running_key is not None:
            return
        while self._group_compare_pending:
            group_key = self._group_compare_pending.popleft()
            self._group_compare_pending_set.discard(group_key)
            if (
                group_key in self._group_compare_cached_labels
                or group_key in self._group_compare_cached_group_error
            ):
                continue
            payload = self._group_compare_payloads.get(group_key, [])
            if len(payload) < 2:
                self._group_compare_cached_labels[group_key] = {}
                self._group_compare_cached_errors[group_key] = {}
                continue

            worker = ExactMatchGroupWorker(
                row_token=self._dataset_token,
                group_key=group_key,
                files=payload,
                block_mib=self._identical_block_mib,
                sample_a_pct=self._identical_sample_a_pct,
                sample_b_pct=self._identical_sample_b_pct,
            )
            worker.signals.ready.connect(self._on_group_compare_ready)
            worker.signals.error.connect(self._on_group_compare_error)
            self._group_compare_workers[worker.worker_id] = worker
            self._group_compare_running_key = group_key
            self._thread_pool.start(worker)
            return

    def _on_group_compare_ready(self, payload: Any) -> None:
        """Apply a successful exact-match worker result."""
        payload_map = payload_dict(payload)
        if not payload_map:
            return
        worker_id = coerce_int(payload_map.get("worker_id", 0))
        self._group_compare_workers.pop(worker_id, None)

        token = str(payload_map.get("token", ""))
        if token != self._dataset_token:
            return

        group_key = str(payload_map.get("group_key", ""))
        raw_labels = payload_dict(payload_map.get("labels", {}))
        raw_errors = payload_dict(payload_map.get("errors", {}))
        labels: dict[int, str] = {}
        errors: dict[int, str] = {}
        for raw_file_id, raw_label in raw_labels.items():
            file_id = coerce_int(raw_file_id, -1)
            if file_id < 0:
                continue
            labels[file_id] = str(raw_label or "")
        for raw_file_id, raw_error in raw_errors.items():
            file_id = coerce_int(raw_file_id, -1)
            if file_id < 0:
                continue
            error_text = str(raw_error or "").strip()
            if error_text:
                errors[file_id] = error_text

        self._group_compare_cached_labels[group_key] = labels
        self._group_compare_cached_errors[group_key] = errors
        self._group_compare_cached_group_error.pop(group_key, None)
        self._group_compare_running_key = None
        self._apply_group_compare_result(group_key)
        self._start_next_group_compare()

    def _on_group_compare_error(self, payload: Any) -> None:
        """Apply a failed exact-match worker result."""
        payload_map = payload_dict(payload)
        if not payload_map:
            return
        worker_id = coerce_int(payload_map.get("worker_id", 0))
        self._group_compare_workers.pop(worker_id, None)

        token = str(payload_map.get("token", ""))
        if token != self._dataset_token:
            return

        group_key = str(payload_map.get("group_key", ""))
        self._group_compare_cached_labels[group_key] = {}
        self._group_compare_cached_errors[group_key] = {}
        self._group_compare_cached_group_error[group_key] = str(
            payload_map.get("message", "")
        ).strip()
        self._group_compare_running_key = None
        self._apply_group_compare_result(group_key)
        self._start_next_group_compare()

    def _apply_group_compare_result(self, group_key: str) -> None:
        """Update visible rows for one completed identical-compare group."""
        rows = self._group_rows_visible.get(group_key, [])
        if not rows:
            return
        labels = self._group_compare_cached_labels.get(group_key, {})
        errors = self._group_compare_cached_errors.get(group_key, {})
        group_error = self._group_compare_cached_group_error.get(group_key, "")
        for row in rows:
            meta = self._row_meta(row)
            if meta is None:
                continue
            item = self.results_table.item(row, COL_IDENTICAL)
            if item is None:
                item = QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.results_table.setItem(row, COL_IDENTICAL, item)
            item.setText(labels.get(meta.file_id, ""))
            tooltip = errors.get(meta.file_id, "") or group_error
            item.setToolTip(tooltip)
