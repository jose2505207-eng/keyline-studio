"""Keyline Studio API."""

from __future__ import annotations

import json
import os

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import db, pipeline
from .dem_source import DemSourceError

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
        raise HTTPException(404, "Project not found")
    return proj


@app.post("/api/projects")
def create_project(body: ProjectIn):
    geom = body.aoi.get("geometry", body.aoi)  # accept Feature or bare geometry
    if geom.get("type") != "Polygon":
        raise HTTPException(422, "aoi must be a GeoJSON Polygon")
    pid = db.create_project(body.name, geom)
    os.makedirs(project_dir(pid), exist_ok=True)
    return {"project_id": pid}


@app.post("/api/projects/{pid}/drone-dem")
async def upload_drone_dem(pid: str, file: UploadFile):
    _require_project(pid)
    import rasterio

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
                raise ValueError("raster has no CRS")
    except Exception as exc:
        os.remove(dest)
        raise HTTPException(422, f"Not a usable GeoTIFF DEM: {exc}")
    db.set_drone_path(pid, dest)
    return {"ok": True}


def _run_job(jid: str, pid: str):
    proj = db.get_project(pid)
    try:
        def progress(step: str):
            db.update_job(jid, f"running:{step}", log_line=step)

        db.update_job(jid, "running:starting", log_line="starting")
        pipeline.run_pipeline(
            project_dir(pid), proj["aoi"],
            drone_path=proj.get("drone_path"), progress=progress,
        )
        db.update_job(jid, "done", log_line="done")
    except (DemSourceError, ValueError) as exc:
        db.update_job(jid, f"error:{exc}", log_line=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface anything to the job record
        db.update_job(jid, f"error:internal error: {exc}", log_line=str(exc))


@app.post("/api/projects/{pid}/analyze")
def analyze(pid: str, background: BackgroundTasks):
    _require_project(pid)
    jid = db.create_job(pid)
    background.add_task(_run_job, jid, pid)
    return {"job_id": jid}


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
    path = os.path.join(project_dir(pid), "results.geojson")
    if not os.path.exists(path):
        raise HTTPException(404, "No results yet — run analyze first")
    with open(path) as f:
        return JSONResponse(json.load(f))


@app.get("/api/projects/{pid}/hillshade")
def hillshade(pid: str):
    _require_project(pid)
    path = os.path.join(project_dir(pid), "hillshade.png")
    bounds_path = os.path.join(project_dir(pid), "hillshade_bounds.json")
    if not os.path.exists(path):
        raise HTTPException(404, "No hillshade yet")
    with open(bounds_path) as f:
        bounds = json.load(f)
    return FileResponse(path, media_type="image/png",
                        headers={"X-Bounds": json.dumps(bounds)})


@app.get("/api/projects/{pid}/hillshade-bounds")
def hillshade_bounds(pid: str):
    _require_project(pid)
    bounds_path = os.path.join(project_dir(pid), "hillshade_bounds.json")
    if not os.path.exists(bounds_path):
        raise HTTPException(404, "No hillshade yet")
    with open(bounds_path) as f:
        return JSONResponse(json.load(f))


@app.post("/api/projects/{pid}/keypoints/{kid}/move")
def move_keypoint(pid: str, kid: str, body: MoveIn):
    proj = _require_project(pid)
    try:
        return pipeline.recompute_keyline(
            project_dir(pid), proj["aoi"], kid, body.lng, body.lat)
    except KeyError:
        raise HTTPException(404, f"Keypoint {kid} not found")
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc))
