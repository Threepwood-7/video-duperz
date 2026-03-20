# High-ROI vNext Performance and Design Improvements

## Summary

- Keep the main scan runtime thread-based, but stop spending optimization effort on "more threads" alone.
- The next meaningful gains are likely to come from:
  1. a persistent fingerprint worker process pool
  2. a DB/cache redesign that separates scan membership from reusable media artifacts
  3. a faster matcher that uses compact numeric fingerprints and vectorized comparisons
  4. smarter lane scheduling and adaptive backend routing
- Since backward compatibility is not required, use a schema-breaking vNext design instead of incremental patching.

## Key Changes

- Runtime architecture:
  - Keep one threaded coordinator for enumeration, queueing, DB flushes, and progress.
  - Move heavy fingerprint decoding to a small long-lived worker process pool instead of spawning a new child per decoder attempt.
  - Use a hybrid model:
    - threads for orchestration
    - persistent child processes for killable decode/fingerprint work
  - Prefer one active fingerprint worker per physical-drive lane by default, with lane-aware dispatch still controlling contention.
  - Keep micro-batch scheduling as an evaluation strategy, but treat it as a secondary gain; current evidence suggests it helps a little, not dramatically.

- Fingerprint pipeline:
  - Replace per-attempt process spawn with reusable decoder workers that accept many jobs over a simple message protocol.
  - Worker responsibilities:
    - decode/sample frames
    - compute hashes
    - return decoder provenance and timing
  - Parent responsibilities:
    - lane scheduling
    - timeout enforcement
    - worker restart on hang/crash
  - Add adaptive decoder routing:
    - keep `ffmpeg` first for risky formats
    - remember recent decoder failures/timeouts by extension/container family
    - bias future files of the same family toward the decoder that has been healthiest in the current run

- Database redesign:
  - Break the current "files belong to one scan row" model into reusable artifact tables plus per-scan membership.
  - Recommended split:
    - `media_files`: stable path identity
    - `file_observations`: per-scan stat snapshot, root, lane, exists state
    - `probe_cache`: backend-scoped reusable metadata keyed by file stat identity
    - `fingerprint_cache`: algorithm-scoped reusable hashes keyed by file stat identity
    - `scan_runs`: top-level scan execution row
    - `scan_results_edges` or `scan_clusters`: persisted match results for one run
  - Stop persisting duplicate groups as the primary canonical form.
  - Persist edges or cluster membership instead, and materialize UI groups on demand.
  - This will make cache reuse, rescans, and scan-history comparisons cleaner and faster.

- Matching engine:
  - Replace the pure Python pairwise matcher with a more numeric pipeline.
  - Store fingerprints as fixed-size numeric arrays and run bucket-local comparisons with vectorized XOR/bitcount logic.
  - Keep the existing bucket strategy conceptually, but make the expensive inner loop array-based instead of Python `itertools.combinations`.
  - Optionally add a cheap coarse signature column to prune candidates before full distance checks.

- Telemetry and worker guidance:
  - Keep the new per-lane benchmark/evaluation harness.
  - Extend runtime telemetry with:
    - decoder startup overhead
    - worker restart count
    - queue wait time
    - per-lane service time
    - cache-hit quality by backend/decoder
  - Use this to derive default worker recommendations, but do not auto-change production `burst` behavior in the first pass.

## Public Interfaces / Types

- Break DB schema deliberately and version it cleanly.
- Add internal worker-pool protocol types for:
  - fingerprint task request
  - fingerprint task result
  - worker health / restart reason
- Replace scan-result persistence from `duplicate_groups`-first to `edges/clusters`-first.
- Keep UI-facing duplicate-group objects if helpful, but build them from result tables instead of storing them as the canonical DB shape.
- Do not add new user-facing settings in the first pass; keep new scheduling and pool controls internal until benchmarked.

## Test Cases and Scenarios

- Runtime:
  - persistent fingerprint workers process multiple files without respawn
  - hung worker is killed and replaced without stalling the scan
  - lane-aware scheduling still prevents benchmark-time same-drive contention
  - risky-format routing plus adaptive decoder memory improves completion rate
- DB:
  - reusable cache rows survive across scans cleanly
  - scan membership can be deleted without losing reusable probe/fingerprint artifacts
  - result materialization from edges/clusters produces the same UI group semantics
- Matching:
  - vectorized matcher returns the same duplicate decisions as the current matcher on fixed fixtures
  - large bucket performance is measurably better than the current Python loop
- Benchmarking:
  - rerun the focused risky subset, mixed 240 set, and incomplete set
  - compare current baseline vs persistent-worker design on:
    - elapsed time
    - fallback count
    - timeout count
    - worker restarts
    - final failures

## Assumptions

- The best next step is not "more threads," but better separation of orchestration threads from heavy decode work.
- A small persistent process pool for fingerprinting is more promising than a whole-file process pool.
- DB compatibility is intentionally out of scope; the redesign should optimize for cleaner cache reuse and simpler runtime behavior.
- Micro-batches are worth keeping as an optimization seam, but they are not the main expected win.
