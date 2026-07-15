"""Managed DTM library.

DTMs enter the library three ways — browser upload, import from an allowed
server path, or automatic registration of survey-generated DTMs — and are
addressed everywhere else by a stable ``dtm_id`` that only the backend
resolves to a filesystem path. List responses never expose internal storage
paths.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import uuid

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import config, db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dtms", tags=["dtms"])

_TIFF_EXTENSIONS = (".tif", ".tiff")


def ensure_dtm_dir() -> str:
    d = config.dtm_storage_dir()
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Validation helpers


class DtmPathError(ValueError):
    pass


def resolve_allowed_path(path: str) -> str:
    """Resolve a user-supplied server path (symlinks and .. collapsed) and
    require it to stay inside the configured allowed roots."""
    if not path or not path.strip():
        raise DtmPathError("Empty path")
    real = os.path.realpath(path.strip())
    for root in config.dtm_allowed_external_roots():
        root_real = os.path.realpath(root)
        if real == root_real or real.startswith(root_real + os.sep):
            return real
    raise DtmPathError(
        "Path is outside the allowed server directories "
        f"({', '.join(config.dtm_allowed_external_roots()[:2])}, …). Ask the "
        "operator to place the file under an allowed root.")


def inspect_dtm_raster(path: str) -> dict:
    """Open the raster lazily (never loading full data) and return metadata.
    Raises DtmPathError with a user-safe message on any problem."""
    import numpy as np
    import rasterio

    if not os.path.exists(path):
        raise DtmPathError("File does not exist on the server")
    if os.path.isdir(path):
        raise DtmPathError("Path is a directory, not a GeoTIFF file")
    if not os.access(path, os.R_OK):
        raise DtmPathError("File is not readable by the server process")
    if not path.lower().endswith(_TIFF_EXTENSIONS):
        raise DtmPathError("Only .tif/.tiff GeoTIFF files are accepted")
    try:
        src = rasterio.open(path)
    except Exception as exc:
        raise DtmPathError(f"Not a readable raster: {exc}")
    with src:
        if src.count < 1:
            raise DtmPathError("Raster has no bands")
        if src.width <= 0 or src.height <= 0:
            raise DtmPathError("Raster has zero width or height")
        if src.crs is None:
            raise DtmPathError(
                "Raster has no CRS — export it georeferenced (a DTM without "
                "a coordinate system cannot be placed on the map)")
        # decimated read: sanity-check values without loading the raster
        out = (min(src.height, 256), min(src.width, 256))
        arr = src.read(1, out_shape=out, masked=True)
        arr = np.ma.masked_invalid(arr)
        if arr.mask.all():
            raise DtmPathError("Raster contains only nodata")
        lo, hi = float(arr.min()), float(arr.max())
        if lo < -500.0 or hi > 9000.0:
            raise DtmPathError(
                f"Elevations {lo:.0f}..{hi:.0f} m are outside -500..9000 m — "
                "is this really a terrain model?")
        # geographic footprint: valid-data outline when computable cheaply
        # (decimated mask), else the raster bounds — always in EPSG:4326
        from pyproj import Transformer
        from rasterio import features as rio_features
        from shapely.geometry import box as shp_box
        from shapely.geometry import mapping, shape as shp_shape
        from shapely.ops import transform as shp_transform, unary_union

        to_wgs = Transformer.from_crs(src.crs, "EPSG:4326",
                                      always_xy=True).transform
        bounds_poly = shp_box(*src.bounds)
        footprint = bounds_poly
        try:
            scale_x = src.width / arr.shape[1]
            scale_y = src.height / arr.shape[0]
            dec_transform = src.transform * src.transform.scale(scale_x,
                                                                scale_y)
            valid = (~np.ma.getmaskarray(arr)).astype("uint8")
            polys = [shp_shape(geom) for geom, val in
                     rio_features.shapes(valid, transform=dec_transform)
                     if val == 1]
            if polys:
                merged = unary_union(polys).simplify(
                    max(abs(dec_transform.a), abs(dec_transform.e)))
                if not merged.is_empty:
                    footprint = merged
        except Exception:  # noqa: BLE001 — bounds footprint is a fine fallback
            pass
        footprint_wgs = shp_transform(to_wgs, footprint)
        bbox_poly_wgs = shp_transform(to_wgs, bounds_poly)
        w_, s_, e_, n_ = bbox_poly_wgs.bounds
        centroid = footprint_wgs.centroid

        return {
            "crs": str(src.crs),
            "width": src.width,
            "height": src.height,
            "nodata": (float(src.nodata)
                       if src.nodata is not None else None),
            "size_bytes": os.path.getsize(path),
            "resolution_m": [round(abs(src.res[0]), 4),
                             round(abs(src.res[1]), 4)],
            "elevation_range_m": [round(lo, 1), round(hi, 1)],
            "bbox_wgs84": [round(v, 7) for v in (w_, s_, e_, n_)],
            "center_wgs84": [round(centroid.x, 7), round(centroid.y, 7)],
            "footprint_geojson": mapping(footprint_wgs),
            "valid_pct": round(100.0 * float((~np.ma.getmaskarray(arr)).mean()), 1),
        }


def _sanitize_name(name: str) -> str:
    base = os.path.basename(name or "dtm.tif")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return safe[:120] or "dtm.tif"


# ---------------------------------------------------------------------------
# Survey DTM auto-registration


def register_survey_dtms() -> None:
    """Idempotently register every survey-generated DTM in the library and
    keep their availability status honest (a survey may claim a DTM whose
    file has since vanished — those become status 'missing')."""
    for s in db.surveys_with_dtm():
        existing = db.find_dtm_by_survey(s["id"])
        available = bool(s["dtm_path"]) and os.path.isfile(s["dtm_path"])
        if existing is None:
            meta = {}
            if available:
                try:
                    meta = inspect_dtm_raster(s["dtm_path"])
                except DtmPathError as exc:
                    log.warning("survey %s DTM unreadable: %s", s["id"], exc)
                    available = False
            db.create_dtm(
                storage_path=s["dtm_path"],
                display_name=f"{s['project_name']} — survey DTM",
                original_filename="drone_dtm.tif",
                source_type="survey",
                size_bytes=meta.get("size_bytes"),
                checksum=None,
                crs=meta.get("crs"),
                width=meta.get("width"),
                height=meta.get("height"),
                nodata=meta.get("nodata"),
                survey_id=s["id"],
                project_id=s["project_id"],
                status="ready" if available else "missing",
                metadata=meta,
            )
        else:
            new_status = "ready" if available else "missing"
            if existing["status"] != new_status:
                db.update_dtm(existing["id"], status=new_status)


def refresh_dtm_status(dtm: dict) -> dict:
    """Cheap availability re-check before showing or using a record."""
    available = os.path.isfile(dtm["storage_path"])
    status = "ready" if available else "missing"
    if dtm["status"] in ("ready", "missing") and dtm["status"] != status:
        db.update_dtm(dtm["id"], status=status)
        dtm = {**dtm, "status": status}
    return dtm


def resolve_dtm_for_analysis(dtm_id: str) -> tuple[dict, str]:
    """dtm_id -> (record, verified path). Raises HTTPException on any state
    that must not be queued."""
    dtm = db.get_dtm(dtm_id)
    if dtm is None:
        raise HTTPException(404, f"Unknown DTM {dtm_id}")
    dtm = refresh_dtm_status(dtm)
    if dtm["status"] != "ready" or not os.path.isfile(dtm["storage_path"]):
        raise HTTPException(
            422, f"DTM '{dtm['display_name']}' is not available on the "
            "server (file missing). Re-upload it or pick another DTM.")
    if not os.access(dtm["storage_path"], os.R_OK):
        raise HTTPException(
            422, f"DTM '{dtm['display_name']}' exists but is not readable "
            "by the analysis worker.")
    return dtm, dtm["storage_path"]


# ---------------------------------------------------------------------------
# Schemas


class DtmOut(BaseModel):
    id: str
    display_name: str
    original_filename: str | None
    source_type: str
    status: str
    size_bytes: int | None
    created_at: float
    crs: str | None
    width: int | None
    height: int | None
    nodata: float | None
    survey_id: str | None
    project_id: str | None
    resolution_m: list[float] | None = None
    # geographic placement (EPSG:4326) so the frontend can fly to the DTM
    bbox_wgs84: list[float] | None = None
    center_wgs84: list[float] | None = None
    footprint_geojson: dict | None = None
    elevation_range_m: list[float] | None = None
    valid_pct: float | None = None


class DtmListOut(BaseModel):
    items: list[DtmOut]


class ValidatePathIn(BaseModel):
    path: str


class ValidatePathOut(BaseModel):
    valid: bool
    reason: str | None = None
    metadata: dict | None = None


class ImportPathIn(BaseModel):
    path: str
    copy_to_library: bool = True
    project_id: str | None = None


def _dtm_out(d: dict, include_footprint: bool = True) -> DtmOut:
    meta = d.get("metadata_json") or {}
    return DtmOut(
        id=d["id"], display_name=d["display_name"],
        original_filename=d.get("original_filename"),
        source_type=d["source_type"], status=d["status"],
        size_bytes=d.get("size_bytes"), created_at=d["created_at"],
        crs=d.get("crs"), width=d.get("width"), height=d.get("height"),
        nodata=d.get("nodata"), survey_id=d.get("survey_id"),
        project_id=d.get("project_id"),
        resolution_m=meta.get("resolution_m"),
        bbox_wgs84=meta.get("bbox_wgs84"),
        center_wgs84=meta.get("center_wgs84"),
        footprint_geojson=(meta.get("footprint_geojson")
                           if include_footprint else None),
        elevation_range_m=meta.get("elevation_range_m"),
        valid_pct=meta.get("valid_pct"),
    )


# ---------------------------------------------------------------------------
# Endpoints


@router.get("", response_model=DtmListOut)
def list_dtms():
    ensure_dtm_dir()
    register_survey_dtms()
    items = [refresh_dtm_status(d) for d in db.list_dtms()]
    # list stays light: bbox/center included, full footprint via detail
    return DtmListOut(items=[_dtm_out(d, include_footprint=False)
                             for d in items])


def _backfill_placement(d: dict) -> dict:
    """Records registered before geographic placement existed get their
    footprint computed on first detail access."""
    meta = d.get("metadata_json") or {}
    if meta.get("bbox_wgs84") or d["status"] != "ready":
        return d
    try:
        meta.update(inspect_dtm_raster(d["storage_path"]))
        db.update_dtm(d["id"], metadata_json=meta, crs=meta["crs"],
                      width=meta["width"], height=meta["height"])
        d = {**d, "metadata_json": meta}
    except DtmPathError as exc:
        log.warning("placement backfill failed for %s: %s", d["id"], exc)
    return d


@router.get("/{dtm_id}", response_model=DtmOut)
def get_dtm(dtm_id: str):
    d = db.get_dtm(dtm_id)
    if d is None:
        raise HTTPException(404, "DTM not found")
    d = refresh_dtm_status(d)
    d = _backfill_placement(d)
    log.info("dtm detail: id=%s crs=%s bbox_wgs84=%s status=%s",
             d["id"], d.get("crs"),
             (d.get("metadata_json") or {}).get("bbox_wgs84"), d["status"])
    return _dtm_out(d)


@router.post("/upload", response_model=DtmOut)
async def upload_dtm(file: UploadFile, project_id: str | None = None):
    original = _sanitize_name(file.filename or "dtm.tif")
    if not original.lower().endswith(_TIFF_EXTENSIONS):
        raise HTTPException(422, "Only .tif/.tiff GeoTIFF files are accepted")

    storage_dir = ensure_dtm_dir()
    limit = config.dtm_max_upload_mb() * 1024 * 1024
    md5 = hashlib.md5()
    size = 0
    # temp file already carries .tif so raster inspection accepts it; it is
    # renamed to its final collision-safe name only after validation passes
    fd, tmp_path = tempfile.mkstemp(dir=storage_dir, prefix="uploading-",
                                    suffix=".tif")
    final = None
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        413, f"File exceeds the {config.dtm_max_upload_mb()} "
                             "MB upload limit (DTM_MAX_UPLOAD_MB)")
                md5.update(chunk)
                out.write(chunk)
        try:
            meta = inspect_dtm_raster(tmp_path)
        except DtmPathError as exc:
            raise HTTPException(422, f"Not a usable GeoTIFF DTM: {exc}")
        final = os.path.join(storage_dir, f"dtm_{uuid.uuid4().hex[:12]}.tif")
        os.replace(tmp_path, final)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    did = db.create_dtm(
        storage_path=final, display_name=original,
        original_filename=original, source_type="upload",
        size_bytes=meta["size_bytes"], checksum=md5.hexdigest(),
        crs=meta["crs"], width=meta["width"], height=meta["height"],
        nodata=meta["nodata"], project_id=project_id, metadata=meta)
    return _dtm_out(db.get_dtm(did))


@router.post("/validate-path", response_model=ValidatePathOut)
def validate_path(body: ValidatePathIn):
    try:
        real = resolve_allowed_path(body.path)
        meta = inspect_dtm_raster(real)
    except DtmPathError as exc:
        return ValidatePathOut(valid=False, reason=str(exc))
    return ValidatePathOut(valid=True, metadata={
        "filename": os.path.basename(real), **meta})


@router.post("/import-path", response_model=DtmOut)
def import_path(body: ImportPathIn):
    import shutil

    try:
        real = resolve_allowed_path(body.path)
        meta = inspect_dtm_raster(real)
    except DtmPathError as exc:
        raise HTTPException(422, str(exc))

    display = os.path.basename(real)
    if body.copy_to_library:
        storage_dir = ensure_dtm_dir()
        final = os.path.join(storage_dir, f"dtm_{uuid.uuid4().hex[:12]}.tif")
        shutil.copyfile(real, final)
        source_type = "imported_path"
        storage_path = final
    else:
        # used in place: must stay reachable by API and worker (shared mount)
        existing = db.find_dtm_by_path(real)
        if existing:
            return _dtm_out(refresh_dtm_status(existing))
        source_type = "external_path"
        storage_path = real

    md5 = hashlib.md5()
    with open(storage_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)

    did = db.create_dtm(
        storage_path=storage_path, display_name=display,
        original_filename=display, source_type=source_type,
        size_bytes=meta["size_bytes"], checksum=md5.hexdigest(),
        crs=meta["crs"], width=meta["width"], height=meta["height"],
        nodata=meta["nodata"], project_id=body.project_id, metadata=meta)
    return _dtm_out(db.get_dtm(did))
