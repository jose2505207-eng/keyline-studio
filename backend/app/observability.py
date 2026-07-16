"""Health/readiness probes and request correlation.

`/api/health` is pure liveness (no dependencies touched). `/api/ready`
checks every dependency the configured execution mode actually needs and
reports queue/worker/storage/database detail for operators — degraded
dependencies that have a working fallback (e.g. Redis down while
ANALYSIS_EXECUTION=auto) make the service *degraded* but still ready.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from . import config, db

log = logging.getLogger(__name__)

APP_VERSION = os.environ.get("APP_VERSION", "dev")
_STARTED_AT = time.time()


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def health() -> dict:
    return {"status": "ok", "version": APP_VERSION,
            "uptime_seconds": int(time.time() - _STARTED_AT)}


def _check_db() -> dict:
    try:
        import sqlite3

        conn = sqlite3.connect(db.DB_PATH, timeout=5)
        conn.execute("SELECT 1").fetchone()
        n = conn.execute("SELECT COUNT(*) FROM analysis_runs "
                         "WHERE state='running'").fetchone()[0]
        conn.close()
        return {"ok": True, "running_runs": n}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _check_queue() -> dict:
    try:
        import redis
        from rq import Queue, Worker

        conn = redis.Redis.from_url(config.redis_url(),
                                    socket_connect_timeout=2,
                                    socket_timeout=2)
        conn.ping()
        q = Queue("keyline", connection=conn)
        workers = Worker.all(queue=q)
        return {"ok": True, "depth": q.count, "workers": len(workers)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _check_storage() -> dict:
    out: dict = {}
    try:
        d = config.dtm_storage_dir()
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".ready-probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        out["dtm_library"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        out["dtm_library"] = {"ok": False, "error": str(exc)}
    try:
        from .storage import get_storage

        backend = config.storage_backend()
        get_storage()  # constructs + validates configuration
        out["object_storage"] = {"ok": True, "backend": backend}
    except Exception as exc:  # noqa: BLE001
        out["object_storage"] = {"ok": False,
                                 "backend": config.storage_backend(),
                                 "error": str(exc)}
    return out


def readiness() -> tuple[dict, int]:
    """(payload, http_status). 503 only when a hard dependency is down:
    the database always, and the queue only when ANALYSIS_EXECUTION=rq."""
    db_check = _check_db()
    queue_check = _check_queue()
    storage_check = _check_storage()
    mode = config.analysis_execution()

    hard_failures = []
    if not db_check["ok"]:
        hard_failures.append("database")
    if mode == "rq" and not queue_check["ok"]:
        hard_failures.append("queue")
    if not storage_check["dtm_library"]["ok"]:
        hard_failures.append("dtm_library")

    degraded = (not queue_check["ok"] and mode == "auto") or \
        not storage_check["object_storage"]["ok"]
    status = "unavailable" if hard_failures else (
        "degraded" if degraded else "ok")
    payload = {
        "status": status,
        "version": APP_VERSION,
        "analysis_execution": mode,
        "database": db_check,
        "queue": queue_check,
        "storage": storage_check,
        "hard_failures": hard_failures,
    }
    return payload, (503 if hard_failures else 200)
