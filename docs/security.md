# Security

## Authentication & tenancy

- `AUTH_MODE=disabled` (default): single-tenant; every request acts as the
  default organization's owner. Suitable for local/self-hosted single-user
  deployments only. **Do not host multi-tenant with auth disabled.**
- `AUTH_MODE=token`: every `/api` route requires
  `Authorization: Bearer <token>` except `/docs`, `/openapi.json`,
  `/api/health`, `/api/ready`, and the `ADMIN_TOKEN`-guarded `/api/admin/*`
  routes. Tokens are 256-bit random values (`kls_…`) shown once at
  creation; only their sha256 is stored (`api_tokens.token_hash`).

Bootstrap (token mode):

```bash
curl -X POST $API/api/admin/users \
  -H "x-admin-token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"email":"owner@ranch.com","role":"owner","org_name":"My Ranch Co"}'
# → { user_id, org_id, role, token }   # store the token now
```

## Authorization

- Roles: `owner`, `admin`, `designer`, `field_operator` (write),
  `viewer` (read-only — mutating requests answer 403).
- Org scoping is enforced in middleware for every `/api/projects/{pid}…`
  and `/api/dtms/{dtm_id}…` route: cross-tenant access answers **404**
  (existence is not leaked). DTM library listings are filtered per org.
  Covered by `tests/test_tenancy.py`.
- Artifact downloads verify project membership + file existence before
  streaming.

## Hardening in place

- Path allow-listing for server-side DTM imports
  (`DTM_ALLOWED_EXTERNAL_ROOTS`; realpath containment, no traversal).
- Upload limits: `DTM_MAX_UPLOAD_MB` (default 1024),
  `DRONE_MAX_FILE_BYTES`/`DRONE_MAX_TOTAL_BYTES`; extension + raster
  validation before a file is accepted; uploads validated before the
  atomic rename into the library.
- Filenames sanitized (`safe_filename`, `_sanitize_name`); ZIP archive
  names are fixed literals under a sanitized folder (no traversal).
- Rate limiting on analyze/reanalyze/retry: 10/min/actor in token mode
  (`ANALYSIS_RATE_LIMIT_PER_MINUTE` to tune or to enable in disabled
  mode). In-process only — add proxy-level limits for multi-replica.
- Audit log (`audit_log` table) for analysis starts/cancels/retries,
  DTM uploads/imports, and downloads, with user/org/request id.
- Central error boundary: clients receive `Internal server error` + a
  request id; tracebacks stay in server logs.
- Security headers on every response (`X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`);
  `X-Request-ID` correlation.
- All SQL is parameterized; no shell commands are built from user input.
- Secrets come only from environment variables; `.env` is gitignored and
  `.env.example` contains no real secrets. No secrets are baked into the
  frontend bundle (only `VITE_API_BASE`).

## Known gaps (be honest before an enterprise launch)

- No CSRF tokens: the API is a bearer-token JSON API (no cookies), so
  classic CSRF does not apply while that remains true. Do not add cookie
  sessions without adding CSRF protection.
- CORS allows `*.vercel.app` origins — tighten to the exact frontend
  origin before enabling token auth in production.
- Token issuance is admin-driven; there is no self-service login, token
  rotation UI, password flow, or SSO/OIDC. The tenancy layer is designed
  so an OIDC provider can replace token issuance without touching
  enforcement.
- SQLite has no at-rest encryption; use full-disk encryption or move to
  Postgres for stricter requirements.
- The maps/scan endpoints (`/api/maps/*`) are authenticated but not yet
  org-scoped (no org column on map records).
