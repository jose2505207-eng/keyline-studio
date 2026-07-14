"""Regression battery for the Caliterra correctness failures: CRS/stale-data
integrity, identical valley/ridge, honest empty-result semantics, DTM tilt
QA, and reanalysis without photogrammetry. All offline."""

import json
import os
import re

import numpy as np
import pytest
from pyproj import Transformer

from app import config, db, spatial, terrain_quality
from app.pipeline import TerrainResult, _write_outputs, run_pipeline
from fake_provider import write_synthetic_dtm
from survey_helpers import aoi_inside_fake_dtm, make_jpeg_bytes


# ---------------------------------------------------------------------------
# helpers

def _site(crs: str, lon: float, lat: float, size=(120, 120), tilt=(0.08, 0.05)):
    """Origin easting/northing of a synthetic site around (lon, lat)."""
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    e, n = tr.transform(lon, lat)
    return (round(e, 0), round(n + size[0] / 2, 0))


def _aoi_for(crs: str, origin, size=(120, 120), margin=20.0) -> dict:
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    e0, e1 = origin[0] + margin, origin[0] + size[1] - margin
    n1, n0 = origin[1] - margin, origin[1] - size[0] + margin
    ring = [tr.transform(e, n) for e, n in
            [(e0, n0), (e1, n0), (e1, n1), (e0, n1), (e0, n0)]]
    return {"type": "Polygon", "coordinates": [[list(c) for c in ring]]}


def _make_site(tmp_path, name: str, crs: str, lon: float, lat: float,
               **dtm_kwargs):
    origin = _site(crs, lon, lat)
    dtm = str(tmp_path / f"{name}.tif")
    write_synthetic_dtm(dtm, crs=crs, origin=origin, nodata_corner=False,
                        **dtm_kwargs)
    return dtm, _aoi_for(crs, origin)


TEXAS = dict(crs="EPSG:32614", lon=-98.0901, lat=30.1712)   # Caliterra
JALISCO = dict(crs="EPSG:32613", lon=-103.1495, lat=19.8474)  # Sta Quiteria


def _ctx_for(dtm, aoi, crs, pid="proj-a", rid="run-1"):
    import rasterio

    with rasterio.open(dtm) as src:
        bounds = tuple(src.bounds)
    return spatial.build_spatial_context(
        project_id=pid, survey_id=None, analysis_run_id=rid,
        dem_path=dtm, dem_crs=crs, analysis_crs=crs,
        aoi_wgs84_geojson=aoi, dem_bounds_analysis=bounds)


# ---------------------------------------------------------------------------
# 1 + 2: spatial-integrity gate


def test_texas_dtm_rejects_mexico_vectors(tmp_path):
    dtm, aoi = _make_site(tmp_path, "texas", **TEXAS)
    ctx = _ctx_for(dtm, aoi, TEXAS["crs"])
    mexico_fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [
            [-103.14992539591896, 19.847289300216012],
            [-103.14963793853066, 19.847555393275403],
            [-103.14935359949114, 19.847552442014596]]},
        "properties": {"kind": "valley", "id": "v0"},
    }]}
    with pytest.raises(spatial.SpatialIntegrityError) as exc:
        spatial.validate_fc_bounds(mexico_fc, ctx)
    msg = str(exc.value)
    assert "RESULT_CRS_MISMATCH" in msg
    # diagnostic includes both bounds
    assert "-103.14" in msg and "-98." in msg


def test_vectors_just_outside_footprint_not_exported(tmp_path):
    dtm, aoi = _make_site(tmp_path, "texas2", **TEXAS)
    ctx = _ctx_for(dtm, aoi, TEXAS["crs"])
    # 2 km north of the footprint: same region, still impossible output
    lat = ctx.dem_bounds_wgs84[3] + 0.02
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [[-98.0905, lat], [-98.0895, lat]]},
        "properties": {"kind": "ridge", "id": "r0"}}]}
    with pytest.raises(spatial.SpatialIntegrityError):
        spatial.validate_fc_bounds(fc, ctx)
    # while genuinely-on-site vectors pass
    lon = (ctx.dem_bounds_wgs84[0] + ctx.dem_bounds_wgs84[2]) / 2
    mid = (ctx.dem_bounds_wgs84[1] + ctx.dem_bounds_wgs84[3]) / 2
    ok = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [[lon, mid], [lon + 0.0003, mid]]},
        "properties": {"kind": "ridge", "id": "r0"}}]}
    spatial.validate_fc_bounds(ok, ctx)  # no raise


# ---------------------------------------------------------------------------
# 3: a previous project's CRS cannot leak into the next one


def test_sequential_projects_in_different_zones_stay_separate(tmp_path):
    results = {}
    for name, site in (("jalisco", JALISCO), ("texas", TEXAS)):
        dtm, aoi = _make_site(tmp_path, f"seq-{name}", **site)
        proj_dir = str(tmp_path / f"proj-{name}")
        fc = run_pipeline(proj_dir, aoi, drone_path=dtm)
        results[name] = fc
        assert fc["properties"]["analysis_crs"] == site["crs"]
        # every geometry inside this site's WGS84 bounds
        b = fc["properties"]["dem_bounds_wgs84"]
        for f in fc["features"]:
            xs = np.array(json.dumps(f["geometry"]))  # noqa: F841
        for f in fc["features"]:
            coords = np.array(f["geometry"]["coordinates"], dtype=float)
            coords = coords.reshape(-1, 2)
            assert (coords[:, 0] >= b[0] - 0.001).all()
            assert (coords[:, 0] <= b[2] + 0.001).all()
    # the second (Texas) run kept its own CRS despite Jalisco running first
    assert results["texas"]["properties"]["analysis_crs"] == "EPSG:32614"
    assert results["jalisco"]["properties"]["analysis_crs"] == "EPSG:32613"


# ---------------------------------------------------------------------------
# 4: concurrent projects cannot overwrite each other's outputs


def test_two_projects_write_isolated_run_dirs(drone_env, tmp_path):
    from app.jobs.terrain_job import execute_analysis_run, run_output_dir

    dirs = []
    for name, site in (("texas", TEXAS), ("jalisco", JALISCO)):
        dtm, aoi = _make_site(tmp_path, f"iso-{name}", **site)
        r = drone_env.client.post("/api/projects",
                                  json={"name": name, "aoi": aoi})
        pid = r.json()["project_id"]
        rid = db.create_analysis_run(pid, None, dtm, {"dem_mode": "auto"})
        execute_analysis_run(rid)
        out = run_output_dir(pid, rid)
        dirs.append(out)
        fc = json.load(open(os.path.join(out, "results.geojson")))
        assert fc["properties"]["project_id"] == pid
        assert fc["properties"]["analysis_run_id"] == rid
    assert len(set(dirs)) == 2
    for d in dirs:
        assert os.path.isfile(os.path.join(d, "results.geojson"))


# ---------------------------------------------------------------------------
# 5 + 6: identical valley/ridge, empty collections stay empty


def _fc_from_terrain(tmp_path, result: TerrainResult, monkeypatch=None,
                     mode="warn"):
    import rioxarray  # noqa: F401
    import xarray as xr
    from affine import Affine

    dtm, aoi = _make_site(tmp_path, f"vr-{mode}", **TEXAS)
    ctx = _ctx_for(dtm, aoi, TEXAS["crs"])
    # tiny DEM dataarray covering the same bounds
    import rasterio

    with rasterio.open(dtm) as src:
        arr = src.read(1)
        transform = src.transform
    da = xr.DataArray(arr, dims=("y", "x"))
    da.rio.write_crs(TEXAS["crs"], inplace=True)
    da.rio.write_transform(transform, inplace=True)
    out = str(tmp_path / f"out-{mode}")
    if monkeypatch:
        monkeypatch.setenv("TERRAIN_QA_MODE", mode)
    return _write_outputs(out, da, result, ctx, None), out


def _diag_line(ctx_aoi_center, offset=0.0):
    from shapely.geometry import LineString

    e, n = ctx_aoi_center
    return LineString([(e - 30, n - 30 + offset), (e, n + offset),
                       (e + 30, n + 30 + offset)])


def test_identical_valley_and_ridge_dropped_in_warn_mode(tmp_path, monkeypatch):
    dtm, aoi = _make_site(tmp_path, "warn-site", **TEXAS)
    ctx = _ctx_for(dtm, aoi, TEXAS["crs"])
    c = ctx.aoi_analysis.centroid
    line = _diag_line((c.x, c.y))
    result = TerrainResult(valleys=[line], ridges=[type(line)(line.coords)])
    fc, _ = _fc_from_terrain(tmp_path, result, monkeypatch, mode="warn")
    counts = fc["properties"]["counts"]
    assert counts["valleys"] == 1
    assert counts["ridges"] == 0  # contradiction dropped, not exported
    assert any("DUPLICATE_TERRAIN_GEOMETRY" in n
               for n in fc["properties"]["notices"])


def test_identical_valley_and_ridge_rejected_in_strict_mode(tmp_path, monkeypatch):
    dtm, aoi = _make_site(tmp_path, "strict-site", **TEXAS)
    ctx = _ctx_for(dtm, aoi, TEXAS["crs"])
    c = ctx.aoi_analysis.centroid
    line = _diag_line((c.x, c.y))
    result = TerrainResult(valleys=[line], ridges=[type(line)(line.coords)])
    with pytest.raises(spatial.TerrainIntegrityError,
                       match="DUPLICATE_TERRAIN_GEOMETRY"):
        _fc_from_terrain(tmp_path, result, monkeypatch, mode="strict")


def test_empty_ridge_collection_stays_empty(tmp_path, monkeypatch):
    dtm, aoi = _make_site(tmp_path, "empty-site", **TEXAS)
    ctx = _ctx_for(dtm, aoi, TEXAS["crs"])
    c = ctx.aoi_analysis.centroid
    result = TerrainResult(valleys=[_diag_line((c.x, c.y))], ridges=[])
    fc, _ = _fc_from_terrain(tmp_path, result, monkeypatch)
    kinds = [f["properties"]["kind"] for f in fc["features"]]
    assert kinds.count("valley") == 1 and kinds.count("ridge") == 0


# ---------------------------------------------------------------------------
# 7: honest empty-keypoint semantics


def test_no_keypoints_produces_no_valid_keypoint_notice(tmp_path):
    dtm, aoi = _make_site(tmp_path, "nokp", **TEXAS)
    fc = run_pipeline(str(tmp_path / "nokp-proj"), aoi, drone_path=dtm)
    props = fc["properties"]
    # the tiny tilted plane yields no keypoint — that is a result, not an error
    assert props["counts"]["keypoints"] == 0
    assert props["counts"]["keylines"] == 0
    assert "NO_VALID_KEYPOINT" in props["notices"]


# ---------------------------------------------------------------------------
# 8 + 12 + 13: KML/GeoJSON coordinate agreement + no keyline folder when empty


def test_kml_matches_geojson_and_stays_on_footprint(drone_env, tmp_path):
    from app.jobs.terrain_job import execute_analysis_run

    dtm, aoi = _make_site(tmp_path, "kmlsite", **TEXAS)
    r = drone_env.client.post("/api/projects", json={"name": "kml", "aoi": aoi})
    pid = r.json()["project_id"]
    rid = db.create_analysis_run(pid, None, dtm, {"dem_mode": "auto"})
    execute_analysis_run(rid)

    fc = drone_env.client.get(f"/api/projects/{pid}/results").json()
    kml = drone_env.client.get(f"/api/projects/{pid}/export.kml").text
    b = fc["properties"]["dem_bounds_wgs84"]

    kml_coords = re.findall(r"(-?\d+\.\d+),(-?\d+\.\d+),0", kml)
    assert kml_coords, "KML has no coordinates at all"
    lons = [float(x) for x, _ in kml_coords]
    lats = [float(y) for _, y in kml_coords]
    # every KML coordinate on the active DTM footprint (+tiny slack)
    assert min(lons) >= b[0] - 0.001 and max(lons) <= b[2] + 0.001
    assert min(lats) >= b[1] - 0.001 and max(lats) <= b[3] + 0.001

    # KML and GeoJSON serialize the same geometry (compare valley v0)
    geo_valleys = [f for f in fc["features"]
                   if f["properties"]["kind"] == "valley"]
    if geo_valleys:
        first = geo_valleys[0]["geometry"]["coordinates"][0]
        assert any(abs(float(x) - first[0]) < 1e-6 and
                   abs(float(y) - first[1]) < 1e-6 for x, y in kml_coords)

    # zero keylines -> no keyline folder claimed in the export
    assert fc["properties"]["counts"]["keylines"] == 0
    assert "<Folder><name>Keylines</name>" not in kml


# ---------------------------------------------------------------------------
# 9 + 10: tilt QA


def test_39_degree_plane_triggers_suspect_global_tilt(tmp_path):
    origin = _site(**{k: TEXAS[k] for k in ("crs", "lon", "lat")})
    dtm = str(tmp_path / "tilted.tif")
    import rasterio
    from rasterio.transform import from_origin

    h = w = 200
    y, x = np.mgrid[0:h, 0:w]
    dem = (300.0 + 0.60 * x + 0.55 * y).astype("float32")  # |grad| ~0.814
    with rasterio.open(dtm, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs=TEXAS["crs"],
                       transform=from_origin(origin[0], origin[1], 1, 1)) as dst:
        dst.write(dem, 1)

    report = terrain_quality.assess_dtm(dtm, gcp_supplied=False)
    codes = [i.code for i in report.issues]
    assert terrain_quality.SUSPECT_GLOBAL_TILT in codes
    assert terrain_quality.VERTICAL_REFERENCE_UNVERIFIED in codes
    assert report.severe
    assert report.metrics["plane_slope_deg"] > 35


def test_legitimate_slope_not_flagged_and_never_flattened(tmp_path):
    origin = _site(**{k: TEXAS[k] for k in ("crs", "lon", "lat")})
    dtm = str(tmp_path / "legit.tif")
    import rasterio
    from rasterio.transform import from_origin

    h = w = 200
    y, x = np.mgrid[0:h, 0:w]
    dem = (300.0 + 0.12 * x + 3.0 * np.sin(y / 9.0)).astype("float32")  # 12%
    with rasterio.open(dtm, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs=TEXAS["crs"],
                       transform=from_origin(origin[0], origin[1], 1, 1)) as dst:
        dst.write(dem, 1)

    report = terrain_quality.assess_dtm(dtm, gcp_supplied=True)
    assert terrain_quality.SUSPECT_GLOBAL_TILT not in [i.code for i in report.issues]

    # run the pipeline and prove the analyzed surface kept its slope: QA
    # never silently detrends production data
    aoi = _aoi_for(TEXAS["crs"], origin, size=(h, w))
    proj = str(tmp_path / "legit-proj")
    fc = run_pipeline(proj, aoi, drone_path=dtm)
    with rasterio.open(os.path.join(proj, "dem_utm.tif")) as out:
        arr = out.read(1, masked=True)
        cols = np.arange(arr.shape[1]) * abs(out.res[0])
        row = np.ma.filled(arr[arr.shape[0] // 2], np.nan)
        ok = np.isfinite(row)
        slope = np.polyfit(cols[ok], row[ok], 1)[0]
    assert 0.10 < abs(slope) < 0.14  # ~12% preserved
    assert fc["properties"]["qa"] is None or not fc["properties"]["qa"]["severe"]


def test_strict_mode_blocks_keylines_on_severe_tilt(tmp_path, monkeypatch):
    monkeypatch.setenv("TERRAIN_QA_MODE", "strict")
    dtm, aoi = _make_site(tmp_path, "strictblock", **TEXAS,
                          size=(200, 200))
    # overwrite with a steep plane to trip QA
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(dtm) as src:
        t = src.transform
        crs = src.crs
    h = w = 200
    y, x = np.mgrid[0:h, 0:w]
    dem = (300.0 + 0.60 * x + 0.55 * y).astype("float32")
    with rasterio.open(dtm, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs=crs, transform=t) as dst:
        dst.write(dem, 1)
    fc = run_pipeline(str(tmp_path / "sb-proj"), aoi, drone_path=dtm)
    props = fc["properties"]
    assert props["qa"]["severe"]
    assert props["counts"]["keypoints"] == 0 and props["counts"]["keylines"] == 0
    assert "KEYLINE_GENERATION_BLOCKED" in props["notices"]
    assert props["watermark"] is None  # strict blocks instead of watermarking


def test_warn_mode_watermarks_severe_tilt(tmp_path, monkeypatch):
    monkeypatch.setenv("TERRAIN_QA_MODE", "warn")
    origin = _site(**{k: TEXAS[k] for k in ("crs", "lon", "lat")})
    dtm = str(tmp_path / "warn-tilt.tif")
    import rasterio
    from rasterio.transform import from_origin

    h = w = 200
    y, x = np.mgrid[0:h, 0:w]
    dem = (300.0 + 0.60 * x + 0.55 * y).astype("float32")
    with rasterio.open(dtm, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs=TEXAS["crs"],
                       transform=from_origin(origin[0], origin[1], 1, 1)) as dst:
        dst.write(dem, 1)
    aoi = _aoi_for(TEXAS["crs"], origin, size=(h, w))
    fc = run_pipeline(str(tmp_path / "warn-proj"), aoi, drone_path=dtm)
    assert fc["properties"]["watermark"] == terrain_quality.WATERMARK


# ---------------------------------------------------------------------------
# 11: reanalysis reuses the DTM and never calls the provider


def test_reanalyze_uses_existing_dtm_without_provider(drone_env, monkeypatch):
    import app.jobs as jobs_pkg
    from app.jobs.terrain_job import run_analysis_job

    monkeypatch.setenv("PHOTOGRAMMETRY_POLL_SECONDS", "0")
    # complete a full survey first (creates the DTM via the fake provider)
    client = drone_env.client
    r = client.post("/api/projects",
                    json={"name": "re", "aoi": aoi_inside_fake_dtm()})
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
    from app.jobs.photogrammetry_job import run_survey

    run_survey(sid)
    assert drone_env.provider.create_calls == 1

    enqueued = []

    class _FakeQueue:
        def enqueue(self, func, rid, **kw):
            enqueued.append(rid)

    monkeypatch.setattr(jobs_pkg, "get_queue", lambda: _FakeQueue())
    r = client.post(f"/api/projects/{pid}/reanalyze", json={})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    assert enqueued == [rid]

    run_analysis_job(rid)  # what the worker would execute
    assert drone_env.provider.create_calls == 1  # provider untouched

    run = client.get(f"/api/projects/{pid}/analysis-runs/{rid}").json()
    assert run["state"] == "completed"
    assert run["dem_mode"] == "drone_only"
    assert run["counts"] is not None
    # both runs preserved for comparison
    runs = client.get(f"/api/projects/{pid}/analysis-runs").json()["runs"]
    assert len(runs) == 2
