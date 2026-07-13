"""Copernicus GLO-30 DEM access over anonymous AWS S3.

GLO-30 notes:
- It is a *DSM* (digital surface model): heights include vegetation and
  buildings, not bare earth.
- Heights are referenced to the EGM2008 geoid (approximately mean sea level),
  not the WGS84 ellipsoid.
- Tiles are Cloud-Optimized GeoTIFFs; we only ever read the window covering
  the requested bbox, never whole tiles.

Bucket layout (anonymous access, no API key):
  s3://copernicus-dem-30m/Copernicus_DSM_COG_10_{NS}{lat:02d}_00_{EW}{lon:03d}_00_DEM/
      Copernicus_DSM_COG_10_{NS}{lat:02d}_00_{EW}{lon:03d}_00_DEM.tif
where lat/lon are the tile's SW corner in whole degrees.
"""

from __future__ import annotations

import math
import os

import numpy as np
import rasterio
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from rasterio.merge import merge as rio_merge
from rasterio.windows import from_bounds

BUCKET = "copernicus-dem-30m"


class DemSourceError(Exception):
    """Raised when elevation data cannot be obtained for an AOI."""


def tile_name(lat: float, lon: float) -> str:
    """Return the GLO-30 tile name whose 1x1 degree cell contains (lat, lon).

    The tile is named after its SW corner. Pure string/math logic — no network.
    """
    sw_lat = math.floor(lat)
    sw_lon = math.floor(lon)
    ns = "N" if sw_lat >= 0 else "S"
    ew = "E" if sw_lon >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_{ns}{abs(sw_lat):02d}_00_"
        f"{ew}{abs(sw_lon):03d}_00_DEM"
    )


def tiles_for_bbox(west: float, south: float, east: float, north: float) -> list[str]:
    """All tile names whose 1-degree cells intersect the WGS84 bbox."""
    names = []
    for lat in range(math.floor(south), math.floor(north) + 1):
        for lon in range(math.floor(west), math.floor(east) + 1):
            names.append(tile_name(lat + 0.5, lon + 0.5))
    return names


def tile_url(name: str) -> str:
    return f"/vsis3/{BUCKET}/{name}/{name}.tif"


def _open_env() -> rasterio.Env:
    # Anonymous access to the public bucket; skip sidecar probing for speed.
    return rasterio.Env(
        AWS_NO_SIGN_REQUEST="YES",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    )


def fetch_glo30(west: float, south: float, east: float, north: float) -> xr.DataArray:
    """Fetch the GLO-30 mosaic clipped to a WGS84 bbox as an in-memory DataArray.

    Reads only the COG window covering the bbox from each intersecting tile,
    mosaicking when the bbox spans tile boundaries. Raises DemSourceError when
    no tile exists (open ocean) or the returned window is all nodata.
    """
    os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
    names = tiles_for_bbox(west, south, east, north)
    datasets = []
    missing = []
    with _open_env():
        for name in names:
            try:
                datasets.append(rasterio.open(tile_url(name)))
            except rasterio.errors.RasterioIOError:
                missing.append(name)

        if not datasets:
            raise DemSourceError(
                "No GLO-30 tiles exist for this area — the AOI appears to be "
                f"entirely over ocean (missing tiles: {', '.join(missing)})."
            )

        try:
            if len(datasets) == 1:
                ds = datasets[0]
                window = from_bounds(west, south, east, north, ds.transform)
                data = ds.read(1, window=window, boundless=True, fill_value=np.nan)
                transform = ds.window_transform(window)
                nodata = ds.nodata
            else:
                data, transform = rio_merge(
                    datasets, bounds=(west, south, east, north), nodata=np.nan
                )
                data = data[0]
                nodata = np.nan
        finally:
            for ds in datasets:
                ds.close()

    data = data.astype("float32")
    if nodata is not None and not np.isnan(nodata):
        data[data == nodata] = np.nan
    if np.all(np.isnan(data)) or np.all(data == 0):
        # GLO-30 encodes ocean as 0; an all-zero window is open water.
        raise DemSourceError("The AOI contains no land elevation data (open ocean).")

    height, width = data.shape
    xs = transform.c + transform.a * (np.arange(width) + 0.5)
    ys = transform.f + transform.e * (np.arange(height) + 0.5)
    da = xr.DataArray(data, coords={"y": ys, "x": xs}, dims=("y", "x"), name="elevation")
    da.rio.write_crs("EPSG:4326", inplace=True)
    da.rio.write_transform(transform, inplace=True)
    da.rio.write_nodata(np.nan, inplace=True)
    return da
