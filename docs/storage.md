# Storage

## Three storage domains

1. **Object storage (S3/MinIO)** — drone photo uploads only. Browser PUTs
   directly with presigned URLs; the API never proxies image bytes.
   `STORAGE_BACKEND=local` (dev/tests) serves "presigned" PUTs from the
   API itself under `backend/data/uploads`.
2. **Managed DTM library** (`DTM_STORAGE_DIR`, compose: `/data/dtm`) —
   every DTM is addressed by a stable `dtm_id`; list/detail responses
   never expose filesystem paths. Entry paths:
   - browser upload (`POST /api/dtms/upload`, size-limited, validated,
     atomic rename after validation, md5 checksum),
   - controlled server-path import (`POST /api/dtms/import-path`) —
     the path must resolve inside `DTM_ALLOWED_EXTERNAL_ROOTS`
     (symlinks/.. collapsed); the file is copied into the library by
     default (`copy_to_library=false` keeps it in place as
     `external_path` with provenance),
   - automatic registration of survey-generated DTMs.
   Records whose file has vanished flip to `status="missing"` on read and
   are refused for analysis before anything is queued.
3. **Run outputs** — `data/<project>/analysis/<run>/…`, one directory per
   run, never overwritten by later runs. All artifacts are additionally
   registered in the `artifacts` table (sha256, size, MIME, raster
   metadata) and served only through the verifying download endpoints.

## Layout

```
$KEYLINE_DATA/
  keyline.sqlite                  # unless KEYLINE_DB points elsewhere
  <project_id>/
    drone_dem.tif                 # legacy direct upload (also registered in the library)
    photogrammetry/{drone_dtm.tif, orthophoto.tif, manifest.json, provider-output.log}
    analysis/<run_id>/
      dem_utm.tif                 # processed, analysis-ready DEM
      hillshade.png + .pgw + hillshade_bounds.json
      results.geojson             # all vector layers + run metadata
      slope.tif, flow_accumulation.tif
      keyline-design-map.tif      # visual (NOT elevation)
      meta.json
      exports/{keylines.geojson, keylines.kml, terrain.gpkg, design-package.zip}
  maps/<map_id>/…                 # georeferenced scan overlays
$DTM_STORAGE_DIR/
  dtm_<12hex>.tif                 # managed library files (collision-safe names)
```

## Guarantees

- Atomic writes: JSON/rasters via temp+rename (`app/spatial.py`), ZIPs
  built to `*.zip.tmp` then renamed; aborted runs clean `*.tmp` files.
- Artifact registration happens only after the file exists and is
  nonempty; downloads re-verify existence + size and answer 410 for
  vanished files.
- Nothing user-uploaded is ever deleted by the application; retries write
  into a new run directory.
- Persistence: set `KEYLINE_DB` and mount `KEYLINE_DATA`/`DTM_STORAGE_DIR`
  on durable volumes. On ephemeral hosts the frontend auto-recreates
  projects from the stored AOI + managed DTM (degraded but not broken).
