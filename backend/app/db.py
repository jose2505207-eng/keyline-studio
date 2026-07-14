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
