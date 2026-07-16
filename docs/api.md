# API reference

Interactive docs: `GET /docs` (OpenAPI). In `AUTH_MODE=token`, send
`Authorization: Bearer <token>` on every route except those marked
*public*. Cross-organization access answers 404.

## Health (public)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | liveness, dependency-free |
| GET | `/api/ready` | db/queue/workers/storage; 503 on hard failure |

## Organizations & ranches

| Method | Path | Notes |
|---|---|---|
| GET | `/api/ranches` | ranches in the caller's org |
| POST | `/api/ranches` | `{name, geometry?}` |
| POST | `/api/admin/users` | bootstrap user+org+token; `x-admin-token` guard |

## Projects

| Method | Path | Notes |
|---|---|---|
| POST | `/api/projects` | `{name, aoi, ranch_id?}` → `{project_id}` |
| GET | `/api/projects/{pid}` | summary: org/ranch, has_results, latest_run_id |
| POST | `/api/projects/{pid}/drone-dem` | legacy direct DTM upload (also registers in library) |
| GET | `/api/projects/{pid}/status` | legacy shape, derived from the latest run |
| GET | `/api/projects/{pid}/results` | results.geojson of the effective run |
| GET | `/api/projects/{pid}/hillshade`, `/hillshade-bounds` | PNG overlay |
| POST | `/api/projects/{pid}/keypoints/{kid}/move` | recompute keyline |
| POST | `/api/projects/{pid}/attach-map`, GET `/map` | scan overlay link |

## Analysis runs

| Method | Path | Notes |
|---|---|---|
| POST | `/api/projects/{pid}/analyze` | `{dtm_id?, dem_mode?, terrain?, fill_missing_areas_with_satellite?, force?}` → `{run_id, executor}`; 409 + `active_run_id` when a run is live; rate-limited |
| POST | `/api/projects/{pid}/reanalyze` | re-run with stored DTM/survey; same semantics |
| GET | `/api/projects/{pid}/analysis-runs` | all runs (stale ones swept first) |
| GET | `/api/projects/{pid}/analysis-runs/{rid}` | full run incl. stage plan, progress, health, warnings, log, exports |
| GET | `/api/projects/{pid}/analysis-runs/{rid}/events` | **SSE**: `run` events on change, keepalives, `end` at terminal |
| POST | `/api/projects/{pid}/analysis-runs/{rid}/retry` | new linked run from the same params; 409 for completed/active runs |
| POST | `/api/projects/{pid}/analysis-runs/{rid}/cancel` | cooperative, between stages |
| POST | `/api/projects/{pid}/analysis-runs/{rid}/regenerate-exports` | rebuild exports, no hydrology re-run |

## Artifacts (download center)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/projects/{pid}/artifacts?run_id=` | registered outputs with size/sha256/MIME/created + verified `available` |
| GET | `/api/projects/{pid}/artifacts/{aid}/download` | re-verified stream; 410 when the file vanished |

Run-scoped convenience downloads (same verification philosophy):
`…/analysis-runs/{rid}/downloads/{dtm | keylines.geojson | keylines.kml |
keyline-design-map.tif | design-package.zip}` and availability at
`…/analysis-runs/{rid}/exports`.

## Managed DTM library

| Method | Path | Notes |
|---|---|---|
| GET | `/api/dtms` | org-scoped list (no filesystem paths exposed) |
| GET | `/api/dtms/{dtm_id}` | detail incl. WGS84 footprint for map fly-to |
| POST | `/api/dtms/upload` | multipart GeoTIFF; validated then atomically stored |
| POST | `/api/dtms/validate-path` | dry-run check of an allow-listed server path |
| POST | `/api/dtms/import-path` | ingest an allow-listed server path (copy by default) |

## Drone surveys (photogrammetry)

`/api/projects/{pid}/drone-surveys/*` — create → presign → upload →
complete → start → poll; `/api/photogrammetry/health` (provider health).

## Legacy project-level exports

`/api/projects/{pid}/exports/{availability | keylines.geojson |
keylines.kml | terrain.gpkg | keylines.dxf}` and `/export.kml` — retained
for compatibility; new clients should use run-scoped artifacts.

## Errors

Structured JSON: `{detail, request_id?}`. 401 missing/invalid token,
403 role denied, 404 not found or cross-tenant, 409 conflict (duplicate
start, retry of completed run), 410 vanished artifact, 413 too large,
422 validation, 429 rate limit, 503 hard dependency down. Unhandled
errors return an opaque 500 with a `request_id` that correlates to the
full server-side traceback.
