# Keyline Studio — Production Readiness Audit

Date: 2026-07-16 · Auditor: principal architecture review · Repo state: `main @ 7b41e96`

This audit was produced by reading the full backend and frontend source, the
test suite, and the deployment configuration, before making structural
changes. It maps the mission's "known failures" to root causes in the current
code, inventories what already works (a lot does), and defines the migration
sequence actually implemented on top of it.

---

## 1. Current architecture

```
Browser (React 18 + MapLibre + Vite, TypeScript)
  └── REST → FastAPI backend (backend/app/main.py + dtm_api.py + surveys_api.py)
        ├── SQLite (backend/data/keyline.sqlite; WAL; additive migrations in app/migrations.py)
        ├── Filesystem artifacts (backend/data/<project>/analysis/<run>/…, DTM library in $DTM_STORAGE_DIR)
        ├── Object storage abstraction (app/storage: local | S3/MinIO) — drone photo uploads only
        └── RQ on Redis (app/jobs) — photogrammetry jobs and /reanalyze terrain jobs
              └── rq worker "keyline" (same code image, separate process)
                    ├── NodeODM (photogrammetry provider, app/photogrammetry/*)
                    └── Terrain pipeline (app/pipeline.py + hydrology/terrain/fusion/spatial/terrain_quality)
```

- **Frontend entry**: `frontend/src/main.tsx` → `App.tsx` (single-page app,
  ~1700 lines). API client in `src/api.ts`. Panels: `DtmPanel` (managed DTM
  library), `DroneSurveyPanel` (photogrammetry), `AnalysisProgressPanel`
  (pipeline status), `GeorefModal` (map-scan georeferencing).
- **Backend entry**: `backend/app/main.py` (FastAPI, uvicorn). Routers:
  core project/analysis routes in `main.py`, `/api/dtms/*` in `dtm_api.py`,
  `/api/projects/{pid}/drone-surveys/*` + `/api/photogrammetry/health` in
  `surveys_api.py`.
- **Database**: SQLite via hand-rolled DAO (`app/db.py`), schema versioned by
  additive migrations (`app/migrations.py`, versions 1–5). Tables: `projects`,
  `jobs` (legacy), `drone_surveys`, `analysis_runs` (rich: stage plan, stage
  index, progress %, heartbeat, claim, cancel flag, log ring, warnings,
  exports), `dtms` (managed DTM library).
- **Jobs**: RQ queue `keyline` (`app/jobs/queue.py`). Photogrammetry survives
  restarts (external task id persisted; startup reconciliation in
  `reconcile_stale_surveys`). Terrain runs execute through
  `jobs/terrain_job.execute_analysis_run`, which claims the run atomically,
  drives a `ProgressReporter` (`app/progress.py`) with real stage
  transitions, a background heartbeat thread, a no-progress watchdog
  (`STAGE_STALLED`), cooperative cancellation, and persists warnings/logs.
- **Storage**: `app/storage` protocol with `LocalStorage` (dev; "presigned"
  PUTs served by the API itself) and `S3Storage` (MinIO-compatible) — used
  for drone photos. DTMs and analysis outputs live on the shared filesystem
  (`backend/data`, `$DTM_STORAGE_DIR`), addressed by `dtm_id` through the
  managed library (`dtm_api.py`); raw path exposure is limited to the
  explicit, root-allowlisted import flow (`resolve_allowed_path`).
- **Deployment**: docker-compose (backend, worker, redis, minio(+init),
  nodeodm, frontend), `render.yaml`, `backend/fly.toml`, Dockerfiles, a
  self-healing tunnel script (`scripts/keyline-tunnel.sh`) with an
  ADMIN_TOKEN-guarded provider-URL endpoint.
- **Tests**: 24 backend test files (workers, migrations, storage, exports,
  progress, project recovery, DTM workflows, synthetic-terrain pipeline
  regression) + 3 frontend vitest files.

## 2. Current end-to-end workflow

1. User draws or imports an AOI → `POST /api/projects` → `project_id`
   (persisted in `localStorage`, revalidated on load; stale ids are
   auto-recreated from the stored AOI).
2. Terrain source: drone photos (presigned upload → RQ photogrammetry job →
   NodeODM → validated DTM → auto terrain run), **existing DTM** (upload to
   the managed library / import from an allow-listed server path / pick a
   library entry), or satellite-only.
3. Analysis: `POST /api/projects/{pid}/analyze` (**in-process
   BackgroundTasks**) or `POST /reanalyze` (**RQ**). Both create an
   `analysis_runs` row first; outputs go to
   `data/<project>/analysis/<run>/` (never overwritten by later runs).
4. Frontend polls `GET /analysis-runs/{rid}` (no-store) and renders the
   persisted stage plan, stage index, progress %, health verdict
   (active/slow/possibly_stalled/worker_missing), warnings, errors; Cancel
   is wired; page refresh restores the run from the DB.
5. On completion the map loads `results.geojson` (keylines, keypoints,
   valleys, ridges, contours as real vector features with per-feature
   metadata) + hillshade PNG overlay; keypoints are draggable
   (`/keypoints/{kid}/move` recomputes the keyline).
6. Downloads: run-scoped original DTM, keylines GeoJSON/KML, visual GeoTIFF,
   design-package ZIP (atomic build, manifest + checksums inside), plus
   legacy project-level GeoJSON/KML/GPKG/DXF exports.

## 3. Root causes of the known failures

| # | Known failure | Root cause | Status |
|---|---|---|---|
| 1 | Analysis appears frozen | Historic: no stage persistence; later fixed by `ProgressReporter` + heartbeat + stage plan. Residual: heavy Whitebox/GDAL calls emit no intra-stage progress (only heartbeat), and an unbounded fusion grid could stall (fixed in `7b41e96` by `ANALYSIS_MAX_GRID_CELLS`). | Largely fixed; intra-stage `operation()` used for satellite fetch/fusion path only |
| 2 | No visible pipeline stage/progress | Same as above — solved by migrations 4–5 + `AnalysisProgressPanel`. | Fixed |
| 3 | Existing-DTM import completes without navigating/showing keylines | Frontend didn't fly to DTM bounds nor auto-open results; fixed in `b26ab3d`/`34c3db5` (footprint/bbox in DTM metadata, auto-locate). | Fixed |
| 4 | Reanalysis fails: project/DTM path not found | Ephemeral hosting wiped SQLite + `drone_path` pointed at temp upload paths. Mitigations: project auto-recovery, managed DTM library keyed by `dtm_id`, availability re-check before queueing. Residual: `projects.drone_path` is still a raw path column used by legacy flows. | Mostly fixed; legacy path column remains |
| 5 | Reliance on arbitrary local filepaths | DTM library (`dtm_id`) + `resolve_allowed_path` allow-list now cover the normal workflow. Residual: legacy `/drone-dem` upload writes `data/<pid>/drone_dem.tif` and sets `drone_path`; run outputs addressed by directory convention rather than artifact records. | Partially fixed |
| 6 | Refresh loses job state | Fixed: run state lives in `analysis_runs`; frontend recovers via `localStorage` project id + run polling. | Fixed |
| 7 | Results not consistently persisted/reopened | Fixed by versioned run directories + `latest_completed_run` fallback logic (`_results_dir`). | Fixed |
| 8 | Download workflow incomplete | Run-scoped downloads exist and check file existence, but there is **no durable artifact registry** (no size/checksum/mime records, no single download-center listing), no run-scoped GPKG/processed-DTM/hillshade-GeoTIFF products. | Partially fixed |
| 9 | Vague/invisible errors | Error codes + messages + log ring + health classifier now exist. Residual: no request-id correlation, no central error middleware, stack traces can leak via generic 500s. | Partially fixed |
| 10 | No single source of truth | `analysis_runs` is now authoritative for runs; `dtms` for DTMs. Residual: **the `/analyze` path still executes in the API process** (BackgroundTasks) while `/reanalyze` uses RQ — two execution paths with different failure/recovery semantics; legacy `jobs` table duplicates run state; legacy project-level export endpoints duplicate run-scoped ones. | Partially fixed |

## 4. High-risk technical debt (found in this audit)

1. **Dual execution paths.** `POST /analyze` runs the pipeline via FastAPI
   `BackgroundTasks` inside the API process (`main.py:_run_job`): an API
   restart/redeploy kills the analysis silently; uvicorn worker CPU is
   consumed by hydrology. `/reanalyze` uses RQ. One code path must win.
2. **No startup recovery for analysis runs.** Surveys are reconciled at
   startup (`reconcile_stale_surveys`), but an `analysis_runs` row left
   `running` by a dead process stays `running` forever in the DB (the
   health classifier reports `worker_missing`, but state never transitions
   and there is **no retry endpoint**).
3. **No retry.** The mission requires `POST …/retry`; the UI can only start
   a brand-new analysis, losing the failed run's parameters linkage.
4. **No durable artifact registry.** Downloads are resolved by
   filename-convention probing (`_run_downloads`). No checksums, sizes,
   MIME types, or provenance records for outputs; no verification API.
5. **No authentication / authorization / tenancy at all.** Every project,
   run, DTM, and download is world-readable and world-writable to anyone
   who can reach the API. There are no `users`/`organizations`/`ranches`
   tables. Acceptable for a single-user local tool; disqualifying for
   hosted multi-tenant use.
6. **No general health/readiness endpoints.** Only
   `/api/photogrammetry/health`. Compose healthcheck abuses `/docs`.
7. **No SSE/WebSocket** — polling only (works, but the mission asks for
   push with polling fallback).
8. **Legacy duplication:** `jobs` table vs `analysis_runs`;
   project-level `/exports/*` vs run-scoped `/analysis-runs/{rid}/downloads/*`;
   `projects.drone_path` vs `dtms.storage_path`; legacy
   `/api/projects/{pid}/drone-dem` upload vs `/api/dtms/upload`.
9. **Observability gaps:** no request ids, no central exception handler,
   no metrics, logs are unstructured strings.
10. **SQLite under concurrency.** WAL + short transactions are fine for the
    current write volume; a hosted multi-tenant deployment would need
    Postgres. All DAO SQL is parameterized (no injection found).
11. **`md5` used for checksums** (fine for integrity, not for security —
    document or upgrade to sha256).
12. **CORS allows any `*.vercel.app` origin** — acceptable while there is
    no auth (nothing to steal cross-origin), but must be tightened the
    moment credentials exist.

## 5. Existing reusable components (keep, do not rewrite)

- `app/progress.py` — honest stage plans, monotonic progress, heartbeat
  thread, stall watchdog, health classifier. **Reuse as-is.**
- `app/migrations.py` — additive, idempotent, multi-process-safe. Extend.
- `app/db.py` DAO incl. `claim_analysis_run` duplicate-worker guard.
- `app/dtm_api.py` — managed library, path allow-listing, raster
  inspection/validation (CRS, bands, nodata, plausible elevations,
  WGS84 footprint), atomic upload with size limits.
- `app/exports.py` + `visual_export.py` — atomic ZIP with manifest,
  honest `ExportUnavailable` reasons.
- `app/jobs/*` — RQ integration, survey reconciliation, terrain job with
  claim + cleanup + "export failure never demotes a completed analysis".
- `app/spatial.py` — atomic writes, result-bounds validation.
- The whole photogrammetry subsystem and the frontend panels.

## 6. Proposed target architecture (implemented in this pass)

Single execution path: every analysis (analyze/reanalyze/survey-triggered)
is an `analysis_runs` row executed by `execute_analysis_run`, dispatched
**via RQ when Redis is reachable** and via a supervised in-process thread
only as an explicit local-dev fallback (`ANALYSIS_EXECUTION=auto|rq|inline`),
recorded on the run (`executor` column). Runs left `running`/`queued` with a
stale heartbeat are swept at startup and on read into a recoverable
`failed` (`WORKER_LOST`) state; `POST …/analysis-runs/{rid}/retry` creates a
fresh run from the same parameters (never mutating the old row).

Artifacts become first-class rows (`artifacts` table: type, filename, size,
sha256, mime, CRS, bounds, run/project provenance, algorithm version)
registered atomically after each successful export; the download center API
lists only verified-on-disk artifacts and every download endpoint checks the
registry + file before streaming.

Tenancy: `organizations` and `ranches` tables above `projects`
(`Organization → Ranch → Project → Analysis Run → Artifacts`), backfilled
with a default org/ranch for existing rows; optional token auth
(`AUTH_MODE=disabled|token`) enforcing org scoping on every project-nested
route when enabled — disabled by default so existing local single-user
deployments keep working unchanged.

Observability: `/api/health` (liveness) and `/api/ready`
(db/queue/storage checks), request-id middleware, structured key=value
logging with run/project ids, central exception handler that hides
internals from clients and logs full tracebacks.

## 7. Migration sequence

1. `docs/production-readiness-audit.md` (this file).
2. Migration 6: `analysis_runs.executor`, `retry_of`, `retry_count`;
   `artifacts` table; `organizations`/`ranches`/`users`/`api_tokens`
   tables + default backfill; `projects.org_id`, `projects.ranch_id`.
3. Analyze/reanalyze unification + retry + startup sweep (backend + tests).
4. SSE events endpoint (DB-poll bridge, no new infra) + frontend consumption
   with polling fallback.
5. Artifact registration in `generate_run_exports` + download-center API +
   run-scoped GPKG/processed-DTM products + verified downloads (+ frontend).
6. Auth middleware + org scoping + cross-tenant tests.
7. Health/readiness + request ids + error middleware.
8. Docs (`architecture`, `deployment`, `storage`, `analysis-pipeline`,
   `security`, `disaster-recovery`, `api`) + README refresh.

## 8. Files and modules expected to change

- `backend/app/migrations.py`, `db.py` (new tables/columns + DAO)
- `backend/app/main.py` (analyze dispatch, retry, SSE, downloads, middleware)
- `backend/app/jobs/terrain_job.py` (executor recording, artifact registration)
- `backend/app/exports.py` (run-scoped GPKG, artifact manifest)
- new: `backend/app/artifacts.py`, `backend/app/auth.py`, `backend/app/events.py`, `backend/app/observability.py`
- `backend/tests/*` (new coverage), `frontend/src/api.ts`,
  `AnalysisProgressPanel.tsx`, `App.tsx` (retry/SSE/download center)
- `README.md`, `docs/*.md`, `docker-compose.yml` (healthchecks)

## 9. Risks of the migration

- **SQLite lock contention** from the SSE bridge and heartbeats — mitigated:
  SSE reads reuse the same short-lived WAL connections and a coarse poll
  interval; SSE is read-only.
- **Behavior change on `/analyze`**: now enqueues to RQ when Redis is up.
  A deployment with Redis configured but **no worker running** would queue
  forever — mitigated by readiness checks, queue-depth surfacing, and the
  documented `ANALYSIS_EXECUTION=inline` escape hatch.
- **Auth is opt-in**: enabling `AUTH_MODE=token` on an existing deployment
  requires creating a token first (documented); default stays open for
  backward compatibility, which must be revisited before any hosted
  multi-tenant launch.
- **Existing data**: all migrations are additive; existing projects are
  backfilled into the default org/ranch; no user file is deleted or moved.
- The frontend keeps polling as the source of truth if SSE fails, so a
  proxy that buffers SSE cannot break progress display.

## 10. Frontend audit findings

- **State restore**: one `localStorage` key `keyline.active`
  (`{projectId, surveyId, aoi, dtmId, runId}`, `App.tsx:32-48`). On load the
  app revalidates the project id against the API and recreates stale
  projects from the stored AOI (`projectFlow.ts:19-32`), re-arms the run
  monitor from the persisted `runId` (`App.tsx:308`), and re-fetches
  orthophoto + results. Not persisted: map-scan overlay state, advanced
  terrain params, satellite-fill checkbox, `lastAnalyzeOptions` (so a
  post-refresh Retry falls back to `/reanalyze` defaults).
- **Progress**: polling only (confirmed — no EventSource/WebSocket anywhere).
  Adaptive 2 s/5 s cadence, visibility-aware, exponential backoff
  (`App.tsx:341-405`). `AnalysisProgressPanel` renders the persisted stage
  plan (✓/●/○), overall %, stage X of Y, elapsed, heartbeat age, health
  messages, warnings, error code/message, technical log, Cancel (when
  `cancellable`), Retry (only when failed/cancelled/`worker_missing`), and
  Re-run. **Gap**: a `possibly_stalled` run shows a warning but no action
  button. Retry re-invokes `/analyze` with remembered options — there is no
  backend retry endpoint tied to the failed run.
- **Downloads**: two systems — run-scoped downloads (`…/downloads/*`) where
  the keyline buttons are availability-gated but Original DTM / design map /
  ZIP are always enabled with no pre-check, and legacy project-level
  `Export ▾` (`/exports/*`) gated by the availability endpoint. No artifact
  metadata (size/created/checksum) is shown anywhere.
- **DTM workflow**: library list/select with disabled non-ready entries,
  XHR upload with real progress, import-by-server-path with pre-validation,
  metadata display (CRS/size/resolution/elevation range/valid %), automatic
  fly-to on selection (`App.tsx:767-807`).
- **Analyze**: single entry point sending `dtm_id` + mode + satellite-fill
  flag; DTM source of truth is `lastDtmRef`/`dtmId` flowing from DtmPanel.
- **Map**: hillshade/contours/valleys/ridges/keylines/keypoints layers with
  toggles; keypoints are draggable with optimistic patch + server reconcile;
  keylines are real vector features with metadata popups.
- **Dead code**: legacy `api.startAnalysis`/`getStatus` job flow,
  `api.uploadDroneDem`, `api.regenerateExports` (unused client-side), inert
  `pollRef`/`rerunTimer` scaffolding, unused `droneName/droneInfo/isDsm`
  state, dead locals in `DroneSurveyPanel.retryFailed`.
