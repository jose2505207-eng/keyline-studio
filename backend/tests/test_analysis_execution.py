"""Unified analysis execution: dispatch (rq | inline), duplicate-start
protection, stale-run recovery (WORKER_LOST), and safe retry."""

import time

import pytest

from app import db
from app.jobs import QueueUnavailable


@pytest.fixture()
def project(drone_env):
    ring = [[-104.0, 39.0], [-104.0, 39.01], [-103.99, 39.01],
            [-103.99, 39.0], [-104.0, 39.0]]
    r = drone_env.client.post("/api/projects", json={
        "name": "exec", "aoi": {"type": "Polygon", "coordinates": [ring]}})
    assert r.status_code == 200
    return r.json()["project_id"]


def _mark_running(rid: str, heartbeat_age: float) -> None:
    now = time.time()
    db.update_analysis_run(rid, state="running", started_at=now - heartbeat_age,
                           heartbeat_at=now - heartbeat_age)


def _fresh_run(pid: str, state: str = "failed", **fields) -> str:
    rid = db.create_analysis_run(pid, None, None, {"trigger": "analyze",
                                                   "dem_mode": "auto"})
    if state != "queued" or fields:
        db.update_analysis_run(rid, state=state, **fields)
    return rid


# ---------------------------------------------------------------------------
# migration


def test_migration_6_adds_execution_columns(drone_env):
    import sqlite3

    conn = sqlite3.connect(db.DB_PATH)
    cols = {row[1] for row in
            conn.execute("PRAGMA table_info(analysis_runs)").fetchall()}
    conn.close()
    assert {"executor", "retry_of", "retry_count"} <= cols


# ---------------------------------------------------------------------------
# stale-run sweep


def test_sweep_marks_orphaned_running_run_worker_lost(drone_env, project):
    rid = _fresh_run(project, state="queued")
    _mark_running(rid, heartbeat_age=3600)
    swept = db.sweep_stale_running_runs(300)
    assert rid in swept
    run = db.get_analysis_run(rid)
    assert run["state"] == "failed"
    assert run["error_code"] == "WORKER_LOST"
    assert "Retry" in run["error_message"]


def test_sweep_leaves_live_run_alone(drone_env, project):
    rid = _fresh_run(project, state="queued")
    _mark_running(rid, heartbeat_age=5)
    assert db.sweep_stale_running_runs(300) == []
    assert db.get_analysis_run(rid)["state"] == "running"


def test_run_endpoints_sweep_stale_runs(drone_env, project):
    rid = _fresh_run(project, state="queued")
    _mark_running(rid, heartbeat_age=3600)
    out = drone_env.client.get(
        f"/api/projects/{project}/analysis-runs/{rid}").json()
    assert out["state"] == "failed"
    assert out["error_code"] == "WORKER_LOST"
    assert out["retryable"] is True


# ---------------------------------------------------------------------------
# duplicate-start protection


def test_second_analyze_conflicts_with_active_run(drone_env, project):
    rid = _fresh_run(project, state="queued")
    _mark_running(rid, heartbeat_age=2)
    r = drone_env.client.post(f"/api/projects/{project}/analyze", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["active_run_id"] == rid


def test_force_bypasses_duplicate_guard(drone_env, project, monkeypatch):
    executed = []
    import app.jobs.terrain_job as tj

    monkeypatch.setattr(tj, "execute_analysis_run",
                        lambda rid, **kw: executed.append(rid))
    rid = _fresh_run(project, state="queued")
    _mark_running(rid, heartbeat_age=2)
    r = drone_env.client.post(f"/api/projects/{project}/analyze",
                              json={"force": True})
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] in executed


def test_stale_run_does_not_block_new_analyze(drone_env, project, monkeypatch):
    import app.jobs.terrain_job as tj

    monkeypatch.setattr(tj, "execute_analysis_run", lambda rid, **kw: None)
    rid = _fresh_run(project, state="queued")
    _mark_running(rid, heartbeat_age=3600)
    r = drone_env.client.post(f"/api/projects/{project}/analyze", json={})
    assert r.status_code == 200, r.text
    # and the stale run was honestly failed, not left running
    assert db.get_analysis_run(rid)["state"] == "failed"


# ---------------------------------------------------------------------------
# dispatch


def test_analyze_prefers_rq_when_queue_available(drone_env, project,
                                                 monkeypatch):
    import app.jobs as jobs_pkg

    enqueued = []

    class _Job:
        id = "rq-test-job"

    class _Q:
        def enqueue(self, fn, rid, **kw):
            enqueued.append((fn, rid))
            return _Job()

    monkeypatch.setattr(jobs_pkg, "get_queue", lambda: _Q())
    r = drone_env.client.post(f"/api/projects/{project}/analyze", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executor"] == "rq"
    assert enqueued == [("app.jobs.terrain_job.run_analysis_job",
                         body["run_id"])]
    run = db.get_analysis_run(body["run_id"])
    assert run["executor"] == "rq" and run["rq_job_id"] == "rq-test-job"


def test_analyze_falls_back_inline_when_queue_down(drone_env, project,
                                                   monkeypatch):
    import app.jobs as jobs_pkg
    import app.jobs.terrain_job as tj

    def _raise():
        raise QueueUnavailable("redis down")

    executed = []
    monkeypatch.setattr(jobs_pkg, "get_queue", _raise)
    monkeypatch.setattr(tj, "execute_analysis_run",
                        lambda rid, **kw: executed.append(rid))
    r = drone_env.client.post(f"/api/projects/{project}/analyze", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executor"] == "inline"
    assert executed == [body["run_id"]]
    assert db.get_analysis_run(body["run_id"])["executor"] == "inline"


def test_rq_mode_fails_fast_when_queue_down(drone_env, project, monkeypatch):
    import app.jobs as jobs_pkg

    def _raise():
        raise QueueUnavailable("redis down")

    monkeypatch.setattr(jobs_pkg, "get_queue", _raise)
    monkeypatch.setenv("ANALYSIS_EXECUTION", "rq")
    r = drone_env.client.post(f"/api/projects/{project}/analyze", json={})
    assert r.status_code == 503
    runs = db.list_analysis_runs(project)
    assert runs[0]["state"] == "failed"
    assert runs[0]["error_code"] == "QUEUE_UNAVAILABLE"


def test_inline_mode_never_touches_queue(drone_env, project, monkeypatch):
    import app.jobs as jobs_pkg
    import app.jobs.terrain_job as tj

    def _explode():
        raise AssertionError("queue must not be consulted in inline mode")

    monkeypatch.setattr(jobs_pkg, "get_queue", _explode)
    monkeypatch.setattr(tj, "execute_analysis_run", lambda rid, **kw: None)
    monkeypatch.setenv("ANALYSIS_EXECUTION", "inline")
    r = drone_env.client.post(f"/api/projects/{project}/analyze", json={})
    assert r.status_code == 200, r.text
    assert r.json()["executor"] == "inline"


# ---------------------------------------------------------------------------
# retry


def test_retry_failed_run_creates_linked_new_run(drone_env, project,
                                                 monkeypatch):
    import app.jobs.terrain_job as tj

    executed = []
    monkeypatch.setattr(tj, "execute_analysis_run",
                        lambda rid, **kw: executed.append(rid))
    rid = _fresh_run(project, state="failed", error_code="ANALYSIS_FAILED",
                     error_message="boom")
    r = drone_env.client.post(
        f"/api/projects/{project}/analysis-runs/{rid}/retry")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["retry_of"] == rid
    new = db.get_analysis_run(body["run_id"])
    assert new["retry_of"] == rid and new["retry_count"] == 1
    assert new["params_json"]["trigger"] == "retry"
    assert executed == [body["run_id"]]
    # original run untouched
    assert db.get_analysis_run(rid)["state"] == "failed"


def test_retry_of_completed_run_refused(drone_env, project):
    rid = _fresh_run(project, state="completed")
    r = drone_env.client.post(
        f"/api/projects/{project}/analysis-runs/{rid}/retry")
    assert r.status_code == 409


def test_retry_of_live_run_refused(drone_env, project):
    rid = _fresh_run(project, state="queued")
    _mark_running(rid, heartbeat_age=2)
    r = drone_env.client.post(
        f"/api/projects/{project}/analysis-runs/{rid}/retry")
    assert r.status_code == 409


def test_retry_of_stale_run_allowed_after_sweep(drone_env, project,
                                                monkeypatch):
    import app.jobs.terrain_job as tj

    monkeypatch.setattr(tj, "execute_analysis_run", lambda rid, **kw: None)
    rid = _fresh_run(project, state="queued")
    _mark_running(rid, heartbeat_age=3600)
    r = drone_env.client.post(
        f"/api/projects/{project}/analysis-runs/{rid}/retry")
    assert r.status_code == 200, r.text
    assert db.get_analysis_run(rid)["error_code"] == "WORKER_LOST"


def test_retry_with_missing_dem_file_refused(drone_env, project):
    rid = _fresh_run(project, state="failed")
    db.update_analysis_run(rid, dem_path="/nonexistent/gone.tif")
    r = drone_env.client.post(
        f"/api/projects/{project}/analysis-runs/{rid}/retry")
    assert r.status_code == 422
    assert "no longer available" in r.json()["detail"]


# ---------------------------------------------------------------------------
# legacy /status derived from analysis runs


def test_status_derived_from_latest_run(drone_env, project):
    r = drone_env.client.get(f"/api/projects/{project}/status")
    assert r.json()["state"] == "none"
    rid = _fresh_run(project, state="failed", error_message="boom")
    st = drone_env.client.get(f"/api/projects/{project}/status").json()
    assert st["run_id"] == rid
    assert st["state"].startswith("error:boom")
    db.update_analysis_run(rid, state="completed")
    st = drone_env.client.get(f"/api/projects/{project}/status").json()
    assert st["state"] == "done"
