# Deployment

## Services

| Service | Role | Command |
|---|---|---|
| backend | FastAPI API | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| worker | RQ worker (same image) | `rq worker keyline --url $REDIS_URL` |
| redis | queue | `redis-server --appendonly yes` |
| minio | S3-compatible object storage (drone photos) | see docker-compose.yml |
| nodeodm | photogrammetry provider | `opendronemap/nodeodm` |
| frontend | Vite build / dev server | `npm run dev` / static build |

## Development (full stack)

```bash
cp .env.example .env       # defaults work
docker compose up --build  # backend :8000, frontend :5173, minio :9000/:9001
```

Bare-metal dev (no Redis/MinIO needed — `ANALYSIS_EXECUTION=auto` falls
back to supervised in-process runs and `STORAGE_BACKEND=local` serves
uploads through the API):

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
cd frontend && npm install && npm run dev     # proxies /api to :8000
```

## Tests / checks

```bash
cd backend && .venv/bin/python -m pytest tests/ -q     # offline; ~6 min
cd frontend && npx tsc --noEmit && npx vitest run && npm run build
```

## Database migrations

Migrations are additive/idempotent and applied automatically on API
startup (`app/migrations.py`; `schema_version` table). To run them
explicitly (e.g. before rolling a new worker):

```bash
cd backend && .venv/bin/python -c "from app import db; db.init_db()"
```

Rollback strategy: migrations never drop or rewrite — deploying a
previous code version against a newer schema is safe (extra columns are
ignored). Restore-from-backup is the escape hatch for data problems
(see docs/disaster-recovery.md).

## Health / readiness

- `GET /api/health` — liveness (no dependencies). Compose healthcheck
  uses this.
- `GET /api/ready` — database/queue/worker-count/queue-depth/storage
  checks; 503 only when a hard dependency for the configured
  `ANALYSIS_EXECUTION` mode is down; `"degraded"` when a fallback covers
  it. Point Kubernetes/Render readiness probes here.

## Environment variables

Core (see `.env.example` for the full annotated list):

| Var | Default | Meaning |
|---|---|---|
| `KEYLINE_DATA` | `backend/data` | project/run output root (mount a volume) |
| `KEYLINE_DB` | `$KEYLINE_DATA/keyline.sqlite` | SQLite path — set explicitly in production |
| `DTM_STORAGE_DIR` | `$KEYLINE_DATA/dtm` | managed DTM library |
| `DTM_ALLOWED_EXTERNAL_ROOTS` | `/data,/app/data` | allowed import roots |
| `REDIS_URL` | `redis://localhost:6379/0` | queue backend |
| `ANALYSIS_EXECUTION` | `auto` | auto \| rq \| inline (prod: `rq`) |
| `ANALYSIS_WORKER_LOST_SECONDS` | 300 | stale-run sweep threshold |
| `ANALYSIS_STAGE_TIMEOUT_SECONDS` | 600 | no-progress watchdog |
| `AUTH_MODE` | `disabled` | disabled \| token |
| `ADMIN_TOKEN` | — | guards /api/admin/* (users, provider URL) |
| `ANALYSIS_RATE_LIMIT_PER_MINUTE` | 10 (token mode) | per-actor analyze throttle |
| `STORAGE_BACKEND` | `local` | local \| s3 (+ `S3_*` vars) |
| `PHOTOGRAMMETRY_PROVIDER` / `NODEODM_URL` | nodeodm / localhost:3000 | provider |
| `APP_VERSION` | `dev` | reported by health endpoints |

## Production notes

- Run API and worker as **separate processes/containers** sharing
  `KEYLINE_DATA` and `DTM_STORAGE_DIR` (compose does this). Set
  `ANALYSIS_EXECUTION=rq` so a queue outage is a visible 503, never a
  silent in-API computation.
- Reverse proxy: forward `/api` to :8000; disable buffering for
  `/analysis-runs/*/events` (SSE; the endpoint sets
  `X-Accel-Buffering: no`). Set request-size limits ≥ your
  `DTM_MAX_UPLOAD_MB`. Keep proxy read timeouts modest — long work runs
  in the worker, not in requests.
- CORS: restrict to your exact frontend origin (see `app/main.py`).
- Graceful shutdown: RQ finishes the current job on SIGTERM (give the
  worker a generous termination grace period); interrupted runs are swept
  to WORKER_LOST and are retryable.
- Backups: see docs/disaster-recovery.md.
