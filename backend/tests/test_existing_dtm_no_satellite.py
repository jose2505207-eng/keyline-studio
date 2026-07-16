"""Existing-DTM analysis must never silently invoke Copernicus/fusion.

Regression guard for the "terrain_source=existing_dtm but the backend switched
to Fused (drone + satellite) and hung on Fusing DEM" bug. Covers full vs
partial coverage, explicit satellite gap-filling, reanalysis provenance
preservation, the stall watchdog, cancellation, and duplicate-worker guard.
"""

from __future__ import annotations

import time

import pytest

import app.db as db
from app import pipeline, progress
from app.pipeline import InsufficientCoverageError, run_pipeline
from fake_provider import write_synthetic_dtm
from survey_helpers import aoi_inside_fake_dtm


def _partial_aoi_dtm(tmp_path):
    """A DTM covering only part of the standard AOI footprint."""
    dtm = str(tmp_path / "half.tif")
    write_synthetic_dtm(dtm, size=(120, 55), nodata_corner=False)
    return dtm


# --- 1. existing DTM fully covering the polygon -> drone_only, no satellite ---
def test_existing_dtm_full_coverage_runs_drone_only(tmp_path, monkeypatch):
    from app import dem_source

    monkeypatch.setattr(dem_source, "fetch_glo30", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("satellite fetch must not happen for a covering DTM")))
    dtm = str(tmp_path / "full.tif")
    write_synthetic_dtm(dtm, nodata_corner=False)
    fc = run_pipeline(str(tmp_path / "proj"), aoi_inside_fake_dtm(),
                      drone_path=dtm)
    props = fc["properties"]
    assert props["dem_mode"] == "drone_only"
    assert props["drone_coverage"] >= 0.98


# --- 2. partial coverage + gap filling disabled -> actionable error -----------
def test_existing_dtm_partial_coverage_stops_with_error(tmp_path, monkeypatch):
    from app import dem_source

    monkeypatch.setattr(dem_source, "fetch_glo30", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("satellite must not be fetched when gap-filling is off")))
    dtm = _partial_aoi_dtm(tmp_path)
    with pytest.raises(InsufficientCoverageError) as ei:
        run_pipeline(str(tmp_path / "proj"), aoi_inside_fake_dtm(),
                     drone_path=dtm)
    assert ei.value.code == "DTM_COVERAGE_INSUFFICIENT"
    assert f"{ei.value.coverage * 100:.1f}%" in str(ei.value)


# --- 3. explicit satellite gap filling enabled -> satellite IS fetched --------
def test_existing_dtm_partial_coverage_fetches_satellite_on_optin(tmp_path,
                                                                  monkeypatch):
    """With the explicit opt-in, a partially-covering DTM resolves to fused and
    Copernicus is fetched. We stop at the fetch (sentinel) to keep the test
    offline while proving the satellite path is entered only on opt-in."""
    called = {"n": 0}

    class _FetchReached(RuntimeError):
        pass

    def _sentinel(*a, **k):
        called["n"] += 1
        raise _FetchReached()

    monkeypatch.setattr(pipeline.dem_source, "fetch_glo30", _sentinel)
    dtm = _partial_aoi_dtm(tmp_path)
    with pytest.raises(_FetchReached):
        run_pipeline(str(tmp_path / "proj"), aoi_inside_fake_dtm(),
                     drone_path=dtm, fill_missing_areas_with_satellite=True)
    assert called["n"] == 1  # satellite fetched — only because opt-in was set


# --- reporter: terrain_source is never overwritten by mode resolution ---------
@pytest.fixture()
def run_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "k.sqlite"))
    db.init_db()
    pid = db.create_project("t", {"type": "Polygon", "coordinates": [
        [[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]})

    class C:
        pass
    c = C()
    c.pid = pid
    return c


def test_terrain_source_preserved_through_mode_resolution(run_ctx):
    rid = db.create_analysis_run(run_ctx.pid, None, None,
                                 {"terrain_source": "existing_dtm"})
    r = progress.ProgressReporter(rid, dem_mode="auto",
                                  terrain_source="existing_dtm")
    # the engine resolves to drone_only, but provenance must stay existing_dtm
    r.set_mode("drone_only")
    row = db.get_analysis_run(rid)
    assert row["dem_mode"] == "drone_only"
    assert row["terrain_source"] == "existing_dtm"
    # even a fused resolution keeps the user's provenance label
    r.set_mode("fused")
    assert db.get_analysis_run(rid)["terrain_source"] == "existing_dtm"


# --- 4. reanalysis preserves terrain_source -----------------------------------
def test_reanalysis_preserves_terrain_source(drone_env, monkeypatch):
    # Build a project with an existing (upload) DTM via the library.
    client = drone_env.client
    from survey_helpers import aoi_inside_fake_dtm as _aoi
    from fake_provider import write_synthetic_dtm as _wd

    dtm_path = str(drone_env.tmp / "existing.tif")
    _wd(dtm_path, nodata_corner=False)
    did = drone_env.db.create_dtm(
        storage_path=dtm_path, display_name="existing.tif",
        original_filename="existing.tif", source_type="upload",
        size_bytes=1, checksum=None, crs="EPSG:32613", width=120, height=120,
        nodata=-9999.0)
    pid = client.post("/api/projects", json={"name": "ex", "aoi": _aoi()}).json()[
        "project_id"]
    r = client.post(f"/api/projects/{pid}/analyze",
                    json={"dtm_id": did, "dem_mode": "auto"})
    assert r.status_code == 200
    rid = r.json()["run_id"]
    run = db.get_analysis_run(rid)
    assert (run.get("params_json") or {}).get("terrain_source") == "existing_dtm"

    # reanalyze without arguments must inherit existing_dtm (not become fused)
    import app.jobs as jobs_pkg
    from app.jobs.terrain_job import run_analysis_job

    class _Q:
        def enqueue(self, func, r, **kw):
            pass
    monkeypatch.setattr(jobs_pkg, "get_queue", lambda: _Q())
    rid2 = client.post(f"/api/projects/{pid}/reanalyze", json={}).json()["run_id"]
    run2 = db.get_analysis_run(rid2)
    assert (run2.get("params_json") or {}).get("terrain_source") == "existing_dtm"


# --- 5a. stall watchdog marks a no-progress stage failed ----------------------
def test_watchdog_marks_stalled(run_ctx, monkeypatch):
    monkeypatch.setenv("ANALYSIS_STAGE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("ANALYSIS_HEARTBEAT_INTERVAL_SECONDS", "1")
    rid = db.create_analysis_run(run_ctx.pid, None, None, {})
    r = progress.ProgressReporter(rid, dem_mode="drone_only", stage_timeout=1)
    r.start_stage(progress.FUSING_DEM, "fusing")
    r.start()
    # do NOT report progress; the watchdog should flip the run to failed/stalled
    deadline = time.time() + 8
    while time.time() < deadline:
        row = db.get_analysis_run(rid)
        if row["state"] == "failed":
            break
        time.sleep(0.3)
    r.close()
    row = db.get_analysis_run(rid)
    assert row["state"] == "failed"
    assert row["error_code"] == "STAGE_STALLED"


# --- 5b. cancellation stops the run at the next stage boundary ----------------
def test_cancellation_stops_and_cleans(run_ctx, tmp_path):
    rid = db.create_analysis_run(run_ctx.pid, None, None, {})
    r = progress.ProgressReporter(rid, dem_mode="drone_only")
    r.start_stage(progress.CONDITIONING_DEM)
    assert db.request_run_cancel(rid) is True
    with pytest.raises(progress.AnalysisCancelled):
        r.start_stage(progress.CALCULATING_FLOW_ACCUMULATION)
    # operation() inside a stage also honours cancellation
    with pytest.raises(progress.AnalysisCancelled):
        r.operation("still working")


# --- 6. duplicate-worker guard: second claim on a live run is refused ---------
def test_duplicate_worker_claim_refused(run_ctx):
    rid = db.create_analysis_run(run_ctx.pid, None, None, {})
    assert db.claim_analysis_run(rid, "worker-A") is True
    # a different worker cannot steal a fresh claim...
    db.update_analysis_run(rid, heartbeat_at=time.time(), state="running")
    assert db.claim_analysis_run(rid, "worker-B", stale_after=120) is False
    # ...but the same worker re-entering is fine (idempotent)
    assert db.claim_analysis_run(rid, "worker-A") is True
    # a stale claim (no recent heartbeat) can be taken over
    db.update_analysis_run(rid, heartbeat_at=time.time() - 10_000)
    assert db.claim_analysis_run(rid, "worker-B", stale_after=120) is True
