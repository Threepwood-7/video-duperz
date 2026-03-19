# Duration-Difference Duplicate Detection — Implementation Plan

Status: [DONE] High-priority implementation landed for phases 1-3. Phase 4 remains deferred.

## Problem Statement

Two copies of the same video that differ by a few seconds in duration — due to intro/outro
trimming, re-encoding with a slightly different start or end point, or minor container-level
timestamp drift — are currently **never compared** and therefore never reported as duplicates.

---

## Root Cause Analysis

### RC-1 — Hard Duration Gate (`matcher.py:50-57`)

`_is_candidate` applies two hard cuts before any hash comparison occurs:

```python
# matcher.py:50-57
if abs(a.duration_s - b.duration_s) > 3.0:          # absolute gate
    return False
shortest = min(a.duration_s, b.duration_s)
longest  = max(a.duration_s, b.duration_s)
if shortest / longest < 0.96:                         # ratio gate (4 % tolerance)
    return False
```

A 90-minute film re-encoded with a 5-second intro removed fails the absolute gate immediately.
No hash comparison is attempted.

### RC-2 — Duration Bucketing (`matcher.py:35-37`, `66-78`)

```python
def duration_bucket(duration_s: float) -> int:
    return round(max(0.0, float(duration_s)) / 5.0)
```

Candidates are bucketed by `(duration_bucket, aspect_bin)`.  Two items more than 2.5 s apart
land in different buckets and never appear in the same `itertools.combinations` pass, so
`_is_candidate` is never even called for them.

### RC-3 — Fixed-Percentage Frame Sampling (`fingerprint.py:48-61`, `192-196`)

```python
SAMPLE_PERCENTS = [0.05, 0.13, 0.21, 0.29, 0.37, 0.45, 0.53, 0.61, 0.69, 0.77, 0.85, 0.93]

def sample_timestamps(duration_s: float) -> list[float]:
    return [duration_s * percent for percent in SAMPLE_PERCENTS]
```

When one copy is 5 s shorter, its 5 % timestamp lands ~0.25 s earlier and its 93 % timestamp
lands ~4.65 s earlier than the reference copy.  The extreme samples (indices 0 and 11,
covering 5 % and 93 %) suffer the most temporal drift and are the ones hardest to match.

### RC-4 — 3-Point Median Uses the Extremes (`fingerprint.py:230-237`)

```python
def normalized_median_distance(hashes_a: list[int], hashes_b: list[int]) -> float:
    distances = [hamming_distance(a, b) for a, b in zip(hashes_a, hashes_b, strict=True)]
    return float(median(distances)) / 64.0
```

The current implementation computes the median over **all 12 distances**.  For a trimmed copy
the extreme-index hashes carry the most temporal misalignment, shifting the median upward and
potentially causing a near-miss rejection even when the mid-section hashes are nearly
identical.

### RC-5 — Quick-Reject Prefilter (`matcher.py:40-46`, `90`)

```python
def _prefilter_hamming_median(a: list[int], b: list[int]) -> float:
    indices = sorted({0, length // 2, length - 1})   # start, middle, end
    ...

if _prefilter_hamming_median(a.hashes, b.hashes) > 22.0:
    continue
```

The prefilter samples indices `[0, 5, 11]`.  Index 0 (5 % of duration) and index 11 (93 %)
are the most drifted for a trimmed copy; including them in the quick-reject median means
legitimate pairs may be rejected before full comparison.

---

## Implementation Plan

### Phase 1 — [DONE] Configurable Duration Tolerance

**Goal:** Allow pairs with a larger duration difference to reach hash comparison.
**Files:** `models.py`, `matcher.py`
**No fingerprint data change, no `ALGO_VERSION` bump required.**

#### 1.1 — Add fields to `Settings` (`models.py:49`)

```python
# Add inside Settings dataclass, after `similarity_profile`:
duration_tolerance_s: float = 8.0
```

`8.0 s` is the new default.  It covers the most common real-world cases:
- short intro/outro clips (≤ 5 s)
- container re-mux with timestamp drift (≤ 2 s)
- credits removal (often 3–8 s)

The ratio gate is derived dynamically in the matcher; it does **not** need a separate field.

#### 1.2 — Thread tolerance through `find_duplicate_edges` (`matcher.py:66`)

Add `duration_tolerance_s: float = 8.0` as a keyword parameter to `find_duplicate_edges`.
Pass it through to `_is_candidate`.

```python
def find_duplicate_edges(
    items: list[MatchItem],
    profile: str = "balanced",
    duration_tolerance_s: float = 8.0,
) -> tuple[list[DuplicateEdge], MatchStats]:
```

#### 1.3 — Widen the duration bucket window (`matcher.py:35-37`, `71-78`)

The bucket key must also widen so that duration-offset pairs can land in the same bucket.
Replace the fixed `/5.0` divisor with a derived window size:

```python
_DURATION_BUCKET_WINDOW_S = 5.0   # kept for bucket key, widened lookup below
```

In `find_duplicate_edges`, after building `buckets`, add a second pass that merges adjacent
bucket entries when `duration_tolerance_s > _DURATION_BUCKET_WINDOW_S / 2`.  Concretely:
for each `(dur_bucket, asp_bin)` key, also include items from `(dur_bucket ± 1, asp_bin)`
in the candidate set so that items in neighboring 5-second windows can be compared.

```python
# New helper — build the expanded candidate pool for one bucket key
def _neighbor_items(
    buckets: dict[tuple[int, float], list[MatchItem]],
    dur_bucket: int,
    asp_bin: float,
    tolerance_s: float,
) -> list[MatchItem]:
    """Collect items from this bucket and adjacent ones within tolerance."""
    extra_buckets = max(0, math.ceil(tolerance_s / _DURATION_BUCKET_WINDOW_S) - 1)
    seen_ids: set[int] = set()
    result: list[MatchItem] = []
    for offset in range(-extra_buckets, extra_buckets + 1):
        for item in buckets.get((dur_bucket + offset, asp_bin), []):
            if item.file_id not in seen_ids:
                seen_ids.add(item.file_id)
                result.append(item)
    return result
```

This replaces the single-bucket `bucket_items` loop body.

#### 1.4 — Replace hardcoded constants in `_is_candidate` (`matcher.py:49-63`)

```python
def _is_candidate(
    a: MatchItem,
    b: MatchItem,
    duration_tolerance_s: float = 8.0,
) -> bool:
    if abs(a.duration_s - b.duration_s) > duration_tolerance_s:
        return False
    shortest = min(a.duration_s, b.duration_s)
    longest  = max(a.duration_s, b.duration_s)
    if longest <= 0:
        return False
    # Derive ratio floor from the tolerance: at 8 s tolerance on a 30 s video → 0.73;
    # floor at 0.70 so we never reject two 90-min films that differ by 7 s (ratio 0.9987).
    ratio_floor = max(0.70, 1.0 - duration_tolerance_s / max(1.0, longest))
    if shortest / longest < ratio_floor:
        return False
    ra = a.width / a.height if a.height else 0.0
    rb = b.width / b.height if b.height else 0.0
    if ra <= 0 or rb <= 0:
        return False
    return abs(math.log2(ra / rb)) <= 0.2
```

#### 1.5 — Wire tolerance from `pipeline.py` / `pipeline_runtime.py`

`find_duplicate_edges` is called from the pipeline.  Thread `Settings.duration_tolerance_s`
through to that call site so the user setting takes effect.

---

### Phase 2 — [DONE] Inner-Section Comparison for Duration-Mismatched Pairs

**Goal:** When a pair passes the duration gate but their durations differ by more than a
small threshold, compare only the **inner frame hashes** (indices 2–9, covering 21 %–77 %
of duration) rather than all 12.  These frames are the least affected by trim at either end.

**Files:** `fingerprint.py`, `matcher.py`
**No `ALGO_VERSION` bump required** — uses the existing `hashes` list, different slice only.

#### 2.1 — New comparison function (`fingerprint.py`, after `normalized_median_distance:230`)

```python
# Inner frame index range — covers 21 % to 77 % of the video timeline.
_INNER_FRAME_SLICE = slice(2, 10)   # indices 2,3,4,5,6,7,8,9 → 8 frames


def inner_median_distance(hashes_a: list[int], hashes_b: list[int]) -> float:
    """Compare two hash sequences using only the temporally stable inner frames.

    Restricts the comparison to frame indices 2–9 (21 %–77 % of duration) to
    reduce sensitivity to intro/outro trimming in duration-mismatched pairs.
    Returns a normalized distance in [0.0, 1.0].
    """
    inner_a = hashes_a[_INNER_FRAME_SLICE]
    inner_b = hashes_b[_INNER_FRAME_SLICE]
    if len(inner_a) != len(inner_b) or not inner_a:
        return 1.0
    distances = [hamming_distance(a, b) for a, b in zip(inner_a, inner_b, strict=True)]
    return float(median(distances)) / 64.0
```

#### 2.2 — Inner-only prefilter (`matcher.py`)

Add a companion to `_prefilter_hamming_median` that samples only inner indices:

```python
def _prefilter_hamming_inner(a: list[int], b: list[int]) -> float:
    """Quick-reject prefilter using inner frame indices only."""
    inner_a = a[2:10]
    inner_b = b[2:10]
    length = min(len(inner_a), len(inner_b))
    if length <= 0:
        return 64.0
    mid = length // 2
    indices = sorted({0, mid, length - 1})
    distances = [hamming_distance(inner_a[i], inner_b[i]) for i in indices]
    return float(median(distances)) if distances else 64.0
```

#### 2.3 — Dispatch logic in `find_duplicate_edges` (`matcher.py:82-101`)

Introduce a `_INNER_ONLY_THRESHOLD_S` constant (default `3.0` — equal to the old hard gate)
and use it to choose between the full and inner comparison:

```python
_INNER_ONLY_THRESHOLD_S = 3.0   # duration gap above which inner-only mode activates


# Inside the pair loop, replace the existing prefilter + distance calls:

dur_gap = abs(a.duration_s - b.duration_s)
use_inner = dur_gap > _INNER_ONLY_THRESHOLD_S

prefilter_distance = (
    _prefilter_hamming_inner(a.hashes, b.hashes)
    if use_inner
    else _prefilter_hamming_median(a.hashes, b.hashes)
)
if prefilter_distance > 22.0:
    stats.prefilter_rejected_pairs += 1
    continue

stats.full_distance_pairs += 1
distance = (
    inner_median_distance(a.hashes, b.hashes)
    if use_inner
    else normalized_median_distance(a.hashes, b.hashes)
)
```

#### 2.4 — Inner-mode threshold adjustment

The inner comparison uses 8 frames instead of 12 and all 8 are well-aligned, so the signal
is denser.  Tighten the threshold slightly when in inner mode to compensate for the reduced
noise floor:

```python
_INNER_MODE_THRESHOLD_FACTOR = 0.90   # 10 % tighter than the profile threshold

effective_threshold = (
    threshold * _INNER_MODE_THRESHOLD_FACTOR if use_inner else threshold
)
if distance <= effective_threshold:
    ...
```

#### 2.5 — Track inner-mode matches in `MatchStats` (`models.py:259`)

Add a counter to `MatchStats` so the UI can surface how many pairs were found via inner mode:

```python
@dataclass(slots=True)
class MatchStats:
    ...
    inner_mode_pairs: int = 0      # pairs compared with inner-only hashes
    inner_mode_accepted: int = 0   # pairs accepted via inner-only comparison
```

---

### Phase 3 — [DONE] Match Reason Tagging and UI Display

**Goal:** Distinguish `"perceptual"` matches (full hash, same-duration pairs) from
`"trimmed_match"` (inner-hash, duration-offset pairs) in the results so the user can
assess confidence.

**Files:** `models.py`, `matcher.py`, UI result panel

#### 3.1 — Extend `DuplicateEdge` (`models.py:250`)

```python
MatchReason = Literal["perceptual", "trimmed_match"]


@dataclass(slots=True)
class DuplicateEdge:
    file_a: int
    file_b: int
    score: float
    match_reason: MatchReason = "perceptual"
```

#### 3.2 — Set reason in `find_duplicate_edges` (`matcher.py`)

```python
edges.append(
    DuplicateEdge(
        file_a=a.file_id,
        file_b=b.file_id,
        score=1.0 - distance,
        match_reason="trimmed_match" if use_inner else "perceptual",
    )
)
```

#### 3.3 — Propagate to `DuplicateItem` (`models.py:294`)

```python
@dataclass(slots=True)
class DuplicateItem:
    ...
    match_reason: MatchReason = "perceptual"
```

Populate it in `build_duplicate_groups` from the per-pair `score_map` (extend the map to
also store the reason).

#### 3.4 — UI badge

In the results table delegate, render `"trimmed_match"` items with an amber confidence
indicator and a `Δt` tooltip showing the duration gap.  `"perceptual"` items keep the
existing green indicator.

---

### Phase 4 — Temporal Offset Search (Advanced, Optional)

**Goal:** Handle the harder case where the extra content is at the **start** (different
intro), making even the inner frames drift by a fixed offset.

**Files:** `fingerprint.py`, `matcher.py`, `db_fingerprints.py`

This phase is **not required** for the common trim-at-end case covered by Phases 1–2.
Implement only if users report near-miss groups with large start-offset content.

#### 4.1 — Shifted hash sampling (`fingerprint.py`)

```python
def shifted_inner_hashes(
    path: str,
    duration_s: float,
    offset_s: float,
    decoder_backend: FrameDecodeBackendId = "ffmpeg",
    *,
    ffmpeg_exe_path: str = "",
) -> list[int]:
    """Sample 8 inner frames shifted by offset_s and return their dHash values.

    Used to align a trimmed copy whose extra content appears at the start.
    The shifted timestamps are clamped to [0, duration_s].
    """
    inner_percents = SAMPLE_PERCENTS[2:10]   # 21 %–77 %
    shifted_timestamps = [
        max(0.0, min(duration_s, duration_s * p + offset_s))
        for p in inner_percents
    ]
    frames = [
        _ffmpeg_gray_frame(
            ensure_ffmpeg_available(ffmpeg_exe_path), path, ts,
        )
        for ts in shifted_timestamps
    ]
    return _hash_gray_frames(path, frames)
```

#### 4.2 — Offset-search trigger in `find_duplicate_edges` (`matcher.py`)

After the inner-only comparison returns a near-miss (distance within `1.5 × threshold`),
compute the expected offset `Δt = (longer.duration_s - shorter.duration_s) / 2` and
call `shifted_inner_hashes` on the shorter video.  Re-compare against the longer video's
inner hashes.

Cache the shifted hashes keyed by `(file_id, round(offset_s, 1))` in a local dict for
the scan session to avoid re-decoding the same file at the same offset.

#### 4.3 — Persist shifted hashes (`db_fingerprints.py`)

Add an `offset_hashes` table:

```sql
CREATE TABLE IF NOT EXISTS offset_hashes (
    file_id     INTEGER NOT NULL,
    offset_s    REAL    NOT NULL,
    algo_version INTEGER NOT NULL,
    hashes      TEXT    NOT NULL,   -- JSON int array, 8 elements
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (file_id, offset_s, algo_version)
);
```

On re-scan, load cached offset hashes and skip re-decoding unless `algo_version` changed.

---

## Summary of Changes per File

| File | Phase | Change |
|------|-------|--------|
| `models.py` | 1 | Add `duration_tolerance_s: float = 8.0` to `Settings` |
| `models.py` | 3 | Add `MatchReason` literal; add `match_reason` to `DuplicateEdge`, `DuplicateItem` |
| `models.py` | 2 | Add `inner_mode_pairs`, `inner_mode_accepted` to `MatchStats` |
| `matcher.py` | 1 | Add `duration_tolerance_s` param to `find_duplicate_edges` and `_is_candidate` |
| `matcher.py` | 1 | Add `_neighbor_items` helper; replace single-bucket loop with widened lookup |
| `matcher.py` | 1 | Derive `ratio_floor` dynamically instead of hardcoded `0.96` |
| `matcher.py` | 2 | Add `_prefilter_hamming_inner`; dispatch on `dur_gap > _INNER_ONLY_THRESHOLD_S` |
| `matcher.py` | 2, 3 | Apply `_INNER_MODE_THRESHOLD_FACTOR`; set `match_reason` on each edge |
| `fingerprint.py` | 2 | Add `_INNER_FRAME_SLICE`, `inner_median_distance` |
| `fingerprint.py` | 4 | Add `shifted_inner_hashes` |
| `pipeline.py` / `pipeline_runtime.py` | 1 | Thread `duration_tolerance_s` from `Settings` to `find_duplicate_edges` |
| `db_fingerprints.py` | 4 | Add `offset_hashes` table and read/write helpers |
| UI result panel | 3 | Amber badge + `Δt` tooltip for `"trimmed_match"` items |

---

## Constants Summary

| Constant | Location | Value | Purpose |
|----------|----------|-------|---------|
| `Settings.duration_tolerance_s` | `models.py` | `8.0 s` | User-configurable max duration gap |
| `_DURATION_BUCKET_WINDOW_S` | `matcher.py` | `5.0 s` | Existing bucket granularity |
| `_INNER_ONLY_THRESHOLD_S` | `matcher.py` | `3.0 s` | Gap above which inner-only mode activates |
| `_INNER_FRAME_SLICE` | `fingerprint.py` | `slice(2, 10)` | Indices 2–9 = 21 %–77 % of duration |
| `_INNER_MODE_THRESHOLD_FACTOR` | `matcher.py` | `0.90` | Tighter similarity gate for inner-mode pairs |

---

## Test Plan

### Unit tests to add

**`tests/unit/test_matcher.py`**
- `test_duration_offset_pair_accepted_within_tolerance` — pair with 6 s gap is accepted
  when `duration_tolerance_s=8.0`
- `test_duration_offset_pair_rejected_outside_tolerance` — same pair rejected at `5.0 s`
- `test_neighbor_items_expands_bucket_lookup` — items in adjacent buckets are included
- `test_inner_only_mode_activates_above_threshold` — `use_inner` path chosen when
  `dur_gap > _INNER_ONLY_THRESHOLD_S`
- `test_inner_prefilter_skips_extreme_indices` — verify indices 0 and 11 not used
- `test_match_reason_trimmed_for_inner_mode_pairs` — `DuplicateEdge.match_reason` is
  `"trimmed_match"` for duration-offset pairs

**`tests/unit/test_fingerprint.py`**
- `test_inner_median_distance_uses_only_inner_frames` — mock 12 hashes; pollute indices
  0,1,10,11 with maximum distance; verify `inner_median_distance` still returns near-zero
- `test_inner_median_distance_empty_guard` — empty/mismatched lists return `1.0`

### Integration test

**`tests/integration/test_pipeline_integration.py`**
- `test_pipeline_finds_trimmed_duplicate` — craft two real or synthetic video fixtures that
  differ in duration by 5 s; verify they appear in the same `DuplicateGroup` with
  `match_reason == "trimmed_match"`.

---

## Implementation Order

1. Phase 1 (`models.py` + `matcher.py`) — pure filter change, self-contained, low risk.
2. Phase 2 (`fingerprint.py` + `matcher.py`) — reuses existing hashes, no I/O.
3. Phase 3 (`models.py` + UI) — additive, can ship independently after Phase 1+2.
4. Phase 4 — only if Phase 1+2 leaves measurable near-miss cases in user reports.
