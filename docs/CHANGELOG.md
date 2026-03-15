# Changelog

## 2026-03-15

- Added guarded fingerprint decoder fallbacks so fingerprinting no longer hangs indefinitely on pathological media files.
- Routed `wmv` and `asf` files away from the OpenCV fingerprint path and through `PyAV -> ffmpeg` with a hard `15s` timeout per decoder attempt.
- Kept the fast path for normal formats as `OpenCV -> PyAV -> ffmpeg`, preserving the existing dHash frame-normalization logic while making each decoder attempt killable.
- Persisted quiet fingerprint decoder provenance in SQLite so successful fallbacks and decoder timeouts can be inspected later without surfacing extra scan issues for recovered files.
- Added a hidden `fingerprint-child` CLI entrypoint used by the guarded decoder subprocess path.
- Added unit coverage for risky-format routing, decoder fallback sequencing, structured child-process responses, GUI backend-availability handling, and fingerprint provenance persistence.
- Validated the new behavior read-only on real media sets, including a previously wedged WMV sample and larger deterministic mixed-library sample sets.
