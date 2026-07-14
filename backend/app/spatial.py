"""Single source of spatial truth for a terrain-analysis run.

Root-cause background: an exported result once contained vectors 500 km from
the project's DTM because serialization trusted ambient state instead of an
explicit, immutable description of *this run's* geography. Everything spatial
now flows through one frozen AnalysisSpatialContext built exactly once after
the DEM source is selected, and every exported FeatureCollection must pass
the spatial-integrity gate against that context before it is written.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from typing import Iterable

from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

log = logging.getLogger(__name__)

WGS84 = "EPSG:4326"


class SpatialIntegrityError(ValueError):
    """Raised when exported geometry cannot belong to the active raster."""

    code = "RESULT_CRS_MISMATCH"


class TerrainIntegrityError(ValueError):
    """Raised when terrain vectors are internally inconsistent."""

    code = "DUPLICATE_TERRAIN_GEOMETRY"


@dataclass(frozen=True)
class AnalysisSpatialContext:
    """Immutable geography of one analysis run. Built once, passed explicitly;
    never read CRS/AOI from module globals or previous jobs."""

    project_id: str
    survey_id: str | None
    analysis_run_id: str | None
    dem_path: str | None          # None for satellite_only
    dem_crs: str | None           # CRS of the source DEM file
    analysis_crs: str             # local projected CRS all terrain math uses
    wgs84_crs: str
    aoi_wgs84_geojson: dict       # AOI exactly as stored on the project
    aoi_analysis_wkt: str         # AOI transformed once into analysis CRS
    dem_bounds_analysis: tuple[float, float, float, float]
    dem_bounds_wgs84: tuple[float, float, float, float]

    # -- derived helpers (recomputed on demand; the dataclass stays frozen) --
    @property
    def aoi_wgs84(self) -> BaseGeometry:
        return shape(self.aoi_wgs84_geojson)

    @property
    def aoi_analysis(self) -> BaseGeometry:
        from shapely import wkt

        return wkt.loads(self.aoi_analysis_wkt)

    def to_wgs84(self):
        """Transformer analysis CRS -> WGS84 (always_xy). Applied exactly
        once, at serialization time."""
        return Transformer.from_crs(self.analysis_crs, self.wgs84_crs,
                                    always_xy=True).transform

    def footprint_wgs84(self, buffer_m: float = 0.0) -> BaseGeometry:
        """Analysis-grid footprint in WGS84, optionally buffered (meters)."""
        poly = box(*self.dem_bounds_analysis)
        if buffer_m:
            poly = poly.buffer(buffer_m)
        return shp_transform(self.to_wgs84(), poly)


def build_spatial_context(
    *,
    project_id: str,
    survey_id: str | None,
    analysis_run_id: str | None,
    dem_path: str | None,
    dem_crs: str | None,
    analysis_crs: str,
    aoi_wgs84_geojson: dict,
    dem_bounds_analysis: tuple[float, float, float, float],
) -> AnalysisSpatialContext:
    to_analysis = Transformer.from_crs(WGS84, analysis_crs,
                                       always_xy=True).transform
    aoi_analysis = shp_transform(to_analysis, shape(aoi_wgs84_geojson))
    to_wgs = Transformer.from_crs(analysis_crs, WGS84, always_xy=True).transform
    fp_wgs = shp_transform(to_wgs, box(*dem_bounds_analysis))
    return AnalysisSpatialContext(
        project_id=project_id,
        survey_id=survey_id,
        analysis_run_id=analysis_run_id,
        dem_path=dem_path,
        dem_crs=str(dem_crs) if dem_crs else None,
        analysis_crs=str(analysis_crs),
        wgs84_crs=WGS84,
        aoi_wgs84_geojson=aoi_wgs84_geojson,
        aoi_analysis_wkt=aoi_analysis.wkt,
        dem_bounds_analysis=tuple(dem_bounds_analysis),
        dem_bounds_wgs84=tuple(fp_wgs.bounds),
    )


# ---------------------------------------------------------------------------
# Spatial-integrity gate


def _meters_to_degrees(buffer_m: float, at_lat: float) -> float:
    return buffer_m / (111_320.0 * max(math.cos(math.radians(at_lat)), 0.1))


def validate_fc_bounds(fc: dict, ctx: AnalysisSpatialContext,
                       buffer_m: float = 50.0) -> None:
    """Every exported geometry must intersect the active DTM/analysis-grid
    footprint (small buffer allowed) AND be on/near the active AOI. A result
    that fails is geographically impossible for this run and must never be
    exported — fail with RESULT_CRS_MISMATCH and both bounds."""
    feats = fc.get("features", [])
    if not feats:
        return
    lat = (ctx.dem_bounds_wgs84[1] + ctx.dem_bounds_wgs84[3]) / 2
    buf_deg = _meters_to_degrees(buffer_m, lat)
    footprint = ctx.footprint_wgs84().buffer(buf_deg)
    aoi_zone = ctx.aoi_wgs84.buffer(buf_deg)

    all_bounds = None
    for feat in feats:
        geom = shape(feat["geometry"])
        b = geom.bounds
        all_bounds = b if all_bounds is None else (
            min(all_bounds[0], b[0]), min(all_bounds[1], b[1]),
            max(all_bounds[2], b[2]), max(all_bounds[3], b[3]))
        if not geom.intersects(footprint) or not geom.intersects(aoi_zone):
            raise SpatialIntegrityError(
                "RESULT_CRS_MISMATCH: exported "
                f"{feat.get('properties', {}).get('kind', 'feature')} at "
                f"bounds {tuple(round(v, 5) for v in b)} does not lie on the "
                "active project's terrain. DTM footprint (WGS84): "
                f"{tuple(round(v, 5) for v in ctx.dem_bounds_wgs84)}; result "
                f"bounds: {tuple(round(v, 5) for v in all_bounds)}; project "
                f"{ctx.project_id}, run {ctx.analysis_run_id}, analysis CRS "
                f"{ctx.analysis_crs}.")


# ---------------------------------------------------------------------------
# Terrain-vector integrity (valley/ridge duplication, degenerate geometry)


def _coord_key(geom: BaseGeometry) -> tuple:
    return tuple((round(x, 9), round(y, 9)) for x, y in geom.coords)


def distinct_points(geom: BaseGeometry) -> int:
    return len({(round(x, 9), round(y, 9)) for x, y in geom.coords})


def check_terrain_sets(valleys: Iterable[BaseGeometry],
                       ridges: Iterable[BaseGeometry],
                       strict: bool) -> list[str]:
    """A ridge must never be byte-identical to a valley: the classifications
    contradict each other, which means the algorithm degenerated (tiny grid,
    symmetric accumulation) — never that the landform is both. Returns
    notices in warn mode; raises TerrainIntegrityError in strict mode."""
    valley_keys = {_coord_key(v) for v in valleys}
    dupes = [r for r in ridges if _coord_key(r) in valley_keys]
    if not dupes:
        return []
    msg = (f"DUPLICATE_TERRAIN_GEOMETRY: {len(dupes)} ridge geometrie(s) are "
           "identical to valley geometries — conflicting terrain "
           "classifications for the same line")
    if strict:
        raise TerrainIntegrityError(msg)
    return [msg]


# ---------------------------------------------------------------------------
# Atomic writes


def atomic_write_json(path: str, obj) -> None:
    """Write JSON to a temp file, validate it parses, rename into place."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        with open(tmp) as f:
            json.load(f)  # must parse back before it may replace the target
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
