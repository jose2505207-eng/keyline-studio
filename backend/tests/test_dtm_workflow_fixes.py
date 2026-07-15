"""Existing-DTM workflow fixes: geographic placement, QA severity semantics,
result contract (status/counts/contours/reasons), keyline attributes, and
specialized exports. All offline."""

import io
import json
import os

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app import terrain_quality as tq
from fake_provider import write_synthetic_dtm

# Caliterra-area site: EPSG:32614 (UTM 14N) near -98.09, 30.17 (Texas)
TX_ORIGIN = (587500.0, 3338200.0)


def _write_vvalley_32614(path: str, cell: float = 10.0) -> None:
    """The proven synthetic V-valley (known keypoint) as a 32614 GeoTIFF."""
    nx = ny = 200
    cols = np.arange(nx) * cell
    rows = np.arange(ny)
    yc = ny // 2
    lon = np.where(cols <= 1200.0, 0.04 * cols,
                   0.04 * 1200.0 + 0.20 * (cols - 1200.0))
    cross = 0.09 * np.abs(rows - yc)[:, None] * cell
    dem = (lon[None, :] + cross + 100.0).astype("float32")
    with rasterio.open(path, "w", driver="GTiff", height=ny, width=nx,
                       count=1, dtype="float32", crs="EPSG:32614",
                       transform=from_origin(TX_ORIGIN[0], TX_ORIGIN[1],
                                             cell, cell)) as dst:
        dst.write(dem, 1)


def _plane_tif(path: str, slope: float, crs="EPSG:32614", noise=1.5):
    h = w = 200
    y, x = np.mgrid[0:h, 0:w]
    dem = (900.0 + slope * x + noise * np.sin(y / 7.0)).astype("float32")
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs=crs,
                       transform=from_origin(TX_ORIGIN[0], TX_ORIGIN[1],
                                             1, 1)) as dst:
        dst.write(dem, 1)


@pytest.fixture()
def dtm_env(drone_env, tmp_path, monkeypatch):
    storage = tmp_path / "dtm-lib"
    monkeypatch.setenv("DTM_STORAGE_DIR", str(storage))
    monkeypatch.setenv("DTM_ALLOWED_EXTERNAL_ROOTS", str(tmp_path))
    drone_env.dtm_storage = storage
    return drone_env


def _upload(env, path: str, name: str) -> dict:
    with open(path, "rb") as f:
        r = env.client.post("/api/dtms/upload",
                            files={"file": (name, io.BytesIO(f.read()),
                                            "image/tiff")})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1+2: EPSG:32614 placement lands in Mexico/Texas, not Europe


def test_32614_dtm_places_in_north_america(dtm_env, tmp_path):
    tif = str(tmp_path / "tx.tif")
    _write_vvalley_32614(tif)
    d = _upload(dtm_env, tif, "tx.tif")
    assert d["crs"] == "EPSG:32614"
    # upload response carries the geography the map needs
    w, s, e, n = d["bbox_wgs84"]
    assert -99.5 < w < e < -97.0, d["bbox_wgs84"]  # UTM 14N longitudes
    assert 29.5 < s < n < 31.0
    lon, lat = d["center_wgs84"]
    assert -99.5 < lon < -97.0 and 29.5 < lat < 31.0
    assert d["footprint_geojson"]["type"] in ("Polygon", "MultiPolygon")
    # detail endpoint agrees (this is what the frontend locate uses)
    det = dtm_env.client.get(f"/api/dtms/{d['id']}").json()
    assert det["bbox_wgs84"] == d["bbox_wgs84"]


def test_no_stale_placement_between_dtms(dtm_env, tmp_path):
    """Selecting DTM B after DTM A must return B's geography, never A's
    cached CRS/bounds (32613 Nayarit vs 32614 Texas)."""
    a = str(tmp_path / "a.tif")
    write_synthetic_dtm(a, nodata_corner=False)  # EPSG:32613, ~ -104, 21.4
    b = str(tmp_path / "b.tif")
    _write_vvalley_32614(b)                       # EPSG:32614, ~ -98, 30.2
    da = _upload(dtm_env, a, "a.tif")
    db_ = _upload(dtm_env, b, "b.tif")
    assert da["crs"] == "EPSG:32613" and db_["crs"] == "EPSG:32614"
    assert abs(da["center_wgs84"][0] - (-104.06)) < 0.2
    assert abs(db_["center_wgs84"][0] - (-98.06)) < 0.2
    assert da["bbox_wgs84"] != db_["bbox_wgs84"]


# ---------------------------------------------------------------------------
# 7: natural slope must not be falsely severe


def test_natural_35pct_slope_is_warning_not_severe(tmp_path):
    tif = str(tmp_path / "ranch.tif")
    _plane_tif(tif, slope=0.35)
    report = tq.assess_dtm(tif, gcp_supplied=True)
    tilt = [i for i in report.issues if i.code == tq.SUSPECT_GLOBAL_TILT]
    assert tilt and tilt[0].severity == "warning"
    assert not report.severe
    # explanation payload carries the exact triggering values
    assert tilt[0].details["plane_slope_pct"] == pytest.approx(35.0, abs=1)
    assert tilt[0].details["satellite_plane_slope_pct"] is None


def test_satellite_confirmed_tilt_stays_severe(tmp_path):
    tif = str(tmp_path / "tilted.tif")
    _plane_tif(tif, slope=0.35)

    def flat_sat():
        y, x = np.mgrid[0:50, 0:50]
        return (900.0 + 0.02 * x * 4).astype("float64"), 4.0, 4.0

    report = tq.assess_dtm(tif, gcp_supplied=False, satellite_surface=flat_sat)
    tilt = [i for i in report.issues if i.code == tq.SUSPECT_GLOBAL_TILT]
    assert tilt and tilt[0].severity == "error"
    assert report.severe
    assert tilt[0].details["satellite_plane_slope_pct"] == pytest.approx(2.0, abs=1)


def test_absurd_plane_severe_even_without_reference(tmp_path):
    tif = str(tmp_path / "absurd.tif")
    _plane_tif(tif, slope=0.82, noise=1.0)  # the Caliterra failure signature
    report = tq.assess_dtm(tif, gcp_supplied=False)
    tilt = [i for i in report.issues if i.code == tq.SUSPECT_GLOBAL_TILT]
    assert tilt and tilt[0].severity == "error"


def test_natural_slope_analysis_completes_with_features(dtm_env, tmp_path,
                                                        monkeypatch):
    """The headline regression: a normally sloped parcel runs to completion
    and produces terrain features — not a QA-blocked zero-result."""
    monkeypatch.setenv("QA_SATELLITE_CROSSCHECK", "0")  # offline
    tif = str(tmp_path / "vv.tif")
    _write_vvalley_32614(tif)
    d = _upload(dtm_env, tif, "vv.tif")

    # default AOI from the raster footprint (what the frontend adopts)
    w, s, e, n = d["bbox_wgs84"]
    pad = 0.0003
    ring = [[w + pad, s + pad], [e - pad, s + pad], [e - pad, n - pad],
            [w + pad, n - pad], [w + pad, s + pad]]
    r = dtm_env.client.post("/api/projects", json={
        "name": "vv", "aoi": {"type": "Polygon", "coordinates": [ring]}})
    pid = r.json()["project_id"]

    r = dtm_env.client.post(
        f"/api/projects/{pid}/analyze",
        json={"dtm_id": d["id"],
              "terrain": {"min_drainage_area_m2": 60000}})
    assert r.status_code == 200, r.text
    import time

    for _ in range(240):
        st = dtm_env.client.get(f"/api/projects/{pid}/status").json()
        if st["state"] == "done" or st["state"].startswith("error"):
            break
        time.sleep(0.5)
    assert st["state"] == "done", st

    fc = dtm_env.client.get(f"/api/projects/{pid}/results").json()
    props = fc["properties"]
    assert props["dem_mode"] == "drone_only"
    assert props["counts"]["valleys"] >= 1
    assert props["counts"]["keypoints"] >= 1
    assert props["counts"]["keylines"] >= 1
    assert props["counts"]["contours"] >= 5
    assert props["status"] in ("completed", "completed_with_warnings")
    assert props["keylines_suppressed"] is False
    # bbox/center for the map, in Texas
    assert -99.5 < props["bbox_wgs84"][0] < -97.0
    assert 29.5 < props["center_wgs84"][1] < 31.0

    # keyline attributes for field-layout exports
    kls = [f for f in fc["features"]
           if f["properties"]["kind"] == "keyline"]
    p = kls[0]["properties"]
    for attr in ("elevation", "confidence", "length_m", "bearing_deg",
                 "keypoint_id", "analysis_run_id"):
        assert p.get(attr) is not None, attr
    assert p["length_m"] > 100

    # every feature within the DTM bbox (spatial gate held)
    b = props["dem_bounds_wgs84"]
    for f in fc["features"]:
        coords = np.array(f["geometry"]["coordinates"], dtype=float).reshape(-1, 2)
        assert (coords[:, 0] >= b[0] - 0.001).all()
        assert (coords[:, 0] <= b[2] + 0.001).all()

    # ---- exports -----------------------------------------------------------
    avail = dtm_env.client.get(
        f"/api/projects/{pid}/exports/availability").json()
    assert avail["keylines_geojson"] and avail["keylines_kml"]
    assert avail["gpkg"] and avail["keylines_dxf"]

    kg = dtm_env.client.get(
        f"/api/projects/{pid}/exports/keylines.geojson")
    assert kg.status_code == 200
    kg_fc = kg.json()
    assert any(f["properties"]["kind"] == "keyline" for f in kg_fc["features"])
    coords = kg_fc["features"][0]["geometry"]["coordinates"]
    assert coords, "keyline export must contain real geometry"

    kk = dtm_env.client.get(f"/api/projects/{pid}/exports/keylines.kml")
    assert kk.status_code == 200 and "<coordinates>" in kk.text

    gp = dtm_env.client.get(f"/api/projects/{pid}/exports/terrain.gpkg")
    assert gp.status_code == 200 and len(gp.content) > 1000

    dx = dtm_env.client.get(f"/api/projects/{pid}/exports/keylines.dxf")
    assert dx.status_code == 200 and b"KEYLINES" in dx.content


# ---------------------------------------------------------------------------
# 8: QA-blocked runs must not masquerade as clean completions


def test_blocked_run_reports_warnings_not_clean_completed(dtm_env, tmp_path,
                                                          monkeypatch):
    monkeypatch.setenv("QA_SATELLITE_CROSSCHECK", "0")
    monkeypatch.setenv("TERRAIN_QA_MODE", "strict")
    tif = str(tmp_path / "absurd2.tif")
    _plane_tif(tif, slope=0.82, noise=1.0)
    d = _upload(dtm_env, tif, "absurd2.tif")
    w, s, e, n = d["bbox_wgs84"]
    ring = [[w, s], [e, s], [e, n], [w, n], [w, s]]
    r = dtm_env.client.post("/api/projects", json={
        "name": "blocked", "aoi": {"type": "Polygon", "coordinates": [ring]}})
    pid = r.json()["project_id"]
    dtm_env.client.post(f"/api/projects/{pid}/analyze",
                        json={"dtm_id": d["id"]})
    import time

    for _ in range(240):
        st = dtm_env.client.get(f"/api/projects/{pid}/status").json()
        if st["state"] == "done" or st["state"].startswith("error"):
            break
        time.sleep(0.5)
    fc = dtm_env.client.get(f"/api/projects/{pid}/results").json()
    props = fc["properties"]
    assert props["status"] == "completed_with_warnings"
    assert props["counts"]["keylines"] == 0
    assert "KEYLINE_GENERATION_BLOCKED" in props["notices"]
    assert any("blocked" in r.lower() for r in props["keypoint_reasons"])

    # keyline exports refuse with the real reason, not an empty file
    kg = dtm_env.client.get(f"/api/projects/{pid}/exports/keylines.geojson")
    assert kg.status_code == 409
    assert "no keylines" in kg.json()["detail"].lower()
    avail = dtm_env.client.get(
        f"/api/projects/{pid}/exports/availability").json()
    assert not avail["keylines_geojson"]
    assert avail["unavailable_reason"]
