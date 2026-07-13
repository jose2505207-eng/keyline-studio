"""Image → map-CRS georeferencing from user-clicked control points.

The user clicks pixel positions on a scanned map and types the printed grid
coordinates (e.g. UTM eastings/northings). With 2 control points we fit a
reflected similarity transform (scale + rotation + translation; the
reflection accounts for image y growing downward while northing grows up);
with 3+ points a full affine via least squares. RMS residual is reported in
CRS units (meters for UTM) so the user can judge the fit.
"""

from __future__ import annotations

import numpy as np


class GeorefError(ValueError):
    pass


def solve_similarity(px: np.ndarray, py: np.ndarray,
                     e: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Reflected similarity: E = a*px + b*py + tx ; N = b*px - a*py + ty.

    Returns a 2x3 matrix M with [E, N]^T = M @ [px, py, 1]^T.
    """
    m = len(px)
    A = np.zeros((2 * m, 4))
    y = np.zeros(2 * m)
    A[0::2, 0] = px
    A[0::2, 1] = py
    A[0::2, 2] = 1.0
    y[0::2] = e
    A[1::2, 0] = -py
    A[1::2, 1] = px
    A[1::2, 3] = 1.0
    y[1::2] = n
    (a, b, tx, ty), *_ = np.linalg.lstsq(A, y, rcond=None)
    return np.array([[a, b, tx], [b, -a, ty]])


def solve_affine(px: np.ndarray, py: np.ndarray,
                 e: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Full affine via least squares (needs >= 3 non-collinear points)."""
    A = np.column_stack([px, py, np.ones(len(px))])
    ce, *_ = np.linalg.lstsq(A, e, rcond=None)
    cn, *_ = np.linalg.lstsq(A, n, rcond=None)
    return np.vstack([ce, cn])


def apply_transform(M: np.ndarray, px, py):
    px = np.asarray(px, float)
    py = np.asarray(py, float)
    e = M[0, 0] * px + M[0, 1] * py + M[0, 2]
    n = M[1, 0] * px + M[1, 1] * py + M[1, 2]
    return e, n


def fit(points: list[dict]) -> tuple[np.ndarray, float]:
    """Fit pixel->CRS from control points [{px, py, e, n}, ...].

    2 points -> similarity; 3+ -> affine. Returns (2x3 matrix, rms in CRS
    units).
    """
    if len(points) < 2:
        raise GeorefError("At least 2 control points are required")
    px = np.array([p["px"] for p in points], float)
    py = np.array([p["py"] for p in points], float)
    e = np.array([p["e"] for p in points], float)
    n = np.array([p["n"] for p in points], float)

    if len(points) == 2:
        M = solve_similarity(px, py, e, n)
    else:
        # Degenerate (collinear) point sets make the affine underdetermined;
        # fall back to similarity which stays well-posed.
        A = np.column_stack([px, py, np.ones(len(px))])
        if np.linalg.matrix_rank(A) < 3:
            M = solve_similarity(px, py, e, n)
        else:
            M = solve_affine(px, py, e, n)

    ee, nn = apply_transform(M, px, py)
    rms = float(np.sqrt(np.mean((ee - e) ** 2 + (nn - n) ** 2)))
    return M, rms


def image_corners_wgs84(M: np.ndarray, width: int, height: int,
                        epsg: int) -> list[list[float]]:
    """Image corner quad (UL, UR, LR, LL) in lon/lat for a MapLibre overlay."""
    from pyproj import Transformer

    tr = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    corners = []
    for cx, cy in [(0, 0), (width, 0), (width, height), (0, height)]:
        e, n = apply_transform(M, cx, cy)
        lon, lat = tr.transform(float(e), float(n))
        corners.append([lon, lat])
    return corners
