"""Validation + normalization of photogrammetry outputs.

Nothing here trusts the provider: the DTM must open, be georeferenced,
contain plausible elevations, and genuinely overlap the AOI before the
terrain pipeline is allowed to run against it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time

import numpy as np

log = logging.getLogger(__name__)


class AssetValidationError(ValueError):
    pass


def _decimated_read(src, band: int = 1, max_px: int = 1024):
    out_h = min(src.height, max_px)
    out_w = min(src.width, max_px)
    return src.read(band, out_shape=(out_h, out_w), masked=True)


def _wgs84_footprint(src) -> dict:
    from pyproj import Transformer

    b = src.bounds
    tr = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
    ring = [list(tr.transform(x, y)) for x, y in
            [(b.left, b.top), (b.right, b.top), (b.right, b.bottom),
             (b.left, b.bottom), (b.left, b.top)]]
    return {"type": "Polygon", "coordinates": [ring]}


def dtm_aoi_coverage(dtm_path: str, aoi_geojson: dict) -> float:
    """Fraction of the AOI covered by valid (non-nodata) DTM cells [0..1]."""
    import rasterio
    from pyproj import Transformer
    from rasterio import features
    from shapely.geometry import shape
    from shapely.ops import transform as shp_transform

    with rasterio.open(dtm_path) as src:
        to_dtm = Transformer.from_crs("EPSG:4326", src.crs,
                                      always_xy=True).transform
        aoi = shp_transform(to_dtm, shape(aoi_geojson))
        data = _decimated_read(src)
        scale_x = src.width / data.shape[1]
        scale_y = src.height / data.shape[0]
        transform = src.transform * src.transform.scale(scale_x, scale_y)
        aoi_mask = features.geometry_mask(
            [aoi.__geo_interface__], out_shape=data.shape,
            transform=transform, invert=True)
        cell_area = abs(transform.a * transform.e)
    if aoi.area <= 0:
        return 0.0
    valid = (~np.ma.getmaskarray(data)) & np.isfinite(np.ma.filled(data, np.nan))
    # Denominator is the full AOI area (not just the part inside the DTM
    # extent), so a DTM covering half the parcel reports ~0.5, not 1.0.
    covered_area = float((valid & aoi_mask).sum()) * cell_area
    return min(covered_area / aoi.area, 1.0)


def validate_dtm(dtm_path: str, aoi_geojson: dict,
                 min_overlap: float = 0.05) -> dict:
    """Full DTM validation; returns metadata for the manifest."""
    import rasterio

    if not dtm_path or not os.path.isfile(dtm_path):
        raise AssetValidationError(
            "The processing node did not produce a DTM (odm_dem/dtm.tif "
            "missing) — was the task created with dtm=true?")
    try:
        src = rasterio.open(dtm_path)
    except Exception as exc:
        raise AssetValidationError(f"DTM does not open as a raster: {exc}")
    with src:
        if src.count != 1:
            # some pipelines emit an alpha band; a single elevation band must
            # be safely identifiable, otherwise refuse
            if src.count == 2 and src.colorinterp and \
                    "alpha" in str(src.colorinterp[1]).lower():
                log.info("DTM has an alpha band; using band 1")
            else:
                raise AssetValidationError(
                    f"DTM has {src.count} bands; expected a single elevation "
                    "band")
        if src.crs is None:
            raise AssetValidationError("DTM has no CRS")
        if src.transform.is_identity:
            raise AssetValidationError("DTM has no georeferencing transform")
        data = _decimated_read(src)
        data = np.ma.masked_invalid(data)
        if data.mask.all():
            raise AssetValidationError("DTM contains only nodata")
        lo, hi = float(data.min()), float(data.max())
        if lo < -500.0 or hi > 9000.0:
            raise AssetValidationError(
                f"DTM elevations {lo:.0f}..{hi:.0f} m are implausible")
        meta = {
            "crs": str(src.crs),
            "resolution_m": [round(abs(src.res[0]), 4),
                             round(abs(src.res[1]), 4)],
            "width": src.width,
            "height": src.height,
            "elevation_range_m": [round(lo, 2), round(hi, 2)],
            "footprint_wgs84": _wgs84_footprint(src),
        }
    coverage = dtm_aoi_coverage(dtm_path, aoi_geojson)
    meta["aoi_coverage"] = round(coverage, 4)
    if coverage < min_overlap:
        raise AssetValidationError(
            f"The DTM covers only {coverage * 100:.1f}% of the AOI — it does "
            "not meaningfully overlap the drawn area. Check the survey "
            "location and georeferencing.")
    return meta


def validate_orthophoto(path: str) -> dict:
    import rasterio

    if not path or not os.path.isfile(path):
        raise AssetValidationError(
            "The processing node did not produce an orthophoto")
    try:
        src = rasterio.open(path)
    except Exception as exc:
        raise AssetValidationError(f"Orthophoto does not open: {exc}")
    with src:
        if src.crs is None:
            raise AssetValidationError("Orthophoto has no CRS")
        return {
            "crs": str(src.crs),
            "resolution_m": [round(abs(src.res[0]), 4),
                             round(abs(src.res[1]), 4)],
            "width": src.width,
            "height": src.height,
            "bands": src.count,
            "footprint_wgs84": _wgs84_footprint(src),
        }


def normalize_and_validate_assets(assets, out_dir: str, aoi_geojson: dict,
                                  survey: dict, provider_health) -> dict:
    """Copy provider outputs into the project layout and write manifest.json.

    Raises AssetValidationError with a user-safe message on any problem.
    """
    os.makedirs(out_dir, exist_ok=True)
    dtm_meta = validate_dtm(assets.dtm_path, aoi_geojson)
    dtm_dest = os.path.join(out_dir, "drone_dtm.tif")
    shutil.copyfile(assets.dtm_path, dtm_dest)

    ortho_meta = None
    ortho_dest = None
    if assets.orthophoto_path:
        ortho_meta = validate_orthophoto(assets.orthophoto_path)
        ortho_dest = os.path.join(out_dir, "orthophoto.tif")
        shutil.copyfile(assets.orthophoto_path, ortho_dest)
    else:
        log.warning("provider returned no orthophoto for survey %s",
                    survey.get("id"))

    manifest = {
        "survey_id": survey.get("id"),
        "project_id": survey.get("project_id"),
        "external_task_id": survey.get("external_task_id"),
        "provider": {
            "name": getattr(provider_health, "provider", "unknown"),
            "version": getattr(provider_health, "version", ""),
            "engine": getattr(provider_health, "engine", ""),
            "engine_version": getattr(provider_health, "engine_version", ""),
        },
        "image_count": survey.get("image_count"),
        "gcp_supplied": bool(survey.get("gcp_key")),
        "options": survey.get("options_json") or {},
        "original_paths": {
            "dtm": assets.dtm_path,
            "orthophoto": assets.orthophoto_path,
        },
        "assets": {
            "dtm": dtm_dest,
            "orthophoto": ortho_dest,
        },
        "dtm": dtm_meta,
        "orthophoto": ortho_meta,
        "started_at": survey.get("started_at"),
        "completed_at": time.time(),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def ensure_orthophoto_preview(tif_path: str, out_dir: str,
                              max_px: int = 2048) -> tuple[str, dict]:
    """Downsampled web preview (PNG, alpha preserved) + WGS84 corner quad.

    Built once and cached next to the GeoTIFF; the multi-gigabyte original
    never goes to MapLibre directly.
    """
    import rasterio
    from PIL import Image
    from rasterio.enums import Resampling

    preview_path = os.path.join(out_dir, "orthophoto_preview.png")
    bounds_path = os.path.join(out_dir, "orthophoto_bounds.json")
    if os.path.isfile(preview_path) and os.path.isfile(bounds_path) and \
            os.path.getmtime(preview_path) >= os.path.getmtime(tif_path):
        with open(bounds_path) as f:
            return preview_path, json.load(f)

    with rasterio.open(tif_path) as src:
        scale = min(1.0, max_px / max(src.width, src.height))
        out_w = max(int(src.width * scale), 1)
        out_h = max(int(src.height * scale), 1)
        count = min(src.count, 4)
        data = src.read(
            indexes=list(range(1, count + 1)),
            out_shape=(count, out_h, out_w),
            resampling=Resampling.bilinear,
        )
        if count >= 4:
            rgba = np.moveaxis(data[:4], 0, -1)
        elif count == 3:
            alpha = np.full((out_h, out_w), 255, dtype=data.dtype)
            nodata = src.nodata
            if nodata is not None:
                alpha[np.all(data == nodata, axis=0)] = 0
            rgba = np.dstack([np.moveaxis(data, 0, -1), alpha])
        else:  # single band: render as grayscale
            band = data[0]
            alpha = np.full((out_h, out_w), 255, dtype="uint8")
            if src.nodata is not None:
                alpha[band == src.nodata] = 0
            band = band.astype("float64")
            lo, hi = np.nanpercentile(band, [2, 98])
            band = np.clip((band - lo) / max(hi - lo, 1e-9) * 255, 0, 255)
            rgba = np.dstack([band, band, band, alpha]).astype("uint8")
        rgba = rgba.astype("uint8")
        bounds = {"coordinates": _wgs84_footprint(src)["coordinates"][0][:4]}

    Image.fromarray(rgba, "RGBA").save(preview_path)
    with open(bounds_path, "w") as f:
        json.dump(bounds, f)
    return preview_path, bounds
