"""SQLite-backed store: projects, drone surveys, analysis runs, DTM library.

Analysis-run states: queued | running | completed | completed_with_warnings
| failed | cancelled. The `analysis_runs` row is the single source of truth
for pipeline state; see app/progress.py for stage/heartbeat semantics.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

DB_PATH = os.environ.get(
    "KEYLINE_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "keyline.sqlite"),
)


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    from . import migrations

    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            aoi TEXT NOT NULL,
            drone_path TEXT,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            state TEXT NOT NULL,
            log TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL
        );
        """)
        migrations.migrate(c)


def create_project(name: str, aoi: dict, org_id: str | None = None,
                   ranch_id: str | None = None) -> str:
    from .migrations import DEFAULT_ORG_ID, DEFAULT_RANCH_ID

    pid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute(
            "INSERT INTO projects (id, name, aoi, org_id, ranch_id, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (pid, name, json.dumps(aoi), org_id or DEFAULT_ORG_ID,
             ranch_id or DEFAULT_RANCH_ID, time.time()),
        )
    return pid


def get_project(pid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["aoi"] = json.loads(d["aoi"])
    return d


def set_drone_path(pid: str, path: str) -> None:
    with _conn() as c:
        c.execute("UPDATE projects SET drone_path=? WHERE id=?", (path, pid))


# The legacy `jobs` table remains in the schema (migrations never drop) but
# is no longer written: analysis_runs is the single source of truth and the
# legacy /status endpoint is derived from it.


# ---------------------------------------------------------------------------
# Drone surveys

_SURVEY_JSON_FIELDS = {"images_json", "options_json", "provider_status_json",
                       "preflight_json", "warnings_json"}


def _survey_row_to_dict(row) -> dict:
    d = dict(row)
    for f in _SURVEY_JSON_FIELDS:
        if d.get(f) is not None:
            try:
                d[f] = json.loads(d[f])
            except (TypeError, json.JSONDecodeError):
                pass
    return d


def create_survey(project_id: str, images: list[dict], options: dict,
                  total_bytes: int) -> str:
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO drone_surveys (id, project_id, image_count, "
            "total_bytes, images_json, options_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, project_id, len(images), total_bytes,
             json.dumps(images), json.dumps(options), now, now),
        )
    return sid


def get_survey(sid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM drone_surveys WHERE id=?", (sid,)).fetchone()
    return _survey_row_to_dict(row) if row else None


def list_surveys(project_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM drone_surveys WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    return [_survey_row_to_dict(r) for r in rows]


def update_survey(sid: str, **fields) -> None:
    """Partial update; dict/list values in JSON columns are serialized."""
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        if k in _SURVEY_JSON_FIELDS and isinstance(v, (dict, list)):
            v = json.dumps(v)
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("updated_at=?")
    vals.append(time.time())
    vals.append(sid)
    with _conn() as c:
        c.execute(f"UPDATE drone_surveys SET {', '.join(cols)} WHERE id=?", vals)


def surveys_in_states(states: list[str]) -> list[dict]:
    q = ",".join("?" for _ in states)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM drone_surveys WHERE state IN ({q})", states
        ).fetchall()
    return [_survey_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Analysis runs (terrain analysis is versioned; photogrammetry is not rerun)

_RUN_JSON_FIELDS = {"params_json", "qa_json", "counts_json", "notices_json",
                    "log_json", "warnings_json", "exports_json",
                    "stage_plan_json"}


def _run_row_to_dict(row) -> dict:
    d = dict(row)
    for f in _RUN_JSON_FIELDS:
        if d.get(f) is not None:
            try:
                d[f] = json.loads(d[f])
            except (TypeError, json.JSONDecodeError):
                pass
    return d


def create_analysis_run(project_id: str, survey_id: str | None,
                        dem_path: str | None, params: dict,
                        retry_of: str | None = None,
                        retry_count: int = 0) -> str:
    rid = uuid.uuid4().hex[:12]
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO analysis_runs (id, project_id, survey_id, dem_path, "
            "params_json, retry_of, retry_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, project_id, survey_id, dem_path, json.dumps(params),
             retry_of, retry_count, now, now),
        )
    return rid


def get_analysis_run(rid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM analysis_runs WHERE id=?", (rid,)).fetchone()
    return _run_row_to_dict(row) if row else None


def list_analysis_runs(project_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM analysis_runs WHERE project_id=? "
            "ORDER BY created_at DESC", (project_id,)).fetchall()
    return [_run_row_to_dict(r) for r in rows]


def latest_completed_run(project_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM analysis_runs WHERE project_id=? AND "
            "state='completed' ORDER BY completed_at DESC LIMIT 1",
            (project_id,)).fetchone()
    return _run_row_to_dict(row) if row else None


def update_analysis_run(rid: str, **fields) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        if k in _RUN_JSON_FIELDS and isinstance(v, (dict, list)):
            v = json.dumps(v)
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("updated_at=?")
    vals.append(time.time())
    vals.append(rid)
    with _conn() as c:
        c.execute(f"UPDATE analysis_runs SET {', '.join(cols)} WHERE id=?", vals)


def append_run_log(rid: str, message: str, *, level: str = "info",
                   stage: str | None = None, cap: int = 400) -> None:
    """Append one technical-log entry to an analysis run (bounded ring)."""
    with _conn() as c:
        row = c.execute("SELECT log_json FROM analysis_runs WHERE id=?",
                        (rid,)).fetchone()
        if row is None:
            return
        try:
            log = json.loads(row["log_json"]) if row["log_json"] else []
        except (TypeError, json.JSONDecodeError):
            log = []
        log.append({"t": time.time(), "level": level, "stage": stage,
                    "msg": message})
        if len(log) > cap:
            log = log[-cap:]
        c.execute("UPDATE analysis_runs SET log_json=?, updated_at=? WHERE id=?",
                  (json.dumps(log), time.time(), rid))


def request_run_cancel(rid: str) -> bool:
    """Flag a run for cooperative cancellation. Returns False if the run is
    already in a terminal state (nothing to cancel)."""
    with _conn() as c:
        row = c.execute("SELECT state FROM analysis_runs WHERE id=?",
                        (rid,)).fetchone()
        if row is None:
            return False
        if row["state"] in ("completed", "completed_with_warnings", "failed",
                            "cancelled"):
            return False
        c.execute("UPDATE analysis_runs SET cancel_requested=1, updated_at=? "
                  "WHERE id=?", (time.time(), rid))
    return True


def run_cancel_requested(rid: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT cancel_requested FROM analysis_runs WHERE id=?",
                        (rid,)).fetchone()
    return bool(row and row["cancel_requested"])


def active_run_for_project(pid: str) -> dict | None:
    """The newest non-terminal analysis run for a project, if any. Used for
    duplicate-start protection; staleness is judged by the caller."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM analysis_runs WHERE project_id=? AND "
            "state IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
            (pid,)).fetchone()
    return _run_row_to_dict(row) if row else None


def sweep_stale_running_runs(stale_after: float) -> list[str]:
    """Mark 'running' runs whose worker stopped heartbeating as failed
    (WORKER_LOST) so they become retryable instead of running forever.
    Returns the affected run ids. Safe to call from any process: the state
    check and heartbeat cutoff are applied atomically in one UPDATE."""
    now = time.time()
    cutoff = now - stale_after
    with _conn() as c:
        rows = c.execute(
            "SELECT id FROM analysis_runs WHERE state='running' AND "
            "COALESCE(heartbeat_at, started_at, created_at) < ?",
            (cutoff,)).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            q = ",".join("?" for _ in ids)
            c.execute(
                f"UPDATE analysis_runs SET state='failed', "
                "error_code='WORKER_LOST', error_message="
                "'The analysis worker stopped reporting progress and is "
                "presumed dead. Retry the analysis.', completed_at=?, "
                f"updated_at=? WHERE id IN ({q}) AND state='running'",
                [now, now, *ids])
    return ids


def claim_analysis_run(rid: str, worker: str, stale_after: float = 120.0) -> bool:
    """Atomically claim a run for one worker so a duplicate worker cannot
    process the same job. Succeeds only when the run is unclaimed, already
    claimed by this same worker, or the previous claim has gone stale (its
    heartbeat is older than ``stale_after`` — the prior worker died). Returns
    True on success."""
    now = time.time()
    cutoff = now - stale_after
    with _conn() as c:
        cur = c.execute(
            "UPDATE analysis_runs SET claimed_by=?, claimed_at=? "
            "WHERE id=? AND ("
            "  claimed_by IS NULL OR claimed_by=? "
            "  OR state IN ('completed','completed_with_warnings','failed','cancelled') "
            "  OR COALESCE(heartbeat_at, claimed_at, 0) < ?)",
            (worker, now, rid, worker, cutoff))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Tenancy: organizations, ranches, users, API tokens, audit log


def create_organization(name: str) -> str:
    oid = "org_" + uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO organizations (id, name, created_at) "
                  "VALUES (?,?,?)", (oid, name, time.time()))
    return oid


def get_organization(oid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM organizations WHERE id=?",
                        (oid,)).fetchone()
    return dict(row) if row else None


def create_ranch(org_id: str, name: str,
                 geometry: dict | None = None) -> str:
    rid = "ranch_" + uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO ranches (id, org_id, name, geometry_json, "
                  "created_at) VALUES (?,?,?,?,?)",
                  (rid, org_id, name,
                   json.dumps(geometry) if geometry else None, time.time()))
    return rid


def list_ranches(org_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM ranches WHERE org_id=? "
                         "ORDER BY created_at", (org_id,)).fetchall()
    return [dict(r) for r in rows]


def create_user(org_id: str, email: str | None, name: str | None,
                role: str = "owner") -> str:
    uid = "user_" + uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO users (id, org_id, email, name, role, "
                  "created_at) VALUES (?,?,?,?,?,?)",
                  (uid, org_id, email, name, role, time.time()))
    return uid


def get_user(uid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


def create_api_token(user_id: str, token_hash: str,
                     label: str | None = None) -> str:
    tid = "tok_" + uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO api_tokens (id, user_id, token_hash, label, "
                  "created_at) VALUES (?,?,?,?,?)",
                  (tid, user_id, token_hash, label, time.time()))
    return tid


def user_for_token_hash(token_hash: str) -> dict | None:
    """The user owning a token, with the token's id attached. Updates
    last_used_at as a side effect (coarse — once per request is fine)."""
    with _conn() as c:
        row = c.execute(
            "SELECT u.*, t.id AS token_id FROM api_tokens t "
            "JOIN users u ON u.id = t.user_id WHERE t.token_hash=?",
            (token_hash,)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?",
                  (time.time(), row["token_id"]))
    return dict(row)


def audit(action: str, *, user_id: str | None = None,
          org_id: str | None = None, resource: str | None = None,
          detail: str | None = None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO audit_log (t, user_id, org_id, action, "
                  "resource, detail) VALUES (?,?,?,?,?,?)",
                  (time.time(), user_id, org_id, action, resource, detail))


# ---------------------------------------------------------------------------
# Artifacts (durable records for every generated/ingested output file)

_ARTIFACT_JSON_FIELDS = {"bounds_json", "resolution_json", "metadata_json"}


def _artifact_row_to_dict(row) -> dict:
    d = dict(row)
    for f in _ARTIFACT_JSON_FIELDS:
        if d.get(f) is not None:
            try:
                d[f] = json.loads(d[f])
            except (TypeError, json.JSONDecodeError):
                pass
    return d


def upsert_artifact(*, project_id: str, run_id: str | None,
                    artifact_type: str, stored_path: str,
                    original_filename: str | None = None,
                    storage_provider: str = "filesystem",
                    size_bytes: int | None = None,
                    checksum_sha256: str | None = None,
                    mime_type: str | None = None,
                    crs: str | None = None,
                    bounds: dict | list | None = None,
                    resolution: list | None = None,
                    width: int | None = None, height: int | None = None,
                    band_count: int | None = None,
                    nodata: float | None = None,
                    elevation_min: float | None = None,
                    elevation_max: float | None = None,
                    algorithm_version: str | None = None,
                    source_artifact_id: str | None = None,
                    created_by: str | None = None,
                    metadata: dict | None = None) -> str:
    """Insert or refresh the artifact record for (run_id, artifact_type).

    Regenerating an export replaces its record (same natural key) so a run
    never lists two versions of the same product."""
    now = time.time()
    with _conn() as c:
        row = None
        if run_id is not None:
            row = c.execute(
                "SELECT id FROM artifacts WHERE run_id=? AND artifact_type=?",
                (run_id, artifact_type)).fetchone()
        aid = row["id"] if row else "art_" + uuid.uuid4().hex[:12]
        c.execute(
            "INSERT OR REPLACE INTO artifacts (id, project_id, run_id, "
            "artifact_type, original_filename, stored_path, storage_provider, "
            "size_bytes, checksum_sha256, mime_type, crs, bounds_json, "
            "resolution_json, width, height, band_count, nodata, "
            "elevation_min, elevation_max, algorithm_version, "
            "source_artifact_id, created_by, metadata_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, project_id, run_id, artifact_type, original_filename,
             stored_path, storage_provider, size_bytes, checksum_sha256,
             mime_type, crs,
             json.dumps(bounds) if bounds is not None else None,
             json.dumps(resolution) if resolution is not None else None,
             width, height, band_count, nodata, elevation_min, elevation_max,
             algorithm_version, source_artifact_id, created_by,
             json.dumps(metadata or {}), now))
    return aid


def get_artifact(aid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    return _artifact_row_to_dict(row) if row else None


def list_artifacts(project_id: str, run_id: str | None = None) -> list[dict]:
    with _conn() as c:
        if run_id is not None:
            rows = c.execute(
                "SELECT * FROM artifacts WHERE project_id=? AND run_id=? "
                "ORDER BY created_at DESC", (project_id, run_id)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM artifacts WHERE project_id=? "
                "ORDER BY created_at DESC", (project_id,)).fetchall()
    return [_artifact_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# DTM library


def _dtm_row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("metadata_json") is not None:
        try:
            d["metadata_json"] = json.loads(d["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            pass
    return d


def create_dtm(*, storage_path: str, display_name: str,
               original_filename: str | None, source_type: str,
               size_bytes: int | None, checksum: str | None,
               crs: str | None, width: int | None, height: int | None,
               nodata: float | None, survey_id: str | None = None,
               project_id: str | None = None, status: str = "ready",
               metadata: dict | None = None,
               org_id: str | None = None) -> str:
    from .migrations import DEFAULT_ORG_ID

    did = "dtm_" + uuid.uuid4().hex[:12]
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO dtms (id, storage_path, display_name, "
            "original_filename, source_type, status, size_bytes, checksum, "
            "crs, width, height, nodata, survey_id, project_id, org_id, "
            "metadata_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, storage_path, display_name, original_filename, source_type,
             status, size_bytes, checksum, crs, width, height, nodata,
             survey_id, project_id, org_id or DEFAULT_ORG_ID,
             json.dumps(metadata or {}), now, now),
        )
    return did


def get_dtm(did: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM dtms WHERE id=?", (did,)).fetchone()
    return _dtm_row_to_dict(row) if row else None


def list_dtms() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM dtms ORDER BY created_at DESC").fetchall()
    return [_dtm_row_to_dict(r) for r in rows]


def find_dtm_by_survey(survey_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM dtms WHERE survey_id=? LIMIT 1",
                        (survey_id,)).fetchone()
    return _dtm_row_to_dict(row) if row else None


def find_dtm_by_path(storage_path: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM dtms WHERE storage_path=? LIMIT 1",
                        (storage_path,)).fetchone()
    return _dtm_row_to_dict(row) if row else None


def update_dtm(did: str, **fields) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        if k == "metadata_json" and isinstance(v, dict):
            v = json.dumps(v)
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("updated_at=?")
    vals.append(time.time())
    vals.append(did)
    with _conn() as c:
        c.execute(f"UPDATE dtms SET {', '.join(cols)} WHERE id=?", vals)


def surveys_with_dtm() -> list[dict]:
    """All surveys that claim a generated DTM, with their project names."""
    with _conn() as c:
        rows = c.execute(
            "SELECT s.id, s.project_id, s.dtm_path, s.completed_at, "
            "p.name AS project_name FROM drone_surveys s "
            "JOIN projects p ON p.id = s.project_id "
            "WHERE s.dtm_path IS NOT NULL").fetchall()
    return [dict(r) for r in rows]
