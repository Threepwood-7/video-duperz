# Changelog

## 2026-03-19

- Added duration-difference duplicate matching so near-identical videos can still group when one copy is a few seconds longer or shorter due to trims or timestamp drift.
- Exposed `duration_tolerance_s` in the Sources tab and persisted it through settings, scan workers, and the runtime matcher pipeline.
- Switched duration-mismatched duplicate comparisons to an inner-frame hash path so intro/outro drift no longer dominates the median-distance decision.
- Persisted duplicate match reasons in SQLite and added a visible `Match` column in Results, including `Trimmed` labeling with duration-delta tooltip details.
- Added unit, GUI, runtime, and ffmpeg-backed integration coverage for tolerance threading, trimmed-match persistence, and the new results rendering path.

## 2026-03-15

- Added guarded fingerprint decoder fallbacks so fingerprinting no longer hangs indefinitely on pathological media files.
- Routed `wmv` and `asf` files away from the OpenCV fingerprint path and through `ffmpeg -> PyAV` with a hard `15s` timeout per decoder attempt.
- Kept the fast path for normal formats as `OpenCV -> PyAV -> ffmpeg`, preserving the existing dHash frame-normalization logic while making each decoder attempt killable.
- Persisted quiet fingerprint decoder provenance in SQLite so successful fallbacks and decoder timeouts can be inspected later without surfacing extra scan issues for recovered files.
- Added a hidden `fingerprint-child` CLI entrypoint used by the guarded decoder subprocess path.
- Added unit coverage for risky-format routing, decoder fallback sequencing, structured child-process responses, GUI backend-availability handling, and fingerprint provenance persistence.
- Validated the new behavior read-only on real media sets, including a previously wedged WMV sample and larger deterministic mixed-library sample sets.
- Added internal benchmark planning and evaluation helpers for deterministic sample sets, lane-safe scheduling, and per-lane fallback/timeout telemetry without changing production `burst` behavior.
