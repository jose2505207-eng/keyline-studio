"""Worker job end-to-end with the fake provider + local storage — including
the full terrain analysis against the generated synthetic DTM (drone_only
mode, so no network is touched). Also: failure preservation, cancellation,
retry idempotency, and restart reconciliation."""

import json
import os

from app.jobs.photogrammetry_job import reconcile_stale_surveys, run_survey
from app.photogrammetry.models import TaskState
from survey_helpers import aoi_inside_fake_dtm, make_jpeg_bytes


def _ready_survey(env, n=3):
    """Create a project + survey with verified uploads, ready to start."""
    client = env.client
    r = client.post("/api/projects",
                    json={"name": "w", "aoi": aoi_inside_fake_dtm()})
    pid = r.json()["project_id"]
    plan = client.post(f"/api/projects/{pid}/drone-surveys", json={
        "images": [{"filename": f"f{i}.jpg", "type": "image/jpeg",
                    "size": len(make_jpeg_bytes(seed=i))} for i in range(n)],
    }).json()
    sid = plan["survey_id"]
    for i, up in enumerate(plan["uploads"]):
        client.put(f"/api/local-uploads/{up['key']}",
                   content=make_jpeg_bytes(seed=i))
    body = client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/complete-upload").json()
    assert body["ok"]
    return pid, sid


def test_full_survey_lifecycle_completes_with_terrain(drone_env, monkeypatch):
    monkeypatch.setenv("PHOTOGRAMMETRY_POLL_SECONDS", "0")
    pid, sid = _ready_survey(drone_env)
    run_survey(sid)

    s = drone_env.db.get_survey(sid)
    assert s["state"] == "completed", s["error_message"]
    assert s["external_task_id"]
    assert os.path.isfile(s["dtm_path"])
    assert os.path.isfile(s["orthophoto_path"])
    assert os.path.isfile(s["manifest_path"])

    manifest = json.load(open(s["manifest_path"]))
    assert manifest["dtm"]["crs"] == "EPSG:32613"
    assert manifest["dtm"]["aoi_coverage"] > 0.9
    assert manifest["provider"]["name"] == "fake"
    assert manifest["gcp_supplied"] is False

    # provider log preserved
    log_path = os.path.join(os.path.dirname(s["dtm_path"]),
                            "provider-output.log")
    assert os.path.isfile(log_path)

    # terrain results generated from the DTM in drone-only mode
    results = json.load(open(drone_env.data_dir / pid / "results.geojson"))
    props = results["properties"]
    assert props["dem_mode"] == "drone_only"
    assert props["drone_coverage"] >= 0.98
    assert props["warning"] is None
    assert not props["keylines_suppressed"]


def test_provider_failure_preserves_error_and_logs(drone_env, monkeypatch):
    monkeypatch.setenv("PHOTOGRAMMETRY_POLL_SECONDS", "0")
    drone_env.provider.statuses = [TaskState.QUEUED, TaskState.RUNNING,
                                   TaskState.FAILED]
    drone_env.provider.fail_error = "Cannot process dataset: not enough overlap"
    pid, sid = _ready_survey(drone_env)
    run_survey(sid)
    s = drone_env.db.get_survey(sid)
    assert s["state"] == "failed"
    assert "not enough overlap" in s["error_message"]
    assert s["external_task_id"]  # preserved for retry/inspection


def test_provider_unavailable_fails_clearly(drone_env):
    drone_env.provider.healthy = False
    pid, sid = _ready_survey(drone_env)
    run_survey(sid)
    s = drone_env.db.get_survey(sid)
    assert s["state"] == "failed"
    assert "unavailable" in s["error_message"].lower()
    assert drone_env.provider.create_calls == 0


def test_cancellation_before_submit(drone_env):
    pid, sid = _ready_survey(drone_env)
    drone_env.db.update_survey(sid, cancel_requested=1)
    run_survey(sid)
    s = drone_env.db.get_survey(sid)
    assert s["state"] == "cancelled"
    assert drone_env.provider.create_calls == 0


def test_retry_is_idempotent_no_duplicate_provider_task(drone_env, monkeypatch):
    monkeypatch.setenv("PHOTOGRAMMETRY_POLL_SECONDS", "0")
    pid, sid = _ready_survey(drone_env)
    run_survey(sid)
    assert drone_env.provider.create_calls == 1
    ext = drone_env.db.get_survey(sid)["external_task_id"]

    # simulate a failure after the external task existed, then a worker retry
    drone_env.db.update_survey(sid, state="failed",
                               error_message="transient crash")
    drone_env.provider.tasks[ext]["step"] = 0  # provider will replay statuses
    run_survey(sid)
    s = drone_env.db.get_survey(sid)
    assert s["state"] == "completed"
    assert s["external_task_id"] == ext
    assert drone_env.provider.create_calls == 1  # no duplicate task


def test_reconciliation_resumes_or_recovers(drone_env):
    pid, sid1 = _ready_survey(drone_env)
    _, sid2 = _ready_survey(drone_env)
    # sid1 crashed mid-poll with an external task; sid2 crashed pre-submit
    drone_env.db.update_survey(sid1, state="provider_running",
                               external_task_id="ext-123")
    drone_env.db.update_survey(sid2, state="preflight")

    # a worker that is still alive keeps updated_at fresh — reconciliation
    # must leave those surveys alone (no duplicate polling job)
    fresh = reconcile_stale_surveys(enqueue=lambda _s: None,
                                    stale_seconds=120.0)
    assert fresh == []

    enqueued = []
    touched = reconcile_stale_surveys(enqueue=enqueued.append,
                                      stale_seconds=0.0)
    assert set(touched) == {sid1, sid2}
    assert enqueued == [sid1]  # resumes polling of the existing task
    s2 = drone_env.db.get_survey(sid2)
    assert s2["state"] == "uploaded"  # recoverable, never falsely completed
    assert "interrupted" in s2["error_message"]


def test_orthophoto_preview_and_bounds_endpoints(drone_env, monkeypatch):
    monkeypatch.setenv("PHOTOGRAMMETRY_POLL_SECONDS", "0")
    pid, sid = _ready_survey(drone_env)
    run_survey(sid)
    r = drone_env.client.get(f"/api/projects/{pid}/orthophoto")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    b = drone_env.client.get(f"/api/projects/{pid}/orthophoto-bounds").json()
    assert len(b["coordinates"]) == 4
    lon, lat = b["coordinates"][0]
    assert -105 < lon < -103 and 21 < lat < 22
    # original GeoTIFFs downloadable
    for path in ("assets/dtm", "assets/orthophoto"):
        r = drone_env.client.get(f"/api/projects/{pid}/{path}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/tiff"
