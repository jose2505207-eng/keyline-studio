"""Satellite + drone DEM fusion.

Strategy (documented simplifications):
- The drone raster is reprojected onto the satellite-derived UTM grid (or a
  finer common grid chosen by the pipeline).
- Vertical co-registration is a single mean-offset removal over the overlap:
  we subtract mean(drone - satellite) from the drone patch. A full vertical
  datum transformation (drone ellipsoidal heights -> EGM2008 geoid heights)
  is a known future enhancement; the mean-offset approach absorbs the datum
  difference plus any GNSS base-station bias in one constant.
- Priority replacement with a feathered seam: drone values win inside the
  drone footprint, but across a ~90 m band (3 GLO-30 pixels) inside the
  footprint edge we linearly blend drone->satellite so no artificial cliff
  is created at the seam.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from scipy import ndimage

DEFAULT_FEATHER_M = 90.0  # ~3 GLO-30 pixels


def coregister_offset(satellite: np.ndarray, drone: np.ndarray) -> float:
    """Mean vertical offset (drone - satellite) over valid overlap cells."""
    valid = ~np.isnan(satellite) & ~np.isnan(drone)
    if not valid.any():
        raise ValueError("Drone DEM does not overlap satellite data")
    return float(np.mean(drone[valid] - satellite[valid]))


def feather_weights(footprint: np.ndarray, cell_size: float,
                    feather_m: float = DEFAULT_FEATHER_M) -> np.ndarray:
    """Blend weights in [0,1]: 1 = pure drone (footprint interior),
    ramping linearly to 0 at the footprint edge, 0 outside.

    Distance is measured from outside the footprint inward, so the ramp lives
    entirely *inside* the drone footprint (satellite data is untouched
    outside it).
    """
    dist_inside = ndimage.distance_transform_edt(footprint, sampling=cell_size)
    w = np.clip(dist_inside / max(feather_m, cell_size), 0.0, 1.0)
    w[~footprint] = 0.0
    return w


def fuse(satellite: np.ndarray, drone: np.ndarray, cell_size: float,
         feather_m: float = DEFAULT_FEATHER_M) -> tuple[np.ndarray, np.ndarray]:
    """Fuse a co-gridded drone patch into the satellite DEM.

    Both arrays share the same grid; drone is NaN outside its footprint.
    Returns (fused_dem, drone_weight) where drone_weight > 0.5 marks cells
    whose elevation is predominantly drone-derived (used for keypoint
    ``source`` attribution).
    """
    offset = coregister_offset(satellite, drone)
    drone_adj = drone - offset

    footprint = ~np.isnan(drone_adj)
    w = feather_weights(footprint, cell_size, feather_m)

    fused = satellite.copy()
    blend = footprint & ~np.isnan(satellite)
    fused[blend] = w[blend] * drone_adj[blend] + (1 - w[blend]) * satellite[blend]
    # Drone-only cells (satellite nodata under the footprint): take drone.
    drone_only = footprint & np.isnan(satellite)
    fused[drone_only] = drone_adj[drone_only]
    return fused, w


def reproject_drone_to_grid(drone_da: xr.DataArray, target: xr.DataArray) -> np.ndarray:
    """Reproject/resample a drone DataArray onto the target grid, NaN-filled."""
    from rasterio.enums import Resampling

    out = drone_da.rio.reproject_match(target, resampling=Resampling.bilinear)
    arr = out.values.astype("float32")
    if out.rio.nodata is not None and not np.isnan(out.rio.nodata):
        arr[arr == out.rio.nodata] = np.nan
    # reproject_match can emit huge fill values when nodata is unset
    arr[np.abs(arr) > 1e10] = np.nan
    return arr
