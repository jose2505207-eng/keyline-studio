"""Control-point georeferencing solvers: 2-point similarity and 3+-point
affine, against known synthetic pixel->UTM transforms including rotation."""

import math

import numpy as np
import pytest

from app.georef import GeorefError, apply_transform, fit


def reflected_similarity(s, theta_deg, tx, ty):
    """Ground-truth generator matching the solver's model: scale+rotation with
    the image-y-down -> northing-up reflection."""
    a = s * math.cos(math.radians(theta_deg))
    b = s * math.sin(math.radians(theta_deg))
    return np.array([[a, b, tx], [b, -a, ty]])


def test_two_point_similarity_recovers_rotated_transform():
    M_true = reflected_similarity(s=2.117, theta_deg=14.0,
                                  tx=712_000.0, ty=2_368_000.0)  # UTM 13N-ish
    pts_px = [(120.0, 80.0), (1400.0, 950.0)]
    points = []
    for px, py in pts_px:
        e, n = apply_transform(M_true, px, py)
        points.append({"px": px, "py": py, "e": float(e), "n": float(n)})

    M, rms = fit(points)
    assert rms < 1e-3  # sub-mm on meter-scale coords

    # a third, unseen point must map correctly
    e3, n3 = apply_transform(M_true, 640.0, 512.0)
    e3f, n3f = apply_transform(M, 640.0, 512.0)
    assert abs(e3 - e3f) < 1e-3 and abs(n3 - n3f) < 1e-3


def test_three_point_affine_exact_rotated_case():
    theta = math.radians(-9.0)
    M_true = np.array([
        [1.8 * math.cos(theta), 1.8 * math.sin(theta), 500_000.0],
        [1.8 * math.sin(theta), -1.8 * math.cos(theta), 2_400_000.0],
    ])
    pts_px = [(0.0, 0.0), (2000.0, 100.0), (300.0, 1500.0)]
    points = []
    for px, py in pts_px:
        e, n = apply_transform(M_true, px, py)
        points.append({"px": px, "py": py, "e": float(e), "n": float(n)})

    M, rms = fit(points)
    assert rms < 1e-3  # sub-mm on meter-scale coords
    e4, n4 = apply_transform(M_true, 987.0, 654.0)
    e4f, n4f = apply_transform(M, 987.0, 654.0)
    assert abs(e4 - e4f) < 1e-3 and abs(n4 - n4f) < 1e-3


def test_rms_reports_misfit():
    M_true = reflected_similarity(2.0, 0.0, 100_000.0, 2_000_000.0)
    points = []
    for px, py in [(0, 0), (1000, 0), (0, 1000), (1000, 1000)]:
        e, n = apply_transform(M_true, px, py)
        points.append({"px": px, "py": py, "e": float(e), "n": float(n)})
    points[0]["e"] += 40.0  # a bad click
    _, rms = fit(points)
    assert 5.0 < rms < 40.0


def test_requires_two_points():
    with pytest.raises(GeorefError):
        fit([{"px": 0, "py": 0, "e": 0, "n": 0}])
