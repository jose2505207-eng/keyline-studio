"""Survey API: upload planning, presign, verification, limits, ownership,
GCP, start/cancel/retry conflicts, and the provider health endpoint."""

from survey_helpers import VALID_GCP, aoi_inside_fake_dtm, make_jpeg_bytes


def _project(env, aoi=None) -> str:
    r = env.client.post("/api/projects", json={
        "name": "t", "aoi": aoi or aoi_inside_fake_dtm()})
    assert r.status_code == 200
    return r.json()["project_id"]


def _plan(env, pid, n=3, size=1000):
    return env.client.post(f"/api/projects/{pid}/drone-surveys", json={
        "images": [{"filename": f"IMG_{i:04d}.JPG", "type": "image/jpeg",
                    "size": size} for i in range(n)],
        "options": {},
    })


def _upload_all(env, pid, sid, plan, data=b"x" * 1000):
    for up in plan["uploads"]:
        r = env.client.put(f"/api/local-uploads/{up['key']}", content=data,
                           headers=up["headers"])
        assert r.status_code == 200, r.text
    r = env.client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/complete-upload")
    assert r.status_code == 200
    return r.json()


def test_create_survey_returns_plan_with_uuid_keys(drone_env):
    pid = _project(drone_env)
    r = _plan(drone_env, pid)
    assert r.status_code == 200
    plan = r.json()
    assert len(plan["uploads"]) == 3
    for up in plan["uploads"]:
        assert up["key"].startswith(f"uploads/{pid}/{plan['survey_id']}/")
        assert "IMG_" not in up["key"]  # user filename is metadata only
        assert up["filename"].startswith("IMG_")


def test_reject_bad_extension_and_mime(drone_env):
    pid = _project(drone_env)
    r = drone_env.client.post(f"/api/projects/{pid}/drone-surveys", json={
        "images": [{"filename": "run.exe", "type": "image/jpeg", "size": 10}]})
    assert r.status_code == 422 and ".jpg" in r.json()["detail"]
    r = drone_env.client.post(f"/api/projects/{pid}/drone-surveys", json={
        "images": [{"filename": "a.jpg", "type": "text/html", "size": 10}]})
    assert r.status_code == 422 and "image/jpeg" in r.json()["detail"]


def test_reject_too_many_files_and_oversize(drone_env, monkeypatch):
    monkeypatch.setenv("DRONE_MAX_IMAGES", "5")
    pid = _project(drone_env)
    assert _plan(drone_env, pid, n=6).status_code == 422
    monkeypatch.setenv("DRONE_MAX_FILE_BYTES", "100")
    assert _plan(drone_env, pid, n=3, size=1000).status_code == 422


def test_path_traversal_filenames_are_neutralized(drone_env):
    pid = _project(drone_env)
    r = drone_env.client.post(f"/api/projects/{pid}/drone-surveys", json={
        "images": [{"filename": "../../evil.jpg", "type": "image/jpeg",
                    "size": 10}] * 3})
    assert r.status_code == 200
    for up in r.json()["uploads"]:
        assert ".." not in up["key"]
        assert up["filename"] == "evil.jpg"


def test_survey_ownership_enforced(drone_env):
    pid_a = _project(drone_env)
    pid_b = _project(drone_env)
    sid = _plan(drone_env, pid_a).json()["survey_id"]
    r = drone_env.client.get(
        f"/api/projects/{pid_b}/drone-surveys/{sid}")
    assert r.status_code == 404


def test_presign_rejects_foreign_keys(drone_env):
    pid = _project(drone_env)
    sid = _plan(drone_env, pid).json()["survey_id"]
    r = drone_env.client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/presign",
        json={"keys": ["uploads/other/whatever.jpg"]})
    assert r.status_code == 422


def test_complete_upload_verifies_existence_and_size(drone_env):
    pid = _project(drone_env)
    plan = _plan(drone_env, pid).json()
    sid = plan["survey_id"]
    # nothing uploaded yet
    r = drone_env.client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/complete-upload")
    body = r.json()
    assert not body["ok"] and len(body["missing"]) == 3
    # upload one with wrong size
    up0 = plan["uploads"][0]
    drone_env.client.put(f"/api/local-uploads/{up0['key']}", content=b"short")
    body = drone_env.client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/complete-upload").json()
    assert body["size_mismatch"] and not body["ok"]
    # upload all correctly
    body = _upload_all(drone_env, pid, sid, plan)
    assert body["ok"] and body["uploaded_count"] == 3
    assert drone_env.db.get_survey(sid)["state"] == "uploaded"


def test_start_requires_verified_upload(drone_env):
    pid = _project(drone_env)
    sid = _plan(drone_env, pid).json()["survey_id"]
    r = drone_env.client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/start")
    assert r.status_code == 409


def test_gcp_upload_validates_format(drone_env):
    pid = _project(drone_env)
    sid = _plan(drone_env, pid).json()["survey_id"]
    r = drone_env.client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/gcp",
        files={"file": ("gcp_list.txt", b"not a gcp file", "text/plain")})
    assert r.status_code == 422
    r = drone_env.client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/gcp",
        files={"file": ("gcp_list.txt", VALID_GCP, "text/plain")})
    assert r.status_code == 200
    assert drone_env.db.get_survey(sid)["gcp_key"]


def test_cancel_and_retry_state_conflicts(drone_env):
    pid = _project(drone_env)
    sid = _plan(drone_env, pid).json()["survey_id"]
    drone_env.db.update_survey(sid, state="completed")
    assert drone_env.client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/cancel").status_code == 409
    assert drone_env.client.post(
        f"/api/projects/{pid}/drone-surveys/{sid}/retry").status_code == 409


def test_photogrammetry_health_endpoint(drone_env):
    r = drone_env.client.get("/api/photogrammetry/health")
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True and body["provider"] == "fake"
    assert "token" not in str(body).lower()


def test_provider_unavailable_reported_by_health(drone_env):
    drone_env.provider.healthy = False
    body = drone_env.client.get("/api/photogrammetry/health").json()
    assert body["reachable"] is False and body["error"]


def test_local_upload_rejects_traversal(drone_env):
    r = drone_env.client.put("/api/local-uploads/uploads/../../etc/passwd",
                             content=b"x")
    assert r.status_code in (404, 422)


def test_jpeg_bytes_fixture_is_valid_jpeg():
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(make_jpeg_bytes()))
    assert img.format == "JPEG"
