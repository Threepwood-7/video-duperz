"""Tests for video_duperz."""

import importlib


def test_package_importable() -> None:
    assert importlib.import_module("video_duperz")
