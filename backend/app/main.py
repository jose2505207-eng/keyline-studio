"""Keyline Studio API."""

from __future__ import annotations

import json
import logging
import os

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import db, pipeline
from .dem_source import DemSourceError

log = logging.getLogger(__name__)

# Surface our own INFO diagnostics (project store location, project lookup
# misses, analyze resolution). uvicorn attaches handlers only to its own
# loggers and leaves the root without one, so records from the "app" logger
# would otherwise be dropped — attach a dedicated handler.
_app_logger = logging.getLogger("app")
_app_logger.setLevel(
    getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
if not _app_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
    _app_logger.addHandler(_h)
    _app_logger.propagate = False

DATA_DIR = os.environ.get(
    "KEYLINE_DATA", os.path.join(os.path.dirname(__file__), "..", "data")
)

app = FastAPI(title="Keyline Studio API")
app.add_middleware(
    CORSMiddleware,
    # Vite dev server origins + the Vercel-hosted frontend
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    db.init_db()
    from .dtm_api import ensure_dtm_dir

    ensure_dtm_dir()
    # Surface where project metadata lives. On hosts without a persistent
    # disk (e.g. Render free tier) this path is wiped on every redeploy, so
    # browser-stored project IDs go stale — the frontend recreates projects
    # from the AOI + selected DTM automatically when that happens. Only an
    # explicit KEYLINE_DB (pointed at a mounted volume) is a reliable
    # persistence signal; an unset default should be treated as ephemeral.
    explicitly_configured = bool(os.environ.get("KEYLINE_DB"))
    log.info("project store: db=%s persistence_configured=%s dtm_dir=%s "
             "(unconfigured stores may reset on redeploy; clients auto-recover)",
             os.path.abspath(db.DB_PATH), explicitly_configured,
             os.path.abspath(ensure_dtm_dir()))
    # Surveys stranded mid-flight by a crash/restart are resumed or returned
    # to a recoverable state (never falsely completed).
    try:
        from .jobs import reconcile_stale_surveys

        reconcile_stale_surveys()
    except Exception as exc:  # noqa: BLE001 — startup must not die on this
        import logging

        logging.getLogger(__name__).warning("survey reconciliation failed: %s", exc)


from . import dtm_api, surveys_api  # noqa: E402

app.include_router(surveys_api.router)
app.include_router(surveys_api.health_router)
app.include_router(dtm_api.router)


def project_dir(pid: str) -> str:
    return os.path.join(DATA_DIR, pid)


class ProjectIn(BaseModel):
    name: str
    aoi: dict  # GeoJSON Polygon, WGS84


class MoveIn(BaseModel):
    lng: float
    lat: float


def _require_project(pid: str) -> dict:
    proj = db.get_project(pid)
    if proj is None:
        log.info("project lookup MISS id=%s db=%s (stale/ephemeral — client "
                 "will recreate)", pid, os.path.abspath(db.DB_PATH))
        raise HTTPException(404, "Project not found")
    return proj


@app.post("/api/projects")
def create_project(body: ProjectIn):
    geom = body.aoi.get("geometry", body.aoi)  # accept Feature or bare geometry
    if geom.get("type") != "Polygon":
        raise HTTPException(422, "aoi must be a GeoJSON Polygon")
    pid = db.create_project(body.name, geom)
    os.makedirs(project_dir(pid), exist_ok=True)
    log.info("project created id=%s name=%r", pid, body.name)
    return {"project_id": pid}


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    """Lightweight existence + summary check so the frontend can validate a
    browser-stored project id before analyzing (and recreate it if stale)."""
    proj = _require_project(pid)
    run = db.latest_completed_run(pid)
    return {
        "project_id": pid,
        "name": proj["name"],
        "has_drone_dtm": bool(proj.get("drone_path")
                              and os.path.isfile(proj["drone_path"])),
        "has_results": bool(run and run.get("result_dir")),
    }


@app.post("/api/projects/{pid}/drone-dem")
async def upload_drone_dem(pid: str, file: UploadFile):
    """Accept a photogrammetry DTM GeoTIFF (any projected or geographic CRS —
    reprojection happens in the pipeline). Validates band count, CRS presence,
    that it isn't all nodata, and that elevations are plausible; reports the
    detected CRS/resolution/bounds and a WGS84 footprint so the user can
    confirm it landed in the right place."""
    _require_project(pid)
    import numpy as np
    import rasterio
    from pyproj import Transformer

    dest = os.path.join(project_dir(pid), "drone_dem.tif")
    os.makedirs(project_dir(pid), exist_ok=True)
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    try:
        with rasterio.open(dest) as src:
            if src.count != 1:
                raise ValueError(f"expected a single-band raster, got {src.count} bands")
            if src.crs is None:
                raise ValueError("raster has no CRS — export it georeferenced")
            # decimated masked read: cheap stats without loading a huge DTM
            out_h = min(src.height, 512)
            out_w = min(src.width, 512)
            arr = src.read(1, out_shape=(out_h, out_w), masked=True)
            arr = np.ma.masked_invalid(arr)
            if arr.mask.all():
                raise ValueError("raster contains only nodata")
            lo, hi = float(arr.min()), float(arr.max())
            if lo < -500.0 or hi > 9000.0:
                raise ValueError(
                    f"elevations {lo:.0f}..{hi:.0f} m are outside -500..9000 m — "
                    "is this really a DTM?")
            b = src.bounds
            tr = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
            ring = [list(tr.transform(x, y)) for x, y in
                    [(b.left, b.top), (b.right, b.top), (b.right, b.bottom),
                     (b.left, b.bottom), (b.left, b.top)]]
            info = {
                "crs": str(src.crs),
                "resolution_m": [round(abs(src.res[0]), 3), round(abs(src.res[1]), 3)],
                "size_px": [src.width, src.height],
                "elevation_range_m": [round(lo, 1), round(hi, 1)],
                "footprint": {"type": "Polygon", "coordinates": [ring]},
            }
    except HTTPException:
        raise
    except Exception as exc:
        os.remove(dest)
        raise HTTPException(422, f"Not a usable GeoTIFF DTM: {exc}")
    db.set_drone_path(pid, dest)
    # also register in the managed DTM library so it shows up in the selector
    from .dtm_api import DtmPathError, inspect_dtm_raster

    if db.find_dtm_by_path(dest) is None:
        try:
            meta = inspect_dtm_raster(dest)
            db.create_dtm(
                storage_path=dest,
                display_name=file.filename or "drone_dem.tif",
                original_filename=file.filename, source_type="upload",
                size_bytes=meta["size_bytes"], checksum=None,
                crs=meta["crs"], width=meta["width"], height=meta["height"],
                nodata=meta["nodata"], project_id=pid, metadata=meta)
        except DtmPathError as exc:
            import logging

            logging.getLogger(__name__).warning(
                "legacy DTM upload not registered in library: %s", exc)
    return {"ok": True, **info}


def _results_dir(pid: str) -> str:
    """Directory holding the project's current analysis outputs: the latest
    completed analysis run, falling back to the legacy project-level layout
    for pre-versioning projects."""
    run = db.latest_completed_run(pid)
    if run and run.get("result_dir") and \
            os.path.isfile(os.path.join(run["result_dir"], "results.geojson")):
        return run["result_dir"]
    return project_dir(pid)


def _run_job(jid: str, rid: str):
    from .jobs.terrain_job import execute_analysis_run

    try:
        def progress(step: str):
            db.update_job(jid, f"running:{step}", log_line=step)

        db.update_job(jid, "running:starting", log_line="starting")
        execute_analysis_run(rid, extra_progress=progress)
        db.update_job(jid, "done", log_line="done")
    except (DemSourceError, ValueError) as exc:
        db.update_job(jid, f"error:{exc}", log_line=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface anything to the job record
        db.update_job(jid, f"error:internal error: {exc}", log_line=str(exc))


class AnalyzeIn(BaseModel):
    dtm_id: str | None = None
    dem_mode: str = "auto"
    terrain: dict | None = None  # advanced terrain parameters (whitelisted)


@app.post("/api/projects/{pid}/analyze")
def analyze(pid: str, background: BackgroundTasks,
            body: AnalyzeIn | None = None):
    """Run terrain analysis. With ``dtm_id`` the library DTM is resolved and
    verified server-side before anything is queued; without a body the legacy
    behavior (project drone_path, auto mode) is preserved."""
    proj = _require_project(pid)
    body = body or AnalyzeIn()

    survey_id = None
    if body.dtm_id:
        from .dtm_api import resolve_dtm_for_analysis

        dtm, dem_path = resolve_dtm_for_analysis(body.dtm_id)
        survey_id = dtm.get("survey_id")
        db.set_drone_path(pid, dem_path)  # keypoint-move + legacy reuse
        log.info("analyze project=%s dtm_id=%s dtm_path=%s survey=%s mode=%s",
                 pid, body.dtm_id, dem_path, survey_id, body.dem_mode)
    else:
        dem_path = proj.get("drone_path")
        if dem_path and not os.path.isfile(dem_path):
            dem_path = None  # stale pointer must not fail the satellite run
        if body.dem_mode in ("drone_only", "fused") and not dem_path:
            raise HTTPException(
                422, f"dem_mode={body.dem_mode} requires a DTM — select or "
                     "upload one first")

    jid = db.create_job(pid)
    rid = db.create_analysis_run(pid, survey_id, dem_path,
                                 {"trigger": "analyze",
                                  "dem_mode": body.dem_mode,
                                  "dtm_id": body.dtm_id,
                                  "terrain": body.terrain})
    background.add_task(_run_job, jid, rid)
    return {"job_id": jid, "run_id": rid}


@app.get("/api/projects/{pid}/status")
def status(pid: str):
    _require_project(pid)
    job = db.latest_job(pid)
    if job is None:
        return {"state": "none", "log": []}
    return {"job_id": job["id"], "state": job["state"], "log": job["log"]}


@app.get("/api/projects/{pid}/results")
def results(pid: str):
    _require_project(pid)
    path = os.path.join(_results_dir(pid), "results.geojson")
    if not os.path.exists(path):
        raise HTTPException(404, "No results yet — run analyze first")
    with open(path) as f:
        return JSONResponse(json.load(f))


@app.get("/api/projects/{pid}/hillshade")
def hillshade(pid: str):
    _require_project(pid)
    path = os.path.join(_results_dir(pid), "hillshade.png")
    bounds_path = os.path.join(_results_dir(pid), "hillshade_bounds.json")
    if not os.path.exists(path):
        raise HTTPException(404, "No hillshade yet")
    with open(bounds_path) as f:
        bounds = json.load(f)
    return FileResponse(path, media_type="image/png",
                        headers={"X-Bounds": json.dumps(bounds)})


@app.get("/api/projects/{pid}/hillshade-bounds")
def hillshade_bounds(pid: str):
    _require_project(pid)
    bounds_path = os.path.join(_results_dir(pid), "hillshade_bounds.json")
    if not os.path.exists(bounds_path):
        raise HTTPException(404, "No hillshade yet")
    with open(bounds_path) as f:
        return JSONResponse(json.load(f))


@app.post("/api/projects/{pid}/keypoints/{kid}/move")
def move_keypoint(pid: str, kid: str, body: MoveIn):
    proj = _require_project(pid)
    try:
        return pipeline.recompute_keyline(
            _results_dir(pid), proj["aoi"], kid, body.lng, body.lat)
    except KeyError:
        raise HTTPException(404, f"Keypoint {kid} not found")
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc))


# ---------------------------------------------------------------------------
# Versioned terrain re-analysis (never resubmits photogrammetry)


class ReanalyzeIn(BaseModel):
    survey_id: str | None = None   # pick a specific survey's DTM
    dtm_id: str | None = None      # pick a library DTM (preferred)
    dem_mode: str = "auto"
    terrain: dict | None = None    # advanced terrain parameters (whitelisted)


@app.post("/api/projects/{pid}/reanalyze")
def reanalyze(pid: str, body: ReanalyzeIn):
    """Re-run terrain analysis with the existing validated drone DTM and the
    current AOI. Photographs are never resubmitted to the processing node;
    a new analysis run is created and previous runs are preserved."""
    proj = _require_project(pid)

    dem_path = None
    survey_id = body.survey_id
    if body.dtm_id:
        from .dtm_api import resolve_dtm_for_analysis

        dtm, dem_path = resolve_dtm_for_analysis(body.dtm_id)
        survey_id = dtm.get("survey_id") or survey_id
    elif survey_id:
        survey = db.get_survey(survey_id)
        if survey is None or survey["project_id"] != pid:
            raise HTTPException(404, "Survey not found in this project")
        dem_path = survey.get("dtm_path")
        if not dem_path or not os.path.isfile(dem_path):
            raise HTTPException(422, "That survey has no validated DTM")
    else:
        # latest survey DTM, then the manually uploaded one
        for s in db.list_surveys(pid):
            if s.get("dtm_path") and os.path.isfile(s["dtm_path"]):
                dem_path, survey_id = s["dtm_path"], s["id"]
                break
        if dem_path is None and proj.get("drone_path") and \
                os.path.isfile(proj["drone_path"]):
            dem_path = proj["drone_path"]

    if body.dem_mode in ("drone_only", "fused") and not dem_path:
        raise HTTPException(422, f"dem_mode={body.dem_mode} requires a drone "
                                 "DTM, and this project has none")

    rid = db.create_analysis_run(pid, survey_id, dem_path,
                                 {"trigger": "reanalyze",
                                  "dem_mode": body.dem_mode,
                                  "dtm_id": body.dtm_id,
                                  "terrain": body.terrain})
    from .jobs import QueueUnavailable, get_queue

    try:
        get_queue().enqueue("app.jobs.terrain_job.run_analysis_job", rid,
                            job_timeout=3600, result_ttl=86400)
    except QueueUnavailable as exc:
        db.update_analysis_run(rid, state="failed",
                               error_message=f"worker queue unavailable: {exc}")
        raise HTTPException(503, str(exc))
    return {"run_id": rid, "state": "queued",
            "dem_path": bool(dem_path), "survey_id": survey_id}


def _rq_job_status(rq_job_id: str | None) -> str | None:
    """RQ job status ('started'/'queued'/'finished'/'failed'/…) or 'missing'
    when the job record is gone. None when there's no RQ job (inline run) or
    the queue backend is unreachable."""
    if not rq_job_id:
        return None
    try:
        import redis
        from rq.job import Job

        from . import config as _cfg

        conn = redis.Redis.from_url(_cfg.redis_url())
        job = Job.fetch(rq_job_id, connection=conn)
        return job.get_status(refresh=True)
    except Exception as exc:  # noqa: BLE001
        from rq.exceptions import NoSuchJobError

        if isinstance(exc, NoSuchJobError):
            return "missing"
        return None


def _run_output_dir(run: dict) -> str:
    if run.get("result_dir"):
        return run["result_dir"]
    from .jobs.terrain_job import run_output_dir

    return run_output_dir(run["project_id"], run["id"])


def _run_downloads(run: dict) -> dict:
    """Which download products this run can currently serve."""
    from . import exports as exports_mod

    out_dir = _run_output_dir(run)
    counts = run.get("counts_json") or {}
    has_keylines = bool(counts.get("keylines"))
    terminal = run.get("state") in {"completed", "completed_with_warnings"}
    original = exports_mod.resolve_original_dtm(run, out_dir) is not None
    visual = os.path.isfile(os.path.join(out_dir, "keyline-design-map.tif"))
    return {
        "original_dtm": bool(terminal and original),
        "keylines_geojson": bool(terminal and has_keylines),
        "keylines_kml": bool(terminal and has_keylines),
        "visual_geotiff": bool(terminal and visual),
        "design_bundle": bool(terminal),
    }


def _run_out(run: dict, *, full: bool = False) -> dict:
    from . import progress as prog

    now = __import__("time").time()
    started = run.get("started_at") or run.get("created_at")
    hb = run.get("heartbeat_at")
    elapsed = int((run.get("completed_at") or now) - started) if started else None
    since_hb = int(now - hb) if hb else None
    worker_status = _rq_job_status(run.get("rq_job_id"))
    health = prog.classify_health(run, worker_status=worker_status, now=now)
    out = {
        "id": run["id"], "project_id": run["project_id"],
        "survey_id": run.get("survey_id"), "state": run["state"],
        "stage": run.get("stage"),
        "stage_label": run.get("stage_label") or prog.stage_label(run.get("stage")),
        "stage_index": run.get("stage_index") or 0,
        "stage_count": run.get("stage_count") or 0,
        "stage_plan": run.get("stage_plan_json") or [],
        "progress_percent": run.get("progress_percent") or 0,
        "current_message": run.get("current_message"),
        "dem_mode": run.get("dem_mode"),
        "terrain_source": run.get("terrain_source") or run.get("dem_mode"),
        "analysis_version": run.get("analysis_version"),
        "has_dem": bool(run.get("dem_path")),
        "params": run.get("params_json") or {},
        "counts": run.get("counts_json"),
        "feature_counts": run.get("counts_json") or {
            "valleys": 0, "ridges": 0, "keypoints": 0, "keylines": 0},
        "notices": run.get("notices_json") or [],
        "qa": run.get("qa_json"),
        "warnings": run.get("warnings_json") or [],
        "error_code": run.get("error_code"),
        "error_message": run.get("error_message"),
        "started_at": run.get("started_at"),
        "heartbeat_at": run.get("heartbeat_at"),
        "updated_at": run.get("updated_at"),
        "created_at": run["created_at"],
        "completed_at": run.get("completed_at"),
        "elapsed_seconds": elapsed,
        "seconds_since_heartbeat": since_hb,
        "health": health,
        "health_message": prog.health_message(health, since_hb),
        "cancellable": run.get("state") in ("queued", "running")
        and not run.get("cancel_requested"),
        "worker": {"rq_job_id": run.get("rq_job_id"),
                   "status": worker_status,
                   "worker_name": run.get("worker_name")},
        "exports": _run_downloads(run),
    }
    if full:
        out["log"] = run.get("log_json") or []
    return out


def _no_store(payload: dict) -> JSONResponse:
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/projects/{pid}/analysis-runs")
def list_analysis_runs(pid: str):
    _require_project(pid)
    return _no_store({"runs": [_run_out(r) for r in db.list_analysis_runs(pid)]})


def _require_run(pid: str, rid: str) -> dict:
    _require_project(pid)
    run = db.get_analysis_run(rid)
    if run is None or run["project_id"] != pid:
        raise HTTPException(404, "Analysis run not found in this project")
    return run


@app.get("/api/projects/{pid}/analysis-runs/{rid}")
def get_analysis_run(pid: str, rid: str):
    run = _require_run(pid, rid)
    return _no_store(_run_out(run, full=True))


@app.post("/api/projects/{pid}/analysis-runs/{rid}/cancel")
def cancel_analysis_run(pid: str, rid: str):
    run = _require_run(pid, rid)
    if not db.request_run_cancel(rid):
        raise HTTPException(409, "Run is already in a terminal state")
    # best-effort: also cancel the RQ job so a queued job never starts
    if run.get("rq_job_id"):
        try:
            import redis
            from rq.job import Job

            from . import config as _cfg

            conn = redis.Redis.from_url(_cfg.redis_url())
            Job.fetch(run["rq_job_id"], connection=conn).cancel()
        except Exception:  # noqa: BLE001 — cooperative flag is the real signal
            pass
    return _no_store({"ok": True, "state": "cancelling"})


@app.post("/api/projects/{pid}/analysis-runs/{rid}/regenerate-exports")
def regenerate_exports(pid: str, rid: str):
    """Rebuild the standing exports (visual GeoTIFF) for a completed run
    without rerunning terrain analysis/hydrology."""
    run = _require_run(pid, rid)
    if run.get("state") not in ("completed", "completed_with_warnings"):
        raise HTTPException(409, "Run has no completed results to export")
    out_dir = _run_output_dir(run)
    results = os.path.join(out_dir, "results.geojson")
    if not os.path.isfile(results):
        raise HTTPException(404, "Run results are unavailable")
    with open(results) as f:
        fc = json.load(f)
    from . import exports as exports_mod

    proj = db.get_project(pid)
    avail = exports_mod.generate_run_exports(
        out_dir, fc, aoi_wgs84=proj["aoi"] if proj else None)
    db.update_analysis_run(rid, exports_json=avail)
    return _no_store({"ok": True, "exports": _run_downloads(
        db.get_analysis_run(rid))})


# ---------------------------------------------------------------------------
# Run-scoped download products (original DTM, keylines, visual map, ZIP)


def _run_results_fc(run: dict) -> dict:
    path = os.path.join(_run_output_dir(run), "results.geojson")
    if not os.path.isfile(path):
        raise HTTPException(404, "No results for this analysis run")
    with open(path) as f:
        return json.load(f)


@app.get("/api/projects/{pid}/analysis-runs/{rid}/exports")
def run_exports_availability(pid: str, rid: str):
    run = _require_run(pid, rid)
    return _no_store(_run_downloads(run))


@app.get("/api/projects/{pid}/analysis-runs/{rid}/downloads/dtm")
def download_run_dtm(pid: str, rid: str):
    from . import exports as exports_mod

    run = _require_run(pid, rid)
    resolved = exports_mod.resolve_original_dtm(run, _run_output_dir(run))
    if resolved is None:
        raise HTTPException(404, "Original DTM is not available for this run")
    path, _ = resolved
    return FileResponse(
        path, media_type="image/tiff",
        headers={"Content-Disposition":
                 f'attachment; filename="keyline-{rid}-original-dtm.tif"'})


@app.get("/api/projects/{pid}/analysis-runs/{rid}/downloads/keylines.geojson")
def download_run_keylines_geojson(pid: str, rid: str):
    from .exports import ExportUnavailable, keylines_geojson

    run = _require_run(pid, rid)
    try:
        sub = keylines_geojson(_run_results_fc(run))
    except ExportUnavailable as exc:
        raise HTTPException(409, str(exc))
    return JSONResponse(sub, media_type="application/geo+json", headers={
        "Content-Disposition":
            f'attachment; filename="keyline-{rid}-keylines.geojson"',
        "Cache-Control": "no-store"})


@app.get("/api/projects/{pid}/analysis-runs/{rid}/downloads/keylines.kml")
def download_run_keylines_kml(pid: str, rid: str):
    from fastapi.responses import Response

    from .exports import ExportUnavailable, keylines_kml

    run = _require_run(pid, rid)
    proj = db.get_project(pid)
    try:
        kml_text = keylines_kml(_run_results_fc(run),
                                f"Keylines — {proj['name'] if proj else pid}")
    except ExportUnavailable as exc:
        raise HTTPException(409, str(exc))
    return Response(content=kml_text,
                    media_type="application/vnd.google-earth.kml+xml",
                    headers={"Content-Disposition":
                             f'attachment; filename="keyline-{rid}-keylines.kml"'})


@app.get("/api/projects/{pid}/analysis-runs/{rid}/downloads/keyline-design-map.tif")
def download_run_visual_map(pid: str, rid: str):
    run = _require_run(pid, rid)
    out_dir = _run_output_dir(run)
    path = os.path.join(out_dir, "keyline-design-map.tif")
    if not os.path.isfile(path):
        # regenerate on demand (no hydrology rerun) if results still exist
        from . import exports as exports_mod

        proj = db.get_project(pid)
        try:
            exports_mod.generate_run_exports(
                out_dir, _run_results_fc(run),
                aoi_wgs84=proj["aoi"] if proj else None)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, f"Could not build visual map: {exc}")
        if not os.path.isfile(path):
            raise HTTPException(404, "Visual map is not available for this run")
    return FileResponse(
        path, media_type="image/tiff",
        headers={"Content-Disposition":
                 f'attachment; filename="keyline-{rid}-design-map.tif"'})


@app.get("/api/projects/{pid}/analysis-runs/{rid}/downloads/design-package.zip")
def download_run_design_package(pid: str, rid: str):
    from . import exports as exports_mod

    run = _require_run(pid, rid)
    proj = db.get_project(pid)
    out_dir = _run_output_dir(run)
    fc = _run_results_fc(run)
    # ensure the visual map is present so the package is complete
    if not os.path.isfile(os.path.join(out_dir, "keyline-design-map.tif")):
        try:
            exports_mod.generate_run_exports(
                out_dir, fc, aoi_wgs84=proj["aoi"] if proj else None)
        except Exception:  # noqa: BLE001 — package still assembles without it
            pass
    ortho = os.path.join(project_dir(pid), "photogrammetry", "orthophoto.tif")
    zip_path = os.path.join(out_dir, "exports", "design-package.zip")
    try:
        exports_mod.build_design_package(
            zip_path, out_dir=out_dir, fc=fc, run=run, project=proj,
            original_dtm=exports_mod.resolve_original_dtm(run, out_dir),
            orthophoto_path=ortho if os.path.isfile(ortho) else None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"Could not build design package: {exc}")
    return FileResponse(
        zip_path, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="keyline-{rid}-design-package.zip"'})


# ---------------------------------------------------------------------------
# Specialized exports (keylines-only, GeoPackage, DXF)


def _load_results(pid: str) -> dict:
    path = os.path.join(_results_dir(pid), "results.geojson")
    if not os.path.exists(path):
        raise HTTPException(404, "No results yet — run analyze first")
    with open(path) as f:
        return json.load(f)


@app.get("/api/projects/{pid}/exports/availability")
def exports_availability(pid: str):
    from .exports import export_availability

    _require_project(pid)
    return export_availability(_load_results(pid))


@app.get("/api/projects/{pid}/exports/keylines.geojson")
def export_keylines_geojson(pid: str):
    from .exports import ExportUnavailable, keylines_geojson

    _require_project(pid)
    try:
        sub = keylines_geojson(_load_results(pid))
    except ExportUnavailable as exc:
        raise HTTPException(409, str(exc))
    return JSONResponse(sub, headers={
        "Content-Disposition":
            f'attachment; filename="keyline-{pid}-keylines.geojson"'})


@app.get("/api/projects/{pid}/exports/keylines.kml")
def export_keylines_kml(pid: str):
    from fastapi.responses import Response

    from .exports import ExportUnavailable, keylines_kml

    proj = _require_project(pid)
    try:
        kml_text = keylines_kml(_load_results(pid),
                                f"Keylines — {proj['name']}")
    except ExportUnavailable as exc:
        raise HTTPException(409, str(exc))
    return Response(content=kml_text,
                    media_type="application/vnd.google-earth.kml+xml",
                    headers={"Content-Disposition":
                             f'attachment; filename="keyline-{pid}-keylines.kml"'})


@app.get("/api/projects/{pid}/exports/terrain.gpkg")
def export_terrain_gpkg(pid: str):
    from .exports import ExportUnavailable, terrain_gpkg

    _require_project(pid)
    out = os.path.join(_results_dir(pid), "terrain.gpkg")
    try:
        terrain_gpkg(_load_results(pid), out)
    except ExportUnavailable as exc:
        raise HTTPException(409, str(exc))
    return FileResponse(out, media_type="application/geopackage+sqlite3",
                        filename=f"keyline-{pid}-terrain.gpkg")


@app.get("/api/projects/{pid}/exports/keylines.dxf")
def export_keylines_dxf(pid: str):
    from .exports import ExportUnavailable, keylines_dxf

    _require_project(pid)
    out = os.path.join(_results_dir(pid), "keylines.dxf")
    try:
        keylines_dxf(_load_results(pid), out)
    except ExportUnavailable as exc:
        raise HTTPException(409, str(exc))
    return FileResponse(out, media_type="application/dxf",
                        filename=f"keyline-{pid}-keylines.dxf")


# ---------------------------------------------------------------------------
# Boundary import (KML/KMZ/GeoJSON) + KML export


@app.post("/api/import-boundary")
async def import_boundary(file: UploadFile):
    from .kml_io import BoundaryError, parse_boundary

    data = await file.read()
    try:
        poly = parse_boundary(file.filename or "", data)
    except BoundaryError as exc:
        raise HTTPException(422, str(exc))
    return {"aoi": poly}


@app.get("/api/projects/{pid}/export.kml")
def export_kml(pid: str):
    from fastapi.responses import Response

    from .kml_io import results_to_kml

    proj = _require_project(pid)
    path = os.path.join(_results_dir(pid), "results.geojson")
    if not os.path.exists(path):
        raise HTTPException(404, "No results yet — run analyze first")
    with open(path) as f:
        fc = json.load(f)
    kml_text = results_to_kml(fc, proj["aoi"], f"Keyline Studio — {proj['name']}")
    return Response(
        content=kml_text,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="keyline-results.kml"'},
    )


# ---------------------------------------------------------------------------
# Georeferenced map scans (PNG/JPG/PDF)

MAPS_DIR = os.path.join(DATA_DIR, "maps")


def _map_dir(mid: str) -> str:
    d = os.path.join(MAPS_DIR, mid)
    if not os.path.isdir(d):
        raise HTTPException(404, "Map not found")
    return d


def _render_map_page(map_dir: str, page: int) -> dict:
    """Render page N of the stored original to map.png (PDF via pypdfium2 at
    ~180 DPI; images pass through)."""
    from PIL import Image

    meta_path = os.path.join(map_dir, "map.json")
    with open(meta_path) as f:
        meta = json.load(f)
    original = os.path.join(map_dir, meta["original"])
    if meta["original"].lower().endswith(".pdf"):
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(original)
        if not (1 <= page <= len(pdf)):
            raise HTTPException(422, f"Page {page} out of range (1..{len(pdf)})")
        bitmap = pdf[page - 1].render(scale=180 / 72)
        img = bitmap.to_pil()
    else:
        img = Image.open(original)
        img.load()
        page = 1
    img = img.convert("RGB")
    img.save(os.path.join(map_dir, "map.png"))
    meta.update({"width": img.width, "height": img.height, "page": page})
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    return meta


@app.post("/api/maps")
async def upload_map(file: UploadFile):
    import uuid

    name = (file.filename or "map").lower()
    ext = os.path.splitext(name)[1]
    if ext not in (".png", ".jpg", ".jpeg", ".pdf"):
        raise HTTPException(422, "Use a .png, .jpg or .pdf map file")
    mid = uuid.uuid4().hex[:12]
    map_dir = os.path.join(MAPS_DIR, mid)
    os.makedirs(map_dir, exist_ok=True)
    original = f"original{ext}"
    with open(os.path.join(map_dir, original), "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)

    page_count = 1
    if ext == ".pdf":
        import pypdfium2 as pdfium

        try:
            page_count = len(pdfium.PdfDocument(os.path.join(map_dir, original)))
        except Exception as exc:
            raise HTTPException(422, f"Could not read PDF: {exc}")
    with open(os.path.join(map_dir, "map.json"), "w") as f:
        json.dump({"original": original, "page_count": page_count}, f)
    try:
        meta = _render_map_page(map_dir, 1)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"Could not render the map image: {exc}")
    return {"map_id": mid, "width": meta["width"], "height": meta["height"],
            "page_count": page_count, "page": meta["page"]}


class PageIn(BaseModel):
    page: int


@app.post("/api/maps/{mid}/page")
def select_map_page(mid: str, body: PageIn):
    meta = _render_map_page(_map_dir(mid), body.page)
    return {"map_id": mid, "width": meta["width"], "height": meta["height"],
            "page_count": meta["page_count"], "page": meta["page"]}


@app.get("/api/maps/{mid}/image")
def map_image(mid: str):
    path = os.path.join(_map_dir(mid), "map.png")
    if not os.path.exists(path):
        raise HTTPException(404, "Map image not rendered")
    return FileResponse(path, media_type="image/png")


class GeorefIn(BaseModel):
    epsg: int
    points: list[dict]  # [{px, py, e, n}, ...]


@app.post("/api/maps/{mid}/georef")
def georef_map(mid: str, body: GeorefIn):
    from . import georef as georef_mod

    map_dir = _map_dir(mid)
    with open(os.path.join(map_dir, "map.json")) as f:
        meta = json.load(f)
    try:
        M, rms = georef_mod.fit(body.points)
        corners = georef_mod.image_corners_wgs84(
            M, meta["width"], meta["height"], body.epsg)
    except georef_mod.GeorefError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(422, f"Georeferencing failed: {exc}")
    result = {"corners": corners, "rms_m": round(rms, 2), "epsg": body.epsg,
              "points": body.points,
              "width": meta["width"], "height": meta["height"]}
    with open(os.path.join(map_dir, "georef.json"), "w") as f:
        json.dump(result, f)
    return result


@app.get("/api/maps/{mid}/georef")
def get_map_georef(mid: str):
    path = os.path.join(_map_dir(mid), "georef.json")
    if not os.path.exists(path):
        raise HTTPException(404, "Map not georeferenced yet")
    with open(path) as f:
        return JSONResponse(json.load(f))


class AttachMapIn(BaseModel):
    map_id: str


@app.post("/api/projects/{pid}/attach-map")
def attach_map(pid: str, body: AttachMapIn):
    """Persist the map<->project link so re-opening the project can restore
    the overlay (control points live in the map's georef.json)."""
    _require_project(pid)
    _map_dir(body.map_id)
    with open(os.path.join(project_dir(pid), "map_ref.json"), "w") as f:
        json.dump({"map_id": body.map_id}, f)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin: runtime provider-URL update (used by the self-healing tunnel script).
# Guarded by ADMIN_TOKEN; the candidate URL must answer as a live NodeODM
# before it is applied, so a typo or dead tunnel can never be persisted.


class ProviderUrlIn(BaseModel):
    url: str


@app.post("/api/admin/provider-url")
def set_provider_url(body: ProviderUrlIn, request: Request):
    import re
    import secrets as _secrets

    from . import config as cfg

    token = os.environ.get("ADMIN_TOKEN", "")
    supplied = request.headers.get("x-admin-token", "")
    if not token or not _secrets.compare_digest(supplied, token):
        raise HTTPException(403, "Admin token missing or invalid")
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?/?", body.url):
        raise HTTPException(422, "Not a plain http(s) origin URL")
    url = body.url.rstrip("/")

    from .photogrammetry.nodeodm import NodeOdmProvider

    health = NodeOdmProvider(url=url, token=cfg.nodeodm_token(),
                             timeout=20).health()
    if not health.ok:
        raise HTTPException(
            422, f"URL does not answer as a NodeODM node: {health.error}")

    path = cfg.provider_url_override_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"url": url, "updated_at": __import__("time").time()}, f)
    return {"ok": True, "url": url, "version": health.version,
            "engine": health.engine}


# ---------------------------------------------------------------------------
# Local-dev storage backend: accepts the "presigned" PUTs issued by
# app.storage.local. Only active when STORAGE_BACKEND=local.


@app.put("/api/local-uploads/{key:path}")
async def local_upload(key: str, request: "Request"):
    from . import config as cfg
    from .storage import LocalStorage, StorageError, get_storage

    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        raise HTTPException(404, "Local upload endpoint is disabled")
    body = await request.body()
    if len(body) > cfg.drone_max_file_bytes():
        raise HTTPException(413, "File exceeds the per-file limit")
    try:
        storage.put_bytes(key, body, request.headers.get("content-type",
                                                         "application/octet-stream"))
    except StorageError as exc:
        raise HTTPException(422, str(exc))
    return {"ok": True, "size": len(body)}


# ---------------------------------------------------------------------------
# Photogrammetry assets (orthophoto preview + original GeoTIFF downloads)


def _photogrammetry_file(pid: str, filename: str) -> str:
    path = os.path.join(project_dir(pid), "photogrammetry", filename)
    if not os.path.exists(path):
        raise HTTPException(404, f"{filename} not available for this project")
    return path


@app.get("/api/projects/{pid}/orthophoto")
def orthophoto_preview(pid: str):
    _require_project(pid)
    from .assets import ensure_orthophoto_preview

    tif = _photogrammetry_file(pid, "orthophoto.tif")
    preview, _ = ensure_orthophoto_preview(
        tif, os.path.join(project_dir(pid), "photogrammetry"))
    return FileResponse(preview, media_type="image/png")


@app.get("/api/projects/{pid}/orthophoto-bounds")
def orthophoto_bounds(pid: str):
    _require_project(pid)
    from .assets import ensure_orthophoto_preview

    tif = _photogrammetry_file(pid, "orthophoto.tif")
    _, bounds = ensure_orthophoto_preview(
        tif, os.path.join(project_dir(pid), "photogrammetry"))
    return JSONResponse(bounds)


@app.get("/api/projects/{pid}/assets/dtm")
def download_dtm(pid: str):
    _require_project(pid)
    return FileResponse(_photogrammetry_file(pid, "drone_dtm.tif"),
                        media_type="image/tiff",
                        filename=f"keyline-{pid}-dtm.tif")


@app.get("/api/projects/{pid}/assets/orthophoto")
def download_orthophoto(pid: str):
    _require_project(pid)
    return FileResponse(_photogrammetry_file(pid, "orthophoto.tif"),
                        media_type="image/tiff",
                        filename=f"keyline-{pid}-orthophoto.tif")


@app.get("/api/projects/{pid}/map")
def project_map(pid: str):
    _require_project(pid)
    ref = os.path.join(project_dir(pid), "map_ref.json")
    if not os.path.exists(ref):
        raise HTTPException(404, "No map attached")
    with open(ref) as f:
        mid = json.load(f)["map_id"]
    georef_path = os.path.join(_map_dir(mid), "georef.json")
    out = {"map_id": mid, "georef": None}
    if os.path.exists(georef_path):
        with open(georef_path) as f:
            out["georef"] = json.load(f)
    return out
