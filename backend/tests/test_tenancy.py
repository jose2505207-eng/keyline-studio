"""Tenancy + optional token auth: cross-organization isolation, roles,
rate limiting, and backward-compatible disabled mode."""

import json

import pytest

from app import db
from survey_helpers import aoi_inside_fake_dtm

ADMIN = {"x-admin-token": "test-admin-secret"}


@pytest.fixture()
def auth_env(drone_env, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-secret")
    return drone_env


def _mk_user(env, email, role="owner", org_id=None, org_name=None):
    r = env.client.post("/api/admin/users", headers=ADMIN, json={
        "email": email, "role": role, "org_id": org_id,
        "org_name": org_name or email})
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {"Authorization": f"Bearer {body['token']}"}


def _mk_project(env, headers, name="p"):
    r = env.client.post("/api/projects", headers=headers, json={
        "name": name, "aoi": aoi_inside_fake_dtm()})
    assert r.status_code == 200, r.text
    return r.json()["project_id"]


# ---------------------------------------------------------------------------
# authentication


def test_requests_require_token_in_token_mode(auth_env):
    r = auth_env.client.get("/api/dtms")
    assert r.status_code == 401
    r = auth_env.client.post("/api/projects", json={
        "name": "x", "aoi": aoi_inside_fake_dtm()})
    assert r.status_code == 401


def test_invalid_token_rejected(auth_env):
    r = auth_env.client.get(
        "/api/dtms", headers={"Authorization": "Bearer kls_bogus"})
    assert r.status_code == 401


def test_docs_stay_public(auth_env):
    assert auth_env.client.get("/docs").status_code == 200


def test_disabled_mode_needs_no_token(drone_env, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    r = drone_env.client.post("/api/projects", json={
        "name": "open", "aoi": aoi_inside_fake_dtm()})
    assert r.status_code == 200
    # backfilled into the default organization
    proj = db.get_project(r.json()["project_id"])
    assert proj["org_id"] == "org_default"


def test_admin_user_creation_requires_admin_token(auth_env):
    r = auth_env.client.post("/api/admin/users", json={"email": "e@x.com"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# cross-organization isolation


def test_projects_isolated_between_orgs(auth_env):
    _, h_a = _mk_user(auth_env, "a@a.com")
    _, h_b = _mk_user(auth_env, "b@b.com")
    pid = _mk_project(auth_env, h_a, "ranch-a")

    assert auth_env.client.get(f"/api/projects/{pid}",
                               headers=h_a).status_code == 200
    # another org: existence is not revealed
    assert auth_env.client.get(f"/api/projects/{pid}",
                               headers=h_b).status_code == 404
    # nor may it start analysis, cancel, or read runs/artifacts
    for method, path in [
            ("post", f"/api/projects/{pid}/analyze"),
            ("post", f"/api/projects/{pid}/reanalyze"),
            ("get", f"/api/projects/{pid}/analysis-runs"),
            ("get", f"/api/projects/{pid}/artifacts"),
            ("get", f"/api/projects/{pid}/results")]:
        r = getattr(auth_env.client, method)(
            path, headers=h_b, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 404, (path, r.status_code)


def test_artifact_download_isolated(auth_env, tmp_path):
    _, h_a = _mk_user(auth_env, "a2@a.com")
    _, h_b = _mk_user(auth_env, "b2@b.com")
    pid = _mk_project(auth_env, h_a)
    rid = db.create_analysis_run(pid, None, None, {})
    f = tmp_path / "results.geojson"
    f.write_bytes(b"{}")
    from app import artifacts

    aid = artifacts.register_file(
        str(f), project_id=pid, run_id=rid, artifact_type="results_geojson",
        download_filename="results.geojson")
    ok = auth_env.client.get(
        f"/api/projects/{pid}/artifacts/{aid}/download", headers=h_a)
    assert ok.status_code == 200
    denied = auth_env.client.get(
        f"/api/projects/{pid}/artifacts/{aid}/download", headers=h_b)
    assert denied.status_code == 404


def test_dtm_library_isolated(auth_env, tmp_path, monkeypatch):
    from fake_provider import write_synthetic_dtm

    monkeypatch.setenv("DTM_STORAGE_DIR", str(tmp_path / "lib"))
    _, h_a = _mk_user(auth_env, "a3@a.com")
    _, h_b = _mk_user(auth_env, "b3@b.com")
    src = tmp_path / "d.tif"
    write_synthetic_dtm(str(src), nodata_corner=False)
    import io

    up = auth_env.client.post(
        "/api/dtms/upload", headers=h_a,
        files={"file": ("d.tif", io.BytesIO(src.read_bytes()), "image/tiff")})
    assert up.status_code == 200, up.text
    did = up.json()["id"]

    ids_a = [d["id"] for d in
             auth_env.client.get("/api/dtms", headers=h_a).json()["items"]]
    ids_b = [d["id"] for d in
             auth_env.client.get("/api/dtms", headers=h_b).json()["items"]]
    assert did in ids_a and did not in ids_b
    assert auth_env.client.get(f"/api/dtms/{did}",
                               headers=h_b).status_code == 404
    assert auth_env.client.get(f"/api/dtms/{did}",
                               headers=h_a).status_code == 200


# ---------------------------------------------------------------------------
# roles


def test_viewer_is_read_only(auth_env):
    owner, h_owner = _mk_user(auth_env, "own@org.com")
    _, h_viewer = _mk_user(auth_env, "view@org.com", role="viewer",
                           org_id=owner["org_id"])
    pid = _mk_project(auth_env, h_owner)
    # viewer can read
    assert auth_env.client.get(f"/api/projects/{pid}",
                               headers=h_viewer).status_code == 200
    # but cannot mutate
    r = auth_env.client.post(f"/api/projects/{pid}/analyze",
                             headers=h_viewer, json={})
    assert r.status_code == 403
    r = auth_env.client.post("/api/projects", headers=h_viewer, json={
        "name": "nope", "aoi": aoi_inside_fake_dtm()})
    assert r.status_code == 403


def test_bad_role_rejected(auth_env):
    r = auth_env.client.post("/api/admin/users", headers=ADMIN, json={
        "email": "r@x.com", "role": "superuser"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# rate limiting + audit


def test_analysis_rate_limited_per_actor(auth_env, monkeypatch):
    import app.jobs.terrain_job as tj

    monkeypatch.setattr(tj, "execute_analysis_run", lambda rid, **kw: None)
    monkeypatch.setenv("ANALYSIS_RATE_LIMIT_PER_MINUTE", "3")
    _, h = _mk_user(auth_env, "rl@x.com")
    pid = _mk_project(auth_env, h)
    statuses = []
    for _ in range(4):
        r = auth_env.client.post(f"/api/projects/{pid}/analyze",
                                 headers=h, json={"force": True})
        statuses.append(r.status_code)
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429


def test_sensitive_actions_audited(auth_env, monkeypatch):
    import sqlite3

    import app.jobs.terrain_job as tj

    monkeypatch.setattr(tj, "execute_analysis_run", lambda rid, **kw: None)
    _, h = _mk_user(auth_env, "aud@x.com")
    pid = _mk_project(auth_env, h)
    auth_env.client.post(f"/api/projects/{pid}/analyze", headers=h, json={})
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE action LIKE ?",
        (f"%/projects/{pid}/analyze",)).fetchall()
    conn.close()
    assert rows, "analyze was not audited"
    assert rows[0]["org_id"]
