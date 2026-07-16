"""Structured progress, heartbeat/health, and design-download products.

Covers the acceptance checklist: dynamic stage plans, monotonic progress,
heartbeat-without-progress, stall/health classification, run/project
ownership, unchanged original-DTM bytes, a spatially-faithful visual GeoTIFF
with burned keylines that never overwrites elevation, honest no-keyline
behaviour, and a path-safe design ZIP.
"""

from __future__ import annotations

import json
import os
import time
import zipfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

import app.db as db
from app import exports, progress
from app.visual_export import build_visual_geotiff


# ---------------------------------------------------------------------------
# 1. Stage plans differ per DEM mode


def test_stage_plan_per_mode():
    sat = progress.build_stage_plan("satellite_only")
    drone = progress.build_stage_plan("drone_only")
    fused = progress.build_stage_plan("fused")
    # satellite must not mention drone fusion/coverage
    assert progress.FUSING_DEM not in sat
    assert progress.COMPUTING_DRONE_COVERAGE not in sat
    assert progress.FETCHING_SATELLITE_DEM in sat
    # drone-only must not fetch satellite
    assert progress.FETCHING_SATELLITE_DEM not in drone
    assert progress.PREPARING_DRONE_DEM in drone
    # fused uses both
    assert progress.FETCHING_SATELLITE_DEM in fused
    assert progress.FUSING_DEM in fused
    for plan in (sat, drone, fused):
        assert plan[0] == progress.LOADING_PROJECT
        assert plan[-1] == progress.COMPLETED


# ---------------------------------------------------------------------------
# Reporter fixture (isolated temp DB with one project + run)


@pytest.fixture()
def run_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "keyline.sqlite"))
    db.init_db()
    pid = db.create_project("t", {"type": "Polygon", "coordinates": [
        [[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]})
    rid = db.create_analysis_run(pid, None, None, {"dem_mode": "drone_only"})

    class C:
        pass
    c = C()
    c.pid, c.rid = pid, rid
    return c


# 2 + 3. progress never decreases; completed stages give normalized percent
def test_progress_monotonic_and_normalized(run_ctx):
    r = progress.ProgressReporter(run_ctx.rid, dem_mode="drone_only")
    plan = progress.build_stage_plan("drone_only")
    last = 0.0
    seen = []
    for stage in plan:
        r.start_stage(stage)
        row = db.get_analysis_run(run_ctx.rid)
        pct = row["progress_percent"]
        assert pct >= last, f"progress went backwards at {stage}"
        last = pct
        seen.append(pct)
    r.complete()
    row = db.get_analysis_run(run_ctx.rid)
    assert row["progress_percent"] == 100.0
    # about a third of the way once ~a third of stages are done
    third = seen[len(plan) // 3]
    assert 20 <= third <= 55


# 4. heartbeat updates liveness without advancing progress
def test_heartbeat_does_not_advance_progress(run_ctx):
    r = progress.ProgressReporter(run_ctx.rid, dem_mode="drone_only")
    r.start_stage(progress.CALCULATING_FLOW_ACCUMULATION)
    before = db.get_analysis_run(run_ctx.rid)
    time.sleep(0.02)
    r.heartbeat("still crunching flow accumulation")
    after = db.get_analysis_run(run_ctx.rid)
    assert after["progress_percent"] == before["progress_percent"]
    assert after["heartbeat_at"] >= before["heartbeat_at"]
    assert after["current_message"] == "still crunching flow accumulation"


# 7. a failed stage persists its error
def test_fail_persists_error(run_ctx):
    r = progress.ProgressReporter(run_ctx.rid, dem_mode="drone_only")
    r.start_stage(progress.CONDITIONING_DEM)
    r.fail("BOOM", "conditioning blew up")
    row = db.get_analysis_run(run_ctx.rid)
    assert row["state"] == "failed"
    assert row["error_code"] == "BOOM"
    assert "blew up" in row["error_message"]


# 5 + 6. health: stale -> warning; missing worker distinguishable from slow
def test_health_classification():
    now = 1_000_000.0
    base = {"state": "running", "started_at": now - 400}
    # fresh heartbeat -> active
    assert progress.classify_health(
        {**base, "heartbeat_at": now - 5}, now=now) == "active"
    # 2 min since heartbeat, worker still there -> slow/possibly_stalled
    slow = progress.classify_health(
        {**base, "heartbeat_at": now - 120}, worker_status="started", now=now)
    assert slow in ("slow", "possibly_stalled")
    # very stale but RQ job still started -> possibly_stalled (NOT missing)
    assert progress.classify_health(
        {**base, "heartbeat_at": now - 600}, worker_status="started",
        now=now) == "possibly_stalled"
    # RQ job gone -> worker_missing (distinct from slow processing)
    assert progress.classify_health(
        {**base, "heartbeat_at": now - 600}, worker_status="missing",
        now=now) == "worker_missing"
    # terminal states short-circuit
    assert progress.classify_health({"state": "completed"}, now=now) == "complete"
    assert progress.classify_health({"state": "failed"}, now=now) == "failed"


# cooperative cancel raises at the next stage boundary
def test_cancel_requested_raises(run_ctx):
    r = progress.ProgressReporter(run_ctx.rid, dem_mode="drone_only")
    r.start_stage(progress.CONDITIONING_DEM)
    assert db.request_run_cancel(run_ctx.rid) is True
    with pytest.raises(progress.AnalysisCancelled):
        r.start_stage(progress.CALCULATING_FLOW_ACCUMULATION)


# ---------------------------------------------------------------------------
# Export products — build a completed run directory by hand (no hydrology)


def _make_run_dir(tmp_path, *, with_keyline=True):
    """A minimal completed-run directory: dem_utm.tif + results.geojson."""
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    crs = "EPSG:32613"
    ox, oy, res, n = 597000.0, 2374000.0, 1.0, 120
    y, x = np.mgrid[0:n, 0:n]
    dem = (1900 + 0.05 * x + 3.0 * np.sin(x / 12.0)).astype("float32")
    transform = from_origin(ox, oy, res, res)
    with rasterio.open(out_dir / "dem_utm.tif", "w", driver="GTiff",
                       height=n, width=n, count=1, dtype="float32",
                       crs=crs, nodata=np.nan, transform=transform) as dst:
        dst.write(dem, 1)

    from pyproj import Transformer
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    def wln(pts):
        return {"type": "LineString",
                "coordinates": [list(to_wgs.transform(ox + e, oy - nn))
                                for e, nn in pts]}

    feats = [
        {"type": "Feature", "properties": {"kind": "valley", "id": "v0"},
         "geometry": wln([(20, 20), (20, 100)])},
        {"type": "Feature", "properties": {"kind": "ridge", "id": "r0"},
         "geometry": wln([(60, 20), (60, 100)])},
    ]
    counts = {"valleys": 1, "ridges": 1, "keypoints": 0, "keylines": 0,
              "contours": 0}
    if with_keyline:
        feats += [
            {"type": "Feature",
             "properties": {"kind": "keypoint", "id": "k0", "elevation": 1905,
                            "confidence": 0.8, "source": "drone"},
             "geometry": {"type": "Point",
                          "coordinates": list(to_wgs.transform(ox + 40, oy - 60))}},
            {"type": "Feature",
             "properties": {"kind": "keyline", "id": "l0", "keypoint_id": "k0"},
             "geometry": wln([(10, 60), (110, 55)])},
        ]
        counts.update(keypoints=1, keylines=1)
    fc = {"type": "FeatureCollection", "features": feats,
          "properties": {"project_id": "p1", "analysis_run_id": "r1",
                         "dem_mode": "drone_only", "analysis_crs": crs,
                         "counts": counts, "status": "completed",
                         "dem_resolution_m": 1.0}}
    with open(out_dir / "results.geojson", "w") as f:
        json.dump(fc, f)
    return str(out_dir), fc, str(out_dir / "dem_utm.tif")


# 11 + 13. visual GeoTIFF preserves grid; elevation raster untouched
def test_visual_geotiff_preserves_grid_and_leaves_dem_untouched(tmp_path):
    out_dir, fc, dem_path = _make_run_dir(tmp_path)
    with rasterio.open(dem_path) as src:
        dem_before = src.read(1)
        crs0, tr0, w0, h0, b0 = (src.crs, src.transform, src.width,
                                 src.height, src.bounds)
    dest = os.path.join(out_dir, "keyline-design-map.tif")
    info = build_visual_geotiff(out_dir, dest, aoi_wgs84=None)
    with rasterio.open(dest) as v:
        assert v.crs == crs0
        assert v.transform == tr0
        assert (v.width, v.height) == (w0, h0)
        assert v.bounds == b0
        assert v.count == 4 and v.dtypes[0] == "uint8"
    # the elevation raster is byte-for-byte unchanged
    with rasterio.open(dem_path) as src:
        assert np.array_equal(src.read(1), dem_before, equal_nan=True)
    assert info["burned"]["keylines"] == 1


# 12. keylines are visibly rasterized (green) into the visual GeoTIFF
def test_keylines_visible_in_visual_geotiff(tmp_path):
    out_dir, fc, _ = _make_run_dir(tmp_path)
    dest = os.path.join(out_dir, "keyline-design-map.tif")
    build_visual_geotiff(out_dir, dest, aoi_wgs84=None)
    with rasterio.open(dest) as v:
        r, g, b = v.read(1), v.read(2), v.read(3)
    green = (g > 180) & (r < 120) & (b < 120)
    assert green.sum() > 50, "keyline green pixels missing from visual map"


# 14. no keyline -> keyline exports unavailable; visual map is diagnostic
def test_no_keyline_marks_design_unavailable(tmp_path):
    out_dir, fc, _ = _make_run_dir(tmp_path, with_keyline=False)
    avail = exports.export_availability(fc)
    assert avail["keylines_geojson"] is False
    assert avail["keylines_kml"] is False
    assert avail["unavailable_reason"]
    with pytest.raises(exports.ExportUnavailable):
        exports.keylines_geojson(fc)
    run_avail = exports.generate_run_exports(out_dir, fc)
    assert run_avail["visual_is_diagnostic"] is True  # still a diagnostic map


# 15. keyline GeoJSON/KML coordinates overlap the DEM footprint
def test_keyline_exports_overlap_footprint(tmp_path):
    out_dir, fc, dem_path = _make_run_dir(tmp_path)
    with rasterio.open(dem_path) as src:
        from pyproj import Transformer
        from shapely.geometry import box, shape
        from shapely.ops import transform as shp_transform
        to_wgs = Transformer.from_crs(src.crs, "EPSG:4326",
                                      always_xy=True).transform
        footprint = shp_transform(to_wgs, box(*src.bounds))
    sub = exports.keylines_geojson(fc)
    for feat in sub["features"]:
        if feat["properties"]["kind"] == "keyline":
            assert shape(feat["geometry"]).intersects(footprint)
    kml = exports.keylines_kml(fc, "t")
    assert "<LineString>" in kml or "<Placemark>" in kml


# 16 + 17. design ZIP contains expected files; no path traversal
def test_design_package_contents_and_no_traversal(tmp_path):
    out_dir, fc, _ = _make_run_dir(tmp_path)
    build_visual_geotiff(out_dir, os.path.join(out_dir, "keyline-design-map.tif"))
    dtm_src = tmp_path / "orig.tif"
    with rasterio.open(out_dir + "/dem_utm.tif") as s:
        prof = s.profile
        with rasterio.open(dtm_src, "w", **prof) as d:
            d.write(s.read())
    zip_path = os.path.join(out_dir, "exports", "design-package.zip")
    exports.build_design_package(
        zip_path, out_dir=out_dir, fc=fc,
        run={"analysis_version": "2"},
        project={"name": "../../etc/evil name"},
        original_dtm=(str(dtm_src), "original-dtm.tif"))
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    base = {n.split("/", 1)[1] for n in names}
    for expected in ("README.txt", "original-dtm.tif", "keyline-design-map.tif",
                     "keylines.geojson", "keylines.kml", "terrain-layers.geojson",
                     "analysis-summary.json", "terrain-qa.json", "manifest.json"):
        assert expected in base, f"{expected} missing from package"
    # every arcname is safely namespaced — no absolute or parent paths
    for n in names:
        assert not n.startswith("/")
        assert ".." not in n.split("/")


# ---------------------------------------------------------------------------
# API-level: ownership, status persistence, downloads (uses the full worker)


def _complete_survey_run(drone_env):
    from app.jobs.photogrammetry_job import run_survey
    from survey_helpers import aoi_inside_fake_dtm, make_jpeg_bytes

    client = drone_env.client
    pid = client.post("/api/projects", json={
        "name": "dl", "aoi": aoi_inside_fake_dtm()}).json()["project_id"]
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
    runs = client.get(f"/api/projects/{pid}/analysis-runs").json()["runs"]
    return client, pid, runs[0]["id"]


# 8 + 9. status refresh is stable; ownership enforced; no-store header
def test_status_refresh_and_ownership(drone_env):
    client, pid, rid = _complete_survey_run(drone_env)
    resp = client.get(f"/api/projects/{pid}/analysis-runs/{rid}")
    assert resp.headers.get("cache-control") == "no-store"
    a = resp.json()
    assert a["state"] in ("completed", "completed_with_warnings")
    assert a["stage_count"] > 0 and a["progress_percent"] == 100
    assert a["health"] == "complete"
    assert set(a["exports"]) == {"original_dtm", "keylines_geojson",
                                 "keylines_kml", "visual_geotiff", "design_bundle"}
    # a second fetch returns the same terminal state
    b = client.get(f"/api/projects/{pid}/analysis-runs/{rid}").json()
    assert b["state"] == a["state"] and b["stage"] == a["stage"]
    # cross-project access is rejected
    other = client.post("/api/projects", json={
        "name": "other", "aoi": {"type": "Polygon", "coordinates": [
            [[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}}).json()["project_id"]
    assert client.get(
        f"/api/projects/{other}/analysis-runs/{rid}").status_code == 404


# 10 + 19. original DTM download is byte-identical + safe attachment name
def test_original_dtm_download_unchanged(drone_env):
    client, pid, rid = _complete_survey_run(drone_env)
    run = db.get_analysis_run(rid)
    src = run["dem_path"]
    with open(src, "rb") as f:
        original = f.read()
    resp = client.get(f"/api/projects/{pid}/analysis-runs/{rid}/downloads/dtm")
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;") and ".tif" in cd and ".." not in cd
    assert resp.content == original  # untouched elevation bytes


# 18. previous run's exports are not overwritten by a new run
def test_previous_run_exports_preserved(drone_env):
    client, pid, rid1 = _complete_survey_run(drone_env)
    # visual map for run 1
    client.get(f"/api/projects/{pid}/analysis-runs/{rid1}/downloads/"
               "keyline-design-map.tif")
    from app.jobs.terrain_job import run_output_dir
    map1 = os.path.join(run_output_dir(pid, rid1), "keyline-design-map.tif")
    assert os.path.isfile(map1)
    # a fresh reanalyze creates a new run with its own directory
    import app.jobs as jobs_pkg
    from app.jobs.terrain_job import run_analysis_job

    class _Q:
        def enqueue(self, func, r, **kw):
            run_analysis_job(r)
    jobs_pkg.get_queue = lambda: _Q()  # type: ignore
    rid2 = client.post(f"/api/projects/{pid}/reanalyze", json={}).json()["run_id"]
    assert rid2 != rid1
    map2 = os.path.join(run_output_dir(pid, rid2), "keyline-design-map.tif")
    assert os.path.isfile(map1) and os.path.isfile(map2)
    assert run_output_dir(pid, rid1) != run_output_dir(pid, rid2)


# 22. regenerating exports does not rerun hydrology (no new run, dem_utm reused)
def test_regenerate_exports_no_hydrology(drone_env):
    client, pid, rid = _complete_survey_run(drone_env)
    from app.jobs.terrain_job import run_output_dir
    dem_utm = os.path.join(run_output_dir(pid, rid), "dem_utm.tif")
    mtime_before = os.path.getmtime(dem_utm)
    r = client.post(
        f"/api/projects/{pid}/analysis-runs/{rid}/regenerate-exports")
    assert r.status_code == 200
    # the hydrology output (dem_utm.tif) was not rewritten
    assert os.path.getmtime(dem_utm) == mtime_before


# design package streams from the run endpoint
def test_design_package_download(drone_env):
    client, pid, rid = _complete_survey_run(drone_env)
    r = client.get(
        f"/api/projects/{pid}/analysis-runs/{rid}/downloads/design-package.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    import io
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert any(n.endswith("manifest.json") for n in zf.namelist())
