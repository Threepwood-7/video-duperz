# New Features & Improvements — video-duperz

Organized by area, priority ranked High / Medium / Low within each section.

---

## 1. Metadata & Probing

### [High][DONE] Display FPS in results table
`probe.py` already extracts `fps` into `VideoMeta.fps` and `MatchItem.fps` carries it, but no
`COL_FPS` column exists in the results view.  Add the column alongside resolution so the user
can distinguish 24 fps from 60 fps copies when choosing which to keep.

### [High][DONE] Capture and display bit depth
`probe.py` reads `pix_fmt` / `bits_per_raw_sample` but discards it.  Add `bit_depth: int` to
`VideoMeta` (default `8`).  Show it in a results column and factor it into quality scoring
(10-bit > 8-bit at equal bitrate).

### [High][DONE] Granular HDR metadata
The current `is_hdr: bool` flag collapses HDR10, HDR10+, HLG, Dolby Vision into one bit.
Replace with `hdr_format: str` (e.g. `"HDR10"`, `"DV"`, `"HLG"`, `""`) derived from
`color_trc` / `color_primaries` / side-data already inspected in `probe.py`.

### [Medium] Codec profile and level
Capture `codec_profile: str` and `codec_level: str` from the stream (e.g. `"High"` / `"5.1"`)
and add them to `VideoMeta`.  Surface in a tooltip or expandable row in the results table so
the user can see why two H.264 files are rated differently.

### [Medium] Container format column
Add `container: str` to `VideoMeta` (e.g. `"mkv"`, `"mp4"`, `"avi"`), derived from the
probed format name rather than inferred from the file extension.  Display as a column.

### [Medium] Audio stream count
Replace the `has_audio: bool` field with `audio_stream_count: int`.  Useful when one copy
has multiple audio tracks (e.g. original + dub) and the other does not.

### [Low] Interlaced video detection
Add `is_interlaced: bool` to `VideoMeta`.  Flag interlaced copies in the results view;
prefer progressive when all else is equal in quality scoring.

### [Low] Encoder tag
Capture the `encoder` tag from container metadata and expose it as an optional tooltip.
Useful to identify files produced by known low-quality encoders.

---

## 2. Quality Scoring

### [High] Configurable scoring weights
`quality.py` hardcodes `0.65 / 0.25 / 0.10` for resolution / bitrate / codec.  Add
`quality_weight_resolution`, `quality_weight_bitrate`, `quality_weight_codec` floats to
`Settings` (validated to sum to 1.0).  Expose sliders in the settings dialog.

### [High] Additional keep rules
`keep_rule` only supports `"best_quality"`.  Add:
- `"highest_resolution"` — prefer the highest pixel count regardless of bitrate
- `"largest_file"` — proxy for original / least-compressed
- `"newest"` / `"oldest"` — mtime-based
- `"most_audio_tracks"` — keep the copy with more audio streams
- `"prefer_path"` — keep whichever copy lives under a user-specified path prefix

Expose via a dropdown in the settings dialog.

### [High] HDR and bit-depth bonus in quality score
HDR files and 10-bit encodes should rank above SDR 8-bit at otherwise equal specs.  Add
optional bonus multipliers: `quality_hdr_bonus: float = 1.10` and
`quality_bit_depth_bonus: float = 1.05` to `Settings`.

### [Medium] FPS factor in quality score
Add `quality_weight_fps: float = 0.0` (off by default).  When non-zero, a 60 fps copy scores
higher than a 24 fps copy at the same resolution and bitrate.

### [Medium] Audio quality factor
Include `audio_bitrate` in the quality formula.  Add `quality_weight_audio: float = 0.0`
(off by default).  Enables keeping the copy with the best audio track.

### [Medium] Per-group keep override
Allow the user to override the auto-selected keep file for a specific group from the results
table without changing the global keep rule.

### [Low] Configurable codec rankings
Expose `CODEC_RANK` as a user-editable ordered list in the settings dialog so the user can,
e.g., rank AV1 above HEVC or demote VP9 below H.264.

---

## 3. Duplicate Detection

### [High][DONE] Duration-difference matching *(see DURDIFF.md for full plan)*
Detect the same video when one copy differs by a few seconds (intro/outro trimming, re-mux
timestamp drift).  Key changes: configurable `duration_tolerance_s`, inner-section hash
comparison (frames 2–9, 21%–77%), and a `"trimmed_match"` result badge.

### [Medium] Alternative perceptual hash algorithms
The entire fingerprint pipeline uses `dhash`.  Add support for `phash` (DCT-based) and
`whash` (wavelet-based) as optional `fingerprint_algo` settings.  Different algorithms catch
different types of re-encoding artefacts.

### [Medium] Configurable frame sample count and positions
`SAMPLE_PERCENTS` in `fingerprint.py` is fixed at 12 positions.  Expose
`fingerprint_frame_count: int` (default `12`) and optionally `fingerprint_sample_percents`
as a settings override.  More samples = slower scan, fewer false positives.

### [Medium][DONE] Scene-change-aware sampling
Instead of fixed percentages, sample at detected scene boundaries so hashes are anchored to
stable visual landmarks rather than arbitrary time positions.  Particularly useful for
variable-length intros.

### [Medium][DONE] Audio fingerprinting for near-identical video detection
Add optional chromaprint / audiohash-based comparison as a secondary signal.  Two files
with the same audio fingerprint but different video encoding are almost certainly the same
content.

### [Low][DONE] Custom similarity thresholds
Beyond the three named profiles (`conservative / balanced / aggressive`), allow the user to
set a numeric threshold directly (a slider in settings, range 0.01–0.30).

### [Low][DONE] Cross-resolution matching
Today aspect ratio is gated strictly (`log2(ra/rb) ≤ 0.2`).  Add an opt-in
`match_cross_resolution: bool` that relaxes this gate for cases like 1080p vs 720p of the
same film.

---

## 4. Action System

### [High] Dry-run preview
Before executing any rename/delete batch, show a detailed preview dialog listing every
planned filesystem operation.  Require an explicit confirmation step.  Log the preview to
`ActionRunResult` with `status = "dry_run"`.

### [High] Action undo / rollback
For `rename` actions, store the original path in `ActionItemResult`.  Add an **Undo Last
Run** button that reverses the rename operations from the most recent `ActionRunResult`.
For `delete` to recycle bin, the OS provides recovery; flag files deleted permanently as
unrecoverable in the UI.

### [Medium] Move to archive folder
Add `"archive"` to `ActionKind`.  The user specifies an archive root path in settings;
selected duplicates are moved there preserving relative path structure.

### [Medium] Action dry-run validation
Before the dry-run modal, pre-validate all targets: check for permission errors, cross-volume
moves, filename collisions, and disk space.  Surface issues inline.

### [Medium] Per-action error recovery
When one action in a batch fails, the current run aborts.  Replace with a continue-on-error
mode: report failures inline, allow the user to retry individual items or skip and continue.

### [Low] Batch convert action
Add `"convert"` to `ActionKind` with configurable ffmpeg preset (codec, CRF, audio).
Allow the user to transcode a lower-quality duplicate to a target spec rather than just
delete it.  This is a destructive operation; require dry-run confirmation.

### [Low] Hardlink creation action
Add `"hardlink"` to `ActionKind`.  Replace a duplicate with a hardlink to the keep copy,
reclaiming disk space while preserving directory visibility.  Validate that both files are
on the same volume first.

---

## 5. Scan Pipeline

### [DONE] Incremental scan (changed-files-only)
On re-scan of a known folder, compare `mtime_ns` and file size against the cached
`VideoRecord`.  Skip re-probing and re-fingerprinting for unchanged files.  Only process
new, modified, or deleted entries.  Dramatically reduces re-scan time for large libraries.

### [High] Estimated time remaining (ETA)
Derive ETA from `analyzed_files_per_s` and `total_work_files - completed_files` already
present in `ScanProgress`.  Display in the scan lane table header.

### [Medium] Scan result diffing
Store a snapshot of group membership from the previous scan.  After re-scan, highlight new
groups (appeared since last scan), resolved groups (no longer duplicates), and unchanged
groups.  Viewable as a separate **Changes** tab.

### [Medium] Configurable frame decode timeout
Expose `fingerprint_timeout_s` (default `15.0`) in `Settings` instead of the hardcoded
`FINGERPRINT_DECODER_TIMEOUT_S`.  Power users on slow spinning HDDs may need 30–60 s.

### [Medium] Disk space and memory guard
Before starting a scan, estimate cache DB growth from the file count and warn if available
disk space is low.  During scanning, monitor working set growth and pause if above a
configurable `max_memory_mib` limit.

### [Medium] Failure severity levels
Extend `ScanIssue` with `severity: Literal["warning", "error", "fatal"]` and
`suggestion: str`.  Display warnings differently from errors in the scan log.  Fatal issues
abort the scan with a clear explanation.

### [Low] Multi-scan result merge
Allow the user to load two or more saved scan results and merge their duplicate groups.
Useful when scanning two separate drives on separate occasions and wanting a unified view.

### [Low] Sample-based pre-scan mode
Add a `quick_sample` scan mode that fingerprints only a random 10 % subset of files to
give a fast estimate of duplicate density before committing to a full scan.

---

## 6. UI & UX

### [High] Column filtering by any displayed field
Add per-column filter inputs to the results table (e.g. filter by codec `= "hevc"`, bitrate
`> 5000`, duration `< 60`).  Complement the existing global filter bar.

### [High] Inline group notes / tags
Allow the user to attach a free-text note or one of several preset tags (e.g. `"reviewed"`,
`"skip"`, `"manual-check"`) to a duplicate group.  Persist in the scan database.

### [Medium] Scan view per-file telemetry
Show per-file probe time, fingerprint time, and decoder backend used in an expandable row in
the scan lane table.  Currently only aggregate stats are available.

### [Medium] Result export to M3U / playlist
In addition to CSV/JSON export, generate an M3U playlist of kept files so the user can
immediately verify the selection in a media player.

### [Medium] Keyboard shortcut configuration UI
Expose the keybindings currently only in `~/.claude/keybindings.json` as an in-app
settings panel tab.

### [Medium] Progress ETA in window title
Display `"Scanning — 3 min remaining"` in the window title bar while a scan is running so
the user can monitor progress from the taskbar.

### [Low] Dark/light theme toggle
Add a theme selector in Settings.  Use QStyle palette swapping; no external stylesheet
dependency.

### [Low] Group splitting and merging from the UI
Allow the user to manually split a group (mark two items as not duplicates) or merge two
separate groups (mark them as the same content).  Persist overrides in the scan database.

---

## 7. Configuration & Settings

### [High] Settings export and import
Add **Export Settings** / **Import Settings** buttons that serialise the `Settings`
dataclass to JSON and restore it.  Useful when migrating to a new machine or sharing
configurations across a team.

### [Medium] Scan profile presets
Pre-ship three named scan presets (e.g. "Movies", "TV Episodes", "Home Videos") with
sensible defaults for `similarity_profile`, `duration_tolerance_s`, `scan_size_mib_min`,
and `extensions`.  Selectable from a dropdown at the top of the sources panel.

### [Medium] Per-root scan settings
Allow different scan roots to have different `similarity_profile` or `extensions` overrides
rather than a single global setting.

### [Low] Settings validation on load
When deserializing `Settings` from JSON, validate all values against their allowed ranges
and emit warnings for out-of-range values rather than silently clamping or crashing.

---

## 8. Testing & Observability

### [High] Real video fixture suite
Add a small set of real (or synthetically generated) video files to a `tests/fixtures/`
folder covering: normal MP4, MKV, AVI with timeout risk, zero-duration, 1-frame, HDR,
multi-audio-track, and intentional corrupt file.  Use them in integration tests.

### [High] Action pipeline end-to-end tests
Add integration tests that exercise the full path from `find_duplicate_edges` →
`build_duplicate_groups` → action execution → file system verification.

### [Medium] Concurrency stress tests
Run the pipeline with `max_workers = 8` over a large synthetic file tree and assert no
deadlocks, no corrupted DB entries, and correct group output.

### [Medium] Performance regression baseline
Record fingerprinting throughput (files/s and MiB/s) in a CI benchmark job.  Fail the
build if throughput drops more than 15 % compared to the stored baseline.

### [Low] Probe backend parity tests
Assert that `ffprobe` and `pyav` return within ±1 % on `duration_s`, ±1 on `width/height`,
and agree on `codec`/`audio_codec` for every fixture file.

---

## 9. Hardcoded Values to Promote to Settings

| Location | Constant | Current value | Proposed setting key |
|----------|----------|---------------|----------------------|
| `fingerprint.py:46` | `FINGERPRINT_DECODER_TIMEOUT_S` | `15.0 s` | `fingerprint_timeout_s` |
| `fingerprint.py:47` | `_PROBLEMATIC_FORMAT_TIMEOUT_MULTIPLIER` | `4.0×` | `fingerprint_risky_timeout_factor` |
| `fingerprint.py:48-61` | `SAMPLE_PERCENTS` | 12 fixed positions | `fingerprint_frame_count` |
| `matcher.py:21-25` | `PROFILE_THRESHOLD` | `0.12/0.18/0.24` | `similarity_threshold_custom` |
| `matcher.py:50` | duration absolute gate | `3.0 s` | `duration_tolerance_s` *(DURDIFF.md)* |
| `matcher.py:90` | prefilter Hamming gate | `22.0 bits` | `matcher_prefilter_bits` |
| `quality.py:27` | scoring weights | `0.65/0.25/0.10` | `quality_weight_*` |
| `quality.py:9-20` | `CODEC_RANK` dict | fixed map | `quality_codec_ranks` |
