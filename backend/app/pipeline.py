"""End-to-end keyline analysis pipeline.

run_terrain_analysis() covers spec steps 4-9 on an in-memory grid (used
directly by the synthetic tests); run_pipeline() wraps it with data fetch,
reprojection, fusion, persistence, and job-progress logging.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import rioxarray  # noqa: F401
import xarray as xr
from affine import Affine
from pyproj import Transformer
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import transform as shp_transform

from . import dem_source, fusion, terrain
from .hydrology import get_engine

MAX_AOI_KM2 = 100.0
MAX_GRID_CELLS = 25_000_000  # memory guard when running at drone resolution
BBOX_PAD_FRAC = 0.10  # pad fetch bbox ~10% to avoid edge artifacts in routing


@dataclass
class Params:
    stream_threshold_frac: float = 0.01
    min_valley_length_m: float = 150.0
    min_keypoint_confidence: float = 0.3
    profile_spacing_px: float = 1.0


@dataclass
class TerrainResult:
    valleys: list[LineString] = field(default_factory=list)
    ridges: list[LineString] = field(default_factory=list)
    keypoints: list[dict] = field(default_factory=list)  # {point, elevation, confidence, valley_idx}
    keylines: list[dict] = field(default_factory=list)   # {line, keypoint_idx}
    conditioned_dem: np.ndarray | None = None


def run_terrain_analysis(
    dem: np.ndarray,
    transform: Affine,
    params: Params = Params(),
    progress: Callable[[str], None] = lambda s: None,
) -> TerrainResult:
    """Spec steps 4-9: conditioning -> flow -> valleys/ridges -> keypoints -> keylines."""
    engine = get_engine()
    cell = abs(transform.a)
    res = TerrainResult()

    progress(f"hydrological conditioning + flow routing ({engine.name})")
    conditioned, facc = engine.flow_accumulation(dem, transform)
    res.conditioned_dem = conditioned

    progress("extracting valleys")
    res.valleys = terrain.extract_stream_lines(
        facc, conditioned, transform,
        threshold_frac=params.stream_threshold_frac,
        min_length_m=cell * 2,
    )

    progress("extracting ridges")
    _, facc_inv = engine.flow_accumulation(
        np.where(np.isnan(dem), np.nan, -dem), transform
    )
    res.ridges = terrain.extract_stream_lines(
        facc_inv, conditioned, transform,
        threshold_frac=params.stream_threshold_frac,
        min_length_m=cell * 2,
    )

    progress("detecting keypoints")
    for vi, valley in enumerate(res.valleys):
        if valley.length < params.min_valley_length_m:
            continue
        dists, elevs, pts = terrain.sample_profile(
            dem, transform, valley, spacing=cell * params.profile_spacing_px
        )
        if len(elevs) < 9:
            continue
        hit = terrain.find_keypoint(dists, elevs, params.min_keypoint_confidence)
        if hit is None:
            continue
        idx, conf = hit
        res.keypoints.append({
            "point": pts[idx],
            "elevation": float(elevs[idx]),
            "confidence": conf,
            "valley_idx": vi,
        })

    progress("generating keylines")
    for ki, kp in enumerate(res.keypoints):
        line = terrain.contour_at(dem, transform, kp["elevation"], kp["point"])
        if line is not None:
            res.keylines.append({"line": line, "keypoint_idx": ki})

    return res


# ---------------------------------------------------------------------------
# Full pipeline


def _utm_grid_for_aoi(aoi_wgs84) -> tuple[str, object]:
    """Pick the local UTM CRS from the AOI centroid (works in both hemispheres)."""
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(geometry=[aoi_wgs84], crs="EPSG:4326")
    utm = gdf.estimate_utm_crs()
    return utm.to_string(), gdf.to_crs(utm).geometry.iloc[0]


def _write_outputs(project_dir: str, dem_da: xr.DataArray, result: TerrainResult,
                   aoi_utm, utm_crs: str, drone_weight: np.ndarray | None):
    """Clip vectors to AOI, reproject to WGS84, write GeoJSON + hillshade."""
    from PIL import Image

    to_wgs = Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True).transform
    dem = dem_da.values
    transform = dem_da.rio.transform()
    cell = abs(transform.a)
    inv = ~transform

    def clip_and_project(geom):
        clipped = geom.intersection(aoi_utm)
        if clipped.is_empty:
            return []
        parts = getattr(clipped, "geoms", [clipped])
        return [shp_transform(to_wgs, g) for g in parts if g.geom_type == "LineString"
                and g.length > 2 * cell]

    features = []
    for i, v in enumerate(result.valleys):
        for g in clip_and_project(v):
            features.append({"type": "Feature", "geometry": mapping(g),
                             "properties": {"kind": "valley", "id": f"v{i}"}})
    for i, r in enumerate(result.ridges):
        for g in clip_and_project(r):
            features.append({"type": "Feature", "geometry": mapping(g),
                             "properties": {"kind": "ridge", "id": f"r{i}"}})

    kp_ids = []
    for i, kp in enumerate(result.keypoints):
        p: Point = kp["point"]
        if not aoi_utm.contains(p):
            kp_ids.append(None)
            continue
        source = "satellite"
        if drone_weight is not None:
            col, row = inv * (p.x, p.y)
            r_, c_ = int(row), int(col)
            if (0 <= r_ < drone_weight.shape[0] and 0 <= c_ < drone_weight.shape[1]
                    and drone_weight[r_, c_] > 0.5):
                source = "drone"
        kid = f"k{i}"
        kp_ids.append(kid)
        wp = shp_transform(to_wgs, p)
        features.append({
            "type": "Feature", "geometry": mapping(wp),
            "properties": {"kind": "keypoint", "id": kid,
                           "elevation": round(kp["elevation"], 2),
                           "confidence": round(kp["confidence"], 3),
                           "source": source},
        })

    for kl in result.keylines:
        kid = kp_ids[kl["keypoint_idx"]]
        if kid is None:
            continue
        for g in clip_and_project(kl["line"]):
            features.append({"type": "Feature", "geometry": mapping(g),
                             "properties": {"kind": "keyline", "keypoint_id": kid,
                                            "id": f"l{kl['keypoint_idx']}"}})

    fc = {"type": "FeatureCollection", "features": features}
    with open(os.path.join(project_dir, "results.geojson"), "w") as f:
        json.dump(fc, f)

    # Hillshade PNG + bounds sidecar (WGS84 corner coords for MapLibre overlay)
    hs = terrain.hillshade(dem, cell)
    alpha = np.where(np.isnan(dem), 0, 255).astype(np.uint8)
    Image.merge("LA", (Image.fromarray(hs), Image.fromarray(alpha))).save(
        os.path.join(project_dir, "hillshade.png"))
    left, bottom, right, top = dem_da.rio.bounds()
    corners = [to_wgs(x, y) for x, y in
               [(left, top), (right, top), (right, bottom), (left, bottom)]]
    with open(os.path.join(project_dir, "hillshade_bounds.json"), "w") as f:
        json.dump({"coordinates": [list(c) for c in corners]}, f)
    # World file so the PNG is also usable in GIS tools
    with open(os.path.join(project_dir, "hillshade.pgw"), "w") as f:
        f.write(f"{transform.a}\n{transform.b}\n{transform.d}\n{transform.e}\n"
                f"{transform.c + transform.a / 2}\n{transform.f + transform.e / 2}\n")

    return fc


def run_pipeline(project_dir: str, aoi_geojson: dict,
                 drone_path: str | None = None,
                 progress: Callable[[str], None] = lambda s: None,
                 params: Params = Params()) -> dict:
    """Full pipeline for a project. Returns the result FeatureCollection."""
    aoi = shape(aoi_geojson)

    # --- guards
    utm_crs, aoi_utm = _utm_grid_for_aoi(aoi)
    area_km2 = aoi_utm.area / 1e6
    if area_km2 > MAX_AOI_KM2:
        raise ValueError(
            f"AOI is {area_km2:.1f} km² — the limit is {MAX_AOI_KM2:.0f} km². "
            "Draw a smaller area.")

    # --- fetch (padded bbox)
    progress("fetching Copernicus GLO-30 elevation")
    w, s, e, n = aoi.bounds
    pw, ph = (e - w) * BBOX_PAD_FRAC, (n - s) * BBOX_PAD_FRAC
    sat = dem_source.fetch_glo30(w - pw, s - ph, e + pw, n + ph)

    # --- reproject to local UTM
    progress(f"reprojecting to {utm_crs}")
    from rasterio.enums import Resampling
    sat_utm = sat.rio.reproject(utm_crs, resampling=Resampling.bilinear)
    sat_utm = sat_utm.where(np.abs(sat_utm) < 1e10)

    drone_weight = None
    dem_da = sat_utm
    if drone_path:
        progress("fusing drone DEM")
        drone = rioxarray.open_rasterio(drone_path, masked=True).squeeze("band", drop=True)
        drone_utm = drone.rio.reproject(utm_crs, resampling=Resampling.bilinear)
        drone_res = abs(drone_utm.rio.transform().a)
        sat_res = abs(sat_utm.rio.transform().a)
        # Single common grid at the finer resolution over the whole AOI,
        # capped so the grid stays in memory.
        target_res = max(drone_res, np.sqrt((sat_utm.sizes["x"] * sat_res) *
                                            (sat_utm.sizes["y"] * sat_res) /
                                            MAX_GRID_CELLS))
        if target_res < sat_res:
            base = sat_utm.rio.reproject(utm_crs, resolution=target_res,
                                         resampling=Resampling.bilinear)
            base = base.where(np.abs(base) < 1e10)
        else:
            base = sat_utm
        drone_arr = fusion.reproject_drone_to_grid(drone_utm, base)
        fused, drone_weight = fusion.fuse(
            base.values.astype("float32"), drone_arr,
            cell_size=abs(base.rio.transform().a))
        dem_da = base.copy(data=fused)

    dem = dem_da.values.astype("float32")
    if np.isnan(dem).all():
        raise ValueError("The AOI contains no elevation data.")
    dem_da = dem_da.copy(data=dem)

    # --- terrain analysis (steps 4-9)
    result = run_terrain_analysis(dem, dem_da.rio.transform(), params, progress)

    # --- persist DEM for keypoint-move recomputation, then outputs
    progress("writing outputs")
    os.makedirs(project_dir, exist_ok=True)
    dem_da.rio.write_nodata(np.nan, inplace=True)
    dem_da.rio.to_raster(os.path.join(project_dir, "dem_utm.tif"))
    if drone_weight is not None:
        np.save(os.path.join(project_dir, "drone_weight.npy"), drone_weight)
    with open(os.path.join(project_dir, "meta.json"), "w") as f:
        json.dump({"utm_crs": utm_crs}, f)

    return _write_outputs(project_dir, dem_da, result, aoi_utm, utm_crs, drone_weight)


def recompute_keyline(project_dir: str, aoi_geojson: dict, kid: str,
                      lng: float, lat: float) -> dict:
    """Recompute one keypoint's keyline after a drag (contour at the DEM
    elevation under the new position). Returns updated keypoint + keyline
    features and rewrites results.geojson in place."""
    with open(os.path.join(project_dir, "meta.json")) as f:
        utm_crs = json.load(f)["utm_crs"]
    dem_da = rioxarray.open_rasterio(
        os.path.join(project_dir, "dem_utm.tif"), masked=True
    ).squeeze("band", drop=True)
    dem = dem_da.values.astype("float32")
    transform = dem_da.rio.transform()

    to_utm = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True).transform
    to_wgs = Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True).transform
    x, y = to_utm(lng, lat)
    col, row = ~transform * (x, y)
    r, c = int(row), int(col)
    if not (0 <= r < dem.shape[0] and 0 <= c < dem.shape[1]) or np.isnan(dem[r, c]):
        raise ValueError("New keypoint position is outside the analyzed DEM.")
    elev = float(dem[r, c])

    aoi = shape(aoi_geojson)
    aoi_utm = shp_transform(to_utm, aoi)
    line = terrain.contour_at(dem, transform, elev, Point(x, y), max_snap_m=500.0)

    with open(os.path.join(project_dir, "results.geojson")) as f:
        fc = json.load(f)

    new_keyline_features = []
    if line is not None:
        clipped = line.intersection(aoi_utm)
        parts = getattr(clipped, "geoms", [clipped]) if not clipped.is_empty else []
        for g in parts:
            if g.geom_type == "LineString":
                new_keyline_features.append({
                    "type": "Feature",
                    "geometry": mapping(shp_transform(to_wgs, g)),
                    "properties": {"kind": "keyline", "keypoint_id": kid,
                                   "id": f"l-{kid}"},
                })

    kp_feature = None
    kept = []
    for feat in fc["features"]:
        p = feat["properties"]
        if p.get("kind") == "keyline" and p.get("keypoint_id") == kid:
            continue  # replaced
        if p.get("kind") == "keypoint" and p.get("id") == kid:
            feat["geometry"] = {"type": "Point", "coordinates": [lng, lat]}
            feat["properties"]["elevation"] = round(elev, 2)
            feat["properties"]["moved"] = True
            kp_feature = feat
        kept.append(feat)
    if kp_feature is None:
        raise KeyError(f"Keypoint {kid} not found")
    kept.extend(new_keyline_features)
    fc["features"] = kept
    with open(os.path.join(project_dir, "results.geojson"), "w") as f:
        json.dump(fc, f)

    return {"keypoint": kp_feature, "keylines": new_keyline_features}
