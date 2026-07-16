"""Visual, georeferenced RGBA GeoTIFF for GIS review.

This is emphatically **not** the elevation DTM: it is a colourised hillshade
base with the analysis vectors (AOI, valleys, ridges, keylines, keypoints)
burned on top, so a planner can open ``keyline-design-map.tif`` in QGIS/ArcGIS
and see the candidate design in place. Elevation values are never written into
this file, and the untouched elevation raster is exported separately.

Spatial alignment is guaranteed by rendering onto the analysis DEM's own grid
(``dem_utm.tif``): identical CRS, transform, width, height and bounds. All
vectors are transformed exactly once from WGS84 into the DEM CRS and rasterised
there with Rasterio geometry rasterisation — never transformed twice.
"""

from __future__ import annotations

import json
import logging
import os

import numpy as np

log = logging.getLogger(__name__)

# RGB colours for burned vectors
_AOI_RGB = (255, 255, 0)       # yellow boundary
_VALLEY_RGB = (40, 110, 230)   # blue
_RIDGE_RGB = (190, 110, 40)    # brown/orange
_KEYLINE_RGB = (30, 220, 60)   # high-contrast green
_KEYPOINT_RGB = (255, 40, 40)  # red


class VisualExportError(RuntimeError):
    pass


def _hillshade_rgb(dem: np.ndarray, cell: float) -> tuple[np.ndarray, np.ndarray]:
    """Grayscale hillshade as an (H,W,3) uint8 array + a valid-data mask."""
    from . import terrain

    valid = np.isfinite(dem)
    hs = terrain.hillshade(dem, cell)  # uint8 grayscale, nan->mid
    rgb = np.repeat(hs[:, :, None], 3, axis=2).astype(np.uint8)
    return rgb, valid


def _to_dem_crs_geoms(fc: dict, dem_crs: str):
    """Yield (kind, shapely geom in DEM CRS) — one transform, WGS84 -> DEM."""
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform as shp_transform

    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True).transform
    for feat in fc.get("features", []):
        kind = (feat.get("properties") or {}).get("kind")
        if kind not in ("valley", "ridge", "keyline", "keypoint"):
            continue
        try:
            geom = shp_transform(to_dem, shape(feat["geometry"]))
        except Exception:  # noqa: BLE001 — skip an unprojectable stray geometry
            continue
        yield kind, geom


def _burn(rgb: np.ndarray, geoms, transform, out_shape, color,
          width_m: float) -> int:
    """Rasterise buffered geometries in the DEM CRS onto rgb. Returns the
    number of geometries that actually intersected the raster footprint."""
    from rasterio.features import rasterize

    shapes = []
    hit = 0
    for g in geoms:
        if g.is_empty:
            continue
        buffered = g.buffer(width_m)
        if buffered.is_empty:
            continue
        shapes.append((buffered, 1))
        hit += 1
    if not shapes:
        return 0
    mask = rasterize(shapes, out_shape=out_shape, transform=transform,
                     fill=0, default_value=1, all_touched=True).astype(bool)
    for i in range(3):
        band = rgb[:, :, i]
        band[mask] = color[i]
    return hit


def build_visual_geotiff(out_dir: str, dest_path: str, *,
                         aoi_wgs84: dict | None = None) -> dict:
    """Render ``keyline-design-map.tif`` from a completed run directory.

    Reads ``dem_utm.tif`` (the analysis DEM, source of truth for the grid) and
    ``results.geojson`` from ``out_dir``. Writes a tiled, DEFLATE-compressed
    RGBA uint8 GeoTIFF that is spatially identical to the DEM grid. Returns a
    small summary dict (crs, width, height, burned counts).
    """
    import rasterio
    from rasterio.transform import array_bounds

    dem_path = os.path.join(out_dir, "dem_utm.tif")
    results_path = os.path.join(out_dir, "results.geojson")
    if not os.path.isfile(dem_path):
        raise VisualExportError("analysis DEM (dem_utm.tif) missing")
    if not os.path.isfile(results_path):
        raise VisualExportError("results.geojson missing")

    with open(results_path) as f:
        fc = json.load(f)

    with rasterio.open(dem_path) as src:
        dem = src.read(1, masked=True).filled(np.nan).astype("float32")
        crs = src.crs
        transform = src.transform
        width, height = src.width, src.height
        cell = abs(transform.a)

    rgb, valid = _hillshade_rgb(dem, cell)
    out_shape = (height, width)

    # bucket vectors by kind (single WGS84 -> DEM transform pass)
    buckets: dict[str, list] = {"valley": [], "ridge": [], "keyline": [],
                                "keypoint": []}
    for kind, geom in _to_dem_crs_geoms(fc, str(crs)):
        buckets[kind].append(geom)

    burned = {}
    # draw order: terrain context first, keylines/keypoints on top
    if aoi_wgs84 is not None:
        from pyproj import Transformer
        from shapely.geometry import shape
        from shapely.ops import transform as shp_transform

        to_dem = Transformer.from_crs("EPSG:4326", str(crs),
                                      always_xy=True).transform
        aoi = shp_transform(to_dem, shape(aoi_wgs84))
        _burn(rgb, [aoi.exterior], transform, out_shape, _AOI_RGB, cell * 1.0)
    burned["valleys"] = _burn(rgb, buckets["valley"], transform, out_shape,
                              _VALLEY_RGB, cell * 1.0)
    burned["ridges"] = _burn(rgb, buckets["ridge"], transform, out_shape,
                             _RIDGE_RGB, cell * 1.0)
    burned["keylines"] = _burn(rgb, buckets["keyline"], transform, out_shape,
                               _KEYLINE_RGB, cell * 1.6)
    burned["keypoints"] = _burn(rgb, buckets["keypoint"], transform, out_shape,
                                _KEYPOINT_RGB, cell * 3.0)

    alpha = np.where(valid, 255, 0).astype(np.uint8)

    # tiled where dimensions allow; else striped (rasterio requires >= 16px)
    tiled = width >= 256 and height >= 256
    profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 4,
        "width": width, "height": height, "crs": crs, "transform": transform,
        "compress": "DEFLATE", "predictor": 2, "photometric": "RGB",
    }
    if tiled:
        profile.update({"tiled": True, "blockxsize": 256, "blockysize": 256})

    tmp = dest_path + ".tmp"
    with rasterio.open(tmp, "w", **profile) as dst:
        for i in range(3):
            dst.write(rgb[:, :, i], i + 1)
        dst.write(alpha, 4)
        dst.colorinterp = [
            rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
            rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha,
        ]
        if width >= 512 and height >= 512:
            dst.build_overviews([2, 4, 8], rasterio.enums.Resampling.average)
    os.replace(tmp, dest_path)

    b = array_bounds(height, width, transform)
    log.info("visual geotiff run_dir=%s crs=%s %dx%d burned=%s",
             out_dir, crs, width, height, burned)
    return {"crs": str(crs), "width": width, "height": height,
            "bounds": list(b), "burned": burned}
