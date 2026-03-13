"""Shared SQLite helper utilities used by the database mixin modules."""

from __future__ import annotations

import json
import sqlite3
import struct
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .models import ActionKind


def encode_hashes(hashes: list[int]) -> bytes:
    """Pack fingerprint hashes into the database blob format.

    Args:
        hashes: Unsigned hash values to persist.

    Returns:
        Packed blob bytes suitable for SQLite storage.
    """
    if not hashes:
        return b""
    return struct.pack(f">{len(hashes)}Q", *hashes)


def decode_hashes(blob: bytes | None) -> list[int]:
    """Unpack a fingerprint blob into individual hash values.

    Args:
        blob: Packed blob value loaded from SQLite.

    Returns:
        Decoded hash list, or an empty list for invalid input.
    """
    if not blob:
        return []
    if len(blob) % 8 != 0:
        return []
    count = len(blob) // 8
    return list(struct.unpack(f">{count}Q", blob))


def coerce_int(value: object, default: int = 0) -> int:
    """Convert a loosely typed SQLite payload value into an ``int``.

    Args:
        value: Raw value to normalize.
        default: Fallback value when coercion fails.

    Returns:
        Normalized integer value.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def coerce_string_list(value: object) -> list[str]:
    """Normalize a JSON-decoded list-like payload into clean strings.

    Args:
        value: Raw decoded JSON payload.

    Returns:
        Non-empty trimmed string items.
    """
    if not isinstance(value, list):
        return []
    raw_items = cast("list[object]", value)
    normalized: list[str] = []
    for item in raw_items:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def decode_string_list_json(value: object) -> list[str]:
    """Decode a JSON string field that stores string lists.

    Args:
        value: Raw SQLite field value.

    Returns:
        Decoded normalized string items.
    """
    if not isinstance(value, str | bytes | bytearray):
        return []
    try:
        payload: object = json.loads(value) if value else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return coerce_string_list(payload)


def normalize_action_kind(value: object) -> ActionKind:
    """Normalize persisted action text into a supported action kind.

    Args:
        value: Raw persisted action string.

    Returns:
        Supported action kind, defaulting to ``"keep"``.
    """
    text = str(value).strip().lower()
    if text in {"keep", "rename", "delete"}:
        return cast("ActionKind", text)
    return "keep"


def require_lastrowid(cursor: sqlite3.Cursor) -> int:
    """Return ``cursor.lastrowid`` or raise when SQLite omitted it.

    Args:
        cursor: SQLite cursor produced by an insert statement.

    Returns:
        Inserted row id.

    Raises:
        sqlite3.DatabaseError: If SQLite did not expose a row id.
    """
    if cursor.lastrowid is None:
        raise sqlite3.DatabaseError("sqlite cursor did not return lastrowid")
    return int(cursor.lastrowid)
