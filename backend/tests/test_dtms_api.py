"""Managed DTM library: listing, upload, path validation/import, survey
discovery, and dtm_id analysis integration. All offline."""

import io
import os

import pytest

from app.dtm_api import DtmPathError, resolve_allowed_path
from fake_provider import write_synthetic_dtm
from survey_helpers import aoi_inside_fake_dtm, make_jpeg_bytes


@pytest.fixture()
def dtm_env(drone_env, tmp_path, monkeypatch):
    """drone_env plus an isolated DTM storage dir + allowed roots."""
    storage = tmp_path / "dtm-lib"
    monkeypatch.setenv("DTM_STORAGE_DIR", str(storage))
    monkeypatch.setenv("DTM_ALLOWED_EXTERNAL_ROOTS", str(tmp_path / "imports"))
    (tmp_path / "imports").mkdir()
    drone_env.dtm_storage = storage
    drone_env.imports = tmp_path / "imports"
    return drone_env


def _tif_bytes(tmp_path, name="up.tif", **kw) -> bytes:
    p = str(tmp_path / f"__gen_{name}")
    write_synthetic_dtm(p, nodata_corner=False, **kw)
    return open(p, "rb").read()


# ---------------------------------------------------------------------------
# library listing + upload


def test_empty_library(dtm_env):
    r = dtm_env.client.get("/api/dtms")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_valid_geotiff_upload_registers_and_lists(dtm_env, tmp_path):
    data = _tif_bytes(tmp_path)
    r = dtm_env.client.post(
        "/api/dtms/upload",
        files={"file": ("caliterra-dtm.tif", io.BytesIO(data), "image/tiff")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["display_name"] == "caliterra-dtm.tif"
    assert d["source_type"] == "upload" and d["status"] == "ready"
    assert d["crs"] == "EPSG:32613" and d["width"] == 120
    assert d["size_bytes"] > 0
    assert "storage_path" not in d  # internal paths never leak in responses

    items = dtm_env.client.get("/api/dtms").json()["items"]
    assert [i["id"] for i in items] == [d["id"]]
    # stored under the managed dir with a collision-safe generated name
    stored = os.listdir(dtm_env.dtm_storage)
    assert len(stored) == 1 and stored[0].startswith("dtm_")


def test_invalid_extension_rejected(dtm_env):
    r = dtm_env.client.post(
        "/api/dtms/upload",
        files={"file": ("evil.exe", io.BytesIO(b"MZ..."), "image/tiff")})
    assert r.status_code == 422
    assert ".tif" in r.json()["detail"]


def test_fake_tiff_rejected_and_not_stored(dtm_env):
    r = dtm_env.client.post(
        "/api/dtms/upload",
        files={"file": ("fake.tif", io.BytesIO(b"not a tiff at all"),
                        "image/tiff")})
    assert r.status_code == 422
    assert "Not a usable GeoTIFF" in r.json()["detail"]
    assert dtm_env.client.get("/api/dtms").json()["items"] == []
    assert os.listdir(dtm_env.dtm_storage) == []  # temp file cleaned up


def test_oversized_upload_rejected(dtm_env, tmp_path, monkeypatch):
    monkeypatch.setenv("DTM_MAX_UPLOAD_MB", "0")  # everything is too big
    data = _tif_bytes(tmp_path)
    r = dtm_env.client.post(
        "/api/dtms/upload",
        files={"file": ("big.tif", io.BytesIO(data), "image/tiff")})
    assert r.status_code == 413
    assert "DTM_MAX_UPLOAD_MB" in r.json()["detail"]


# ---------------------------------------------------------------------------
# custom server path


def test_path_outside_allowed_roots_rejected(dtm_env):
    r = dtm_env.client.post("/api/dtms/validate-path",
                            json={"path": "/etc/passwd"})
    body = r.json()
    assert body["valid"] is False and "allowed" in body["reason"]


def test_path_traversal_rejected(dtm_env, tmp_path):
    sneaky = str(dtm_env.imports / ".." / ".." / "etc" / "hosts")
    with pytest.raises(DtmPathError):
        resolve_allowed_path(sneaky)
    r = dtm_env.client.post("/api/dtms/validate-path", json={"path": sneaky})
    assert r.json()["valid"] is False


def test_missing_path_and_directory_rejected(dtm_env):
    r = dtm_env.client.post(
        "/api/dtms/validate-path",
        json={"path": str(dtm_env.imports / "nope.tif")})
    assert r.json()["valid"] is False
    assert "does not exist" in r.json()["reason"]
    r = dtm_env.client.post("/api/dtms/validate-path",
                            json={"path": str(dtm_env.imports)})
    assert r.json()["valid"] is False
    assert "directory" in r.json()["reason"]


def test_non_tiff_in_allowed_root_rejected(dtm_env):
    p = dtm_env.imports / "readme.txt"
    p.write_text("hello")
    r = dtm_env.client.post("/api/dtms/validate-path", json={"path": str(p)})
    assert r.json()["valid"] is False
    assert ".tif" in r.json()["reason"]


def test_validate_and_import_from_allowed_path(dtm_env):
    src = str(dtm_env.imports / "caliterra-dtm.tif")
    write_synthetic_dtm(src, nodata_corner=False)

    v = dtm_env.client.post("/api/dtms/validate-path",
                            json={"path": src}).json()
    assert v["valid"] is True
    assert v["metadata"]["filename"] == "caliterra-dtm.tif"
    assert v["metadata"]["crs"] == "EPSG:32613"
    assert v["metadata"]["width"] == 120

    r = dtm_env.client.post("/api/dtms/import-path",
                            json={"path": src, "copy_to_library": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source_type"] == "imported_path" and d["status"] == "ready"
    # copied into the library; the original stays where it was
    assert os.path.isfile(src)
    assert any(f.startswith("dtm_") for f in os.listdir(dtm_env.dtm_storage))


def test_import_in_place_is_deduplicated(dtm_env):
    src = str(dtm_env.imports / "inplace.tif")
    write_synthetic_dtm(src, nodata_corner=False)
    d1 = dtm_env.client.post(
        "/api/dtms/import-path",
        json={"path": src, "copy_to_library": False}).json()
    d2 = dtm_env.client.post(
        "/api/dtms/import-path",
        json={"path": src, "copy_to_library": False}).json()
    assert d1["id"] == d2["id"]
    assert d1["source_type"] == "external_path"


# ---------------------------------------------------------------------------
# survey DTM discovery


def _complete_survey(env, monkeypatch):
    from app.jobs.photogrammetry_job import run_survey

    monkeypatch.setenv("PHOTOGRAMMETRY_POLL_SECONDS", "0")
    client = env.client
    r = client.post("/api/projects",
                    json={"name": "disc", "aoi": aoi_inside_fake_dtm()})
    pid = r.json()["project_id"]
    plan = client.post(f"/api/projects/{pid}/drone-surveys", json={
        "images": [{"filename": f"f{i}.jpg", "type": "image/jpeg",
                    "size": len(make_jpeg_bytes(seed=i))} for i in range(3)],
    }).json()
    sid = plan["survey_id"]
    for i, up in enumerate(plan["uploads"]):
        client.put(f"/api/local-uploads/{up['key']}",
                   content=make_jpeg_bytes(seed=i))
    client.post(f"/api/projects/{pid}/drone-surveys/{sid}/complete-upload")
    run_survey(sid)
    assert env.db.get_survey(sid)["state"] == "completed"
    return pid, sid


def test_survey_dtm_discovered_in_library(dtm_env, monkeypatch):
    pid, sid = _complete_survey(dtm_env, monkeypatch)
    items = dtm_env.client.get("/api/dtms").json()["items"]
    survey_items = [i for i in items if i["source_type"] == "survey"]
    assert len(survey_items) == 1
    d = survey_items[0]
    assert d["survey_id"] == sid and d["project_id"] == pid
    assert d["status"] == "ready"
    assert "survey DTM" in d["display_name"]


def test_survey_dtm_missing_file_flagged_and_unusable(dtm_env, monkeypatch):
    pid, sid = _complete_survey(dtm_env, monkeypatch)
    survey = dtm_env.db.get_survey(sid)
    os.unlink(survey["dtm_path"])  # the file vanishes

    items = dtm_env.client.get("/api/dtms").json()["items"]
    d = [i for i in items if i["survey_id"] == sid][0]
    assert d["status"] == "missing"

    # analysis with it must be refused before anything is queued
    r = dtm_env.client.post(f"/api/projects/{pid}/analyze",
                            json={"dtm_id": d["id"]})
    assert r.status_code == 422
    assert "missing" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# analysis integration


def test_analyze_with_valid_dtm_id(dtm_env, tmp_path):
    # AOI matches the synthetic DTM footprint -> drone_only, offline
    aoi = aoi_inside_fake_dtm()
    r = dtm_env.client.post("/api/projects", json={"name": "an", "aoi": aoi})
    pid = r.json()["project_id"]
    data = _tif_bytes(tmp_path, name="an.tif")
    d = dtm_env.client.post(
        "/api/dtms/upload",
        files={"file": ("an.tif", io.BytesIO(data), "image/tiff")}).json()

    r = dtm_env.client.post(f"/api/projects/{pid}/analyze",
                            json={"dtm_id": d["id"]})
    assert r.status_code == 200, r.text
    import time

    for _ in range(120):
        st = dtm_env.client.get(f"/api/projects/{pid}/status").json()
        if st["state"] in ("done",) or st["state"].startswith("error"):
            break
        time.sleep(0.5)
    assert st["state"] == "done", st
    fc = dtm_env.client.get(f"/api/projects/{pid}/results").json()
    assert fc["properties"]["dem_mode"] == "drone_only"


def test_analyze_with_unknown_dtm_id(dtm_env):
    r = dtm_env.client.post("/api/projects",
                            json={"name": "u", "aoi": aoi_inside_fake_dtm()})
    pid = r.json()["project_id"]
    r = dtm_env.client.post(f"/api/projects/{pid}/analyze",
                            json={"dtm_id": "dtm_doesnotexist"})
    assert r.status_code == 404


def test_analyze_with_worker_inaccessible_dtm(dtm_env, tmp_path):
    r = dtm_env.client.post("/api/projects",
                            json={"name": "w", "aoi": aoi_inside_fake_dtm()})
    pid = r.json()["project_id"]
    data = _tif_bytes(tmp_path, name="gone.tif")
    d = dtm_env.client.post(
        "/api/dtms/upload",
        files={"file": ("gone.tif", io.BytesIO(data), "image/tiff")}).json()
    # the stored file disappears before analysis is requested
    stored = [f for f in os.listdir(dtm_env.dtm_storage)
              if f.startswith("dtm_")]
    for f in stored:
        os.unlink(os.path.join(dtm_env.dtm_storage, f))
    r = dtm_env.client.post(f"/api/projects/{pid}/analyze",
                            json={"dtm_id": d["id"]})
    assert r.status_code == 422
    assert "not available" in r.json()["detail"]


def test_reanalyze_survey_id_still_works(dtm_env, monkeypatch):
    """Backward compatibility: reanalysis by survey_id, no dtm_id."""
    import app.jobs as jobs_pkg
    from app.jobs.terrain_job import run_analysis_job

    pid, sid = _complete_survey(dtm_env, monkeypatch)
    enqueued = []

    class _Q:
        def enqueue(self, fn, rid, **kw):
            enqueued.append(rid)

    monkeypatch.setattr(jobs_pkg, "get_queue", lambda: _Q())
    r = dtm_env.client.post(f"/api/projects/{pid}/reanalyze",
                            json={"survey_id": sid})
    assert r.status_code == 200, r.text
    run_analysis_job(enqueued[0])
    run = dtm_env.client.get(
        f"/api/projects/{pid}/analysis-runs/{enqueued[0]}").json()
    assert run["state"] == "completed"


def test_reanalyze_with_dtm_id(dtm_env, tmp_path, monkeypatch):
    import app.jobs as jobs_pkg
    from app.jobs.terrain_job import run_analysis_job

    r = dtm_env.client.post("/api/projects",
                            json={"name": "rd", "aoi": aoi_inside_fake_dtm()})
    pid = r.json()["project_id"]
    data = _tif_bytes(tmp_path, name="rd.tif")
    d = dtm_env.client.post(
        "/api/dtms/upload",
        files={"file": ("rd.tif", io.BytesIO(data), "image/tiff")}).json()

    enqueued = []

    class _Q:
        def enqueue(self, fn, rid, **kw):
            enqueued.append(rid)

    monkeypatch.setattr(jobs_pkg, "get_queue", lambda: _Q())
    r = dtm_env.client.post(f"/api/projects/{pid}/reanalyze",
                            json={"dtm_id": d["id"]})
    assert r.status_code == 200, r.text
    run_analysis_job(enqueued[0])
    run = dtm_env.client.get(
        f"/api/projects/{pid}/analysis-runs/{enqueued[0]}").json()
    assert run["state"] == "completed" and run["dem_mode"] == "drone_only"
