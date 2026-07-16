"""SSE progress stream: initial state push, terminal end event, and the
gone event for vanished runs."""

import json

import pytest

from app import db


@pytest.fixture()
def project(drone_env):
    ring = [[-104.0, 39.0], [-104.0, 39.01], [-103.99, 39.01],
            [-103.99, 39.0], [-104.0, 39.0]]
    r = drone_env.client.post("/api/projects", json={
        "name": "sse", "aoi": {"type": "Polygon", "coordinates": [ring]}})
    return r.json()["project_id"]


def _events(raw: str) -> list[tuple[str, dict]]:
    out = []
    for block in raw.strip().split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if name:
            out.append((name, data))
    return out


def test_stream_pushes_state_and_ends_on_terminal(drone_env, project):
    rid = db.create_analysis_run(project, None, None, {"trigger": "analyze"})
    db.update_analysis_run(rid, state="completed", progress_percent=100.0)
    with drone_env.client.stream(
            "GET",
            f"/api/projects/{project}/analysis-runs/{rid}/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = _events(body)
    names = [n for n, _ in events]
    assert names == ["run", "end"]
    run_evt = events[0][1]
    assert run_evt["id"] == rid and run_evt["state"] == "completed"
    assert events[1][1]["state"] == "completed"


def test_stream_404s_for_unknown_run(drone_env, project):
    r = drone_env.client.get(
        f"/api/projects/{project}/analysis-runs/nope/events")
    assert r.status_code == 404


def test_stream_sweeps_stale_run_to_worker_lost(drone_env, project):
    import time

    rid = db.create_analysis_run(project, None, None, {"trigger": "analyze"})
    old = time.time() - 3600
    db.update_analysis_run(rid, state="running", started_at=old,
                           heartbeat_at=old)
    with drone_env.client.stream(
            "GET",
            f"/api/projects/{project}/analysis-runs/{rid}/events") as r:
        body = "".join(r.iter_text())
    events = _events(body)
    assert events[0][1]["state"] == "failed"
    assert events[0][1]["error_code"] == "WORKER_LOST"
    assert events[-1][0] == "end"
