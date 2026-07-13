"""Terrain analysis primitives: stream vectorization, keypoints, keylines.

All functions here operate on numpy grids + an affine transform in a
*projected* CRS (meters). No I/O, no network — fully unit-testable.
"""

from __future__ import annotations

import numpy as np
from affine import Affine
from scipy.signal import savgol_filter
from shapely.geometry import LineString, Point
from skimage import measure
from skimage.morphology import skeletonize

# ---------------------------------------------------------------------------
# Skeleton -> polylines


def _neighbors(r: int, c: int, shape):
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            rr, cc = r + dr, c + dc
            if 0 <= rr < shape[0] and 0 <= cc < shape[1]:
                yield rr, cc


def skeleton_to_pixel_chains(skel: np.ndarray) -> list[list[tuple[int, int]]]:
    """Trace a binary skeleton into pixel chains split at junctions.

    Each chain runs between two graph nodes (endpoints or junction pixels);
    isolated loops are returned as closed chains.
    """
    pixels = set(zip(*np.nonzero(skel)))
    degree = {}
    for p in pixels:
        degree[p] = sum(1 for n in _neighbors(*p, skel.shape) if n in pixels)
    nodes = {p for p, d in degree.items() if d != 2}

    visited_edges: set[frozenset] = set()
    chains: list[list[tuple[int, int]]] = []

    def walk(start, first):
        chain = [start, first]
        prev, cur = start, first
        while cur not in nodes:
            nxt = [n for n in _neighbors(*cur, skel.shape)
                   if n in pixels and n != prev]
            # Avoid stepping back diagonally onto a pixel adjacent to prev
            nxt = [n for n in nxt if frozenset((cur, n)) not in visited_edges]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            visited_edges.add(frozenset((prev, cur)))
            chain.append(cur)
            if cur == start:  # closed loop
                break
        return chain

    for node in nodes:
        for n in _neighbors(*node, skel.shape):
            if n in pixels and frozenset((node, n)) not in visited_edges:
                visited_edges.add(frozenset((node, n)))
                chains.append(walk(node, n))

    # Pure loops with no junction pixel
    chained = {p for ch in chains for p in ch}
    for p in pixels - chained:
        if degree.get(p, 0) == 2:
            n = next(n for n in _neighbors(*p, skel.shape) if n in pixels)
            visited_edges.add(frozenset((p, n)))
            chains.append(walk(p, n))
            chained.update(chains[-1])

    return [c for c in chains if len(c) >= 2]


def chains_to_lines(chains, transform: Affine) -> list[LineString]:
    """Pixel chains (row, col) -> LineStrings in map coordinates (cell centers)."""
    lines = []
    for chain in chains:
        coords = [transform * (c + 0.5, r + 0.5) for r, c in chain]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    return lines


def extract_stream_lines(
    facc: np.ndarray,
    dem: np.ndarray,
    transform: Affine,
    threshold_frac: float = 0.01,
    min_length_m: float = 0.0,
) -> list[LineString]:
    """Threshold flow accumulation -> skeletonize -> vectorize.

    Each returned polyline is oriented downstream -> upstream using the DEM
    (first vertex is the lower end).
    """
    with np.errstate(invalid="ignore"):
        mask = facc > (np.nanmax(facc) * threshold_frac)
    mask &= ~np.isnan(facc)
    if not mask.any():
        return []
    skel = skeletonize(mask)
    chains = skeleton_to_pixel_chains(skel)
    lines = []
    for chain, line in zip(chains, chains_to_lines(chains, transform)):
        if line.length < min_length_m:
            continue
        z0 = dem[chain[0][0], chain[0][1]]
        z1 = dem[chain[-1][0], chain[-1][1]]
        if not np.isnan(z0) and not np.isnan(z1) and z1 < z0:
            line = LineString(list(line.coords)[::-1])
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Profiles and keypoints


def sample_profile(dem: np.ndarray, transform: Affine, line: LineString,
                   spacing: float) -> tuple[np.ndarray, np.ndarray, list[Point]]:
    """Sample elevations along a line at ~`spacing` meters.

    Returns (distances, elevations, points). NaN samples are dropped.
    """
    n = max(int(line.length / spacing) + 1, 2)
    dists = np.linspace(0, line.length, n)
    inv = ~transform
    ds, zs, pts = [], [], []
    for d in dists:
        p = line.interpolate(d)
        col, row = inv * (p.x, p.y)
        r, c = int(row), int(col)
        if 0 <= r < dem.shape[0] and 0 <= c < dem.shape[1]:
            z = dem[r, c]
            if not np.isnan(z):
                ds.append(d)
                zs.append(float(z))
                pts.append(p)
    return np.asarray(ds), np.asarray(zs), pts


def find_keypoint(
    dists: np.ndarray,
    elevs: np.ndarray,
    min_confidence: float = 0.3,
    smooth_window_frac: float = 0.15,
) -> tuple[int, float] | None:
    """Locate the keypoint on a downstream->upstream longitudinal profile.

    The keypoint is the slope break where the flatter lower valley meets the
    steeper upper valley — the point of maximum concavity of the profile,
    i.e. the extremum of d2z/ds2 (equivalently the most negative second
    derivative when traversing the profile downstream). Returns
    (sample_index, confidence) or None if no clear break exists.
    """
    n = len(elevs)
    if n < 9:
        return None
    window = max(5, int(n * smooth_window_frac) | 1)  # odd, >= 5
    window = min(window, n - 1 if (n - 1) % 2 else n - 2)
    if window < 5:
        return None
    smooth = savgol_filter(elevs, window_length=window, polyorder=3)

    ds = np.gradient(dists)
    d1 = np.gradient(smooth) / ds
    d2 = np.gradient(d1) / ds

    # Ignore profile ends where smoothing/derivatives are unreliable.
    edge = max(2, window // 2)
    core = d2[edge:-edge]
    if core.size < 3:
        return None
    idx = int(np.argmax(core)) + edge  # max concavity (see docstring)
    peak = d2[idx]

    noise = float(np.median(np.abs(core - np.median(core)))) * 1.4826  # robust sigma
    if noise <= 0:
        noise = float(np.std(core)) or 1e-9
    zscore = (peak - float(np.median(core))) / noise
    confidence = float(np.clip(zscore / 8.0, 0.0, 1.0))
    if confidence < min_confidence or peak <= 0:
        return None
    return idx, confidence


# ---------------------------------------------------------------------------
# Keylines (contour through a keypoint)


def contour_at(dem: np.ndarray, transform: Affine, elevation: float,
               near: Point, max_snap_m: float = 200.0) -> LineString | None:
    """Full connected contour component at `elevation` through/near `near`.

    Uses skimage.find_contours (array index space) and keeps whole contour
    components — never truncated pieces. At a keypoint's elevation several
    components can pass close by (e.g. a tiny closed loop around a bump right
    at the keypoint, plus the long landform-following contour a few meters
    away); among components passing within one pixel of the closest, the
    longest is chosen so the keyline traverses the surface instead of
    circling a noise bump. The result is lightly simplified to suppress
    pixel-staircase zigzag.
    """
    cell = abs(transform.a)
    filled = np.where(np.isnan(dem), np.nanmin(dem) - 1000.0, dem)
    contours = measure.find_contours(filled, level=elevation)
    candidates: list[tuple[float, LineString]] = []
    for cont in contours:
        if len(cont) < 2:
            continue
        coords = [transform * (c + 0.5, r + 0.5) for r, c in cont]
        line = LineString(coords)
        d = line.distance(near)
        if d <= max_snap_m:
            candidates.append((d, line))
    if not candidates:
        return None
    best_d = min(d for d, _ in candidates)
    best = max((line for d, line in candidates if d <= best_d + cell),
               key=lambda line: line.length)
    smoothed = best.simplify(cell * 0.5, preserve_topology=False)
    return smoothed if isinstance(smoothed, LineString) else best


def hillshade(dem: np.ndarray, cell_size: float, azimuth: float = 315.0,
              altitude: float = 45.0) -> np.ndarray:
    """Simple Lambertian hillshade, uint8, NaN -> 0."""
    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)
    dy, dx = np.gradient(np.where(np.isnan(dem), np.nanmean(dem), dem), cell_size)
    slope = np.pi / 2.0 - np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    shaded = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    out = np.clip(shaded * 255, 0, 255).astype(np.uint8)
    out[np.isnan(dem)] = 0
    return out
