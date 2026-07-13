"""End-to-end terrain analysis (spec steps 4-9) on a synthetic DEM.

The DEM is a V-shaped valley draining west with a known longitudinal slope
break (steep upper valley -> flat lower valley) at a known easting. Asserts:
- exactly one valley polyline is found,
- the detected keypoint lies within N pixels of the known break,
- the keyline is a contour within ±0.5 m of the keypoint elevation.
"""

import numpy as np
from affine import Affine

from app.pipeline import Params, run_terrain_analysis

CELL = 10.0
NX, NY = 200, 200
X0, Y0 = 500_000.0, 4_000_000.0  # fake UTM origin (top-left)
TRANSFORM = Affine(CELL, 0, X0, 0, -CELL, Y0)

BREAK_DIST = 1200.0   # meters from the west (downstream) edge
# D8 constraint: for flank water to converge onto the valley line as clean
# 45-degree trajectories (no parallel streams, no tributary capture at the
# break column), the diagonal descent must dominate both the straight-west
# and straight-cross descents: 0.414*CROSS < slope < 2.41*CROSS must hold for
# BOTH longitudinal slopes.
S_LOWER = 0.04        # flat lower-valley longitudinal slope
S_UPPER = 0.20        # steep upper-valley longitudinal slope
CROSS = 0.09          # valley cross slope (V flanks)


def synthetic_dem():
    cols = np.arange(NX) * CELL           # distance east of west edge
    rows = np.arange(NY)
    yc = NY // 2

    lon = np.where(cols <= BREAK_DIST,
                   S_LOWER * cols,
                   S_LOWER * BREAK_DIST + S_UPPER * (cols - BREAK_DIST))
    cross = CROSS * np.abs(rows - yc)[:, None] * CELL
    dem = (lon[None, :] + cross + 100.0).astype("float32")
    return dem


def test_synthetic_valley_keypoint_keyline():
    dem = synthetic_dem()
    result = run_terrain_analysis(dem, TRANSFORM, Params())

    # -- exactly one valley inside the AOI. The pipeline fetches a ~10%-padded
    # bbox and clips vectors to the AOI precisely to discard domain-boundary
    # flow artifacts; mirror that here with an interior box.
    from shapely.geometry import box
    aoi = box(X0 + 3 * CELL, Y0 - (NY - 3) * CELL, X0 + (NX - 3) * CELL, Y0 - 3 * CELL)
    long_valleys = [v.intersection(aoi) for v in result.valleys]
    long_valleys = [v for v in long_valleys if not v.is_empty and v.length >= 150.0]
    assert len(long_valleys) == 1, f"expected 1 valley, got {len(long_valleys)}"
    valley = long_valleys[0]

    # valley runs along the center row, oriented downstream (west) first
    assert valley.coords[0][0] < valley.coords[-1][0]

    # -- exactly one keypoint, near the known slope break
    assert len(result.keypoints) == 1
    kp = result.keypoints[0]
    expected_x = X0 + BREAK_DIST
    error_px = abs(kp["point"].x - expected_x) / CELL
    assert error_px <= 10, f"keypoint {error_px:.1f} px from known break"
    assert 0.3 <= kp["confidence"] <= 1.0

    # -- keyline is a contour at the keypoint elevation (±0.5 m)
    assert len(result.keylines) == 1
    line = result.keylines[0]["line"]
    from scipy.ndimage import map_coordinates
    inv = ~TRANSFORM
    for x, y in list(line.coords)[::5]:
        col, row = inv * (x, y)
        z = map_coordinates(dem, [[row - 0.5], [col - 0.5]], order=1)[0]
        assert abs(float(z) - kp["elevation"]) <= 0.5

    # keyline passes close to the keypoint itself
    assert line.distance(kp["point"]) <= 2 * CELL


def test_keyline_spans_surface():
    """Regression: the keyline must be the full contour component traversing
    the terrain, not a short fragment. On the tilted V-valley the contour at
    the keypoint elevation is a chevron running from the west edge to the
    slope break and back — well over 60% of the grid width in total length."""
    dem = synthetic_dem()
    result = run_terrain_analysis(dem, TRANSFORM, Params())
    assert len(result.keylines) == 1
    line = result.keylines[0]["line"]
    assert line.length >= 0.6 * NX * CELL, (
        f"keyline is a fragment: {line.length:.0f} m < "
        f"{0.6 * NX * CELL:.0f} m (60% of grid width)")


def test_ridges_found_on_flanks():
    dem = synthetic_dem()
    result = run_terrain_analysis(dem, TRANSFORM, Params())
    assert len(result.ridges) >= 1
    # ridges should sit away from the valley center row
    yc_map = Y0 - (NY // 2) * CELL
    for ridge in result.ridges:
        ys = [c[1] for c in ridge.coords]
        assert min(abs(y - yc_map) for y in ys) > 5 * CELL


def test_no_keypoint_on_uniform_slope():
    """A constant-slope valley has no slope break -> no keypoint emitted."""
    cols = np.arange(NX) * CELL
    rows = np.arange(NY)
    yc = NY // 2
    dem = (0.05 * cols[None, :]
           + CROSS * np.abs(rows - yc)[:, None] * CELL + 100.0).astype("float32")
    result = run_terrain_analysis(dem, TRANSFORM, Params())
    assert len(result.keypoints) == 0
