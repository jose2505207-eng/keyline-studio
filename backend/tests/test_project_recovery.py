"""Project existence/recovery: the GET summary endpoint the frontend uses to
validate a browser-stored project id, and the 404 behavior that drives
automatic project recreation after an ephemeral-store reset."""

import io

from fake_provider import write_synthetic_dtm
from survey_helpers import aoi_inside_fake_dtm


def _project(env) -> str:
    r = env.client.post("/api/projects",
                        json={"name": "p", "aoi": aoi_inside_fake_dtm()})
    assert r.status_code == 200
    return r.json()["project_id"]


def test_get_existing_project_summary(drone_env):
    pid = _project(drone_env)
    r = drone_env.client.get(f"/api/projects/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == pid
    assert body["has_drone_dtm"] is False
    assert body["has_results"] is False


def test_get_missing_project_is_404(drone_env):
    r = drone_env.client.get("/api/projects/does_not_exist")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_stale_project_analyze_returns_404(drone_env):
    # browser has an id the backend never had (ephemeral reset scenario)
    r = drone_env.client.post("/api/projects/ghost1234567/analyze",
                              json={"dem_mode": "satellite_only"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_stale_project_reanalyze_returns_404(drone_env):
    r = drone_env.client.post("/api/projects/ghost1234567/reanalyze",
                              json={})
    assert r.status_code == 404


def _upload_dtm(env, tmp_path):
    tif = str(tmp_path / "recover.tif")
    write_synthetic_dtm(tif, nodata_corner=False)
    with open(tif, "rb") as f:
        r = env.client.post("/api/dtms/upload",
                            files={"file": ("recover.tif", io.BytesIO(f.read()),
                                            "image/tiff")})
    assert r.status_code == 200
    return r.json()


def test_backend_restart_then_automatic_recreation_and_analysis(drone_env,
                                                                tmp_path,
                                                                monkeypatch):
    """End-to-end recovery: a project exists, the store is wiped (simulating a
    redeploy), the stale id 404s, the client recreates from the DTM footprint
    and analysis then succeeds."""
    import app.db as db

    monkeypatch.setenv("DTM_STORAGE_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("QA_SATELLITE_CROSSCHECK", "0")
    dtm = _upload_dtm(drone_env, tmp_path)
    aoi = aoi_inside_fake_dtm()

    stale = _project(drone_env)
    assert drone_env.client.get(f"/api/projects/{stale}").status_code == 200

    # simulate the ephemeral reset: drop the project row (the DTM library and
    # its file survive because they live under the mounted /data in prod)
    with db._conn() as c:
        c.execute("DELETE FROM projects WHERE id=?", (stale,))

    # the client's stored id is now stale
    assert drone_env.client.get(f"/api/projects/{stale}").status_code == 404
    assert drone_env.client.post(f"/api/projects/{stale}/analyze",
                                 json={"dtm_id": dtm["id"]}).status_code == 404

    # ...so it recreates a project from the same AOI + DTM and analyzes
    fresh = drone_env.client.post(
        "/api/projects", json={"name": "recreated", "aoi": aoi}).json()["project_id"]
    assert fresh != stale
    r = drone_env.client.post(f"/api/projects/{fresh}/analyze",
                              json={"dtm_id": dtm["id"]})
    assert r.status_code == 200, r.text
    import time

    for _ in range(120):
        st = drone_env.client.get(f"/api/projects/{fresh}/status").json()
        if st["state"] == "done" or st["state"].startswith("error"):
            break
        time.sleep(0.5)
    assert st["state"] == "done", st
    fc = drone_env.client.get(f"/api/projects/{fresh}/results").json()
    assert fc["properties"]["dem_mode"] == "drone_only"
    # the recreated project reports itself present and DTM-backed
    summary = drone_env.client.get(f"/api/projects/{fresh}").json()
    assert summary["has_drone_dtm"] and summary["has_results"]
