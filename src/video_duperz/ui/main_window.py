"""Public main window entry point composed from focused mixin modules."""

from __future__ import annotations

from .main_window_scan_actions import MainWindowScanActionMixin


class MainWindow(MainWindowScanActionMixin):
    """Top-level application window that coordinates sources, scan, and results."""
