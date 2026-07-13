"""Hydrology engine abstraction.

One interface, two interchangeable engines:

- WhiteboxEngine: wraps WhiteboxTools (the ``whitebox`` pip package downloads
  a native binary on first init). Preferred when the binary is available.
- PyshedsEngine: pure-Python/numba fallback via ``pysheds`` for environments
  where the Whitebox binary download fails.

Both engines take a conditioned-DEM-agnostic float32 elevation grid (NaN =
nodata) plus its affine transform and return ``(conditioned_dem,
flow_accumulation)`` where flow accumulation is in upstream cell counts and
conditioning means depressions have been filled/breached so flow routing
never terminates in pits.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Protocol

import numpy as np
from affine import Affine

log = logging.getLogger(__name__)


class HydrologyEngine(Protocol):
    name: str

    def flow_accumulation(
        self, dem: np.ndarray, transform: Affine
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (conditioned_dem, flow_accumulation_cells)."""
        ...


class PyshedsEngine:
    name = "pysheds"

    def flow_accumulation(self, dem, transform):
        # pysheds 0.5 still calls np.in1d, removed in numpy 2.x
        if not hasattr(np, "in1d"):
            np.in1d = np.isin  # type: ignore[attr-defined]
        from pysheds.grid import Grid
        from pysheds.view import Raster, ViewFinder

        dem = dem.astype(np.float64)
        nan_mask = np.isnan(dem)
        # pysheds handles nodata poorly with NaN in some ops; use a sentinel.
        work = np.where(nan_mask, -32768.0, dem)
        view = ViewFinder(affine=transform, shape=dem.shape, nodata=np.float64(-32768.0))
        raster = Raster(work, viewfinder=view)
        grid = Grid(viewfinder=view)

        pit_filled = grid.fill_pits(raster)
        flooded = grid.fill_depressions(pit_filled)
        conditioned = grid.resolve_flats(flooded)
        fdir = grid.flowdir(conditioned)
        acc = grid.accumulation(fdir)

        conditioned = np.asarray(conditioned, dtype=np.float32)
        conditioned[nan_mask] = np.nan
        acc = np.asarray(acc, dtype=np.float32)
        acc[nan_mask] = np.nan
        return conditioned, acc


class WhiteboxEngine:
    name = "whitebox"

    def __init__(self):
        from whitebox import WhiteboxTools

        self.wbt = WhiteboxTools()
        self.wbt.set_verbose_mode(False)
        # Raises if the binary is absent and cannot be downloaded.
        if not os.path.exists(self.wbt.exe_path):
            raise RuntimeError("WhiteboxTools binary not available")

    def flow_accumulation(self, dem, transform):
        import rasterio

        nan_mask = np.isnan(dem)
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "dem.tif")
            filled_p = os.path.join(tmp, "filled.tif")
            acc_p = os.path.join(tmp, "acc.tif")
            profile = {
                "driver": "GTiff",
                "height": dem.shape[0],
                "width": dem.shape[1],
                "count": 1,
                "dtype": "float32",
                "transform": transform,
                "nodata": -32768.0,
                "crs": "EPSG:32633",  # placeholder CRS; whitebox only needs geometry
            }
            with rasterio.open(src, "w", **profile) as dst:
                dst.write(np.where(nan_mask, -32768.0, dem).astype("float32"), 1)

            self.wbt.breach_depressions_least_cost(src, filled_p, dist=100, fill=True)
            self.wbt.d8_flow_accumulation(filled_p, acc_p, out_type="cells")

            with rasterio.open(filled_p) as f:
                conditioned = f.read(1).astype(np.float32)
            with rasterio.open(acc_p) as f:
                acc = f.read(1).astype(np.float32)

        conditioned[nan_mask] = np.nan
        acc[nan_mask] = np.nan
        return conditioned, acc


_engine: HydrologyEngine | None = None


def get_engine() -> HydrologyEngine:
    """Return the process-wide hydrology engine, preferring Whitebox.

    Set KEYLINE_HYDRO_ENGINE=pysheds|whitebox to force one.
    """
    global _engine
    if _engine is not None:
        return _engine

    forced = os.environ.get("KEYLINE_HYDRO_ENGINE")
    if forced == "pysheds":
        _engine = PyshedsEngine()
        return _engine
    try:
        _engine = WhiteboxEngine()
        log.info("Using WhiteboxTools hydrology engine")
    except Exception as exc:  # binary download failed, unsupported platform, ...
        log.warning("Whitebox unavailable (%s); falling back to pysheds", exc)
        _engine = PyshedsEngine()
    return _engine
