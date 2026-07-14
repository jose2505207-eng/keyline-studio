"""DTM quality assurance.

Photogrammetric DTMs built without ground control can carry severe global
tilt (the Caliterra survey fit an 81% grade plane across a visibly gentle
site). Production data is never silently detrended — that could erase real
terrain — instead this module measures the surface, compares its large-scale
trend against satellite elevation when available, and emits structured
issues. TERRAIN_QA_MODE decides whether severe issues block keyline
generation (strict) or merely watermark the output (warn).
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

# Issue codes (also used by other modules for a single vocabulary)
SUSPECT_GLOBAL_TILT = "SUSPECT_GLOBAL_TILT"
EXTREME_RELIEF_FOR_FOOTPRINT = "EXTREME_RELIEF_FOR_FOOTPRINT"
VERTICAL_REFERENCE_UNVERIFIED = "VERTICAL_REFERENCE_UNVERIFIED"
INSUFFICIENT_GROUND_CONTROL = "INSUFFICIENT_GROUND_CONTROL"
RASTER_VECTOR_BOUNDS_MISMATCH = "RASTER_VECTOR_BOUNDS_MISMATCH"
DUPLICATE_TERRAIN_GEOMETRY = "DUPLICATE_TERRAIN_GEOMETRY"
NO_VALID_KEYPOINT = "NO_VALID_KEYPOINT"

SEVERE = "error"
WARNING = "warning"

# Severe codes block keyline generation in strict mode.
SEVERE_CODES = {SUSPECT_GLOBAL_TILT, EXTREME_RELIEF_FOR_FOOTPRINT,
                RASTER_VECTOR_BOUNDS_MISMATCH}

WATERMARK = "Diagnostic result — terrain quality checks failed."


@dataclass
class QAIssue:
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity,
                "message": self.message}


@dataclass
class QAReport:
    metrics: dict = field(default_factory=dict)
    issues: list[QAIssue] = field(default_factory=list)
    mode: str = "warn"

    @property
    def severe(self) -> bool:
        return any(i.severity == SEVERE for i in self.issues)

    @property
    def passed(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {"metrics": self.metrics,
                "issues": [i.to_dict() for i in self.issues],
                "mode": self.mode, "passed": self.passed,
                "severe": self.severe}


def _fit_plane(z: np.ndarray, res_x: float, res_y: float,
               step: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares plane over valid cells (decimated). Returns
    (coefficients [gx, gy, z0] in m/m and m, residuals array)."""
    sub = z[::step, ::step]
    ys, xs = np.mgrid[0:z.shape[0]:step, 0:z.shape[1]:step]
    m = np.isfinite(sub)
    A = np.column_stack([xs[m] * res_x, ys[m] * res_y, np.ones(int(m.sum()))])
    coef, *_ = np.linalg.lstsq(A, sub[m], rcond=None)
    residuals = sub[m] - A @ coef
    return coef, residuals


def _local_slopes(z: np.ndarray, res_x: float, res_y: float,
                  max_px: int = 1024) -> np.ndarray:
    step = max(1, max(z.shape) // max_px)
    sub = z[::step, ::step]
    dy, dx = np.gradient(sub, res_y * step, res_x * step)
    s = np.hypot(dx, dy)
    return s[np.isfinite(s)]


def assess_dtm(dtm_path: str, *, aoi_coverage: float | None = None,
               gcp_supplied: bool = False, checkpoints_supplied: bool = False,
               satellite_surface=None,
               tilt_threshold_pct: float | None = None,
               relief_footprint_ratio: float | None = None,
               slope_thresholds_pct: tuple[float, ...] = (30.0, 60.0, 100.0),
               mode: str | None = None) -> QAReport:
    """Measure a drone DTM and emit structured quality issues.

    ``satellite_surface`` is optional: a callable returning (elevation_2d,
    res_x, res_y) resampled over the DTM footprint, used only to detect
    large-scale orientation errors — satellite resolution is never treated
    as drone precision.
    """
    import rasterio

    from . import config

    mode = mode or config.terrain_qa_mode()
    tilt_threshold_pct = (tilt_threshold_pct
                          if tilt_threshold_pct is not None
                          else config.qa_tilt_threshold_pct())
    relief_footprint_ratio = (relief_footprint_ratio
                              if relief_footprint_ratio is not None
                              else config.qa_relief_footprint_ratio())

    report = QAReport(mode=mode)
    with rasterio.open(dtm_path) as src:
        res_x, res_y = abs(src.res[0]), abs(src.res[1])
        width_m = src.width * res_x
        height_m = src.height * res_y
        a = src.read(1, masked=True)
    z = np.ma.filled(np.ma.masked_invalid(a), np.nan).astype("float64")
    valid = np.isfinite(z)
    valid_pct = 100.0 * valid.sum() / z.size
    v = z[valid]

    p1, p99 = float(np.percentile(v, 1)), float(np.percentile(v, 99))
    raw_relief = float(v.max() - v.min())
    robust_relief = p99 - p1

    coef, residuals = _fit_plane(z, res_x, res_y)
    plane_grad = float(np.hypot(coef[0], coef[1]))
    plane_slope_pct = 100.0 * plane_grad
    plane_slope_deg = math.degrees(math.atan(plane_grad))
    residual_relief = float(np.percentile(residuals, 99)
                            - np.percentile(residuals, 1))

    slopes = _local_slopes(z, res_x, res_y)
    slope_above = {f"pct_above_{int(t)}pct":
                   round(100.0 * float((slopes > t / 100.0).mean()), 2)
                   for t in slope_thresholds_pct}

    # nodata fragmentation: how many disjoint nodata regions inside the frame
    from scipy import ndimage

    hole_labels, hole_count = ndimage.label(~valid)
    # edge artifacts: does the outermost valid ring deviate hard from its
    # immediate interior?
    edge_score = 0.0
    if valid.any():
        interior = np.zeros_like(valid)
        interior[2:-2, 2:-2] = valid[2:-2, 2:-2]
        ring = valid & ~interior
        if ring.any() and interior.any():
            edge_score = float(abs(np.nanmedian(z[ring])
                                   - np.nanmedian(z[interior])))
    # spikes/pits: cells far from a median-filtered surface
    sub = z[::max(1, max(z.shape) // 512), ::max(1, max(z.shape) // 512)]
    med = ndimage.median_filter(np.nan_to_num(sub, nan=float(np.nanmedian(v))), 5)
    spike_mask = np.isfinite(sub) & (np.abs(sub - med) > 5.0)
    spike_pct = 100.0 * float(spike_mask.mean())

    report.metrics = {
        "width_m": round(width_m, 1), "height_m": round(height_m, 1),
        "resolution_m": [round(res_x, 4), round(res_y, 4)],
        "valid_pct": round(valid_pct, 1),
        "min_m": round(float(v.min()), 2), "max_m": round(float(v.max()), 2),
        "p1_m": round(p1, 2), "p99_m": round(p99, 2),
        "raw_relief_m": round(raw_relief, 2),
        "robust_relief_m": round(robust_relief, 2),
        "plane_coefficients": [round(float(c), 6) for c in coef],
        "plane_slope_pct": round(plane_slope_pct, 1),
        "plane_slope_deg": round(plane_slope_deg, 1),
        "residual_relief_m": round(residual_relief, 2),
        "median_local_slope_pct": round(100 * float(np.median(slopes)), 1),
        "p95_local_slope_pct": round(100 * float(np.percentile(slopes, 95)), 1),
        **slope_above,
        "nodata_regions": int(hole_count),
        "edge_artifact_m": round(edge_score, 2),
        "spike_pct": round(spike_pct, 2),
        "aoi_coverage": aoi_coverage,
        "gcp_supplied": gcp_supplied,
        "checkpoints_supplied": checkpoints_supplied,
    }

    # ---- issues ------------------------------------------------------------
    if plane_slope_pct > tilt_threshold_pct and \
            residual_relief < 0.5 * (robust_relief or 1.0):
        report.issues.append(QAIssue(
            SUSPECT_GLOBAL_TILT, SEVERE,
            f"The DTM is dominated by a {plane_slope_pct:.0f}% "
            f"({plane_slope_deg:.0f}°) global plane; residual terrain after "
            f"plane removal is only {residual_relief:.1f} m of "
            f"{robust_relief:.1f} m total relief. This is characteristic of "
            "a tilted reconstruction (insufficient ground control), not real "
            "topography."))

    footprint_diag = math.hypot(width_m, height_m)
    if robust_relief > relief_footprint_ratio * footprint_diag:
        report.issues.append(QAIssue(
            EXTREME_RELIEF_FOR_FOOTPRINT, SEVERE,
            f"{robust_relief:.0f} m of relief across a "
            f"{footprint_diag:.0f} m footprint "
            f"({100 * robust_relief / footprint_diag:.0f}% of the diagonal) "
            "is implausible for most survey sites."))

    if not gcp_supplied:
        report.issues.append(QAIssue(
            VERTICAL_REFERENCE_UNVERIFIED, WARNING,
            "No ground-control points were supplied; the vertical reference "
            "and absolute orientation of this DTM are unverified."))
        if report.severe:
            report.issues.append(QAIssue(
                INSUFFICIENT_GROUND_CONTROL, WARNING,
                "The reconstruction shows orientation problems and had no "
                "ground control — re-fly with GCPs or add a gcp_list.txt."))

    # ---- optional satellite cross-check ------------------------------------
    if satellite_surface is not None:
        try:
            sat_z, sat_rx, sat_ry = satellite_surface()
            sat_coef, _ = _fit_plane(np.asarray(sat_z, dtype="float64"),
                                     sat_rx, sat_ry, step=1)
            sat_grad = float(np.hypot(sat_coef[0], sat_coef[1]))
            grad_diff = abs(plane_grad - sat_grad)
            angle = None
            if plane_grad > 1e-6 and sat_grad > 1e-6:
                dot = (coef[0] * sat_coef[0] + coef[1] * sat_coef[1]) / (
                    plane_grad * sat_grad)
                angle = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
            report.metrics["satellite_plane_slope_pct"] = round(100 * sat_grad, 1)
            report.metrics["satellite_gradient_angle_diff_deg"] = (
                round(angle, 1) if angle is not None else None)
            # satellite DEM only detects LARGE-scale orientation errors
            if grad_diff > 0.15 and plane_grad > 2.0 * sat_grad + 0.05:
                if not any(i.code == SUSPECT_GLOBAL_TILT for i in report.issues):
                    report.issues.append(QAIssue(
                        SUSPECT_GLOBAL_TILT, SEVERE,
                        f"Drone surface slopes {100 * plane_grad:.0f}% while "
                        f"satellite elevation over the same footprint slopes "
                        f"{100 * sat_grad:.0f}% — gross large-scale tilt "
                        "disagreement."))
        except Exception as exc:  # noqa: BLE001 — cross-check is best-effort
            log.warning("satellite cross-check unavailable: %s", exc)
            report.metrics["satellite_check"] = f"unavailable: {exc}"

    return report


def satellite_surface_for(dtm_path: str):
    """Best-effort provider of satellite elevation resampled over the DTM
    footprint, for the large-scale tilt cross-check (needs network)."""
    def _fetch():
        import rasterio
        from pyproj import Transformer

        from . import dem_source

        with rasterio.open(dtm_path) as src:
            b = src.bounds
            tr = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
            lons, lats = zip(*[tr.transform(x, y) for x, y in
                               [(b.left, b.bottom), (b.right, b.top)]])
            crs = src.crs
        pad = 0.0005
        da = dem_source.fetch_glo30(min(lons) - pad, min(lats) - pad,
                                    max(lons) + pad, max(lats) + pad)
        sat = da.rio.reproject(crs)
        res = sat.rio.resolution()
        vals = np.asarray(sat.values, dtype="float64")
        vals[np.abs(vals) > 1e10] = np.nan
        return vals, abs(float(res[0])), abs(float(res[1]))

    return _fetch
