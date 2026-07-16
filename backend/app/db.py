"""SQLite-backed project + job store (no Redis/Celery — minimal infra).

Job states: queued | running:<step> | done | error:<message>
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


def create_project(name: str, aoi: dict) -> str:
    pid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute(
            "INSERT INTO projects (id, name, aoi, created_at) VALUES (?,?,?,?)",
            (pid, name, json.dumps(aoi), time.time()),
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


def create_job(pid: str) -> str:
    jid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, project_id, state, updated_at) VALUES (?,?,?,?)",
            (jid, pid, "queued", time.time()),
        )
    return jid


def update_job(jid: str, state: str, log_line: str | None = None) -> None:
    with _conn() as c:
        if log_line is not None:
            row = c.execute("SELECT log FROM jobs WHERE id=?", (jid,)).fetchone()
            log = json.loads(row["log"]) if row else []
            log.append({"t": time.time(), "msg": log_line})
            c.execute("UPDATE jobs SET state=?, log=?, updated_at=? WHERE id=?",
                      (state, json.dumps(log), time.time(), jid))
        else:
            c.execute("UPDATE jobs SET state=?, updated_at=? WHERE id=?",
                      (state, time.time(), jid))


def latest_job(pid: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM jobs WHERE project_id=? ORDER BY updated_at DESC LIMIT 1",
            (pid,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["log"] = json.loads(d["log"])
    return d


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
                        dem_path: str | None, params: dict) -> str:
    rid = uuid.uuid4().hex[:12]
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO analysis_runs (id, project_id, survey_id, dem_path, "
            "params_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (rid, project_id, survey_id, dem_path, json.dumps(params), now, now),
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
               metadata: dict | None = None) -> str:
    did = "dtm_" + uuid.uuid4().hex[:12]
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO dtms (id, storage_path, display_name, "
            "original_filename, source_type, status, size_bytes, checksum, "
            "crs, width, height, nodata, survey_id, project_id, "
            "metadata_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, storage_path, display_name, original_filename, source_type,
             status, size_bytes, checksum, crs, width, height, nodata,
             survey_id, project_id, json.dumps(metadata or {}), now, now),
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
