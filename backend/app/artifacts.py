"""Durable artifact registry for generated outputs.

Every downloadable product of an analysis run gets a database record with
checksum, size, MIME type and geospatial metadata, registered only after the
file verifiably exists on disk. Download endpoints serve artifacts through
this registry — never by guessing filenames — and re-verify the file before
streaming, so a download button can never point at a nonexistent file.
"""

from __future__ import annotations

import hashlib
import logging
import os

from . import db

log = logging.getLogger(__name__)

MIME_TYPES = {
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".png": "image/png",
    ".geojson": "application/geo+json",
    ".json": "application/json",
    ".kml": "application/vnd.google-earth.kml+xml",
    ".gpkg": "application/geopackage+sqlite3",
    ".zip": "application/zip",
    ".dxf": "application/dxf",
}

# artifact_type -> (relative path in run dir, download filename, description)
RUN_PRODUCTS = {
    "results_geojson": ("results.geojson", "results.geojson",
                        "All analysis layers (GeoJSON, EPSG:4326)"),
    "processed_dtm": ("dem_utm.tif", "processed-dtm.tif",
                      "Analysis-ready DEM (clipped/reprojected GeoTIFF)"),
    "hillshade_png": ("hillshade.png", "hillshade.png",
                      "Hillshade map preview (PNG)"),
    "visual_geotiff": ("keyline-design-map.tif", "keyline-design-map.tif",
                       "Georeferenced visual design map (GeoTIFF)"),
    "slope_geotiff": ("slope.tif", "slope.tif", "Slope raster (GeoTIFF)"),
    "flow_accumulation_geotiff": (
        "flow_accumulation.tif", "flow-accumulation.tif",
        "Flow accumulation raster (GeoTIFF)"),
    "keylines_geojson": (os.path.join("exports", "keylines.geojson"),
                         "keylines.geojson",
                         "Candidate keylines + keypoints (GeoJSON)"),
    "keylines_kml": (os.path.join("exports", "keylines.kml"), "keylines.kml",
                     "Candidate keylines (KML, Google Earth)"),
    "terrain_gpkg": (os.path.join("exports", "terrain.gpkg"), "terrain.gpkg",
                     "All vector layers (GeoPackage)"),
    "design_package_zip": (os.path.join("exports", "design-package.zip"),
                           "design-package.zip",
                           "Complete design package (ZIP)"),
}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _raster_metadata(path: str) -> dict:
    """Cheap raster metadata for GeoTIFF artifacts; empty dict on failure —
    registration must never fail because metadata extraction did."""
    try:
        import numpy as np
        import rasterio

        with rasterio.open(path) as src:
            meta = {
                "crs": str(src.crs) if src.crs else None,
                "bounds": list(src.bounds),
                "resolution": [abs(src.res[0]), abs(src.res[1])],
                "width": src.width, "height": src.height,
                "band_count": src.count,
                "nodata": float(src.nodata) if src.nodata is not None else None,
            }
            if src.count == 1:
                out = (min(src.height, 256), min(src.width, 256))
                arr = np.ma.masked_invalid(
                    src.read(1, out_shape=out, masked=True))
                if not arr.mask.all():
                    meta["elevation_min"] = float(arr.min())
                    meta["elevation_max"] = float(arr.max())
            return meta
    except Exception as exc:  # noqa: BLE001
        log.debug("raster metadata skipped for %s: %s", path, exc)
        return {}


def register_file(path: str, *, project_id: str, run_id: str | None,
                  artifact_type: str, download_filename: str,
                  algorithm_version: str | None = None,
                  created_by: str | None = None,
                  metadata: dict | None = None) -> str | None:
    """Register one existing, nonempty file. Returns the artifact id, or
    None when the file is absent/empty (nothing is registered)."""
    if not os.path.isfile(path):
        return None
    size = os.path.getsize(path)
    if size == 0:
        log.warning("refusing to register empty artifact %s (%s)",
                    artifact_type, path)
        return None
    ext = os.path.splitext(path)[1].lower()
    raster = _raster_metadata(path) if ext in (".tif", ".tiff") else {}
    return db.upsert_artifact(
        project_id=project_id, run_id=run_id, artifact_type=artifact_type,
        stored_path=os.path.abspath(path),
        original_filename=download_filename,
        size_bytes=size, checksum_sha256=sha256_file(path),
        mime_type=MIME_TYPES.get(ext, "application/octet-stream"),
        crs=raster.get("crs"), bounds=raster.get("bounds"),
        resolution=raster.get("resolution"),
        width=raster.get("width"), height=raster.get("height"),
        band_count=raster.get("band_count"), nodata=raster.get("nodata"),
        elevation_min=raster.get("elevation_min"),
        elevation_max=raster.get("elevation_max"),
        algorithm_version=algorithm_version, created_by=created_by,
        metadata=metadata)


def register_run_outputs(run: dict, out_dir: str) -> dict[str, str]:
    """Register every standing product of a run that exists on disk.

    Idempotent: re-registration refreshes checksum/size for the same
    (run, type) key. Returns {artifact_type: artifact_id}."""
    registered: dict[str, str] = {}
    version = run.get("analysis_version")
    for artifact_type, (rel, download_name, description) in \
            RUN_PRODUCTS.items():
        aid = register_file(
            os.path.join(out_dir, rel), project_id=run["project_id"],
            run_id=run["id"], artifact_type=artifact_type,
            download_filename=download_name, algorithm_version=version,
            created_by="analysis-worker",
            metadata={"description": description})
        if aid:
            registered[artifact_type] = aid
    return registered


def verify_artifact(artifact: dict) -> tuple[bool, str | None]:
    """Availability check performed before every listing/download."""
    path = artifact.get("stored_path") or ""
    if not os.path.isfile(path):
        return False, "The file is missing on the server."
    size = os.path.getsize(path)
    if size == 0:
        return False, "The file is empty."
    if artifact.get("size_bytes") not in (None, size):
        return False, "The file changed since it was registered."
    return True, None


def artifact_out(artifact: dict) -> dict:
    """API shape: internal paths never leak."""
    available, reason = verify_artifact(artifact)
    return {
        "id": artifact["id"],
        "project_id": artifact["project_id"],
        "run_id": artifact.get("run_id"),
        "artifact_type": artifact["artifact_type"],
        "filename": artifact.get("original_filename"),
        "description": (artifact.get("metadata_json") or {}).get("description"),
        "mime_type": artifact.get("mime_type"),
        "size_bytes": artifact.get("size_bytes"),
        "checksum_sha256": artifact.get("checksum_sha256"),
        "crs": artifact.get("crs"),
        "bounds": artifact.get("bounds_json"),
        "resolution": artifact.get("resolution_json"),
        "algorithm_version": artifact.get("algorithm_version"),
        "created_at": artifact.get("created_at"),
        "created_by": artifact.get("created_by"),
        "available": available,
        "unavailable_reason": reason,
    }
