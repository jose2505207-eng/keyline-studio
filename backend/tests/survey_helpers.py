"""Shared fixtures/helpers for the drone-survey test suite."""

from __future__ import annotations

import io
import os

from PIL import Image
from PIL.TiffImagePlugin import IFDRational


def make_jpeg_bytes(gps: bool = True, size=(64, 48), seed: int = 0,
                    camera: str = "DJI Mavic 3E") -> bytes:
    img = Image.new("RGB", size, ((seed * 37) % 255, 120, 80))
    exif = Image.Exif()
    make, _, model = camera.partition(" ")
    exif[271] = make
    exif[272] = model or make
    if gps:
        R = IFDRational
        exif[34853] = {1: "N", 2: (R(21, 1), R(27, 1), R(30 + seed % 20, 1)),
                       3: "W", 4: (R(104, 1), R(3, 1), R(seed % 50, 1))}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def make_jpeg(path: str, **kwargs) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(make_jpeg_bytes(**kwargs))
    return path


def aoi_inside_fake_dtm(margin_m: float = 20.0) -> dict:
    """A WGS84 AOI polygon fully inside the FakeProvider DTM footprint
    (EPSG:32613, origin 597000/2374000, 120x120 m)."""
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:32613", "EPSG:4326", always_xy=True)
    e0, e1 = 597000 + margin_m, 597120 - margin_m
    n0, n1 = 2374000 - 120 + margin_m, 2374000 - margin_m
    ring = [tr.transform(e, n) for e, n in
            [(e0, n0), (e1, n0), (e1, n1), (e0, n1), (e0, n0)]]
    return {"type": "Polygon", "coordinates": [[list(c) for c in ring]]}


VALID_GCP = (b"EPSG:32613\n"
             b"597010.0 2373990.0 1902.1 100.5 200.5 0000_a.jpg\n"
             b"597100.0 2373990.0 1909.7 3900.0 210.0 0001_b.jpg\n"
             b"597050.0 2373910.0 1905.3 2000.0 2900.0 0002_c.jpg\n")
