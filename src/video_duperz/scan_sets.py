"""Normalization helpers for comparing and persisting scan-set selections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .models import CrossResolutionMode, SimilarityProfile


def normalize_similarity_profile(value: str) -> SimilarityProfile:
    """Normalize user input to one of the supported similarity profiles."""
    text = str(value).strip().lower()
    if text in {"balanced", "conservative", "aggressive", "custom"}:
        return cast("SimilarityProfile", text)
    return "balanced"


def normalize_cross_resolution_mode(value: object) -> CrossResolutionMode:
    """Normalize one cross-resolution mode to the supported literal set."""
    text = str(value or "").strip().lower()
    if text in {"same_aspect", "any_aspect"}:
        return cast("CrossResolutionMode", text)
    return "off"


def normalize_custom_similarity_threshold(value: object) -> float:
    """Clamp one custom similarity threshold to the supported UI range."""
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.18
    return max(0.01, min(0.30, parsed))


def normalize_extensions(extensions: list[str]) -> list[str]:
    """Normalize extension strings and preserve their first-seen order."""
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
    """Normalize scan roots for display while preserving user-facing casing."""
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
    """Normalize scan roots into a stable case-folded list for comparisons."""
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
    roots: list[str],
    similarity_profile: str,
    extensions: list[str],
    *,
    custom_similarity_threshold: float = 0.18,
    scene_aware_sampling: bool = False,
    audio_fingerprint_enabled: bool = False,
    cross_resolution_mode: str = "off",
) -> dict[str, object]:
    """Build the normalized scan-set payload used for persistence and lookup."""
    ext = normalize_extensions(extensions)
    ext.sort()
    return {
        "roots": canonical_roots(roots),
        "similarity_profile": normalize_similarity_profile(similarity_profile),
        "custom_similarity_threshold": normalize_custom_similarity_threshold(
            custom_similarity_threshold
        ),
        "scene_aware_sampling": bool(scene_aware_sampling),
        "audio_fingerprint_enabled": bool(audio_fingerprint_enabled),
        "cross_resolution_mode": normalize_cross_resolution_mode(
            cross_resolution_mode
        ),
        "extensions": ext,
    }


def build_scan_set_key(
    roots: list[str],
    similarity_profile: str,
    extensions: list[str],
    *,
    custom_similarity_threshold: float = 0.18,
    scene_aware_sampling: bool = False,
    audio_fingerprint_enabled: bool = False,
    cross_resolution_mode: str = "off",
) -> str:
    """Serialize a normalized scan-set payload into a deterministic string key."""
    spec = build_scan_set_spec(
        roots=roots,
        similarity_profile=similarity_profile,
        extensions=extensions,
        custom_similarity_threshold=custom_similarity_threshold,
        scene_aware_sampling=scene_aware_sampling,
        audio_fingerprint_enabled=audio_fingerprint_enabled,
        cross_resolution_mode=cross_resolution_mode,
    )
    return json.dumps(spec, separators=(",", ":"), sort_keys=True)
