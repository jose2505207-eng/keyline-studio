# Architecture

```
Browser (React 18 + MapLibre, Vite/TS)
  │  REST + SSE (progress)          presigned PUT (drone photos)
  ▼                                  ▼
FastAPI backend ──────────────► S3 / MinIO (object storage)
  │        │
  │        ├── SQLite (WAL) — projects, ranches, organizations, users,
  │        │   drone_surveys, analysis_runs, dtms, artifacts, audit_log
  │        └── Filesystem — DTM library ($DTM_STORAGE_DIR),
  │            run outputs (data/<project>/analysis/<run>/…)
  │  enqueue (RQ)
  ▼
Redis ──► rq worker "keyline" (same code image, separate process)
             ├── photogrammetry job → NodeODM (OpenDroneMap)
             └── terrain job → keyline pipeline (whitebox | pysheds)
                    └── Copernicus GLO-30 (satellite/fused modes only)
```

## Domain model

```
Organization
  └── Ranch
        └── Project (AOI polygon, WGS84)
              └── Analysis Run (versioned; never overwritten)
                    ├── input: DTM (managed library, dtm_id) | satellite
                    ├── outputs: artifacts (registered, checksummed)
                    ├── stage plan + progress + heartbeat + logs + warnings
                    └── retry chain (retry_of → new run)
```

Existing single-user data lives in the backfilled `org_default` /
`ranch_default`. Auth is opt-in (`AUTH_MODE=token`); see `docs/security.md`.

## Analysis-run lifecycle

1. `POST /api/projects/{pid}/analyze` (or `/reanalyze`, or a completed
   drone survey) creates an `analysis_runs` row (`queued`) and returns the
   `run_id` immediately — never blocks on processing.
2. Dispatch (`ANALYSIS_EXECUTION`):
   - `auto` (default): enqueue to RQ; if Redis is unreachable, fall back to
     a supervised in-process execution (dev mode). The chosen path is
     recorded in `analysis_runs.executor`.
   - `rq`: queue only; 503 + `QUEUE_UNAVAILABLE` when Redis is down.
   - `inline`: always in-process (single-machine dev).
3. The worker claims the run atomically (`claim_analysis_run`) so a
   duplicate worker can never execute the same run, then drives a
   `ProgressReporter` (app/progress.py): a stage plan matched to the real
   DEM mode, monotonic progress from completed stages, fine-grained
   `operation()` updates inside heavy stages, a 15 s heartbeat thread, and
   a no-progress watchdog that fails the run with `STAGE_STALLED` instead
   of running forever.
4. Progress is read via polling (`GET /analysis-runs/{rid}`) or SSE
   (`GET /analysis-runs/{rid}/events`); both serve the same DB row, so a
   browser refresh or a dropped stream can never lose or fork state.
5. Terminal states: `completed`, `completed_with_warnings` (an optional
   export failed — the terrain analysis itself is intact), `failed`
   (with `error_code`/`error_message`), `cancelled` (cooperative, between
   stages).
6. Recovery: runs left `running` with a stale heartbeat are swept to
   `failed`/`WORKER_LOST` at startup, on every run read, and inside the SSE
   loop. `POST /analysis-runs/{rid}/retry` creates a linked new run from
   the same stored parameters — the original row is never mutated and a
   successful run can never be overwritten.
7. Duplicate-start protection: starting an analysis while one is live
   returns 409 with the active run id (the frontend attaches to it);
   `force: true` bypasses.

## Artifacts

Every standing output of a run is registered in the `artifacts` table
after the file verifiably exists (nonempty), with sha256, size, MIME type,
raster metadata (CRS/bounds/resolution/elevation range) and the algorithm
version. Registered products per run:

results.geojson · dem_utm.tif (processed DTM) · hillshade.png ·
keyline-design-map.tif · slope.tif · flow_accumulation.tif ·
exports/keylines.geojson · exports/keylines.kml · exports/terrain.gpkg ·
exports/design-package.zip

`GET /api/projects/{pid}/artifacts` is the download center (existence and
size re-verified on every listing); `GET …/artifacts/{aid}/download`
re-checks before streaming and answers 410 when a file has vanished.

## Frontend state

One `localStorage` key (`keyline.active`) holds `{projectId, surveyId,
aoi, dtmId, runId}`. On load the app revalidates the project id against
the backend (stale ids are recreated from the stored AOI), re-attaches to
the persisted run id, and restores results — the backend is always the
source of truth; the browser only remembers which ids to ask about.
