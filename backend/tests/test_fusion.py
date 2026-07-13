"""Fusion: vertical co-registration + feathered seam."""

import numpy as np
import pytest

from app.fusion import coregister_offset, feather_weights, fuse

CELL = 30.0
FEATHER = 90.0


def _grids():
    """Satellite plane + drone patch (rows 30-70, cols 30-70) with +5 m offset
    and a zero-mean east-west tilt disagreement of ±3 m across the patch."""
    y, x = np.mgrid[0:100, 0:100]
    sat = (0.05 * x * CELL + 0.02 * y * CELL).astype("float32")

    drone = np.full_like(sat, np.nan)
    r0, r1, c0, c1 = 30, 70, 30, 70
    cols = np.arange(c0, c1)
    tilt = 3.0 * (cols - cols.mean()) / (cols - cols.mean()).max()  # ±3 m, mean 0
    drone[r0:r1, c0:c1] = sat[r0:r1, c0:c1] + 5.0 + tilt[None, :]
    return sat, drone, (r0, r1, c0, c1), tilt


def test_coregistration_removes_known_offset():
    sat, drone, _, _ = _grids()
    assert coregister_offset(sat, drone) == pytest.approx(5.0, abs=1e-4)

    # With a pure constant offset, fusion must reproduce the satellite surface.
    pure = np.full_like(sat, np.nan)
    pure[30:70, 30:70] = sat[30:70, 30:70] + 5.0
    fused, _ = fuse(sat, pure, CELL, FEATHER)
    assert np.allclose(fused, sat, atol=1e-3)


def test_no_overlap_raises():
    sat = np.zeros((10, 10), dtype="float32")
    drone = np.full_like(sat, np.nan)
    with pytest.raises(ValueError):
        coregister_offset(sat, drone)


def test_feather_weights_shape():
    fp = np.zeros((50, 50), dtype=bool)
    fp[10:40, 10:40] = True
    w = feather_weights(fp, CELL, FEATHER)
    assert w[25, 25] == 1.0          # deep interior: pure drone
    assert w[0, 0] == 0.0            # outside footprint: pure satellite
    assert 0.0 < w[25, 10] <= 0.5    # at the edge: mostly satellite
    # monotonic non-decreasing moving inward along the center row
    row = w[25, 5:25]
    assert np.all(np.diff(row) >= -1e-9)


def test_seam_blend_is_monotonic_no_cliff():
    sat, drone, (r0, r1, c0, c1), tilt = _grids()
    fused, w = fuse(sat, drone, CELL, FEATHER)

    mid = (r0 + r1) // 2
    residual = fused[mid, :] - sat[mid, :]  # 0 outside, w*tilt inside

    # Satellite untouched outside the drone footprint
    assert np.allclose(residual[:c0], 0) and np.allclose(residual[c1:], 0)

    # No cliff: an unfeathered replacement would step |tilt_edge| = 3 m at the
    # seam; the feathered seam must step no more than amplitude*cell/feather
    # plus the tilt's own per-cell change.
    steps = np.abs(np.diff(residual))
    tilt_step = np.max(np.abs(np.diff(tilt)))
    tolerance = 3.0 * CELL / FEATHER + tilt_step + 1e-6
    assert steps.max() <= tolerance
    assert steps.max() < 3.0  # strictly better than the unfeathered cliff

    # Blend weight is monotonic across the seam (east side, moving inward)
    inward = w[mid, c0:c0 + 10]
    assert np.all(np.diff(inward) >= -1e-9)


def test_nodata_hole_does_not_bleed_into_blend():
    """A nodata hole inside the drone footprint must fall back to satellite
    exactly (weight 0), with the feather ramping around it — no NaN or bogus
    values bleeding into the fused surface."""
    sat, drone, (r0, r1, c0, c1), _ = _grids()
    drone[45:55, 45:55] = np.nan  # sensor dropout hole inside the footprint

    fused, w = fuse(sat, drone, CELL, FEATHER)

    # fused is finite wherever the satellite is finite
    assert np.isfinite(fused).all()
    # the hole is pure satellite
    assert np.allclose(fused[48:52, 48:52], sat[48:52, 48:52])
    assert np.all(w[45:55, 45:55] == 0.0)
    # weight ramps down toward the hole (no cliff at its rim)
    mid = 50
    ramp = w[mid, 40:45]  # approaching the hole from the west
    assert np.all(np.diff(ramp) <= 1e-9)
