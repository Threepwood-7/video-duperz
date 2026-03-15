# Changelog

## Unreleased

### Process-isolated analyze runtime

- Replaced the per-file thread-executed analyze path with a parent-supervised child-process model.
- Each file now runs metadata probe fallback, fingerprint fallback, and result packaging inside its own hidden Python child process.
- Scan timeouts now apply to the child process lifetime instead of only marking a stuck thread logically complete.
- When a file exceeds the analyze timeout, the parent runtime now:
  - requests a cooperative stop first
  - waits a short grace period
  - kills the full child process tree if the file still does not exit
- Timed-out files remain excluded from current-run artifact persistence and duplicate matching, but are still recorded for manual review.

### Analyze fallback chain

- Added an always-on analyze fallback system for difficult files instead of failing after a single metadata or frame-decoding path.
- Metadata probing now retries with the alternate backend when the primary backend fails:
  - `pyav` primary scans retry with `ffprobe`
  - `ffprobe` primary scans retry with `pyav`
- Fingerprint frame extraction now uses an ordered decoder chain:
  - OpenCV primary
  - PyAV frame-decoding fallback
  - `ffmpeg` frame-extraction fallback for the hardest damaged files
- The full fallback chain runs inside the existing per-file analyze task, so scan timeouts still apply to the entire retry sequence instead of resetting per attempt.

### Corrupted file handling

- Analyze retries are now lenient by default for damaged or partially broken videos.
- Relaxed FFmpeg/libav probing and decoding options are used on fallback attempts to improve the chances of recovering usable metadata or sample frames from corrupted files.
- Files are only marked as failed after all compatible metadata and frame-decoding fallbacks have been exhausted.

### Persistence and provenance

- Successful fallback usage is now persisted quietly per file instead of only living in memory for the current run.
- Added stable per-file provenance markers for:
  - alternate metadata backend fallback success
  - fallback fingerprint decoder success
- Updated analysis-issue storage so multiple provenance and failure records can coexist for the same file/backend combination.
- Successful later scans on the primary path clear stale fallback provenance for that file/backend.

### Runtime requirements

- `ffmpeg` is now a required runtime tool alongside `ffprobe` because it participates in the supported fingerprint fallback chain.
- Startup and CLI scan entry preflight now validate the full analyze fallback toolchain instead of checking only the selected metadata backend.

### Documentation

- Updated the README requirements, architecture notes, and troubleshooting guidance to describe the dual-backend metadata fallback path and the OpenCV -> PyAV -> `ffmpeg` fingerprint fallback chain.
