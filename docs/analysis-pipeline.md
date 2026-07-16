# Analysis pipeline

## Stages

The stage plan is built for the resolved DEM mode (`app/progress.py:
build_stage_plan`) so the UI never shows stages that will not run:

| Mode | Head stages |
|---|---|
| satellite_only | loading_project → selecting_dem_mode → fetching_satellite_dem → reprojecting_satellite_dem → terrain_quality_checks |
| drone_only / existing full-coverage DTM | loading_project → resolving_dtm → validating_dtm → selecting_dem_mode → computing_drone_coverage → preparing_drone_dem → terrain_quality_checks |
| fused | …plus fetching/reprojecting satellite and fusing_dem |

Common tail: conditioning_dem → calculating_flow_accumulation →
extracting_valleys → extracting_ridges → detecting_keypoints →
generating_keylines → validating_spatial_results → generating_hillshade →
generating_exports → saving_results → completed.

## Honesty rules

- A stage is marked complete only when the next real operation starts.
- Overall progress = completed stages / plan length; it never decreases
  and is never derived from elapsed time.
- `operation()` reports measurable in-stage work (download %, resampling,
  fusion) and resets the stall watchdog; a bare heartbeat only proves the
  worker is alive.
- No progress for `ANALYSIS_STAGE_TIMEOUT_SECONDS` (default 600) fails
  the run with `STAGE_STALLED`. A dead worker (no heartbeat for
  `ANALYSIS_WORKER_LOST_SECONDS`, default 300) is swept to `WORKER_LOST`.
  Both are retryable.
- Health verdicts exposed to the UI: `active`, `slow`,
  `possibly_stalled`, `worker_missing`, `failed`, `complete` — derived
  from heartbeat age plus the RQ job status, never guessed.

## Provenance

Each run stores: parameters (`params_json`), terrain source
(existing_dtm | drone_only | satellite_only | fused — user-selected
provenance is never overwritten by engine mode resolution), DEM path,
analysis version, QA report, feature counts, warnings, a bounded
technical log, executor, worker name, and the retry chain. Keyline
features carry per-feature metadata (id, keypoint id, elevation, length,
confidence where derivable — confidence is null when no defined method
exists; it is never fabricated).

## Terrain quality assurance

`app/terrain_quality.py` produces a QA report (tilt, relief/footprint
ratio, satellite cross-check when enabled) stored on the run and included
in the design package as `terrain-qa.json`. `TERRAIN_QA_MODE=strict`
blocks keyline generation on severe errors; `warn` (default) watermarks.

## Grid bounds

`ANALYSIS_MAX_GRID_CELLS` (default 1.5 M) coarsens very high-resolution
DTMs before hydrology so a small worker cannot OOM/stall ("Fusing DEM"
hang fix).
