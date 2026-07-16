"""End-to-end keyline analysis pipeline.

run_terrain_analysis() covers spec steps 4-9 on an in-memory grid (used
directly by the synthetic tests); run_pipeline() wraps it with data fetch,
reprojection, fusion, persistence, and job-progress logging.
"""

from __future__ import annotations

import json
import logging
import math
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

log = logging.getLogger(__name__)

MAX_AOI_KM2 = 100.0
MAX_GRID_CELLS = 25_000_000  # memory guard when running at drone resolution
BBOX_PAD_FRAC = 0.10  # pad fetch bbox ~10% to avoid edge artifacts in routing


@dataclass
class Params:
    min_drainage_area_m2: float = 5000.0   # min contributing area for a stream cell
    min_line_length_m: float = 100.0       # valley/ridge polylines shorter than this are noise
    min_valley_length_m: float = 150.0     # minimum valley length to search for a keypoint
    min_keypoint_confidence: float = 0.5   # weak slope breaks are dropped
    min_valley_relief_m: float = 2.0       # satellite keypoints need >= this profile relief
    profile_spacing_px: float = 1.0
    smooth_sigma_px: float = 1.5           # satellite DEM pre-smooth; 0 disables (advanced)
    relief_warn_m: float = 15.0            # satellite relief below this -> reliability warning
    relief_reject_m: float = 6.0           # satellite relief below this -> no keypoints/keylines
    min_aoi_px: int = 40                   # AOIs under ~40x40 satellite pixels -> warning
    contour_interval_m: float = 0.0        # 0 = auto (~12 lines over the relief)


# User-tunable subset (the "Advanced terrain parameters" UI); anything else
# in a request's params dict is ignored rather than trusted.
TUNABLE_PARAMS = {
    "min_drainage_area_m2", "min_line_length_m", "min_valley_length_m",
    "min_keypoint_confidence", "smooth_sigma_px", "contour_interval_m",
}


def params_from_dict(raw: dict | None) -> Params:
    kwargs = {}
    for key in TUNABLE_PARAMS:
        if raw and key in raw:
            try:
                kwargs[key] = float(raw[key])
            except (TypeError, ValueError):
                pass
    return Params(**kwargs)


def assess_terrain_quality(dem_values: np.ndarray, has_drone: bool,
                           params: Params = Params()) -> dict:
    """Honest data-quality check before analysis.

    GLO-30 has ~2-4 m vertical RMSE; when an AOI's total relief (p98 - p2)
    approaches that error, flow routing mostly routes noise. Returns the
    relief, a user-facing warning when results will be unreliable, and
    whether keypoints/keylines must be suppressed entirely (relief below
    ``relief_reject_m``). Drone-sourced grids are trusted as-is.
    """
    v = dem_values[np.isfinite(dem_values)]
    relief = float(np.percentile(v, 98) - np.percentile(v, 2)) if v.size else 0.0
    n_px = int(v.size)
    warning = None
    suppress = False
    if not has_drone:
        too_small = n_px < params.min_aoi_px ** 2
        if relief < params.relief_reject_m:
            suppress = True
        if relief < params.relief_warn_m or too_small:
            extra = (" The AOI also spans very few satellite pixels"
                     f" ({n_px}, ~{params.min_aoi_px}x{params.min_aoi_px} needed)."
                     if too_small else "")
            warning = (
                f"Terrain relief at this site ({relief:.1f} m) is close to the "
                "satellite DEM's vertical error (~4 m). Results are unreliable — "
                "upload a drone DTM for this parcel." + extra
            )
            if suppress:
                warning += (" Relief is below the 6 m minimum, so keypoints and "
                            "keylines were not generated (hillshade only).")
    return {"relief_m": round(relief, 1), "n_px": n_px, "warning": warning,
            "suppress": suppress}


@dataclass
class TerrainResult:
    valleys: list[LineString] = field(default_factory=list)
    ridges: list[LineString] = field(default_factory=list)
    keypoints: list[dict] = field(default_factory=list)  # {point, elevation, confidence, valley_idx}
    keylines: list[dict] = field(default_factory=list)   # {line, keypoint_idx}
    conditioned_dem: np.ndarray | None = None
    flow_accumulation: np.ndarray | None = None


def run_terrain_analysis(
    dem: np.ndarray,
    transform: Affine,
    params: Params = Params(),
    progress: Callable[[str], None] = lambda s: None,
    drone_weight: np.ndarray | None = None,
    reporter=None,
) -> TerrainResult:
    """Spec steps 4-9: conditioning -> flow -> valleys/ridges -> keypoints -> keylines."""
    from . import progress as prog

    def stage(name: str, msg: str) -> None:
        if reporter is not None:
            reporter.start_stage(name, msg)
        else:
            progress(msg)

    engine = get_engine()
    cell = abs(transform.a)
    res = TerrainResult()
    # Physically meaningful stream threshold: contributing area in m² -> cells.
    threshold_cells = max(params.min_drainage_area_m2 / (cell * cell), 2.0)

    stage(prog.CONDITIONING_DEM,
          f"hydrological conditioning + flow routing ({engine.name})")
    conditioned, facc = engine.flow_accumulation(dem, transform)
    res.conditioned_dem = conditioned
    res.flow_accumulation = facc

    stage(prog.CALCULATING_FLOW_ACCUMULATION,
          "computing flow accumulation + extracting valleys")
    stage(prog.EXTRACTING_VALLEYS, "extracting valleys")
    res.valleys = terrain.extract_stream_lines(
        facc, conditioned, transform,
        threshold_cells=threshold_cells,
        min_length_m=params.min_line_length_m,
    )

    stage(prog.EXTRACTING_RIDGES, "extracting ridges")
    conditioned_inv, facc_inv = engine.flow_accumulation(
        np.where(np.isnan(dem), np.nan, -dem), transform
    )
    # orient ridge lines by real elevation (negate the inverted-conditioned
    # surface) — previously the valley-conditioned surface leaked in here
    ridge_orient = np.where(np.isnan(conditioned_inv), np.nan, -conditioned_inv)
    res.ridges = terrain.extract_stream_lines(
        facc_inv, ridge_orient, transform,
        threshold_cells=threshold_cells,
        min_length_m=params.min_line_length_m,
    )

    def _drone_backed(pt) -> bool:
        if drone_weight is None:
            return False
        col, row = ~transform * (pt.x, pt.y)
        r, c = int(row), int(col)
        return (0 <= r < drone_weight.shape[0] and 0 <= c < drone_weight.shape[1]
                and drone_weight[r, c] > 0.5)

    stage(prog.DETECTING_KEYPOINTS, "detecting keypoints")
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
        relief = float(np.max(elevs) - np.min(elevs))
        # On satellite data, a valley whose whole profile spans less than the
        # DEM's vertical noise cannot support a credible slope break.
        if relief < params.min_valley_relief_m and not _drone_backed(pts[idx]):
            continue
        res.keypoints.append({
            "point": pts[idx],
            "elevation": float(elevs[idx]),
            "confidence": conf,
            "valley_idx": vi,
        })

    stage(prog.GENERATING_KEYLINES, "generating keylines")
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


def _write_outputs(out_dir: str, dem_da: xr.DataArray, result: TerrainResult,
                   ctx, drone_weight: np.ndarray | None,
                   extra_properties: dict | None = None,
                   notices: list[str] | None = None,
                   contour_interval_m: float = 0.0,
                   reporter=None):
    """Clip vectors to the AOI, validate them, reproject to WGS84 exactly
    once, gate them against the run's spatial context, and write atomically.

    All spatial facts come from the immutable AnalysisSpatialContext — never
    from module state or a previous job."""
    import io

    from PIL import Image

    from . import config as _config
    from . import spatial

    notices = list(notices or [])
    strict = _config.terrain_qa_mode() == "strict"
    to_wgs = ctx.to_wgs84()
    aoi_analysis = ctx.aoi_analysis
    dem = dem_da.values
    transform = dem_da.rio.transform()
    cell = abs(transform.a)
    inv = ~transform

    def clip_lines(geom):
        """Clip to the AOI in the analysis CRS; stays in analysis CRS."""
        clipped = geom.intersection(aoi_analysis)
        if clipped.is_empty:
            return []
        parts = getattr(clipped, "geoms", [clipped])
        return [g for g in parts if g.geom_type == "LineString"
                and g.length > 2 * cell and spatial.distinct_points(g) >= 2]

    # ---- build vectors in the ANALYSIS CRS ---------------------------------
    valley_lines = [g for v in result.valleys for g in clip_lines(v)]
    ridge_lines = [g for r in result.ridges for g in clip_lines(r)]

    # ---- contours (elevation isolines, kept muted in the UI) ---------------
    contour_lines: list[tuple[float, object]] = []
    with np.errstate(all="ignore"):
        v = dem[np.isfinite(dem)]
    if v.size:
        relief = float(np.percentile(v, 99) - np.percentile(v, 1))
        interval = float(contour_interval_m or 0.0)
        if interval <= 0:
            # auto: ~12 lines snapped to a friendly step
            raw = max(relief / 12.0, 0.1)
            for nice in (0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100):
                if raw <= nice:
                    interval = nice
                    break
            else:
                interval = 100.0
        lo = math.floor(float(np.percentile(v, 1)) / interval) * interval
        hi = float(np.percentile(v, 99))
        levels = []
        level = lo + interval
        while level < hi and len(levels) < 40:  # hard cap
            levels.append(level)
            level += interval
        from skimage import measure as _measure

        work = np.where(np.isnan(dem), float(np.nanmin(dem)) - 1000.0, dem)
        for lv in levels:
            try:
                conts = _measure.find_contours(work, lv)
            except ValueError:
                continue
            for cont in conts:
                if len(cont) < 4:
                    continue
                xs, ys = transform * (cont[:, 1] + 0.5, cont[:, 0] + 0.5)
                line = LineString(np.column_stack([xs, ys])).simplify(cell)
                for part in clip_lines(line):
                    contour_lines.append((lv, part))

    # ridge == valley geometry is a contradiction, not a landform
    dupe_notices = spatial.check_terrain_sets(valley_lines, ridge_lines,
                                              strict=strict)
    if dupe_notices:
        valley_keys = {spatial._coord_key(v) for v in valley_lines}
        ridge_lines = [r for r in ridge_lines
                       if spatial._coord_key(r) not in valley_keys]
        notices.extend(dupe_notices)

    # ---- serialize: one transform to WGS84, at the very end ----------------
    features = []
    for i, g in enumerate(valley_lines):
        features.append({"type": "Feature",
                         "geometry": mapping(shp_transform(to_wgs, g)),
                         "properties": {"kind": "valley", "id": f"v{i}"}})
    for i, g in enumerate(ridge_lines):
        features.append({"type": "Feature",
                         "geometry": mapping(shp_transform(to_wgs, g)),
                         "properties": {"kind": "ridge", "id": f"r{i}"}})

    kp_ids = []
    kp_count = 0
    for i, kp in enumerate(result.keypoints):
        p: Point = kp["point"]
        if not aoi_analysis.contains(p):
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
        kp_count += 1
        features.append({
            "type": "Feature",
            "geometry": mapping(shp_transform(to_wgs, p)),
            "properties": {"kind": "keypoint", "id": kid,
                           "elevation": round(kp["elevation"], 2),
                           "confidence": round(kp["confidence"], 3),
                           "source": source},
        })

    # slope grid for keyline attributes (computed once, only if needed)
    slope_grid = None
    if result.keylines:
        with np.errstate(all="ignore"):
            gy, gx = np.gradient(dem, cell)
            slope_grid = np.hypot(gx, gy).astype("float32")

    keyline_count = 0
    for kl in result.keylines:
        kid = kp_ids[kl["keypoint_idx"]]
        if kid is None:
            continue
        kp = result.keypoints[kl["keypoint_idx"]]
        for g in clip_lines(kl["line"]):
            keyline_count += 1
            # attributes useful in field-layout exports
            (x0, y0), (x1, y1) = g.coords[0], g.coords[-1]
            bearing = (math.degrees(math.atan2(x1 - x0, y1 - y0)) + 360) % 180
            avg_slope = None
            if slope_grid is not None:
                n = min(max(int(g.length / cell), 2), 200)
                pts = [g.interpolate(d) for d in
                       np.linspace(0, g.length, n)]
                cols_rows = [~transform * (p.x, p.y) for p in pts]
                samples = [slope_grid[int(r), int(c)] for c, r in cols_rows
                           if 0 <= int(r) < slope_grid.shape[0]
                           and 0 <= int(c) < slope_grid.shape[1]]
                finite = [s for s in samples if np.isfinite(s)]
                if finite:
                    avg_slope = round(100 * float(np.mean(finite)), 1)
            features.append({"type": "Feature",
                             "geometry": mapping(shp_transform(to_wgs, g)),
                             "properties": {
                                 "kind": "keyline",
                                 "keypoint_id": kid,
                                 "id": f"l{kl['keypoint_idx']}",
                                 "elevation": round(kp["elevation"], 2),
                                 "confidence": round(kp["confidence"], 3),
                                 "length_m": round(g.length, 1),
                                 "avg_slope_pct": avg_slope,
                                 "bearing_deg": round(bearing, 1),
                                 "analysis_run_id": ctx.analysis_run_id,
                             }})

    for i, (lv, g) in enumerate(contour_lines):
        features.append({"type": "Feature",
                         "geometry": mapping(shp_transform(to_wgs, g)),
                         "properties": {"kind": "contour", "id": f"c{i}",
                                        "elevation": round(lv, 2)}})

    counts = {"valleys": len(valley_lines), "ridges": len(ridge_lines),
              "keypoints": kp_count, "keylines": keyline_count,
              "contours": len(contour_lines)}
    log.info("terrain features run=%s: %s", ctx.analysis_run_id, counts)

    keypoint_reasons: list[str] = []
    if kp_count == 0:
        # honest semantics: not a software failure, but no keyline design —
        # explain the most likely cause instead of a bare zero
        notices.append("NO_VALID_KEYPOINT")
        props_in = extra_properties or {}
        if "KEYLINE_GENERATION_BLOCKED" in notices:
            keypoint_reasons.append(
                "Terrain-quality checks blocked keyline generation "
                "(strict mode).")
        if props_in.get("keylines_suppressed"):
            keypoint_reasons.append(
                "Terrain relief is below the satellite reliability floor.")
        if v.size and relief < 3.0:
            keypoint_reasons.append(
                f"Terrain is nearly flat ({relief:.1f} m of relief).")
        if v.size < 40 * 40:
            keypoint_reasons.append(
                "The AOI covers very few raster cells — enlarge it.")
        if not valley_lines:
            keypoint_reasons.append(
                "No drainage lines emerged — the minimum contributing "
                "area may be too large for this parcel.")
        if not keypoint_reasons:
            keypoint_reasons.append(
                "No clear valley transition (slope break) was detected on "
                "any drainage line; try lowering the keypoint confidence "
                "threshold in advanced parameters.")

    fc = {"type": "FeatureCollection", "features": features}
    props = dict(extra_properties or {})
    qa_props = props.get("qa") or {}
    status = "completed"
    if qa_props.get("severe") or props.get("watermark") or \
            [n for n in notices if n != "NO_VALID_KEYPOINT"] or \
            (kp_count == 0 and qa_props.get("issues")):
        status = "completed_with_warnings"
    w_, s_, e_, n_ = ctx.dem_bounds_wgs84
    props.update({
        "project_id": ctx.project_id,
        "survey_id": ctx.survey_id,
        "analysis_run_id": ctx.analysis_run_id,
        "analysis_crs": ctx.analysis_crs,
        "dem_bounds_wgs84": [round(v, 6) for v in ctx.dem_bounds_wgs84],
        "bbox_wgs84": [round(v, 6) for v in ctx.dem_bounds_wgs84],
        "center_wgs84": [round((w_ + e_) / 2, 6), round((s_ + n_) / 2, 6)],
        "status": status,
        "counts": counts,
        "notices": notices,
        "keypoint_reasons": keypoint_reasons,
    })
    fc["properties"] = props  # foreign member (RFC 7946 §6.1)

    # ---- spatial-integrity gate: never export impossible geography ---------
    if reporter is not None:
        from . import progress as _prog
        reporter.start_stage(_prog.VALIDATING_SPATIAL_RESULTS,
                             "validating spatial integrity of results")
    spatial.validate_fc_bounds(fc, ctx,
                               buffer_m=_config.result_bounds_buffer_m())

    os.makedirs(out_dir, exist_ok=True)
    spatial.atomic_write_json(os.path.join(out_dir, "results.geojson"), fc)

    # Hillshade PNG + bounds sidecar (WGS84 corner coords for MapLibre overlay)
    if reporter is not None:
        from . import progress as _prog
        reporter.start_stage(_prog.GENERATING_HILLSHADE, "generating hillshade")
    hs = terrain.hillshade(dem, cell)
    alpha = np.where(np.isnan(dem), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.merge("LA", (Image.fromarray(hs), Image.fromarray(alpha))).save(
        buf, format="PNG")
    spatial.atomic_write_bytes(os.path.join(out_dir, "hillshade.png"),
                               buf.getvalue())
    left, bottom, right, top = dem_da.rio.bounds()
    corners = [to_wgs(x, y) for x, y in
               [(left, top), (right, top), (right, bottom), (left, bottom)]]
    spatial.atomic_write_json(os.path.join(out_dir, "hillshade_bounds.json"),
                              {"coordinates": [list(c) for c in corners]})
    # World file so the PNG is also usable in GIS tools
    spatial.atomic_write_bytes(
        os.path.join(out_dir, "hillshade.pgw"),
        (f"{transform.a}\n{transform.b}\n{transform.d}\n{transform.e}\n"
         f"{transform.c + transform.a / 2}\n"
         f"{transform.f + transform.e / 2}\n").encode())

    return fc


class InsufficientCoverageError(ValueError):
    """The selected DTM does not cover enough of the AOI and the user did not
    opt in to satellite gap-filling. Never silently fall back to satellite."""

    code = "DTM_COVERAGE_INSUFFICIENT"

    def __init__(self, coverage: float, threshold: float):
        self.coverage = coverage
        self.threshold = threshold
        super().__init__(
            f"The selected DTM covers only {coverage * 100:.1f}% of the "
            f"analysis area (at least {threshold * 100:.0f}% is required for a "
            "DTM-only analysis). Draw the area within the DTM footprint, or "
            "enable 'fill gaps with satellite elevation' to complete the "
            "missing areas with Copernicus GLO-30.")


def select_dem_mode(drone_path: str | None, aoi_geojson: dict,
                    requested: str = "auto", *,
                    fill_missing_areas_with_satellite: bool = False
                    ) -> tuple[str, float | None]:
    """Pick satellite_only | drone_only | fused and report drone coverage.

    A DTM covering at least DRONE_ONLY_MIN_AOI_COVERAGE of the AOI is analyzed
    alone — Copernicus is never fetched to sit beneath a complete DTM. For a
    partially-covering DTM in ``auto`` mode, satellite fusion is used **only**
    when the caller explicitly set ``fill_missing_areas_with_satellite``;
    otherwise this raises :class:`InsufficientCoverageError` with the coverage
    percentage. Existing-DTM analysis must never silently invoke satellite.
    """
    if requested not in ("auto", "satellite_only", "drone_only", "fused"):
        raise ValueError(f"Unknown dem_mode {requested!r}")
    if not drone_path:
        if requested in ("drone_only", "fused"):
            raise ValueError(f"dem_mode={requested} requires a drone DTM")
        return "satellite_only", None
    from .assets import dtm_aoi_coverage

    coverage = dtm_aoi_coverage(drone_path, aoi_geojson)
    # Explicit modes are honoured verbatim (fused is itself an explicit opt-in
    # to satellite); only the default 'auto' path is coverage-driven.
    if requested == "satellite_only":
        return "satellite_only", coverage
    if requested == "drone_only":
        return "drone_only", coverage
    if requested == "fused":
        return "fused", coverage

    from . import config as _config

    threshold = _config.drone_only_min_aoi_coverage()
    if coverage >= threshold:
        return "drone_only", coverage
    # Partial coverage in auto mode — never fuse silently.
    if fill_missing_areas_with_satellite:
        return "fused", coverage
    raise InsufficientCoverageError(coverage, threshold)


def _prepare_drone_only_grid(drone_path: str, aoi_wgs, utm_crs: str,
                             progress: Callable[[str], None]):
    """Clip the drone DTM to the AOI (+ small routing buffer), reproject to
    the analysis CRS, and coarsen only if the memory guard demands it.
    No Gaussian smoothing: high-resolution drone detail is real signal."""
    from rasterio.enums import Resampling

    progress("preparing drone DTM (drone-only mode)")
    drone = rioxarray.open_rasterio(drone_path, masked=True).squeeze(
        "band", drop=True)

    to_drone = Transformer.from_crs("EPSG:4326", drone.rio.crs,
                                    always_xy=True).transform
    aoi_in_drone = shp_transform(to_drone, aoi_wgs)
    buffer = 30.0 if getattr(drone.rio.crs, "is_projected", True) else 30.0 / 111_320
    clipped = drone.rio.clip([aoi_in_drone.buffer(buffer).__geo_interface__],
                             all_touched=True, drop=True)

    drone_utm = clipped.rio.reproject(utm_crs, resampling=Resampling.bilinear,
                                      nodata=np.nan)
    native_res = abs(drone_utm.rio.transform().a)
    cells = drone_utm.sizes["x"] * drone_utm.sizes["y"]
    if cells > MAX_GRID_CELLS:
        target = native_res * float(np.sqrt(cells / MAX_GRID_CELLS))
        progress(f"coarsening drone DTM to {target:.2f} m (memory guard)")
        drone_utm = clipped.rio.reproject(utm_crs, resolution=target,
                                          resampling=Resampling.bilinear,
                                          nodata=np.nan)
    return drone_utm.where(np.abs(drone_utm) < 1e10)


def run_pipeline(project_dir: str, aoi_geojson: dict,
                 drone_path: str | None = None,
                 progress: Callable[[str], None] = lambda s: None,
                 params: Params = Params(),
                 dem_mode: str = "auto",
                 out_dir: str | None = None,
                 survey_id: str | None = None,
                 analysis_run_id: str | None = None,
                 gcp_supplied: bool = False,
                 satellite_qa: bool = False,
                 fill_missing_areas_with_satellite: bool = False,
                 reporter=None) -> dict:
    """Full pipeline for a project. Returns the result FeatureCollection.

    ``out_dir`` (defaults to ``project_dir`` for backward compatibility)
    receives the run's outputs; callers doing versioned analysis runs pass a
    per-run directory so no two runs can overwrite each other.

    ``reporter`` (a :class:`app.progress.ProgressReporter`) drives structured
    stage transitions when present; otherwise the plain ``progress`` string
    callback is used (the synthetic tests rely on the latter)."""
    from . import progress as prog

    out_dir = out_dir or project_dir
    project_id = os.path.basename(os.path.normpath(project_dir))
    aoi = shape(aoi_geojson)

    def stage(name: str, msg: str) -> None:
        if reporter is not None:
            reporter.start_stage(name, msg)
        else:
            progress(msg)

    # --- guards
    utm_crs, aoi_utm = _utm_grid_for_aoi(aoi)
    area_km2 = aoi_utm.area / 1e6
    if area_km2 > MAX_AOI_KM2:
        raise ValueError(
            f"AOI is {area_km2:.1f} km² — the limit is {MAX_AOI_KM2:.0f} km². "
            "Draw a smaller area.")

    if drone_path:
        stage(prog.COMPUTING_DRONE_COVERAGE, "computing drone DTM coverage")
    else:
        stage(prog.SELECTING_DEM_MODE, "selecting DEM mode")
    dem_mode, drone_coverage = select_dem_mode(
        drone_path, aoi_geojson, dem_mode,
        fill_missing_areas_with_satellite=fill_missing_areas_with_satellite)
    if reporter is not None:
        # preserve the user-selected provenance (terrain_source); only the
        # engine mode changes here
        reporter.set_mode(dem_mode)

    from rasterio.enums import Resampling

    if dem_mode == "drone_only":
        stage(prog.PREPARING_DRONE_DEM, "preparing drone DTM (drone-only mode)")
        dem_da = _prepare_drone_only_grid(drone_path, aoi, utm_crs, progress)
        dem = dem_da.values.astype("float32")
        if np.isnan(dem).all():
            raise ValueError("The drone DTM has no valid data over the AOI.")
        dem_da = dem_da.copy(data=dem)
        # every cell is drone-derived; terrain-quality metrics come from the
        # DTM itself (no satellite warning/suppression applies)
        drone_weight = np.where(np.isfinite(dem), 1.0, 0.0).astype("float32")
        try:
            aoi_clip = dem_da.rio.clip([aoi_utm.__geo_interface__],
                                       all_touched=True)
            clip_values = aoi_clip.values
        except Exception:
            clip_values = dem
        stage(prog.TERRAIN_QUALITY_CHECKS,
              "checking terrain relief and DTM quality")
        quality = assess_terrain_quality(
            np.asarray(clip_values, dtype="float32"), has_drone=True,
            params=params)
    else:
        def op(msg: str, pct: float | None = None) -> None:
            if reporter is not None:
                reporter.operation(msg, pct)
            else:
                progress(msg)

        # --- fetch (padded bbox)
        stage(prog.FETCHING_SATELLITE_DEM, "fetching Copernicus GLO-30 elevation")
        w, s, e, n = aoi.bounds
        pw, ph = (e - w) * BBOX_PAD_FRAC, (n - s) * BBOX_PAD_FRAC
        op("contacting Copernicus GLO-30 tile store")
        sat = dem_source.fetch_glo30(w - pw, s - ph, e + pw, n + ph)
        op("Copernicus GLO-30 window downloaded")

        # --- reproject to local UTM
        stage(prog.REPROJECTING_SATELLITE_DEM, f"reprojecting to {utm_crs}")
        op(f"reprojecting satellite DEM to {utm_crs}")
        sat_utm = sat.rio.reproject(utm_crs, resampling=Resampling.bilinear)
        sat_utm = sat_utm.where(np.abs(sat_utm) < 1e10)
        op("satellite DEM reprojected")

        # --- honest data-quality guard, on the raw (unsmoothed) satellite DEM
        stage(prog.TERRAIN_QUALITY_CHECKS,
              "checking terrain relief vs satellite vertical error")
        try:
            aoi_clip = sat_utm.rio.clip([aoi_utm.__geo_interface__],
                                        all_touched=True)
            clip_values = aoi_clip.values
        except Exception:  # degenerate AOIs — fall back to the padded grid
            clip_values = sat_utm.values
        quality = assess_terrain_quality(
            np.asarray(clip_values, dtype="float32"),
            has_drone=(dem_mode == "fused"), params=params)

        # --- satellite pre-smooth at native resolution (never the drone raster)
        if params.smooth_sigma_px > 0:
            progress("smoothing satellite DEM (noise suppression)")
            sat_utm = sat_utm.copy(
                data=terrain.presmooth_dem(
                    sat_utm.values.astype("float32"), params.smooth_sigma_px))

        drone_weight = None
        dem_da = sat_utm
        if dem_mode == "fused":
            stage(prog.FUSING_DEM, "fusing drone DEM with satellite base")
            op("loading + reprojecting drone DEM for fusion")
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
                op(f"resampling satellite base to {target_res:.2f} m")
                base = sat_utm.rio.reproject(utm_crs, resolution=target_res,
                                             resampling=Resampling.bilinear)
                base = base.where(np.abs(base) < 1e10)
            else:
                base = sat_utm
            op("resampling drone DEM onto the common grid")
            drone_arr = fusion.reproject_drone_to_grid(drone_utm, base)
            op("blending drone + satellite elevation")
            fused, drone_weight = fusion.fuse(
                base.values.astype("float32"), drone_arr,
                cell_size=abs(base.rio.transform().a))
            dem_da = base.copy(data=fused)
            op("fusion complete")

        dem = dem_da.values.astype("float32")
        if np.isnan(dem).all():
            raise ValueError("The AOI contains no elevation data.")
        dem_da = dem_da.copy(data=dem)

    # --- immutable spatial context: the single source of geographic truth
    # for everything downstream (built once, after the DEM is selected)
    from . import spatial as spatial_mod

    dem_crs = None
    if dem_mode != "satellite_only" and drone_path:
        import rasterio as _rio

        with _rio.open(drone_path) as _src:
            dem_crs = str(_src.crs)
    ctx = spatial_mod.build_spatial_context(
        project_id=project_id,
        survey_id=survey_id,
        analysis_run_id=analysis_run_id,
        dem_path=drone_path if dem_mode != "satellite_only" else None,
        dem_crs=dem_crs,
        analysis_crs=utm_crs,
        aoi_wgs84_geojson=aoi_geojson,
        dem_bounds_analysis=tuple(dem_da.rio.bounds()),
    )

    # --- DTM quality assurance (drone-derived surfaces only) -----------------
    from . import config as _config
    from . import terrain_quality

    def note(msg: str) -> None:
        if reporter is not None:
            reporter.heartbeat(msg)
        else:
            progress(msg)

    notices: list[str] = []
    qa_dict = None
    watermark = None
    qa_blocks_keylines = False
    if dem_mode in ("drone_only", "fused") and drone_path:
        note("running DTM quality assurance")
        sat_fn = (terrain_quality.satellite_surface_for(drone_path)
                  if satellite_qa else None)
        qa = terrain_quality.assess_dtm(
            drone_path, aoi_coverage=drone_coverage,
            gcp_supplied=gcp_supplied, satellite_surface=sat_fn)
        qa_dict = qa.to_dict()
        if qa.severe:
            if qa.mode == "strict":
                qa_blocks_keylines = True
                notices.append("KEYLINE_GENERATION_BLOCKED")
                if reporter is not None:
                    reporter.warning(
                        "TERRAIN_QA_SEVERE", "severe terrain-quality issues — "
                        "keyline generation blocked (strict mode)")
                note("severe terrain-quality issues — keyline generation "
                     "blocked (strict mode)")
            else:
                watermark = terrain_quality.WATERMARK
                if reporter is not None:
                    reporter.warning(
                        "TERRAIN_QA_SEVERE", "severe terrain-quality issues — "
                        "result will be watermarked as diagnostic")
                note("severe terrain-quality issues — result will be "
                     "watermarked as diagnostic")

    # --- terrain analysis (steps 4-9); suppressed entirely when the terrain
    # signal is below the satellite noise floor (hillshade still produced)
    if quality["suppress"]:
        note("relief below reliability floor — skipping vector analysis")
        result = TerrainResult()
    else:
        result = run_terrain_analysis(dem, dem_da.rio.transform(), params,
                                      progress, drone_weight=drone_weight,
                                      reporter=reporter)
    if qa_blocks_keylines:
        result.keypoints = []
        result.keylines = []

    # --- persist DEM for keypoint-move recomputation, then outputs
    note("writing derived rasters")
    os.makedirs(out_dir, exist_ok=True)
    dem_da.rio.write_nodata(np.nan, inplace=True)
    dem_da.rio.to_raster(os.path.join(out_dir, "dem_utm.tif"))
    if drone_weight is not None:
        np.save(os.path.join(out_dir, "drone_weight.npy"), drone_weight)
    spatial_mod.atomic_write_json(os.path.join(out_dir, "meta.json"),
                                  {"utm_crs": utm_crs})

    # persist derived rasters alongside the DEM for downstream use
    if result.flow_accumulation is not None:
        facc_da = dem_da.copy(
            data=result.flow_accumulation.astype("float32"))
        facc_da.rio.write_nodata(np.nan, inplace=True)
        facc_da.rio.to_raster(os.path.join(out_dir, "flow_accumulation.tif"),
                              compress="LZW")
    with np.errstate(all="ignore"):
        gy, gx = np.gradient(dem, abs(dem_da.rio.transform().a))
        slope_da = dem_da.copy(data=np.hypot(gx, gy).astype("float32"))
    slope_da.rio.write_nodata(np.nan, inplace=True)
    slope_da.rio.to_raster(os.path.join(out_dir, "slope.tif"),
                           compress="LZW")

    log.info("analysis run=%s project=%s dem_mode=%s analysis_crs=%s "
             "dem_crs=%s bounds_wgs84=%s", analysis_run_id, project_id,
             dem_mode, utm_crs, dem_crs, ctx.dem_bounds_wgs84)
    fc = _write_outputs(out_dir, dem_da, result, ctx, drone_weight,
                        reporter=reporter,
                        contour_interval_m=params.contour_interval_m,
                        extra_properties={
                            "warning": quality["warning"],
                            "relief_m": quality["relief_m"],
                            "keylines_suppressed": quality["suppress"],
                            "dem_mode": dem_mode,
                            "drone_coverage": (round(drone_coverage, 4)
                                               if drone_coverage is not None
                                               else None),
                            "dem_resolution_m": round(
                                abs(dem_da.rio.transform().a), 3),
                            "qa": qa_dict,
                            "qa_mode": _config.terrain_qa_mode(),
                            "watermark": watermark,
                        },
                        notices=notices)
    return fc


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
