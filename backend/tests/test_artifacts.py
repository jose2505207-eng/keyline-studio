"""Artifact registry: registration semantics, verified listings, downloads,
and end-to-end registration by a real analysis run."""

import io
import json
import os

import pytest

from app import artifacts, db
from fake_provider import write_synthetic_dtm
from survey_helpers import aoi_inside_fake_dtm


@pytest.fixture()
def dtm_env(drone_env, tmp_path, monkeypatch):
    storage = tmp_path / "dtm-lib"
    monkeypatch.setenv("DTM_STORAGE_DIR", str(storage))
    drone_env.dtm_storage = storage
    return drone_env


@pytest.fixture()
def project(drone_env):
    r = drone_env.client.post("/api/projects", json={
        "name": "arts", "aoi": aoi_inside_fake_dtm()})
    return r.json()["project_id"]


def _run_with_outputs(project, tmp_path, files: dict[str, bytes]) -> tuple[str, str]:
    rid = db.create_analysis_run(project, None, None, {"trigger": "analyze"})
    db.update_analysis_run(rid, state="completed", analysis_version="2")
    out_dir = tmp_path / "out" / rid
    for rel, data in files.items():
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return rid, str(out_dir)


# ---------------------------------------------------------------------------
# registration


def test_register_only_existing_nonempty_files(drone_env, project, tmp_path):
    rid, out_dir = _run_with_outputs(project, tmp_path, {
        "results.geojson": json.dumps({"type": "FeatureCollection",
                                       "features": []}).encode(),
        "hillshade.png": b"\x89PNG fake",
        "exports/keylines.geojson": b"",  # empty: must NOT be registered
    })
    run = db.get_analysis_run(rid)
    registered = artifacts.register_run_outputs(run, out_dir)
    assert "results_geojson" in registered
    assert "hillshade_png" in registered
    assert "keylines_geojson" not in registered      # empty file
    assert "design_package_zip" not in registered    # absent file

    a = db.get_artifact(registered["results_geojson"])
    assert a["size_bytes"] > 0
    assert len(a["checksum_sha256"]) == 64
    assert a["mime_type"] == "application/geo+json"
    assert a["algorithm_version"] == "2"


def test_reregistration_replaces_not_duplicates(drone_env, project, tmp_path):
    rid, out_dir = _run_with_outputs(project, tmp_path, {
        "results.geojson": b"{}1"})
    run = db.get_analysis_run(rid)
    first = artifacts.register_run_outputs(run, out_dir)
    with open(os.path.join(out_dir, "results.geojson"), "wb") as f:
        f.write(b"{} regenerated")
    second = artifacts.register_run_outputs(run, out_dir)
    assert first["results_geojson"] == second["results_geojson"]
    assert len(db.list_artifacts(project, rid)) == 1
    a = db.get_artifact(second["results_geojson"])
    assert a["size_bytes"] == len(b"{} regenerated")


def test_raster_metadata_extracted_for_geotiff(drone_env, project, tmp_path):
    dem = tmp_path / "out" / "dem_utm.tif"
    dem.parent.mkdir(parents=True, exist_ok=True)
    write_synthetic_dtm(str(dem), nodata_corner=False)
    rid = db.create_analysis_run(project, None, None, {})
    aid = artifacts.register_file(
        str(dem), project_id=project, run_id=rid,
        artifact_type="processed_dtm", download_filename="processed-dtm.tif")
    a = db.get_artifact(aid)
    assert a["crs"] == "EPSG:32613"
    assert a["mime_type"] == "image/tiff"
    assert a["resolution_json"] == [1.0, 1.0]
    assert a["elevation_min"] is not None and a["elevation_max"] is not None


# ---------------------------------------------------------------------------
# API: listing + download verification


def test_listing_reports_verified_availability(drone_env, project, tmp_path):
    rid, out_dir = _run_with_outputs(project, tmp_path, {
        "results.geojson": b"{}"})
    artifacts.register_run_outputs(db.get_analysis_run(rid), out_dir)
    items = drone_env.client.get(
        f"/api/projects/{project}/artifacts?run_id={rid}").json()["items"]
    assert len(items) == 1
    a = items[0]
    assert a["available"] is True
    assert a["artifact_type"] == "results_geojson"
    assert a["size_bytes"] == 2 and a["checksum_sha256"]
    assert "stored_path" not in a  # internal paths never leak

    # delete the file: the listing must flip to unavailable, honestly
    os.remove(os.path.join(out_dir, "results.geojson"))
    items = drone_env.client.get(
        f"/api/projects/{project}/artifacts?run_id={rid}").json()["items"]
    assert items[0]["available"] is False
    assert items[0]["unavailable_reason"]


def test_download_streams_real_file_with_mime(drone_env, project, tmp_path):
    rid, out_dir = _run_with_outputs(project, tmp_path, {
        "results.geojson": b'{"type":"FeatureCollection","features":[]}'})
    reg = artifacts.register_run_outputs(db.get_analysis_run(rid), out_dir)
    aid = reg["results_geojson"]
    r = drone_env.client.get(
        f"/api/projects/{project}/artifacts/{aid}/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/geo+json")
    assert "results.geojson" in r.headers["content-disposition"]
    assert r.content == b'{"type":"FeatureCollection","features":[]}'


def test_download_of_missing_file_is_410_not_500(drone_env, project, tmp_path):
    rid, out_dir = _run_with_outputs(project, tmp_path, {
        "results.geojson": b"{}"})
    reg = artifacts.register_run_outputs(db.get_analysis_run(rid), out_dir)
    os.remove(os.path.join(out_dir, "results.geojson"))
    r = drone_env.client.get(
        f"/api/projects/{project}/artifacts/{reg['results_geojson']}/download")
    assert r.status_code == 410
    assert "no longer available" in r.json()["detail"]


def test_download_scoped_to_project(drone_env, project, tmp_path):
    rid, out_dir = _run_with_outputs(project, tmp_path, {
        "results.geojson": b"{}"})
    reg = artifacts.register_run_outputs(db.get_analysis_run(rid), out_dir)
    other = drone_env.client.post("/api/projects", json={
        "name": "other", "aoi": aoi_inside_fake_dtm()}).json()["project_id"]
    r = drone_env.client.get(
        f"/api/projects/{other}/artifacts/{reg['results_geojson']}/download")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# end-to-end: a real analysis run registers its outputs


def test_real_analysis_run_registers_artifacts(dtm_env, tmp_path, monkeypatch):
    lib = tmp_path / "gen.tif"
    write_synthetic_dtm(str(lib), nodata_corner=False)
    d = dtm_env.client.post(
        "/api/dtms/upload",
        files={"file": ("gen.tif", io.BytesIO(lib.read_bytes()),
                        "image/tiff")}).json()
    pid = dtm_env.client.post("/api/projects", json={
        "name": "e2e", "aoi": aoi_inside_fake_dtm()}).json()["project_id"]
    r = dtm_env.client.post(f"/api/projects/{pid}/analyze",
                            json={"dtm_id": d["id"]})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    run = dtm_env.client.get(
        f"/api/projects/{pid}/analysis-runs/{rid}").json()
    assert run["state"] in ("completed", "completed_with_warnings"), run
    items = dtm_env.client.get(
        f"/api/projects/{pid}/artifacts?run_id={rid}").json()["items"]
    types = {a["artifact_type"] for a in items if a["available"]}
    assert "results_geojson" in types
    assert "processed_dtm" in types
    assert "hillshade_png" in types
    # every listed artifact is downloadable for real
    for a in items:
        if not a["available"]:
            continue
        dl = dtm_env.client.get(
            f"/api/projects/{pid}/artifacts/{a['id']}/download")
        assert dl.status_code == 200, a["artifact_type"]
        assert len(dl.content) == a["size_bytes"]
