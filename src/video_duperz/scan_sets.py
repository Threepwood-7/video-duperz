from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .models import SimilarityProfile


def normalize_similarity_profile(value: str) -> SimilarityProfile:
    text = str(value).strip().lower()
    if text in {"balanced", "conservative", "aggressive"}:
        return cast("SimilarityProfile", text)
    return "balanced"


def normalize_extensions(extensions: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in extensions:
        ext = str(raw).strip().lower().lstrip(".")
        if not ext or ext in seen:
            continue
        seen.add(ext)
        normalized.append(ext)
    return normalized


def normalize_roots_for_display(roots: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in roots:
        text = str(Path(str(raw)).expanduser()).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def canonical_roots(roots: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in roots:
        text = str(Path(str(raw)).expanduser()).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    normalized.sort()
    return normalized


def build_scan_set_spec(
    roots: list[str], similarity_profile: str, extensions: list[str]
) -> dict[str, object]:
    ext = normalize_extensions(extensions)
    ext.sort()
    return {
        "roots": canonical_roots(roots),
        "similarity_profile": normalize_similarity_profile(similarity_profile),
        "extensions": ext,
    }


def build_scan_set_key(
    roots: list[str], similarity_profile: str, extensions: list[str]
) -> str:
    spec = build_scan_set_spec(
        roots=roots, similarity_profile=similarity_profile, extensions=extensions
    )
    return json.dumps(spec, separators=(",", ":"), sort_keys=True)
