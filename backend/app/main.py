"""Keyline Studio API."""

from __future__ import annotations

import json
import os

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
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
    # Surveys stranded mid-flight by a crash/restart are resumed or returned
    # to a recoverable state (never falsely completed).
    try:
        from .jobs import reconcile_stale_surveys

        reconcile_stale_surveys()
    except Exception as exc:  # noqa: BLE001 — startup must not die on this
        import logging

        logging.getLogger(__name__).warning("survey reconciliation failed: %s", exc)


from . import surveys_api  # noqa: E402

app.include_router(surveys_api.router)
app.include_router(surveys_api.health_router)


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
    return {"ok": True, **info}


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
    path = os.path.join(project_dir(pid), "results.geojson")
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
