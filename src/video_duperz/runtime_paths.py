from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings

SETTINGS_ORG_NAME = "ThreepSoftwz"
SETTINGS_APP_NAME = "video_duperz"


def _resolve_windows_home_dir(
    primary_env: str,
    secondary_env: str | None,
    fallback_parts: tuple[str, ...],
) -> Path:
    primary = str(os.getenv(primary_env, "")).strip()
    if primary:
        return Path(primary).expanduser()
    if secondary_env:
        secondary = str(os.getenv(secondary_env, "")).strip()
        if secondary:
            return Path(secondary).expanduser()
    return Path.home().joinpath(*fallback_parts)


def resolve_config_root(override_dir: str | None = None) -> Path:
    override = str(override_dir or os.getenv("CONFIG_DIR", "")).strip()
    if override:
        root = Path(override).expanduser()
    else:
        root = _resolve_windows_home_dir(
            "APPDATA",
            "LOCALAPPDATA",
            ("AppData", "Roaming"),
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_data_root(override_dir: str | None = None) -> Path:
    override = str(override_dir or os.getenv("DATA_DIR", "")).strip()
    if override:
        root = Path(override).expanduser()
    else:
        base = _resolve_windows_home_dir(
            "LOCALAPPDATA",
            "APPDATA",
            ("AppData", "Local"),
        )
        root = base / SETTINGS_ORG_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_app_data_dir(override_dir: str | None = None) -> Path:
    app_root = resolve_data_root(override_dir) / SETTINGS_APP_NAME
    app_root.mkdir(parents=True, exist_ok=True)
    return app_root


def configure_qsettings(config_dir_override: str | None = None) -> Path:
    config_root = resolve_config_root(config_dir_override)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(config_root),
    )
    return config_root
