# Disaster recovery

## What to back up

| Data | Where | Loss impact |
|---|---|---|
| SQLite database | `$KEYLINE_DB` (default `backend/data/keyline.sqlite`) | projects, runs, DTM records, artifacts, users/orgs/tokens, audit log |
| DTM library | `$DTM_STORAGE_DIR` | source terrain inputs |
| Run outputs | `$KEYLINE_DATA/<project>/analysis/…` | regenerable by re-running analysis if the DTM library survives |
| Drone photos | S3/MinIO bucket (`uploads/<project>/<survey>/…`) | photogrammetry re-runs need them |
| Redis | none needed | queue state is transient; runs are DB-backed and recoverable |

## Backup

```bash
# consistent SQLite snapshot without stopping the API (WAL-safe)
sqlite3 "$KEYLINE_DB" ".backup '$BACKUP_DIR/keyline-$(date +%F).sqlite'"
rsync -a "$DTM_STORAGE_DIR/" "$BACKUP_DIR/dtm/"
rsync -a "$KEYLINE_DATA/" "$BACKUP_DIR/data/"      # optional (regenerable)
mc mirror local/keyline-uploads "$BACKUP_DIR/uploads/"   # MinIO
```

Schedule daily; retain per your data-retention policy.

## Restore

1. Stop API + worker.
2. Restore the SQLite file to `$KEYLINE_DB`, the DTM dir, and (optionally)
   the data dir / bucket.
3. Start the API: migrations re-apply idempotently; startup reconciliation
   sweeps any runs that were mid-flight at backup time to a retryable
   state and re-checks survey/provider status. DTM records whose files did
   not survive flip to `status="missing"` and are refused for analysis
   with a clear message.

## Failure behaviors (already engineered)

- **Worker killed mid-run** → heartbeat stops → run swept to
  `WORKER_LOST` (startup, run reads, SSE) → Retry creates a linked new run.
- **Queue down** → `auto`: inline fallback (dev); `rq`: 503 with the run
  marked `QUEUE_UNAVAILABLE`; `/api/ready` reports it.
- **Export failure** → run completes as `completed_with_warnings`; the
  terrain analysis is never demoted; exports can be regenerated
  (`POST …/regenerate-exports`) without re-running hydrology.
- **Artifact file deleted** → listing flips `available:false` with a
  reason; download answers 410; nothing 500s.
- **Ephemeral host reset (no volumes)** → browser session recreates the
  project from its stored AOI + managed DTM automatically.

## Data retention

Uploaded photos remain in object storage until deleted
(`delete_prefix`). Run directories accumulate per analysis; prune old
run directories only after confirming the run is not any project's
latest successful run, or keep them per your traceability requirements
(each is the evidence behind a delivered design).
